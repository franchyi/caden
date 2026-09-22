#!/usr/bin/env python3
"""Read-only, partial-campaign diagnostic from completed replay JSON artifacts.

This intentionally does not require COMPLETED.json: every individual replay must
be complete and faithful, but the campaign can still be running. It never
launches workloads, changes thresholds, or accepts a formal performance claim.
"""
import argparse
import collections
import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path

ACTIVE = {"pool_prepare", "create", "trace_replay"}
COMPONENTS = ("wake_restore_ns", "server_command_ns", "rpc_overhead_ns",
              "result_validation_ns", "result_pack_ns")
MEMORY = ("task_service_cgroup_sum_bytes", "task_service_anon_bytes",
          "task_service_file_bytes", "task_service_kernel_bytes",
          "task_daemon_cgroup_sum_bytes", "sandbox_memory_current_bytes",
          "sandbox_memory_swap_bytes", "sandbox_memory_compressed_bytes",
          "signed_host_physical_delta_bytes", "signed_host_available_delta_bytes")
FEATURES = ("mode", "policy", "constant_cpu", "reclaim_tier", "reclaim_mode",
            "hot_reserve_mib", "speculative_restore", "allow_process_madvise_restore",
            "speculative_restore_probability", "prediction_interval_ms",
            "allow_zswap_compression", "compression_max_wait_ms", "zswap_max_mib",
            "pool_target", "pool_max", "queue_aware_pool", "max_admissions",
            "max_wakes", "wait_scale", "active_sandboxes", "drop_caches")


def distribution(values, scale=1.0):
    data = sorted(value / scale for value in values)
    if not data:
        raise ValueError("empty measurement population")
    return {"n": len(data), "mean": statistics.mean(data), "min": data[0],
            **{f"p{p}": data[max(0, math.ceil(p * len(data) / 100) - 1)]
               for p in (50, 95, 99)}, "max": data[-1]}


def integral(samples, field, phases=ACTIVE):
    area = duration = 0
    values = []
    for left, right in zip(samples, samples[1:]):
        dt = right["monotonic_ns"] - left["monotonic_ns"]
        if dt <= 0:
            raise ValueError("non-monotonic memory samples")
        if left["phase"] in phases:
            area += left[field] * dt
            duration += dt
            values.append(left[field])
    if not duration:
        return None
    return {"time_weighted_mean_mib": area / duration / 2**20,
            "duration_s": duration / 1e9,
            "sample_distribution_mib": distribution(values, 2**20)}


def fingerprint(replay):
    result = replay.get("fingerprint", {})
    if result.get("exit_code") != 0:
        raise ValueError("missing or failed final workspace fingerprint")
    return json.loads(result["stdout"])


