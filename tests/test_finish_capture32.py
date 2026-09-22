import json
import hashlib
import shlex
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from experiments.swebench_verified import finish_capture32 as finish


def capture(task, revision="pinned"):
    return {"schema": "crate-verified-real-capture-v1", "task": task,
            **finish.PROTOCOL, "dataset_revision": revision,
            "commands": [{"sequence": 0, "command": "pytest", "argv": ["bash", "-c", "pytest"],
                          "response": {"exit_code": 1}}],
            "agent": {"exit_status": "step_limit", "stage_events": [
                {"stage": "LLM_WAIT", "start": 0., "end": 1.},
                {"stage": "TOOL_BURST", "start": 1., "end": 2.}]},
            "fingerprint": {"exit_code": 0, "stdout": '{"diff_sha256":"fixed","untracked":{}}'}}


def put_capture(package, task):
    folder = package / "captures-v2" / task["instance_id"]
    folder.mkdir(parents=True)
    (folder / "capture.json").write_text(json.dumps(capture(task)))
    (package / "captures-v2/logs" / (task["instance_id"] + ".log")).write_text("capture retained\n")


@pytest.fixture
def package(tmp_path):
    (tmp_path / "selection").mkdir()
    (tmp_path / "captures-v2/logs").mkdir(parents=True)
    tasks = [{"sequence": i, "instance_id": f"task-{i:02d}", "family": "tools",
              "base": f"sv-{i:02d}", "repo": "org/repo"} for i in range(32)]
    selection = {"dataset": "SWE-bench_Verified", "revision": "pinned", "tasks": tasks}
    (tmp_path / "selection/manifest.json").write_text(json.dumps(selection))
    for task in tasks:
        issue = tmp_path / "selection/tasks" / task["instance_id"]
        issue.mkdir(parents=True)
        (issue / "task.txt").write_text("Fix the pinned issue; no hidden evaluation data.")
    for task in tasks[:24]:
        put_capture(tmp_path, task)
    return tmp_path


def snapshot(active="inactive", completed=True):
    return {"unit": {"LoadState": "loaded", "ActiveState": active, "SubState": "dead",
                     "Result": "success", "ExecMainStatus": "0"},
            "completed": {"finished_unix": 123, "kind": "development"} if completed else None,
            "identity": {"plan_sha256": "a" * 64, "source_manifest_sha256": "b" * 64,
                         "source_root": finish.REMOTE + "/source-development-q1", "source_verified": True},
            "paths": {name: {"exists": False, "symlink": False}
                      for name in ("normalized-formal", "captures-v2")}}


def args(package, marker=None):
    return Namespace(package=package, after_campaign="development-q1",
                     after_unit="crate-sv-development-q1.service", wait_marker=marker)


def read_status(package):
    return json.loads((package / (finish.STEM + "-status.json")).read_text())


def test_remote_gate_requires_marker_and_successful_inactive_unit():
    assert not finish.remote_gate(snapshot("active", completed=True))
    assert finish.remote_gate(snapshot())
    with pytest.raises(RuntimeError, match="marker"):
        finish.remote_gate(snapshot(completed=False))
    for key, bad in (("ActiveState", "failed"), ("Result", "exit-code"),
                     ("ExecMainStatus", "1"), ("LoadState", "error")):
        state = snapshot()
        state["unit"][key] = bad
        with pytest.raises(RuntimeError):
            finish.remote_gate(state)
    state = snapshot()
    state["completed"]["success"] = False
    with pytest.raises(RuntimeError, match="failure"):
        finish.remote_gate(state)


def test_garbage_collected_unit_requires_verified_plan_source_and_marker():
    state = snapshot()
    state["unit"]["LoadState"] = "not-found"
    assert finish.remote_gate(state)
    state["identity"] = None
    with pytest.raises(RuntimeError, match="identity"):
        finish.remote_gate(state)
    state["completed"] = None
    with pytest.raises(RuntimeError, match="marker"):
        finish.remote_gate(state)


