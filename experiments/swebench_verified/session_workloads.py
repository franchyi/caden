"""Derive independent serving sessions without changing SWE tool/wait events."""
import argparse
import copy
import hashlib
import json
import math
import random
from pathlib import Path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_arrivals(workloads, manifest, root=None):
    plan = manifest.get("orchestration", {})
    if plan.get("schema") != "crate-swe-session-serving-v1":
        raise ValueError("arrival-driven mode requires a serving manifest")
    if plan.get("sessions") != len(workloads):
        raise ValueError("serving session count differs from immutable manifest")
    previous = -1
    identities = set()
    for workload in workloads:
        offset = workload.get("arrival_offset_ms")
        if (isinstance(offset, bool) or not isinstance(offset, (int, float))
                or not math.isfinite(offset) or offset < 0 or offset < previous):
            raise ValueError("arrivals must be finite, nonnegative and ordered")
        previous = offset
        identity = workload["source"]["trajectory_id"]
        if identity in identities:
            raise ValueError("replicas must have unique trajectory identities")
        identities.add(identity)
        if root is not None:
            replica = workload["replica"]
            path = (root / replica["source_file"]).resolve()
            if root.resolve() not in path.parents:
                raise ValueError("unsafe replica source path")
            raw = path.read_bytes()
            if digest(raw) != replica["source_sha256"]:
                raise ValueError("replica source checksum mismatch")
            original = json.loads(raw)
            if any(workload[key] != original[key] for key in (
                    "events", "base", "tool_count", "tool_execution", "fingerprint_expected")):
                raise ValueError("replication changed real work or prepared base")
            if workload["source"]["instance_id"] != original["source"]["instance_id"]:
                raise ValueError("replication changed source instance")


def build(source, target, *, indices=(0, 1, 2, 3), replicas=8, spacing_ms=2000, seed=20260921):
    if target.exists() or target.is_symlink():
        raise ValueError("refusing existing derived workload directory")
    if (replicas < 1 or not indices or len(set(indices)) != len(indices)
            or not math.isfinite(spacing_ms) or spacing_ms < 0):
        raise ValueError("invalid session design")
    raw_manifest = (source / "manifest.json").read_bytes()
    original = json.loads(raw_manifest)
    if any(i < 0 or i >= len(original["workloads"]) for i in indices):
        raise ValueError("invalid source index")
    sessions, selected = [], []
    originals = {}
    for index in indices:
        row = original["workloads"][index]
        path = (source / row["path"]).resolve()
        if source.resolve() not in path.parents:
            raise ValueError("unsafe source path")
        raw = path.read_bytes()
        if digest(raw) != row["sha256"]:
            raise ValueError("source checksum mismatch")
        workload = json.loads(raw)
        source_file = f"source-traces/source-{index:03d}.json"
        originals[source_file] = raw
        selected.append({"index": index, "instance_id": workload["source"]["instance_id"],
                         "base": workload["base"], "source_sha256": row["sha256"],
                         "capture_sha256": row["capture_sha256"]})
        for number in range(replicas):
            session = copy.deepcopy(workload)
            session["schema"] = "caden-tool-trajectory-v1"
            session["source"]["trajectory_id"] += f"--replica-{number:03d}"
            session["replica"] = {"source_index": index, "replica_index": number,
                "source_file": source_file, "source_sha256": row["sha256"]}
            sessions.append((session, row))
    random.Random(seed).shuffle(sessions)
    target.mkdir(parents=True)
    for relative, raw in originals.items():
        (target / relative).parent.mkdir(exist_ok=True)
        (target / relative).write_bytes(raw)
    (target / "source-manifest.json").write_bytes(raw_manifest)
    entries, workloads = [], []
    for sequence, (workload, source_row) in enumerate(sessions):
        workload["arrival_offset_ms"] = sequence * spacing_ms
        name = f"session-{sequence:03d}.json"
        raw = (json.dumps(workload, indent=2) + "\n").encode()
        (target / name).write_bytes(raw)
        entries.append({"path": name, "sha256": digest(raw), "tool_count": workload["tool_count"],
                        "capture_sha256": source_row["capture_sha256"]})
        workloads.append(workload)
    manifest = {"schema": "caden-tool-trajectory-manifest-v1", "source": original["source"],
        "conversion": original["conversion"], "workloads": entries,
        "orchestration": {"schema": "crate-swe-session-serving-v1", "sessions": len(sessions),
            "unique_source_tasks": len(indices), "replicas_per_source": replicas,
            "source_manifest_sha256": digest(raw_manifest), "selected_sources": selected,
            "seed": seed, "arrival_spacing_ms": spacing_ms, "wait_scale": 1,
            "selection": "explicit source indices declared before performance measurement",
            "scope": "controlled repeated-attempt serving; replicas are not distinct SWE tasks",
            "timing": "fixed session arrivals; original closed-loop per-session waits"}}
    validate_arrivals(workloads, manifest, target)
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest["orchestration"]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--indices", default="0,1,2,3")
    ap.add_argument("--replicas", type=int, default=8)
    ap.add_argument("--spacing-ms", type=float, default=2000)
    ap.add_argument("--seed", type=int, default=20260921)
    a = ap.parse_args()
    print(json.dumps(build(a.source, a.output, indices=tuple(map(int, a.indices.split(','))),
                           replicas=a.replicas, spacing_ms=a.spacing_ms, seed=a.seed), indent=2))