def inspect(path, source_sha256, task_count):
    report = json.loads(path.read_text())
    if not report["summary"]["success"] or report["summary"]["errors"]:
        raise ValueError(f"failed replay: {path.name}")
    if report["config"]["wait_scale"] != 1:
        raise ValueError("changed wait scale")
    if report["commits"]["source_manifest_sha256"] != source_sha256:
        raise ValueError("source manifest differs from requested frozen revision")
    if len(report["replays"]) != task_count:
        raise ValueError("incomplete task count")
    indexed, tasks, signatures = {}, {}, []
    for replay in report["replays"]:
        if replay.get("error") or replay.get("fidelity_errors"):
            raise ValueError("replay fidelity error")
        task = replay["trajectory_id"]
        task_tools = []
        for raw in replay["tools"]:
            tool = dict(raw)
            key = (task, tool["sequence"])
            if key in indexed:
                raise ValueError("duplicate task/tool key")
            if tool["exit_code"] != tool["expected_exit_code"]:
                raise ValueError("changed tool outcome")
            if tool["turn_ns"] != tool["wake_restore_ns"] + tool["command_ns"]:
                raise ValueError("latency endpoint mismatch")
            if not isinstance(tool.get("server_command_ns"), int):
                raise ValueError("server timing unavailable")
            tool["rpc_overhead_ns"] = tool["exec_rpc_ns"] - tool["server_command_ns"]
            if tool["rpc_overhead_ns"] < 0:
                raise ValueError("server duration exceeds client RPC duration")
            if sum(tool[field] for field in COMPONENTS) != tool["turn_ns"]:
                raise ValueError("latency decomposition does not reconcile exactly")
            indexed[key] = tool
            task_tools.append(tool)
            signatures.append((task, tool["sequence"], tool["source_arguments_sha256"],
                               tool["expected_exit_code"]))
        tasks[task] = {"tool_ms": distribution([t["turn_ns"] for t in task_tools], 1e6),
            "component_mean_ms": {field: statistics.mean(t[field] for t in task_tools) / 1e6
                                  for field in COMPONENTS},
            "actual_wait_s": sum(replay.get("waits_ns", [])) / 1e9,
            "wait_count": len(replay.get("waits_ns", [])),
            "final_fingerprint": fingerprint(replay),
            "major_faults": replay.get("final_major_faults"),
            "final_swap_bytes": replay.get("final_swap_bytes")}
    if len(indexed) != report["summary"]["expected_tool_calls"]:
        raise ValueError("incomplete tool count")
    samples = report["samples"]
    tools = list(indexed.values())
    idle = [sample for sample in samples if sample["phase"] == "idle"]
    events = report["scheduler_events"]
    hazards = [json.loads(event["detail"]) for event in events if event["action"] == "hazard"]
    result = {"raw_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "generated_at": report["generated_at"], "source_manifest_sha256": source_sha256,
        "workload_manifest_sha256": report["workload"]["manifest_sha256"],
        "tool_ms": distribution([t["turn_ns"] for t in tools], 1e6),
        "component_ms": {field: distribution([t[field] for t in tools], 1e6) for field in COMPONENTS},
        "request_to_ready_ms": distribution([r["request_to_ready_ns"] for r in report["requests"]], 1e6),
        "nonzero_tool_exits": sum(t["exit_code"] != 0 for t in tools),
        "timeout_tool_exits": sum(t["exit_code"] in (124, 137) for t in tools),
        "deadline_violations": sum(t["turn_ns"] > 180_000_000_000 for t in tools),
        "memory": {field: integral(samples, field) for field in MEMORY},
        "trace_only_memory": {field: integral(samples, field, {"trace_replay"}) for field in MEMORY},
        "pre_run_idle_mean_mib": {field: statistics.mean(s[field] for s in idle) / 2**20
                                  for field in MEMORY} if idle else {},
        "sampling_duration_ms": distribution([s["sampling_duration_ns"] for s in samples], 1e6),
        "sample_gap_ms": distribution([b["monotonic_ns"] - a["monotonic_ns"]
                                       for a, b in zip(samples, samples[1:])], 1e6),
        "sampling_serial_wall_fraction": sum(s["sampling_duration_ns"] for s in samples) /
            (samples[-1]["monotonic_ns"] - samples[0]["monotonic_ns"]),
        "features": {key: report["config"][key] for key in FEATURES},
        "capabilities": report["memory_capabilities"],
        "diagnostics": {key: value for key, value in report["summary"].items()
            if key.startswith(("reclaim", "speculative", "pool_", "major_fault", "slo_")) or
            key in {"completed_turns_per_second", "trace_turns_per_second", "active_duration_ns"}},
        "scheduler_action_counts": dict(collections.Counter(e["action"] for e in events)),
        "hazard_diagnostics": {
            "evaluations": len(hazards),
            "maximum_probability": max((h["probability"] for h in hazards), default=None),
            "at_speculative_probability_threshold": sum(h["probability"] >=
                report["config"]["speculative_restore_probability"] for h in hazards),
            "expected_remaining_within_lead": sum(h["expected_remaining_seconds"] <=
                h["horizon_seconds"] for h in hazards),
            "estimate_sources": dict(collections.Counter(h["source"] for h in hazards))},
        "movement_events": [e for e in events if any(s in e["action"] for s in
                           ("reclaim", "speculat", "restore", "breaker"))],
        "pool_events": report["pool_events"], "tasks": tasks,
        "signature": sorted(signatures)}
    return result, indexed


