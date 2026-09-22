"""Generation-fenced admission and sandbox-residency policy.

The implementation has no GPU/KV-cache scheduling. It supports the first Crate
milestone only: fixed, elapsed-only, or request-aware sandbox demotion;
selective SSD/zswap reclaim; speculative pre-restore; hazard-weighted wake
reserve; and an online latency breaker.
"""

from __future__ import annotations

import collections
import json
import math
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Self

from .execution import Execution
from .pool import ReadyPool
from .prediction import EmpiricalReturnEstimator, ReturnEstimate
from .scheduler import ReqState, ReqStatus
from .types import (
    AgentTask,
    CpuClass,
    DemotionRequest,
    DemotionResult,
    ReclaimMode,
    ReqId,
    ResidencyMode,
    RestoreHint,
    RestoreRequest,
    RestoreResult,
    SandboxId,
    Stage,
    StageContext,
    Tier,
)


@dataclass(frozen=True)
class CadenPolicyConfig:
    estimated_wss_bytes: int = 512 << 20
    fixed_dram_reserve_bytes: int = 2 << 30
    wake_reserve_per_waiter_bytes: int = 128 << 20
    wake_reserve_safety_bytes: int = 0
    wake_reserve_horizon_seconds: float = 0.25

    reclaim_grace_seconds: float = 1.0
    reclaim_tier: Tier = Tier.SSD
    reclaim_mode: ReclaimMode = ReclaimMode.BALANCED
    residency_mode: ResidencyMode = ResidencyMode.FIXED_GRACE
    sandbox_hot_reserve_bytes: int = 0
    restore_profile_hot_reserve_bytes: tuple[tuple[str, int], ...] = ()
    minimum_reclaim_bytes: int = 1 << 20
    minimum_cold_residency_seconds: float = 0.05
    minimum_memory_time_byte_seconds: float = 0.0
    estimated_demote_seconds: float = 0.06
    estimated_restore_seconds: float = 0.03
    maximum_early_wake_probability: float = 0.25

    prediction_interval_seconds: float = 0.05
    prediction_minimum_class_samples: int = 3
    prediction_maximum_samples: int = 256
    prediction_prior_mean_seconds: float = 1.0
    prediction_prior_weight: float = 2.0

    speculative_restore_enabled: bool = False
    speculative_restore_probability: float = 0.50
    speculative_restore_lead_seconds: float = 0.10
    speculative_restore_queue_margin_seconds: float = 0.02
    max_concurrent_speculative_restores: int = 1

    compression_max_wait_seconds: float = 0.0

    wake_latency_slo_seconds: float = 0.0
    wake_latency_p99_slo_seconds: float = 0.0
    turn_latency_slo_seconds: float = 0.0
    turn_latency_p99_slo_seconds: float = 0.0
    slo_window_samples: int = 100
    slo_minimum_samples: int = 20
    slo_cooldown_seconds: float = 30.0

    max_concurrent_admissions: int = 4
    max_concurrent_reclaims: int = 2
    max_concurrent_wakes: int = 2
    max_concurrent_movements: int = 2
    confirmed_movement_reserve: int = 0
    reclaim_enabled: bool = True
    max_queue_depth: int = 10_000

    def __post_init__(self) -> None:
        nonnegative_ints = {
            "estimated_wss_bytes": self.estimated_wss_bytes,
            "fixed_dram_reserve_bytes": self.fixed_dram_reserve_bytes,
            "wake_reserve_per_waiter_bytes": self.wake_reserve_per_waiter_bytes,
            "wake_reserve_safety_bytes": self.wake_reserve_safety_bytes,
            "sandbox_hot_reserve_bytes": self.sandbox_hot_reserve_bytes,
            "minimum_reclaim_bytes": self.minimum_reclaim_bytes,
            "max_queue_depth": self.max_queue_depth,
        }
        if any(value < 0 for value in nonnegative_ints.values()):
            raise ValueError("policy byte/count values must not be negative")
        profiles = [profile for profile, _ in self.restore_profile_hot_reserve_bytes]
        if any(not profile for profile in profiles):
            raise ValueError("restore-profile hot-reserve names must not be empty")
        if len(profiles) != len(set(profiles)):
            raise ValueError("restore-profile hot-reserve names must be unique")
        if any(value < 0 for _, value in self.restore_profile_hot_reserve_bytes):
            raise ValueError("restore-profile hot reserves must not be negative")
        positive_ints = {
            "prediction_minimum_class_samples": self.prediction_minimum_class_samples,
            "prediction_maximum_samples": self.prediction_maximum_samples,
            "slo_window_samples": self.slo_window_samples,
            "slo_minimum_samples": self.slo_minimum_samples,
            "max_concurrent_admissions": self.max_concurrent_admissions,
            "max_concurrent_reclaims": self.max_concurrent_reclaims,
            "max_concurrent_wakes": self.max_concurrent_wakes,
            "max_concurrent_movements": self.max_concurrent_movements,
            "max_concurrent_speculative_restores": self.max_concurrent_speculative_restores,
        }
        if any(value <= 0 for value in positive_ints.values()):
            raise ValueError("policy concurrency/sample values must be positive")
        nonnegative_floats = {
            "reclaim_grace_seconds": self.reclaim_grace_seconds,
            "wake_reserve_horizon_seconds": self.wake_reserve_horizon_seconds,
            "minimum_cold_residency_seconds": self.minimum_cold_residency_seconds,
            "minimum_memory_time_byte_seconds": self.minimum_memory_time_byte_seconds,
            "estimated_demote_seconds": self.estimated_demote_seconds,
            "estimated_restore_seconds": self.estimated_restore_seconds,
            "prediction_interval_seconds": self.prediction_interval_seconds,
            "speculative_restore_lead_seconds": self.speculative_restore_lead_seconds,
            "speculative_restore_queue_margin_seconds": self.speculative_restore_queue_margin_seconds,
            "compression_max_wait_seconds": self.compression_max_wait_seconds,
            "wake_latency_slo_seconds": self.wake_latency_slo_seconds,
            "wake_latency_p99_slo_seconds": self.wake_latency_p99_slo_seconds,
            "turn_latency_slo_seconds": self.turn_latency_slo_seconds,
            "turn_latency_p99_slo_seconds": self.turn_latency_p99_slo_seconds,
            "slo_cooldown_seconds": self.slo_cooldown_seconds,
        }
        if any(value < 0 for value in nonnegative_floats.values()):
            raise ValueError("policy durations/costs must not be negative")
        if self.prediction_interval_seconds <= 0:
            raise ValueError("prediction_interval_seconds must be positive")
        if self.prediction_prior_mean_seconds <= 0 or self.prediction_prior_weight <= 0:
            raise ValueError("prediction prior values must be positive")
        for name, value in {
            "maximum_early_wake_probability": self.maximum_early_wake_probability,
            "speculative_restore_probability": self.speculative_restore_probability,
        }.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.reclaim_tier is Tier.DRAM:
            raise ValueError("DRAM cannot be configured as a reclaim tier")
        if self.slo_minimum_samples > self.slo_window_samples:
            raise ValueError("SLO minimum samples cannot exceed window size")
        if not 0 <= self.confirmed_movement_reserve < self.max_concurrent_movements:
            raise ValueError("confirmed_movement_reserve must be in [0, max movements)")


