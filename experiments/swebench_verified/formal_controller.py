#!/usr/bin/env python3
"""Explicitly reviewed formal launch; never recapture, retune, overwrite or retry.

The separate local review receipt must already exist. Merely completing Q2 is
not acceptance. All SSH interactions are campaign-scoped; no swap/CXL changes.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

if not __package__:
    sys.dont_write_bytecode = True
if __package__:
    from . import development_controller as gate
    from .analyze import analyze
    from .checkpoints import compare
    from .finish_capture32 import digest, tree_hashes, validate_normalized
    from .run_suite import FORMAL_ORDERS, formal_orders
else:
    import development_controller as gate
    from analyze import analyze
    from checkpoints import compare
    from finish_capture32 import digest, tree_hashes, validate_normalized
    from run_suite import FORMAL_ORDERS, formal_orders

REMOTE = gate.REMOTE
ORDERS = FORMAL_ORDERS  # Historical default remains unchanged.
RUNTIME_PREFIXES = ("src/", "experiments/trajectory_replay/", "experiments/sandboxfs_memory/")
RUNTIME_FILES = {"experiments/swebench_verified/routing.py", "experiments/swebench_verified/capture.py"}


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--package", type=Path, required=True)
    ap.add_argument("--source", required=True, help="fresh frozen formal harness source")
    ap.add_argument("--campaign", required=True, help="fresh formal result label")
    ap.add_argument("--review-receipt", type=Path, required=True,
                    help="already-existing explicit reviewed candidate selection; never waits for approval")
    ap.add_argument("--pool-strategy", choices=["legacy", "queue"], required=True)
    ap.add_argument("--restore-mode", choices=["prewarm", "thaw-only"], required=True)
    ap.add_argument("--scheduler-wakes", type=int, choices=[2, 8], required=True)
    ap.add_argument("--first-touch", action="store_true")
    ap.add_argument("--repetitions", type=int, choices=[1, 3], default=3)
    ap.add_argument("--formal-profile", choices=["full", "comparison"], default="full")
    ap.add_argument("--cold-repetitions", type=int, choices=[1, 3], default=3)
    return ap


def candidate_options(a):
    return {"pool_strategy": a.pool_strategy, "restore_mode": a.restore_mode,
            "scheduler_wakes": a.scheduler_wakes, "first_touch": a.first_touch}


def measurement_options(a):
    formal_orders(a.repetitions, a.formal_profile)
    return {"repetitions": a.repetitions, "formal_profile": a.formal_profile,
            "cold_repetitions": a.cold_repetitions}


def safe_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
        raise ValueError("unsafe artifact name")
    return value


def validate_review(a):
    """Read-only local qualification. No launch follows a missing/failed review."""
    p = a.package.resolve()
    safe_name(a.source)
    safe_name(a.campaign)
    if a.source == a.campaign:
        raise ValueError("source and campaign directories must differ")
    review_hash = digest(a.review_receipt)
    review = json.loads(a.review_receipt.read_text())
    if (review.get("schema") != "crate-reviewed-formal-selection-v1" or review.get("approved") is not True or
            not isinstance(review.get("review_note"), str) or not review["review_note"].strip()):
        raise ValueError("requires an explicit approved review with a review note, not a Q2 completion receipt")
    if review.get("candidate") != candidate_options(a):
        raise ValueError("selected candidate CLI differs from explicit review")
    historical = {"repetitions": 3, "formal_profile": "full", "cold_repetitions": 3}
    if review.get("measurement", historical) != measurement_options(a):
        raise ValueError("selected measurement protocol differs from explicit review")
    q2 = safe_name(review["q2_campaign"])
    q2_source = safe_name(review["q2_source"])
    if q2 in {a.source, a.campaign} or q2_source in {a.source, a.campaign}:
        raise ValueError("formal source/results must not overwrite reviewed development evidence")
    status = json.loads((p / (q2 + "-status.json")).read_text())
    if status.get("success") is not True or status.get("stage") != "development_review_required":
        raise ValueError("reviewed development campaign did not finish cleanly")
    identity = gate.verify_local_source(p / a.source)
    q2_identity = gate.verify_local_source(p / q2_source)
    if (review.get("formal_source") != a.source or
            review.get("formal_source_manifest_sha256") != identity["source_manifest_sha256"] or
            review.get("q2_source_manifest_sha256") != q2_identity["source_manifest_sha256"]):
        raise ValueError("review does not bind the exact formal and development frozen source hashes")
    def runtime(files):
        return {name: value for name, value in files.items()
                if name.startswith(RUNTIME_PREFIXES) or name in RUNTIME_FILES}
    old, new = runtime(q2_identity["files"]), runtime(identity["files"])
    if (not old or old != new or not all(any(name.startswith(prefix) for name in old) for prefix in RUNTIME_PREFIXES)
            or not RUNTIME_FILES <= set(old)):
        raise ValueError("selected policy/runtime changed since reviewed development; requires new development review")
    artifact = p / (q2 + "-analysis") / "analysis.json"
    if digest(artifact) != review.get("q2_analysis_sha256"):
        raise ValueError("reviewed development analysis hash changed")
    saved = json.loads(artifact.read_text())
    checked = analyze(p / q2, workloads_dir=p / "normalized-development", source_root=p / q2_source)
    for field in ("systems", "cold", "tool_vs_fullcopy", "raw_sha256", "workload_manifest_sha256"):
        if saved[field] != checked[field]:
            raise ValueError("reviewed development analysis no longer matches raw evidence: " + field)
    fidelity = checked["fidelity_verification"]
    if (fidelity.get("formal_evidence_verified") is not True or not fidelity.get("performance_guards_pass") or
            any(row["baseline_deadline_violations"] for row in fidelity["checks"].values())):
        raise ValueError("reviewed development candidate failed unchanged performance/fidelity guards")
    plan = checked["plan"]
    if (plan["kind"] != "development" or plan["tasks"] != 8 or
            plan["orders"] != [["F0-S0", "T1-S2"]] or plan.get("development_profile") != "candidate" or
            plan.get("pool_strategy") != a.pool_strategy or plan.get("confirmed_restore_mode") != a.restore_mode or
            plan.get("scheduler_wake_burst_limit") != a.scheduler_wakes or plan.get("post_cold_first_touch") != a.first_touch):
        raise ValueError("review does not select the actually measured development candidate")
    selection = gate.read_selection(p)
    if not gate.receipt_ready(p / "finish-capture32-status.json") or not gate.captures_ready(p, selection, 32)[0]:
        raise ValueError("all 32 captures must already be validated")
    validate_normalized(p, selection)
    return {"review_sha256": review_hash, "review": review, "source": identity, "q2_source": q2_identity,
            "q2_plan": plan, "selection": selection, "selection_sha256": digest(p / "selection/manifest.json"),
            "workloads": tree_hashes(p / "normalized-formal")}


def suite_argv(a):
    argv = ["/usr/bin/python3", REMOTE + "/" + a.source + "/experiments/swebench_verified/run_suite.py",
            "--kind", "formal", "--output", REMOTE + "/" + a.campaign,
            "--pool-strategy", a.pool_strategy, "--restore-mode", a.restore_mode,
            "--scheduler-wakes", str(a.scheduler_wakes), "--repetitions", str(a.repetitions),
            "--formal-profile", a.formal_profile, "--cold-repetitions", str(a.cold_repetitions)]
    if a.first_touch:
        argv.append("--first-touch")
    return argv


def verify_remote_inputs(identity):
    gate.verify_remote_source(REMOTE + "/normalized-formal", {"files": identity["workloads"]})
    code = "import hashlib,sys;from pathlib import Path; p=Path(sys.argv[1]); assert not p.is_symlink(); assert hashlib.sha256(p.read_bytes()).hexdigest()==sys.argv[2]"
    gate.ssh(shlex.join(["python3", "-c", code, REMOTE + "/selection/manifest.json", identity["selection_sha256"]]))


def verify_local_unchanged(a, identity):
    p = a.package.resolve()
    if (digest(a.review_receipt) != identity["review_sha256"] or
            gate.verify_local_source(p / a.source) != identity["source"] or
            digest(p / "selection/manifest.json") != identity["selection_sha256"] or
            tree_hashes(p / "normalized-formal") != identity["workloads"]):
        raise ValueError("approved source, review or workload changed; no automatic continuation")


def validate_plan(plan, a, identity):
    if (plan.get("kind") != "formal" or plan.get("tasks") != 32 or plan.get("repetitions") != a.repetitions or
            plan.get("orders") != formal_orders(a.repetitions, a.formal_profile) or plan.get("active_sandboxes") != 8 or
            plan.get("formal_profile", "full") != a.formal_profile or plan.get("cold_repetitions") != a.cold_repetitions or
            plan.get("configs") != identity["q2_plan"]["configs"] or
            plan.get("pool_strategy") != a.pool_strategy or plan.get("confirmed_restore_mode") != a.restore_mode or
            plan.get("scheduler_wake_burst_limit") != a.scheduler_wakes or plan.get("post_cold_first_touch") != a.first_touch or
            plan.get("wait_scale") != 1 or plan.get("relative_p95_p99_guard") != 1.10 or
            plan.get("turn_latency_absolute_deadline_ms") != 180000):
        raise ValueError("formal PLAN differs from reviewed candidate/registered guards")
    tasks = identity["selection"]["tasks"]
    ids = [task["instance_id"] for task in tasks]
    expected = {"schema": "crate-formal-cohorts-v1", "manifest_path": "selection.json",
                "manifest_sha256": identity["selection_sha256"], "task_ids": ids,
                "sequences": list(range(32)), "development_task_ids": ids[:8], "heldout_task_ids": ids[8:]}
    if plan.get("selection") != expected:
        raise ValueError("formal PLAN did not freeze the registered development/heldout split")


PROBE = r'''
import hashlib,json,sys
from pathlib import Path
p=Path(sys.argv[1]); files={}
if p.is_symlink(): raise ValueError("campaign symlink")
for f in p.glob("CONFIGURATION-*.json"):
    if f.is_symlink(): raise ValueError("configuration receipt symlink")
    files[f.name]=hashlib.sha256(f.read_bytes()).hexdigest()
print(json.dumps({"configurations":files,"completed":(p/"COMPLETED.json").is_file(),"exists":p.is_dir()}))
'''


def sync_campaign(a):
    gate.run(["rsync", "-a", "--protect-args", "--timeout=30", "-e",
              "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=1",
              "nsl17:" + REMOTE + "/" + a.campaign + "/", str(a.package.resolve() / a.campaign) + "/"])


def inspect_download(a, identity):
    p, result = a.package.resolve(), {"standalone": {}, "pairs": {}}
    campaign = p / a.campaign
    plan = json.loads((campaign / "PLAN.json").read_text())
    validate_plan(plan, a, identity)
    if digest(campaign / "selection.json") != identity["selection_sha256"]:
        raise ValueError("synced selection bytes changed")
    orders = formal_orders(a.repetitions, a.formal_profile)
    expected = {(rep, label) for rep, order in enumerate(orders) for label in order}
    for path in sorted(campaign.glob("CONFIGURATION-*.json")):
        receipt = json.loads(path.read_text())
        rep, label = receipt["repeat"], receipt["label"]
        if (rep, label) not in expected or path.name != f"CONFIGURATION-r{rep}-{label}.json":
            raise ValueError("unexpected configuration receipt")
        report_path = campaign / f"replay-r{rep}-{label}.json"
        if digest(report_path) != receipt["report_sha256"]:
            raise ValueError("per-configuration raw report checksum changed")
        report = json.loads(report_path.read_text())
        if (report["config"]["requests"] != 32 or report["config"]["active_sandboxes"] != 8 or
                [request["instance_id"] for request in report["requests"]] != plan["selection"]["task_ids"]):
            raise ValueError("synced report differs from registered formal task population")
        own = compare(report, report, workloads_dir=p / "normalized-formal", source_root=p / a.source)
        result["standalone"][f"r{rep}-{label}"] = own
        if receipt.get("success") is not True or not own["fidelity"]["formal_evidence_verified"] or own["deadline_violations"]:
            raise ValueError("per-configuration guard failure; remote reports retained")
    for rep, order in enumerate(orders):
        baseline_path = campaign / f"replay-r{rep}-F0-S0.json"
        if f"r{rep}-F0-S0" not in result["standalone"]:
            continue
        baseline = json.loads(baseline_path.read_text())
        for label in order:
            if label == "F0-S0" or f"r{rep}-{label}" not in result["standalone"]:
                continue
            paired = compare(baseline, json.loads((campaign / f"replay-r{rep}-{label}.json").read_text()),
                             workloads_dir=p / "normalized-formal", source_root=p / a.source)
            result["pairs"][f"r{rep}-{label}"] = paired
            if not paired["guards_pass"] or paired["baseline_deadline_violations"]:
                raise ValueError("same-repetition paired guard failure; remote reports retained")
    return result


def validate_cleanup(a, identity):
    cleanup = json.loads((a.package.resolve() / a.campaign / "cleanup.json").read_text())
    units = [f"crate-sv-{task['sequence']:02d}.service" for task in identity["selection"]["tasks"]]
    if (cleanup.get("success") is not True or cleanup.get("owned_units") != units or
            cleanup.get("verified_inactive") != units):
        raise ValueError("formal cleanup was not verified for exactly the owned services")
    for unit in units:
        if gate.read_unit(unit)["ActiveState"] != "inactive":
            raise RuntimeError("owned service no longer inactive: " + unit)


def run_controller(a):
    # Approval is required NOW; this controller never waits and self-launches on
    # an ordinary Q2 completion status or an absent future review file.
    identity = validate_review(a)
    p = a.package.resolve()
    for suffix in ("", "-analysis", "-status.json", "-status.tmp", "-controller.json", "-monitor.json"):
        target = p / (a.campaign + suffix)
        if target.exists() or target.is_symlink():
            raise FileExistsError("refusing existing formal evidence: " + str(target))
    with (p / (a.campaign + "-controller.json")).open("x") as output:
        json.dump({"pid": os.getpid(), "started_unix": time.time(), "argv": sys.argv,
                   "review_sha256": identity["review_sha256"], "candidate": candidate_options(a),
                   "measurement": measurement_options(a)}, output, indent=2)
    def status(stage, **fields):
        value = {"stage": stage, "observed_unix": time.time(), **fields}
        path = p / (a.campaign + "-status.json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n")
        temporary.replace(path)
        print(json.dumps(value), flush=True)
    try:
        status("review_verified", review_sha256=identity["review_sha256"], quiet=gate.require_remote_quiet())
        remote_source = REMOTE + "/" + a.source
        gate.require_remote_fresh(REMOTE + "/" + a.campaign)
        gate.require_remote_fresh(remote_source)
        verify_remote_inputs(identity)
        verify_local_unchanged(a, identity)
        gate.ssh(shlex.join(["mkdir", "--", remote_source]))
        gate.run(["rsync", "-a", "--protect-args", "--timeout=30", "--exclude=__pycache__/", "-e",
                  "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=1",
                  str(p / a.source) + "/", "nsl17:" + remote_source + "/"])
        verify_local_unchanged(a, identity)
        gate.verify_remote_source(remote_source, identity["source"])
        status("before_measurement", quiet=gate.require_remote_quiet())
        unit = "crate-sv-" + a.campaign + ".service"
        command = ["sudo", "-n", "systemd-run", "--unit=" + unit, "--property=Type=exec",
                   "--property=AllowedCPUs=0-7", "--property=StandardOutput=append:" + REMOTE + "/logs/" + a.campaign + ".log",
                   "--property=StandardError=append:" + REMOTE + "/logs/" + a.campaign + ".log", *suite_argv(a)]
        gate.ssh(shlex.join(command))
        if gate.exec_argv(unit) != suite_argv(a):
            raise RuntimeError("formal service command differs from reviewed launch")
        seen = {}
        while True:
            verify_local_unchanged(a, identity)
            progress = json.loads(gate.ssh(shlex.join(["python3", "-c", PROBE, REMOTE + "/" + a.campaign])).stdout)
            state = gate.read_unit(unit)
            if progress["configurations"] != seen or state["ActiveState"] not in gate.ACTIVE_STATES or progress["completed"]:
                if progress["exists"]:
                    sync_campaign(a)
                    checks = inspect_download(a, identity)
                    (p / (a.campaign + "-monitor.json")).write_text(json.dumps(checks, indent=2) + "\n")
                seen = progress["configurations"]
            status("formal_running", service=state, completed_configurations=sorted(seen))
            if state["ActiveState"] not in gate.ACTIVE_STATES:
                if (state["ActiveState"] != "inactive" or state.get("Result") != "success" or
                        state.get("ExecMainStatus") != "0" or not progress["completed"] or
                        len(seen) != sum(map(len, formal_orders(a.repetitions, a.formal_profile)))):
                    raise RuntimeError("formal service stopped without all successful configuration receipts and completion")
                break
            time.sleep(30)
        validate_cleanup(a, identity)
        gate.run([sys.executable, p / a.source / "experiments/swebench_verified/analyze.py",
                  "--campaign", p / a.campaign, "--output", p / (a.campaign + "-analysis"),
                  "--workloads-dir", p / "normalized-formal", "--source-root", p / a.source])
        status("formal_review_required", success=True,
               note="Raw reports and all guard outcomes retained; no automatic retries, retuning or paper claims.")
    except BaseException as error:
        status("failed_review_required", success=False, error=f"{type(error).__name__}: {error}",
               note="No automatic remote kill, restart, overwrite or retry; inspect the exact owned unit and retained reports.")
        raise


def main():
    run_controller(parser().parse_args())


if __name__ == "__main__":
    main()
