#!/usr/bin/env python3
"""Analyze SWE-rebench-derived Claude Code traces for Caden scheduling.

This script treats tool execution as local sandbox work and the interval from a
tool result to the next assistant message as an off-host LLM wait stage.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATASET_LABELS = {
    "all_images_haiku": "SWE-rebench Claude Haiku",
    "all_images_local": "SWE-rebench local GLM",
    "batch_swebench_18tasks": "SWE-bench 18-task batch",
}

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRACE_ROOT = REPO_ROOT.parent / "agentcgroup" / "experiments"


def dataset_label(dataset: str) -> str:
    return DATASET_LABELS.get(dataset, dataset)


def parse_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def summarize(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "sum": sum(values),
        "mean": (sum(values) / len(values)) if values else None,
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def format_seconds(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if value >= 3600:
        return f"{value / 3600:.2f}h"
    if value >= 60:
        return f"{value / 60:.2f}m"
    return f"{value:.2f}s"


def format_ratio(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def content_items(content: Any) -> list[Any]:
    if isinstance(content, list):
        return content
    return [content]


def content_has_type(content: Any, expected_type: str) -> bool:
    for item in content_items(content):
        if isinstance(item, dict) and item.get("type") == expected_type:
            return True
    return False


def load_trace_events(trace_path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_no, line in enumerate(trace_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        message = obj.get("message")
        role = obj.get("type")
        content = obj.get("content")
        if isinstance(message, dict):
            role = message.get("role") or role
            content = message.get("content")

        ts = parse_timestamp(obj.get("timestamp"))
        if ts is None or role not in {"user", "assistant"}:
            continue

        events.append(
            {
                "line": line_no,
                "timestamp": ts,
                "role": role,
                "has_tool_result": content_has_type(content, "tool_result"),
                "has_tool_use": content_has_type(content, "tool_use"),
                "raw_type": obj.get("type"),
            }
        )
    return events


def next_assistant_wait(events: list[dict[str, Any]], start_idx: int) -> float | None:
    start_ts = events[start_idx]["timestamp"]
    for event in events[start_idx + 1 :]:
        if event["role"] == "assistant":
            wait = event["timestamp"] - start_ts
            if wait >= 0:
                return wait
            return None
    return None


def measure_llm_waits(events: list[dict[str, Any]]) -> tuple[list[float], list[float]]:
    post_tool_waits: list[float] = []
    initial_waits: list[float] = []
    saw_initial_user = False

    for idx, event in enumerate(events):
        if event["role"] != "user":
            continue
        if event["has_tool_result"]:
            wait = next_assistant_wait(events, idx)
            if wait is not None:
                post_tool_waits.append(wait)
        elif not saw_initial_user:
            wait = next_assistant_wait(events, idx)
            if wait is not None:
                initial_waits.append(wait)
            saw_initial_user = True

    return post_tool_waits, initial_waits


def load_tool_calls(tool_path: Path) -> tuple[list[dict[str, Any]], Counter[str], dict[str, list[float]]]:
    try:
        raw_calls = json.loads(tool_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], Counter(), defaultdict(list)

    calls: list[dict[str, Any]] = []
    tool_counts: Counter[str] = Counter()
    tool_durations: dict[str, list[float]] = defaultdict(list)
    if not isinstance(raw_calls, list):
        return calls, tool_counts, tool_durations

    for call in raw_calls:
        if not isinstance(call, dict):
            continue
        start = parse_timestamp(call.get("timestamp"))
        end = parse_timestamp(call.get("end_timestamp"))
        if start is None or end is None:
            continue
        duration = end - start
        if duration < 0:
            continue
        tool = str(call.get("tool") or "unknown")
        calls.append(
            {
                "tool": tool,
                "timestamp": start,
                "end_timestamp": end,
                "duration_s": duration,
            }
        )
        tool_counts[tool] += 1
        tool_durations[tool].append(duration)
    return calls, tool_counts, tool_durations


def parse_percent(text: Any) -> float | None:
    if text is None:
        return None
    cleaned = str(text).strip().rstrip("%")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_memory_mib(text: Any) -> float | None:
    if text is None:
        return None
    first = str(text).split("/", 1)[0].strip()
    parts = first.split()
    if len(parts) == 1:
        number = "".join(ch for ch in parts[0] if ch.isdigit() or ch == ".")
        unit = "".join(ch for ch in parts[0] if ch.isalpha()).upper()
    else:
        number = parts[0]
        unit = parts[1].upper()
    try:
        value = float(number)
    except ValueError:
        return None
    factors = {
        "B": 1 / (1024 * 1024),
        "KB": 1 / 1024,
        "KIB": 1 / 1024,
        "MB": 1,
        "MIB": 1,
        "GB": 1024,
        "GIB": 1024,
        "TB": 1024 * 1024,
        "TIB": 1024 * 1024,
    }
    return value * factors.get(unit, 1)


def load_resources(resources_path: Path) -> dict[str, float | int | None]:
    if not resources_path.exists():
        return {
            "resource_samples": 0,
            "avg_cpu_percent": None,
            "p95_cpu_percent": None,
            "avg_mem_mib": None,
            "max_mem_mib": None,
        }
    try:
        raw = json.loads(resources_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        raw = {}
    samples = raw.get("samples", []) if isinstance(raw, dict) else []
    cpu_values: list[float] = []
    mem_values: list[float] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        cpu = parse_percent(sample.get("cpu_percent"))
        mem = parse_memory_mib(sample.get("mem_usage"))
        if cpu is not None:
            cpu_values.append(cpu)
        if mem is not None:
            mem_values.append(mem)
    return {
        "resource_samples": len(samples),
        "avg_cpu_percent": (sum(cpu_values) / len(cpu_values)) if cpu_values else None,
        "p95_cpu_percent": percentile(cpu_values, 0.95),
        "avg_mem_mib": (sum(mem_values) / len(mem_values)) if mem_values else None,
        "max_mem_mib": max(mem_values) if mem_values else None,
    }


def trace_meta(root: Path, trace_path: Path) -> dict[str, str]:
    rel_parts = trace_path.relative_to(root).parts
    dataset = rel_parts[0] if len(rel_parts) > 0 else ""
    return {
        "dataset": dataset,
        "dataset_label": dataset_label(dataset),
        "task": rel_parts[1] if len(rel_parts) > 1 else "",
        "attempt": rel_parts[2] if len(rel_parts) > 2 else "",
        "path": str(trace_path),
    }


def analyze_trace(root: Path, trace_path: Path) -> dict[str, Any]:
    attempt_dir = trace_path.parent
    tool_path = attempt_dir / "tool_calls.json"
    events = load_trace_events(trace_path)
    post_tool_waits, initial_waits = measure_llm_waits(events)
    tool_calls, tool_counts, tool_durations = load_tool_calls(tool_path)
    resource_stats = load_resources(attempt_dir / "resources.json")

    event_timestamps = [event["timestamp"] for event in events]
    span_timestamps = event_timestamps[:]
    for call in tool_calls:
        span_timestamps.extend([call["timestamp"], call["end_timestamp"]])
    trace_span_s = max(span_timestamps) - min(span_timestamps) if len(span_timestamps) >= 2 else 0.0
    tool_durations_flat = [call["duration_s"] for call in tool_calls]
    tool_time_s = sum(tool_durations_flat)
    llm_wait_time_s = sum(post_tool_waits) + sum(initial_waits)
    observed_stage_time_s = tool_time_s + llm_wait_time_s
    llm_fraction = llm_wait_time_s / observed_stage_time_s if observed_stage_time_s > 0 else None
    tool_fraction = tool_time_s / observed_stage_time_s if observed_stage_time_s > 0 else None
    observed_fraction_of_span = observed_stage_time_s / trace_span_s if trace_span_s > 0 else None
    alternation_coverage = len(post_tool_waits) / len(tool_calls) if tool_calls else None

    return {
        **trace_meta(root, trace_path),
        "has_tool_calls_file": tool_path.exists(),
        "event_count": len(events),
        "tool_call_count": len(tool_calls),
        "post_tool_wait_count": len(post_tool_waits),
        "initial_wait_count": len(initial_waits),
        "trace_span_s": trace_span_s,
        "tool_time_s": tool_time_s,
        "llm_wait_time_s": llm_wait_time_s,
        "post_tool_llm_wait_time_s": sum(post_tool_waits),
        "initial_llm_wait_time_s": sum(initial_waits),
        "observed_stage_time_s": observed_stage_time_s,
        "llm_fraction_of_observed_stage_time": llm_fraction,
        "tool_fraction_of_observed_stage_time": tool_fraction,
        "observed_fraction_of_trace_span": observed_fraction_of_span,
        "alternation_coverage": alternation_coverage,
        "stage_pattern_observed": bool(
            len(tool_calls) >= 3
            and len(post_tool_waits) >= max(2, int(0.5 * len(tool_calls)))
            and llm_fraction is not None
            and llm_fraction >= 0.5
        ),
        "tool_duration_stats": summarize(tool_durations_flat),
        "post_tool_wait_stats": summarize(post_tool_waits),
        "initial_wait_stats": summarize(initial_waits),
        "tool_counts": dict(tool_counts),
        "tool_durations_by_name": {tool: summarize(values) for tool, values in tool_durations.items()},
        **resource_stats,
    }


def aggregate_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [
        record
        for record in records
        if record["observed_stage_time_s"] > 0 and record["tool_call_count"] > 0
    ]
    tool_times = [record["tool_time_s"] for record in valid]
    llm_times = [record["llm_wait_time_s"] for record in valid]
    spans = [record["trace_span_s"] for record in valid]
    llm_fracs = [
        record["llm_fraction_of_observed_stage_time"]
        for record in valid
        if record["llm_fraction_of_observed_stage_time"] is not None
    ]
    tool_fracs = [
        record["tool_fraction_of_observed_stage_time"]
        for record in valid
        if record["tool_fraction_of_observed_stage_time"] is not None
    ]
    alternation = [
        record["alternation_coverage"]
        for record in valid
        if record["alternation_coverage"] is not None
    ]

    total_tool = sum(tool_times)
    total_llm = sum(llm_times)
    total_observed = total_tool + total_llm

    return {
        "trace_count": len(records),
        "valid_trace_count": len(valid),
        "traces_with_tool_calls_file": sum(1 for record in records if record["has_tool_calls_file"]),
        "traces_with_stage_pattern": sum(1 for record in valid if record["stage_pattern_observed"]),
        "total_tool_calls": sum(record["tool_call_count"] for record in valid),
        "total_post_tool_waits": sum(record["post_tool_wait_count"] for record in valid),
        "total_tool_time_s": total_tool,
        "total_llm_wait_time_s": total_llm,
        "total_trace_span_s": sum(spans),
        "weighted_llm_fraction_of_observed_stage_time": total_llm / total_observed if total_observed else None,
        "weighted_tool_fraction_of_observed_stage_time": total_tool / total_observed if total_observed else None,
        "stage_pattern_trace_fraction": (
            sum(1 for record in valid if record["stage_pattern_observed"]) / len(valid) if valid else None
        ),
        "llm_fraction_ge_70pct_trace_fraction": (
            sum(1 for value in llm_fracs if value >= 0.70) / len(llm_fracs) if llm_fracs else None
        ),
        "llm_fraction_ge_80pct_trace_fraction": (
            sum(1 for value in llm_fracs if value >= 0.80) / len(llm_fracs) if llm_fracs else None
        ),
        "llm_fraction_ge_90pct_trace_fraction": (
            sum(1 for value in llm_fracs if value >= 0.90) / len(llm_fracs) if llm_fracs else None
        ),
        "tool_time_stats_per_trace": summarize(tool_times),
        "llm_wait_stats_per_trace": summarize(llm_times),
        "trace_span_stats": summarize(spans),
        "llm_fraction_stats_per_trace": summarize(llm_fracs),
        "tool_fraction_stats_per_trace": summarize(tool_fracs),
        "alternation_coverage_stats": summarize(alternation),
    }


def group_by_dataset(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["dataset"]].append(record)
    return {dataset: aggregate_records(dataset_records) for dataset, dataset_records in sorted(grouped.items())}


def aggregate_tools(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    durations: dict[str, list[float]] = defaultdict(list)
    for record in records:
        for tool, stats in record["tool_durations_by_name"].items():
            # Reconstruct only aggregate rows from per-trace stats would lose percentiles.
            # The caller keeps individual calls out of the report, so this is a conservative
            # weighted estimate for count/sum/mean and max.
            count = int(stats["count"] or 0)
            mean = stats["mean"] or 0.0
            max_value = stats["max"]
            if count:
                durations[tool].extend([mean] * count)
                if max_value is not None:
                    durations[tool][-1] = max_value
    return {tool: summarize(values) for tool, values in sorted(durations.items())}


def collect_tool_durations(records: list[dict[str, Any]]) -> dict[str, list[float]]:
    durations: dict[str, list[float]] = defaultdict(list)
    for record in records:
        tool_path = Path(record["path"]).parent / "tool_calls.json"
        tool_calls, _, _ = load_tool_calls(tool_path)
        for call in tool_calls:
            durations[call["tool"]].append(call["duration_s"])
    return durations


def write_csv(records: list[dict[str, Any]], csv_path: Path) -> None:
    fields = [
        "dataset",
        "dataset_label",
        "task",
        "attempt",
        "path",
        "event_count",
        "tool_call_count",
        "post_tool_wait_count",
        "initial_wait_count",
        "trace_span_s",
        "tool_time_s",
        "llm_wait_time_s",
        "observed_stage_time_s",
        "llm_fraction_of_observed_stage_time",
        "tool_fraction_of_observed_stage_time",
        "observed_fraction_of_trace_span",
        "alternation_coverage",
        "stage_pattern_observed",
        "avg_cpu_percent",
        "p95_cpu_percent",
        "avg_mem_mib",
        "max_mem_mib",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field) for field in fields})


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def write_report(
    report_path: Path,
    root: Path,
    summary: dict[str, Any],
    by_dataset: dict[str, dict[str, Any]],
    tool_stats: dict[str, dict[str, Any]],
    records: list[dict[str, Any]],
) -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    task_tool_time = tool_stats.get("Task", {}).get("sum") or 0.0
    adjusted_tool_time = max(0.0, summary["total_tool_time_s"] - task_tool_time)
    adjusted_total = summary["total_llm_wait_time_s"] + adjusted_tool_time
    adjusted_llm_fraction = (
        summary["total_llm_wait_time_s"] / adjusted_total if adjusted_total > 0 else None
    )
    adjusted_tool_fraction = adjusted_tool_time / adjusted_total if adjusted_total > 0 else None
    comparable_records = [
        record
        for record in records
        if record["tool_call_count"] > 0
        and record["observed_stage_time_s"] > 0
        and record["llm_fraction_of_observed_stage_time"] is not None
    ]
    example_records = [record for record in comparable_records if record["tool_call_count"] >= 10]
    if not example_records:
        example_records = comparable_records
    top_llm = sorted(
        example_records,
        key=lambda record: (record["llm_fraction_of_observed_stage_time"], record["llm_wait_time_s"]),
        reverse=True,
    )[:8]
    top_tool = sorted(
        example_records,
        key=lambda record: (record["tool_fraction_of_observed_stage_time"], record["tool_time_s"]),
        reverse=True,
    )[:8]
    tool_rows = []
    for tool, stats in sorted(tool_stats.items(), key=lambda item: item[1]["sum"] or 0, reverse=True)[:12]:
        tool_rows.append(
            [
                tool,
                str(stats["count"]),
                format_seconds(stats["sum"]),
                format_seconds(stats["mean"]),
                format_seconds(stats["p50"]),
                format_seconds(stats["p95"]),
                format_seconds(stats["max"]),
            ]
        )

    dataset_rows = []
    for dataset, stats in by_dataset.items():
        dataset_rows.append(
            [
                dataset_label(dataset),
                str(stats["valid_trace_count"]),
                format_ratio(stats["weighted_llm_fraction_of_observed_stage_time"]),
                format_ratio(stats["weighted_tool_fraction_of_observed_stage_time"]),
                format_ratio(stats["stage_pattern_trace_fraction"]),
                str(stats["total_tool_calls"]),
                format_seconds(stats["total_llm_wait_time_s"]),
                format_seconds(stats["total_tool_time_s"]),
            ]
        )

    lines = [
        "# SWE-Rebench Claude Code Stage Pattern Analysis",
        "",
        f"Generated: {generated}",
        "",
        f"Input root: `{root}`",
        "",
        "Workload source: local `agentcgroup` experiment traces from SWE-rebench/SWE-bench-style coding tasks.",
        "",
        "## Result",
        "",
        (
            f"Analyzed {summary['valid_trace_count']} valid timestamped Claude Code traces. "
            f"Across observed tool and LLM-wait stages, LLM wait accounts for "
            f"{format_ratio(summary['weighted_llm_fraction_of_observed_stage_time'])} "
            f"and local tool execution accounts for "
            f"{format_ratio(summary['weighted_tool_fraction_of_observed_stage_time'])}."
        ),
        "",
        (
            f"{summary['traces_with_stage_pattern']} traces "
            f"({format_ratio(summary['stage_pattern_trace_fraction'])}) show the recurring "
            "`tool -> tool_result -> LLM_WAIT -> assistant` pattern under the conservative "
            "detector used here."
        ),
        "",
        (
            "`Task` is counted as tool time in the main ratio. If treated as a nested-agent "
            f"wrapper rather than local sandbox work, the ratio becomes {format_ratio(adjusted_llm_fraction)} "
            f"LLM wait and {format_ratio(adjusted_tool_fraction)} local tool time."
        ),
        "",
        "## Aggregate Summary",
        "",
        markdown_table(
            ["Metric", "Value"],
            [
                ["Trace files found", str(summary["trace_count"])],
                ["Valid traces with observed stages", str(summary["valid_trace_count"])],
                ["Traces with `tool_calls.json`", str(summary["traces_with_tool_calls_file"])],
                ["Tool calls", str(summary["total_tool_calls"])],
                ["Post-tool LLM waits", str(summary["total_post_tool_waits"])],
                ["Total LLM-wait time", format_seconds(summary["total_llm_wait_time_s"])],
                ["Total local tool time", format_seconds(summary["total_tool_time_s"])],
                [
                    "Median per-trace LLM fraction",
                    format_ratio(summary["llm_fraction_stats_per_trace"]["p50"]),
                ],
                [
                    "P90 per-trace LLM fraction",
                    format_ratio(summary["llm_fraction_stats_per_trace"]["p90"]),
                ],
                [
                    "Traces with LLM fraction >= 80%",
                    format_ratio(summary["llm_fraction_ge_80pct_trace_fraction"]),
                ],
                [
                    "Median alternation coverage",
                    format_ratio(summary["alternation_coverage_stats"]["p50"]),
                ],
            ],
        ),
        "",
        "## By Workload",
        "",
        markdown_table(
            [
                "Workload",
                "Traces",
                "LLM wait",
                "Tool time",
                "Stage-pattern traces",
                "Tool calls",
                "LLM wait total",
                "Tool total",
            ],
            dataset_rows,
        ),
        "",
        "## Tool-Time Contributors",
        "",
        markdown_table(["Tool", "Calls", "Total", "Mean", "P50", "P95", "Max"], tool_rows),
        "",
        "## LLM-Dominant Multi-Step Examples",
        "",
        markdown_table(
            ["Workload", "Task", "LLM wait", "Tool time", "LLM fraction", "Waits/calls"],
            [
                [
                    record["dataset_label"],
                    record["task"],
                    format_seconds(record["llm_wait_time_s"]),
                    format_seconds(record["tool_time_s"]),
                    format_ratio(record["llm_fraction_of_observed_stage_time"]),
                    f"{record['post_tool_wait_count']}/{record['tool_call_count']}",
                ]
                for record in top_llm
            ],
        ),
        "",
        "## Tool-Dominant Multi-Step Examples",
        "",
        markdown_table(
            ["Workload", "Task", "LLM wait", "Tool time", "Tool fraction", "Waits/calls"],
            [
                [
                    record["dataset_label"],
                    record["task"],
                    format_seconds(record["llm_wait_time_s"]),
                    format_seconds(record["tool_time_s"]),
                    format_ratio(record["tool_fraction_of_observed_stage_time"]),
                    f"{record['post_tool_wait_count']}/{record['tool_call_count']}",
                ]
                for record in top_tool
            ],
        ),
        "",
        "## Method",
        "",
        "- Tool stage: `tool_calls.json` interval from `timestamp` to `end_timestamp`.",
        "- LLM wait stage: interval from a `user` event containing `tool_result` to the next `assistant` event.",
        "- Initial LLM wait: interval from the first non-tool user prompt to the next assistant event.",
        "- The ratio is `LLM wait / (LLM wait + local tool time)`, so it measures classified stage time rather than every byte of end-to-end wall time.",
        "",
        "## Caveats",
        "",
        "- The workload is SWE-rebench/SWE-bench-style coding tasks, not a generic sample of all agent workloads.",
        "- The LLM-wait interval includes provider latency, queueing, network time, and model inference. It is still the interval where the sandbox is mostly waiting on the LLM side.",
        "- Tool duration is wall-clock tool wrapper time. Long `Bash` calls can include test execution, build time, or command wait time.",
        "- `Task` tool calls can contain nested agent activity, so treating them as pure local tool work is conservative for Caden.",
        "- External SWE-agent trajectories without timestamps are useful for action sequences, but not for this timing ratio.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_TRACE_ROOT)
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "doc" / "eval")
    args = parser.parse_args()

    root = args.root
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    trace_paths = sorted(root.glob("**/attempt_*/trace.jsonl"))
    records = [analyze_trace(root, trace_path) for trace_path in trace_paths]
    summary = aggregate_records(records)
    by_dataset = group_by_dataset(records)
    true_tool_durations = collect_tool_durations(records)
    tool_stats = {tool: summarize(values) for tool, values in sorted(true_tool_durations.items())}

    json_path = out_dir / "local-trace-stage-summary.json"
    csv_path = out_dir / "local-trace-stage-per-trace.csv"
    report_path = out_dir / "local-trace-stage-report.md"

    json_path.write_text(
        json.dumps(
            {
                "summary": summary,
                "by_dataset": by_dataset,
                "tool_stats": tool_stats,
                "records": records,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    write_csv(records, csv_path)
    write_report(report_path, root, summary, by_dataset, tool_stats, records)

    print(f"Wrote {report_path}")
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    print(
        "LLM wait fraction: "
        f"{format_ratio(summary['weighted_llm_fraction_of_observed_stage_time'])}; "
        f"tool fraction: {format_ratio(summary['weighted_tool_fraction_of_observed_stage_time'])}; "
        f"stage-pattern traces: {summary['traces_with_stage_pattern']}/{summary['valid_trace_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
