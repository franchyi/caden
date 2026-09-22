#!/usr/bin/env python3
"""Fail-closed matched campaign, on nsl17 only; original waits, no model calls."""
import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
C = Path("/sandboxfs/crate-swebench-20260919")
FORMAL_ORDERS = [["F0-S0", "T1-S0", "T1-S1", "T1-S2"],
                 ["T1-S2", "T1-S1", "T1-S0", "F0-S0"],
                 ["T1-S1", "F0-S0", "T1-S2", "T1-S0"]]
if __package__:
    from .checkpoints import compare
else:
    from checkpoints import compare


def formal_orders(repetitions=3, profile="full"):
    if repetitions not in (1, 3) or profile not in {"full", "comparison"}:
        raise ValueError("invalid formal measurement protocol")
    if profile == "comparison":
        if repetitions != 1:
            raise ValueError("comparison profile is an explicitly single-run descriptive pair")
        return [["F0-S0", "T1-S2"]]
    return [list(order) for order in FORMAL_ORDERS[:repetitions]]


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def verify_source():
    p = json.loads((ROOT / "SOURCE_PROVENANCE.json").read_text())
    if not p["source_sha256"] or hashlib.sha256(json.dumps(p["source_sha256"], sort_keys=True).encode()).hexdigest() != p["source_manifest_sha256"]:
        raise RuntimeError("invalid frozen source manifest")
    for relative, expected in p["source_sha256"].items():
        path = (ROOT / relative).resolve()
        if ROOT.resolve() not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"source drift: {relative}")


