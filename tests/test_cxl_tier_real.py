"""REAL PAGING tests for the DRAM-CXL tier (Linux only, file-emulated store).

Evidence class: actual page-out/page-in of a live process's memory through
``crate_pagerd`` - resident pages are released (checked independently through
the owner's ``RssShmem``) and restored with verified contents. The store medium
here is a regular file, so nothing in this module is a CXL hardware result, and
the cgroup control files are synthetic (quiescence is a real ``SIGSTOP`` driven
by a fake freezer). Mock-only contract tests live in
``test_memory_tier_contract.py``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from dataclasses import dataclass
from pathlib import Path

from caden.cxl_tier import (
    CXLTierBackend,
    CXLTierConfig,
    CXLTierError,
    PagerClient,
    PagerDaemon,
    PagerError,
)
from caden.memory_tier import (
    MemoryTierBackend,
    StaleGenerationError,
    TierCapacityError,
    UnsupportedTierError,
)
from caden.sandboxfs_backend import SandboxFSConfig, SandboxFSExecution
from caden.types import AgentTask, DemotionRequest, RestoreRequest, Tier

BUILD = Path(__file__).resolve().parents[1] / "native" / "cxl_coldstore" / "build"
PAGERD = BUILD / "crate_pagerd"
HOLDER = BUILD / "coop_holder"
MIB = 1 << 20

_SKIP = ""
if sys.platform != "linux":
    _SKIP = "unsupported platform: real paging needs Linux memfd/pidfd/userfaultfd"
elif not (PAGERD.exists() and HOLDER.exists()):
    _SKIP = f"native pager not built: run make -C {BUILD.parent}"


@dataclass
class Sandbox:
    id: str
    cgroup_path: Path
    incarnation: int = 1


class Holder:
    """A live cooperative process plus its synthetic cgroup directory."""

    def __init__(self, root: Path, name: str, size_mib: int, *, uffd: bool = True) -> None:
        self.socket = root / f"{name}.sock"
        environment = dict(os.environ, CRATE_COOP_PTRACER_ANY="1")
        if not uffd:
            environment["CRATE_COOP_NO_UFFD"] = "1"
        self.process = subprocess.Popen(
            [str(HOLDER), "serve", "--socket", str(self.socket), "--size-mib", str(size_mib)],
            stdout=subprocess.PIPE,
            env=environment,
        )
        assert self.process.stdout is not None
        self.ready = json.loads(self.process.stdout.readline())
        self.pid = self.process.pid
        self.cgroup = root / "cgroup" / name
        self.cgroup.mkdir(parents=True)
        for file, value in {
            "cgroup.procs": f"{self.pid}\n",
            "cgroup.events": "populated 1\nfrozen 0\n",
            "cgroup.freeze": "0",
            "memory.current": str((size_mib + 64) * MIB),
            "memory.swap.current": "0",
            "memory.swap.max": "max",
            "memory.reclaim": "",
            "memory.stat": "pgmajfault 0\npgfault 0\n",
            "cpu.stat": "usage_usec 0\n",
            "cpu.weight": "100",
        }.items():
            (self.cgroup / file).write_text(value)
        self.sandbox = Sandbox(name, self.cgroup)

    def freeze(self) -> None:
        os.kill(self.pid, signal.SIGSTOP)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = Path(f"/proc/{self.pid}/status").read_text()
            if "State:\tT" in state:
                break
            time.sleep(0.002)
        (self.cgroup / "cgroup.events").write_text("populated 1\nfrozen 1\n")

    def thaw(self) -> None:
        (self.cgroup / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        os.kill(self.pid, signal.SIGCONT)

    def ask(self, verb: str, timeout: float = 60) -> dict[str, object]:
        completed = subprocess.run(
            [str(HOLDER), verb, "--socket", str(self.socket)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return json.loads(completed.stdout)

    def rss_shmem(self) -> int:
        for line in Path(f"/proc/{self.pid}/status").read_text().splitlines():
            if line.startswith("RssShmem:"):
                return int(line.split()[1]) * 1024
        return -1

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        if self.process.stdout is not None:
            self.process.stdout.close()


class _PagingFixture(unittest.TestCase):
    store_mib = 96
    daemon_options: dict[str, object] = {}

    def setUp(self) -> None:
        if _SKIP:
            self.skipTest(_SKIP)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = self.root / "store.bin"
        with self.store.open("wb") as handle:
            handle.truncate(self.store_mib * MIB)
        self.daemon = PagerDaemon(
            PAGERD,
            self.root / "pager.sock",
            store_path=self.store,
            offset=0,
            capacity=self.store_mib * MIB,
            logical_bytes=(self.store_mib - 1) * MIB,
            cgroup_root=self.root / "cgroup",
            log_path=self.root / "pagerd.log",
            **self.daemon_options,
        )
        self.daemon.start()
        self.addCleanup(self._stop_daemon)
        self.holders: list[Holder] = []

    def _stop_daemon(self) -> None:
        for holder in self.holders:
            holder.close()
        if self.daemon.process is not None and self.daemon.process.poll() is None:
            self.daemon.stop(force=True)

    def holder(self, name: str = "sbx", size_mib: int = 16, **options: bool) -> Holder:
        holder = Holder(self.root, name, size_mib, **options)
        self.holders.append(holder)
        return holder

    def backend(self, **options: object) -> CXLTierBackend:
        # Kernels without userfaultfd (some container hosts) can still exercise
        # eager paging; production keeps such regions ineligible by default.
        options.setdefault("allow_regions_without_userfaultfd", True)
        config = CXLTierConfig(
            socket_path=str(self.root / "pager.sock"),
            reclaim_settle_seconds=0,
            **options,  # type: ignore[arg-type]
        )
        return CXLTierBackend(config)

    def require_uffd(self, holder: Holder) -> None:
        if int(holder.ready["uffd"]) < 0:
            self.skipTest("unsupported kernel: userfaultfd unavailable, lazy paging not run")


@unittest.skipIf(bool(_SKIP), _SKIP)
class RealPagingTest(_PagingFixture):
    def test_lost_demotion_reply_restores_verified_contents(self) -> None:
        holder, backend = self.holder(size_mib=4), self.backend()
        backend.attach(holder.sandbox)
        holder.freeze()
        original = backend._client.call

        def lose_reply(*tokens):
            result = original(*tokens)
            if tokens[0] == "DEMOTE":
                raise CXLTierError("test: lost native reply after page release")
            return result

        with patch.object(backend._client, "call", side_effect=lose_reply):
            with self.assertRaises(CXLTierError):
                backend.demote(holder.sandbox, DemotionRequest(Tier.CXL))
        self.assertEqual(backend.accounting(holder.sandbox).resident_in_tier_bytes, 4 * MIB)
        restored = backend.restore(holder.sandbox, RestoreRequest())
        self.assertEqual(restored.tier_restored_bytes, 4 * MIB)
        holder.thaw()
        self.assertTrue(holder.ask("check")["ok"])
        backend.detach(holder.sandbox)

    def test_shutdown_drains_idle_connections(self) -> None:
        clients = [PagerClient(str(self.root / "pager.sock"), 5) for _ in range(8)]
        for client in clients:
            self.addCleanup(client.close)
            client.call("HELLO")
        self.daemon.stop(timeout=5)
        self.assertFalse((self.root / "pager.sock").exists())

    def test_shutdown_waits_for_demotion_before_cold_registration(self) -> None:
        self.daemon.stop()
        library = self.root / "pause.so"
        subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                        str(Path(__file__).with_name("pager_pause.c")), "-ldl", "-o", str(library)], check=True)
        ready, release = self.root / "punch-ready", self.root / "punch-release"
        with patch.dict(os.environ, {"LD_PRELOAD": str(library),
                                    "CRATE_TEST_PUNCH_READY": str(ready),
                                    "CRATE_TEST_PUNCH_RELEASE": str(release)}):
            self.daemon.start()
        holder, backend = self.holder(size_mib=4), self.backend()
        backend.attach(holder.sandbox)
        holder.freeze()
        errors, shutdown_errors = [], []
        stopped = threading.Event()

        def demote():
            try:
                backend.demote(holder.sandbox, DemotionRequest(Tier.CXL))
            except Exception as error:
                errors.append(error)

        def shutdown():
            client = PagerClient(str(self.root / "pager.sock"), 10)
            try:
                client.call("SHUTDOWN")
            except Exception as error:
                shutdown_errors.append(error)
            finally:
                client.close()
                stopped.set()

        demoter = threading.Thread(target=demote)
        demoter.start()
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists(), errors)
        stopper = threading.Thread(target=shutdown)
        stopper.start()
        try:
            self.assertFalse(stopped.wait(.1), "shutdown raced past an in-flight demotion")
        finally:
            release.touch()
            demoter.join(10)
            stopper.join(10)
        self.assertFalse(demoter.is_alive() or stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(shutdown_errors), 1)
        self.assertIsInstance(shutdown_errors[0], PagerError)
        self.assertEqual(shutdown_errors[0].errno, 16)  # EBUSY, not silent exit
        self.assertIsNone(self.daemon.process.poll())
        backend.restore(holder.sandbox, RestoreRequest())
        holder.thaw()
        self.assertTrue(holder.ask("check")["ok"])
        backend.detach(holder.sandbox)

    def test_backend_satisfies_shared_protocol_and_reports_emulation(self) -> None:
        backend = self.backend()
        self.assertIsInstance(backend, MemoryTierBackend)
        capabilities = backend.capabilities()
        self.assertEqual(capabilities.supported_tiers, frozenset({Tier.CXL}))
        self.assertEqual(capabilities.medium, "file-emulation")
        self.assertEqual(capabilities.evidence_class, "file-emulation-cooperative-paging")
        self.assertFalse(capabilities.details["transparent_for_unmodified_tools"])
        with self.assertRaises(UnsupportedTierError):
            backend.validate(Tier.SSD)

    def test_eager_round_trip_releases_source_pages_and_restores_contents(self) -> None:
        holder, backend = self.holder(size_mib=16), self.backend()
        backend.attach(holder.sandbox)
        self.assertTrue(holder.ask("check")["ok"])
        hot = holder.rss_shmem()
        holder.freeze()
        result = backend.demote(holder.sandbox, DemotionRequest(Tier.CXL, generation=1))
        self.assertEqual(result.backend, "cxl")
        self.assertEqual(result.stored_bytes, 16 * MIB)
        self.assertEqual(result.released_bytes, 16 * MIB)
        self.assertEqual(result.swap_delta_bytes, 0)
        # Independent of the daemon's receipt: the owner's resident shmem fell.
        self.assertLessEqual(holder.rss_shmem(), hot - 15 * MIB)
        self.assertEqual((holder.cgroup / "memory.swap.max").read_text(), "0")
        speculative = backend.restore(
            holder.sandbox, RestoreRequest(speculative=True, generation=1)
        )
        self.assertFalse(speculative.ready_for_dispatch)
        self.assertEqual(speculative.tier_restored_bytes, 16 * MIB)
        confirmed = backend.restore(holder.sandbox, RestoreRequest(generation=2))
        self.assertTrue(confirmed.ready_for_dispatch)
        self.assertEqual(confirmed.lazy_pending_bytes, 0)
        holder.thaw()
        check = holder.ask("check")
        self.assertTrue(check["ok"], check)
        accounting = backend.accounting(holder.sandbox)
        self.assertEqual(accounting.stored_bytes, 16 * MIB)
        self.assertEqual(accounting.released_bytes, 16 * MIB)
        self.assertEqual(accounting.restored_bytes, 16 * MIB)
        self.assertEqual(accounting.resident_in_tier_bytes, 0)
        self.assertEqual(accounting.demand_faults, 0)
        self.assertGreater(accounting.metadata_bytes, 0)
        backend.detach(holder.sandbox)
        self.assertEqual((holder.cgroup / "memory.swap.max").read_text(), "max")
        backend.close()

    def test_lazy_restore_serves_demand_faults_inside_the_tool_call(self) -> None:
        holder = self.holder(size_mib=8)
        self.require_uffd(holder)
        backend = self.backend(restore_mode="lazy", allow_user_mode_only_lazy=True)
        backend.attach(holder.sandbox)
        holder.freeze()
        demoted = backend.demote(holder.sandbox, DemotionRequest(Tier.CXL, generation=1))
        restored = backend.restore(holder.sandbox, RestoreRequest(generation=2))
        self.assertTrue(restored.ready_for_dispatch)
        self.assertEqual(restored.tier_restored_bytes, 0)
        self.assertEqual(restored.lazy_pending_bytes, demoted.stored_bytes)
        holder.thaw()
        self.assertTrue(holder.ask("mutate")["ok"])
        accounting = backend.accounting(holder.sandbox)
        self.assertGreater(accounting.demand_faults, 0)
        self.assertEqual(accounting.details["restored_fault_bytes"], demoted.stored_bytes)
        self.assertEqual(accounting.resident_in_tier_bytes, 0)
        # A second cycle over mutated contents, eager this time.
        holder.freeze()
        backend.demote(holder.sandbox, DemotionRequest(Tier.CXL, generation=3))
        backend.restore(holder.sandbox, RestoreRequest(speculative=True, generation=3))
        holder.thaw()
        self.assertTrue(holder.ask("check")["ok"])
        backend.detach(holder.sandbox)

    def test_user_mode_only_lazy_is_refused_then_falls_back_to_eager(self) -> None:
        holder = self.holder(size_mib=4)
        self.require_uffd(holder)
        if holder.ready["kernel_faults"]:
            self.skipTest("holder obtained a kernel-fault-capable userfaultfd")
        backend = self.backend(restore_mode="lazy")
        backend.attach(holder.sandbox)
        holder.freeze()
        backend.demote(holder.sandbox, DemotionRequest(Tier.CXL))
        restored = backend.restore(holder.sandbox, RestoreRequest())
        self.assertEqual(restored.tier_restored_bytes, 4 * MIB)
        self.assertEqual(backend.accounting(holder.sandbox).details["lazy_refusals"], 1)
        holder.thaw()
        self.assertTrue(holder.ask("sysread")["ok"])
        backend.detach(holder.sandbox)

    def test_region_without_userfaultfd_supports_only_eager_restore(self) -> None:
        holder = self.holder(size_mib=4, uffd=False)
        backend = self.backend(restore_mode="lazy", lazy_fallback_to_eager=False)
        backend.attach(holder.sandbox)
        holder.freeze()
        backend.demote(holder.sandbox, DemotionRequest(Tier.CXL))
        with self.assertRaises(CXLTierError):
            backend.restore(holder.sandbox, RestoreRequest())
        eager = self.backend()
        eager._attached = backend._attached  # same daemon-side attachment
        self.assertEqual(
            eager.restore(holder.sandbox, RestoreRequest()).tier_restored_bytes, 4 * MIB
        )
        holder.thaw()
        self.assertTrue(holder.ask("check")["ok"])
        backend.detach(holder.sandbox)

    def test_regions_without_userfaultfd_are_ineligible_by_default(self) -> None:
        holder = self.holder(size_mib=4, uffd=False)
        backend = self.backend(allow_regions_without_userfaultfd=False)
        backend.attach(holder.sandbox)
        holder.freeze()
        result = backend.demote(holder.sandbox, DemotionRequest(Tier.CXL))
        self.assertEqual((result.stored_bytes, result.released_bytes), (0, 0))
        accounting = backend.accounting(holder.sandbox)
        self.assertEqual(accounting.details["skipped_regions_without_uffd"], 1)
        holder.thaw()
        self.assertTrue(holder.ask("check")["ok"])
        backend.detach(holder.sandbox)

    def test_demotion_requires_confirmed_quiescence(self) -> None:
        holder, backend = self.holder(size_mib=4), self.backend()
        backend.attach(holder.sandbox)
        with self.assertRaisesRegex(CXLTierError, "frozen"):
            backend.demote(holder.sandbox, DemotionRequest(Tier.CXL))
        self.assertTrue(holder.ask("check")["ok"])
        self.assertEqual(backend.accounting(holder.sandbox).stored_bytes, 0)
        backend.detach(holder.sandbox)

    def test_stale_generation_is_fenced_before_any_movement(self) -> None:
        holder, backend = self.holder(size_mib=4), self.backend()
        backend.attach(holder.sandbox)
        holder.freeze()
        backend.demote(holder.sandbox, DemotionRequest(Tier.CXL, generation=5))
        backend.restore(holder.sandbox, RestoreRequest(generation=6))
        with self.assertRaises(StaleGenerationError):
            backend.demote(holder.sandbox, DemotionRequest(Tier.CXL, generation=5))
        # The daemon fences independently of the Python-side fence.
        with self.assertRaises(PagerError) as raised:
            PagerClient(str(self.root / "pager.sock"), 10).call("DEMOTE", "sbx", 1, 5, -1)
        self.assertEqual(raised.exception.errno, 116)  # ESTALE
        self.assertEqual(backend.accounting(holder.sandbox).resident_in_tier_bytes, 0)
        holder.thaw()
        self.assertTrue(holder.ask("check")["ok"])
        backend.detach(holder.sandbox)

    def test_partial_target_leaves_the_rest_resident(self) -> None:
        holder, backend = self.holder(size_mib=8), self.backend(file_cache_reclaim=False)
        backend.attach(holder.sandbox)
        holder.freeze()
        result = backend.demote(
            holder.sandbox, DemotionRequest(Tier.CXL, target_bytes=3 * MIB)
        )
        self.assertEqual(result.requested_bytes, 3 * MIB)
        self.assertEqual(result.stored_bytes, 3 * MIB)
        self.assertEqual(result.released_bytes, 3 * MIB)
        self.assertGreaterEqual(result.eligible_bytes, 3 * MIB)
        backend.restore(holder.sandbox, RestoreRequest())
        holder.thaw()
        self.assertTrue(holder.ask("check")["ok"])
        backend.detach(holder.sandbox)

    def test_per_sandbox_allocation_is_bounded(self) -> None:
        holder = self.holder(size_mib=8)
        backend = self.backend(per_sandbox_max_bytes=4 * MIB)
        backend.attach(holder.sandbox)
        holder.freeze()
        with self.assertRaises(TierCapacityError):
            backend.demote(holder.sandbox, DemotionRequest(Tier.CXL))
        holder.thaw()
        self.assertTrue(holder.ask("check")["ok"])
        backend.detach(holder.sandbox)

    def test_store_corruption_fails_closed_and_poisons_the_sandbox(self) -> None:
        holder, backend = self.holder(size_mib=4), self.backend()
        backend.attach(holder.sandbox)
        holder.freeze()
        backend.demote(holder.sandbox, DemotionRequest(Tier.CXL))
        with self.store.open("r+b") as handle:  # corrupt every stored payload
            handle.write(b"\xa5" * (8 * MIB))
        with self.assertRaisesRegex(CXLTierError, "RESTORE"):
            backend.restore(holder.sandbox, RestoreRequest())
        accounting = backend.accounting(holder.sandbox)
        self.assertTrue(accounting.details["poisoned"])
        self.assertGreater(accounting.errors, 0)
        with self.assertRaises(CXLTierError):  # never dispatched, never re-demoted
            backend.demote(holder.sandbox, DemotionRequest(Tier.CXL))
        holder.close()
        self.assertGreater(backend.detach(holder.sandbox).details["discarded_bytes"], 0)

    def test_store_owner_refuses_shutdown_while_consumers_are_cold(self) -> None:
        holder, backend = self.holder(size_mib=4), self.backend()
        backend.attach(holder.sandbox)
        holder.freeze()
        backend.demote(holder.sandbox, DemotionRequest(Tier.CXL))
        with self.assertRaises(PagerError) as raised:
            PagerClient(str(self.root / "pager.sock"), 10).call("SHUTDOWN")
        self.assertEqual(raised.exception.errno, 16)  # EBUSY
        with self.assertRaisesRegex(CXLTierError, "attached"):
            backend.close()
        backend.restore(holder.sandbox, RestoreRequest())
        holder.thaw()
        self.assertTrue(holder.ask("check")["ok"])
        backend.detach(holder.sandbox)
        backend.close()
        self.daemon.stop()
        self.assertFalse((self.root / "pager.sock").exists())

    def test_owner_failure_is_fail_stop_not_silent_zero_fill(self) -> None:
        holder = self.holder(size_mib=4)
        self.require_uffd(holder)
        backend = self.backend(restore_mode="lazy", allow_user_mode_only_lazy=True)
        backend.attach(holder.sandbox)
        holder.freeze()
        backend.demote(holder.sandbox, DemotionRequest(Tier.CXL))
        backend.restore(holder.sandbox, RestoreRequest())
        assert self.daemon.process is not None
        self.daemon.process.kill()
        self.daemon.process.wait()
        holder.thaw()
        # The consumer blocks on its first cold page; it never sees zeros.
        with self.assertRaises(subprocess.TimeoutExpired):
            holder.ask("check", timeout=2)
        with self.assertRaises(CXLTierError):
            backend.accounting(holder.sandbox)

    def test_detach_after_owner_exit_discards_every_page(self) -> None:
        holder, backend = self.holder(size_mib=4), self.backend()
        backend.attach(holder.sandbox)
        holder.freeze()
        backend.demote(holder.sandbox, DemotionRequest(Tier.CXL))
        holder.close()
        accounting = backend.detach(holder.sandbox)
        self.assertEqual(accounting.details["discarded_bytes"], 4 * MIB)
        stats = backend.store_stats()
        self.assertEqual((stats["cold"], stats["regions"], stats["sandboxes"]), ("0", "0", "0"))
        self.assertEqual(stats["written_pages"], "0")

    def test_wake_synchronizes_with_in_flight_demotion_across_sandboxes(self) -> None:
        holders = [self.holder(f"sbx{n}", size_mib=12) for n in range(3)]
        backend = self.backend()
        errors: list[BaseException] = []

        def cycle(holder: Holder) -> None:
            try:
                backend.attach(holder.sandbox)
                holder.freeze()
                demotion = threading.Thread(
                    target=backend.demote,
                    args=(holder.sandbox, DemotionRequest(Tier.CXL, generation=1)),
                )
                demotion.start()
                # Overlapping wake: waits for the demotion, then restores it all.
                restored = backend.restore(holder.sandbox, RestoreRequest(generation=2))
                demotion.join()
                if backend.accounting(holder.sandbox).resident_in_tier_bytes:
                    backend.restore(holder.sandbox, RestoreRequest(generation=3))
                holder.thaw()
                assert holder.ask("check")["ok"], restored
                backend.detach(holder.sandbox)
            except BaseException as error:  # noqa: BLE001 - surfaced below
                errors.append(error)

        threads = [threading.Thread(target=cycle, args=(holder,)) for holder in holders]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(backend.store_stats()["cold"], "0")


class _FakeFreezer(threading.Thread):
    """Turns writes to the synthetic ``cgroup.freeze`` into a real SIGSTOP."""

    def __init__(self, holder: Holder) -> None:
        super().__init__(daemon=True)
        self.holder, self.running, self.state = holder, True, "0"

    def run(self) -> None:
        while self.running:
            wanted = (self.holder.cgroup / "cgroup.freeze").read_text().strip() or "0"
            if wanted != self.state:
                self.holder.freeze() if wanted == "1" else self.holder.thaw()
                self.state = wanted
            time.sleep(0.002)


@unittest.skipIf(bool(_SKIP), _SKIP)
class ExecutionIntegrationTest(_PagingFixture):
    """The injected backend under the real execution-layer ordering."""

    def execution(self, holder: Holder, backend: CXLTierBackend) -> SandboxFSExecution:
        proc = self.root / "proc"
        (proc / "4321").mkdir(parents=True)
        (proc / "4321" / "cgroup").write_text(f"0::/{holder.cgroup.name}\n")
        commands: list[list[str]] = []
        self.commands = commands

        def runner(argv, timeout=None):  # noqa: ANN001, ANN202
            commands.append(list(argv))
            if "destroy" in argv:
                freezer.running = False
                freezer.join(5)
                holder.close()
                (holder.cgroup / "cgroup.procs").write_text("")
                (holder.cgroup / "cgroup.events").write_text("populated 0\nfrozen 0\n")
            payload = {"id": holder.sandbox.id, "pid": 4321, "exit_code": 0, "duration_ns": 1}
            return subprocess.CompletedProcess(list(argv), 0, json.dumps(payload), "")

        freezer = _FakeFreezer(holder)
        freezer.start()
        def stop_freezer():
            freezer.running = False
            freezer.join(5)
        self.addCleanup(stop_freezer)
        return SandboxFSExecution(
            SandboxFSConfig(
                proc_root=proc,
                cgroup_root=self.root / "cgroup",
                restore_mode="thaw-only",
                freeze_timeout_seconds=5,
            ),
            runner=runner,
            id_factory=lambda: holder.sandbox.id,
            memory_tier=backend,
        )

    def test_demote_wake_and_teardown_through_the_execution_layer(self) -> None:
        holder, backend = self.holder(size_mib=8), self.backend()
        execution = self.execution(holder, backend)
        sandbox = execution.run(AgentTask([], "base"), lambda *_: None)
        self.assertEqual(
            execution.memory_capabilities()["memory_tier_backend"]["backend"], "cxl"
        )
        with self.assertRaises(UnsupportedTierError):
            execution.demote(sandbox, Tier.SSD)  # never silently substituted
        result = execution.demote_selective(sandbox, DemotionRequest(Tier.CXL, generation=1))
        self.assertEqual(result.stored_bytes, 8 * MIB)
        prepared = execution.restore_selective(
            sandbox, RestoreRequest(speculative=True, generation=1)
        )
        self.assertFalse(prepared.ready_for_dispatch)
        self.assertEqual((holder.cgroup / "cgroup.freeze").read_text(), "1")
        woken = execution.restore_selective(sandbox, RestoreRequest(generation=2))
        self.assertTrue(woken.ready_for_dispatch)
        self.assertEqual(woken.backend, "cxl")
        self.assertTrue(holder.ask("check")["ok"])
        execution.demote_selective(sandbox, DemotionRequest(Tier.CXL, generation=3))
        execution.revoke(sandbox)  # restore -> thaw -> destroy -> discard
        self.assertTrue(any("destroy" in command for command in self.commands))
        self.assertEqual(backend.store_stats()["sandboxes"], "0")
        self.assertIsNotNone(holder.process.poll())

    def test_failed_restore_never_thaws_or_dispatches(self) -> None:
        holder, backend = self.holder(size_mib=4), self.backend()
        execution = self.execution(holder, backend)
        sandbox = execution.run(AgentTask([], "base"), lambda *_: None)
        execution.demote_selective(sandbox, DemotionRequest(Tier.CXL))
        with self.store.open("r+b") as handle:
            handle.write(b"\x5a" * (8 * MIB))
        self.commands.clear()
        with self.assertRaises(CXLTierError):
            execution.restore_selective(sandbox, RestoreRequest())
        self.assertEqual((holder.cgroup / "cgroup.freeze").read_text(), "1")
        self.assertEqual(self.commands, [])

    def test_corrupt_restore_teardown_kills_without_thaw_before_detach(self) -> None:
        holder, backend = self.holder(size_mib=4), self.backend()
        execution = self.execution(holder, backend)
        sandbox = execution.run(AgentTask([], "base"), lambda *_: None)
        execution.demote_selective(sandbox, DemotionRequest(Tier.CXL))
        with self.store.open("r+b") as handle:
            handle.write(b"\x5a" * (8 * MIB))
        with patch.object(execution, "_set_frozen", wraps=execution._set_frozen) as freezer:
            execution.revoke(sandbox)
        freezer.assert_not_called()
        self.assertIsNotNone(holder.process.poll())
        self.assertEqual(backend.store_stats()["cold"], "0")
        self.assertEqual(execution.sandbox_ids(), [])


if __name__ == "__main__":
    unittest.main()
