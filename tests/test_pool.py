from __future__ import annotations

import unittest
import threading

from caden.pool import ElasticReadyPool, ReadyPoolConfig
from caden.types import AgentTask, HostStat


class FakePoolExecution:
    def __init__(self) -> None:
        self.next_id = 0
        self.created: list[str] = []
        self.assigned: list[str] = []
        self.revoked: list[str] = []

    def run(self, task, on_report) -> str:
        self.next_id += 1
        sandbox = f"sandbox-{self.next_id}"
        self.created.append(sandbox)
        return sandbox

    def assign(self, sandbox, task, on_report) -> None:
        self.assigned.append(sandbox)

    def revoke(self, sandbox) -> None:
        self.revoked.append(sandbox)

    def host_stat(self) -> HostStat:
        return HostStat(64 << 30, 64 << 30, 0, 0)


class ElasticReadyPoolTest(unittest.TestCase):
    def test_hit_is_one_shot_and_refilled(self) -> None:
        execution = FakePoolExecution()
        pool = ElasticReadyPool(
            execution,  # type: ignore[arg-type]
            ReadyPoolConfig(
                minimum_ready=0,
                target_ready=1,
                maximum_ready=2,
                dram_reserve_bytes=0,
                estimated_ready_bytes=1,
            ),
        )
        self.addCleanup(pool.close)
        pool.start("base")
        pool.wait_for_refill(1.0)
        self.assertEqual(pool.ready_count("base"), 1)

        sandbox = pool.acquire(AgentTask(["true"], "base"), lambda *_: None)
        self.assertEqual(sandbox, "sandbox-1")
        self.assertEqual(execution.assigned, [sandbox])
        pool.wait_for_refill(1.0)
        self.assertEqual(pool.ready_count("base"), 1)

        pool.release(sandbox)
        self.assertIn(sandbox, execution.revoked)
        pool.wait_for_refill(1.0)
        self.assertEqual(pool.ready_count("base"), 1)

        actions = [event.action for event in pool.events()]
        self.assertIn("prepare", actions)
        self.assertIn("hit", actions)
        self.assertIn("destroy_lease", actions)

    def test_refill_respects_memory_reserve(self) -> None:
        execution = FakePoolExecution()
        execution.host_stat = lambda: HostStat(0, 64 << 30, 0, 0)  # type: ignore[method-assign]
        pool = ElasticReadyPool(
            execution,  # type: ignore[arg-type]
            ReadyPoolConfig(target_ready=1, maximum_ready=1),
        )
        self.addCleanup(pool.close)
        pool.start("base")
        pool.wait_for_refill(1.0)
        self.assertEqual(pool.ready_count(), 0)
        self.assertIn("refill_blocked", [event.action for event in pool.events()])

    def test_pending_demand_prefills_next_base_not_the_consumed_base(self) -> None:
        execution = FakePoolExecution()
        pool = ElasticReadyPool(execution, ReadyPoolConfig(target_ready=1, maximum_ready=1),
                                pending_bases=["a", "b"])
        self.addCleanup(pool.close)
        pool.start("a")
        pool.wait_for_refill(1)
        first = pool.acquire(AgentTask([], "a"), lambda *_: None)
        pool.wait_for_refill(1)
        self.assertEqual(pool.ready_count("a"), 0)
        self.assertEqual(pool.ready_count("b"), 1)
        second = pool.acquire(AgentTask([], "b"), lambda *_: None)
        pool.release(first)
        pool.release(second)
        pool.wait_for_refill(1)
        self.assertEqual(pool.ready_count(), 0)
        self.assertEqual(len(execution.created), 2)
        self.assertEqual(len(execution.revoked), 2)

    def test_repeated_pending_base_has_only_the_needed_one_shot_leases(self) -> None:
        execution = FakePoolExecution()
        pool = ElasticReadyPool(execution, ReadyPoolConfig(target_ready=2, maximum_ready=2),
                                pending_bases=["a", "a"])
        self.addCleanup(pool.close)
        pool.start("a")
        pool.wait_for_refill(1)
        leases = [pool.acquire(AgentTask([], "a"), lambda *_: None) for _ in range(2)]
        self.assertEqual(len(set(leases)), 2)
        for lease in leases:
            pool.release(lease)
        pool.wait_for_refill(1)
        self.assertEqual(pool.ready_count(), 0)
        self.assertEqual(len(execution.created), 2)

    def test_inflight_precreation_is_discarded_if_admission_consumes_demand(self) -> None:
        execution = FakePoolExecution()
        started, proceed = threading.Event(), threading.Event()
        original = execution.run
        def delayed(task, callback):
            sandbox = original(task, callback)
            if sandbox == "sandbox-2":
                started.set()
                if not proceed.wait(2):
                    raise RuntimeError("test did not release precreation")
            return sandbox
        execution.run = delayed
        pool = ElasticReadyPool(execution, ReadyPoolConfig(target_ready=1, maximum_ready=1),
                                pending_bases=["a", "b", "c"])
        self.addCleanup(pool.close)
        self.addCleanup(proceed.set)
        pool.start("a")
        pool.wait_for_refill(1)
        a = pool.acquire(AgentTask([], "a"), lambda *_: None)
        self.assertTrue(started.wait(1))
        b = pool.acquire(AgentTask([], "b"), lambda *_: None)
        proceed.set()
        pool.wait_for_refill(1)
        self.assertIn("sandbox-2", execution.revoked)
        self.assertEqual(pool.ready_count("b"), 0)
        self.assertEqual(pool.ready_count("c"), 1)
        pool.release(a)
        pool.release(b)

    def test_unknown_request_is_not_silently_added_to_registered_demand(self) -> None:
        execution = FakePoolExecution()
        pool = ElasticReadyPool(execution, pending_bases=[])
        self.addCleanup(pool.close)
        with self.assertRaises(ValueError):
            pool.acquire(AgentTask([], "unknown"), lambda *_: None)
        self.assertFalse(execution.created)


if __name__ == "__main__":
    unittest.main()
