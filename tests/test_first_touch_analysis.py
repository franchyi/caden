"""Post-cold microprobes are matched, fail-closed and separate from paper endpoints."""
import copy
import hashlib
import json
import stat

import pytest

from experiments.swebench_verified import analyze as analysis
from tests.test_analysis_fidelity import campaign, change_plan, reports, write_json  # noqa: F401


def cold_row(task="task-1", repeat=0, mode="baseline", *, path="pkg/source.py", size=8192, digest=None):
    digest = digest or hashlib.sha256(task.encode()).hexdigest()
    sandbox = f"cold-{task}-{repeat}-{mode}"
    metadata = {"path": path, "size_bytes": size, "device": 19 if mode == "baseline" else 20,
                "inode": 100 + repeat + (10 if mode == "t1" else 0),
                "mtime_ns": 123456 + repeat, "mode": stat.S_IFREG | 0o644}
    read_ns = (repeat + 1) * 1_000_000
    selected = {"file": copy.deepcopy(metadata), "preference": "python",
                "selection_payload_bytes_read": 0}
    read = {"file": copy.deepcopy(metadata), "bytes_read": size, "source_sha256": digest,
            "first_byte_hex": "61", "operation_ns": read_ns,
            "operation": analysis.FIRST_TOUCH_OPERATIONS["read"]}
    written = {"file_path": path, "size_bytes": size, "bytes_written": 1,
               "source_sha256": digest, "after_sha256": digest, "operation_ns": read_ns * 2,
               "operation": analysis.FIRST_TOUCH_OPERATIONS["write"],
               "verification_payload_bytes_read": size, "contents_unchanged": True}
    touch = {"schema": analysis.FIRST_TOUCH_SCHEMA, "sandbox_id": sandbox, "success": True,
             "endpoint": "post-cold microprobe, excluded from original cold-start endpoint",
             "selection": analysis.FIRST_TOUCH_SELECTION,
             "file": {**metadata, "source_sha256": digest}}
    for action, value in (("select", selected), ("read", read), ("write", written)):
        server = value.get("operation_ns", 1000) + 10_000_000
        touch[action] = {"client_rpc_ns": server + 5_000_000, "server_command_ns": server,
                         "response": {"exit_code": 0, "sandbox_id": sandbox, "duration_ns": server,
                                      "stdout": json.dumps(value)}, "result": value}
    return {"task": {"instance_id": task}, "repeat": repeat, "mode": mode, "sandbox_id": sandbox,
            "cold_start_ns": 2_000_000, "filesystem_provision_ns": 1_000_000,
            "client_submission_to_ready_ns": 2_500_000, "wall_monotonic_discrepancy_ns": 0,
            "first_touch": touch}


def plan(tasks=1, repetitions=1):
    return {"tasks": tasks, "cold_repetitions": repetitions, "post_cold_first_touch": True}


def refresh_stdout(row):
    for action in ("select", "read", "write"):
        event = row["first_touch"][action]
        event["response"]["stdout"] = json.dumps(event["result"])


def test_matched_population_aggregates_each_timing_separately_without_inode_equality():
    rows = [cold_row(task, repeat, mode) for task in ("task-1", "task-2")
            for repeat in range(3) for mode in ("baseline", "t1")]
    result = analysis._cold_first_touch(plan(2, 3), rows, {"task-1", "task-2"})
    assert result["schema"] == "crate-post-cold-first-touch-analysis-v1"
    assert result["raw_schema"] == "crate-post-cold-first-touch-v1"
    assert set(result["files"]) == {"task-1", "task-2"}
    assert set(result["files"]["task-1"]) == {"path", "size_bytes", "source_sha256"}
    for mode in ("baseline", "t1"):
        measured = result["modes"][mode]
        assert measured["samples"] == 6
        assert measured["read"]["operation_ms"] == {"n": 6, "mean": 2, "p50": 2, "p95": 3, "p99": 3, "max": 3}
        assert measured["read"]["server_command_ms"]["mean"] == 12
        assert measured["read"]["client_rpc_ms"]["mean"] == 17
        assert measured["write"]["operation_ms"]["mean"] == 4
        assert measured["write"]["server_command_ms"]["mean"] == 14
        assert measured["write"]["client_rpc_ms"]["mean"] == 19


