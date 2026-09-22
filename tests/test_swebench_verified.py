import json
import subprocess
from experiments.swebench_verified.convert import convert
from experiments.swebench_verified.routing import RoutedCtlRunner


def fixture():
    return {"schema": "crate-verified-real-capture-v1",
            "task": {"base": "sv-00", "family": "django", "instance_id": "d-1", "repo": "d/d"},
            "commands": [{"sequence": 0, "command": "pytest", "argv": ["bash", "-c", "pytest"],
                          "response": {"exit_code": 1}}],
            "agent": {"exit_status": "step_limit", "stage_events": [
                {"stage": "LLM_WAIT", "start": 0., "end": 1.},
                {"stage": "LLM_WAIT", "start": 1., "end": 3.},
                {"stage": "TOOL_BURST", "start": 3., "end": 4.}]},
            "fingerprint": {"stdout": '{"diff_sha256":"abc","untracked":{}}'}}


def test_original_commands_and_failed_tests_are_preserved():
    c = fixture()
    w = convert(c)
    assert w["events"][0]["duration_ms"] == 3000
    assert w["events"][1]["argv"] == c["commands"][0]["argv"]
    assert w["events"][1]["expected_exit_code"] == 1
    assert w["capture_exit_status"] == "step_limit"
    assert w["tool_execution"]["proxy"] is False


def test_route_is_selected_by_base_then_exact_sandbox_id():
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "{}", "")
    route = RoutedCtlRunner({"sv-00": "/a.sock", "sv-01": "/b.sock"}, fake)
    prefix = ["ctl", "--socket", "/placeholder", "--timeout", "5s"]
    route(prefix + ["create", "--id", "a", "--base", "sv-01", "--mode", "t1"])
    route(prefix + ["exec-json", "a", "--", "true"])
    route(prefix + ["destroy", "a"])
    assert all(c[2] == "/b.sock" for c in calls)
    assert not route.sandboxes


def test_route_rejects_unknown_sandbox():
    import pytest
    route = RoutedCtlRunner({"sv-00": "/a.sock"})
    with pytest.raises(KeyError):
        route(["ctl", "--socket", "/a.sock", "--timeout", "5s", "destroy", "foreign"])


def test_weighted_memory_uses_elapsed_time_and_excludes_cleanup():
    from experiments.swebench_verified.analyze import integral
    samples = [{"monotonic_ns": t, "phase": phase, "bytes": value} for t, phase, value in
               [(0, "idle", 999), (10, "create", 100), (30, "trace_replay", 300), (60, "cleanup", 999)]]
    assert integral(samples, "bytes")["duration_ns"] == 50
    assert integral(samples, "bytes")["mean_bytes"] == 220


def test_cold_timestamp_keeps_nanosecond_precision():
    from experiments.swebench_verified.run_cold import timestamp_ns
    assert timestamp_ns("1970-01-01T00:00:01.000000123Z") == 1000000123


def test_tool_distribution_is_event_weighted():
    from experiments.swebench_verified.analyze import distribution
    assert distribution([1, 1, 10])["mean"] == 4


def test_image_review_accepts_only_same_blob_regular_file_mode_changes():
    from experiments.swebench_verified.prepare_remote import classify_image_changes
    raw = (":100644 100755 abc abc M\0normal.py\0"
           ":100644 100755 abc def M\0content.py\0"
           ":100644 120000 abc abc T\0symlink\0")
    result = classify_image_changes(raw)
    assert result["mode_only"] == ["normal.py"]
    assert result["content_or_type"] == ["content.py", "symlink"]


def test_image_review_rejects_additions_and_malformed_records():
    import pytest
    from experiments.swebench_verified.prepare_remote import classify_image_changes
    assert classify_image_changes(":000000 100644 000 abc A\0new.py\0")["content_or_type"] == ["new.py"]
    with pytest.raises(ValueError):
        classify_image_changes(":100644 100755 abc abc M\0")


def test_checkpoint_does_not_accept_unequal_work_or_relax_tail_guard():
    import copy
    import pytest
    from experiments.swebench_verified.checkpoints import compare
    baseline = {"summary": {"success": True}, "config": {"wait_scale": 1},
                "workload": {"manifest_sha256": "fixed"}, "replays": [{"trajectory_id": "t", "tools": [
                    {"sequence": 0, "source_arguments_sha256": "arg", "expected_exit_code": 1,
                     "exit_code": 1, "turn_ns": 1000}]}]}
    treatment = copy.deepcopy(baseline)
    assert compare(baseline, treatment)["guards_pass"]
    treatment["replays"][0]["tools"][0]["turn_ns"] = 1101
    assert not compare(baseline, treatment)["guards_pass"]
    treatment["replays"][0]["tools"][0]["source_arguments_sha256"] = "different"
    with pytest.raises(ValueError, match="unequal work"):
        compare(baseline, treatment)


def test_progress_preserves_each_completed_event(tmp_path):
    from experiments.trajectory_replay.run_campaign import emit_progress
    path = tmp_path / "events.jsonl"
    for sequence in range(2):
        emit_progress(path, {"type": "tool_complete", "sequence": sequence})
    assert [json.loads(line)["sequence"] for line in path.read_text().splitlines()] == [0, 1]
