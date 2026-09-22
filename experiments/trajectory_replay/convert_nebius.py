#!/usr/bin/env python3
"""Convert Nebius OpenHands rows into deterministic sandbox tool workloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "caden-tool-trajectory-v1"
MANIFEST_SCHEMA = "caden-tool-trajectory-manifest-v1"
SOURCE_DATASET = "nebius/SWE-rebench-openhands-trajectories"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=Path,
        action="append",
        required=True,
        help="Hugging Face rows API JSON; may be repeated",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--max-workloads", type=positive_int, default=32)
    parser.add_argument("--max-tools", type=positive_int, default=12)
    parser.add_argument("--wait-ms", type=positive_int, default=1000)
    parser.add_argument(
        "--wait-profile-ms",
        type=wait_profile,
        default=(),
        help=(
            "comma-separated deterministic heterogeneous waits; each bucket "
            "gets an opaque synthetic request class"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise SystemExit(f"refusing non-empty output directory: {args.output_dir}")
    rows = list(load_rows(args.input))
    workloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        trajectory_id = require_string(row, "trajectory_id")
        if trajectory_id in seen:
            continue
        seen.add(trajectory_id)
        workload = convert_row(
            row,
            max_tools=args.max_tools,
            wait_ms=args.wait_ms,
            wait_profile_ms=args.wait_profile_ms,
            source_revision=args.source_revision,
        )
        if workload["tool_count"] == 0:
            continue
        workloads.append(workload)
        if len(workloads) >= args.max_workloads:
            break
    if len(workloads) < args.max_workloads:
        raise SystemExit(
            f"only {len(workloads)} executable trajectories found; "
            f"requested {args.max_workloads}"
        )

    workload_dir = args.output_dir / "workloads"
    workload_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    operation_counts: Counter[str] = Counter()
    repositories: set[str] = set()
    instances: set[str] = set()
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
        repositories.add(workload["source"]["repo"])
        instances.add(workload["source"]["instance_id"])
        operation_counts.update(
            event["operation"]
            for event in workload["events"]
            if event["type"] == "tool"
        )

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "dataset": SOURCE_DATASET,
            "revision": args.source_revision,
            "input_files": [
                {
                    "path": str(path),
                    "sha256": sha256_file(path),
                }
                for path in args.input
            ],
        },
        "conversion": {
            "timing": (
                "synthetic-profile" if args.wait_profile_ms else "synthetic-fixed"
            ),
            "wait_ms": args.wait_ms,
            "wait_profile_ms": list(args.wait_profile_ms),
            "maximum_tools_per_trajectory": args.max_tools,
            "tool_execution": "deterministic-proxy",
            "anonymous_memory_injection": False,
        },
        "workload_count": len(entries),
        "instance_count": len(instances),
        "repository_count": len(repositories),
        "operation_counts": dict(sorted(operation_counts.items())),
        "workloads": entries,
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_bytes(canonical_json(manifest))
    checksums = [f"{entry['sha256']}  {entry['path']}" for entry in entries] + [
        f"{sha256_file(manifest_path)}  manifest.json"
    ]
    (args.output_dir / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
    print(
        json.dumps(
            {
                key: manifest[key]
                for key in (
                    "workload_count",
                    "instance_count",
                    "repository_count",
                    "operation_counts",
                )
            },
            indent=2,
        )
    )
    print(f"manifest written to {manifest_path}")
    return 0


def load_rows(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        value = json.loads(path.read_text())
        rows = value.get("rows") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            raise TypeError(f"{path}: expected Hugging Face rows object")
        for wrapper in rows:
            row = wrapper.get("row") if isinstance(wrapper, dict) else None
            if not isinstance(row, dict):
                raise TypeError(f"{path}: malformed row wrapper")
            yield row


def convert_row(
    row: dict[str, Any],
    *,
    max_tools: int,
    wait_ms: int,
    source_revision: str,
    wait_profile_ms: tuple[int, ...] = (),
) -> dict[str, Any]:
    trajectory = row.get("trajectory")
    if not isinstance(trajectory, list):
        raise TypeError("trajectory must be a list")
    events: list[dict[str, Any]] = []
    tool_sequence = 0
    profile = wait_profile_ms or (wait_ms,)
    trajectory_id = str(row.get("trajectory_id", ""))
    profile_offset = int(
        hashlib.sha256(trajectory_id.encode()).hexdigest()[:8], 16
    ) % len(profile)
    for message in trajectory:
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            normalized = normalize_call(call, tool_sequence)
            if normalized is None:
                continue
            profile_index = (profile_offset + tool_sequence) % len(profile)
            events.append(
                {
                    "type": "wait",
                    "duration_ms": profile[profile_index],
                    "timing_source": (
                        "synthetic-profile" if wait_profile_ms else "synthetic-fixed"
                    ),
                    "request_class": (
                        f"synthetic-class-{profile_index}"
                        if wait_profile_ms
                        else "default"
                    ),
                }
            )
            events.append(normalized)
            tool_sequence += 1
            if tool_sequence >= max_tools:
                break
        if tool_sequence >= max_tools:
            break
    source_projection = {
        key: row.get(key)
        for key in ("trajectory_id", "instance_id", "repo", "resolved", "exit_status")
    }
    return {
        "schema": SCHEMA,
        "source": source_projection
        | {
            "dataset": SOURCE_DATASET,
            "revision": source_revision,
            "row_sha256": hashlib.sha256(canonical_json(row)).hexdigest(),
        },
        "timing": {
            "kind": ("synthetic-profile" if wait_profile_ms else "synthetic-fixed"),
            "wait_ms": wait_ms,
            "wait_profile_ms": list(wait_profile_ms),
            "request_class_source": (
                "synthetic-profile-bucket" if wait_profile_ms else "constant"
            ),
            "source_has_timestamps": False,
        },
        "tool_execution": {
            "kind": "deterministic-proxy",
            "workspace": "bench-medium corpus",
            "anonymous_memory_injection": False,
        },
        "tool_count": tool_sequence,
        "events": events,
    }


def normalize_call(call: Any, sequence: int) -> dict[str, Any] | None:
    if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
        return None
    function = call["function"]
    name = function.get("name")
    if not isinstance(name, str):
        return None
    arguments_text = function.get("arguments", "{}")
    if not isinstance(arguments_text, str):
        arguments_text = json.dumps(arguments_text, sort_keys=True)
    try:
        arguments = json.loads(arguments_text)
    except json.JSONDecodeError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    operation = classify_operation(name, arguments)
    if operation is None:
        return None
    command = proxy_command(operation, sequence)
    preview_source = arguments.get("command") or arguments.get("path") or arguments_text
    preview = re.sub(r"\s+", " ", str(preview_source)).strip()[:160]
    return {
        "type": "tool",
        "sequence": sequence,
        "source_tool": name,
        "source_call_id": call.get("id"),
        "source_arguments_sha256": hashlib.sha256(arguments_text.encode()).hexdigest(),
        "source_preview": preview,
        "operation": operation,
        "argv": ["sh", "-lc", command],
        "expected_exit_code": 0,
    }


def classify_operation(name: str, arguments: dict[str, Any]) -> str | None:
    if name == "str_replace_editor":
        command = str(arguments.get("command", ""))
        if command == "view":
            return "view"
        if command in {"create", "str_replace", "insert", "undo_edit"}:
            return "edit"
        return None
    if name not in {"execute_bash", "bash", "terminal"}:
        return None
    command = str(arguments.get("command", "")).lower()
    if re.search(r"\b(pytest|unittest|tox|jest|cargo test|go test)\b", command):
        return "test"
    if re.search(
        r"\b(pip install|npm install|make|cmake|cargo build|go build)\b", command
    ):
        return "build"
    if re.search(r"(>>?|sed\s+-i|\b(cp|mv|rm|mkdir|touch)\b)", command):
        return "edit"
    if re.search(
        r"\b(rg|grep|find|sed|cat|head|tail|ls|git (diff|show|log|status))\b", command
    ):
        return "search"
    return "shell"


def proxy_command(operation: str, sequence: int) -> str:
    if operation == "view":
        return (
            "find repository/dependencies -type f | sort | sed -n '1,512p' "
            "| xargs -r cat >/dev/null"
        )
    if operation == "search":
        return "grep -R -a -l 'mnop' repository/dependencies >/dev/null"
    if operation == "test":
        return (
            "large=$(find repository/large -type f | sort | head -1); "
            'test -n "$large"; dd if="$large" of=/dev/null bs=1M count=64 status=none'
        )
    if operation == "build":
        return (
            "mkdir -p repository/replay; "
            "large=$(find repository/large -type f | sort | head -1); "
            'test -n "$large"; dd if="$large" '
            "of=repository/replay/build.bin bs=1M count=8 conv=fsync status=none"
        )
    if operation == "edit":
        return (
            "mkdir -p repository/replay; "
            f"printf '%08d\\n' {sequence} >> repository/replay/edits.log; "
            f"dd if=/dev/zero of=repository/replay/edit-{sequence:04d}.bin "
            "bs=4096 count=1 conv=fsync status=none"
        )
    if operation == "shell":
        return (
            "find repository -maxdepth 4 -type f -printf '%p %s\\n' "
            "| sort | sha256sum >/dev/null"
        )
    raise ValueError(f"unsupported operation {operation!r}")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def require_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"row lacks {key!r}")
    return item


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def wait_profile(value: str) -> tuple[int, ...]:
    try:
        parsed = tuple(int(item) for item in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "wait profile must be comma-separated integers"
        ) from error
    if not parsed or any(item <= 0 for item in parsed):
        raise argparse.ArgumentTypeError("all wait-profile values must be positive")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