def compare(baseline, treatment, baseline_tools, treatment_tools):
    if baseline["signature"] != treatment["signature"]:
        raise ValueError("unequal command set or task ordering")
    if baseline["workload_manifest_sha256"] != treatment["workload_manifest_sha256"]:
        raise ValueError("unequal normalized workload")
    for task, values in baseline["tasks"].items():
        other = treatment["tasks"][task]
        if values["final_fingerprint"] != other["final_fingerprint"]:
            raise ValueError(f"changed final source fingerprint: {task}")
        if values["wait_count"] != other["wait_count"]:
            raise ValueError("changed wait-event count")
    ratios = {key: treatment["tool_ms"][key] / baseline["tool_ms"][key]
              for key in ("mean", "p50", "p95", "p99")}
    paired = []
    for key, old in baseline_tools.items():
        new = treatment_tools[key]
        paired.append({"task": key[0], "sequence": key[1],
            "baseline_ms": old["turn_ns"] / 1e6, "treatment_ms": new["turn_ns"] / 1e6,
            "ratio": new["turn_ns"] / old["turn_ns"],
            "delta_ms": (new["turn_ns"] - old["turn_ns"]) / 1e6,
            "component_delta_ms": {field: (new[field] - old[field]) / 1e6 for field in COMPONENTS}})
    component_delta = {field: sum(p["component_delta_ms"][field] for p in paired)
                       for field in COMPONENTS}
    return {"matched_tools": len(paired), "tool_ratios": ratios,
        "guards_pass": ratios["p95"] <= 1.10 and ratios["p99"] <= 1.10 and
                        treatment["deadline_violations"] == 0,
        "component_total_delta_ms": component_delta,
        "total_tool_time_delta_ms": sum(p["delta_ms"] for p in paired),
        "service_memory_delta_mib": {field: treatment["memory"][field]["time_weighted_mean_mib"] -
            baseline["memory"][field]["time_weighted_mean_mib"] for field in MEMORY},
        "per_task": {task: {"calls": values["tool_ms"]["n"],
            "mean_ratio": treatment["tasks"][task]["tool_ms"]["mean"] / values["tool_ms"]["mean"],
            "p95_ratio": treatment["tasks"][task]["tool_ms"]["p95"] / values["tool_ms"]["p95"]}
            for task, values in baseline["tasks"].items()},
        "largest_positive_deltas": sorted(paired, key=lambda row: row["delta_ms"], reverse=True)[:15],
        "largest_negative_deltas": sorted(paired, key=lambda row: row["delta_ms"])[:15],
        "paired_tools": paired}


