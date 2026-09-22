"""Fixed formal cohorts preserve adverse outcomes without inventing memory splits."""
import copy
import json

import pytest

from experiments.swebench_verified import analyze as analysis
from tests.test_analysis_fidelity import campaign, change_plan, reports, write_json  # noqa: F401


@pytest.fixture
def formal_campaign(campaign):
    root, workloads, source = campaign
    original = json.loads((workloads / "task-1.json").read_text())
    manifest = json.loads((workloads / "manifest.json").read_text())
    manifest["source"] = {"dataset": "princeton-nlp/SWE-bench_Verified", "revision": "fixed"}
    manifest["workloads"] = []
    tasks = [{"sequence": index, "instance_id": f"task-{index:02d}",
              "base": f"sv-{index:02d}", "repo": "example/repo"} for index in range(32)]
    selection = {"schema": "crate-swebench-verified-selection-v1",
                 **manifest["source"], "tasks": tasks}
    selection_hash = write_json(root / "selection.json", selection)
    for task in tasks:
        workload = copy.deepcopy(original)
        workload["source"] = {"trajectory_id": task["instance_id"],
                              "instance_id": task["instance_id"], "repo": task["repo"]}
        workload["base"] = task["base"]
        name = task["instance_id"] + ".json"
        manifest["workloads"].append({"path": name, "tool_count": 1,
                                      "sha256": write_json(workloads / name, workload),
                                      "capture_sha256": "f" * 64})
    manifest_hash = write_json(workloads / "manifest.json", manifest)
    ids = [task["instance_id"] for task in tasks]
    change_plan(root, tasks=32, active_sandboxes=8, selection={
        "schema": analysis.COHORT_SCHEMA, "manifest_path": "selection.json",
        "manifest_sha256": selection_hash, "task_ids": ids, "sequences": list(range(32)),
        "development_task_ids": ids[:8], "heldout_task_ids": ids[8:]})
    for repeat in range(3):
        for label in ("F0-S0", "T1-S2"):
            path = root / f"replay-r{repeat}-{label}.json"
            report = json.loads(path.read_text())
            replay_template, request_template = report["replays"][0], report["requests"][0]
            report["replays"], report["requests"] = [], []
            report["config"].update(requests=32, active_sandboxes=8)
            report["summary"].update(requests=32, completed_requests=32,
                                     expected_tool_calls=32, completed_tool_calls=32)
            report["workload"].update(manifest_sha256=manifest_hash, workload_count=32,
                                      source=manifest["source"])
            for task in tasks:
                index, task_id = task["sequence"], task["instance_id"]
                request = copy.deepcopy(request_template)
                request.update(sequence=index, trajectory_id=task_id, instance_id=task_id,
                               sandbox_id=f"sandbox-{index}", base=task["base"])
                replay = copy.deepcopy(replay_template)
                replay.update(sequence=index, trajectory_id=task_id, sandbox_id=request["sandbox_id"])
                duration = (index + 1) * 1_000_000
                if label == "T1-S2":
                    duration *= repeat + 2
                    if index == 31:
                        duration = 200_000_000_000 + repeat * 1_000_000_000
                replay["tools"][0].update(turn_ns=duration, wake_restore_ns=100,
                                            command_ns=duration-100, exec_rpc_ns=duration-200,
                                            server_command_ns=duration-300,
                                            result_validation_ns=30, result_pack_ns=70)
                report["requests"].append(request)
                report["replays"].append(replay)
            write_json(path, report)
    rows = [{"task": task, "repeat": 0, "mode": mode, "cold_start_ns": 2_000_000,
             "filesystem_provision_ns": 1_000_000, "client_submission_to_ready_ns": 2_500_000,
             "wall_monotonic_discrepancy_ns": 0} for task in tasks for mode in ("baseline", "t1")]
    (root / "cold/samples.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    return root, workloads, source


def test_fixed_cohorts_report_all_runs_and_pooled_adverse_calls(formal_campaign):
    root, _, _ = formal_campaign
    result = analysis.analyze(root)
    cohorts = result["formal_cohorts"]
    assert cohorts["schema"] == analysis.COHORT_SCHEMA
    assert result["raw_sha256"]["selection.json"] == cohorts["selection_manifest_sha256"]
    development, heldout = (cohorts["cohorts"][key] for key in ("development", "heldout"))
    assert development["sequences"] == list(range(8)) and development["distinct_tasks"] == 8
    assert heldout["sequences"] == list(range(8, 32)) and heldout["distinct_tasks"] == 24
    assert not set(development["task_ids"]) & set(heldout["task_ids"])
    for cohort in (development, heldout):
        for label, values in cohort["systems"].items():
            assert [run["repeat"] for run in values["runs"]] == [0, 1, 2]
            pooled = values["pooled"]
            assert pooled["tool_ms"]["n"] == pooled["completed_task_runs"] == cohort["distinct_tasks"] * 3
            assert pooled["completed_tool_calls"] == pooled["expected_tool_calls"] == pooled["tool_ms"]["n"]
            assert pooled["observed_nonzero_shell_exits"] == pooled["expected_nonzero_shell_exits"] == pooled["tool_ms"]["n"]
            assert pooled["unexpected_tool_outcomes"] == pooled["task_execution_failures"] == pooled["infrastructure_failures"] == 0
            assert "memory" not in values and "memory" not in pooled
    assert development["systems"]["F0-S0"]["pooled"]["tool_ms"]["mean"] == 4.5
    assert heldout["systems"]["F0-S0"]["pooled"]["tool_ms"]["mean"] == 20.5
    adverse = heldout["systems"]["T1-S2"]
    assert [run["absolute_deadline_violations"] for run in adverse["runs"]] == [1, 1, 1]
    assert adverse["pooled"]["absolute_deadline_violations"] == 3
    assert adverse["pooled"]["tool_ms"]["p99"] == 202_000
    assert not result["fidelity_verification"]["performance_guards_pass"]
    text = analysis.markdown(result)
    for phrase in ("not independent experimental groups", "No cohort DRAM", "Expected nonzero shell exits",
                   "Deadline misses", "Development: 8", "Heldout: 24", "pooled",
                   "Manager.Create entry", "initial server request JSON decoding are outside"):
        assert phrase in text


def test_single_run_reports_32_tasks_not_three_replicates(formal_campaign):
    root, workloads, source = formal_campaign
    change_plan(root, repetitions=1, cold_repetitions=1, formal_profile="comparison",
                orders=[["F0-S0", "T1-S2"]])
    result = analysis.analyze(root, workloads_dir=workloads, source_root=source)
    for label in ("F0-S0", "T1-S2"):
        assert result["systems"][label]["tool_ms"]["n"] == 32
        for name, expected in (("development", 8), ("heldout", 24)):
            cohort = result["formal_cohorts"]["cohorts"][name]["systems"][label]
            assert len(cohort["runs"]) == 1
            assert cohort["pooled"]["completed_task_runs"] == expected
    assert not result["fidelity_verification"]["performance_guards_pass"]  # Preserve the adverse tail.


def test_tool_weighted_pooling_is_not_an_average_of_task_means():
    def tool(duration, code=0):
        return {"turn_ns": duration * 1_000_000, "exit_code": code, "expected_exit_code": code}
    population = [{"tools": [tool(100)]}, {"tools": [tool(1), tool(1), tool(1, 124)]}]
    result = analysis._cohort_population(population, 4, 50)
    assert result["tool_ms"]["mean"] == 25.75
    assert result["absolute_deadline_violations"] == 1
    assert result["expected_nonzero_shell_exits"] == result["observed_nonzero_shell_exits"] == 1
    assert result["observed_timeout_like_shell_exits"] == 1 and result["infrastructure_failures"] == 0


@pytest.mark.parametrize("change", [
    lambda plan: plan["selection"].update(schema="invented"),
    lambda plan: plan["selection"].update(manifest_path="../selection.json"),
    lambda plan: plan["selection"].update(manifest_sha256="0" * 64),
    lambda plan: plan["selection"]["task_ids"].reverse(),
    lambda plan: plan["selection"]["sequences"].__setitem__(0, False),
    lambda plan: plan["selection"]["development_task_ids"].append("task-08"),
    lambda plan: plan["selection"]["heldout_task_ids"].pop(),
    lambda plan: plan.update(kind="development"),
    lambda plan: plan.update(selection=None),
])
def test_registration_cannot_repartition_reorder_or_downgrade(formal_campaign, change):
    root, _, _ = formal_campaign
    plan = json.loads((root / "PLAN.json").read_text())
    change(plan)
    write_json(root / "PLAN.json", plan)
    with pytest.raises(ValueError):
        analysis.analyze(root)


@pytest.mark.parametrize("change", [
    lambda selection: selection["tasks"].pop(),
    lambda selection: selection["tasks"].reverse(),
    lambda selection: selection["tasks"][0].update(instance_id="task-01"),
    lambda selection: selection["tasks"][0].update(instance_id="foreign"),
    lambda selection: selection["tasks"][0].update(base="other-base"),
    lambda selection: selection["tasks"][0].update(repo="other/repo"),
    lambda selection: selection["tasks"][0].update(sequence=True),
    lambda selection: selection.update(revision="other-revision"),
    lambda selection: selection.update(dataset="other-dataset"),
])
def test_selection_evidence_cannot_diverge_even_with_updated_registration_hash(formal_campaign, change):
    root, _, _ = formal_campaign
    selection = json.loads((root / "selection.json").read_text())
    change(selection)
    digest = write_json(root / "selection.json", selection)
    plan = json.loads((root / "PLAN.json").read_text())
    plan["selection"]["manifest_sha256"] = digest
    write_json(root / "PLAN.json", plan)
    with pytest.raises(ValueError):
        analysis.analyze(root)


@pytest.mark.parametrize("missing", ["selection", "source", "workload"])
def test_missing_or_mutated_artifacts_fail_closed(formal_campaign, missing):
    root, workloads, source = formal_campaign
    if missing == "selection":
        (root / "selection.json").unlink()
    elif missing == "source":
        (source / "runner.py").write_text("# changed\n")
    else:
        (workloads / "task-31.json").write_text("{}")
    with pytest.raises(ValueError):
        analysis.analyze(root)


@pytest.mark.parametrize("change", [
    lambda report: report["replays"].pop(),
    lambda report: report["requests"][0].update(sequence=8),
    lambda report: report["requests"][0].update(instance_id="foreign"),
    lambda report: report["replays"][31].update(error="infrastructure failure"),
    lambda report: report["summary"].update(errors=["background failure"]),
    lambda report: report["replays"][31]["tools"][0].update(exit_code=0),
])
def test_failure_or_missing_holdout_work_never_becomes_a_successful_subset(formal_campaign, change):
    root, _, _ = formal_campaign
    path = root / "replay-r2-T1-S2.json"
    report = json.loads(path.read_text())
    change(report)
    write_json(path, report)
    with pytest.raises(ValueError):
        analysis.analyze(root)


def test_legacy_plans_keep_metrics_without_invented_cohort_registration(formal_campaign):
    root, _, _ = formal_campaign
    registered = analysis.analyze(root)
    plan = json.loads((root / "PLAN.json").read_text())
    plan.pop("selection")
    write_json(root / "PLAN.json", plan)
    legacy = analysis.analyze(root)
    assert "formal_cohorts" not in legacy
    assert "no development-versus-heldout partition is inferred retroactively" in analysis.markdown(legacy)
    for key in ("systems", "cold", "tool_vs_fullcopy", "workload_manifest_sha256"):
        assert legacy[key] == registered[key]


def test_exactly_32_is_required_for_registered_cohorts(campaign):
    root, _, _ = campaign
    change_plan(root, selection={"schema": analysis.COHORT_SCHEMA})
    with pytest.raises(ValueError, match="exactly 32"):
        analysis.analyze(root)


def test_registered_cohorts_and_first_touch_keep_distinct_populations(formal_campaign):
    from tests.test_first_touch_analysis import cold_row

    root, _, _ = formal_campaign
    before = analysis.analyze(root)
    tasks = json.loads((root / "selection.json").read_text())["tasks"]
    rows = [cold_row(task["instance_id"], mode=mode) for task in tasks for mode in ("baseline", "t1")]
    (root / "cold/samples.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    change_plan(root, post_cold_first_touch=True)
    result = analysis.analyze(root)
    assert result["formal_cohorts"] == before["formal_cohorts"]
    assert result["systems"] == before["systems"]
    assert result["cold_first_touch"]["modes"]["t1"]["samples"] == 32


def test_coordinated_workload_and_report_reorder_still_cannot_redefine_cohorts(formal_campaign):
    root, workloads, _ = formal_campaign
    manifest = json.loads((workloads / "manifest.json").read_text())
    manifest["workloads"][0], manifest["workloads"][8] = manifest["workloads"][8], manifest["workloads"][0]
    digest = write_json(workloads / "manifest.json", manifest)
    for repeat in range(3):
        for label in ("F0-S0", "T1-S2"):
            path = root / f"replay-r{repeat}-{label}.json"
            report = json.loads(path.read_text())
            report["workload"]["manifest_sha256"] = digest
            for field in ("requests", "replays"):
                report[field][0], report[field][8] = report[field][8], report[field][0]
                for index, row in enumerate(report[field]):
                    row["sequence"] = index
            write_json(path, report)
    with pytest.raises(ValueError, match="cohort workload identity/order"):
        analysis.analyze(root)


def test_synced_local_workload_and_source_overrides_preserve_cohort_evidence(formal_campaign):
    root, workloads, source = formal_campaign
    expected = analysis.analyze(root)["formal_cohorts"]
    for repeat in range(3):
        for label in ("F0-S0", "T1-S2"):
            path = root / f"replay-r{repeat}-{label}.json"
            report = json.loads(path.read_text())
            report["config"].update(workloads_dir="/remote/unavailable/workloads",
                                    source_provenance="/remote/unavailable/source/SOURCE_PROVENANCE.json")
            write_json(path, report)
    assert analysis.analyze(root, workloads_dir=workloads, source_root=source)["formal_cohorts"] == expected
