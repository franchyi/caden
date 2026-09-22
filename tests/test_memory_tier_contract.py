"""MOCK contract tests shared by both memory-tier backends.

Evidence class: mock only. The SSD backend runs over synthetic cgroup files
with a simulated ``memory.reclaim``; the CXL backend runs against an in-process
fake of the pager protocol. Nothing here moves a real page - real paging is in
``test_cxl_tier_real.py`` and hardware runs are separate again. What this suite
pins down is the policy-facing contract both implementations must share.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import caden.sandboxfs_backend as ssd_module
from caden.cxl_tier import (
    CoopRegion,
    CXLTierBackend,
    CXLTierConfig,
    CXLTierError,
    PagerError,
    discover_regions,
)
from caden.memory_tier import (
    GenerationFence,
    MemoryTierBackend,
    MemoryTierError,
    StaleGenerationError,
    UnsupportedTierError,
)
from caden.sandboxfs_backend import SandboxFSConfig, SandboxFSError, SSDTierBackend
from caden.types import DemotionRequest, RestoreRequest, Tier

MIB = 1 << 20


@dataclass
class Sandbox:
    id: str
    cgroup_path: Path
    incarnation: int = 1
    zswap_writeback_original: str | None = None


class FakePager:
    """In-process stand-in for crate_pagerd's line protocol (mock evidence)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sandboxes: dict[tuple[str, str], dict[str, int]] = {}
        self.movable: dict[str, int] = {}
        self.partial: set[str] = set()
        self.failing: set[str] = set()
        self.refuse_lazy = False
        self.calls: list[tuple[str, ...]] = []
        self.demote_gate: threading.Event | None = None
        self.demote_entered = threading.Event()

    def call(self, *tokens: object) -> dict[str, str]:
        words = tuple(str(token) for token in tokens)
        self.calls.append(words)
        verb = words[0]
        if verb == "HELLO":
            return {"medium": "file-emulation", "codec": "none", "path": "mock", "offset": "0",
                    "capacity": str(64 * MIB)}
        if verb == "STATS":
            return {"logical": str(64 * MIB), "logical_free": str(64 * MIB)}
        key = (words[1], words[2])
        with self.lock:
            if verb == "ATTACH":
                self.sandboxes[key] = dict.fromkeys(
                    ("cold", "stored", "released", "restored_eager", "restored_fault",
                     "demotions", "restores", "stale", "errors", "generation", "regions",
                     "requested", "eligible"), 0)
                self.sandboxes[key]["generation"] = -1
                return {}
            state = self.sandboxes.get(key)
            if state is None:
                raise PagerError(errno.ENOENT, f"{verb} sandbox")
            if verb == "REGION":
                state["regions"] += 1
                return {"region": "1"}
            if verb == "STAT":
                return {name: str(value) for name, value in state.items()} | {"metadata": "4096"}
            if verb == "DETACH":
                discarded = state["cold"]
                del self.sandboxes[key]
                return {"discarded": str(discarded)}
            generation = int(words[3])
            if 0 <= generation < state["generation"]:
                state["stale"] += 1
                raise PagerError(errno.ESTALE, f"{verb} generation")
            state["generation"] = max(state["generation"], generation)
        if verb == "DEMOTE":
            self.demote_entered.set()
            if self.demote_gate is not None:
                self.demote_gate.wait(5)
            with self.lock:
                if words[1] in self.failing:
                    state["errors"] += 1
                    raise PagerError(errno.EIO, "DEMOTE moved nothing")
                target = int(words[4])
                moved = min(self.movable.get(words[1], 0), target if target >= 0 else 1 << 62)
                partial = words[1] in self.partial
                if partial:
                    moved //= 2
                self.movable[words[1]] = self.movable.get(words[1], 0) - moved
                state["cold"] += moved
                state["stored"] += moved
                state["released"] += moved
                state["eligible"] += moved
                state["requested"] += max(target, 0)
                state["demotions"] += 1
                return {"eligible": str(moved), "stored": str(moved), "released": str(moved),
                        "cold": str(state["cold"]), "partial": "1" if partial else "0"}
        if verb == "RESTORE":
            with self.lock:
                if words[1] in self.failing:
                    state["errors"] += 1
                    raise PagerError(errno.EILSEQ, "RESTORE store read")
                state["restores"] += 1
                if words[4].startswith("lazy"):
                    if self.refuse_lazy:
                        raise PagerError(errno.EOPNOTSUPP, "RESTORE lazy refused")
                    return {"restored": "0", "lazy_pending": str(state["cold"])}
                restored, state["cold"] = state["cold"], 0
                state["restored_eager"] += restored
                self.movable[words[1]] = self.movable.get(words[1], 0) + restored
                return {"restored": str(restored), "lazy_pending": "0"}
        raise PagerError(errno.ENOSYS, verb)

    def close(self) -> None:
        return None


