#!/usr/bin/env python3
"""Deterministically expand a normalized trajectory manifest to a fixed queue."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "caden-tool-trajectory-manifest-v1"
WORKLOAD_SCHEMA = "caden-tool-trajectory-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--count", type=positive_int, required=True)
    parser.add_argument(
        "--template-index",
        type=nonnegative_int,
        help="repeat only this zero-based source entry instead of round-robin expansion",
    )
    return parser.parse_args()


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return parsed


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def encoded(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def checked_entries(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = root / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema") not in {MANIFEST_SCHEMA, MANIFEST_SCHEMA.replace("caden-", "orca-", 1)}:
        raise ValueError(f"invalid manifest schema: {manifest_path}")
    entries = manifest.get("workloads")
    if not isinstance(entries, list) or not entries:
        raise ValueError("source manifest has no workloads")

    checked: list[dict[str, Any]] = []
    resolved_root = root.resolve()
    for entry in entries:
        path = (root / entry["path"]).resolve()
        if resolved_root not in path.parents:
            raise ValueError(f"unsafe workload path: {path}")
        payload = path.read_bytes()
        if digest(payload) != entry["sha256"]:
            raise ValueError(f"workload checksum mismatch: {path}")
        workload = json.loads(payload)
        if workload.get("schema") not in {WORKLOAD_SCHEMA, WORKLOAD_SCHEMA.replace("caden-", "orca-", 1)}:
            raise ValueError(f"invalid workload schema: {path}")
        if workload.get("tool_count") != entry.get("tool_count"):
            raise ValueError(f"tool count mismatch: {path}")
        checked.append(
            {
                "entry": entry,
                "workload": workload,
                "sha256": digest(payload),
            }
        )
    manifest["_source_manifest_sha256"] = digest(manifest_bytes)
    return manifest, checked


def expand(
    source_root: Path,
    output_root: Path,
    count: int,
    template_index: int | None,
) -> dict[str, Any]:
    if output_root.exists():
        raise FileExistsError(f"refusing to overwrite {output_root}")
    manifest, sources = checked_entries(source_root)
    if template_index is not None and template_index >= len(sources):
        raise ValueError(
            f"template index {template_index} is outside 0..{len(sources) - 1}"
        )

    workload_root = output_root / "workloads"
    workload_root.mkdir(parents=True)
    entries: list[dict[str, Any]] = []
    operation_counts: Counter[str] = Counter()

    for queue_index in range(count):
        source_index = template_index if template_index is not None else queue_index % len(sources)
        assert source_index is not None
        source = sources[source_index]
        workload = copy.deepcopy(source["workload"])
        workload["schema"] = WORKLOAD_SCHEMA
        original_source = workload["source"]
        original_trajectory = str(original_source["trajectory_id"])
        original_instance = str(original_source["instance_id"])
        suffix = f"queue-{queue_index:04d}"
        trajectory_id = f"{original_trajectory}::{suffix}"
        instance_id = f"{original_instance}::{suffix}"
        original_source.update(
            {
                "trajectory_id": trajectory_id,
                "instance_id": instance_id,
                "replica_of_trajectory_id": original_trajectory,
                "replica_of_instance_id": original_instance,
                "fixed_queue_index": queue_index,
                "fixed_queue_source_index": source_index,
                "fixed_queue_source_sha256": source["sha256"],
            }
        )
        for operation, value in workload.get("operation_counts", {}).items():
            operation_counts[str(operation)] += int(value)

        relative_path = f"workloads/workload-{queue_index:04d}.json"
        payload = encoded(workload)
        (output_root / relative_path).write_bytes(payload)
        entries.append(
            {
                "instance_id": instance_id,
                "path": relative_path,
                "repo": source["entry"]["repo"],
                "sha256": digest(payload),
                "tool_count": workload["tool_count"],
                "trajectory_id": trajectory_id,
            }
        )

    source_manifest_sha256 = manifest.pop("_source_manifest_sha256")
    expanded_manifest = copy.deepcopy(manifest)
    expanded_manifest["schema"] = MANIFEST_SCHEMA
    expanded_manifest.update(
        {
            "workload_count": count,
            "instance_count": count,
            "repository_count": len({entry["repo"] for entry in entries}),
            "operation_counts": dict(sorted(operation_counts.items())),
            "workloads": entries,
            "expansion": {
                "schema": "caden-fixed-queue-expansion-v1",
                "method": "single-template" if template_index is not None else "round-robin",
                "count": count,
                "template_index": template_index,
                "source_manifest_sha256": source_manifest_sha256,
                "source_workload_count": len(sources),
                "content_changes": "schema/identity metadata only; events are byte-for-byte equivalent JSON values",
            },
        }
    )
    manifest_payload = encoded(expanded_manifest)
    (output_root / "manifest.json").write_bytes(manifest_payload)

    checksum_lines = [f"{digest(manifest_payload)}  manifest.json"]
    checksum_lines.extend(
        f"{entry['sha256']}  {entry['path']}" for entry in entries
    )
    (output_root / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n")
    return expanded_manifest


def main() -> int:
    args = parse_args()
    manifest = expand(
        args.input_dir,
        args.output_dir,
        args.count,
        args.template_index,
    )
    print(
        json.dumps(
            {
                "output": str(args.output_dir),
                "workload_count": manifest["workload_count"],
                "operation_counts": manifest["operation_counts"],
                "expansion": manifest["expansion"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
