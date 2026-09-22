import builtins
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from experiments.swebench_verified import run_cold as cold


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_bytes(b"abcde")
    (repo / "z.py").write_bytes(b"12345")
    (repo / "larger.txt").write_bytes(b"t" * 30)
    (repo / "too-big.py").write_bytes(b"x" * ((1 << 20) + 1))
    (repo / "empty.py").write_bytes(b"")
    (repo / "link.py").symlink_to(repo / "larger.txt")
    subprocess.run(["/usr/bin/git", "init", "-q", str(repo)], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(repo), "add", "--", "."], check=True)
    (repo / "untracked.py").write_bytes(b"u" * 100)
    return repo


def execute_probe(repo, action, payload):
    program = cold.FIRST_TOUCH_PROGRAM.replace('ROOT = "/workspace/repository"', f"ROOT = {str(repo)!r}")
    previous = sys.argv
    output = io.StringIO()
    try:
        sys.argv = ["probe", action, json.dumps(payload)]
        with contextlib.redirect_stdout(output):
            exec(program, {})
    finally:
        sys.argv = previous
    return json.loads(output.getvalue())


def test_selection_is_metadata_only_and_deterministic(repository, monkeypatch):
    original_open, original_os_open = builtins.open, os.open
    def in_repo(file):
        return isinstance(file, (str, bytes, os.PathLike)) and Path(os.fsdecode(file)).is_relative_to(repository)
    def no_payload_open(file, *args, **kwargs):
        if in_repo(file):
            raise AssertionError("selection opened repository file payload")
        return original_open(file, *args, **kwargs)
    def no_payload_os_open(file, *args, **kwargs):
        if in_repo(file):
            raise AssertionError("selection opened repository file payload")
        return original_os_open(file, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", no_payload_open)
    monkeypatch.setattr(os, "open", no_payload_os_open)
    first = execute_probe(repository, "select", {})
    second = execute_probe(repository, "select", {})
    assert first == second
    assert first["file"]["path"] == "a.py"  # Python preferred, then bytewise tie-break.
    assert first["file"]["size_bytes"] == 5
    assert first["selection_payload_bytes_read"] == 0
    assert "source_sha256" not in first["file"]


def test_fallback_is_largest_tracked_regular_file_and_no_candidate_fails(tmp_path):
    repo = tmp_path / "fallback"
    repo.mkdir()
    (repo / "tiny.txt").write_bytes(b"t")
    (repo / "large.dat").write_bytes(b"abc")
    subprocess.run(["/usr/bin/git", "init", "-q", str(repo)], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(repo), "add", "."], check=True)
    result = execute_probe(repo, "select", {})
    assert result["file"]["path"] == "large.dat" and result["preference"] == "tracked-regular"
    (repo / "tiny.txt").write_bytes(b"")
    (repo / "large.dat").write_bytes(b"")
    with pytest.raises(RuntimeError, match="no eligible"):
        execute_probe(repo, "select", {})


def test_first_read_then_same_byte_write_preserves_whole_file(repository):
    selected = execute_probe(repository, "select", {})
    read = execute_probe(repository, "read", {"file": selected["file"]})
    assert read["source_sha256"] == hashlib.sha256(b"abcde").hexdigest()
    assert read["first_byte_hex"] == "61" and read["bytes_read"] == 5
    written = execute_probe(repository, "write", {"file": selected["file"],
        "source_sha256": read["source_sha256"], "first_byte_hex": read["first_byte_hex"]})
    assert written["contents_unchanged"] and written["bytes_written"] == 1
    assert written["after_sha256"] == read["source_sha256"]
    assert written["operation_ns"] > 0 and read["operation_ns"] > 0
    assert (repository / "a.py").read_bytes() == b"abcde"
    assert written["verification_payload_bytes_read"] == 5


def test_old_python_clock_fallback_preserves_read_write_validation(repository, monkeypatch):
    monkeypatch.delattr(time, "monotonic_ns")
    selected = execute_probe(repository, "select", {})
    read = execute_probe(repository, "read", {"file": selected["file"]})
    written = execute_probe(repository, "write", {"file": selected["file"],
        "source_sha256": read["source_sha256"], "first_byte_hex": read["first_byte_hex"]})
    for result in (selected, read, written):
        assert result["clock_source"] == "time.monotonic_float_seconds"
        assert result["clock_resolution_ns"] > 0 and result["python_version"]
    assert read["operation_ns"] > 0 and written["operation_ns"] > 0
    assert written["contents_unchanged"]