def test_slower_treatment_is_reported_without_filtering_or_reclassification():
    rows = [cold_row(mode=mode) for mode in ("baseline", "t1")]
    for action in ("read", "write"):
        event = rows[1]["first_touch"][action]
        event["result"]["operation_ns"] *= 4
        event["server_command_ns"] *= 4
        event["response"]["duration_ns"] *= 4
        event["client_rpc_ns"] *= 4
    refresh_stdout(rows[1])
    result = analysis._cold_first_touch(plan(), rows, {"task-1"})
    assert result["modes"]["t1"]["read"]["operation_ms"]["mean"] == 4
    assert result["modes"]["baseline"]["read"]["operation_ms"]["mean"] == 1


@pytest.mark.parametrize("change", [
    lambda row: row.pop("first_touch"),
    lambda row: row["first_touch"].update(success=False),
    lambda row: row["first_touch"].update(schema="crate-swebench-real-analysis-v1"),
    lambda row: row["first_touch"].update(sandbox_id="other"),
    lambda row: row["first_touch"].update(endpoint="cold_start_ns includes first touch"),
    lambda row: row["first_touch"].update(selection="random file"),
    lambda row: row["first_touch"].pop("read"),
    lambda row: row["first_touch"]["read"]["response"].update(exit_code=1),
    lambda row: row["first_touch"]["read"]["response"].update(sandbox_id="other"),
    lambda row: row["first_touch"]["read"]["response"].update(duration_ns=1),
    lambda row: row["first_touch"]["write"]["response"].update(stdout="{}"),
])
def test_missing_failed_or_field_mixed_action_evidence_fails(change):
    rows = [cold_row(mode=mode) for mode in ("baseline", "t1")]
    change(rows[1])
    with pytest.raises(ValueError):
        analysis._cold_first_touch(plan(), rows, {"task-1"})


@pytest.mark.parametrize("action,field,value", [
    ("select", "client_rpc_ns", 0), ("select", "server_command_ns", -1),
    ("read", "client_rpc_ns", float("nan")), ("read", "server_command_ns", float("inf")),
    ("write", "client_rpc_ns", True), ("write", "server_command_ns", 2.5),
    ("read", "client_rpc_ns", 1),
])
def test_nonpositive_nonfinite_or_misordered_api_timings_are_rejected(action, field, value):
    rows = [cold_row(mode=mode) for mode in ("baseline", "t1")]
    rows[0]["first_touch"][action][field] = value
    with pytest.raises(ValueError):
        analysis._cold_first_touch(plan(), rows, {"task-1"})


@pytest.mark.parametrize("change", [
    lambda touch: touch["read"]["result"].update(operation_ns=0),
    lambda touch: touch["read"]["result"].update(operation_ns=float("nan")),
    lambda touch: touch["write"]["result"].update(operation_ns=float("inf")),
    lambda touch: touch["write"]["result"].update(operation_ns=1_000_000_000),
    lambda touch: touch["read"]["result"].update(operation=analysis.FIRST_TOUCH_OPERATIONS["write"]),
    lambda touch: touch["write"]["result"].update(operation="write without fsync"),
    lambda touch: touch["write"]["result"].update(after_sha256="a" * 64),
    lambda touch: touch["write"]["result"].update(contents_unchanged=False),
    lambda touch: touch["write"]["result"].update(bytes_written=2),
    lambda touch: touch["write"]["result"].update(verification_payload_bytes_read=0),
    lambda touch: touch["read"]["result"].update(bytes_read=1),
    lambda touch: touch["read"]["result"].update(first_byte_hex=""),
    lambda touch: touch["select"]["result"].update(selection_payload_bytes_read=8192),
    lambda touch: touch["read"]["result"].pop("operation_ns"),
])
def test_hash_write_preservation_and_inner_endpoint_invariants_fail_closed(change):
    rows = [cold_row(mode=mode) for mode in ("baseline", "t1")]
    change(rows[0]["first_touch"])
    refresh_stdout(rows[0])
    with pytest.raises(ValueError):
        analysis._cold_first_touch(plan(), rows, {"task-1"})


@pytest.mark.parametrize("path", ["../base/file.py", "/base/file.py", "pkg//file.py", "pkg/./file.py", "bad\0name.py", ""])
def test_unsafe_file_paths_are_rejected(path):
    rows = [cold_row(mode=mode, path=path) for mode in ("baseline", "t1")]
    with pytest.raises(ValueError, match="identity"):
        analysis._cold_first_touch(plan(), rows, {"task-1"})


