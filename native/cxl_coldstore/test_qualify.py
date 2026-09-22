#!/usr/bin/env python3
"""Pure/mocked qualification checks: no device access, subprocesses or hardware."""
import copy
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("qualify", Path(__file__).with_name("qualify.py"))
q = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(q)


def completed(stdout="", code=0, stderr=""):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)


def probe_result(hardware=False, pattern="mixed", system="Linux", codec="none"):
    pages = q.LOGICAL_BYTES // 4096
    raw = codec == "none" or pattern == "random"
    zeros = codec == "lz4" and pattern == "zeros"
    return {"schema": "crate-cold-tier-backend-probe-v2", "success": True, "codec": codec,
            "backend": "device-dax" if hardware else "regular-file-emulation",
            "pattern": pattern, "offset": q.DAX_OFFSET if hardware else q.GUARD_BYTES,
            "mapped_capacity": q.CAPACITY, "source_bytes": q.LOGICAL_BYTES,
            "source_release_munmap_succeeded": True,
            "source_mapping_absent_after_release": True if system == "Linux" else None,
            "crc32_expected": 17, "crc32_restored": 17,
            "payload_bytes": q.LOGICAL_BYTES if raw else 0 if zeros else 200,
            "allocator_bytes": q.LOGICAL_BYTES if raw else 0 if zeros else 256,
            "metadata_mapping_bytes": 4096, "store_ns": 1, "restore_ns": 2,
            "compression_calls": pages if codec == "lz4" and not zeros else 0,
            "decompression_calls": pages if not raw and not zeros else 0,
            "codec_state_bytes": 1024 if codec == "lz4" else 0,
            "raw_pages": pages if raw else 0, "zero_pages": pages if zeros else 0}


