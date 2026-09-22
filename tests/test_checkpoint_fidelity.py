"""Formal checkpoint rejects incomplete/drifted artifacts, not sleep jitter."""
import copy
import hashlib
import json

import pytest

from experiments.swebench_verified.checkpoints import compare


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def write_json(path, value):
    payload = (json.dumps(value, indent=2) + "\n").encode()
    path.write_bytes(payload)
    return digest(payload)


@pytest.fixture(params=['caden', 'orca'])
def reports(tmp_path, request):
    source = tmp_path / "source"
    source.mkdir()
    (source / "runner.py").write_text("# fixed replay runner\n")
    hashes = {"runner.py": digest((source / "runner.py").read_bytes())}
    commits = {f"{request.param}_base_commit": "a" * 40, "sandboxfs_commit": "b" * 40,
               "source_sha256": hashes,
               "source_manifest_sha256": digest(json.dumps(hashes, sort_keys=True).encode())}
    write_json(source / "SOURCE_PROVENANCE.json", commits)
    workloads = tmp_path / "workloads"
    workloads.mkdir()
    expected = {"diff_sha256": "c" * 64, "untracked": {"new.py": "d" * 64}}
    event = {"type": "tool", "sequence": 0, "source_arguments_sha256": "e" * 64,
             "source_tool": "bash", "operation": "original-command",
             "expected_exit_code": 1, "argv": ["/bin/bash", "-c", "false"]}
    workload = {"schema": f"{request.param}-tool-trajectory-v1", "base": "sv-00",
                "source": {"trajectory_id": "task-1", "instance_id": "task-1"},
                "tool_execution": {"kind": "original-commands", "proxy": False,
                                   "anonymous_memory_injection": False},
                "tool_count": 1, "fingerprint_expected": expected,
                "events": [{"type": "wait", "duration_ms": 1000.25,
                            "request_class": "django", "restore_profile": "default"},
                           event,
                           {"type": "wait", "duration_ms": 2000.5,
                            "request_class": "django", "restore_profile": "default"}]}
    manifest = {"schema": f"{request.param}-tool-trajectory-manifest-v1",
                "source": {"dataset": "SWE-bench_Verified", "revision": "fixed"},
                "conversion": {"kind": "original-commands", "wait_scale": 1.0,
                               "success_filter": False},
                "workloads": [{"path": "task-1.json", "tool_count": 1,
                               "sha256": write_json(workloads / "task-1.json", workload),
                               "capture_sha256": "f" * 64}]}
    manifest_hash = write_json(workloads / "manifest.json", manifest)
    tool = {key: value for key, value in event.items() if key not in {"type", "argv"}}
    tool.update(exit_code=1, turn_ns=1000, wake_restore_ns=100, command_ns=900,
                exec_rpc_ns=800, server_command_ns=700, result_validation_ns=30,
                result_pack_ns=70)
    baseline = {"schema": f"{request.param}-trajectory-replay-v1", "commits": commits,
                "config": {"wait_scale": 1, "requests": 1, "active_sandboxes": 1,
                           "workloads_dir": str(workloads),
                           "source_provenance": str(source / "SOURCE_PROVENANCE.json")},
                "summary": {"success": True, "errors": [], "requests": 1,
                            "completed_requests": 1, "expected_tool_calls": 1,
                            "completed_tool_calls": 1},
                "workload": {"manifest_sha256": manifest_hash, "workload_count": 1,
                             "source": manifest["source"], "conversion": manifest["conversion"]},
                "requests": [{"sequence": 0, "trajectory_id": "task-1", "instance_id": "task-1",
                              "base": "sv-00", "sandbox_id": "sandbox-1", "wave": 0}],
                "replays": [{"sequence": 0, "trajectory_id": "task-1", "sandbox_id": "sandbox-1",
                             "tools": [tool], "error": "", "wave": 0,
                             "waits_ns": [1001250000, 2001500000],
                             "fingerprint": {"exit_code": 0, "stdout": json.dumps(expected)}}]}
    return baseline, copy.deepcopy(baseline), workloads, source


def test_complete_artifacts_pass_with_jitter_and_unchanged_guards(reports):
    baseline, treatment, _, _ = reports
    treatment["replays"][0]["waits_ns"] = [1100000000, 2400000000]
    result = compare(baseline, treatment)
    assert result["guards_pass"]
    assert result["fidelity"]["formal_evidence_verified"]
    assert result["fidelity"]["planned_wait_events"] == 2
    assert not result["fidelity"]["runtime_requested_wait_sequence_logged"]
    assert result["per_task"]["task-1"]["calls"] == 1
    tool = treatment["replays"][0]["tools"][0]
    tool["turn_ns"] += 101
    tool["command_ns"] += 101
    tool["exec_rpc_ns"] += 101
    assert not compare(baseline, treatment)["guards_pass"]