@pytest.mark.parametrize("field,value", [("mode", stat.S_IFLNK | 0o777), ("inode", -1), ("device", "dev"), ("mtime_ns", None)])
def test_unsafe_or_invalid_metadata_is_rejected(field, value):
    rows = [cold_row(mode=mode) for mode in ("baseline", "t1")]
    touch = rows[0]["first_touch"]
    for file in (touch["file"], touch["select"]["result"]["file"], touch["read"]["result"]["file"]):
        file[field] = value
    refresh_stdout(rows[0])
    with pytest.raises(ValueError):
        analysis._cold_first_touch(plan(), rows, {"task-1"})


@pytest.mark.parametrize("changed", [{"path": "pkg/other.py"}, {"size": 4096}, {"digest": "d" * 64}])
@pytest.mark.parametrize("changed_repeat,changed_mode", [(0, "t1"), (1, "baseline")])
def test_selected_path_size_and_hash_must_match_across_modes_and_repeats(changed, changed_repeat, changed_mode):
    rows = [cold_row(repeat=repeat, mode=mode,
                     **(changed if (repeat, mode) == (changed_repeat, changed_mode) else {}))
            for repeat in range(2) for mode in ("baseline", "t1")]
    with pytest.raises(ValueError, match="across modes or repeats"):
        analysis._cold_first_touch(plan(repetitions=2), rows, {"task-1"})


@pytest.mark.parametrize("mutation", [
    lambda rows: rows.pop(),
    lambda rows: rows.append(copy.deepcopy(rows[0])),
    lambda rows: rows[0].update(repeat=1),
    lambda rows: rows[0].update(repeat=True),
    lambda rows: rows[0].update(mode="t0"),
    lambda rows: rows[0].update(task={"instance_id": "different-task"}),
])
def test_every_registered_task_repeat_mode_is_required(mutation):
    rows = [cold_row(mode=mode) for mode in ("baseline", "t1")]
    mutation(rows)
    with pytest.raises(ValueError):
        analysis._cold_first_touch(plan(), rows, {"task-1"})


def test_opt_in_analysis_preserves_strict_fidelity_cold_and_real_tool_metrics(campaign):
    root, _, _ = campaign
    legacy = analysis.analyze(root)
    rows = [cold_row(mode=mode) for mode in ("baseline", "t1")]
    (root / "cold/samples.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    change_plan(root, post_cold_first_touch=True)
    result = analysis.analyze(root)
    for key in ("cold", "systems", "tool_vs_fullcopy"):
        assert result[key] == legacy[key]
    assert result["fidelity_verification"]["formal_evidence_verified"]
    assert result["cold_first_touch"]["modes"]["baseline"]["samples"] == 1
    assert result["cold_first_touch"]["modes"]["baseline"]["read"]["operation_ms"]["mean"] == 1
    text = analysis.markdown(result)
    for boundary in ("Separate post-cold", "Metadata-selected real tracked file", "cache warmth is unspecified",
                     "write follows the read", "Same-byte write + fsync", "outside the inner write",
                     "inside the server command and API/RPC", "device/inode need not match"):
        assert boundary in text


def test_legacy_plan_without_flag_remains_compatible_and_does_not_promote_unregistered_probes(campaign):
    root, _, _ = campaign
    legacy = analysis.analyze(root)
    cold = [json.loads(line) for line in (root / "cold/samples.jsonl").read_text().splitlines()]
    for row in cold:
        row["first_touch"] = {"success": False}
    (root / "cold/samples.jsonl").write_text("".join(json.dumps(row) + "\n" for row in cold))
    for flag in (None, False):
        if flag is False:
            change_plan(root, post_cold_first_touch=False)
        result = analysis.analyze(root)
        assert "cold_first_touch" not in result
        for key in ("cold", "systems", "tool_vs_fullcopy"):
            assert result[key] == legacy[key]


def test_enabled_plan_rejects_missing_probes_and_nonboolean_flag(campaign):
    root, _, _ = campaign
    change_plan(root, post_cold_first_touch=True)
    with pytest.raises(ValueError, match="first-touch"):
        analysis.analyze(root)
    change_plan(root, post_cold_first_touch="true")
    with pytest.raises(ValueError, match="Boolean"):
        analysis.analyze(root)