@dataclass(frozen=True)
class DecisionEvent:
    timestamp_ns: int
    action: str
    request_id: ReqId | None = None
    sandbox_id: SandboxId | None = None
    stage: Stage | None = None
    duration_ns: int = 0
    bytes: int = 0
    detail: str = ""


@dataclass
class _Request:
    id: ReqId
    task: AgentTask
    status: ReqStatus
    sandbox_id: SandboxId | None = None
    stage: Stage | None = None
    generation: int = 0
    admitting: bool = False
    reclaiming: bool = False
    reclaimed: bool = False
    restoring: bool = False
    speculative_restoring: bool = False
    speculative_restored: bool = False
    commit_restore_required: bool = False
    demotion_attempted: bool = False
    reclaimed_bytes: int = 0
    last_resident_bytes: int = 0
    demoted_tier: Tier | None = None
    speculative_reserved_bytes: int = 0
    wake_slot: bool = False
    timer: threading.Timer | None = None
    wait_started_ns: int | None = None
    turn_started_ns: int | None = None
    request_class: str = "default"
    restore_profile: str = "default"
    pending_hint: RestoreHint | None = None
    hint_received_ns: int | None = None
    residency_lock: threading.Lock = field(default_factory=threading.Lock)


class _PriorityMovementGate:
    """Bound movement concurrency and prioritize confirmed wake work."""

    CONFIRMED = 0
    SPECULATIVE = 1
    RECLAIM = 2

    def __init__(self, capacity: int, confirmed_reserve: int) -> None:
        self.capacity = capacity
        self.confirmed_reserve = confirmed_reserve
        self._condition = threading.Condition()
        self._active = 0
        self._background_active = 0
        self._sequence = 0
        self._waiters: list[tuple[int, int]] = []

    def acquire(self, priority: int) -> int:
        started = time.monotonic_ns()
        with self._condition:
            waiter = (priority, self._sequence)
            self._sequence += 1
            self._waiters.append(waiter)
            while not self._eligible(waiter):
                self._condition.wait()
            self._waiters.remove(waiter)
            self._active += 1
            if priority != self.CONFIRMED:
                self._background_active += 1
        return time.monotonic_ns() - started

    def release(self, priority: int) -> None:
        with self._condition:
            self._active -= 1
            if priority != self.CONFIRMED:
                self._background_active -= 1
            self._condition.notify_all()

    def _eligible(self, waiter: tuple[int, int]) -> bool:
        priority, _ = waiter
        if self._active >= self.capacity:
            return False
        earlier_or_higher = min(self._waiters)
        if waiter != earlier_or_higher:
            return False
        if priority == self.CONFIRMED:
            return True
        background_capacity = self.capacity - self.confirmed_reserve
        return self._background_active < background_capacity


