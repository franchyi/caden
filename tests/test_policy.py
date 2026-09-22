from __future__ import annotations

import threading
import time
import unittest

from caden.execution import Execution, SelectiveMemoryPath
from caden.policy import CadenPolicyConfig, StageAwareCaden
from caden.scheduler import ReqState
from caden.types import (
    AgentTask,
    CpuClass,
    DemotionRequest,
    DemotionResult,
    HostStat,
    ReclaimMode,
    ResidencyMode,
    RestoreHint,
    RestoreRequest,
    RestoreResult,
    SandboxStat,
    Stage,
    StageContext,
    Tier,
)


class FakeExecution:
    def __init__(self, free_bytes: int = 16 << 30) -> None:
        self.free_bytes = free_bytes
        self.next_id = 0
        self.calls: list[tuple[object, ...]] = []
        self.stages: dict[str, Stage] = {}
        self.reclaim_started = threading.Event()

    def run(self, task: AgentTask, on_report) -> str:
        self.next_id += 1
        sandbox = f"sandbox-{self.next_id}"
        self.stages[sandbox] = Stage.RESPONSE_WAKE
        self.calls.append(("run", sandbox, task.repo, on_report))
        return sandbox

    def revoke(self, sandbox: str) -> None:
        self.calls.append(("revoke", sandbox))

    def set_cpu(self, sandbox: str, cls: CpuClass) -> None:
        self.calls.append(("cpu", sandbox, cls))

    def demote(self, sandbox: str, tier: Tier) -> int:
        self.reclaim_started.set()
        self.calls.append(("demote", sandbox, tier))
        return 400 << 20

    def restore(self, sandbox: str) -> None:
        self.calls.append(("restore", sandbox))

    def stat(self, sandbox: str) -> SandboxStat:
        return SandboxStat(
            stage=self.stages[sandbox],
            cpu_class=CpuClass.NORMAL,
            mem_dram_bytes=0,
            mem_demoted_bytes=0,
            major_faults=0,
        )

    def host_stat(self) -> HostStat:
        return HostStat(self.free_bytes, 64 << 30, 0, 0)


class SelectiveExecution(FakeExecution):
    def __init__(self, *, restore_delay: float = 0.0) -> None:
        super().__init__()
        self.restore_delay = restore_delay
        self.selective_demotions: list[DemotionRequest] = []
        self.selective_restores: list[RestoreRequest] = []
        self.speculative_restore_done = threading.Event()

    def stat(self, sandbox: str) -> SandboxStat:
        return SandboxStat(
            stage=self.stages[sandbox],
            cpu_class=CpuClass.NORMAL,
            mem_dram_bytes=512 << 20,
            mem_demoted_bytes=0,
            major_faults=0,
        )

    def demote_selective(
        self, sandbox: str, request: DemotionRequest
    ) -> DemotionResult:
        self.reclaim_started.set()
        self.selective_demotions.append(request)
        self.calls.append(("demote_selective", sandbox, request))
        target = request.target_bytes or (512 << 20)
        return DemotionResult(
            requested_bytes=target,
            reclaimed_bytes=target,
            before_bytes=512 << 20,
            after_bytes=(512 << 20) - target,
            tier=request.tier,
            reclaim_mode=request.reclaim_mode,
        )

    def restore_selective(self, sandbox: str, request: RestoreRequest) -> RestoreResult:
        if self.restore_delay:
            time.sleep(self.restore_delay)
        self.selective_restores.append(request)
        self.calls.append(("restore_selective", sandbox, request))
        if request.speculative:
            self.speculative_restore_done.set()
        return RestoreResult(
            resident_delta_bytes=384 << 20,
            prefetched_bytes=1 << 20 if request.speculative else 0,
            ready_for_dispatch=not request.speculative,
            profile=request.profile,
        )


class CapacityLimitedExecution(SelectiveExecution):
    def demote_selective(
        self, sandbox: str, request: DemotionRequest
    ) -> DemotionResult:
        result = super().demote_selective(sandbox, request)
        self.free_bytes = 0
        return result