def test_local_qualification_gate_is_explicit_success_only(tmp_path):
    marker = tmp_path / "qualification.json"
    assert finish.local_gate(None)
    assert not finish.local_gate(marker)
    for bad in ({"success": False}, {}, {"success": "true"}, {"success": 1}):
        marker.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            finish.local_gate(marker)
    marker.write_text('{"success":true}')
    assert finish.local_gate(marker)


def test_existing_captures_accept_step_limit_and_expected_nonzero_exit(package):
    selection = finish.read_selection(package)
    finish.validate_captures(package, selection, 24)
    result = finish.immutable_snapshot(package, selection)
    assert len([p for p in result if p.endswith("/capture.json")]) == 24
    finish.require_fresh_outputs(package, selection)


@pytest.mark.parametrize("bad", [{"error": "failure"}, {"cleanup_error": "still active"},
    {"model": "changed"}, {"capture_protocol_version": 1}, {"dataset_revision": "changed"}])
def test_invalid_captures_fail_closed(package, bad):
    selection = finish.read_selection(package)
    task = selection["tasks"][0]
    path = package / "captures-v2" / task["instance_id"] / "capture.json"
    value = json.loads(path.read_text())
    value.update(bad)
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        finish.validate_captures(package, selection, 24)


@pytest.mark.parametrize("relative", ["captures-v2/task-24", "captures-v2/logs/task-24.log",
    "captures-v2/batch-24-32.json", "normalized-formal", "finish-capture32-batch.log"])
def test_no_overwriting_remaining_capture_or_normalized_outputs(package, relative):
    path = package / relative
    path.write_text("previous evidence")
    with pytest.raises(FileExistsError):
        finish.require_fresh_outputs(package, finish.read_selection(package))
    assert path.read_text() == "previous evidence"


def test_broken_symlink_is_not_a_fresh_output(package):
    (package / "normalized-formal").symlink_to(package / "missing")
    with pytest.raises(FileExistsError):
        finish.require_fresh_outputs(package, finish.read_selection(package))


@pytest.mark.parametrize("relative", ["captures-v2/task-00/capture.json",
    "captures-v2/logs/task-00.log", "selection/tasks/task-31/task.txt"])
def test_first24_and_selection_remain_immutable(package, monkeypatch, relative):
    selection = finish.read_selection(package)
    initial = finish.immutable_snapshot(package, selection)
    monkeypatch.setattr(finish, "source_hashes", lambda: {"capture.py": "fixed"})
    path = package / relative
    path.write_text(path.read_text() + "\nchanged")
    with pytest.raises(ValueError, match="immutable"):
        finish.require_unchanged(package, selection, initial, {"capture.py": "fixed"})


def test_extra_file_in_old_capture_and_source_drift_are_detected(package, monkeypatch):
    selection = finish.read_selection(package)
    initial = finish.immutable_snapshot(package, selection)
    monkeypatch.setattr(finish, "source_hashes", lambda: {"capture.py": "changed"})
    with pytest.raises(ValueError, match="source changed"):
        finish.require_unchanged(package, selection, initial, {"capture.py": "fixed"})
    (package / "captures-v2/task-00/new.json").write_text("{}")
    with pytest.raises(ValueError, match="immutable"):
        finish.require_unchanged(package, selection, initial, {"capture.py": "changed"})


def test_remote_normalized_and_capture_symlinks_are_refused():
    state = snapshot()
    state["paths"]["normalized-formal"]["exists"] = True
    with pytest.raises(FileExistsError):
        finish.require_remote_fresh(state)
    state = snapshot()
    state["paths"]["captures-v2"]["symlink"] = True
    with pytest.raises(ValueError):
        finish.require_remote_fresh(state)


