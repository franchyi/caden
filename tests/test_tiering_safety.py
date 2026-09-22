"""Failure and lifecycle regressions from the independent September 20 review.

Mock evidence only. Linux/native paging is covered separately.
"""
import errno
import threading
import unittest
from unittest.mock import patch

from caden.cxl_tier import CXLTierError, PagerError
from caden.memory_tier import StaleGenerationError
from caden.sandboxfs_backend import SandboxFSError
from caden.types import AgentTask, DemotionRequest, RestoreRequest, Tier
from tests import test_memory_tier_contract as contracts
from tests import test_sandboxfs_backend as execution_tests


class CXLControlSafetyTest(unittest.TestCase):
    def setUp(self):
        self.ctx = contracts.CXLTierMockContractTest()
        self.ctx.setUp()
        self.addCleanup(self.ctx.doCleanups)
        self.sandbox = self.ctx.new_sandbox()
        self.ctx.arm(self.sandbox, resident=64 << 20, movable=32 << 20)

    def test_lost_demotion_reply_restores_actual_cold_pages(self):
        original = self.ctx.pager.call
        self.ctx.make_partial(self.sandbox)

        def lost(*tokens):
            result = original(*tokens)
            if tokens[0] == "DEMOTE":
                raise CXLTierError("reply lost after partial movement")
            return result

        with patch.object(self.ctx.pager, "call", side_effect=lost):
            with self.assertRaises(CXLTierError):
                self.ctx.backend.demote(self.sandbox, DemotionRequest(Tier.CXL))
        self.assertTrue(self.ctx.backend.accounting(self.sandbox).details["movement_uncertain"])
        restored = self.ctx.backend.restore(self.sandbox, RestoreRequest())
        self.assertTrue(restored.ready_for_dispatch)
        self.assertEqual(restored.tier_restored_bytes, 16 << 20)
        self.assertEqual(self.ctx.backend.accounting(self.sandbox).resident_in_tier_bytes, 0)

    def test_timeout_before_server_admission_is_fenced_even_without_policy_generation(self):
        original = self.ctx.pager.call
        delayed = []

        def delayed_call(*tokens):
            if tokens[0] == "DEMOTE":
                delayed.append(tokens)
                raise CXLTierError("timeout before server admission")
            return original(*tokens)

        with patch.object(self.ctx.pager, "call", side_effect=delayed_call):
            with self.assertRaises(CXLTierError):
                self.ctx.backend.demote(self.sandbox, DemotionRequest(Tier.CXL))
        self.assertTrue(self.ctx.backend.restore(self.sandbox, RestoreRequest()).ready_for_dispatch)
        with self.assertRaises(PagerError) as caught:
            original(*delayed[0])
        self.assertEqual(caught.exception.errno, errno.ESTALE)
        self.assertEqual(self.ctx.backend.accounting(self.sandbox).stored_bytes, 0)

    def test_lost_restore_reply_can_be_retried_without_double_restore(self):
        self.ctx.backend.demote(self.sandbox, DemotionRequest(Tier.CXL, generation=7))
        original = self.ctx.pager.call

        def lost(*tokens):
            result = original(*tokens)
            if tokens[0] == "RESTORE":
                raise CXLTierError("restore reply lost")
            return result

        with patch.object(self.ctx.pager, "call", side_effect=lost):
            with self.assertRaises(CXLTierError):
                self.ctx.backend.restore(self.sandbox, RestoreRequest(generation=8))
        restored = self.ctx.backend.restore(self.sandbox, RestoreRequest(generation=8))
        self.assertTrue(restored.ready_for_dispatch)
        self.assertEqual(restored.tier_restored_bytes, 0)
        self.assertEqual(self.ctx.backend.accounting(self.sandbox).restored_bytes, 32 << 20)

    def test_eager_restore_rejects_incomplete_or_pending_receipt(self):
        self.ctx.backend.demote(self.sandbox, DemotionRequest(Tier.CXL))
        original = self.ctx.pager.call
        for bad in ({}, {"restored": "0", "lazy_pending": "4096"},
                    {"restored": "-1", "lazy_pending": "0"}):
            with self.subTest(receipt=bad):
                with patch.object(self.ctx.pager, "call", side_effect=lambda *t: bad if t[0] == "RESTORE" else original(*t)):
                    with self.assertRaises(CXLTierError):
                        self.ctx.backend.restore(self.sandbox, RestoreRequest())

    def test_lost_detach_reply_is_retryable(self):
        original = self.ctx.pager.call

        def lost(*tokens):
            result = original(*tokens)
            if tokens[0] == "DETACH":
                raise CXLTierError("detach reply lost")
            return result

        with patch.object(self.ctx.pager, "call", side_effect=lost):
            with self.assertRaises(CXLTierError):
                self.ctx.backend.detach(self.sandbox)
        final = self.ctx.backend.detach(self.sandbox)
        self.assertTrue(final.details["already_detached"])
        self.ctx.backend.close()


