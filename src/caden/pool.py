"""Bounded one-shot ready pool for native SandboxFS sandboxes.

Prepared sandboxes may be leased once and are always destroyed on release.  No
tenant filesystem or process state is ever returned to another request.
"""

from __future__ import annotations

import collections
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

from .execution import StageReport
from .sandboxfs_backend import SandboxFSExecution
from .types import AgentTask, SandboxId


class ReadyPool(Protocol):
    def acquire(self, task: AgentTask, on_report: StageReport) -> SandboxId: ...
    def release(self, sandbox: SandboxId) -> None: ...


@dataclass(frozen=True)
class ReadyPoolConfig:
    minimum_ready: int = 0
    target_ready: int = 1
    maximum_ready: int = 4
    estimated_ready_bytes: int = 32 << 20
    dram_reserve_bytes: int = 2 << 30

    def __post_init__(self) -> None:
        if not (0 <= self.minimum_ready <= self.target_ready <= self.maximum_ready):
            raise ValueError(
                "ready counts must satisfy 0 <= minimum <= target <= maximum"
            )
        if self.estimated_ready_bytes < 0 or self.dram_reserve_bytes < 0:
            raise ValueError("pool memory values must not be negative")


@dataclass(frozen=True)
class PoolEvent:
    timestamp_ns: int
    action: str
    base: str
    sandbox_id: SandboxId | None = None
    ready: int = 0
    detail: str = ""