def test_changed_target_is_rejected_before_write(repository):
    selected = execute_probe(repository, "select", {})
    read = execute_probe(repository, "read", {"file": selected["file"]})
    (repository / "a.py").write_bytes(b"longer changed content")
    with pytest.raises(RuntimeError, match="metadata changed"):
        execute_probe(repository, "write", {"file": selected["file"],
            "source_sha256": read["source_sha256"], "first_byte_hex": read["first_byte_hex"]})
    assert (repository / "a.py").read_bytes() == b"longer changed content"


def test_same_byte_hash_verification_fails_closed(repository):
    selected = execute_probe(repository, "select", {})
    read = execute_probe(repository, "read", {"file": selected["file"]})
    with pytest.raises(RuntimeError, match="preserve complete file"):
        execute_probe(repository, "write", {"file": selected["file"],
            "source_sha256": "0" * 64, "first_byte_hex": read["first_byte_hex"]})
    assert (repository / "a.py").read_bytes() == b"abcde"


def test_helper_uses_same_sandbox_normal_api_and_separate_timings(repository):
    calls = []
    def call(argv):
        started = time.monotonic_ns()
        calls.append(argv)
        assert argv[:3] == ["exec-json", "sv-cold-test", "--"]
        assert argv[3:8] == ["/opt/miniconda3/envs/testbed/bin/python", "-I", "-S", "-B", "-c"]
        result = execute_probe(repository, argv[-2], json.loads(argv[-1]))
        return {"exit_code": 0, "duration_ns": time.monotonic_ns() - started, "stdout": json.dumps(result)}
    record = {}
    cold.first_touch_probe(call, "sv-cold-test", record)
    assert [argv[-2] for argv in calls] == ["select", "read", "write"]
    assert record["success"] and record["file"]["source_sha256"] == hashlib.sha256(b"abcde").hexdigest()
    for action in ("select", "read", "write"):
        assert 0 < record[action]["server_command_ns"] <= record[action]["client_rpc_ns"]
    for action in ("read", "write"):
        assert 0 < record[action]["result"]["operation_ns"] <= record[action]["server_command_ns"]
    assert "excluded" in record["endpoint"]


def test_api_failure_retains_response_and_does_not_proceed():
    calls, record = [], {}
    def call(argv):
        calls.append(argv)
        return {"exit_code": 1, "duration_ns": 19, "stdout": "", "stderr": "no file"}
    with pytest.raises(RuntimeError, match="select command failed"):
        cold.first_touch_probe(call, "one-sandbox", record)
    assert len(calls) == 1 and record["select"]["response"]["stderr"] == "no file"
    assert "success" not in record


def test_nonzero_cli_exit_retains_structured_sandbox_stderr(monkeypatch):
    response = {"exit_code": 1, "duration_ns": 19, "stdout": "", "stderr": "inner traceback"}
    monkeypatch.setattr(cold.subprocess, "run", lambda command, **kwargs:
        subprocess.CompletedProcess(command, 1, json.dumps(response), "sandboxfsctl: command failed"))
    record = {}
    with pytest.raises(RuntimeError, match="inner traceback"):
        cold.first_touch_probe(lambda argv: cold.api_call(["ctl"], argv), "test", record)
    assert record["select"]["response"] == response
    assert "success" not in record


@pytest.mark.parametrize("stdout", ["", "not-json", "{}", "[]"])
def test_transport_or_non_command_failure_is_not_accepted(monkeypatch, stdout):
    monkeypatch.setattr(cold.subprocess, "run", lambda command, **kwargs:
        subprocess.CompletedProcess(command, 1, stdout, "connection denied"))
    with pytest.raises(RuntimeError):
        cold.api_call(["ctl"], ["exec-json", "test", "--", "/bin/true"])
    with pytest.raises(RuntimeError):
        cold.api_call(["ctl"], ["create", "--id", "test"])


