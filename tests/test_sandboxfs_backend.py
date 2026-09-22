from __future__ import annotations

import dataclasses
import errno
import json
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import caden.sandboxfs_backend as backend
from caden.sandboxfs_backend import (
    SandboxFSConfig,
    SandboxFSError,
    SandboxFSExecution,
)
from caden.types import (
    AgentTask,
    CpuClass,
    DemotionRequest,
    ReclaimMode,
    RestoreRequest,
    Stage,
    Tier,
)


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.fail_exec = False
        self.cgroup: Path | None = None

    def __call__(
        self, argv: Sequence[str], timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.commands.append(command)
        if "create" in command:
            payload = {"id": "caden-test", "pid": 4321, "timings": {"total_ns": 10}}
        elif "exec-json" in command:
            if self.fail_exec:
                return subprocess.CompletedProcess(command, 137, "", "killed")
            payload = {"exit_code": 0, "stdout": "", "stderr": "", "duration_ns": 1}
        elif "destroy" in command:
            if self.cgroup is not None:
                (self.cgroup / "cgroup.procs").write_text("")
                if (self.cgroup / "cgroup.events").exists():
                    (self.cgroup / "cgroup.events").write_text("populated 0\nfrozen 0\n")
            payload = {"id": "caden-test", "cleanup_ns": 1}
        else:
            return subprocess.CompletedProcess(command, 1, "", "unexpected command")
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")


class SandboxFSExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.proc_root = root / "proc"
        self.cgroup_root = root / "cgroup"
        self.cgroup = self.cgroup_root / "system.slice" / "sandboxfs" / "caden-test"
        (self.proc_root / "4321").mkdir(parents=True)
        self.cgroup.mkdir(parents=True)
        (self.proc_root / "4321" / "cgroup").write_text(
            "0::/system.slice/sandboxfs/caden-test\n"
        )
        (self.proc_root / "4321" / "maps").write_text(
            "1000-3000 rw-p 00000000 00:00 0 [heap]\n"
            "4000-5000 r--p 00000000 00:00 0 /lib/excluded.so\n"
            "5000-6000 rw-p 00000000 00:00 0\n"
        )
        (self.cgroup / "cgroup.procs").write_text("4321\n")
        for name, value in {
            "cpu.weight": "100",
            "cgroup.freeze": "0",
            "memory.reclaim": "",
            "memory.current": "1048576",
            "memory.swap.current": "4096",
            "memory.zswap.current": "0",
            "memory.zswap.writeback": "1",
            "memory.zswap.max": "max",
            "memory.stat": "pgmajfault 7\nanon 1000\n",
            "cpu.stat": "usage_usec 1234\n",
        }.items():
            (self.cgroup / name).write_text(value)
        self.meminfo = root / "meminfo"
        self.meminfo.write_text("MemTotal:       65536 kB\nMemAvailable:   32768 kB\n")
        self.runner = FakeRunner()
        self.runner.cgroup = self.cgroup
        self.execution = SandboxFSExecution(
            SandboxFSConfig(
                proc_root=self.proc_root,
                cgroup_root=self.cgroup_root,
                meminfo_path=self.meminfo,
                reclaim_settle_seconds=0,
                prewarm_argv=("/bin/true",),
            ),
            runner=self.runner,
            id_factory=lambda: "caden-test",
        )

    def test_cache_delegation_must_match_actual_sandbox_service(self) -> None:
        delegated = self.cgroup_root.resolve() / "other.service" / "daemon"
        self.execution = SandboxFSExecution(dataclasses.replace(
            self.execution.config, service_cache_roots={"bench-medium": delegated}),
            runner=self.runner, id_factory=lambda: "caden-test")
        with self.assertRaisesRegex(SandboxFSError, "delegated cache service"):
            self.execution.run(AgentTask([], "bench-medium"), None)
        self.assertTrue(any("destroy" in command for command in self.runner.commands))
        self.assertEqual(self.execution.sandbox_ids(), [])

    def test_lifecycle_stats_and_controls(self) -> None:
        reports: list[tuple[str, Stage]] = []
        sandbox = self.execution.run(
            AgentTask(["true"], "bench-medium"),
            lambda identity, stage: reports.append((identity, stage)),
        )
        self.assertEqual(sandbox, "caden-test")
        self.assertEqual(self.execution.state(sandbox)["pid"], 4321)

        self.execution.report(sandbox, Stage.LLM_WAIT)
        self.assertEqual(reports, [(sandbox, Stage.LLM_WAIT)])
        self.execution.set_cpu(sandbox, CpuClass.IDLE)
        self.assertEqual((self.cgroup / "cpu.weight").read_text(), "1")

        reclaimed = self.execution.demote(sandbox, Tier.SSD)
        self.assertEqual(reclaimed, 0)
        self.assertEqual((self.cgroup / "cgroup.freeze").read_text(), "1")
        self.assertEqual((self.cgroup / "memory.reclaim").read_text(), "1048576")

        self.execution.restore(sandbox)
        self.assertEqual((self.cgroup / "cgroup.freeze").read_text(), "0")
        self.assertTrue(any("exec-json" in command for command in self.runner.commands))

        stat = self.execution.stat(sandbox)
        self.assertEqual(stat.mem_dram_bytes, 1048576)
        self.assertEqual(stat.mem_swap_bytes, 4096)
        self.assertEqual(stat.major_faults, 7)
        self.assertEqual(stat.cpu_usage_usec, 1234)
        host = self.execution.host_stat()
        self.assertEqual(host.dram_total_bytes, 65536 * 1024)
        self.assertEqual(host.dram_free_bytes, 32768 * 1024)

        self.execution.revoke(sandbox)
        self.assertEqual(self.execution.sandbox_ids(), [])

    def test_selective_compressed_reclaim_is_fail_closed_and_anon_only(self) -> None:
        enabled = Path(self.temporary.name) / "zswap-enabled"
        enabled.write_text("Y")
        execution = SandboxFSExecution(
            dataclasses.replace(
                self.execution.config,
                allow_zswap_compression=True,
                zswap_enabled_path=enabled,
                zswap_max_bytes=8 << 20,
            ),
            runner=self.runner,
            id_factory=lambda: "caden-test",
        )
        sandbox = execution.run(AgentTask([], "base"), lambda *_: None)
        result = execution.demote_selective(
            sandbox,
            DemotionRequest(
                tier=Tier.COMPRESSED,
                target_bytes=512 << 10,
                reclaim_mode=ReclaimMode.ANON_ONLY,
            ),
        )
        self.assertEqual(result.requested_bytes, 512 << 10)
        self.assertEqual(
            (self.cgroup / "memory.reclaim").read_text(),
            f"{512 << 10} swappiness=200",
        )
        self.assertEqual((self.cgroup / "memory.zswap.writeback").read_text(), "0")
        self.assertEqual((self.cgroup / "memory.zswap.max").read_text(), str(8 << 20))
        execution.restore_selective(sandbox, RestoreRequest())
        self.assertEqual((self.cgroup / "memory.zswap.writeback").read_text(), "1")

    def test_compressed_reclaim_requires_explicit_host_preflight(self) -> None:
        sandbox = self.execution.run(AgentTask([], "base"), lambda *_: None)
        with self.assertRaisesRegex(SandboxFSError, "fail-closed"):
            self.execution.demote_selective(
                sandbox,
                DemotionRequest(
                    tier=Tier.COMPRESSED,
                    reclaim_mode=ReclaimMode.ANON_ONLY,
                ),
            )

    def test_restore_mode_rejects_unknown_values(self) -> None:
        self.assertEqual(self.execution.config.restore_mode, "prewarm")
        with self.assertRaisesRegex(ValueError, "restore_mode"):
            dataclasses.replace(self.execution.config, restore_mode="unknown")

    def test_thaw_only_skips_prewarm_but_waits_for_confirmed_thaw(self) -> None:
        execution = SandboxFSExecution(
            dataclasses.replace(
                self.execution.config,
                restore_mode="thaw-only",
                prewarm_profiles={"python": ("/bin/false",)},
            ),
            runner=self.runner,
            id_factory=lambda: "caden-test",
        )
        sandbox = execution.run(AgentTask([], "base"), lambda *_: None)
        execution.demote(sandbox, Tier.SSD)
        (self.cgroup / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        self.runner.commands.clear()
        with (
            patch(
                "caden.sandboxfs_backend._read_key_values",
                side_effect=[{"frozen": 1}, {"frozen": 0}],
            ) as read_events,
            patch("caden.sandboxfs_backend.time.sleep") as wait,
        ):
            result = execution.restore_selective(
                sandbox, RestoreRequest(profile="python")
            )
        self.assertTrue(result.ready_for_dispatch)
        self.assertEqual(result.profile, "python")
        self.assertEqual(read_events.call_count, 2)
        wait.assert_called_once_with(0.005)
        self.assertEqual((self.cgroup / "cgroup.freeze").read_text(), "0")
        self.assertEqual(self.runner.commands, [])
        self.assertEqual(
            execution.memory_capabilities()["confirmed_restore_mode"], "thaw-only"
        )

    def test_thaw_only_does_not_dispatch_speculative_restore(self) -> None:
        execution = SandboxFSExecution(
            dataclasses.replace(self.execution.config, restore_mode="thaw-only"),
            runner=self.runner,
            id_factory=lambda: "caden-test",
        )
        sandbox = execution.run(AgentTask([], "base"), lambda *_: None)
        execution.demote(sandbox, Tier.SSD)
        self.runner.commands.clear()
        result = execution.restore_selective(sandbox, RestoreRequest(speculative=True))
        self.assertFalse(result.ready_for_dispatch)
        self.assertEqual((self.cgroup / "cgroup.freeze").read_text(), "1")
        self.assertEqual(self.runner.commands, [])

    def test_thaw_only_fails_closed_without_events(self) -> None:
        execution = SandboxFSExecution(
            dataclasses.replace(self.execution.config, restore_mode="thaw-only"),
            runner=self.runner,
            id_factory=lambda: "caden-test",
        )
        sandbox = execution.run(AgentTask([], "base"), lambda *_: None)
        execution.demote(sandbox, Tier.SSD)
        self.runner.commands.clear()
        with self.assertRaisesRegex(SandboxFSError, "confirmed thaw requires"):
            execution.restore_selective(sandbox, RestoreRequest())
        self.assertEqual((self.cgroup / "cgroup.freeze").read_text(), "1")
        self.assertEqual(execution._record(sandbox).last_tier, Tier.SSD)
        self.assertEqual(self.runner.commands, [])

    def test_thaw_only_timeout_is_not_dispatch_ready_and_restores_writeback(self) -> None:
        execution = SandboxFSExecution(
            dataclasses.replace(
                self.execution.config,
                restore_mode="thaw-only",
                freeze_timeout_seconds=0.001,
            ),
            runner=self.runner,
            id_factory=lambda: "caden-test",
        )
        sandbox = execution.run(AgentTask([], "base"), lambda *_: None)
        execution.demote(sandbox, Tier.SSD)
        (self.cgroup / "cgroup.events").write_text("populated 1\nfrozen 1\n")
        execution._record(sandbox).zswap_writeback_original = "1"
        (self.cgroup / "memory.zswap.writeback").write_text("0")
        self.runner.commands.clear()
        with self.assertRaisesRegex(SandboxFSError, "did not become thawed"):
            execution.restore_selective(sandbox, RestoreRequest())
        self.assertEqual(execution._record(sandbox).last_tier, Tier.SSD)
        self.assertEqual((self.cgroup / "memory.zswap.writeback").read_text(), "1")
        self.assertEqual(self.runner.commands, [])

    def test_thaw_only_leaves_faults_and_command_failure_to_real_exec(self) -> None:
        def actual_command(argv: Sequence[str], timeout: float | None = None):
            if "exec-json" not in argv:
                return self.runner(argv, timeout)
            self.runner.commands.append(list(argv))
            (self.cgroup / "memory.stat").write_text("pgmajfault 9\npgfault 11\n")
            payload = {
                "exit_code": 1,
                "stdout": "",
                "stderr": "actual command failed",
                "duration_ns": 7654321,
            }
            return subprocess.CompletedProcess(list(argv), 1, json.dumps(payload), "")

        execution = SandboxFSExecution(
            dataclasses.replace(self.execution.config, restore_mode="thaw-only"),
            runner=actual_command,
            id_factory=lambda: "caden-test",
        )
        sandbox = execution.run(AgentTask([], "base"), lambda *_: None)
        execution.demote(sandbox, Tier.SSD)
        (self.cgroup / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        self.runner.commands.clear()
        self.assertTrue(
            execution.restore_selective(sandbox, RestoreRequest()).ready_for_dispatch
        )
        self.assertEqual(self.runner.commands, [])
        self.assertEqual(execution.stat(sandbox).major_faults, 7)
        response = execution.exec(sandbox, ("/bin/false",))
        self.assertEqual(response["exit_code"], 1)
        self.assertEqual(response["duration_ns"], 7654321)
        self.assertEqual(execution.stat(sandbox).major_faults, 9)
        self.assertEqual(len(self.runner.commands), 1)
        self.assertEqual(self.runner.commands[0][-1], "/bin/false")

    def test_speculative_prefetch_requires_an_explicit_root(self) -> None:
        path = Path(self.temporary.name) / "hot-profile"
        with self.assertRaisesRegex(ValueError, "explicit immutable root"):
            dataclasses.replace(
                self.execution.config,
                speculative_prefetch_profiles={"default": (path,)},
            )

    def test_speculative_restore_prefetches_without_dispatch(self) -> None:
        profile_file = Path(self.temporary.name) / "hot-profile"
        profile_file.write_bytes(b"profile-data")
        execution = SandboxFSExecution(
            dataclasses.replace(
                self.execution.config,
                speculative_prefetch_profiles={"python": (profile_file,)},
                speculative_prefetch_roots=(Path(self.temporary.name),),
            ),
            runner=self.runner,
            id_factory=lambda: "caden-test",
        )
        sandbox = execution.run(AgentTask([], "base"), lambda *_: None)
        execution.demote(sandbox, Tier.SSD)
        self.runner.commands.clear()
        result = execution.restore_selective(
            sandbox,
            RestoreRequest(profile="python", speculative=True),
        )
        self.assertEqual(result.prefetched_bytes, len(b"profile-data"))
        self.assertFalse(result.ready_for_dispatch)
        self.assertEqual((self.cgroup / "cgroup.freeze").read_text(), "1")
        self.assertFalse(
            any("exec-json" in command for command in self.runner.commands)
        )

        execution.restore_selective(
            sandbox,
            RestoreRequest(profile="python", speculative=False),
        )
        self.assertTrue(any("exec-json" in command for command in self.runner.commands))
        self.assertEqual((self.cgroup / "cgroup.freeze").read_text(), "0")

    def test_speculative_process_madvise_uses_exact_cgroup_pids(self) -> None:
        execution = SandboxFSExecution(
            dataclasses.replace(
                self.execution.config,
                allow_process_madvise_restore=True,
                speculative_madvise_max_bytes=12 << 10,
            ),
            runner=self.runner,
            id_factory=lambda: "caden-test",
        )
        sandbox = execution.run(AgentTask([], "base"), lambda *_: None)
        (self.cgroup / "cgroup.freeze").write_text("1")
        with (
            patch("caden.sandboxfs_backend._open_pidfd", return_value=99),
            patch("caden.sandboxfs_backend.os.close") as close_pidfd,
            patch(
                "caden.sandboxfs_backend._process_madvise_willneed",
                side_effect=lambda _pid, _pidfd, ranges: sum(
                    length for _, length in ranges
                ),
            ) as advise,
        ):
            result = execution.restore_selective(
                sandbox,
                RestoreRequest(speculative=True),
            )

        self.assertEqual(result.advised_bytes, 12 << 10)
        self.assertFalse(result.ready_for_dispatch)
        self.assertEqual((self.cgroup / "cgroup.freeze").read_text(), "1")
        pid, pidfd, ranges = advise.call_args.args
        self.assertEqual((pid, pidfd), (4321, 99))
        self.assertEqual(ranges, [(0x1000, 0x2000), (0x5000, 0x1000)])
        close_pidfd.assert_called_once_with(99)

    def test_speculative_process_madvise_can_require_populate_read(self) -> None:
        execution = SandboxFSExecution(
            dataclasses.replace(
                self.execution.config,
                allow_process_madvise_restore=True,
                speculative_madvise_max_bytes=12 << 10,
                speculative_madvise_passes=2,
                speculative_madvise_advice="populate-read",
            ),
            runner=self.runner,
            id_factory=lambda: "caden-test",
        )
        sandbox = execution.run(AgentTask([], "base"), lambda *_: None)
        with (
            patch("caden.sandboxfs_backend._open_pidfd", return_value=99),
            patch("caden.sandboxfs_backend.os.close"),
            patch(
                "caden.sandboxfs_backend._process_madvise_populate_read",
                side_effect=lambda _pid, _pidfd, ranges: sum(
                    length for _, length in ranges
                ),
            ) as populate,
            patch("caden.sandboxfs_backend._process_madvise_willneed") as willneed,
        ):
            result = execution.restore_selective(
                sandbox,
                RestoreRequest(speculative=True),
            )

        self.assertEqual(result.advised_bytes, 24 << 10)
        self.assertEqual(populate.call_count, 2)
        willneed.assert_not_called()

    def test_rejects_unsupported_cxl_placement(self) -> None:
        sandbox = self.execution.run(AgentTask([], "base"), lambda *_: None)
        with self.assertRaises(SandboxFSError):
            self.execution.demote(sandbox, Tier.CXL)

    def test_partial_eagain_reclaim_reports_actual_progress(self) -> None:
        sandbox = self.execution.run(AgentTask([], "base"), lambda *_: None)
        real_write = backend._write

        def partial_write(path: Path, value: str) -> None:
            if path.name != "memory.reclaim":
                real_write(path, value)
                return
            (self.cgroup / "memory.current").write_text("262144")
            try:
                raise OSError(errno.EAGAIN, "partial reclaim")
            except OSError as cause:
                raise SandboxFSError("partial reclaim") from cause

        with patch("caden.sandboxfs_backend._write", side_effect=partial_write):
            reclaimed = self.execution.demote(sandbox, Tier.SSD)

        self.assertEqual(reclaimed, 786432)

    def test_exec_surfaces_process_failure_before_json_decode(self) -> None:
        sandbox = self.execution.run(AgentTask([], "base"), lambda *_: None)
        self.runner.fail_exec = True
        with self.assertRaisesRegex(SandboxFSError, r"command failed \(137\).*killed"):
            self.execution.exec(sandbox, ("/bin/true",))

    def test_exec_returns_json_for_nonzero_sandbox_command(self) -> None:
        sandbox = self.execution.run(AgentTask([], "base"), lambda *_: None)

        def command_failed(argv: Sequence[str], timeout: float | None = None):
            command = list(argv)
            if "exec-json" in command:
                payload = {"exit_code": 1, "stdout": "", "stderr": "not ready"}
                return subprocess.CompletedProcess(command, 1, json.dumps(payload), "")
            return self.runner(argv, timeout)

        execution = SandboxFSExecution(
            self.execution.config,
            runner=command_failed,
            id_factory=lambda: "caden-test",
        )
        execution.run(AgentTask([], "base"), lambda *_: None)
        response = execution.exec(sandbox, ("/bin/false",))
        self.assertEqual(response["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