@pytest.mark.parametrize("change", [
    lambda report: report["replays"][0]["waits_ns"].pop(),
    lambda report: report["replays"][0].pop("fingerprint"),
    lambda report: report["replays"][0]["fingerprint"].update(exit_code=1),
    lambda report: report["replays"][0]["fingerprint"].update(stdout='{"diff_sha256":"changed","untracked":{}}'),
    lambda report: report["summary"].update(expected_tool_calls=2),
    lambda report: report["summary"].update(completed_requests=2),
    lambda report: report["summary"].update(errors=["hidden error"]),
    lambda report: report["replays"][0]["tools"][0].update(operation="proxy-read"),
    lambda report: report["replays"][0]["tools"][0].update(source_tool="python"),
    lambda report: report["replays"][0]["tools"][0].update(source_arguments_sha256="changed"),
    lambda report: report["replays"][0]["tools"][0].update(expected_exit_code=0, exit_code=0),
    lambda report: report["replays"][0]["tools"][0].update(turn_ns=999),
    lambda report: report["replays"][0]["tools"][0].update(server_command_ns=801),
    lambda report: report["replays"][0]["tools"][0].update(result_pack_ns=-1),
    lambda report: report["replays"][0]["tools"][0].update(turn_ns=float("nan")),
    lambda report: report["requests"][0].update(base="wrong-base"),
    lambda report: report["requests"][0].update(sequence=1),
    lambda report: report["requests"][0].update(wave=1),
    lambda report: report["replays"][0].update(sandbox_id="other"),
    lambda report: report["commits"]["source_sha256"].update({"runner.py": "0" * 64}),
    lambda report: report["commits"].pop("source_manifest_sha256"),
    lambda report: report["workload"]["source"].update(revision="other"),
    lambda report: report["workload"]["conversion"].update(success_filter=True),
    lambda report: report["config"].update(active_sandboxes=2),
    lambda report: report.pop("schema"),
])
def test_realistic_reports_fail_closed_on_missing_or_changed_evidence(reports, change):
    baseline, treatment, _, _ = reports
    change(treatment)
    with pytest.raises(ValueError):
        compare(baseline, treatment)


def test_equal_but_wrong_final_fingerprints_are_rejected_against_capture(reports):
    baseline, treatment, _, _ = reports
    for report in (baseline, treatment):
        report["replays"][0]["fingerprint"]["stdout"] = '{"diff_sha256":"wrong","untracked":{}}'
    with pytest.raises(ValueError, match="unexpected final"):
        compare(baseline, treatment)


def test_source_file_drift_is_rejected_even_when_report_hashes_agree(reports):
    baseline, treatment, _, source = reports
    (source / "runner.py").write_text("# changed implementation\n")
    with pytest.raises(ValueError, match="frozen source drift"):
        compare(baseline, treatment)


def test_same_count_changed_wait_order_in_workload_is_rejected(reports):
    baseline, treatment, workloads, _ = reports
    workload = json.loads((workloads / "task-1.json").read_text())
    workload["events"][0], workload["events"][2] = workload["events"][2], workload["events"][0]
    write_json(workloads / "task-1.json", workload)
    with pytest.raises(ValueError, match="workload checksum"):
        compare(baseline, treatment)


def test_changed_argv_is_rejected_even_when_opaque_command_hash_unchanged(reports):
    baseline, treatment, workloads, _ = reports
    workload = json.loads((workloads / "task-1.json").read_text())
    workload["events"][1]["argv"][-1] = "true"
    write_json(workloads / "task-1.json", workload)
    with pytest.raises(ValueError, match="workload checksum"):
        compare(baseline, treatment)


def test_local_overrides_verify_synced_artifacts_without_rewriting_report(reports):
    baseline, treatment, workloads, source = reports
    for report in (baseline, treatment):
        report["config"]["workloads_dir"] = "/remote/unavailable/workloads"
        report["config"]["source_provenance"] = "/remote/unavailable/source/SOURCE_PROVENANCE.json"
    with pytest.raises(ValueError, match="checkpoint evidence"):
        compare(baseline, treatment)
    assert compare(baseline, treatment, workloads_dir=workloads, source_root=source)["guards_pass"]


def test_report_cannot_drop_provenance_to_enter_minimal_fixture_mode(reports):
    baseline, treatment, _, _ = reports
    for report in (baseline, treatment):
        for key in ("schema", "requests", "commits"):
            report.pop(key)
        report["config"].pop("workloads_dir")
        report["config"].pop("source_provenance")
    with pytest.raises(ValueError, match="checkpoint evidence"):
        compare(baseline, treatment)


def test_duplicate_tools_rejected_even_if_both_reports_match(reports):
    baseline, treatment, _, _ = reports
    for report in (baseline, treatment):
        report["replays"][0]["tools"].append(copy.deepcopy(report["replays"][0]["tools"][0]))
        report["summary"].update(expected_tool_calls=2, completed_tool_calls=2)
    with pytest.raises(ValueError):
        compare(baseline, treatment)


def test_minimal_fixture_compatibility_is_explicitly_not_formal_evidence():
    fixture = {"summary": {"success": True}, "config": {"wait_scale": 1},
               "workload": {"manifest_sha256": "fixed"}, "replays": [{"trajectory_id": "task", "tools": [
                   {"sequence": 0, "source_arguments_sha256": "arg", "expected_exit_code": 1,
                    "exit_code": 1, "turn_ns": 1000}]}]}
    result = compare(fixture, copy.deepcopy(fixture))
    assert result["guards_pass"]
    assert result["fidelity"]["mode"] == "minimal-fixture-unverified"
    assert not result["fidelity"]["formal_evidence_verified"]