def mocked_run(package, monkeypatch, *, fail_capture=False):
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        if any(str(item).endswith("capture_batch.py") for item in command):
            assert command[command.index("--start") + 1] == "24"
            assert command[command.index("--limit") + 1] == "32"
            selection = finish.read_selection(package)
            tasks = selection["tasks"][24:]
            for task in tasks[:1] if fail_capture else tasks:
                put_capture(package, task)
            if fail_capture:
                raise subprocess.CalledProcessError(1, command)
            (package / "captures-v2/batch-24-32.json").write_text(json.dumps([
                {"instance_id": task["instance_id"], "returncode": 0} for task in tasks]))
        elif any(str(item).endswith("convert.py") for item in command):
            from experiments.swebench_verified import convert
            with monkeypatch.context() as context:
                context.setattr("sys.argv", command[1:])
                convert.main()
        else:
            assert command[:2] == ["rsync", "-a"]
            assert "--ignore-existing" in command
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(finish.subprocess, "run", fake_run)
    monkeypatch.setattr(finish, "source_hashes", lambda: {"capture.py": "fixed"})
    monkeypatch.setattr(finish, "ssh", lambda command, input=None: subprocess.CompletedProcess(
        [], 0, json.dumps({"success": True, "verified_files": len(json.loads(input))}), ""))
    return calls


def test_controller_waits_both_gates_then_captures_converts_syncs_only(package, monkeypatch):
    marker = package / "qualified.json"
    states = iter([snapshot("active"), snapshot(), snapshot()])
    monkeypatch.setattr(finish, "remote_snapshot", lambda *_: next(states))
    waits = []
    def sleep(seconds):
        waits.append(seconds)
        marker.write_text('{"success":true}')
    monkeypatch.setattr(finish.time, "sleep", sleep)
    calls = mocked_run(package, monkeypatch)
    before = finish.immutable_snapshot(package, finish.read_selection(package))
    finish.run_controller(args(package, marker), ["finish_capture32.py", "--package", str(package)])
    assert waits == [30]
    assert len(calls) == 4
    assert [Path(calls[i][1]).name for i in (0, 1)] == ["capture_batch.py", "convert.py"]
    assert {call[-1] for call in calls[2:]} == {
        "nsl17:" + finish.REMOTE + "/captures-v2/", "nsl17:" + finish.REMOTE + "/normalized-formal/"}
    status = read_status(package)
    assert status["success"] and status["capture_count"] == 32 and status["formal_started"] is False
    assert status["stage"] == "captures_32_ready_for_review"
    assert status["immutable_sha256"] == before
    assert finish.immutable_snapshot(package, finish.read_selection(package)) == before
    assert not any("systemd-run" in str(c) or "swapon" in str(c) or "run_suite.py" in str(c) for c in calls)
    with pytest.raises(FileExistsError):
        finish.run_controller(args(package), ["second-launch"])
    assert len(calls) == 4


def test_capture_failure_preserves_partial_evidence_and_never_syncs(package, monkeypatch):
    monkeypatch.setattr(finish, "remote_snapshot", lambda *_: snapshot())
    calls = mocked_run(package, monkeypatch, fail_capture=True)
    with pytest.raises(subprocess.CalledProcessError):
        finish.run_controller(args(package), ["attempt"])
    assert len(calls) == 1
    assert (package / "captures-v2/task-24/capture.json").exists()
    assert not (package / "normalized-formal").exists()
    assert read_status(package)["stage"] == "failed_review_required"
    assert read_status(package)["success"] is False


def test_failed_remote_unit_never_launches_capture(package, monkeypatch):
    monkeypatch.setattr(finish, "remote_snapshot", lambda *_: snapshot("failed"))
    calls = mocked_run(package, monkeypatch)
    with pytest.raises(RuntimeError):
        finish.run_controller(args(package), ["attempt"])
    assert not calls
    assert read_status(package)["stage"] == "failed_review_required"


