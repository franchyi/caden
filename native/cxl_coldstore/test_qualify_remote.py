#!/usr/bin/env python3
"""Mocked controller tests only: no SSH, rsync, processes or device access."""
import argparse
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("qualify_remote", Path(__file__).with_name("qualify_remote.py"))
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)
q = r.q


def state(active="inactive", load="loaded", result="success", code="0"):
    return {"LoadState": load, "ActiveState": active, "SubState": "dead" if active == "inactive" else "running",
            "Result": result, "ExecMainStatus": code}


def identity(host="nsl17"):
    return {"hostname": host, "size_bytes": q.UPPER_END, "sysfs_dev": "250:1", "device_dev": "250:1",
            "character_device": True, "resource": None}


def probe(pattern, codec="none"):
    pages = q.LOGICAL_BYTES // 4096
    raw = codec == "none" or pattern == "random"
    zeros = codec == "lz4" and pattern == "zeros"
    return {"schema": "crate-cold-tier-backend-probe-v2", "success": True, "backend": "device-dax", "codec": codec,
            "pattern": pattern, "offset": q.DAX_OFFSET, "mapped_capacity": q.CAPACITY,
            "source_bytes": q.LOGICAL_BYTES, "source_release_munmap_succeeded": True,
            "source_mapping_absent_after_release": True, "crc32_expected": 99, "crc32_restored": 99,
            "payload_bytes": q.LOGICAL_BYTES if raw else 0 if zeros else pages * 1200,
            "allocator_bytes": q.LOGICAL_BYTES if raw else 0 if zeros else pages * 2048,
            "metadata_mapping_bytes": 4096,
            "raw_pages": pages if raw else 0, "zero_pages": pages if zeros else 0,
            "codec_state_bytes": 1024 if codec == "lz4" else 0,
            "compression_calls": pages if codec == "lz4" and not zeros else 0,
            "decompression_calls": pages if not raw and not zeros else 0,
            "store_ns": 10, "restore_ns": 10}


def artifacts(output, manifest, codec="none"):
    q.write_json(output / "SOURCE_SHA256.json", manifest)
    report = {"schema": "crate-cold-tier-qualification-v2", "success": True, "backend": "device-dax", "codec": codec,
              "hostname": "nsl-node17", "offset": q.DAX_OFFSET, "mapped_capacity": q.CAPACITY,
              "logical_bytes": q.LOGICAL_BYTES, "repetitions": 3, "errors": [], "probes": [],
              "guards_unchanged": True, "swap_unchanged": True, "dax_unchanged": True,
              "no_kernel_attachment": True, "no_swap_configuration_actions": True,
              "reservation_confirmed_by_operator": True, "scope": "cooperative owned-buffer only"}
    guards = {"before": {"offset": q.UPPER_START, "length": q.GUARD_BYTES, "sha256": "b" * 64},
              "after": {"offset": q.DAX_OFFSET + q.CAPACITY, "length": q.GUARD_BYTES, "sha256": "c" * 64}}
    for phase in ("before", "after"):
        q.write_json(output / f"guards-{phase}.json", guards)
        q.write_json(output / f"dax-{phase}.json", {"path": "/dev/dax0.0", "major_minor": "250:1", "size_bytes": q.UPPER_END})
        q.write_json(output / f"holders-{phase}.json", {"local_holders": []})
        proc = "Filename Type Size Used Priority\n/swap.img file 1024 0 -2\n"
        shown = "/swap.img file 1048576 -2\n"
        q.write_json(output / f"swap-{phase}.json", {"available": True, "proc_raw": proc, "show_raw": shown,
                     "proc_configuration_kib": q.parse_proc_swaps(proc), "show_configuration_bytes": q.parse_swap_show(shown)})
    q.write_json(output / "quiet-final.json", {"available": True, "project_units": []})
    for repetition in range(3):
        for pattern in q.PATTERNS:
            value = probe(pattern, codec)
            suffix = f"r{repetition}-{pattern}"
            report["probes"].append({"repetition": repetition, "pattern": pattern, "result": value})
            q.write_json(output / f"probe-{suffix}.json", value)
            q.write_json(output / f"guards-after-{suffix}.json", guards)
    q.write_json(output / "REPORT.json", report)
    return report


