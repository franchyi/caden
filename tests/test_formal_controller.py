"""Formal-controller authorization and immutable-evidence gates; no real SSH."""
import copy
import hashlib
import json
import shlex
import subprocess

import pytest

from experiments.swebench_verified import formal_controller as controller


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def make_source(package, name, *, runtime="fixed candidate", harness="guard code"):
    source = package / name
    files = {"src/caden/policy.py": runtime,
             "experiments/trajectory_replay/run_campaign.py": runtime,
             "experiments/sandboxfs_memory/run_campaign.py": runtime,
             "experiments/swebench_verified/routing.py": runtime,
             "experiments/swebench_verified/capture.py": runtime,
             "experiments/swebench_verified/run_suite.py": harness}
    for relative, content in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    hashes = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()}
    write(source / "SOURCE_PROVENANCE.json", {"source_sha256": hashes,
        "source_manifest_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()})
    return controller.gate.verify_local_source(source)


@pytest.fixture
def reviewed(tmp_path, monkeypatch):
    q2 = make_source(tmp_path, "source-development-q2", harness="old harness")
    formal = make_source(tmp_path, "source-formal-q2", harness="new guards")
    args = controller.parser().parse_args(["--package", str(tmp_path), "--source", "source-formal-q2",
        "--campaign", "formal-q2", "--review-receipt", str(tmp_path / "REVIEW.json"),
        "--pool-strategy", "queue", "--restore-mode", "thaw-only", "--scheduler-wakes", "8", "--first-touch"])
    analysis = {"plan": {"kind": "development", "tasks": 8, "orders": [["F0-S0", "T1-S2"]],
        "development_profile": "candidate", "pool_strategy": "queue", "confirmed_restore_mode": "thaw-only",
        "scheduler_wake_burst_limit": 8, "post_cold_first_touch": True, "configs": {"F0-S0": ["fixed"], "T1-S2": ["fixed"]}},
        "systems": {}, "cold": {}, "tool_vs_fullcopy": {}, "raw_sha256": {}, "workload_manifest_sha256": "fixed",
        "fidelity_verification": {"formal_evidence_verified": True, "performance_guards_pass": True,
                                  "checks": {"r0-T1-S2": {"baseline_deadline_violations": 0}}}}
    artifact = tmp_path / "development-q2-analysis/analysis.json"
    write(artifact, analysis)
    review = {"schema": "crate-reviewed-formal-selection-v1", "approved": True,
        "review_note": "Explicitly reviewed candidate and frozen formal harness; no claim acceptance.",
        "q2_campaign": "development-q2", "q2_source": "source-development-q2",
        "q2_source_manifest_sha256": q2["source_manifest_sha256"], "formal_source": args.source,
        "formal_source_manifest_sha256": formal["source_manifest_sha256"],
        "q2_analysis_sha256": controller.digest(artifact), "candidate": controller.candidate_options(args)}
    write(args.review_receipt, review)
    write(tmp_path / "development-q2-status.json", {"success": True, "stage": "development_review_required"})
    write(tmp_path / "finish-capture32-status.json", {"success": True})
    selection = {"tasks": [{"sequence": i, "instance_id": f"task-{i:02d}", "base": f"sv-{i:02d}"} for i in range(32)]}
    write(tmp_path / "selection/manifest.json", selection)
    write(tmp_path / "normalized-formal/manifest.json", {"fixed": "fixture"})
    monkeypatch.setattr(controller, "analyze", lambda *_a, **_k: copy.deepcopy(analysis))
    monkeypatch.setattr(controller.gate, "read_selection", lambda _: selection)
    monkeypatch.setattr(controller.gate, "captures_ready", lambda *_: (True, []))
    monkeypatch.setattr(controller, "validate_normalized", lambda *_: None)
    monkeypatch.setattr(controller.gate, "ssh", lambda *_a, **_k: pytest.fail("unexpected real remote call"))
    return args, review, analysis


def test_review_is_required_now_not_future_q2_completion(reviewed):
    args, _, _ = reviewed
    args.review_receipt.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        controller.run_controller(args)
    assert not (args.package / "formal-q2-controller.json").exists()


def test_plain_q2_success_receipt_cannot_authorize_formal(reviewed):
    args, _, _ = reviewed
    write(args.review_receipt, {"success": True, "stage": "development_review_required"})
    with pytest.raises(ValueError, match="explicit approved review"):
        controller.validate_review(args)


def test_review_accepts_harness_changes_but_not_candidate_runtime_changes(reviewed):
    args, review, _ = reviewed
    identity = controller.validate_review(args)
    assert identity["source"]["source_manifest_sha256"] != identity["q2_source"]["source_manifest_sha256"]
    source = args.package / args.source
    path = source / "src/caden/policy.py"
    path.write_text("changed candidate")
    provenance_path = source / "SOURCE_PROVENANCE.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["source_sha256"]["src/caden/policy.py"] = controller.digest(path)
    provenance["source_manifest_sha256"] = hashlib.sha256(json.dumps(provenance["source_sha256"], sort_keys=True).encode()).hexdigest()
    write(provenance_path, provenance)
    review["formal_source_manifest_sha256"] = provenance["source_manifest_sha256"]
    write(args.review_receipt, review)
    with pytest.raises(ValueError, match="policy/runtime changed"):
        controller.validate_review(args)


@pytest.mark.parametrize("mutation", ["options", "analysis", "guards", "source"])
def test_review_rejects_drift_or_failed_guards_before_remote_calls(reviewed, mutation):
    args, review, analysis = reviewed
    if mutation == "options":
        args.scheduler_wakes = 2
    elif mutation == "analysis":
        (args.package / "development-q2-analysis/analysis.json").write_text("{}")
    elif mutation == "guards":
        analysis["fidelity_verification"]["performance_guards_pass"] = False
    else:
        (args.package / args.source / "src/caden/policy.py").write_text("drift")
    with pytest.raises(ValueError):
        controller.run_controller(args)
    assert not (args.package / "formal-q2-controller.json").exists()


def test_exact_flags_propagate_and_local_drift_stops(reviewed):
    args, _, _ = reviewed
    argv = controller.suite_argv(args)
    assert argv[argv.index("--kind") + 1] == "formal"
    assert argv[argv.index("--pool-strategy") + 1] == "queue"
    assert argv[argv.index("--restore-mode") + 1] == "thaw-only"
    assert argv[argv.index("--scheduler-wakes") + 1] == "8"
    assert "--first-touch" in argv
    identity = controller.validate_review(args)
    controller.verify_local_unchanged(args, identity)
    (args.package / "normalized-formal/manifest.json").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        controller.verify_local_unchanged(args, identity)


def formal_plan(args, identity):
    ids = [task["instance_id"] for task in identity["selection"]["tasks"]]
    return {"kind": "formal", "tasks": 32, "repetitions": 3, "orders": controller.ORDERS,
        "cold_repetitions": 3, "formal_profile": "full",
        "active_sandboxes": 8, "configs": identity["q2_plan"]["configs"], "pool_strategy": "queue",
        "confirmed_restore_mode": "thaw-only", "scheduler_wake_burst_limit": 8, "post_cold_first_touch": True,
        "wait_scale": 1.0, "relative_p95_p99_guard": 1.10, "turn_latency_absolute_deadline_ms": 180000,
        "selection": {"schema": "crate-formal-cohorts-v1", "manifest_path": "selection.json",
            "manifest_sha256": identity["selection_sha256"], "task_ids": ids, "sequences": list(range(32)),
            "development_task_ids": ids[:8], "heldout_task_ids": ids[8:]}}


def test_formal_plan_requires_all_three_orders_fixed32_and_cohorts(reviewed):
    args, _, _ = reviewed
    identity = controller.validate_review(args)
    plan = formal_plan(args, identity)
    controller.validate_plan(plan, args, identity)
    for change in ({"repetitions": 1}, {"scheduler_wake_burst_limit": 2}, {"relative_p95_p99_guard": 1.2}):
        with pytest.raises(ValueError):
            controller.validate_plan({**plan, **change}, args, identity)
    broken = copy.deepcopy(plan)
    broken["selection"]["heldout_task_ids"] = broken["selection"]["task_ids"]
    with pytest.raises(ValueError, match="split"):
        controller.validate_plan(broken, args, identity)


def test_single_comparison_requires_explicit_review_and_exact_plan(reviewed):
    args, review, _ = reviewed
    args.repetitions, args.formal_profile, args.cold_repetitions = 1, "comparison", 1
    with pytest.raises(ValueError, match="measurement protocol"):
        controller.validate_review(args)
    review["measurement"] = controller.measurement_options(args)
    write(args.review_receipt, review)
    identity = controller.validate_review(args)
    plan = formal_plan(args, identity)
    plan.update(repetitions=1, formal_profile="comparison", cold_repetitions=1,
                orders=[["F0-S0", "T1-S2"]])
    controller.validate_plan(plan, args, identity)
    argv = controller.suite_argv(args)
    assert argv[argv.index("--repetitions") + 1] == "1"
    assert argv[argv.index("--cold-repetitions") + 1] == "1"
    assert argv[argv.index("--formal-profile") + 1] == "comparison"
    for change in ({"repetitions": 3}, {"cold_repetitions": 3}, {"orders": [["F0-S0"]]},
                   {"relative_p95_p99_guard": 1.2}):
        with pytest.raises(ValueError):
            controller.validate_plan({**plan, **change}, args, identity)


def test_existing_campaign_evidence_blocks_all_remote_activity(reviewed):
    args, _, _ = reviewed
    (args.package / args.campaign).mkdir()
    with pytest.raises(FileExistsError):
        controller.run_controller(args)


def test_failed_synced_configuration_preserves_raw_evidence_and_fails_closed(reviewed, monkeypatch):
    args, _, _ = reviewed
    identity = controller.validate_review(args)
    campaign = args.package / args.campaign
    write(campaign / "PLAN.json", formal_plan(args, identity))
    (campaign / "selection.json").write_bytes((args.package / "selection/manifest.json").read_bytes())
    raw = campaign / "replay-r1-T1-S2.json"
    write(raw, {"config": {"requests": 32, "active_sandboxes": 8},
                "requests": [{"instance_id": task["instance_id"]} for task in identity["selection"]["tasks"]]})
    write(campaign / "CONFIGURATION-r1-T1-S2.json", {"repeat": 1, "label": "T1-S2",
          "report_sha256": controller.digest(raw), "success": False})
    monkeypatch.setattr(controller, "compare", lambda *_a, **_k: {
        "fidelity": {"formal_evidence_verified": True}, "deadline_violations": 0})
    with pytest.raises(ValueError, match="per-configuration guard failure"):
        controller.inspect_download(args, identity)
    assert raw.exists() and not (campaign / "COMPLETED.json").exists()


def test_final_cleanup_requires_all_exact_owned_units_and_current_inactivity(reviewed, monkeypatch):
    args, _, _ = reviewed
    identity = controller.validate_review(args)
    units = [f"crate-sv-{i:02d}.service" for i in range(32)]
    cleanup = {"success": True, "owned_units": units, "verified_inactive": units}
    write(args.package / args.campaign / "cleanup.json", cleanup)
    inspected = []
    monkeypatch.setattr(controller.gate, "read_unit", lambda unit: inspected.append(unit) or {"ActiveState": "inactive"})
    controller.validate_cleanup(args, identity)
    assert inspected == units
    monkeypatch.setattr(controller.gate, "read_unit", lambda _: {"ActiveState": "active"})
    with pytest.raises(RuntimeError, match="no longer inactive"):
        controller.validate_cleanup(args, identity)
    cleanup["verified_inactive"] = units[:-1]
    write(args.package / args.campaign / "cleanup.json", cleanup)
    with pytest.raises(ValueError, match="exactly the owned"):
        controller.validate_cleanup(args, identity)


@pytest.mark.parametrize("single", [False, True])
def test_mocked_launch_deploys_after_quiet_syncs_each_configuration_and_strict_analyzes(reviewed, monkeypatch, single):
    args, review, _ = reviewed
    if single:
        args.repetitions, args.formal_profile, args.cold_repetitions = 1, "comparison", 1
        review["measurement"] = controller.measurement_options(args)
        write(args.review_receipt, review)
    orders = controller.formal_orders(args.repetitions, args.formal_profile)
    events, polls = [], iter([{"configurations": {"CONFIGURATION-r0-F0-S0.json": "1"}, "exists": True, "completed": False},
        {"configurations": {f"CONFIGURATION-r{r}-{label}.json": "hash" for r, order in enumerate(orders) for label in order},
         "exists": True, "completed": True}])
    states = iter([{"ActiveState": "active"}, {"ActiveState": "inactive", "Result": "success", "ExecMainStatus": "0"}])
    def ssh(command, **_):
        argv = shlex.split(command)
        events.append(("ssh", argv))
        payload = json.dumps(next(polls)) if controller.PROBE in argv else ""
        return subprocess.CompletedProcess(argv, 0, payload, "")
    monkeypatch.setattr(controller.gate, "ssh", ssh)
    monkeypatch.setattr(controller.gate, "run", lambda argv: events.append(("run", list(map(str, argv)))))
    monkeypatch.setattr(controller.gate, "require_remote_quiet", lambda: events.append(("quiet", [])))
    monkeypatch.setattr(controller.gate, "require_remote_fresh", lambda path: events.append(("fresh", [path])))
    monkeypatch.setattr(controller.gate, "verify_remote_source", lambda *_: events.append(("sourceverified", [])))
    monkeypatch.setattr(controller, "verify_remote_inputs", lambda *_: events.append(("inputsverified", [])))
    monkeypatch.setattr(controller.gate, "exec_argv", lambda _: controller.suite_argv(args))
    monkeypatch.setattr(controller.gate, "read_unit", lambda _: next(states))
    monkeypatch.setattr(controller, "sync_campaign", lambda _: events.append(("sync", [])))
    monkeypatch.setattr(controller, "inspect_download", lambda *_: {"standalone": {}, "pairs": {}})
    monkeypatch.setattr(controller, "validate_cleanup", lambda *_: events.append(("cleanupverified", [])))
    monkeypatch.setattr(controller.time, "sleep", lambda _: None)
    controller.run_controller(args)
    quiet = [i for i, (kind, _) in enumerate(events) if kind == "quiet"]
    upload = next(i for i, (kind, argv) in enumerate(events) if kind == "run" and argv[0] == "rsync")
    launch = next(i for i, (kind, argv) in enumerate(events) if kind == "ssh" and "systemd-run" in argv)
    assert quiet[0] < upload < quiet[1] < launch
    assert sum(kind == "sync" for kind, _ in events) == 2
    analysis = events[-1][1]
    assert "--workloads-dir" in analysis and "--source-root" in analysis
    assert str(args.package / args.source / "experiments/swebench_verified/analyze.py") in analysis
    assert not any("capture_batch.py" in str(argv) or "convert.py" in str(argv) for _, argv in events)
    status = json.loads((args.package / "formal-q2-status.json").read_text())
    assert status["stage"] == "formal_review_required" and status["success"]