def test_bad_local_receipt_never_launches_capture(package, monkeypatch):
    marker = package / "bad-receipt.json"
    marker.write_text('{"success":false}')
    monkeypatch.setattr(finish, "remote_snapshot", lambda *_: snapshot())
    calls = mocked_run(package, monkeypatch)
    with pytest.raises(ValueError):
        finish.run_controller(args(package, marker), ["attempt"])
    assert not calls


def test_source_drift_while_waiting_never_launches_capture(package, monkeypatch):
    states = iter([snapshot("active"), snapshot()])
    monkeypatch.setattr(finish, "remote_snapshot", lambda *_: next(states))
    calls = mocked_run(package, monkeypatch)
    monkeypatch.setattr(finish.time, "sleep", lambda _: monkeypatch.setattr(
        finish, "source_hashes", lambda: {"capture.py": "changed"}))
    with pytest.raises(ValueError, match="source changed"):
        finish.run_controller(args(package), ["attempt"])
    assert not calls


def test_normalized_hash_tampering_is_rejected(package, monkeypatch):
    monkeypatch.setattr(finish, "remote_snapshot", lambda *_: snapshot())
    mocked_run(package, monkeypatch)
    finish.run_controller(args(package), ["attempt"])
    path = package / "normalized-formal/task-00.json"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        finish.validate_normalized(package, finish.read_selection(package))


def test_ssh_probe_is_quoted_and_bounded(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps(snapshot()), "")
    monkeypatch.setattr(finish.subprocess, "run", run)
    finish.remote_snapshot("development-q1", "crate-sv-development-q1.service")
    command, options = calls[0]
    remote = shlex.split(command[-1])
    assert remote[:2] == ["python3", "-c"]
    assert remote[-3:] == [finish.REMOTE, "development-q1", "crate-sv-development-q1.service"]
    assert options["timeout"] == 30 and options["check"] is True
    assert "BatchMode=yes" in command and "ConnectTimeout=10" in command


def test_probe_verifies_frozen_source_even_after_unit_garbage_collection(tmp_path, monkeypatch, capsys):
    campaign = tmp_path / "development-q1"
    campaign.mkdir()
    (campaign / "PLAN.json").write_text(json.dumps({"kind": "development", "orders": [["F0-S0"]]}))
    (campaign / "COMPLETED.json").write_text('{"finished_unix":123,"kind":"development"}')
    source = tmp_path / "source-development-q1"
    source.mkdir()
    code = source / "runner.py"
    code.write_text("pass\n")
    hashes = {"runner.py": hashlib.sha256(code.read_bytes()).hexdigest()}
    (source / "SOURCE_PROVENANCE.json").write_text(json.dumps({"source_sha256": hashes,
        "source_manifest_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}))
    def show(command, **kwargs):
        assert command[:2] == ["systemctl", "show"]
        return subprocess.CompletedProcess(command, 1,
            "LoadState=not-found\nActiveState=inactive\nResult=success\nExecMainStatus=0\nExecStart=\n", "")
    monkeypatch.setattr(finish.subprocess, "run", show)
    monkeypatch.setattr("sys.argv", ["probe", str(tmp_path), "development-q1", "crate-sv-development-q1.service"])
    exec(finish.REMOTE_PROBE, {})
    result = json.loads(capsys.readouterr().out)
    assert finish.remote_gate(result)
    assert result["identity"]["source_verified"] is True
    code.write_text("changed\n")
    with pytest.raises(RuntimeError, match="frozen source changed"):
        exec(finish.REMOTE_PROBE, {})


def test_campaign_unit_mismatch_and_unsafe_names_are_rejected_before_receipt(package):
    for campaign, unit in [("development-q2", "crate-sv-development-q1.service"),
                           ("bad; touch other", "crate-sv-development-q1.service"),
                           ("development-q1", "other.service")]:
        options = args(package)
        options.after_campaign, options.after_unit = campaign, unit
        with pytest.raises(ValueError):
            finish.run_controller(options, ["attempt"])
    assert not (package / (finish.STEM + "-controller.json")).exists()