class TierContract:
    """Behavior every ``MemoryTierBackend`` must share. Mixed into TestCases."""

    tier: Tier
    foreign_tier: Tier
    backend: MemoryTierBackend

    # -- provided by each backend's fixture ----------------------------------
    def arm(self, sandbox: Sandbox, *, resident: int, movable: int) -> None: ...
    def make_partial(self, sandbox: Sandbox) -> None: ...
    def make_failing(self, sandbox: Sandbox) -> None: ...
    def hold_demotion(self) -> tuple[threading.Event, threading.Event]: ...

    def new_sandbox(self, name: str = "sbx", incarnation: int = 1) -> Sandbox:
        cgroup = self.root / "cgroup" / f"{name}-{incarnation}"  # type: ignore[attr-defined]
        cgroup.mkdir(parents=True)
        for file, value in {"memory.current": "0", "memory.swap.current": "0",
                            "memory.zswap.current": "0", "memory.swap.max": "max",
                            "memory.reclaim": "", "cgroup.procs": "4321\n"}.items():
            (cgroup / file).write_text(value)
        sandbox = Sandbox(name, cgroup, incarnation)
        self.backend.attach(sandbox)
        return sandbox

    def test_protocol_and_capabilities_are_explicit(self) -> None:
        self.assertIsInstance(self.backend, MemoryTierBackend)
        capabilities = self.backend.capabilities()
        self.assertIn(self.tier, capabilities.supported_tiers)
        self.assertNotIn(self.foreign_tier, capabilities.supported_tiers)
        self.assertTrue(capabilities.eligible_memory and capabilities.ineligible_memory)
        self.assertTrue(capabilities.required_permissions)
        self.assertTrue(capabilities.placement_guarantee and capabilities.evidence_class)
        json.dumps(capabilities.as_json())  # run artifacts must serialize it

    def test_unsupported_tier_fails_explicitly_without_substitution(self) -> None:
        sandbox = self.new_sandbox()
        self.arm(sandbox, resident=64 * MIB, movable=32 * MIB)
        with self.assertRaises(UnsupportedTierError):
            self.backend.validate(self.foreign_tier)
        with self.assertRaises(UnsupportedTierError):
            self.backend.demote(sandbox, DemotionRequest(self.foreign_tier))
        self.assertEqual(self.backend.accounting(sandbox).demotions, 0)
        self.assertEqual((sandbox.cgroup_path / "memory.current").read_text(), str(64 * MIB))

    def test_receipt_reports_requested_stored_and_released_separately(self) -> None:
        sandbox = self.new_sandbox()
        self.arm(sandbox, resident=64 * MIB, movable=24 * MIB)
        result = self.backend.demote(
            sandbox, DemotionRequest(self.tier, min_resident_bytes=16 * MIB, generation=1)
        )
        self.assertEqual(result.backend, self.backend.name)
        self.assertEqual(result.tier, self.tier)
        self.assertEqual(result.requested_bytes, 48 * MIB)  # resident - hot floor
        self.assertEqual(result.before_bytes, 64 * MIB)
        self.assertEqual(result.reclaimed_bytes, result.before_bytes - result.after_bytes)
        self.assertLessEqual(result.stored_bytes, result.requested_bytes)
        self.assertGreater(result.released_bytes, 0)
        self.assertEqual(result.generation, 1)
        accounting = self.backend.accounting(sandbox)
        self.assertEqual(accounting.demotions, 1)
        self.assertEqual(accounting.stored_bytes, result.stored_bytes)
        json.dumps(accounting.as_json())

    def test_hot_floor_above_resident_memory_moves_nothing(self) -> None:
        sandbox = self.new_sandbox()
        self.arm(sandbox, resident=8 * MIB, movable=8 * MIB)
        result = self.backend.demote(
            sandbox, DemotionRequest(self.tier, min_resident_bytes=32 * MIB)
        )
        self.assertEqual((result.requested_bytes, result.reclaimed_bytes), (0, 0))
        self.assertEqual(result.after_bytes, 8 * MIB)

    def test_partial_progress_is_reported_not_discarded(self) -> None:
        sandbox = self.new_sandbox()
        self.arm(sandbox, resident=64 * MIB, movable=32 * MIB)
        self.make_partial(sandbox)
        result = self.backend.demote(sandbox, DemotionRequest(self.tier))
        self.assertTrue(result.partial)
        self.assertGreater(result.reclaimed_bytes, 0)
        self.assertLess(result.reclaimed_bytes, result.requested_bytes)

    def test_error_without_progress_raises_and_is_counted(self) -> None:
        sandbox = self.new_sandbox()
        self.arm(sandbox, resident=64 * MIB, movable=32 * MIB)
        self.make_failing(sandbox)
        with self.assertRaises((MemoryTierError, SandboxFSError)):
            self.backend.demote(sandbox, DemotionRequest(self.tier))
        self.assertGreater(self.backend.accounting(sandbox).errors, 0)

    def test_stale_generation_is_rejected_before_movement(self) -> None:
        sandbox = self.new_sandbox()
        self.arm(sandbox, resident=64 * MIB, movable=32 * MIB)
        self.backend.demote(sandbox, DemotionRequest(self.tier, generation=4))
        self.backend.restore(sandbox, RestoreRequest(generation=5))
        before = self.backend.accounting(sandbox).demotions
        with self.assertRaises(StaleGenerationError):
            self.backend.demote(sandbox, DemotionRequest(self.tier, generation=4))
        with self.assertRaises(StaleGenerationError):
            self.backend.restore(sandbox, RestoreRequest(speculative=True, generation=3))
        self.assertEqual(self.backend.accounting(sandbox).demotions, before)

    def test_preparation_only_restore_never_reports_dispatch_readiness(self) -> None:
        sandbox = self.new_sandbox()
        self.arm(sandbox, resident=64 * MIB, movable=32 * MIB)
        self.backend.demote(sandbox, DemotionRequest(self.tier, generation=1))
        prepared = self.backend.restore(sandbox, RestoreRequest(speculative=True, generation=1))
        self.assertFalse(prepared.ready_for_dispatch)
        confirmed = self.backend.restore(sandbox, RestoreRequest(generation=2))
        self.assertTrue(confirmed.ready_for_dispatch)
        self.assertEqual(confirmed.backend, self.backend.name)
        self.backend.finish_restore(sandbox, RestoreRequest(generation=2))

    def test_wake_waits_for_an_overlapping_demotion(self) -> None:
        sandbox = self.new_sandbox()
        self.arm(sandbox, resident=64 * MIB, movable=32 * MIB)
        entered, release = self.hold_demotion()
        order: list[str] = []

        def demote() -> None:
            self.backend.demote(sandbox, DemotionRequest(self.tier, generation=1))
            order.append("demote")

        def wake() -> None:
            self.backend.restore(sandbox, RestoreRequest(generation=2))
            order.append("restore")

        demotion = threading.Thread(target=demote)
        demotion.start()
        self.assertTrue(entered.wait(5))
        waker = threading.Thread(target=wake)
        waker.start()
        time.sleep(0.05)
        self.assertEqual(order, [])  # the wake is synchronized, not interleaved
        release.set()
        demotion.join(5)
        waker.join(5)
        self.assertEqual(order, ["demote", "restore"])

    def test_sandboxes_and_incarnations_are_accounted_independently(self) -> None:
        first, second = self.new_sandbox("a"), self.new_sandbox("b")
        self.arm(first, resident=64 * MIB, movable=32 * MIB)
        self.arm(second, resident=64 * MIB, movable=32 * MIB)
        self.backend.demote(first, DemotionRequest(self.tier, generation=9))
        self.assertEqual(self.backend.accounting(second).demotions, 0)
        final = self.backend.detach(first)
        self.assertEqual(final.demotions, 1)
        # A reused identifier is a new incarnation: clean fence, clean counters.
        reused = self.new_sandbox("a", incarnation=2)
        self.arm(reused, resident=64 * MIB, movable=32 * MIB)
        self.backend.demote(reused, DemotionRequest(self.tier, generation=1))
        self.assertEqual(self.backend.accounting(reused).demotions, 1)
        self.backend.detach(reused)
        self.backend.detach(second)
        self.backend.close()


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)