class ExecutionSafetyTest(unittest.TestCase):
    def setUp(self):
        self.ctx = execution_tests.SandboxFSExecutionTest()
        self.ctx.setUp()
        self.addCleanup(self.ctx.doCleanups)
        self.execution = self.ctx.execution
        self.sandbox = self.execution.run(AgentTask(["true"], "base"), lambda *_: None)

    def test_stale_demotion_does_not_refreeze_confirmed_wake(self):
        self.execution.restore_selective(self.sandbox, RestoreRequest(generation=2))
        with self.assertRaises(StaleGenerationError):
            self.execution.demote_selective(self.sandbox, DemotionRequest(Tier.SSD, generation=1))
        self.assertEqual((self.ctx.cgroup / "cgroup.freeze").read_text(), "0")

    def test_failed_teardown_restore_destroys_without_thaw(self):
        with patch.object(self.execution._tier, "restore", side_effect=CXLTierError("CRC")), \
             patch.object(self.execution, "_set_frozen") as freezer:
            self.execution.revoke(self.sandbox)
        freezer.assert_not_called()
        self.assertEqual(self.execution.sandbox_ids(), [])
        self.assertTrue(any("destroy" in c for c in self.ctx.runner.commands))

    def test_detach_failure_keeps_retry_record_and_does_not_destroy_twice(self):
        with patch.object(self.execution._tier, "detach", side_effect=CXLTierError("offline")):
            with self.assertRaises(CXLTierError):
                self.execution.revoke(self.sandbox)
        self.assertIn(self.sandbox, self.execution.sandbox_ids())
        with self.assertRaises(SandboxFSError):
            self.execution.exec(self.sandbox, ["true"])
        self.execution.revoke(self.sandbox)
        self.assertEqual(sum("destroy" in c for c in self.ctx.runner.commands), 1)
        self.assertEqual(self.execution.sandbox_ids(), [])

    def test_destroy_receipt_without_exit_proof_does_not_discard_pages(self):
        self.ctx.runner.cgroup = None  # Simulate an incorrect/incomplete destroy.
        with patch.object(self.execution._tier, "detach") as detach:
            with self.assertRaisesRegex(SandboxFSError, "still has consumers"):
                self.execution.revoke(self.sandbox)
        detach.assert_not_called()
        self.assertIn(self.sandbox, self.execution.sandbox_ids())

    def test_demotion_failure_blocks_dispatch_and_remembers_tier(self):
        with patch.object(self.execution._tier, "demote", side_effect=CXLTierError("lost reply")):
            with self.assertRaises(CXLTierError):
                self.execution.demote_selective(self.sandbox, DemotionRequest(Tier.SSD))
        self.assertIs(self.execution._records[self.sandbox].last_tier, Tier.SSD)
        with self.assertRaisesRegex(SandboxFSError, "confirmed restore"):
            self.execution.exec(self.sandbox, ["true"])
        self.execution.restore(self.sandbox)
        self.execution.exec(self.sandbox, ["true"])

    def test_speculative_restore_does_not_reopen_dispatch(self):
        self.execution.demote(self.sandbox, Tier.SSD)
        self.execution.restore_selective(self.sandbox, RestoreRequest(speculative=True))
        self.assertIs(self.execution._records[self.sandbox].last_tier, Tier.SSD)
        with self.assertRaises(SandboxFSError):
            self.execution.exec(self.sandbox, ["true"])

    def test_wake_waits_for_execution_layer_freeze_and_demotion(self):
        entered, release, waking = threading.Event(), threading.Event(), threading.Event()
        original = self.execution._set_frozen
        errors = []

        def freeze(path, value, **kwargs):
            if value:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test release")
            return original(path, value, **kwargs)

        def demote():
            try:
                self.execution.demote_selective(self.sandbox, DemotionRequest(Tier.SSD, generation=1))
            except Exception as error:
                errors.append(error)

        def wake():
            waking.set()
            try:
                self.execution.restore_selective(self.sandbox, RestoreRequest(generation=2))
            except Exception as error:
                errors.append(error)

        with patch.object(self.execution, "_set_frozen", side_effect=freeze):
            demoter = threading.Thread(target=demote)
            waker = threading.Thread(target=wake)
            demoter.start()
            self.assertTrue(entered.wait(5))
            waker.start()
            self.assertTrue(waking.wait(5))
            release.set()
            demoter.join(5)
            waker.join(5)
        self.assertFalse(demoter.is_alive() or waker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual((self.ctx.cgroup / "cgroup.freeze").read_text(), "0")
