"""Analysis callers distinguish strict artifact checks from legacy metrics."""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from experiments.swebench_verified import analyze as analysis
from tests.test_checkpoint_fidelity import reports, write_json  # noqa: F401


MEMORY = ("sandbox_memory_current_bytes", "task_daemon_cgroup_sum_bytes",
          "task_service_cgroup_sum_bytes", "daemon_plus_sandbox_cgroup_bytes",
          "signed_host_physical_delta_bytes", "signed_host_available_delta_bytes",
          "sandbox_memory_swap_bytes")


@pytest.fixture
def campaign(tmp_path, reports):
    baseline, treatment, workloads, source = reports
    root = tmp_path / "campaign"
    root.mkdir()
    plan = {"kind": "formal", "tasks": 1, "active_sandboxes": 1,
            "repetitions": 3, "orders": [["F0-S0", "T1-S2"], ["T1-S2", "F0-S0"],
                                         ["F0-S0", "T1-S2"]],
            "turn_latency_absolute_deadline_ms": 180000,
            "relative_p95_p99_guard": 1.10, "cold_repetitions": 1}
    write_json(root / "PLAN.json", plan)
    write_json(root / "COMPLETED.json", {"kind": "formal"})
    for report in (baseline, treatment):
        report["samples"] = [{"monotonic_ns": n * 1000, "phase": phase,
                              **{field: (n + 1) * 1024 for field in MEMORY}}
                             for n, phase in enumerate(("idle", "create", "trace_replay", "cleanup"))]
        report["summary"].update(reclaim_events=0, reclaim_errors=0,
                                 speculative_prepared_bytes=0, speculative_restore_errors=0,
                                 pool_hits=0, pool_misses=1, completed_turns_per_second=1)
    for repeat in range(3):
        write_json(root / f"replay-r{repeat}-F0-S0.json", baseline)
        write_json(root / f"replay-r{repeat}-T1-S2.json", treatment)
    cold = [{"task": {"instance_id": "task-1"}, "repeat": 0, "mode": mode,
             "cold_start_ns": 2000000, "filesystem_provision_ns": 1000000,
             "client_submission_to_ready_ns": 2500000, "wall_monotonic_discrepancy_ns": 0}
            for mode in ("baseline", "t1")]
    (root / "cold").mkdir()
    (root / "cold/samples.jsonl").write_text("".join(json.dumps(row) + "\n" for row in cold))
    return root, workloads, source


def change_plan(root, **values):
    plan = json.loads((root / "PLAN.json").read_text())
    plan.update(values)
    write_json(root / "PLAN.json", plan)


def change_report(root, repeat, label, change):
    path = root / f"replay-r{repeat}-{label}.json"
    report = json.loads(path.read_text())
    change(report)
    write_json(path, report)


def test_formal_uses_resolvable_defaults_and_preserves_full_checks(campaign):
    root, _, _ = campaign
    with patch.object(analysis, "compare_checkpoint", wraps=analysis.compare_checkpoint) as compare:
        result = analysis.analyze(root)
    assert compare.call_count == 3
    evidence = result["fidelity_verification"]
    assert evidence["mode"] == "artifact-verified" and evidence["formal_evidence_verified"]
    assert set(evidence["checks"]) == {"r0-T1-S2", "r1-T1-S2", "r2-T1-S2"}
    assert all(check["fidelity"]["source_files_verified"] for check in evidence["checks"].values())
    text = analysis.markdown(result)
    assert "Strict replay evidence verified: True" in text
    assert "runtime_requested_wait_sequence_logged" in text
    assert "raw_capture_files_verified" in text
    assert all(key in text for key in evidence["checks"])


@pytest.mark.parametrize("kind", ["pilot", "development"])
def test_legacy_without_overrides_is_explicitly_unverified(campaign, kind):
    root, _, _ = campaign
    change_plan(root, kind=kind)
    with patch.object(analysis, "compare_checkpoint", side_effect=AssertionError("should not verify")):
        result = analysis.analyze(root)
    assert result["fidelity_verification"]["mode"] == "legacy-not-artifact-verified"
    assert not result["fidelity_verification"]["formal_evidence_verified"]
    assert "No new strict-gate success is claimed" in analysis.markdown(result)


def test_explicit_overrides_verify_legacy_and_preserve_all_metric_values(campaign):
    root, workloads, source = campaign
    change_plan(root, kind="pilot")
    legacy = analysis.analyze(root)
    verified = analysis.analyze(root, workloads_dir=workloads, source_root=source)
    assert verified["fidelity_verification"]["formal_evidence_verified"]
    for key in ("systems", "cold", "tool_vs_fullcopy", "raw_sha256", "workload_manifest_sha256"):
        assert verified[key] == legacy[key]


def test_formal_fails_closed_on_missing_default_paths_but_accepts_local_copies(campaign):
    root, workloads, source = campaign
    for repeat in range(3):
        for label in ("F0-S0", "T1-S2"):
            change_report(root, repeat, label, lambda report: report["config"].update(
                workloads_dir="/remote/missing/workloads", source_provenance="/remote/missing/source/SOURCE_PROVENANCE.json"))
    with pytest.raises(ValueError, match="checkpoint evidence"):
        analysis.analyze(root)
    assert analysis.analyze(root, workloads_dir=workloads, source_root=source)["fidelity_verification"]["formal_evidence_verified"]