class SSDTierContractTest(TierContract, _Fixture):
    tier, foreign_tier = Tier.SSD, Tier.CXL

    def setUp(self) -> None:
        super().setUp()
        self.backend = SSDTierBackend(
            SandboxFSConfig(proc_root=self.root / "proc", cgroup_root=self.root / "cgroup",
                            reclaim_settle_seconds=0, swaps_path=self.root / "swaps")
        )
        (self.root / "swaps").write_text(
            "Filename Type Size Used Priority\n/swap.img file 8388604 0 -2\n"
        )
        self.movable: dict[Path, int] = {}
        self.partial: set[Path] = set()
        self.failing: set[Path] = set()
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        real_write = ssd_module._write

        def simulated_kernel(path: Path, value: str) -> None:
            if path.name != "memory.reclaim":
                real_write(path, value)
                return
            self.entered.set()
            if self.gate is not None:
                self.gate.wait(5)
            cgroup = path.parent
            if cgroup in self.failing:
                raise SandboxFSError(f"write {path}") from OSError(errno.EIO, "io")
            moved = min(int(value.split()[0]), self.movable.get(cgroup, 0))
            if cgroup in self.partial:
                moved //= 2
            current = int((cgroup / "memory.current").read_text())
            (cgroup / "memory.current").write_text(str(current - moved))
            swapped = int((cgroup / "memory.swap.current").read_text()) + moved // 4
            (cgroup / "memory.swap.current").write_text(str(swapped))
            if cgroup in self.partial:
                raise SandboxFSError(f"write {path}") from OSError(errno.EAGAIN, "again")

        patcher = patch.object(ssd_module, "_write", side_effect=simulated_kernel)
        patcher.start()
        self.addCleanup(patcher.stop)

    def arm(self, sandbox: Sandbox, *, resident: int, movable: int) -> None:
        (sandbox.cgroup_path / "memory.current").write_text(str(resident))
        self.movable[sandbox.cgroup_path] = movable

    def make_partial(self, sandbox: Sandbox) -> None:
        self.partial.add(sandbox.cgroup_path)

    def make_failing(self, sandbox: Sandbox) -> None:
        self.failing.add(sandbox.cgroup_path)

    def hold_demotion(self) -> tuple[threading.Event, threading.Event]:
        self.gate = threading.Event()
        return self.entered, self.gate

    def test_stored_bytes_are_only_the_swap_delta_never_dropped_file_cache(self) -> None:
        sandbox = self.new_sandbox()
        self.arm(sandbox, resident=64 * MIB, movable=32 * MIB)
        result = self.backend.demote(sandbox, DemotionRequest(Tier.SSD))
        self.assertEqual(result.released_bytes, 32 * MIB)
        self.assertEqual(result.stored_bytes, result.swap_delta_bytes)
        self.assertEqual(result.file_reclaimed_bytes, 32 * MIB - result.swap_delta_bytes)
        details = self.backend.capabilities().details
        self.assertFalse(details["activates_swap_device"])
        self.assertEqual(details["configured_swaps"][0]["name"], "/swap.img")

    def test_default_execution_backend_remains_ssd(self) -> None:
        execution = ssd_module.SandboxFSExecution(SandboxFSConfig())
        self.assertIsInstance(execution.memory_tier, SSDTierBackend)