def checkpoint_configuration(output, rep, label, order, kind):
    """Immediately validate every run; only same-repetition F0 supplies tails."""
    stem = f"r{rep}-{label}"
    report_path = output / ("replay-" + stem + ".json")
    receipt = {"repeat": rep, "label": label, "success": False, "paired_checkpoints": []}
    standalone = {"success": False, "relative_guard_status":
                  "baseline-only" if label == "F0-S0" else "deferred-until-same-repetition-baseline"}
    try:
        result = json.loads(report_path.read_text())
        receipt["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
        if (output / "PLAN.json").exists():
            plan = json.loads((output / "PLAN.json").read_text())
            if (result["config"]["requests"] != plan["tasks"] or
                    result["config"]["active_sandboxes"] != plan["active_sandboxes"]):
                raise RuntimeError("report does not match registered task population/concurrency")
            if "selection" in plan and [request["instance_id"] for request in result["requests"]] != plan["selection"]["task_ids"]:
                raise RuntimeError("report does not match frozen formal selection order")
        own = compare(result, result)
        standalone.update(fidelity=own["fidelity"], matched_tools=own["matched_tools"],
                          deadline_violations=own["deadline_violations"])
        if not own["fidelity"]["formal_evidence_verified"]:
            raise RuntimeError("standalone checkpoint lacks verified source/workload evidence")
        # The first baseline anchors source/workload identity across all repetitions.
        # Ratios against that anchor are never used as paired performance evidence.
        anchor = output / "replay-r0-F0-S0.json"
        if anchor.exists():
            compare(json.loads(anchor.read_text()), result)
            standalone["campaign_identity_verified"] = True
        if kind == "formal" and own["deadline_violations"]:
            raise RuntimeError("standalone checkpoint failed absolute deadline; preserve results")
        standalone["success"] = True
        write_json(output / ("standalone-" + stem + ".json"), standalone)
        baseline = output / f"replay-r{rep}-F0-S0.json"
        if baseline.exists():
            for completed_label in order:
                candidate = output / f"replay-r{rep}-{completed_label}.json"
                if completed_label == "F0-S0" or not candidate.exists():
                    continue
                checkpoint = compare(json.loads(baseline.read_text()), json.loads(candidate.read_text()))
                name = f"checkpoint-r{rep}-{completed_label}.json"
                write_json(output / name, checkpoint)
                receipt["paired_checkpoints"].append(name)
                print("CHECKPOINT", rep, completed_label, json.dumps(checkpoint), flush=True)
                if not checkpoint["fidelity"]["formal_evidence_verified"]:
                    raise RuntimeError("checkpoint lacks verified source/workload evidence")
                if kind == "formal" and (not checkpoint["guards_pass"] or checkpoint["baseline_deadline_violations"]):
                    raise RuntimeError("matched checkpoint failed guards; preserve results and review before continuing")
            if label != "F0-S0":
                standalone["relative_guard_status"] = "same-repetition-pair-checked"
        receipt["success"] = True
    except Exception as error:
        receipt["error"] = f"{type(error).__name__}: {error}"
        if not standalone["success"]:
            standalone["error"] = receipt["error"]
        raise
    finally:
        write_json(output / ("standalone-" + stem + ".json"), standalone)
        receipt["standalone"] = "standalone-" + stem + ".json"
        # Last atomic file is the controller's per-configuration sync trigger,
        # including a failed guard. Reports/checkpoints are never removed.
        write_json(output / ("CONFIGURATION-" + stem + ".json"), receipt)


def cleanup_owned_services(tasks, output):
    cleanup = {"owned_units": [], "verified_inactive": [], "success": False,
               "retained": "All prepared bases, source snapshots and result files are retained."}
    try:
        for task in tasks:
            index = f"{task['sequence']:02d}"
            unit = f"crate-sv-{index}.service"
            raw = subprocess.check_output(["systemctl", "show", unit, "--property=ExecStart", "--value"], text=True).strip()
            match = re.fullmatch(r"\{\s*path=([^\s;]+)\s*;\s*argv\[\]=(.*?)\s*;[^{}]*\}", raw, flags=re.DOTALL)
            argv = shlex.split(match.group(2)) if match and raw.count("argv[]=") == 1 else []
            if (not argv or match.group(1) != argv[0] or argv[0] not in {"/bin/bash", "/usr/bin/bash"} or
                    argv[1:] != [str(C / "scripts/launch-daemon.sh"), index]):
                raise RuntimeError(f"unrecognized exact service command; not stopping {unit}")
            if task["socket"] != f"/run/crate-sv-{index}.sock":
                raise RuntimeError(f"unexpected task socket; not stopping {unit}")
            active = json.loads(subprocess.check_output(
                [str(C / "bin/sandboxfsctl"), "--socket", task["socket"], "list"], text=True))["sandboxes"]
            if active:
                raise RuntimeError(f"sandboxes remain; not stopping {unit}: {active}")
            cleanup["owned_units"].append(unit)
        if not cleanup["owned_units"] or len(set(cleanup["owned_units"])) != len(tasks):
            raise RuntimeError("invalid cleanup unit population")
        subprocess.run(["systemctl", "stop", *cleanup["owned_units"]], check=True)
        for unit in cleanup["owned_units"]:
            state = subprocess.check_output(["systemctl", "show", unit, "--property=ActiveState", "--value"], text=True).strip()
            if state != "inactive":
                raise RuntimeError(f"owned service not verified inactive: {unit}: {state}")
            cleanup["verified_inactive"].append(unit)
        cleanup["success"] = True
    except Exception as error:
        cleanup["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        write_json(output / "cleanup.json", cleanup)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["pilot", "development", "formal"], required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--pool-strategy", choices=["legacy", "queue", "ablation"], default="legacy")
    ap.add_argument("--restore-mode", choices=["prewarm", "thaw-only"], default="prewarm")
    ap.add_argument("--scheduler-wakes", type=int, choices=[2, 8], default=2,
                    help="scheduler burst gate; movement concurrency remains separately bounded")
    ap.add_argument("--first-touch", action="store_true",
                    help="separate real-file read/write probes after the unchanged cold-ready endpoint")
    ap.add_argument("--development-profile", choices=["full", "candidate"], default="full",
                    help="candidate runs a fresh FullCopy/T1-S2 pair; development only")
    ap.add_argument("--repetitions", type=int, choices=[1, 3],
                    help="explicit formal repetition count; old default is three")
    ap.add_argument("--formal-profile", choices=["full", "comparison"], default="full",
                    help="comparison is one descriptive FullCopy/T1-S2 pair, without treatment ablations")
    ap.add_argument("--cold-repetitions", type=int, choices=[1, 3], default=3)
    a = ap.parse_args()
    if a.pool_strategy == "ablation" and a.kind != "development":
        raise SystemExit("pool ablation is a development-only diagnostic")
    if a.development_profile == "candidate" and (a.kind != "development" or a.pool_strategy == "ablation"):
        raise SystemExit("candidate profile requires development and a selected pool strategy")
    if a.kind != "formal" and (a.formal_profile != "full" or a.repetitions not in (None, 1)):
        raise SystemExit("formal profile/repetition options cannot change pilot/development population")
    if a.kind == "formal":
        formal_orders(a.repetitions or 3, a.formal_profile)
    if os.geteuid() != 0 or a.output.exists() or a.output.is_symlink():
        raise SystemExit("requires root and fresh output directory")
    verify_source()
    limit, repetitions, active = {"pilot": (4, 1, 4), "development": (8, 1, 8),
                                  "formal": (32, 3, 8)}[a.kind]
    if a.repetitions is not None:
        repetitions = a.repetitions
    for unit in ("crate-sv-prepare32.service", "crate-sv-isolation32.service"):
        state = subprocess.check_output(["systemctl", "show", unit, "-p", "ActiveState", "--value"], text=True).strip()
        if state in {"active", "activating", "deactivating"}:
            raise RuntimeError(f"own infrastructure work overlaps measurement: {unit}: {state}")
    selection_bytes = (C / "selection/manifest.json").read_bytes()
    selection_tasks = json.loads(selection_bytes)["tasks"]
    if (len(selection_tasks) != 32 or [task["sequence"] for task in selection_tasks] != list(range(32)) or
            len({task["instance_id"] for task in selection_tasks}) != 32):
        raise RuntimeError("expected fixed ordered unique 32-task selection")
    tasks = selection_tasks[:limit]
    workloads_root = C / ("normalized-" + a.kind)
    workload_manifest = json.loads((workloads_root / "manifest.json").read_text())
    if len(workload_manifest["workloads"]) != limit:
        raise RuntimeError("normalized workload does not contain the registered task population")
    for task, entry in zip(tasks, workload_manifest["workloads"]):
        path = (workloads_root / entry["path"]).resolve()
        if workloads_root.resolve() not in path.parents:
            raise RuntimeError("unsafe normalized task path")
        payload = path.read_bytes()
        workload = json.loads(payload)
        if (hashlib.sha256(payload).hexdigest() != entry["sha256"] or
                workload["source"]["instance_id"] != task["instance_id"] or workload["base"] != task["base"]):
            raise RuntimeError("normalized task differs from ordered selection")
    for task in tasks:
        receipt = C / "artifacts" / f"prepare-{task['sequence']:02d}.json"
        if not receipt.exists():
            raise RuntimeError(f"missing prepared environment: {task['instance_id']}")
        listed = subprocess.check_output([str(C / "bin/sandboxfsctl"), "--socket", task["socket"], "list"], text=True)
        if json.loads(listed)["sandboxes"]:
            raise RuntimeError(f"task still has active sandboxes: {task['instance_id']}")
    a.output.mkdir(parents=True)
    sockets = a.output / "sockets.json"
    sockets.write_text(json.dumps({t["base"]: t["socket"] for t in tasks}, indent=2) + "\n")
    configs = {
        "F0-S0": ["--mode", "baseline", "--policy", "static", "--constant-cpu", "--max-admissions", str(active), "--max-wakes", str(active)],
        "T1-S0": ["--mode", "t1", "--policy", "static", "--constant-cpu", "--max-admissions", str(active), "--max-wakes", str(active)],
        "T1-S1": ["--mode", "t1", "--policy", "static", "--pool-target", "1", "--pool-max", "1", "--max-admissions", "4", "--max-wakes", "2"],
        "T1-S2": ["--mode", "t1", "--policy", "request-aware", "--pool-target", "1", "--pool-max", "1", "--max-admissions", "4", "--max-wakes", "2",
                  "--speculative-restore", "--allow-process-madvise-restore", "--speculative-madvise-mib", "256",
                  "--hot-reserve-mib", "32", "--minimum-cold-ms", "100", "--max-early-wake-probability", "0.20",
                  "--confirmed-movement-reserve", "1", "--wake-reserve-safety-mib", "256"]}
    configs["T1-S2"] += ["--restore-mode", a.restore_mode]
    for label in ("T1-S1", "T1-S2"):
        configs[label][configs[label].index("--max-wakes") + 1] = str(a.scheduler_wakes)
    if a.pool_strategy == "queue":
        for label in ("T1-S1", "T1-S2"):
            configs[label] = configs[label] + ["--queue-aware-pool"]
    elif a.pool_strategy == "ablation":
        for label in ("T1-S1", "T1-S2"):
            configs[label + "-Q"] = configs[label] + ["--queue-aware-pool"]
    orders = [["F0-S0", "T1-S2"]] if a.kind == "pilot" else formal_orders(repetitions, a.formal_profile)
    if a.kind == "development":
        orders = [["F0-S0", "T1-S2", "T1-S0", "T1-S1"]]
        if a.pool_strategy == "ablation":
            orders[0] += ["T1-S2-Q", "T1-S1-Q"]
        if a.development_profile == "candidate":
            orders = [["F0-S0", "T1-S2"]]
    plan = {"kind": a.kind, "tasks": limit, "active_sandboxes": active,
            "repetitions": repetitions, "orders": orders, "configs": configs,
            "pool_strategy": a.pool_strategy,
            "confirmed_restore_mode": a.restore_mode,
            "scheduler_wake_burst_limit": a.scheduler_wakes,
            "post_cold_first_touch": a.first_touch,
            "development_profile": a.development_profile,
            "wait_scale": 1.0, "cold_repetitions": a.cold_repetitions,
            "formal_profile": a.formal_profile,
            "measurement_limitation": "Single ordered repetition is descriptive, not repeatability, isolated policy causality or density evidence."
                if repetitions == 1 else "Repeated shared-host campaign; not independent host trials.",
            "arrival_model": "fixed queue available at time zero; bounded closed-loop waves, not an open-loop arrival-rate experiment",
            "wait_model": "fixed measured per-task LLM-query durations after preceding commands; absolute response times can shift with command execution",
            "turn_latency_absolute_deadline_ms": 180000,
            "relative_p95_p99_guard": 1.10,
            "no_density_at_slo_claim": True, "registered_unix": time.time()}
    if a.kind == "formal":
        (a.output / "selection.json").write_bytes(selection_bytes)
        identities = [task["instance_id"] for task in tasks]
        plan["selection"] = {"schema": "crate-formal-cohorts-v1", "manifest_path": "selection.json",
            "manifest_sha256": hashlib.sha256(selection_bytes).hexdigest(), "task_ids": identities,
            "sequences": [task["sequence"] for task in tasks],
            "development_task_ids": identities[:8], "heldout_task_ids": identities[8:]}
    (a.output / "PLAN.json").write_text(json.dumps(plan, indent=2) + "\n")
    for rep, order in enumerate(orders):
        for label in order:
            verify_source()
            report = a.output / f"replay-r{rep}-{label}.json"
            argv = [sys.executable, str(ROOT / "experiments/trajectory_replay/run_campaign.py"),
                "--workloads-dir", str(C / ("normalized-" + a.kind)), "--base", tasks[0]["base"],
                "--base-socket-map", str(sockets), "--source-provenance", str(ROOT / "SOURCE_PROVENANCE.json"),
                "--socket", tasks[0]["socket"], "--ctl", str(C / "bin/sandboxfsctl"),
                "--requests", str(limit), "--active-sandboxes", str(active), "--wait-scale", "1",
                "--sample-ms", "100", "--estimated-wss-mib", "256", "--dram-reserve-mib", "2048",
                "--turn-slo-ms", "180000", "--turn-p99-slo-ms", "180000", "--output", str(report), *configs[label]]
            print("START", rep, label, flush=True)
            with (a.output / f"replay-r{rep}-{label}.log").open("w") as log:
                completed = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT)
            if not report.exists():
                raise RuntimeError(f"runner failed: {rep} {label}")
            result = json.loads(report.read_text())
            checkpoint_configuration(a.output, rep, label, order, a.kind)
            if completed.returncode:
                raise RuntimeError(f"runner exited unsuccessfully: {rep} {label}")
            print("DONE", rep, label, json.dumps(result["summary"]), flush=True)
    cold = [sys.executable, str(ROOT / "experiments/swebench_verified/run_cold.py"),
            "--selection", str(C / "selection/manifest.json"), "--limit", str(limit),
            "--repeats", str(a.cold_repetitions), "--output", str(a.output / "cold")]
    if a.first_touch:
        cold += ["--first-touch"]
    subprocess.run(cold, check=True)
    if a.kind == "formal":
        cleanup_owned_services(tasks, a.output)
    (a.output / "COMPLETED.json").write_text(json.dumps({"finished_unix": time.time(), "kind": a.kind}) + "\n")


if __name__ == "__main__":
    main()