class QualificationTests(unittest.TestCase):
    def test_default_is_emulation_and_repetitions_are_bounded(self):
        options = q.parser().parse_args(["--output", "/tmp/new-qualification"])
        self.assertFalse(options.reserved_dax)
        self.assertFalse(options.cross_host_reservation_confirmed)
        self.assertEqual(options.repetitions, 1)
        self.assertEqual(options.codec, "none")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            q.parser().parse_args(["--output", "/tmp/new-qualification", "--repetitions", "2"])

    def test_fixed_arena_and_guards_are_inside_upper_half(self):
        self.assertEqual(q.DAX_OFFSET, 274880004096)
        q.validate_arena(q.DAX_OFFSET, q.CAPACITY, q.UPPER_START, q.UPPER_END)
        self.assertEqual(q.DAX_OFFSET - q.GUARD_BYTES, q.UPPER_START)
        for offset, capacity in ((q.UPPER_START, q.CAPACITY), (q.DAX_OFFSET + 1, q.CAPACITY),
                                 (q.UPPER_END - q.CAPACITY, q.CAPACITY), (q.DAX_OFFSET, 4096)):
            with self.subTest(offset=offset, capacity=capacity), self.assertRaises(q.QualificationError):
                q.validate_arena(offset, capacity, q.UPPER_START, q.UPPER_END)

    def test_hardware_restriction(self):
        q.require_hardware_host("Linux", "nsl-node17", 0, True)
        q.require_hardware_host("Linux", "nsl17.example", 0, True)
        for system, host, uid, confirmed in (("Darwin", "nsl17", 0, True),
                ("Linux", "nsl18", 0, True), ("Linux", "nsl17", 1000, True),
                ("Linux", "nsl17", 0, False)):
            with self.assertRaises(q.QualificationError):
                q.require_hardware_host(system, host, uid, confirmed)

    def test_unknown_active_work_fails_closed(self):
        for row in ("crate-sv-formal.service loaded active running Campaign",
                    "crate-sv-prepare32.service loaded activating start Preparation",
                    "crate-sv-future-experiment.timer loaded active waiting Unknown",
                    "crate-sv-00.service loaded activating start Daemon"):
            with self.subTest(row=row), self.assertRaises(q.QualificationError):
                q.quiet_preflight(lambda *_args, **_kwargs: completed(row), "Linux")
        for row in ("unexpected formatted result", "crate-sv-00.service not-found active running"):
            with self.assertRaises(q.QualificationError):
                q.parse_units(row)
        self.assertEqual(q.parse_units(""), [])

    def test_only_recognized_empty_task_daemon_is_exempt(self):
        calls = []
        def run(argv, **_kwargs):
            calls.append([str(x) for x in argv])
            if argv == q.SYSTEMCTL_QUERY:
                return completed("crate-sv-03.service loaded active running Daemon")
            if argv[:2] == ["systemctl", "show"]:
                return completed("{ path=/bin/bash ; argv[]=/bin/bash /sandboxfs/crate-swebench-20260919/scripts/launch-daemon.sh 03 ; }")
            return completed('{"sandboxes": []}')
        result = q.quiet_preflight(run, "Linux")
        self.assertTrue(result["project_units"][0]["empty_sandbox_list"])
        self.assertEqual(calls[-1], [str(q.TASK_CTL), "--socket", "/run/crate-sv-03.sock", "list"])
        def busy(argv, **kwargs):
            if str(argv[0]) == str(q.TASK_CTL):
                return completed('{"sandboxes": [{"id": "ongoing"}]}')
            return run(argv, **kwargs)
        with self.assertRaises(q.QualificationError):
            q.quiet_preflight(busy, "Linux")
        def unknown(argv, **kwargs):
            if argv[:2] == ["systemctl", "show"]:
                return completed("/some/other/daemon")
            return run(argv, **kwargs)
        with self.assertRaises(q.QualificationError):
            q.quiet_preflight(unknown, "Linux")
        for foreign in ("{ path=/bin/bash ; argv[]=/bin/bash /sandboxfs/crate-swebench-20260919/scripts/launch-daemon.sh 03 --extra ; }",
                        "{ path=/bin/bash ; argv[]=/bin/bash /sandboxfs/crate-swebench-20260919/scripts/launch-daemon.sh 030 ; }"):
            def changed(argv, **kwargs):
                return completed(foreign) if argv[:2] == ["systemctl", "show"] else run(argv, **kwargs)
            with self.assertRaises(q.QualificationError):
                q.quiet_preflight(changed, "Linux")

    def test_swap_comparison_ignores_usage_only(self):
        first = "Filename\tType\tSize\tUsed\tPriority\n/swap.img file 8388604 0 -2\n"
        second = first.replace("8388604 0", "8388604 12345")
        self.assertEqual(q.parse_proc_swaps(first), q.parse_proc_swaps(second))
        before = {"available": True, "proc_raw": first, "proc_configuration_kib": q.parse_proc_swaps(first),
                  "show_configuration_bytes": q.parse_swap_show("/swap.img file 8589930496 -2\n")}
        after = copy.deepcopy(before)
        after["proc_raw"] = second
        self.assertTrue(q.swap_unchanged(before, after))
        after["show_configuration_bytes"] = q.parse_swap_show("/swap.img file 8589930496 5\n")
        self.assertFalse(q.swap_unchanged(before, after))
        with self.assertRaises(q.QualificationError):
            q.parse_proc_swaps("bad header")
        with self.assertRaises(q.QualificationError):
            q.parse_swap_show("missing columns")
        self.assertIn("--show=NAME,TYPE,SIZE,PRIO", q.SWAP_QUERY)
        self.assertNotIn("USED", " ".join(q.SWAP_QUERY))

    def test_only_exact_isolated_idle_docker_is_exempt(self):
        calls = []
        execstart = "{ path=/usr/bin/dockerd ; argv[]=" + " ".join(q.DOCKER_EXECSTART) + " ; ignore_errors=no ; pid=123 ; }"
        def run(argv, **_kwargs):
            calls.append(argv)
            if argv == q.SYSTEMCTL_QUERY:
                return completed("crate-sv-docker.service loaded active running IsolatedDocker")
            if argv[:2] == ["systemctl", "show"]:
                return completed(execstart)
            self.assertEqual(argv, q.DOCKER_PS_QUERY)
            return completed()
        result = q.quiet_preflight(run, "Linux")
        self.assertTrue(result["project_units"][0]["empty_running_container_list"])
        self.assertEqual(calls[-1], ["docker", "--host", "unix:///run/crate-sv-docker.sock", "ps", "-q"])
        for response in (completed("0123abcd\n"), completed(stderr="permission denied")):
            def busy(argv, **kwargs):
                return response if argv == q.DOCKER_PS_QUERY else run(argv, **kwargs)
            with self.assertRaises(q.QualificationError):
                q.quiet_preflight(busy, "Linux")
        for foreign in (execstart.replace("/run/crate-sv-docker.sock", "/run/docker.sock"),
                        execstart.replace("--bridge none", "--bridge docker0"),
                        execstart.replace("path=/usr/bin/dockerd", "path=/bin/bash"),
                        execstart.replace(" ; ignore_errors", " --debug ; ignore_errors"),
                        execstart + " " + execstart):
            with self.subTest(foreign=foreign):
                before = len(calls)
                def changed(argv, **kwargs):
                    if argv[:2] == ["systemctl", "show"]:
                        return completed(foreign)
                    return run(argv, **kwargs)
                with self.assertRaises(q.QualificationError):
                    q.quiet_preflight(changed, "Linux")
                self.assertNotIn(q.DOCKER_PS_QUERY, calls[before:])

    def test_fuser_requires_unambiguous_no_holders(self):
        q.no_dax_holders(lambda *_args, **_kwargs: completed(code=1))
        for result in (completed("12345", 0), completed(code=1, stderr="permission denied"), completed(code=0)):
            with self.assertRaises(q.QualificationError):
                q.no_dax_holders(lambda *_args, **_kwargs: result)

    def test_probe_validation_requires_crc_release_and_correct_scope(self):
        arguments = dict(hardware=False, offset=q.GUARD_BYTES, pattern="mixed", system="Linux")
        q.validate_probe(probe_result(), **arguments)
        for key, value in (("success", False), ("success", 1), ("backend", "device-dax"),
                           ("crc32_restored", 18), ("crc32_expected", -1),
                           ("source_mapping_absent_after_release", None),
                           ("source_release_munmap_succeeded", False), ("offset", q.DAX_OFFSET),
                           ("payload_bytes", 257), ("allocator_bytes", q.LOGICAL_BYTES + 1),
                           ("metadata_mapping_bytes", 0)):
            with self.subTest(key=key):
                result = probe_result(); result[key] = value
                with self.assertRaises(q.QualificationError):
                    q.validate_probe(result, **arguments)
        q.validate_probe(probe_result(system="Darwin"), **dict(arguments, system="Darwin"))

    def test_codec_contract_and_counters_are_enforced(self):
        arguments = dict(hardware=False, offset=q.GUARD_BYTES, system="Linux")
        for codec in ("none", "lz4"):
            for pattern in q.PATTERNS:
                result = probe_result(pattern=pattern, codec=codec)
                q.validate_probe(result, codec=codec, pattern=pattern, **arguments)
                for field in ("codec", "compression_calls", "decompression_calls", "codec_state_bytes", "raw_pages", "zero_pages"):
                    bad = copy.deepcopy(result)
                    bad[field] = "wrong" if field == "codec" else -1
                    with self.assertRaises(q.QualificationError):
                        q.validate_probe(bad, codec=codec, pattern=pattern, **arguments)
        for field in ("compression_calls", "decompression_calls", "codec_state_bytes", "zero_pages"):
            bad = probe_result(); bad[field] = 1
            with self.assertRaises(q.QualificationError):
                q.validate_probe(bad, pattern="mixed", **arguments)

    def test_guard_mappings_are_read_only_and_exact(self):
        class FakeMapping:
            def __enter__(self): return b"unchanged guard"
            def __exit__(self, *_args): return False
        with patch.object(q.os, "open", return_value=17) as opening, \
                patch.object(q.os, "close") as closing, \
                patch.object(q.mmap, "mmap", return_value=FakeMapping()) as mapping:
            result = q.guard_hashes(q.DAX_PATH, q.DAX_OFFSET, q.CAPACITY)
        self.assertEqual(opening.call_args.args[1], q.os.O_RDONLY | q.os.O_CLOEXEC | q.os.O_NOFOLLOW)
        self.assertEqual(mapping.call_count, 2)
        for call in mapping.call_args_list:
            self.assertEqual(call.kwargs["prot"], q.mmap.PROT_READ)
            self.assertEqual(call.args, (17, q.GUARD_BYTES))
        self.assertEqual(mapping.call_args_list[0].kwargs["offset"], q.UPPER_START)
        self.assertEqual(mapping.call_args_list[1].kwargs["offset"], q.DAX_OFFSET + q.CAPACITY)
        self.assertEqual(result["before"]["sha256"], result["after"]["sha256"])
        closing.assert_called_once_with(17)

    def test_source_snapshot_includes_nested_vendor_and_license(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source", Path(directory) / "output"
            source.mkdir(); output.mkdir()
            for name in q.SOURCE_FILES:
                (source / name).write_text(name)
            vendor = source / "third_party/lz4"; vendor.mkdir(parents=True)
            (vendor / "lz4.c").write_text("vendor source")
            (vendor / "LICENSE").write_text("license")
            native = q.snapshot_source(source, output)
            manifest = json.loads((output / "SOURCE_SHA256.json").read_text())
            self.assertIn("third_party/lz4/LICENSE", manifest)
            self.assertEqual((native / "third_party/lz4/lz4.c").read_text(), "vendor source")
            self.assertFalse((native / "build").exists())

    def test_fresh_output_and_failure_artifacts_without_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory)
            options = q.parser().parse_args(["--output", str(existing)])
            with self.assertRaises(FileExistsError):
                q.qualify(options)
            output = existing / "failed-run"
            options = q.parser().parse_args(["--output", str(output), "--reserved-dax"])
            with patch.object(q.platform, "system", return_value="Darwin"), \
                    patch.object(q.subprocess, "run") as execution:
                report = q.qualify(options)
            execution.assert_not_called()
            self.assertFalse(report["success"])
            self.assertTrue((output / "PLAN.json").exists())
            self.assertTrue((output / "REPORT.json").exists())
            self.assertIn("Linux nsl17", report["errors"][0])

    def test_failed_probe_still_checks_guards_and_swap_and_preserves_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            args = q.parser().parse_args(["--output", str(output)])
            commands = []
            def run(_recorder, argv, **_kwargs):
                commands.append([str(x) for x in argv])
                if argv in (["make", "check"], ["cc", "--version"]):
                    return completed()
                result = probe_result(pattern="zeros", system="Darwin")
                result["crc32_restored"] = 99
                return completed(json.dumps(result))
            with patch.object(q.platform, "system", return_value="Darwin"), \
                    patch.object(q.Recorder, "run", new=run), \
                    patch.object(q, "snapshot_source", return_value=output / "native"), \
                    patch.object(q, "quiet_preflight", return_value={"available": False}), \
                    patch.object(q, "swap_snapshot", return_value={"available": False}) as swaps, \
                    patch.object(q, "guard_hashes", return_value={"before": "same", "after": "same"}) as guards:
                report = q.qualify(args)
            self.assertFalse(report["success"])
            self.assertTrue(report["guards_unchanged"])
            self.assertTrue(report["swap_unchanged"])
            self.assertEqual(swaps.call_count, 2)
            self.assertEqual(guards.call_count, 2)
            self.assertEqual(len(commands), 3)
            self.assertEqual(commands[0], ["cc", "--version"])
            self.assertEqual(commands[2][1], "--emulate-file")
            self.assertTrue((output / "probe-r0-zeros.json").exists())
            self.assertTrue((output / "REPORT.json").exists())

    def test_successful_emulation_is_labeled_and_never_runs_hardware_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            args = q.parser().parse_args(["--output", str(output), "--repetitions", "3"])
            commands = []
            def run(_recorder, argv, **_kwargs):
                commands.append([str(x) for x in argv])
                return completed() if argv in (["make", "check"], ["cc", "--version"]) else completed(
                    json.dumps(probe_result(pattern=argv[-3], codec=argv[-1], system="Darwin")))
            with patch.object(q.platform, "system", return_value="Darwin"), \
                    patch.object(q.Recorder, "run", new=run), \
                    patch.object(q, "snapshot_source", return_value=output / "native"), \
                    patch.object(q, "quiet_preflight", return_value={"available": False}), \
                    patch.object(q, "swap_snapshot", return_value={"available": False}), \
                    patch.object(q, "guard_hashes", return_value={"before": "same", "after": "same"}), \
                    patch.object(q, "dax_identity") as identity, \
                    patch.object(q, "no_dax_holders") as holders:
                report = q.qualify(args)
            self.assertTrue(report["success"])
            self.assertEqual(report["backend"], "regular-file-emulation")
            self.assertEqual(len(report["probes"]), 9)
            self.assertEqual(len(commands), 11)
            self.assertEqual(commands[0], ["cc", "--version"])
            for command in commands[2:]:
                self.assertEqual(command[1], "--emulate-file")
                self.assertTrue(command[2].startswith(str(output)))
            identity.assert_not_called()
            holders.assert_not_called()


if __name__ == "__main__":
    unittest.main()