class ElasticReadyPool:
    """Memory-bounded asynchronous ready reserve grouped by base name."""

    def __init__(
        self,
        execution: SandboxFSExecution,
        config: ReadyPoolConfig | None = None,
        *,
        pending_bases: list[str] | None = None,
    ) -> None:
        self.execution = execution
        self.config = config or ReadyPoolConfig()
        self._ready: dict[str, collections.deque[SandboxId]] = {}
        self._base_by_sandbox: dict[SandboxId, str] = {}
        self._leased: set[SandboxId] = set()
        self._events: list[PoolEvent] = []
        self._refills: dict[str, Future[None]] = {}
        self._lock = threading.RLock()
        self._closed = False
        # Only already-arrived requests belong here. None preserves the original
        # per-base replenishment policy; an empty list means no remaining demand.
        self._pending_bases = list(pending_bases) if pending_bases is not None else None
        self._demand_refill_again = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="caden-pool")

    def start(self, base: str) -> None:
        self._schedule_refill(base, self.config.target_ready)

    def add_pending(self, base: str) -> None:
        """Notify one actually arrived request; never a future trace schedule."""
        with self._lock:
            self._ensure_open()
            if self._pending_bases is None:
                raise RuntimeError("arrival notifications require a demand-aware pool")
            self._pending_bases.append(base)
            self._event("demand_arrived", base)
        self._schedule_refill(base, self.config.target_ready)

    def acquire(self, task: AgentTask, on_report: StageReport) -> SandboxId:
        with self._lock:
            self._ensure_open()
            if self._pending_bases is not None:
                # Consume at admission start, so an in-flight precreation cannot
                # leave an unused duplicate after an on-demand miss.
                self._pending_bases.remove(task.repo)
            queue = self._ready.setdefault(task.repo, collections.deque())
            sandbox = queue.popleft() if queue else None
            if sandbox is not None:
                self._leased.add(sandbox)
                self.execution.assign(sandbox, task, on_report)
                self._event("hit", task.repo, sandbox)
        if sandbox is None:
            sandbox = self.execution.run(task, on_report)
            with self._lock:
                self._leased.add(sandbox)
                self._base_by_sandbox[sandbox] = task.repo
                self._event("miss", task.repo, sandbox)
        self._schedule_refill(task.repo, self.config.target_ready)
        return sandbox

    def release(self, sandbox: SandboxId) -> None:
        with self._lock:
            base = self._base_by_sandbox.get(sandbox, "")
            self._leased.discard(sandbox)
        self.execution.revoke(sandbox)
        with self._lock:
            self._base_by_sandbox.pop(sandbox, None)
            self._event("destroy_lease", base, sandbox)
        if base:
            self._schedule_refill(base, self.config.target_ready)

    def trim(self, target_ready: int | None = None) -> None:
        target = self.config.minimum_ready if target_ready is None else target_ready
        if target < 0:
            raise ValueError("target_ready must not be negative")
        victims: list[tuple[str, SandboxId]] = []
        with self._lock:
            for base, queue in self._ready.items():
                while len(queue) > target:
                    victims.append((base, queue.pop()))
        for base, sandbox in victims:
            self.execution.revoke(sandbox)
            with self._lock:
                self._base_by_sandbox.pop(sandbox, None)
                self._event("trim", base, sandbox)

    def ready_count(self, base: str | None = None) -> int:
        with self._lock:
            if base is not None:
                return len(self._ready.get(base, ()))
            return sum(len(queue) for queue in self._ready.values())

    def events(self) -> list[PoolEvent]:
        with self._lock:
            return list(self._events)

    def wait_for_refill(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                futures = list(self._refills.values())
                if all(f.done() for f in futures):
                    if self._demand_refill_again and not self._closed:
                        self._schedule_refill("", self.config.target_ready)
                        futures = list(self._refills.values())
                    else:
                        for future in futures:
                            future.result()
                        return
            for future in futures:
                remaining = None if deadline is None else max(0, deadline - time.monotonic())
                future.result(timeout=remaining)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
        victims: list[SandboxId] = []
        with self._lock:
            for queue in self._ready.values():
                victims.extend(queue)
                queue.clear()
        for sandbox in victims:
            try:
                self.execution.revoke(sandbox)
            finally:
                with self._lock:
                    self._base_by_sandbox.pop(sandbox, None)

    def _schedule_refill(self, base: str, target: int) -> None:
        with self._lock:
            if self._closed or target <= 0:
                return
            if self._pending_bases is not None:
                key = "\0pending-demand"
                current = self._refills.get(key)
                if current is not None and not current.done():
                    self._demand_refill_again = True
                    return
                self._demand_refill_again = False
                future = self._executor.submit(self._refill, base, target)
                self._refills[key] = future
                future.add_done_callback(lambda _: self._finish_demand_refill(target))
                return
            current = self._refills.get(base)
            if current is not None and not current.done():
                return
            self._refills[base] = self._executor.submit(self._refill, base, target)

    def _finish_demand_refill(self, target: int) -> None:
        with self._lock:
            if self._demand_refill_again and not self._closed:
                self._schedule_refill("", target)

    def _refill(self, base: str, target: int) -> None:
        while True:
            with self._lock:
                if self._closed:
                    return
                if self._pending_bases is not None:
                    counts = collections.Counter(self._pending_bases)
                    base = next((b for b in self._pending_bases
                                 if len(self._ready.get(b, ())) < counts[b]), "")
                    if not base or self.ready_count() >= target:
                        return
                queue = self._ready.setdefault(base, collections.deque())
                if len(queue) >= target or self.ready_count() >= self.config.maximum_ready:
                    return
            host = self.execution.host_stat()
            required = self.config.dram_reserve_bytes + self.config.estimated_ready_bytes
            if host.dram_free_bytes < required:
                with self._lock:
                    self._event(
                        "refill_blocked",
                        base,
                        detail=f"short_by={required - host.dram_free_bytes}",
                    )
                return
            try:
                sandbox = self.execution.run(AgentTask([], base), lambda *_: None)
            except Exception as error:
                with self._lock:
                    self._event("refill_error", base, detail=str(error))
                return
            with self._lock:
                no_remaining_demand = (self._pending_bases is not None and
                    self._pending_bases.count(base) <= len(self._ready.get(base, ())))
                if self._closed or no_remaining_demand:
                    destroy = True
                    if no_remaining_demand:
                        self._event("discard_no_pending_demand", base, sandbox)
                else:
                    self._ready[base].append(sandbox)
                    self._base_by_sandbox[sandbox] = base
                    self._event("prepare", base, sandbox)
                    destroy = False
            if destroy:
                self.execution.revoke(sandbox)
                # Demand may have been consumed while create was in flight.
                # Continue with the next arrived request, without retaining the
                # unused sandbox or losing a refill notification.
                if self._closed:
                    return

    def _event(
        self,
        action: str,
        base: str,
        sandbox_id: SandboxId | None = None,
        *,
        detail: str = "",
    ) -> None:
        self._events.append(
            PoolEvent(
                timestamp_ns=time.time_ns(),
                action=action,
                base=base,
                sandbox_id=sandbox_id,
                ready=sum(len(queue) for queue in self._ready.values()),
                detail=detail,
            )
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("ready pool is closed")

    def __enter__(self) -> ElasticReadyPool:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