class RemoteQualificationTests(unittest.TestCase):
    def test_safe_names(self):
        self.assertEqual(r.safe_name("reserved-dax-v1"), "reserved-dax-v1")
        for name in ("../other", "other/name", "x;touch file", "-option", "", "A", "a" * 49):
            with self.assertRaises(argparse.ArgumentTypeError):
                r.safe_name(name)

    def test_completion_needs_marker_and_successful_inactive_service(self):
        marker = {"kind": "development", "finished_unix": 12345}
        self.assertFalse(r.completion_ready(state("active"), marker))
        self.assertTrue(r.completion_ready(state(), marker))
        self.assertTrue(r.completion_ready(state(load="not-found"), marker))
        for service, completed in ((state(), None), (state(result="exit-code", code="1"), marker),
                (state("failed"), marker), (state(), {"kind": "formal", "finished_unix": 12345}),
                (state(), {"kind": "development", "finished_unix": True})):
            with self.assertRaises(q.QualificationError):
                r.completion_ready(service, completed)

    def test_service_parser_rejects_missing_duplicate_and_invalid_properties(self):
        text = "\n".join(f"{key}={value}" for key, value in state().items())
        self.assertEqual(r.parse_service(text), state())
        self.assertEqual(r.parse_service(text.replace("LoadState=loaded", "LoadState=not-found")), state(load="not-found"))
        for bad in (text + "\nResult=success", text.replace("ExecMainStatus=0", "ExecMainStatus=bad"),
                    text.replace("LoadState=loaded\n", ""), text + "\nUnknown=value"):
            with self.assertRaises(q.QualificationError):
                r.parse_service(bad)

    def test_identity_is_host_specific_exact_size_and_character_device(self):
        r.validate_host_identity("nsl17", identity())
        r.validate_host_identity("nsl18", identity("nsl18"))
        for key, value in (("hostname", "nsl18"), ("size_bytes", q.UPPER_END - 1),
                           ("character_device", False), ("device_dev", "250:2")):
            changed = identity(); changed[key] = value
            with self.assertRaises(q.QualificationError):
                r.validate_host_identity("nsl17", changed)

    def test_privileged_fuser_blocks_holders_and_failed_visibility(self):
        remote = object.__new__(r.Remote)
        for result in (SimpleNamespace(returncode=0, stdout="12345", stderr=""),
                       SimpleNamespace(returncode=1, stdout="", stderr="sudo denied")):
            remote.ssh = lambda *_args, **_kwargs: result
            with self.assertRaises(q.QualificationError):
                remote.host_preflight("nsl17")
        calls = []
        def ssh(host, argv, **_kwargs):
            calls.append((host, argv))
            if argv[:3] == ["sudo", "-n", "fuser"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout=json.dumps(identity(host)), stderr="")
        remote.ssh = ssh
        checked = remote.host_preflight("nsl18")
        self.assertTrue(checked["no_local_holders"])
        self.assertEqual(calls[0], ("nsl18", ["sudo", "-n", "fuser", "/dev/dax0.0"]))
        self.assertEqual(calls[1][0], "nsl18")
        self.assertEqual(calls[1][1][:4], ["sudo", "-n", "python3", "-c"])
        self.assertIn("r.read_text()", calls[1][1][4])

    def test_source_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for name in q.SOURCE_FILES:
                (source / name).write_text(name)
            vendor = source / "third_party/lz4"; vendor.mkdir(parents=True)
            (vendor / "LICENSE").write_text("license")
            expected = r.source_hashes(source)
            self.assertIn("third_party/lz4/LICENSE", expected)
            r.verify_snapshot(source, expected)
            (source / "coldstore.c").write_text("changed")
            with self.assertRaises(q.QualificationError):
                r.verify_snapshot(source, expected)

    def test_download_requires_matching_raw_guards_swaps_crc_and_source(self):
        expected = {"coldstore.c": "a" * 64}
        with tempfile.TemporaryDirectory() as directory, patch.object(r, "verify_snapshot"):
            output = Path(directory)
            artifacts(output, expected)
            self.assertTrue(r.validate_download(output, expected, identity())["success"])
            mutations = [("REPORT.json", lambda x: x.update(success=False)),
                         ("SOURCE_SHA256.json", lambda x: x.update({"coldstore.c": "d" * 64})),
                         ("guards-after-r1-mixed.json", lambda x: x["before"].update(sha256="d" * 64)),
                         ("swap-after.json", lambda x: x.update(show_raw="/swap.img file 1048576 3\n")),
                         ("probe-r0-zeros.json", lambda x: x.update(crc32_restored=999)),
                         ("probe-r0-zeros.json", lambda x: x.update(compression_calls=1)),
                         ("REPORT.json", lambda x: x.update(codec="lz4")),
                         ("dax-after.json", lambda x: x.update(size_bytes=q.UPPER_END - 1))]
            for name, mutate in mutations:
                with self.subTest(name=name):
                    artifacts(output, expected)
                    value = r.load_object(output / name); mutate(value); q.write_json(output / name, value)
                    with self.assertRaises(q.QualificationError):
                        r.validate_download(output, expected, identity())

    def test_download_validates_selected_codec(self):
        expected = {"coldstore.c": "a" * 64}
        with tempfile.TemporaryDirectory() as directory, patch.object(r, "verify_snapshot"):
            output = Path(directory)
            artifacts(output, expected, "lz4")
            self.assertTrue(r.validate_download(output, expected, identity(), codec="lz4")["success"])
            with self.assertRaises(q.QualificationError):
                r.validate_download(output, expected, identity(), codec="none")

    def exercise_controller(self, package, fail_holder=False, remote_failure=False, codec="none"):
        calls = []
        expected = {"coldstore.c": "a" * 64}
        class FakeRemote:
            def __init__(self, output): self.output = output; self.polls = 0
            def gate(self):
                self.polls += 1
                return (state("active") if self.polls == 1 else state(), {"kind": "development", "finished_unix": 12345})
            def host_preflight(self, host):
                calls.append(("inspect", host))
                if fail_holder and host == "nsl18": raise q.QualificationError("existing nsl18 holder")
                return {"host": host, "checked_unix": 12345, "no_local_holders": True, "identity": identity(host)}
            def create_remote_source(self, name):
                calls.append(("create", name))
                return r.REMOTE + "/native-" + name, r.REMOTE + "/" + name
            def verify_deployed(self, *_args): return {"verified": True, "files": 1}
            def run(self, argv, **_kwargs):
                calls.append(("run", argv))
                if "--rsync-path=sudo -n rsync" in argv:
                    result = artifacts(Path(argv[-1]), expected, codec)
                    if remote_failure:
                        result["success"] = False
                        q.write_json(Path(argv[-1]) / "REPORT.json", result)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            def ssh(self, host, argv, **kwargs):
                calls.append(("qualify", host, argv, kwargs))
                return SimpleNamespace(returncode=1 if remote_failure else 0, stdout="", stderr="")
        def snapshot(_source, output):
            native = output / "native"; native.mkdir()
            q.write_json(output / "SOURCE_SHA256.json", expected)
            return native
        args = r.parser().parse_args(["--package", str(package), "--name", "reserved-dax-v1", "--codec", codec])
        with patch.object(r, "Remote", FakeRemote), patch.object(r, "source_hashes", return_value=expected), \
                patch.object(r, "verify_snapshot"), patch.object(q, "snapshot_source", side_effect=snapshot), \
                patch.object(r.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            code = r.controller(args)
        sleep.assert_called_once_with(30)
        return code, package / "coldstore-backend/reserved-dax-v1", calls

    def test_controller_success_is_gated_and_uses_exact_bounded_command(self):
        with tempfile.TemporaryDirectory() as directory:
            code, output, calls = self.exercise_controller(Path(directory))
            self.assertEqual(code, 0)
            self.assertTrue((output / "SUCCESS.json").exists())
            create = next(i for i, call in enumerate(calls) if call[0] == "create")
            self.assertEqual(calls[:create], [("inspect", "nsl17"), ("inspect", "nsl18")])
            launch = next(call for call in calls if call[0] == "qualify")
            self.assertEqual(launch[1], "nsl17")
            self.assertEqual(launch[2][:6], ["sudo", "-n", "taskset", "-c", "0-7", "python3"])
            self.assertIn("--reserved-dax", launch[2])
            self.assertEqual(launch[2][-4:], ["--repetitions", "3", "--codec", "none"])
            self.assertEqual(r.load_object(output / "SUCCESS.json")["codec"], "none")
            self.assertEqual(launch[3]["timeout"], 600)
            self.assertEqual(sum(call == ("inspect", "nsl18") for call in calls), 2)
            self.assertNotIn("systemd-run", launch[2])
            self.assertNotIn("swapon", launch[2])

    def test_controller_propagates_explicit_lz4(self):
        self.assertEqual(r.parser().parse_args(["--package", "/tmp/package"]).codec, "none")
        with tempfile.TemporaryDirectory() as directory:
            code, output, calls = self.exercise_controller(Path(directory), codec="lz4")
            self.assertEqual(code, 0)
            launch = next(call for call in calls if call[0] == "qualify")
            self.assertEqual(launch[2][-2:], ["--codec", "lz4"])
            self.assertEqual(r.load_object(output / "SUCCESS.json")["codec"], "lz4")

    def test_holder_failure_prevents_deployment_and_no_success_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            code, output, calls = self.exercise_controller(Path(directory), fail_holder=True)
            self.assertEqual(code, 1)
            self.assertFalse((output / "SUCCESS.json").exists())
            self.assertTrue((output / "STATUS.json").exists())
            self.assertFalse(any(call[0] in {"create", "run", "qualify"} for call in calls))

    def test_failed_qualification_preserves_download_without_success(self):
        with tempfile.TemporaryDirectory() as directory:
            code, output, _calls = self.exercise_controller(Path(directory), remote_failure=True)
            self.assertEqual(code, 1)
            self.assertTrue((output / "remote/REPORT.json").exists())
            self.assertFalse((output / "SUCCESS.json").exists())

    def test_existing_output_is_never_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "coldstore-backend/reserved-dax-v1").mkdir(parents=True)
            args = r.parser().parse_args(["--package", str(package)])
            with self.assertRaises(FileExistsError), patch.object(r, "Remote") as remote:
                r.controller(args)
            remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