class StageAwareCaden:
    """FIFO admission plus predictive, bounded sandbox residency control."""

    def __init__(
        self,
        execution: Execution,
        config: CadenPolicyConfig | None = None,
        *,
        ready_pool: ReadyPool | None = None,
        request_id_factory: Callable[[], str] | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        estimator: EmpiricalReturnEstimator | None = None,
    ) -> None:
        self.execution = execution
        self.ready_pool = ready_pool
        self.config = config or CadenPolicyConfig()
        self._request_id_factory = request_id_factory or (
            lambda: f"req-{uuid.uuid4().hex[:16]}"
        )
        self._clock_ns = clock_ns
        self._monotonic_ns = monotonic_ns
        self._estimator = estimator or EmpiricalReturnEstimator(
            minimum_class_samples=self.config.prediction_minimum_class_samples,
            maximum_samples=self.config.prediction_maximum_samples,
            prior_mean_seconds=self.config.prediction_prior_mean_seconds,
            prior_weight=self.config.prediction_prior_weight,
        )
        self._requests: dict[ReqId, _Request] = {}
        self._sandbox_to_request: dict[SandboxId, ReqId] = {}
        self._queue: collections.deque[ReqId] = collections.deque()
        self._events: list[DecisionEvent] = []
        self._lock = threading.RLock()
        self._inflight_admissions = 0
        self._closed = False
        self._admission_executor = ThreadPoolExecutor(
            max_workers=self.config.max_concurrent_admissions,
            thread_name_prefix="caden-admit",
        )
        self._admission_futures: set[Future[None]] = set()
        self._wake_slots = threading.BoundedSemaphore(self.config.max_concurrent_wakes)
        self._movement_gate = _PriorityMovementGate(
            self.config.max_concurrent_movements,
            self.config.confirmed_movement_reserve,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.max_concurrent_reclaims,
            thread_name_prefix="caden-memory",
        )
        self._speculation_executor = ThreadPoolExecutor(
            max_workers=self.config.max_concurrent_speculative_restores,
            thread_name_prefix="caden-spec-restore",
        )
        self._futures: set[Future[None]] = set()
        self._speculation_futures: set[Future[None]] = set()
        self._demote_seconds: dict[Tier, collections.deque[float]] = {
            tier: collections.deque(maxlen=128)
            for tier in (Tier.SSD, Tier.COMPRESSED, Tier.CXL)
        }
        self._restore_seconds: dict[Tier, collections.deque[float]] = {
            tier: collections.deque(maxlen=128)
            for tier in (Tier.SSD, Tier.COMPRESSED, Tier.CXL)
        }
        self._wake_seconds: collections.deque[float] = collections.deque(
            maxlen=self.config.slo_window_samples
        )
        self._turn_seconds: collections.deque[float] = collections.deque(
            maxlen=self.config.slo_window_samples
        )
        self._breaker_until_ns = 0
        self._breaker_open = False
        self._speculative_reserved_bytes = 0

    def submit(self, task: AgentTask) -> ReqId:
        request_id = self._request_id_factory()
        with self._lock:
            self._ensure_open()
            if request_id in self._requests:
                raise ValueError(f"duplicate request ID {request_id!r}")
            if len(self._queue) >= self.config.max_queue_depth:
                self._requests[request_id] = _Request(
                    id=request_id,
                    task=task,
                    status=ReqStatus(ReqState.REJECTED, "queue capacity exceeded"),
                )
                self._event("reject", request_id=request_id, detail="queue capacity")
                return request_id
            self._requests[request_id] = _Request(
                id=request_id,
                task=task,
                status=ReqStatus(ReqState.QUEUED),
            )
            self._queue.append(request_id)
            self._event("queue", request_id=request_id)
        self.refresh_admission()
        return request_id

    def poll(self, req: ReqId) -> ReqStatus:
        with self._lock:
            request = self._requests.get(req)
            if request is None:
                raise KeyError(req)
            return ReqStatus(request.status.state, request.status.result)

    def request_sandbox(self, req: ReqId) -> SandboxId | None:
        with self._lock:
            request = self._requests.get(req)
            if request is None:
                raise KeyError(req)
            return request.sandbox_id

    def current_generation(self, sandbox: SandboxId) -> int:
        with self._lock:
            return self._request_for_sandbox(sandbox).generation

    def estimator_snapshot(self) -> dict[str, object]:
        return self._estimator.snapshot()

    def on_restore_hint(self, sandbox: SandboxId, hint: RestoreHint) -> None:
        with self._lock:
            request = self._request_for_sandbox(sandbox)
            if (
                request.status.state is not ReqState.RUNNING
                or request.stage is not Stage.LLM_WAIT
            ):
                self._event(
                    "restore_hint_reject",
                    request_id=request.id,
                    sandbox_id=sandbox,
                    detail="not waiting",
                )
                return
            if hint.generation != request.generation:
                self._event(
                    "restore_hint_stale",
                    request_id=request.id,
                    sandbox_id=sandbox,
                    detail=_detail(
                        hint_generation=hint.generation,
                        current_generation=request.generation,
                        hint_id=hint.hint_id,
                    ),
                )
                return
            request.pending_hint = hint
            request.hint_received_ns = self._monotonic_ns()
            if hint.profile:
                request.restore_profile = hint.profile
            generation = request.generation
            lead = self._restore_lead_seconds_locked(request.demoted_tier)
            delay = max(0.0, hint.horizon_seconds - lead) if request.reclaimed else 0.0
            self._event(
                "restore_hint",
                request_id=request.id,
                sandbox_id=sandbox,
                stage=Stage.LLM_WAIT,
                detail=_detail(
                    probability=hint.probability,
                    horizon_seconds=hint.horizon_seconds,
                    profile=hint.profile,
                    hint_id=hint.hint_id,
                    evaluation_delay_seconds=delay,
                ),
            )
            self._set_wait_timer_locked(request, generation, delay)

    def cancel(self, req: ReqId) -> None:
        sandbox: SandboxId | None = None
        release_wake = False
        with self._lock:
            request = self._requests.get(req)
            if request is None:
                raise KeyError(req)
            if request.status.state in {ReqState.DONE, ReqState.CANCELLED}:
                return
            self._cancel_timer_locked(request)
            if request.status.state is ReqState.QUEUED and not request.admitting:
                try:
                    self._queue.remove(req)
                except ValueError:
                    pass
            sandbox = request.sandbox_id
            request.generation += 1
            release_wake = request.wake_slot
            request.wake_slot = False
            request.status = ReqStatus(ReqState.CANCELLED, "cancelled")
            if sandbox is not None:
                self._sandbox_to_request.pop(sandbox, None)
            self._event("cancel", request_id=req, sandbox_id=sandbox)
        if sandbox is not None:
            with request.residency_lock:
                self._release(sandbox)
        if release_wake:
            self._wake_slots.release()
        self.refresh_admission()

    def on_report(
        self,
        sandbox: SandboxId,
        stage: Stage,
        context: StageContext | None = None,
    ) -> None:
        report_started_ns = self._monotonic_ns()
        self._acquire_wake_slot(sandbox, stage)
        release_wake = False
        restore = False
        observation: tuple[str, float] | None = None
        turn_duration: float | None = None
        with self._lock:
            request = self._request_for_sandbox(sandbox)
            if request.status.state is not ReqState.RUNNING:
                return
            previous_stage = request.stage
            request.generation += 1
            generation = request.generation
            request.stage = stage
            self._cancel_timer_locked(request)
            self._event(
                "stage",
                request_id=request.id,
                sandbox_id=sandbox,
                stage=stage,
                detail=_detail(generation=generation),
            )

            if stage is Stage.LLM_WAIT:
                request.wait_started_ns = report_started_ns
                request.turn_started_ns = None
                request.request_class = (
                    context.request_class if context else request.task.klass
                ) or "default"
                request.restore_profile = (
                    context.restore_profile if context else "default"
                ) or "default"
                request.demotion_attempted = False
                request.speculative_restored = False
                request.speculative_restoring = False
                request.commit_restore_required = False
                request.pending_hint = None
                request.hint_received_ns = None
                request.reclaimed_bytes = 0
                request.demoted_tier = None
                if self.config.reclaim_enabled:
                    if self.config.residency_mode is ResidencyMode.FIXED_GRACE:
                        self._set_fixed_reclaim_timer_locked(request, generation)
                    else:
                        self._set_wait_timer_locked(
                            request,
                            generation,
                            self.config.reclaim_grace_seconds,
                        )
                cpu_class = CpuClass.IDLE
            elif stage in {Stage.RESPONSE_WAKE, Stage.TOOL_BURST}:
                cpu_class = CpuClass.BOOST
                restore = (
                    request.reclaimed
                    or request.reclaiming
                    or request.speculative_restoring
                    or request.commit_restore_required
                )
                if (
                    previous_stage is Stage.LLM_WAIT
                    and request.wait_started_ns is not None
                ):
                    duration = max(
                        0.0,
                        (report_started_ns - request.wait_started_ns) / 1e9,
                    )
                    observation = (request.request_class, duration)
                    request.wait_started_ns = None
                if stage is Stage.RESPONSE_WAKE:
                    request.turn_started_ns = report_started_ns
            else:
                cpu_class = CpuClass.NORMAL
                if request.turn_started_ns is not None:
                    turn_duration = max(
                        0.0,
                        (report_started_ns - request.turn_started_ns) / 1e9,
                    )
                    request.turn_started_ns = None
            if stage in {Stage.LLM_WAIT, Stage.RESULT_PACK} and request.wake_slot:
                request.wake_slot = False
                release_wake = True

        if observation is not None:
            request_class, duration = observation
            self._estimator.observe(request_class, duration)
            self._event(
                "return_observe",
                request_id=request.id,
                sandbox_id=sandbox,
                stage=stage,
                duration_ns=round(duration * 1e9),
                detail=_detail(request_class=request_class),
            )
        if restore:
            self._restore_confirmed(request, sandbox, stage, generation)
        with self._lock:
            inactive = (
                request.status.state is not ReqState.RUNNING
                or self._sandbox_to_request.get(sandbox) != request.id
            )
            if not inactive:
                # Serialize the short cgroup write with cancel/complete so the
                # execution object cannot be destroyed between validation and use.
                self.execution.set_cpu(sandbox, cpu_class)
                self._event(
                    "cpu_class",
                    request_id=request.id,
                    sandbox_id=sandbox,
                    stage=stage,
                    detail=cpu_class.value,
                )
        if inactive:
            if release_wake:
                self._wake_slots.release()
            return
        if release_wake:
            self._wake_slots.release()
            self._event(
                "wake_release",
                request_id=request.id,
                sandbox_id=sandbox,
                stage=stage,
            )
        if stage is Stage.RESPONSE_WAKE:
            wake_seconds = max(0.0, (self._monotonic_ns() - report_started_ns) / 1e9)
            self._event(
                "wake_complete",
                request_id=request.id,
                sandbox_id=sandbox,
                stage=stage,
                duration_ns=round(wake_seconds * 1e9),
            )
            self._record_slo_sample("wake", wake_seconds)
        if turn_duration is not None:
            self._event(
                "turn_complete",
                request_id=request.id,
                sandbox_id=sandbox,
                stage=stage,
                duration_ns=round(turn_duration * 1e9),
            )
            self._record_slo_sample("turn", turn_duration)

    def complete(self, sandbox: SandboxId, result: str | None = None) -> None:
        release_wake = False
        with self._lock:
            request = self._request_for_sandbox(sandbox)
            self._cancel_timer_locked(request)
            request.generation += 1
            release_wake = request.wake_slot
            request.wake_slot = False
            request.status = ReqStatus(ReqState.DONE, result)
            self._sandbox_to_request.pop(sandbox, None)
            self._event("complete", request_id=request.id, sandbox_id=sandbox)
        with request.residency_lock:
            self._release(sandbox)
        if release_wake:
            self._wake_slots.release()
        self.refresh_admission()

    def refresh_admission(self) -> None:
        with self._lock:
            if self._closed:
                return
            while (
                self._queue
                and self._inflight_admissions < self.config.max_concurrent_admissions
            ):
                request_id = self._queue[0]
                wake_reserve = self._wake_reserve_locked()
                host = self.execution.host_stat()
                required = (
                    (self._inflight_admissions + 1) * self.config.estimated_wss_bytes
                    + self.config.fixed_dram_reserve_bytes
                    + wake_reserve
                )
                if host.dram_free_bytes < required:
                    self._event(
                        "admission_wait",
                        request_id=request_id,
                        bytes=required - host.dram_free_bytes,
                        detail=_detail(wake_reserve_bytes=wake_reserve),
                    )
                    return
                self._queue.popleft()
                request = self._requests[request_id]
                if request.status.state is not ReqState.QUEUED:
                    continue
                request.admitting = True
                self._inflight_admissions += 1
                future = self._admission_executor.submit(self._admit, request_id)
                self._admission_futures.add(future)
                future.add_done_callback(self._discard_admission_future)

    def events(self) -> list[DecisionEvent]:
        with self._lock:
            return list(self._events)

    def wait_for_background(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                futures = list(self._futures | self._speculation_futures)
            if not futures:
                return
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if remaining == 0:
                return
            wait(futures, timeout=remaining)
            with self._lock:
                if not (self._futures | self._speculation_futures):
                    return

    def wait_for_admission(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                futures = list(self._admission_futures)
                inflight = self._inflight_admissions
            if inflight == 0:
                return
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if remaining == 0:
                raise TimeoutError("Caden admission did not finish")
            wait(futures, timeout=remaining)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for request in self._requests.values():
                self._cancel_timer_locked(request)
        self._admission_executor.shutdown(wait=True, cancel_futures=False)
        self._executor.shutdown(wait=True, cancel_futures=True)
        # Every submitted speculation releases a DRAM reservation in its
        # finally block, including work made stale by shutdown.
        self._speculation_executor.shutdown(wait=True, cancel_futures=False)
        if self.ready_pool is not None:
            close_pool = getattr(self.ready_pool, "close", None)
            if close_pool is not None:
                close_pool()
        with self._lock:
            active = [
                request
                for request in self._requests.values()
                if request.status.state is ReqState.RUNNING
                and request.sandbox_id is not None
            ]
        for request in active:
            assert request.sandbox_id is not None
            try:
                self._release(request.sandbox_id)
                detail = "scheduler closed"
            except Exception as error:  # noqa: BLE001 - plugin boundary
                detail = f"scheduler closed; revoke failed: {error}"
            with self._lock:
                request.status = ReqStatus(ReqState.CANCELLED, detail)
                self._sandbox_to_request.pop(request.sandbox_id, None)
                release_wake = request.wake_slot
                request.wake_slot = False
            if release_wake:
                self._wake_slots.release()

    def _admit(self, request_id: ReqId) -> None:
        sandbox: SandboxId | None = None
        try:
            with self._lock:
                request = self._requests[request_id]
            if self.ready_pool is None:
                sandbox = self.execution.run(request.task, self.on_report)
            else:
                sandbox = self.ready_pool.acquire(request.task, self.on_report)
            with self._lock:
                if self._closed or request.status.state is ReqState.CANCELLED:
                    release = True
                else:
                    request.sandbox_id = sandbox
                    request.status = ReqStatus(ReqState.RUNNING)
                    self._sandbox_to_request[sandbox] = request_id
                    self._event("admit", request_id=request_id, sandbox_id=sandbox)
                    # Keep cancellation from racing the initial CPU-class write.
                    self.on_report(sandbox, Stage.RESULT_PACK)
                    release = False
            if release:
                self._release(sandbox)
        except Exception as error:  # noqa: BLE001 - execution plugin boundary
            if sandbox is not None:
                try:
                    self._release(sandbox)
                except Exception as cleanup_error:  # noqa: BLE001
                    self._event(
                        "admission_cleanup_error",
                        request_id=request_id,
                        sandbox_id=sandbox,
                        detail=str(cleanup_error),
                    )
            with self._lock:
                request = self._requests[request_id]
                if request.status.state is not ReqState.CANCELLED:
                    request.status = ReqStatus(ReqState.REJECTED, str(error))
                    self._event(
                        "admission_error",
                        request_id=request_id,
                        detail=str(error),
                    )
        finally:
            with self._lock:
                request = self._requests[request_id]
                request.admitting = False
                self._inflight_admissions -= 1
            self.refresh_admission()

    def _set_fixed_reclaim_timer_locked(
        self, request: _Request, generation: int
    ) -> None:
        timer = threading.Timer(
            self.config.reclaim_grace_seconds,
            self._submit_reclaim,
            args=(request.id, generation),
        )
        timer.daemon = True
        request.timer = timer
        timer.start()

    def _set_wait_timer_locked(
        self, request: _Request, generation: int, delay_seconds: float
    ) -> None:
        self._cancel_timer_locked(request)
        timer = threading.Timer(
            max(0.0, delay_seconds),
            self._submit_wait_evaluation,
            args=(request.id, generation),
        )
        timer.daemon = True
        request.timer = timer
        timer.start()

    def _cancel_timer_locked(self, request: _Request) -> None:
        if request.timer is not None:
            request.timer.cancel()
            request.timer = None

    def _submit_reclaim(self, request_id: ReqId, generation: int) -> None:
        with self._lock:
            if self._closed:
                return
            request = self._requests.get(request_id)
            if request is not None and request.generation == generation:
                request.timer = None
            future = self._executor.submit(self._reclaim, request_id, generation)
            self._track_future(future, speculative=False)

    def _submit_wait_evaluation(self, request_id: ReqId, generation: int) -> None:
        with self._lock:
            if self._closed:
                return
            request = self._requests.get(request_id)
            if request is not None and request.generation == generation:
                request.timer = None
            future = self._executor.submit(self._evaluate_wait, request_id, generation)
            self._track_future(future, speculative=False)

    def _evaluate_wait(self, request_id: ReqId, generation: int) -> None:
        with self._lock:
            request = self._valid_waiting_request_locked(request_id, generation)
            if request is None or request.wait_started_ns is None:
                return
            elapsed = max(0.0, (self._monotonic_ns() - request.wait_started_ns) / 1e9)
            request_class = request.request_class
            restore_profile = request.restore_profile
            reclaimed = request.reclaimed
            reclaiming = request.reclaiming
            hint = request.pending_hint
            hint_remaining = (
                max(
                    0.0,
                    hint.horizon_seconds
                    - (self._monotonic_ns() - request.hint_received_ns) / 1e9,
                )
                if hint is not None and request.hint_received_ns is not None
                else None
            )
            breaker_open = self._breaker_is_open_locked()
            lead = self._restore_lead_seconds_locked(request.demoted_tier)
        estimate = self._estimate_return(request_class, elapsed, lead)
        effective_probability = estimate.probability_within_horizon
        if hint is not None and hint_remaining is not None and hint_remaining <= lead:
            effective_probability = max(effective_probability, hint.probability)
        self._event(
            "hazard",
            request_id=request_id,
            sandbox_id=request.sandbox_id,
            stage=Stage.LLM_WAIT,
            detail=_detail(
                request_class=request_class,
                elapsed_seconds=elapsed,
                horizon_seconds=lead,
                probability=effective_probability,
                expected_remaining_seconds=estimate.expected_remaining_seconds,
                samples=estimate.sample_count,
                survivors=estimate.survivor_count,
                source=estimate.source,
                breaker_open=breaker_open,
            ),
        )
        if reclaiming:
            # A speculative preparation must never overtake a demotion that has
            # not committed yet. Confirmed wakes instead fence it by changing
            # the stage generation.
            self._reschedule_wait_evaluation(request_id, generation)
            return
        if reclaimed:
            if breaker_open or (
                self.config.speculative_restore_enabled
                and (
                    effective_probability >= self.config.speculative_restore_probability
                    or estimate.expected_remaining_seconds <= lead
                )
            ):
                self._submit_speculative_restore(
                    request_id,
                    generation,
                    reason="breaker" if breaker_open else "hazard",
                )
            else:
                self._reschedule_wait_evaluation(request_id, generation)
            return
        if breaker_open or request.demotion_attempted:
            return

        assert request.sandbox_id is not None
        try:
            stat = self.execution.stat(request.sandbox_id)
        except Exception as error:  # noqa: BLE001 - observation plugin boundary
            self._event(
                "reclaim_skip",
                request_id=request_id,
                sandbox_id=request.sandbox_id,
                stage=Stage.LLM_WAIT,
                detail=f"stat error: {error}",
            )
            return
        hot_reserve_bytes = self._hot_reserve_bytes(restore_profile)
        target = max(0, stat.mem_dram_bytes - hot_reserve_bytes)
        if target < self.config.minimum_reclaim_bytes:
            self._event(
                "reclaim_skip",
                request_id=request_id,
                sandbox_id=request.sandbox_id,
                stage=Stage.LLM_WAIT,
                bytes=target,
                detail=_detail(
                    reason="below minimum reclaim bytes",
                    restore_profile=restore_profile,
                    hot_reserve_bytes=hot_reserve_bytes,
                    resident_bytes=stat.mem_dram_bytes,
                ),
            )
            return
        tier = self._select_tier(estimate.expected_remaining_seconds)
        with self._lock:
            demote_seconds = self._latency_p95_locked(
                self._demote_seconds[tier], self.config.estimated_demote_seconds
            )
            restore_seconds = self._restore_lead_seconds_locked(tier)
        risk_horizon = demote_seconds + restore_seconds
        risk = self._estimate_return(request_class, elapsed, risk_horizon)
        early_probability = risk.probability_within_horizon
        if (
            hint is not None
            and hint_remaining is not None
            and hint_remaining <= risk_horizon
        ):
            early_probability = max(early_probability, hint.probability)
        cold_seconds = max(
            0.0,
            estimate.expected_remaining_seconds - demote_seconds - restore_seconds,
        )
        memory_time = target * cold_seconds
        eligible = (
            cold_seconds >= self.config.minimum_cold_residency_seconds
            and early_probability <= self.config.maximum_early_wake_probability
            and memory_time >= self.config.minimum_memory_time_byte_seconds
        )
        detail = _detail(
            eligible=eligible,
            tier=tier.value,
            target_bytes=target,
            resident_bytes=stat.mem_dram_bytes,
            restore_profile=restore_profile,
            hot_reserve_bytes=hot_reserve_bytes,
            expected_remaining_seconds=estimate.expected_remaining_seconds,
            estimated_demote_seconds=demote_seconds,
            restore_lead_seconds=restore_seconds,
            predicted_cold_seconds=cold_seconds,
            early_wake_probability=early_probability,
            memory_time_byte_seconds=memory_time,
        )
        self._event(
            "reclaim_decision",
            request_id=request_id,
            sandbox_id=request.sandbox_id,
            stage=Stage.LLM_WAIT,
            bytes=target,
            detail=detail,
        )
        if eligible:
            self._reclaim(
                request_id,
                generation,
                tier=tier,
                target_bytes=target,
                min_resident_bytes=hot_reserve_bytes,
                decision_detail=detail,
            )

    def _estimate_return(
        self, request_class: str, elapsed_seconds: float, horizon_seconds: float
    ) -> ReturnEstimate:
        return self._estimator.estimate(
            request_class,
            elapsed_seconds,
            horizon_seconds,
            request_aware=self.config.residency_mode is ResidencyMode.REQUEST_AWARE,
        )

    def _hot_reserve_bytes(self, restore_profile: str) -> int:
        return dict(self.config.restore_profile_hot_reserve_bytes).get(
            restore_profile, self.config.sandbox_hot_reserve_bytes
        )

    def _select_tier(self, expected_remaining_seconds: float) -> Tier:
        if (
            self.config.compression_max_wait_seconds > 0
            and expected_remaining_seconds <= self.config.compression_max_wait_seconds
        ):
            return Tier.COMPRESSED
        return self.config.reclaim_tier

    def _reclaim(
        self,
        request_id: ReqId,
        generation: int,
        *,
        tier: Tier | None = None,
        target_bytes: int | None = None,
        min_resident_bytes: int | None = None,
        decision_detail: str = "",
    ) -> None:
        selected_tier = tier or self.config.reclaim_tier
        with self._lock:
            request = self._valid_waiting_request_locked(request_id, generation)
            if request is None or request.sandbox_id is None or request.reclaiming:
                return
            if self._breaker_is_open_locked():
                self._event(
                    "reclaim_skip",
                    request_id=request_id,
                    sandbox_id=request.sandbox_id,
                    stage=Stage.LLM_WAIT,
                    detail="SLO breaker open",
                )
                return
            request.reclaiming = True
            request.reclaimed = True
            request.demotion_attempted = True
            request.demoted_tier = selected_tier
            sandbox = request.sandbox_id
            hot_reserve_bytes = (
                self._hot_reserve_bytes(request.restore_profile)
                if min_resident_bytes is None
                else min_resident_bytes
            )
        reclaim_mode = (
            ReclaimMode.ANON_ONLY
            if selected_tier is Tier.COMPRESSED
            else self.config.reclaim_mode
        )
        started = self._monotonic_ns()
        result: DemotionResult | None = None
        error_detail = ""
        stale = False
        movement_acquired = False
        try:
            movement_queue_ns = self._movement_gate.acquire(
                _PriorityMovementGate.RECLAIM
            )
            movement_acquired = True
            self._event(
                "movement_admit",
                request_id=request_id,
                sandbox_id=sandbox,
                stage=Stage.LLM_WAIT,
                duration_ns=movement_queue_ns,
                detail="reclaim",
            )
            with request.residency_lock:
                with self._lock:
                    stale = (
                        request.generation != generation
                        or request.stage is not Stage.LLM_WAIT
                    )
                if not stale:
                    result = self._execution_demote(
                        sandbox,
                        DemotionRequest(
                            tier=selected_tier,
                            target_bytes=target_bytes,
                            min_resident_bytes=hot_reserve_bytes,
                            reclaim_mode=reclaim_mode,
                            generation=generation,
                        ),
                    )
        except Exception as error:  # noqa: BLE001 - movement plugin boundary
            error_detail = str(error)
        finally:
            if movement_acquired:
                self._movement_gate.release(_PriorityMovementGate.RECLAIM)
            duration_ns = self._monotonic_ns() - started
            if not stale:
                with self._lock:
                    self._demote_seconds[selected_tier].append(duration_ns / 1e9)
            with self._lock:
                request.reclaiming = False
                if stale:
                    request.reclaimed = False
                elif result is not None:
                    request.reclaimed_bytes = result.reclaimed_bytes
                    request.last_resident_bytes = result.after_bytes
                action = (
                    "reclaim_stale"
                    if stale
                    else ("reclaim_error" if error_detail else "reclaim")
                )
                detail = error_detail
                if result is not None:
                    detail = _detail(
                        tier=result.tier.value,
                        reclaim_mode=result.reclaim_mode.value,
                        requested_bytes=result.requested_bytes,
                        before_bytes=result.before_bytes,
                        after_bytes=result.after_bytes,
                        swap_delta_bytes=result.swap_delta_bytes,
                        compressed_delta_bytes=result.compressed_delta_bytes,
                        decision=decision_detail,
                        backend=result.backend,
                        eligible_bytes=result.eligible_bytes,
                        stored_bytes=result.stored_bytes,
                        released_bytes=result.released_bytes,
                        file_reclaimed_bytes=result.file_reclaimed_bytes,
                        partial=result.partial,
                    )
                self._event(
                    action,
                    request_id=request_id,
                    sandbox_id=sandbox,
                    stage=Stage.LLM_WAIT,
                    duration_ns=duration_ns,
                    bytes=result.reclaimed_bytes if result is not None else 0,
                    detail=detail,
                )
        if not stale and self.config.residency_mode is not ResidencyMode.FIXED_GRACE:
            self._reschedule_wait_evaluation(request_id, generation)

    def _reschedule_wait_evaluation(self, request_id: ReqId, generation: int) -> None:
        with self._lock:
            request = self._valid_waiting_request_locked(request_id, generation)
            if request is None or self._closed:
                return
            self._set_wait_timer_locked(
                request,
                generation,
                self.config.prediction_interval_seconds,
            )

    def _submit_speculative_restore(
        self, request_id: ReqId, generation: int, *, reason: str
    ) -> None:
        if getattr(self.execution, "restore_selective", None) is None:
            self._event(
                "spec_restore_skip",
                request_id=request_id,
                detail="legacy execution plugin cannot guarantee no-dispatch preparation",
            )
            return
        try:
            host_free_bytes = self.execution.host_stat().dram_free_bytes
        except Exception as error:  # noqa: BLE001 - observation plugin boundary
            self._event(
                "spec_restore_capacity_skip",
                request_id=request_id,
                detail=f"host stat error: {error}",
            )
            return
        with self._lock:
            request = self._valid_waiting_request_locked(request_id, generation)
            if (
                request is None
                or request.sandbox_id is None
                or request.speculative_restoring
                or not request.reclaimed
            ):
                return
            footprint = request.reclaimed_bytes or self.config.estimated_wss_bytes
            protected = (
                self.config.fixed_dram_reserve_bytes
                + self._wake_reserve_locked()
                + self._speculative_reserved_bytes
            )
            required = footprint + protected
            if host_free_bytes < required:
                self._event(
                    "spec_restore_capacity_skip",
                    request_id=request_id,
                    sandbox_id=request.sandbox_id,
                    stage=Stage.LLM_WAIT,
                    bytes=required - host_free_bytes,
                    detail=_detail(
                        host_free_bytes=host_free_bytes,
                        footprint_bytes=footprint,
                        protected_bytes=protected,
                        reason=reason,
                    ),
                )
                self._set_wait_timer_locked(
                    request,
                    generation,
                    self.config.prediction_interval_seconds,
                )
                return
            request.speculative_restoring = True
            request.speculative_reserved_bytes = footprint
            self._speculative_reserved_bytes += footprint
            try:
                future = self._speculation_executor.submit(
                    self._restore_speculative,
                    request_id,
                    generation,
                    reason,
                )
            except RuntimeError as error:
                self._release_speculative_reservation_locked(request)
                request.speculative_restoring = False
                self._event(
                    "spec_restore_error",
                    request_id=request_id,
                    sandbox_id=request.sandbox_id,
                    stage=Stage.LLM_WAIT,
                    detail=f"submit failed: {error}",
                )
                return
            self._track_future(future, speculative=True)
            self._event(
                "spec_restore_submit",
                request_id=request_id,
                sandbox_id=request.sandbox_id,
                stage=Stage.LLM_WAIT,
                bytes=footprint,
                detail=_detail(reason=reason, protected_bytes=protected),
            )

    def _restore_speculative(
        self, request_id: ReqId, generation: int, reason: str
    ) -> None:
        with self._lock:
            request = self._requests.get(request_id)
            if request is None or request.sandbox_id is None:
                return
            sandbox = request.sandbox_id
            profile = request.restore_profile
            restore_tier = request.demoted_tier or self.config.reclaim_tier
        started = self._monotonic_ns()
        result: RestoreResult | None = None
        stale = False
        error_detail = ""
        movement_acquired = False
        try:
            movement_queue_ns = self._movement_gate.acquire(
                _PriorityMovementGate.SPECULATIVE
            )
            movement_acquired = True
            self._event(
                "movement_admit",
                request_id=request_id,
                sandbox_id=sandbox,
                stage=Stage.LLM_WAIT,
                duration_ns=movement_queue_ns,
                detail="speculative_restore",
            )
            with request.residency_lock:
                with self._lock:
                    stale = (
                        request.generation != generation
                        or request.stage is not Stage.LLM_WAIT
                        or not request.reclaimed
                    )
                if not stale:
                    result = self._execution_restore(
                        sandbox,
                        RestoreRequest(
                            profile=profile, speculative=True, generation=generation
                        ),
                    )
                    with self._lock:
                        request.reclaimed = False
                        request.speculative_restored = True
                        request.commit_restore_required = not result.ready_for_dispatch
                        if result.ready_for_dispatch:
                            request.demoted_tier = None
        except Exception as error:  # noqa: BLE001 - movement plugin boundary
            error_detail = str(error)
        finally:
            if movement_acquired:
                self._movement_gate.release(_PriorityMovementGate.SPECULATIVE)
            duration_ns = self._monotonic_ns() - started
            with self._lock:
                self._release_speculative_reservation_locked(request)
                request.speculative_restoring = False
                if result is not None:
                    self._restore_seconds[restore_tier].append(duration_ns / 1e9)
                self._event(
                    "spec_restore_stale"
                    if stale
                    else ("spec_restore_error" if error_detail else "spec_restore"),
                    request_id=request_id,
                    sandbox_id=sandbox,
                    stage=Stage.LLM_WAIT,
                    duration_ns=duration_ns,
                    bytes=(
                        max(
                            result.resident_delta_bytes,
                            result.prefetched_bytes + result.advised_bytes,
                        )
                        if result is not None
                        else 0
                    ),
                    detail=error_detail
                    or _detail(
                        reason=reason,
                        profile=profile,
                        tier=restore_tier.value,
                        swap_delta_bytes=(
                            result.swap_delta_bytes if result is not None else 0
                        ),
                        compressed_delta_bytes=(
                            result.compressed_delta_bytes if result is not None else 0
                        ),
                        prefetched_bytes=(
                            result.prefetched_bytes if result is not None else 0
                        ),
                        advised_bytes=(
                            result.advised_bytes if result is not None else 0
                        ),
                        ready_for_dispatch=(
                            result.ready_for_dispatch if result is not None else False
                        ),
                    ),
                )

    def _restore_confirmed(
        self,
        request: _Request,
        sandbox: SandboxId,
        stage: Stage,
        generation: int,
    ) -> None:
        started = self._monotonic_ns()
        detail = ""
        result: RestoreResult | None = None
        reused = False
        stale = False
        restore_tier = request.demoted_tier or self.config.reclaim_tier
        movement_acquired = False
        try:
            movement_queue_ns = self._movement_gate.acquire(
                _PriorityMovementGate.CONFIRMED
            )
            movement_acquired = True
            self._event(
                "movement_admit",
                request_id=request.id,
                sandbox_id=sandbox,
                stage=stage,
                duration_ns=movement_queue_ns,
                detail="confirmed_restore",
            )
            with request.residency_lock:
                with self._lock:
                    stale = (
                        request.status.state is not ReqState.RUNNING
                        or request.generation != generation
                        or self._sandbox_to_request.get(sandbox) != request.id
                    )
                    needs_restore = not stale and (
                        request.reclaimed or request.commit_restore_required
                    )
                    request.restoring = needs_restore
                if needs_restore:
                    result = self._execution_restore(
                        sandbox,
                        RestoreRequest(
                            profile=request.restore_profile,
                            speculative=False,
                            generation=generation,
                        ),
                    )
                    with self._lock:
                        request.reclaimed = False
                        request.speculative_restoring = False
                        request.commit_restore_required = False
                        request.demoted_tier = None
                elif not stale:
                    request.demoted_tier = None
                    reused = True
        except Exception as error:
            detail = str(error)
            raise
        finally:
            if movement_acquired:
                self._movement_gate.release(_PriorityMovementGate.CONFIRMED)
            duration_ns = self._monotonic_ns() - started
            with self._lock:
                request.restoring = False
                if result is not None:
                    self._restore_seconds[restore_tier].append(duration_ns / 1e9)
                self._event(
                    "restore_stale"
                    if stale
                    else (
                        "restore_reused"
                        if reused
                        else ("restore_error" if detail else "restore")
                    ),
                    request_id=request.id,
                    sandbox_id=sandbox,
                    stage=stage,
                    duration_ns=duration_ns,
                    bytes=result.resident_delta_bytes if result is not None else 0,
                    detail=detail
                    or _detail(
                        profile=request.restore_profile,
                        tier=restore_tier.value,
                        speculative_prepared=request.speculative_restored,
                        swap_delta_bytes=(
                            result.swap_delta_bytes if result is not None else 0
                        ),
                        compressed_delta_bytes=(
                            result.compressed_delta_bytes if result is not None else 0
                        ),
                    ),
                )

    def _release_speculative_reservation_locked(self, request: _Request) -> None:
        reserved = request.speculative_reserved_bytes
        request.speculative_reserved_bytes = 0
        self._speculative_reserved_bytes = max(
            0, self._speculative_reserved_bytes - reserved
        )

    def _execution_demote(
        self, sandbox: SandboxId, request: DemotionRequest
    ) -> DemotionResult:
        selective = getattr(self.execution, "demote_selective", None)
        if selective is not None:
            return selective(sandbox, request)
        # Compatibility seam for pre-selective execution plugins. Such plugins
        # cannot promise a hot floor, so predictive campaigns must report the
        # fallback event and should not credit selective reclaim.
        reclaimed = self.execution.demote(sandbox, request.tier)
        self._event(
            "selective_reclaim_fallback",
            sandbox_id=sandbox,
            bytes=reclaimed,
            detail="legacy execution plugin",
        )
        return DemotionResult(
            requested_bytes=request.target_bytes or reclaimed,
            reclaimed_bytes=reclaimed,
            before_bytes=reclaimed,
            after_bytes=0,
            tier=request.tier,
            reclaim_mode=request.reclaim_mode,
        )

    def _execution_restore(
        self, sandbox: SandboxId, request: RestoreRequest
    ) -> RestoreResult:
        selective = getattr(self.execution, "restore_selective", None)
        if selective is not None:
            return selective(sandbox, request)
        if request.speculative:
            return RestoreResult(
                ready_for_dispatch=False,
                profile=request.profile,
            )
        self.execution.restore(sandbox)
        self._event(
            "selective_restore_fallback",
            sandbox_id=sandbox,
            detail="legacy execution plugin",
        )
        return RestoreResult(profile=request.profile)

    def _wake_reserve_locked(self) -> int:
        waiting = [
            request
            for request in self._requests.values()
            if request.stage is Stage.LLM_WAIT
            and request.status.state is ReqState.RUNNING
        ]
        if self.config.residency_mode is ResidencyMode.FIXED_GRACE:
            return len(waiting) * self.config.wake_reserve_per_waiter_bytes
        now = self._monotonic_ns()
        reserve = self.config.wake_reserve_safety_bytes
        for request in waiting:
            if request.wait_started_ns is None:
                reserve += self.config.wake_reserve_per_waiter_bytes
                continue
            elapsed = max(0.0, (now - request.wait_started_ns) / 1e9)
            estimate = self._estimate_return(
                request.request_class,
                elapsed,
                self.config.wake_reserve_horizon_seconds,
            )
            footprint = request.reclaimed_bytes or request.last_resident_bytes
            if footprint <= 0:
                footprint = self.config.estimated_wss_bytes
            if estimate.source in {"prior", "warmup"}:
                reserve += max(
                    self.config.wake_reserve_per_waiter_bytes,
                    math.ceil(estimate.probability_within_horizon * footprint),
                )
            else:
                reserve += math.ceil(estimate.probability_within_horizon * footprint)
        return reserve

    def _record_slo_sample(self, kind: str, duration_seconds: float) -> None:
        with self._lock:
            if kind == "wake":
                values = self._wake_seconds
                thresholds = (
                    (0.95, self.config.wake_latency_slo_seconds),
                    (0.99, self.config.wake_latency_p99_slo_seconds),
                )
            else:
                values = self._turn_seconds
                thresholds = (
                    (0.95, self.config.turn_latency_slo_seconds),
                    (0.99, self.config.turn_latency_p99_slo_seconds),
                )
            values.append(duration_seconds)
            if len(values) < self.config.slo_minimum_samples:
                return
            violations: list[tuple[float, float, float, float]] = []
            for quantile, threshold in thresholds:
                if threshold <= 0:
                    continue
                observed = _percentile(list(values), quantile)
                if observed > threshold:
                    violations.append(
                        (observed / threshold, quantile, observed, threshold)
                    )
            if not violations:
                return
            _, quantile, observed, threshold = max(violations)
            now = self._monotonic_ns()
            self._breaker_until_ns = max(
                self._breaker_until_ns,
                now + round(self.config.slo_cooldown_seconds * 1e9),
            )
            first_open = not self._breaker_open
            self._breaker_open = True
            self._event(
                "slo_breaker_open" if first_open else "slo_breaker_extend",
                duration_ns=round(observed * 1e9),
                detail=_detail(
                    metric=kind,
                    quantile=quantile,
                    observed_seconds=observed,
                    threshold_seconds=threshold,
                    samples=len(values),
                    cooldown_seconds=self.config.slo_cooldown_seconds,
                ),
            )
            for request in self._requests.values():
                if (
                    request.stage is Stage.LLM_WAIT
                    and request.status.state is ReqState.RUNNING
                    and request.reclaimed
                ):
                    self._set_wait_timer_locked(request, request.generation, 0.0)

    def _breaker_is_open_locked(self) -> bool:
        if not self._breaker_open:
            return False
        if self._monotonic_ns() < self._breaker_until_ns:
            return True
        self._breaker_open = False
        self._breaker_until_ns = 0
        self._event("slo_breaker_close")
        return False

    def _restore_lead_seconds_locked(self, tier: Tier | None = None) -> float:
        values: Iterable[float]
        if tier is None:
            values = (
                value
                for tier_values in self._restore_seconds.values()
                for value in tier_values
            )
        else:
            values = self._restore_seconds[tier]
        observed = self._latency_p95_locked(
            values, self.config.estimated_restore_seconds
        )
        return max(
            self.config.speculative_restore_lead_seconds,
            observed + self.config.speculative_restore_queue_margin_seconds,
        )

    @staticmethod
    def _latency_p95_locked(values: Iterable[float], fallback: float) -> float:
        samples = list(values)
        return _percentile(samples, 0.95) if samples else fallback

    def _valid_waiting_request_locked(
        self, request_id: ReqId, generation: int
    ) -> _Request | None:
        request = self._requests.get(request_id)
        if (
            request is None
            or request.generation != generation
            or request.stage is not Stage.LLM_WAIT
            or request.status.state is not ReqState.RUNNING
            or request.sandbox_id is None
        ):
            return None
        return request

    def _acquire_wake_slot(self, sandbox: SandboxId, stage: Stage) -> None:
        if stage not in {Stage.RESPONSE_WAKE, Stage.TOOL_BURST}:
            return
        with self._lock:
            request_id = self._sandbox_to_request.get(sandbox)
            if request_id is None:
                return
            request = self._requests[request_id]
            if request.status.state is not ReqState.RUNNING or request.wake_slot:
                return
        started = self._monotonic_ns()
        self._wake_slots.acquire()
        with self._lock:
            current_id = self._sandbox_to_request.get(sandbox)
            if (
                current_id != request_id
                or request.status.state is not ReqState.RUNNING
                or request.wake_slot
            ):
                release = True
            else:
                request.wake_slot = True
                release = False
                self._event(
                    "wake_admit",
                    request_id=request_id,
                    sandbox_id=sandbox,
                    stage=stage,
                    duration_ns=self._monotonic_ns() - started,
                )
        if release:
            self._wake_slots.release()

    def _track_future(self, future: Future[None], *, speculative: bool) -> None:
        target = self._speculation_futures if speculative else self._futures
        target.add(future)
        future.add_done_callback(
            self._discard_speculation_future if speculative else self._discard_future
        )

    def _discard_future(self, future: Future[None]) -> None:
        with self._lock:
            self._futures.discard(future)

    def _discard_speculation_future(self, future: Future[None]) -> None:
        with self._lock:
            self._speculation_futures.discard(future)

    def _discard_admission_future(self, future: Future[None]) -> None:
        with self._lock:
            self._admission_futures.discard(future)

    def _request_for_sandbox(self, sandbox: SandboxId) -> _Request:
        request_id = self._sandbox_to_request.get(sandbox)
        if request_id is None:
            raise KeyError(sandbox)
        return self._requests[request_id]

    def _event(
        self,
        action: str,
        *,
        request_id: ReqId | None = None,
        sandbox_id: SandboxId | None = None,
        stage: Stage | None = None,
        duration_ns: int = 0,
        bytes: int = 0,
        detail: str = "",
    ) -> None:
        event = DecisionEvent(
            timestamp_ns=self._clock_ns(),
            action=action,
            request_id=request_id,
            sandbox_id=sandbox_id,
            stage=stage,
            duration_ns=duration_ns,
            bytes=bytes,
            detail=detail,
        )
        with self._lock:
            self._events.append(event)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Caden scheduler is closed")

    def _release(self, sandbox: SandboxId) -> None:
        if self.ready_pool is None:
            self.execution.revoke(sandbox)
        else:
            self.ready_pool.release(sandbox)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _detail(**values: object) -> str:
    return json.dumps(values, sort_keys=True, separators=(",", ":"))


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]
