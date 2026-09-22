#!/usr/bin/env python3
"""Convert measured no-tools mini-agent traces into deterministic replay workloads."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.trajectory_replay.convert_nebius import (
    MANIFEST_SCHEMA,
    SCHEMA,
    canonical_json,
    classify_operation,
    proxy_command,
    sha256_file,
)

SOURCE_KIND = "crate-pi-agent-trace-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-dir", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--provider", default="openai-codex")
    parser.add_argument("--model", default="gpt-5.6-terra")
    parser.add_argument("--request-class", default="terra-coding-v1")
    parser.add_argument("--restore-profile", default="full-touch")
    parser.add_argument("--allow-unresolved", action="store_true")
    return parser.parse_args()


def convert_trace(
    trace_dir: Path,
    *,
    provider: str,
    model: str,
    request_class: str,
    restore_profile: str,
    require_resolved: bool,
) -> dict[str, Any]:
    result_path = trace_dir / "results.json"
    agent_path = trace_dir / "agent_result.json"
    calls_path = trace_dir / "tool_calls.json"
    result = json.loads(result_path.read_text())
    agent = json.loads(agent_path.read_text())
    tool_calls = json.loads(calls_path.read_text())
    if result.get("schema") != SOURCE_KIND:
        raise ValueError(f"{trace_dir}: unsupported trace schema")
    if result.get("provider") != provider or result.get("model") != model:
        raise ValueError(f"{trace_dir}: provider/model mismatch")
    if require_resolved and not result.get("resolved"):
        raise ValueError(f"{trace_dir}: trace did not pass its grader")
    model_calls = result.get("model_calls")
    if not isinstance(model_calls, list) or not model_calls:
        raise ValueError(f"{trace_dir}: no model-call receipts")
    if any(
        call.get("provider") != provider or call.get("model") != model
        for call in model_calls
    ):
        raise ValueError(f"{trace_dir}: model-call provenance mismatch")

    tool_events: list[tuple[dict[str, Any], float]] = []
    pending_wait_seconds = 0.0
    for event in agent.get("stage_events", []):
        if event.get("stage") == "LLM_WAIT":
            pending_wait_seconds += float(event["end"]) - float(event["start"])
        elif event.get("stage") == "TOOL_BURST":
            tool_events.append((event, pending_wait_seconds))
            pending_wait_seconds = 0.0
    if pending_wait_seconds:
        raise ValueError(f"{trace_dir}: terminal LLM wait has no following tool call")
    if len(tool_events) != len(tool_calls):
        raise ValueError(
            f"{trace_dir}: {len(tool_events)} tool stages != {len(tool_calls)} calls"
        )

    events: list[dict[str, Any]] = []
    operation_counts: Counter[str] = Counter()
    for sequence, ((stage, wait_seconds), call) in enumerate(
        zip(tool_events, tool_calls, strict=True)
    ):
        step = int(stage["step"])
        duration_ms = max(1, round(wait_seconds * 1000))
        command = str(call.get("input", {}).get("command", ""))
        operation = classify_operation("bash", {"command": command}) or "shell"
        command_hash = hashlib.sha256(command.encode()).hexdigest()
        events.extend(
            [
                {
                    "type": "wait",
                    "duration_ms": duration_ms,
                    "timing_source": "measured-live-provider",
                    "request_class": request_class,
                    "restore_profile": restore_profile,
                    "source_step": step,
                },
                {
                    "type": "tool",
                    "sequence": sequence,
                    "source_tool": "Bash",
                    "source_call_id": call.get("id"),
                    "source_arguments_sha256": command_hash,
                    "source_preview": command.replace("\n", " ")[:160],
                    "operation": operation,
                    "argv": ["sh", "-lc", proxy_command(operation, sequence)],
                    "expected_exit_code": 0,
                },
            ]
        )
        operation_counts[operation] += 1

    instance_id = str(result["instance_id"])
    hashes = {
        path.name: sha256_file(path)
        for path in (result_path, agent_path, calls_path, trace_dir / "trace.jsonl")
    }
    return {
        "schema": SCHEMA,
        "source": {
            "dataset": SOURCE_KIND,
            "trajectory_id": instance_id,
            "instance_id": instance_id,
            "repo": "crate/terra-task-v1",
            "resolved": result.get("resolved"),
            "exit_status": result.get("exit_status"),
            "provider": provider,
            "model": model,
            "thinking": result.get("thinking"),
            "trace_hashes": hashes,
        },
        "timing": {
            "kind": "measured-live-provider",
            "provider": provider,
            "model": model,
            "clock": "mini-agent model query wall time",
            "wait_scale": 1.0,
            "request_class_source": "preregistered-opaque-task-class",
        },
        "tool_execution": {
            "kind": "deterministic-proxy",
            "workspace": "bench-medium corpus",
            "source_commands_executed_live": True,
            "anonymous_memory_injection": False,
        },
        "tool_count": len(tool_events),
        "operation_counts": dict(sorted(operation_counts.items())),
        "events": events,
    }


def main() -> int:
    args = parse_args()
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise SystemExit(f"refusing non-empty output directory: {args.output_dir}")
    workloads = [
        convert_trace(
            trace_dir,
            provider=args.provider,
            model=args.model,
            request_class=args.request_class,
            restore_profile=args.restore_profile,
            require_resolved=not args.allow_unresolved,
        )
        for trace_dir in args.trace_dir
    ]
    workload_dir = args.output_dir / "workloads"
    workload_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    operation_counts: Counter[str] = Counter()
    for index, workload in enumerate(workloads):
        path = workload_dir / f"workload-{index:04d}.json"
        payload = canonical_json(workload)
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        entries.append(
            {
                "path": str(path.relative_to(args.output_dir)),
                "sha256": digest,
                "trajectory_id": workload["source"]["trajectory_id"],
                "instance_id": workload["source"]["instance_id"],
                "repo": workload["source"]["repo"],
                "tool_count": workload["tool_count"],
            }
        )
        operation_counts.update(workload["operation_counts"])

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "dataset": SOURCE_KIND,
            "provider": args.provider,
            "model": args.model,
            "trace_directories": [str(path) for path in args.trace_dir],
        },
        "conversion": {
            "timing": "measured-live-provider",
            "wait_scale": 1.0,
            "request_class": args.request_class,
            "restore_profile": args.restore_profile,
            "tool_execution": "deterministic-proxy",
            "anonymous_memory_injection": False,
        },
        "workload_count": len(entries),
        "instance_count": len(entries),
        "repository_count": 1,
        "operation_counts": dict(sorted(operation_counts.items())),
        "workloads": entries,
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_bytes(canonical_json(manifest))
    checksums = [f"{entry['sha256']}  {entry['path']}" for entry in entries]
    checksums.append(f"{sha256_file(manifest_path)}  manifest.json")
    (args.output_dir / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
    print(
        json.dumps(
            {
                "provider": args.provider,
                "model": args.model,
                "workload_count": len(entries),
                "operation_counts": dict(sorted(operation_counts.items())),
                "manifest": str(manifest_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