class CXLTierMockContractTest(TierContract, _Fixture):
    tier, foreign_tier = Tier.CXL, Tier.SSD

    def setUp(self) -> None:
        super().setUp()
        self.pager = FakePager()
        self.backend = CXLTierBackend(
            CXLTierConfig(socket_path="unused", proc_root=self.root / "proc",
                          reclaim_settle_seconds=0, file_cache_reclaim=False,
                          restore_mode="eager"),
            client=self.pager,  # type: ignore[arg-type]
        )
        self.regions = [CoopRegion(4321, 7, 8, 0x7F0000000000, 32 * MIB, 0, True)]
        patcher = patch("caden.cxl_tier.discover_regions", side_effect=lambda *_: self.regions)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.watch: dict[str, Path] = {}
        original = self.pager.call

        def call(*tokens: object) -> dict[str, str]:
            reply = original(*tokens)
            words = [str(token) for token in tokens]
            if words[0] in {"DEMOTE", "RESTORE"} and words[1] in self.watch:
                path = self.watch[words[1]] / "memory.current"
                delta = int(reply.get("released", 0)) - int(reply.get("restored", 0))
                path.write_text(str(int(path.read_text()) - delta))
            return reply

        self.pager.call = call  # type: ignore[method-assign]

    def arm(self, sandbox: Sandbox, *, resident: int, movable: int) -> None:
        (sandbox.cgroup_path / "memory.current").write_text(str(resident))
        self.pager.movable[sandbox.id] = movable
        self.watch[sandbox.id] = sandbox.cgroup_path

    def make_partial(self, sandbox: Sandbox) -> None:
        self.pager.partial.add(sandbox.id)

    def make_failing(self, sandbox: Sandbox) -> None:
        self.pager.failing.add(sandbox.id)

    def hold_demotion(self) -> tuple[threading.Event, threading.Event]:
        self.pager.demote_gate = threading.Event()
        return self.pager.demote_entered, self.pager.demote_gate

    def test_anonymous_pages_are_kept_out_of_ssd_swap_for_the_sandbox(self) -> None:
        sandbox = self.new_sandbox()
        self.assertEqual((sandbox.cgroup_path / "memory.swap.max").read_text(), "0")
        self.backend.detach(sandbox)
        self.assertEqual((sandbox.cgroup_path / "memory.swap.max").read_text(), "max")

    def test_attach_fails_closed_when_swap_cannot_be_forbidden(self) -> None:
        cgroup = self.root / "cgroup" / "noswapfile"
        cgroup.mkdir(parents=True)
        with self.assertRaisesRegex(CXLTierError, "out of SSD swap"):
            self.backend.attach(Sandbox("noswapfile", cgroup))
        self.assertNotIn(("ATTACH", "noswapfile"), [call[:2] for call in self.pager.calls])

    def test_file_cache_reclaim_is_reported_separately_and_needs_swap_forbidden(self) -> None:
        with self.assertRaises(ValueError):
            CXLTierConfig(socket_path="x", file_cache_reclaim=True, forbid_swap=False)
        backend = CXLTierBackend(
            CXLTierConfig(socket_path="unused", reclaim_settle_seconds=0), client=self.pager  # type: ignore[arg-type]
        )
        sandbox = Sandbox("files", self.root / "cgroup" / "files")
        sandbox.cgroup_path.mkdir(parents=True)
        for file, value in {"memory.current": str(64 * MIB), "memory.swap.current": "0",
                            "memory.swap.max": "max", "cgroup.procs": ""}.items():
            (sandbox.cgroup_path / file).write_text(value)
        backend.attach(sandbox)
        self.regions = []
        result = backend.demote(sandbox, DemotionRequest(Tier.CXL, min_resident_bytes=32 * MIB))
        self.assertEqual((sandbox.cgroup_path / "memory.reclaim").read_text(), str(32 * MIB))
        self.assertEqual((result.stored_bytes, result.released_bytes), (0, 0))
        self.assertNotIn("DEMOTE", [call[0] for call in self.pager.calls])

    def test_lazy_refusal_falls_back_to_eager_or_fails_explicitly(self) -> None:
        self.pager.refuse_lazy = True
        for fallback in (True, False):
            backend = CXLTierBackend(
                CXLTierConfig(socket_path="unused", reclaim_settle_seconds=0,
                              file_cache_reclaim=False, restore_mode="lazy",
                              lazy_fallback_to_eager=fallback),
                client=self.pager,  # type: ignore[arg-type]
            )
            sandbox = Sandbox(f"lazy-{fallback}", self.root / "cgroup" / f"lazy-{fallback}")
            sandbox.cgroup_path.mkdir(parents=True)
            for file, value in {"memory.current": str(64 * MIB), "memory.swap.current": "0",
                                "memory.swap.max": "max", "cgroup.procs": "4321\n"}.items():
                (sandbox.cgroup_path / file).write_text(value)
            backend.attach(sandbox)
            self.pager.movable[sandbox.id] = 16 * MIB
            backend.demote(sandbox, DemotionRequest(Tier.CXL))
            if fallback:
                restored = backend.restore(sandbox, RestoreRequest())
                self.assertEqual(restored.tier_restored_bytes, 16 * MIB)
                self.assertEqual(backend.accounting(sandbox).details["lazy_refusals"], 1)
            else:
                with self.assertRaises(CXLTierError):
                    backend.restore(sandbox, RestoreRequest())

    def test_failed_restore_raises_so_the_execution_layer_cannot_dispatch(self) -> None:
        sandbox = self.new_sandbox()
        self.arm(sandbox, resident=64 * MIB, movable=32 * MIB)
        self.backend.demote(sandbox, DemotionRequest(Tier.CXL))
        self.make_failing(sandbox)
        with self.assertRaises(CXLTierError):
            self.backend.restore(sandbox, RestoreRequest())

    def test_close_refuses_while_sandboxes_are_attached(self) -> None:
        sandbox = self.new_sandbox()
        with self.assertRaisesRegex(CXLTierError, "attached"):
            self.backend.close()
        self.backend.detach(sandbox)
        self.backend.close()