class BlockingMovementExecution(SelectiveExecution):
    def __init__(self) -> None:
        super().__init__()
        self.demote_entered = threading.Event()
        self.release_demote = threading.Event()
        self.active_demote_sandbox: str | None = None

    def demote_selective(
        self, sandbox: str, request: DemotionRequest
    ) -> DemotionResult:
        self.active_demote_sandbox = sandbox
        self.demote_entered.set()
        self.release_demote.wait(2.0)
        return super().demote_selective(sandbox, request)


class BlockingExecution(FakeExecution):
    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()
        self.release_runs = threading.Event()
        self.two_started = threading.Event()
        self.active_runs = 0
        self.maximum_active_runs = 0

    def run(self, task: AgentTask, on_report) -> str:
        with self.lock:
            self.next_id += 1
            sandbox = f"sandbox-{self.next_id}"
            self.active_runs += 1
            self.maximum_active_runs = max(self.maximum_active_runs, self.active_runs)
            if self.active_runs >= 2:
                self.two_started.set()
        self.release_runs.wait(2.0)
        with self.lock:
            self.active_runs -= 1
            self.stages[sandbox] = Stage.RESPONSE_WAKE
            self.calls.append(("run", sandbox, task.repo, on_report))
        return sandbox


class StageAwareCadenTest(unittest.TestCase):
    def config(self, **overrides: object) -> CadenPolicyConfig:
        values: dict[str, object] = {
            "estimated_wss_bytes": 1,
            "fixed_dram_reserve_bytes": 0,
            "wake_reserve_per_waiter_bytes": 0,
            "reclaim_grace_seconds": 0.01,
            "max_concurrent_reclaims": 1,
        }
        values.update(overrides)
        return CadenPolicyConfig(**values)  # type: ignore[arg-type]

    def test_legacy_execution_remains_protocol_compatible(self) -> None:
        execution = FakeExecution()
        self.assertIsInstance(execution, Execution)
        self.assertNotIsInstance(execution, SelectiveMemoryPath)

    def test_stage_policy_reclaims_then_restores(self) -> None:
        execution = FakeExecution()
        scheduler = StageAwareCaden(
            execution,
            self.config(),
            request_id_factory=lambda: "request-1",
        )
        self.addCleanup(scheduler.close)

        request = scheduler.submit(AgentTask(["true"], "base"))
        scheduler.wait_for_admission(1.0)
        self.assertEqual(scheduler.poll(request).state, ReqState.RUNNING)
        sandbox = scheduler.request_sandbox(request)
        self.assertEqual(sandbox, "sandbox-1")
        self.assertIn(("cpu", sandbox, CpuClass.NORMAL), execution.calls)

        assert sandbox is not None
        scheduler.on_report(sandbox, Stage.LLM_WAIT)
        self.assertIn(("cpu", sandbox, CpuClass.IDLE), execution.calls)
        self.assertTrue(execution.reclaim_started.wait(1.0))
        scheduler.wait_for_background(1.0)
        self.assertIn(("demote", sandbox, Tier.SSD), execution.calls)

        scheduler.on_report(sandbox, Stage.RESPONSE_WAKE)
        self.assertIn(("restore", sandbox), execution.calls)
        self.assertEqual(execution.calls[-1], ("cpu", sandbox, CpuClass.BOOST))

        scheduler.complete(sandbox, "ok")
        self.assertEqual(scheduler.poll(request).state, ReqState.DONE)
        self.assertIn(("revoke", sandbox), execution.calls)

    def test_wake_invalidates_reclaim_grace(self) -> None:
        execution = FakeExecution()
        scheduler = StageAwareCaden(
            execution,
            self.config(reclaim_grace_seconds=0.05),
        )
        self.addCleanup(scheduler.close)
        request = scheduler.submit(AgentTask(["true"], "base"))
        scheduler.wait_for_admission(1.0)
        sandbox = scheduler.request_sandbox(request)
        assert sandbox is not None

        scheduler.on_report(sandbox, Stage.LLM_WAIT)
        scheduler.on_report(sandbox, Stage.RESPONSE_WAKE)
        time.sleep(0.08)
        scheduler.wait_for_background(1.0)

        self.assertFalse(any(call[0] == "demote" for call in execution.calls))

    def test_admission_waits_for_headroom(self) -> None:
        execution = FakeExecution(free_bytes=0)
        scheduler = StageAwareCaden(execution, self.config())
        self.addCleanup(scheduler.close)
        request = scheduler.submit(AgentTask(["true"], "base"))
        self.assertEqual(scheduler.poll(request).state, ReqState.QUEUED)

        execution.free_bytes = 1 << 30
        scheduler.refresh_admission()
        scheduler.wait_for_admission(1.0)
        self.assertEqual(scheduler.poll(request).state, ReqState.RUNNING)

    def test_cancel_queued_request(self) -> None:
        execution = FakeExecution(free_bytes=0)
        scheduler = StageAwareCaden(execution, self.config())
        self.addCleanup(scheduler.close)
        request = scheduler.submit(AgentTask(["true"], "base"))
        scheduler.cancel(request)
        self.assertEqual(scheduler.poll(request).state, ReqState.CANCELLED)
        self.assertFalse(any(call[0] == "run" for call in execution.calls))

    def test_admits_a_burst_concurrently_with_a_bound(self) -> None:
        execution = BlockingExecution()
        scheduler = StageAwareCaden(
            execution,
            self.config(max_concurrent_admissions=2),
        )
        self.addCleanup(scheduler.close)
        self.addCleanup(execution.release_runs.set)

        first = scheduler.submit(AgentTask(["true"], "base"))
        second = scheduler.submit(AgentTask(["true"], "base"))
        self.assertTrue(execution.two_started.wait(1.0))
        self.assertEqual(execution.maximum_active_runs, 2)

        execution.release_runs.set()
        scheduler.wait_for_admission(1.0)
        self.assertEqual(scheduler.poll(first).state, ReqState.RUNNING)
        self.assertEqual(scheduler.poll(second).state, ReqState.RUNNING)

    def test_request_aware_reclaim_hot_floor_and_speculative_restore(self) -> None:
        execution = SelectiveExecution()
        scheduler = StageAwareCaden(
            execution,
            self.config(
                residency_mode=ResidencyMode.REQUEST_AWARE,
                reclaim_grace_seconds=0.001,
                sandbox_hot_reserve_bytes=128 << 20,
                minimum_reclaim_bytes=1,
                minimum_cold_residency_seconds=0,
                estimated_demote_seconds=0,
                estimated_restore_seconds=0,
                maximum_early_wake_probability=1,
                prediction_interval_seconds=0.002,
                prediction_prior_mean_seconds=1,
                speculative_restore_enabled=True,
                speculative_restore_probability=0,
                speculative_restore_lead_seconds=0.01,
                speculative_restore_queue_margin_seconds=0,
            ),
        )
        self.addCleanup(scheduler.close)
        request = scheduler.submit(AgentTask([], "base", klass="interactive"))
        scheduler.wait_for_admission(1.0)
        sandbox = scheduler.request_sandbox(request)
        assert sandbox is not None

        scheduler.on_report(sandbox, Stage.LLM_WAIT)
        self.assertTrue(execution.reclaim_started.wait(1.0))
        self.assertTrue(execution.speculative_restore_done.wait(1.0))
        scheduler.wait_for_background(1.0)

        demotion = execution.selective_demotions[0]
        self.assertEqual(demotion.target_bytes, 384 << 20)
        self.assertEqual(demotion.min_resident_bytes, 128 << 20)
        self.assertTrue(execution.selective_restores[0].speculative)
        scheduler.on_report(sandbox, Stage.RESPONSE_WAKE)
        self.assertEqual(len(execution.selective_restores), 2)
        self.assertFalse(execution.selective_restores[-1].speculative)
        actions = [event.action for event in scheduler.events()]
        self.assertIn("hazard", actions)
        self.assertIn("spec_restore", actions)

    def test_request_aware_profile_hot_floor_can_reject_reclaim(self) -> None:
        execution = SelectiveExecution()
        scheduler = StageAwareCaden(
            execution,
            self.config(
                residency_mode=ResidencyMode.REQUEST_AWARE,
                reclaim_grace_seconds=0.001,
                sandbox_hot_reserve_bytes=128 << 20,
                restore_profile_hot_reserve_bytes=(("full-scan", 512 << 20),),
                minimum_reclaim_bytes=1,
                prediction_interval_seconds=0.002,
            ),
        )
        self.addCleanup(scheduler.close)
        request = scheduler.submit(AgentTask([], "base", klass="interactive"))
        scheduler.wait_for_admission(1.0)
        sandbox = scheduler.request_sandbox(request)
        assert sandbox is not None

        scheduler.on_report(
            sandbox,
            Stage.LLM_WAIT,
            StageContext(restore_profile="full-scan"),
        )
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not any(
            event.action == "reclaim_skip" for event in scheduler.events()
        ):
            time.sleep(0.005)
        scheduler.wait_for_background(1.0)

        self.assertEqual(execution.selective_demotions, [])
        skips = [
            event for event in scheduler.events() if event.action == "reclaim_skip"
        ]
        self.assertTrue(skips)
        self.assertIn('"restore_profile":"full-scan"', skips[-1].detail)
        self.assertIn('"hot_reserve_bytes":536870912', skips[-1].detail)

    def test_speculative_restore_preserves_confirmed_dram_headroom(self) -> None:
        execution = CapacityLimitedExecution()
        scheduler = StageAwareCaden(
            execution,
            self.config(
                residency_mode=ResidencyMode.REQUEST_AWARE,
                reclaim_grace_seconds=0,
                minimum_reclaim_bytes=1,
                minimum_cold_residency_seconds=0,
                estimated_demote_seconds=0,
                estimated_restore_seconds=0,
                maximum_early_wake_probability=1,
                prediction_interval_seconds=0.005,
                speculative_restore_enabled=True,
                speculative_restore_probability=0,
            ),
        )
        self.addCleanup(scheduler.close)
        request = scheduler.submit(AgentTask([], "base"))
        scheduler.wait_for_admission(1.0)
        sandbox = scheduler.request_sandbox(request)
        assert sandbox is not None
        scheduler.on_report(sandbox, Stage.LLM_WAIT)
        self.assertTrue(execution.reclaim_started.wait(1.0))
        time.sleep(0.03)
        scheduler.wait_for_background(1.0)

        self.assertFalse(execution.selective_restores)
        self.assertIn(
            "spec_restore_capacity_skip",
            [event.action for event in scheduler.events()],
        )

    def test_profitability_rejects_a_wait_shorter_than_movement_cost(self) -> None:
        execution = SelectiveExecution()
        scheduler = StageAwareCaden(
            execution,
            self.config(
                residency_mode=ResidencyMode.ELAPSED,
                reclaim_grace_seconds=0,
                minimum_reclaim_bytes=1,
                prediction_prior_mean_seconds=0.01,
                estimated_demote_seconds=0.1,
                estimated_restore_seconds=0.1,
                maximum_early_wake_probability=1,
            ),
        )
        self.addCleanup(scheduler.close)
        request = scheduler.submit(AgentTask([], "base"))
        scheduler.wait_for_admission(1.0)
        sandbox = scheduler.request_sandbox(request)
        assert sandbox is not None
        scheduler.on_report(sandbox, Stage.LLM_WAIT)
        time.sleep(0.03)
        scheduler.wait_for_background(1.0)

        self.assertFalse(execution.selective_demotions)
        decisions = [
            event for event in scheduler.events() if event.action == "reclaim_decision"
        ]
        self.assertTrue(decisions)
        self.assertIn('"eligible":false', decisions[-1].detail)

    def test_compression_selector_forces_anonymous_reclaim(self) -> None:
        execution = SelectiveExecution()
        scheduler = StageAwareCaden(
            execution,
            self.config(
                residency_mode=ResidencyMode.ELAPSED,
                reclaim_grace_seconds=0,
                minimum_reclaim_bytes=1,
                minimum_cold_residency_seconds=0,
                estimated_demote_seconds=0,
                estimated_restore_seconds=0,
                maximum_early_wake_probability=1,
                compression_max_wait_seconds=2,
            ),
        )
        self.addCleanup(scheduler.close)
        request = scheduler.submit(AgentTask([], "base"))
        scheduler.wait_for_admission(1.0)
        sandbox = scheduler.request_sandbox(request)
        assert sandbox is not None
        scheduler.on_report(sandbox, Stage.LLM_WAIT)
        self.assertTrue(execution.reclaim_started.wait(1.0))
        scheduler.wait_for_background(1.0)

        demotion = execution.selective_demotions[0]
        self.assertIs(demotion.tier, Tier.COMPRESSED)
        self.assertIs(demotion.reclaim_mode, ReclaimMode.ANON_ONLY)

    def test_generation_fences_restore_hints(self) -> None:
        execution = SelectiveExecution()
        scheduler = StageAwareCaden(execution, self.config(reclaim_enabled=False))
        self.addCleanup(scheduler.close)
        request = scheduler.submit(AgentTask([], "base"))
        scheduler.wait_for_admission(1.0)
        sandbox = scheduler.request_sandbox(request)
        assert sandbox is not None
        scheduler.on_report(sandbox, Stage.LLM_WAIT)
        generation = scheduler.current_generation(sandbox)
        scheduler.on_restore_hint(
            sandbox,
            RestoreHint(
                probability=1,
                horizon_seconds=0,
                generation=generation - 1,
                hint_id="stale",
            ),
        )
        self.assertEqual(scheduler.events()[-1].action, "restore_hint_stale")

    def test_slo_breaker_disables_later_reclaim(self) -> None:
        execution = SelectiveExecution(restore_delay=0.01)
        scheduler = StageAwareCaden(
            execution,
            self.config(
                reclaim_grace_seconds=0,
                wake_latency_slo_seconds=0.001,
                wake_latency_p99_slo_seconds=0.0005,
                slo_minimum_samples=1,
                slo_window_samples=1,
                slo_cooldown_seconds=1,
            ),
        )
        self.addCleanup(scheduler.close)
        request = scheduler.submit(AgentTask([], "base"))
        scheduler.wait_for_admission(1.0)
        sandbox = scheduler.request_sandbox(request)
        assert sandbox is not None
        scheduler.on_report(sandbox, Stage.LLM_WAIT)
        self.assertTrue(execution.reclaim_started.wait(1.0))
        scheduler.wait_for_background(1.0)
        scheduler.on_report(sandbox, Stage.RESPONSE_WAKE)
        scheduler.on_report(sandbox, Stage.RESULT_PACK)
        first_count = len(execution.selective_demotions)

        execution.reclaim_started.clear()
        scheduler.on_report(sandbox, Stage.LLM_WAIT)
        time.sleep(0.03)
        scheduler.wait_for_background(1.0)
        self.assertEqual(len(execution.selective_demotions), first_count)
        breaker_events = [
            event for event in scheduler.events() if event.action == "slo_breaker_open"
        ]
        self.assertTrue(breaker_events)
        self.assertIn('"quantile":0.99', breaker_events[-1].detail)

    def test_speculation_never_overtakes_an_uncommitted_reclaim(self) -> None:
        execution = BlockingMovementExecution()
        scheduler = StageAwareCaden(
            execution,
            self.config(
                residency_mode=ResidencyMode.REQUEST_AWARE,
                reclaim_grace_seconds=0,
                max_concurrent_reclaims=3,
                max_concurrent_movements=1,
                minimum_reclaim_bytes=1,
                minimum_cold_residency_seconds=0,
                estimated_demote_seconds=0,
                estimated_restore_seconds=0,
                maximum_early_wake_probability=1,
                prediction_interval_seconds=0.005,
                speculative_restore_enabled=True,
                speculative_restore_probability=0,
            ),
        )
        self.addCleanup(scheduler.close)
        self.addCleanup(execution.release_demote.set)
        requests = [
            scheduler.submit(AgentTask([], "base")),
            scheduler.submit(AgentTask([], "base")),
        ]
        scheduler.wait_for_admission(1.0)
        sandboxes = [scheduler.request_sandbox(request) for request in requests]
        for sandbox in sandboxes:
            assert sandbox is not None
            scheduler.on_report(sandbox, Stage.LLM_WAIT)
        self.assertTrue(execution.demote_entered.wait(1.0))
        time.sleep(0.03)
        active = execution.active_demote_sandbox
        queued = next(sandbox for sandbox in sandboxes if sandbox != active)
        assert queued is not None
        scheduler.on_restore_hint(
            queued,
            RestoreHint(
                probability=1,
                horizon_seconds=0,
                generation=scheduler.current_generation(queued),
            ),
        )
        time.sleep(0.03)
        self.assertFalse(execution.selective_restores)

        execution.release_demote.set()
        scheduler.wait_for_background(1.0)

    def test_confirmed_wake_uses_reserved_movement_slot(self) -> None:
        execution = BlockingMovementExecution()
        scheduler = StageAwareCaden(
            execution,
            self.config(
                reclaim_grace_seconds=0,
                max_concurrent_reclaims=2,
                max_concurrent_movements=2,
                confirmed_movement_reserve=1,
            ),
        )
        self.addCleanup(scheduler.close)
        self.addCleanup(execution.release_demote.set)
        requests = [
            scheduler.submit(AgentTask([], "base")),
            scheduler.submit(AgentTask([], "base")),
        ]
        scheduler.wait_for_admission(1.0)
        sandboxes = [scheduler.request_sandbox(request) for request in requests]
        self.assertTrue(all(sandbox is not None for sandbox in sandboxes))
        for sandbox in sandboxes:
            assert sandbox is not None
            scheduler.on_report(sandbox, Stage.LLM_WAIT)
        self.assertTrue(execution.demote_entered.wait(1.0))
        time.sleep(0.02)
        active = execution.active_demote_sandbox
        waking = next(sandbox for sandbox in sandboxes if sandbox != active)
        assert waking is not None

        started = time.monotonic()
        scheduler.on_report(waking, Stage.RESPONSE_WAKE)
        self.assertLess(time.monotonic() - started, 0.2)
        execution.release_demote.set()
        scheduler.wait_for_background(1.0)
        self.assertEqual(len(execution.selective_demotions), 1)
        self.assertIn("reclaim_stale", [event.action for event in scheduler.events()])

    def test_wake_slots_bound_concurrent_tool_bursts(self) -> None:
        execution = FakeExecution()
        scheduler = StageAwareCaden(
            execution,
            self.config(
                max_concurrent_wakes=1,
                reclaim_enabled=False,
            ),
        )
        self.addCleanup(scheduler.close)
        first = scheduler.submit(AgentTask([], "base"))
        second = scheduler.submit(AgentTask([], "base"))
        scheduler.wait_for_admission(1.0)
        first_sandbox = scheduler.request_sandbox(first)
        second_sandbox = scheduler.request_sandbox(second)
        assert first_sandbox is not None and second_sandbox is not None
        scheduler.on_report(first_sandbox, Stage.LLM_WAIT)
        scheduler.on_report(second_sandbox, Stage.LLM_WAIT)

        scheduler.on_report(first_sandbox, Stage.RESPONSE_WAKE)
        second_returned = threading.Event()

        def wake_second() -> None:
            scheduler.on_report(second_sandbox, Stage.RESPONSE_WAKE)
            second_returned.set()

        thread = threading.Thread(target=wake_second)
        thread.start()
        try:
            self.assertFalse(second_returned.wait(0.05))
            scheduler.on_report(first_sandbox, Stage.RESULT_PACK)
            self.assertTrue(second_returned.wait(1.0))
            scheduler.on_report(second_sandbox, Stage.RESULT_PACK)
        finally:
            if not second_returned.is_set():
                scheduler.on_report(first_sandbox, Stage.RESULT_PACK)
            thread.join(1.0)


if __name__ == "__main__":
    unittest.main()