@pytest.mark.parametrize("which", ["workloads_dir", "source_root"])
def test_override_pair_required(campaign, which):
    root, workloads, source = campaign
    with pytest.raises(ValueError, match="supplied together"):
        analysis.analyze(root, **{which: workloads if which == "workloads_dir" else source})


def test_strict_verification_rejects_fingerprint_failure_in_any_repetition(campaign):
    root, _, _ = campaign
    change_report(root, 2, "T1-S2", lambda report: report["replays"][0]["fingerprint"].update(exit_code=1))
    with pytest.raises(ValueError, match="failed final workspace fingerprint"):
        analysis.analyze(root)


def test_strict_verification_rejects_minimal_fixture_success_flag(campaign):
    root, _, _ = campaign
    with patch.object(analysis, "compare_checkpoint", return_value={
        "guards_pass": True, "fidelity": {"formal_evidence_verified": False}
    }):
        with pytest.raises(ValueError, match="artifact-verified checkpoint"):
            analysis.analyze(root)


def test_negative_performance_is_reported_not_suppressed(campaign):
    root, _, _ = campaign
    for repeat in range(3):
        def slow(report):
            tool = report["replays"][0]["tools"][0]
            tool.update(turn_ns=1300, command_ns=1200, exec_rpc_ns=1100)
        change_report(root, repeat, "T1-S2", slow)
    result = analysis.analyze(root)
    assert result["fidelity_verification"]["formal_evidence_verified"]
    assert not result["fidelity_verification"]["performance_guards_pass"]
    assert not result["tool_vs_fullcopy"]["T1-S2"]["both_relative_tail_guards_pass"]
    assert "registered performance guards pass: False" in analysis.markdown(result)


@pytest.mark.parametrize("duration,violation", [(180_000_000_000, False), (180_000_000_001, True)])
def test_baseline_deadline_is_part_of_guard_status_without_filtering_evidence(campaign, duration, violation):
    root, _, _ = campaign

    def slow_baseline(report):
        report["replays"][0]["tools"][0].update(
            turn_ns=duration, command_ns=duration-100, exec_rpc_ns=duration-200)

    change_report(root, 1, "F0-S0", slow_baseline)
    result = analysis.analyze(root)
    evidence = result["fidelity_verification"]
    check = evidence["checks"]["r1-T1-S2"]
    assert evidence["formal_evidence_verified"]
    assert check["guards_pass"]  # Fast treatment passes its unchanged relative/deadline checks.
    assert check["baseline_deadline_violations"] == int(violation)
    assert check["deadline_violations"] == 0
    assert evidence["performance_guards_pass"] is not violation
    baseline = result["systems"]["F0-S0"]
    assert baseline["tool_ms"]["n"] == 3
    assert baseline["tool_ms"]["max"] == duration / 1e6
    assert baseline["run_tool_distributions_ms"][1]["mean"] == duration / 1e6
    assert baseline["absolute_deadline_violations"] == int(violation)
    text = analysis.markdown(result)
    assert f"Aggregate registered performance guards pass: {not violation}." in text
    check_line = next(line for line in text.splitlines() if line.startswith("- r1-T1-S2:"))
    assert f"registered performance guards pass: {not violation};" in check_line
    assert f"baseline absolute-deadline violations: {int(violation)};" in check_line
    assert "treatment absolute-deadline violations: 0" in check_line


def test_strict_source_revision_cannot_change_across_repetitions(campaign):
    root, _, _ = campaign
    change_report(root, 1, "F0-S0", lambda report: report["commits"].update(branch="other"))
    with pytest.raises(ValueError, match="source changed across repetitions"):
        analysis.analyze(root)


@pytest.mark.parametrize("order", [["T1-S2"], ["F0-S0"], ["F0-S0", "T1-S2", "T1-S2"]])
def test_strict_repetition_requires_unique_baseline_and_treatment(campaign, order):
    root, _, _ = campaign
    change_plan(root, orders=[order] * 3)
    with pytest.raises(ValueError, match="one baseline and treatments"):
        analysis.analyze(root)


def test_cli_writes_fidelity_to_json_and_markdown(campaign, tmp_path):
    root, workloads, source = campaign
    output = tmp_path / "analysis-output"
    completed = subprocess.run([
        sys.executable, str(Path(analysis.__file__).resolve()), "--campaign", str(root),
        "--output", str(output), "--workloads-dir", str(workloads), "--source-root", str(source),
    ], check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    result = json.loads((output / "analysis.json").read_text())
    assert result["fidelity_verification"]["formal_evidence_verified"]
    assert "artifact-verified" in (output / "RESULTS.md").read_text()


def test_cli_rejects_unpaired_override_before_creating_output(campaign, tmp_path):
    root, workloads, _ = campaign
    output = tmp_path / "rejected-output"
    completed = subprocess.run([
        sys.executable, str(Path(analysis.__file__).resolve()), "--campaign", str(root),
        "--output", str(output), "--workloads-dir", str(workloads),
    ], check=False, capture_output=True, text=True)
    assert completed.returncode == 2 and "supplied together" in completed.stderr
    assert not output.exists()