class RegionDiscoveryTest(_Fixture):
    def test_only_shared_crate_tier_memfd_mappings_are_eligible(self) -> None:
        process = self.root / "77"
        (process / "fd").mkdir(parents=True)
        (process / "maps").write_text(
            "7f0000000000-7f0000400000 rw-s 00000000 00:01 900 /memfd:crate-tier.u9.k0 (deleted)\n"
            "7f0000400000-7f0000800000 rw-s 00400000 00:01 900 /memfd:crate-tier.u9.k0 (deleted)\n"
            "7f1000000000-7f1000100000 rw-p 00000000 00:01 901 /memfd:crate-tier.u9.k0 (deleted)\n"
            "7f2000000000-7f2000100000 rw-p 00000000 00:00 0 [heap]\n"
            "7f3000000000-7f3000100000 rw-s 00000000 00:01 902 /memfd:other (deleted)\n"
        )
        os.symlink("/memfd:crate-tier.u9.k0 (deleted)", process / "fd" / "5")
        os.symlink("/dev/null", process / "fd" / "6")

        class Status:
            st_ino = 900

        with patch("caden.cxl_tier.os.stat", return_value=Status()):
            regions = discover_regions(self.root, [77, 78])
        self.assertEqual(
            regions, [CoopRegion(77, 5, 9, 0x7F0000000000, 8 * MIB, 0, False)]
        )


class GenerationFenceTest(unittest.TestCase):
    def test_unfenced_requests_pass_and_older_generations_do_not(self) -> None:
        fence, sandbox = GenerationFence(), Sandbox("s", Path("/nonexistent"))
        fence.admit(sandbox, None)
        fence.admit(sandbox, 3)
        fence.admit(sandbox, 3)
        with self.assertRaises(StaleGenerationError):
            fence.admit(sandbox, 2)
        fence.forget(sandbox)
        fence.admit(sandbox, 0)


if __name__ == "__main__":
    unittest.main()
