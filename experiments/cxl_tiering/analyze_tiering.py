#!/usr/bin/env python3
"""Derive the descriptive three-configuration comparison from raw replay reports.

Reads ``<results>/raw/<configuration>/replay.json`` (never modifies it) and
writes ``metrics.json`` plus Markdown tables under ``analysis/``. Percentiles
are nearest-rank, as in the September 19 analysis. One ordered run per
configuration is descriptive: no density, non-inferiority or hardware-causal
claim follows from these numbers.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

ACTIVE = {"pool_prepare", "create", "trace_replay"}
MIB = 1 << 20

DEFINITIONS = {
    "tool_turn_ms": "wake/queue/restore + real command + result handling per tool call; excludes the preceding model wait",
    "command_ms": "normal-API exec of the recorded command, including any demand faults",
    "wake_ms": "confirmed wake: backend restore (before thaw) + confirmed thaw, up to dispatch permission",
    "time_weighted_mib": "integral of the sampled value over active phases (pool_prepare, create, trace_replay) / active duration; 100 ms samples",
    "sandbox_cgroup": "sum of sandbox leaf memory.current; a cgroup charge, not proof of physical DRAM saving",
    "service_tree": "sum of the run's task-daemon service cgroups (parent includes children; never add to sandbox_cgroup)",
    "anon/file/kernel": "memory.stat of the service trees; file cache and anonymous memory are reported apart",
    "cxl_store": "bytes held by the pager's store (payload) and store+pager metadata kept in host DRAM",
    "stored/released/restored": "pager receipts summed over sandboxes: written to the tier / measured source release / copied back",
    "file_reclaimed": "cgroup charge dropped by kernel file-cache reclaim; stored nowhere, not tier placement",
    "denominators": "tool metrics over all completed tool calls of all tasks (unfavourable tasks kept); failures listed separately",
}


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))]


def distribution(values: list[float], scale: float = 1e6) -> dict[str, float]:
    scaled = [value / scale for value in values]
    return {"n": len(scaled), "mean": sum(scaled) / len(scaled) if scaled else 0.0,
            "p50": percentile(scaled, 0.50), "p95": percentile(scaled, 0.95),
            "p99": percentile(scaled, 0.99), "max": max(scaled, default=0.0)}


def weighted(samples: list[dict], value) -> dict[str, float]:
    area = duration = 0.0
    seen: list[float] = []
    for current, following in zip(samples, samples[1:]):
        if current["phase"] not in ACTIVE:
            continue
        delta = following["monotonic_ns"] - current["monotonic_ns"]
        number = float(value(current))
        area += number * delta
        duration += delta
        seen.append(number)
    return {"time_weighted_mean_mib": area / duration / MIB if duration else 0.0,
            "p95_mib": percentile(seen, 0.95) / MIB, "peak_mib": max(seen, default=0.0) / MIB,
            "active_seconds": duration / 1e9}


def analyze(report: dict) -> dict:
    samples, replays = report["samples"], report["replays"]
    tools = [tool for replay in replays for tool in replay["tools"]]
    events = report["scheduler_events"]
    reclaims = [event for event in events if event["action"] == "reclaim"]
    details = [json.loads(event["detail"]) for event in reclaims if event.get("detail")]
    restores = [event for event in events if event["action"] == "restore"]
    idle = [sample for sample in samples if sample["phase"] == "idle"][:1]
    accounting = [replay.get("tier_accounting", {}) for replay in replays]
    tier = lambda key: sum(int(item.get(key, 0)) for item in accounting)  # noqa: E731
    detail = lambda key: sum(int(item.get("details", {}).get(key, 0)) for item in accounting)  # noqa: E731
    final_stat = lambda key: [replay.get("final_memory_stat", {}).get(key, 0) for replay in replays]  # noqa: E731
    summary = report["summary"]
    cache = report.get("service_cache_events", [])
    cache_work = [event for event in cache if "requested_bytes" in event]
    return {
        "service_cache": {
            "chunks": len(cache_work),
            "errors": [event for event in cache if "error" in event],
            "requested_bytes": sum(event["requested_bytes"] for event in cache_work),
            "charge_delta_bytes": sum(event["charge_delta_bytes"] for event in cache_work),
            "file_delta_bytes": sum(event["file_delta_bytes"] for event in cache_work),
            "swap_delta_bytes": sum(event["swap_delta_bytes"] for event in cache_work),
            "duration_ms": distribution([event["duration_ns"] for event in cache_work]),
            "interpretation": "signed observed deltas, not independent physical page attribution",
        },
        "memory_tier": report.get("memory_tier", {}),
        "work": {
            "success": summary["success"], "requests": summary["requests"],
            "completed_requests": summary["completed_requests"],
            "expected_tool_calls": summary["expected_tool_calls"],
            "completed_tool_calls": summary["completed_tool_calls"],
            "task_errors": sum(bool(replay["error"]) for replay in replays),
            "tasks_with_fidelity_errors": sum(bool(replay.get("fidelity_errors")) for replay in replays),
            "runner_errors": summary["errors"],
            "turns_per_second_lifecycle": summary["completed_turns_per_second"],
            "turns_per_second_trace_phase": summary["trace_turns_per_second"],
            "peak_observed_sandboxes": summary["peak_observed_sandboxes"],
        },
        "request_to_ready_ms": distribution([request["request_to_ready_ns"] for request in report["requests"]]),
        "tool_turn_ms": distribution([tool["turn_ns"] for tool in tools]),
        "command_ms": distribution([tool["command_ns"] for tool in tools]),
        "wake_ms": distribution([tool["wake_restore_ns"] for tool in tools]),
        "dram": {
            "sandbox_cgroup": weighted(samples, lambda s: s.get("sandbox_memory_current_bytes", 0)),
            "service_tree": weighted(samples, lambda s: s.get("task_service_cgroup_sum_bytes", 0)),
            "observer_cgroup": (weighted(samples, lambda s: s["observer_cgroup_current_bytes"])
                if all(s.get("observer_cgroup_current_bytes") is not None for s in samples) else None),
            "service_plus_observer": (weighted(samples, lambda s:
                s.get("task_service_cgroup_sum_bytes", 0) + s["observer_cgroup_current_bytes"])
                if all(s.get("observer_cgroup_current_bytes") is not None for s in samples) else None),
            "daemon_cgroup": weighted(samples, lambda s: s.get("task_daemon_cgroup_sum_bytes", 0)),
            "daemon_file": weighted(samples, lambda s: s.get("task_daemon_stat_sum_bytes", {}).get("file", 0)),
            "service_tree_anon": weighted(samples, lambda s: s.get("task_service_anon_bytes", 0)),
            "service_tree_file": weighted(samples, lambda s: s.get("task_service_file_bytes", 0)),
            "service_tree_kernel": weighted(samples, lambda s: s.get("task_service_kernel_bytes", 0)),
            "waiting_sandboxes": weighted(samples, lambda s: s.get("waiting_sandbox_memory_bytes", 0)),
            "host_available_delta_signed": weighted(samples, lambda s: s.get("signed_host_available_delta_bytes", 0)),
            "pre_run_idle_service_tree_mib": (idle[0].get("task_service_cgroup_sum_bytes", 0) / MIB) if idle else None,
            "pre_run_idle_service_tree_file_mib": (idle[0].get("task_service_file_bytes", 0) / MIB) if idle else None,
        },
        "swap": {"sandbox_swap": weighted(samples, lambda s: s.get("sandbox_memory_swap_bytes", 0)),
                 "final_swap_bytes_sum": sum(replay.get("final_swap_bytes", 0) for replay in replays)},
        "cxl_store": {
            "payload": weighted(samples, lambda s: s.get("tier_store", {}).get("payload", 0)),
            "cold": weighted(samples, lambda s: s.get("tier_store", {}).get("cold", 0)),
            "metadata_host_dram": weighted(samples, lambda s: s.get("tier_store", {}).get("store_metadata", 0)
                                           + s.get("tier_store", {}).get("pager_metadata", 0)),
        },
        "movement": {
            "demotions": len(reclaims),
            "demotion_errors": sum(event["action"] == "reclaim_error" for event in events),
            "restore_errors": sum(event["action"] == "restore_error" for event in events),
            "demote_ms": distribution([event["duration_ns"] for event in reclaims]),
            "confirmed_restore_ms": distribution([event["duration_ns"] for event in restores]),
            "cgroup_charge_reclaimed_bytes": sum(event["bytes"] for event in reclaims),
            "swap_delta_bytes": sum(item.get("swap_delta_bytes", 0) for item in details),
            "tier_stored_bytes_receipts": sum(item.get("stored_bytes", 0) for item in details),
            "tier_released_bytes_receipts": sum(item.get("released_bytes", 0) for item in details),
            "eligible_bytes_receipts": sum(item.get("eligible_bytes", 0) for item in details),
            "file_reclaimed_bytes_receipts": sum(item.get("file_reclaimed_bytes", 0) for item in details),
            "partial_demotions": sum(bool(item.get("partial")) for item in details),
            "per_sandbox_accounting_sum": {
                "stored_bytes": tier("stored_bytes"), "released_bytes": tier("released_bytes"),
                "restored_bytes": tier("restored_bytes"), "demand_faults": tier("demand_faults"),
                "stale_rejections": tier("stale_rejections"), "errors": tier("errors"),
                "regions": detail("regions"), "swap_leak_bytes": detail("swap_leak_bytes"),
                "skipped_regions_without_uffd": detail("skipped_regions_without_uffd"),
            },
            "speculative_restores": summary["speculative_restore_events"],
        },
        "final_sandbox_memory_stat_mib": {key: {"mean": sum(final_stat(key)) / max(1, len(replays)) / MIB,
                                                "max": max(final_stat(key), default=0) / MIB}
                                          for key in ("anon", "shmem", "file")},
        "major_faults_completed_sandboxes": summary.get("major_faults_completed_sandboxes_total", 0),
        "per_task": [{"sequence": replay["sequence"], "instance_id": replay["trajectory_id"],
                      "tools_completed": len(replay["tools"]), "error": replay["error"],
                      "fidelity_errors": replay.get("fidelity_errors", []),
                      "tool_turn_mean_ms": (sum(tool["turn_ns"] for tool in replay["tools"]) / len(replay["tools"]) / 1e6
                                            if replay["tools"] else None),
                      "tool_turn_p95_ms": percentile([tool["turn_ns"] / 1e6 for tool in replay["tools"]], 0.95),
                      "tier_stored_bytes": replay.get("tier_accounting", {}).get("stored_bytes", 0)}
                     for replay in replays],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    a = ap.parse_args()
    metrics: dict[str, object] = {"schema": "crate-tiering-metrics-v1", "definitions": DEFINITIONS,
                                  "limitations": [
                                      "one ordered run per configuration: descriptive only",
                                      "closed-loop waves of at most 8 sandboxes with a fixed queue at time zero, not an open-loop arrival schedule",
                                      "shared host; cgroup charge is not uniquely attributable physical DRAM; parent/child charges are not additive",
                                      "the SSD and CXL backends move different page populations with different mechanisms; their difference is not SSD-versus-CXL hardware",
                                  ],
                                  "configurations": {}}
    configurations: dict[str, dict] = metrics["configurations"]  # type: ignore[assignment]
    for label in ("baseline", "crate-ssd", "crate-cxl"):
        path = a.results / "raw" / label / "replay.json"
        if not path.exists():
            configurations[label] = {"state": "not-run-or-no-report"}
            continue
        configurations[label] = analyze(json.loads(path.read_text()))
    cold_path = a.results / "raw" / "cold" / "samples.jsonl"
    if cold_path.exists():
        cold: dict[str, list[dict]] = {"baseline": [], "t1": []}
        for line in cold_path.read_text().splitlines():
            record = json.loads(line)
            if "error" not in record:
                cold[record["mode"]].append(record)
        metrics["cold_start"] = {
            "definition": "SandboxFS create request received -> client-confirmed first successful normal-API /bin/true; no ready pool, warm host, prepared local base; independent of the memory-tier backend",
            **{mode: {"cold_start_ms": distribution([r["cold_start_ns"] for r in rows]),
                      "filesystem_provision_ms": distribution([r["filesystem_provision_ns"] for r in rows])}
               for mode, rows in cold.items()}}
    base = configurations.get("baseline", {})
    if "tool_turn_ms" in base:
        metrics["relative_to_baseline"] = {
            label: {metric: {stat: (configurations[label][metric][stat] / base[metric][stat]
                                    if base[metric][stat] else None) for stat in ("mean", "p50", "p95", "p99")}
                    for metric in ("tool_turn_ms", "command_ms", "wake_ms")}
            for label in ("crate-ssd", "crate-cxl") if "tool_turn_ms" in configurations.get(label, {})}
    (a.results / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")

    analysis = a.results / "analysis"
    analysis.mkdir(exist_ok=True)
    rows = ["| Metric | " + " | ".join(configurations) + " |", "|---|" + "---:|" * len(configurations)]

    def row(name: str, getter) -> None:
        cells = []
        for config in configurations.values():
            try:
                value = getter(config)
                cells.append(f"{value:,.2f}" if isinstance(value, float) else str(value))
            except (KeyError, TypeError):
                cells.append("n/a")
        rows.append(f"| {name} | " + " | ".join(cells) + " |")

    row("completed tasks / 32", lambda c: c["work"]["completed_requests"])
    row("completed tool calls", lambda c: c["work"]["completed_tool_calls"])
    row("task errors", lambda c: c["work"]["task_errors"])
    row("tasks with fidelity errors", lambda c: c["work"]["tasks_with_fidelity_errors"])
    for stat in ("mean", "p50", "p95", "p99"):
        row(f"tool turn {stat} (ms)", lambda c, s=stat: c["tool_turn_ms"][s])
    for stat in ("mean", "p50", "p95", "p99"):
        row(f"wake {stat} (ms)", lambda c, s=stat: c["wake_ms"][s])
    row("request to ready mean (ms)", lambda c: c["request_to_ready_ms"]["mean"])
    for key in ("sandbox_cgroup", "waiting_sandboxes", "service_tree", "service_tree_anon", "service_tree_file"):
        for stat in ("time_weighted_mean_mib", "p95_mib", "peak_mib"):
            row(f"DRAM {key} {stat}", lambda c, k=key, s=stat: c["dram"][k][s])
    row("pre-run idle service tree (MiB)", lambda c: c["dram"]["pre_run_idle_service_tree_mib"])
    row("sandbox swap mean (MiB)", lambda c: c["swap"]["sandbox_swap"]["time_weighted_mean_mib"])
    row("sandbox swap peak (MiB)", lambda c: c["swap"]["sandbox_swap"]["peak_mib"])
    row("CXL store payload mean (MiB)", lambda c: c["cxl_store"]["payload"]["time_weighted_mean_mib"])
    row("CXL store payload peak (MiB)", lambda c: c["cxl_store"]["payload"]["peak_mib"])
    row("CXL store+pager metadata mean in DRAM (MiB)", lambda c: c["cxl_store"]["metadata_host_dram"]["time_weighted_mean_mib"])
    row("CXL store+pager metadata peak in DRAM (MiB)", lambda c: c["cxl_store"]["metadata_host_dram"]["peak_mib"])
    row("demotions", lambda c: c["movement"]["demotions"])
    row("cgroup charge reclaimed (MiB)", lambda c: c["movement"]["cgroup_charge_reclaimed_bytes"] / MIB)
    row("swap delta (MiB)", lambda c: c["movement"]["swap_delta_bytes"] / MIB)
    row("tier stored (MiB)", lambda c: c["movement"]["tier_stored_bytes_receipts"] / MIB)
    row("Reclaim charge residual, not measured file-cache bytes (MiB)", lambda c: c["movement"]["file_reclaimed_bytes_receipts"] / MIB)
    row("demote mean (ms)", lambda c: c["movement"]["demote_ms"]["mean"])
    row("demote p95 (ms)", lambda c: c["movement"]["demote_ms"]["p95"])
    row("major faults (completed sandboxes)", lambda c: c["major_faults_completed_sandboxes"])
    row("turns/s (trace phase)", lambda c: c["work"]["turns_per_second_trace_phase"])
    (analysis / "comparison.md").write_text("\n".join(rows) + "\n")

    inventory = ["| # | task | " + " | ".join(f"{label} tools / error / mean ms" for label in configurations) + " |",
                 "|---|---|" + "---|" * len(configurations)]
    tasks: dict[int, dict[str, dict]] = {}
    for label, config in configurations.items():
        for task in config.get("per_task", []):
            tasks.setdefault(task["sequence"], {})[label] = task
    for sequence in sorted(tasks):
        any_task = next(iter(tasks[sequence].values()))
        cells = []
        for label in configurations:
            task = tasks[sequence].get(label)
            cells.append("not run" if task is None else
                         f"{task['tools_completed']} / {task['error'] or ('fidelity' if task['fidelity_errors'] else 'ok')} / "
                         f"{task['tool_turn_mean_ms']:.1f}" if task and task["tool_turn_mean_ms"] is not None
                         else f"0 / {task['error'] or 'no tools'} / n/a")
        inventory.append(f"| {sequence} | {any_task['instance_id']} | " + " | ".join(cells) + " |")
    (analysis / "per-task-inventory.md").write_text("\n".join(inventory) + "\n")
    print((analysis / "comparison.md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