@pytest.mark.parametrize("duration", [0, -1, True, None, 10**30])
def test_server_timing_must_be_positive_and_within_rpc(duration):
    def call(argv):
        return {"exit_code": 0, "duration_ns": duration, "stdout": "{}"}
    with pytest.raises(ValueError, match="server/RPC timing"):
        cold.first_touch_probe(call, "test", {})


@pytest.mark.parametrize("path", ["../base/file.py", "/prepared/base.py", "sub//file.py", "bad\0name.py"])
def test_untrusted_selection_path_is_rejected_before_read(path):
    calls = []
    def call(argv):
        calls.append(argv)
        return {"exit_code": 0, "duration_ns": 1, "stdout": json.dumps({
            "file": {"path": path, "size_bytes": 1}, "selection_payload_bytes_read": 0})}
    with pytest.raises(ValueError, match="selection"):
        cold.first_touch_probe(call, "test", {})
    assert len(calls) == 1


def setup_main(tmp_path, monkeypatch, enabled, *, fail_touch=False, fail_destroy=False):
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"tasks": [{"sequence": 0, "instance_id": "task",
                                               "base": "sv-00", "socket": "/test.sock"}]}))
    output = tmp_path / "results"
    argv = ["cold", "--selection", str(selection), "--output", str(output), "--limit", "1", "--repeats", "1"]
    if enabled:
        argv.append("--first-touch")
    monkeypatch.setattr(sys, "argv", argv)
    clock, events = {"ns": 1000}, []
    monkeypatch.setattr(cold.time, "monotonic_ns", lambda: clock["ns"])
    monkeypatch.setattr(cold.time, "time_ns", lambda: 1_000_000_000 + clock["ns"])
    def run(command, **kwargs):
        action = command[5]
        if action == "create":
            events.append("create")
            received = f"1970-01-01T00:00:01.{clock['ns']:09d}Z"
            clock["ns"] += 20
            response = {"timings": {"request_received_at": received, "workspace_start_ns": 1,
                                     "workspace_ready_ns": 9, "total_ns": 20}}
        elif action == "exec-json":
            assert command[-1] == "/bin/true"
            events.append("true")
            clock["ns"] += 10
            response = {"exit_code": 0, "duration_ns": 5}
        else:
            assert action == "destroy"
            events.append("destroy")
            if fail_destroy:
                raise RuntimeError("cleanup failed")
            response = {"cleanup_ns": 1}
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")
    monkeypatch.setattr(cold.subprocess, "run", run)
    def touch(call, ident, record):
        events.append("touch")
        assert ident.startswith("sv-cold-00-0-")
        clock["ns"] += 100_000_000
        record["partial_evidence"] = True
        if fail_touch:
            raise RuntimeError("touch failure")
        record["success"] = True
    monkeypatch.setattr(cold, "first_touch_probe", touch)
    return output, events


@pytest.mark.parametrize("enabled", [False, True])
def test_original_cold_endpoint_is_unchanged_and_probes_default_off(tmp_path, monkeypatch, enabled):
    output, events = setup_main(tmp_path, monkeypatch, enabled)
    cold.main()
    rows = [json.loads(line) for line in (output / "samples.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert all(row["cold_start_ns"] == 30 and row["client_submission_to_ready_ns"] == 30 for row in rows)
    assert all(row["wall_monotonic_discrepancy_ns"] == 0 for row in rows)
    assert all(("first_touch" in row) is enabled for row in rows)
    assert events == (["create", "true", "touch", "destroy"] if enabled else ["create", "true", "destroy"]) * 2
    completed = json.loads((output / "completed.json").read_text())
    if not enabled:
        assert completed == {"samples": 2, "tasks": 1, "repeats": 1}


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_probe_failure_keeps_original_cold_sample_and_partial_evidence(tmp_path, monkeypatch, cleanup_failure):
    output, events = setup_main(tmp_path, monkeypatch, True, fail_touch=True, fail_destroy=cleanup_failure)
    with pytest.raises(RuntimeError, match="touch failure"):
        cold.main()
    rows = [json.loads(line) for line in (output / "samples.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["cold_start_ns"] == 30
    assert rows[0]["first_touch"]["partial_evidence"]
    assert ("cleanup_error" in rows[0]) is cleanup_failure
    assert events == ["create", "true", "touch", "destroy"]
    assert not (output / "completed.json").exists()
