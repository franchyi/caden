"""MOCK policy tests: tier selection and generation fencing reach the backend seam."""

from __future__ import annotations

import unittest

from caden.policy import CadenPolicyConfig, StageAwareCaden
from caden.types import AgentTask, Stage, Tier

from tests.test_policy import SelectiveExecution


class PolicyTierFencingTest(unittest.TestCase):
    def scheduler(self, execution: SelectiveExecution, **overrides: object) -> StageAwareCaden:
        values: dict[str, object] = {
            "estimated_wss_bytes": 1,
            "fixed_dram_reserve_bytes": 0,
            "wake_reserve_per_waiter_bytes": 0,
            "reclaim_grace_seconds": 0.01,
            "max_concurrent_reclaims": 1,
        }
        values.update(overrides)
        scheduler = StageAwareCaden(execution, CadenPolicyConfig(**values))  # type: ignore[arg-type]
        self.addCleanup(scheduler.close)
        return scheduler

    def test_policy_requests_the_configured_cxl_tier_without_backend_details(self) -> None:
        execution = SelectiveExecution()
        scheduler = self.scheduler(execution, reclaim_tier=Tier.CXL)
        request = scheduler.submit(AgentTask(["true"], "base"))
        scheduler.wait_for_admission(1.0)
        sandbox = scheduler.request_sandbox(request)
        assert sandbox is not None
        scheduler.on_report(sandbox, Stage.LLM_WAIT)
        self.assertTrue(execution.reclaim_started.wait(1.0))
        scheduler.wait_for_background(1.0)
        self.assertEqual([item.tier for item in execution.selective_demotions], [Tier.CXL])
        scheduler.complete(sandbox, "ok")

    def test_movement_requests_carry_monotonic_lifecycle_generations(self) -> None:
        execution = SelectiveExecution()
        scheduler = self.scheduler(execution)
        request = scheduler.submit(AgentTask(["true"], "base"))
        scheduler.wait_for_admission(1.0)
        sandbox = scheduler.request_sandbox(request)
        assert sandbox is not None
        generations: list[int] = []
        for _ in range(2):
            execution.reclaim_started.clear()
            scheduler.on_report(sandbox, Stage.LLM_WAIT)
            self.assertTrue(execution.reclaim_started.wait(1.0))
            scheduler.wait_for_background(1.0)
            scheduler.on_report(sandbox, Stage.RESPONSE_WAKE)
            scheduler.on_report(sandbox, Stage.TOOL_BURST)
            scheduler.on_report(sandbox, Stage.RESULT_PACK)
        for demotion, restore in zip(execution.selective_demotions, execution.selective_restores):
            self.assertIsNotNone(demotion.generation)
            self.assertIsNotNone(restore.generation)
            generations += [demotion.generation, restore.generation]  # type: ignore[list-item]
        self.assertEqual(len(generations), 4)
        self.assertEqual(generations, sorted(generations))
        self.assertLess(generations[0], generations[1])  # the wake supersedes its demotion
        scheduler.complete(sandbox, "ok")


if __name__ == "__main__":
    unittest.main()