def markdown(data):
    state = "Completed campaign" if data.get("campaign_complete") else "Partial campaign"
    lines = ["# Replay checkpoint audit", "", f"As of {data['audited_at']}. "
        f"{state}; completed individual replays only. One run per configuration is descriptive, "
        "not a formal non-inferiority or serving-density result.", "",
        "| Configuration | Tools | Mean ms | p95 ms | p99 ms | Service MiB | Anon MiB | File MiB | Kernel MiB |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for label, result in data["runs"].items():
        d = result["tool_ms"]
        memory = [result["memory"][field]["time_weighted_mean_mib"] for field in MEMORY[:4]]
        lines.append(f"| {label} | {d['n']} | {d['mean']:.2f} | {d['p95']:.2f} | {d['p99']:.2f} | " +
                     " | ".join(f"{v:.2f}" for v in memory) + " |")
    lines += ["", "Memory entries are time-weighted cgroup charges, not uniquely attributable physical DRAM. "
              "File cache can remain charged after sandbox deletion; inode and page ownership make prior run state relevant.", ""]
    for label, pair in data["comparisons"].items():
        ratio = pair["tool_ratios"]
        lines += [f"## {label} vs F0-S0", "",
            f"Matched {pair['matched_tools']} tools and final workspace fingerprints. Mean {ratio['mean']:.3f}x, "
            f"p95 {ratio['p95']:.3f}x, p99 {ratio['p99']:.3f}x; registered 1.10 tail guards pass: {pair['guards_pass']}.", "",
            "Additive total tool-time differences (treatment minus baseline):", ""]
        lines += [f"- {field}: {value:.2f} ms." for field, value in pair["component_total_delta_ms"].items()]
        lines += ["", "Per-task means (all tasks retained):", ""]
        lines += [f"- {task}: {value['mean_ratio']:.3f}x over {value['calls']} calls."
                  for task, value in pair["per_task"].items()]
        lines += [""]
    if data["treatment_ablations"]:
        lines += ["## Within-OverlayFS ablations", "",
            "Descriptive paired ratios; these references do not replace the registered FullCopy guard.", "",
            "| Pair (candidate / reference) | Mean ratio | p95 ratio | p99 ratio | Service-charge delta MiB |",
            "|---|---:|---:|---:|---:|"]
        for label, pair in data["treatment_ablations"].items():
            ratios = pair["tool_ratios"]
            lines.append(f"| {label} | {ratios['mean']:.3f} | {ratios['p95']:.3f} | {ratios['p99']:.3f} | "
                         f"{pair['service_memory_delta_mib']['task_service_cgroup_sum_bytes']:.2f} |")
        lines += [""]
    lines += ["## Required interpretation", "",
        "- SSD/XFS is the workspace backend. `reclaim_tier=ssd` requests Linux reclaim; it is not proof that state reached SSD. Inspect actual sandbox swap and compressed-byte counters.",
        "- Server command time and wake/RPC/result-pack differences reconcile exactly to the total tool-time difference; this is a decomposition, not proof of the causal mechanism.",
        "- Registered tail guards apply to pooled calls within each complete replay; per-task ratios are diagnostic slices, not newly introduced acceptance thresholds.",
        "- Nearest-rank p99 uses rank ceil(0.99*n), with n equal to the actual measured call count, not the number of tasks. " +
            "; ".join(f"{label}: n={run['tool_ms']['n']}, rank={math.ceil(0.99 * run['tool_ms']['n'])}"
                      for label, run in data["runs"].items()) +
            ". A passing descriptive checkpoint is not an inferential non-inferiority result.",
        "- Absolute host-memory deltas are confounded on the shared host. Do not use clipped nonnegative host deltas or cgroup memory ratios as proof of density improvement.",
        "- Ready latency includes the configured admission/pool path and is not the separate no-pool cold-start endpoint.",
        "- Source, workload, command hashes, exits, wait-event counts, final fingerprints, and endpoint arithmetic were checked by this helper. Full payload hashes and paired tools are in audit.json.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    args = parser.parse_args()
    plan = json.loads((args.campaign / "PLAN.json").read_text())
    data = {"schema": "crate-development-audit-v1", "audited_at": datetime.now(timezone.utc).isoformat(),
            "campaign_complete": (args.campaign / "COMPLETED.json").exists(), "plan": plan,
            "audit_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "runs": {}, "comparisons": {}, "treatment_ablations": {}, "pending": []}
    indexed = {}
    for rep, order in enumerate(plan["orders"]):
        for label in order:
            key = f"r{rep}-{label}"
            path = args.campaign / f"replay-{key}.json"
            if not path.exists():
                data["pending"].append(key)
                continue
            data["runs"][key], indexed[key] = inspect(path, args.source_sha256, plan["tasks"])
        baseline = f"r{rep}-F0-S0"
        if baseline in data["runs"]:
            for label in order:
                key = f"r{rep}-{label}"
                if key != baseline and key in data["runs"]:
                    data["comparisons"][key] = compare(data["runs"][baseline], data["runs"][key],
                                                      indexed[baseline], indexed[key])
        for reference, candidate in (("T1-S0", "T1-S1"), ("T1-S0", "T1-S2"),
                                     ("T1-S1", "T1-S2"), ("T1-S1", "T1-S1-Q"),
                                     ("T1-S2", "T1-S2-Q")):
            ref_key, candidate_key = f"r{rep}-{reference}", f"r{rep}-{candidate}"
            if ref_key not in data["runs"] or candidate_key not in data["runs"]:
                continue
            pair = compare(data["runs"][ref_key], data["runs"][candidate_key],
                           indexed[ref_key], indexed[candidate_key])
            # The preregistered reference is FullCopy, not these ablations.
            pair["descriptive_relative_tails_within_1_10"] = pair.pop("guards_pass")
            pair["reference"] = ref_key
            pair["candidate"] = candidate_key
            pair["feature_differences"] = {field: {
                "reference": data["runs"][ref_key]["features"][field],
                "candidate": data["runs"][candidate_key]["features"][field]}
                for field in FEATURES if data["runs"][ref_key]["features"][field] !=
                data["runs"][candidate_key]["features"][field]}
            data["treatment_ablations"][candidate_key + " / " + ref_key] = pair
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "audit.json").write_text(json.dumps(data, indent=2) + "\n")
    (args.output / "AUDIT.md").write_text(markdown(data))
    print(markdown(data))


if __name__ == "__main__":
    main()
