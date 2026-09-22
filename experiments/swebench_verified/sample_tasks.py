#!/usr/bin/env python3
"""Outcome-blind, pinned SWE-bench Verified sampling; evaluator data stays separate."""
import argparse
import hashlib
import json
from pathlib import Path

DATASET = "princeton-nlp/SWE-bench_Verified"
REVISION = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"
SHA256 = "a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd"
SEED = "crate-verified-20260919-v1"
QUOTAS = [
    ("django", "django/django", 8),
    ("sympy", "sympy/sympy", 8),
    ("scientific", "astropy/astropy", 2),
    ("scientific", "matplotlib/matplotlib", 2),
    ("scientific", "scikit-learn/scikit-learn", 2),
    ("scientific", "pydata/xarray", 2),
    ("tools", "pytest-dev/pytest", 3),
    ("tools", "pylint-dev/pylint", 3),
    ("tools", "psf/requests", 2),
]


def rank(row):
    return hashlib.sha256((SEED + ":" + row["instance_id"]).encode()).hexdigest()


def sample(rows):
    if len(rows) != 500 or len({r["instance_id"] for r in rows}) != 500:
        raise ValueError("expected 500 unique Verified instances")
    buckets = []
    for family, repo, count in QUOTAS:
        selected = sorted((r for r in rows if r["repo"] == repo), key=rank)[:count]
        if len(selected) != count:
            raise ValueError(f"insufficient tasks for {repo}")
        buckets.append([dict(r, family=family) for r in selected])
    # Interleave families. The first four form an outcome-blind pilot.
    groups = [buckets[0], buckets[1], sum(buckets[2:6], []), sum(buckets[6:], [])]
    return [groups[g][i] for i in range(8) for g in range(4)]


def main():
    import pyarrow.parquet as pq
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    if hashlib.sha256(a.parquet.read_bytes()).hexdigest() != SHA256:
        raise ValueError("dataset checksum mismatch")
    rows = sample(pq.read_table(a.parquet).to_pylist())
    a.output.mkdir(parents=True, exist_ok=True)
    manifest = {"schema": "crate-swebench-verified-selection-v1", "dataset": DATASET,
                "revision": REVISION, "parquet_sha256": SHA256, "seed": SEED,
                "selection": "SHA256(seed:instance_id), quota by repository, independent of outcomes",
                "quotas": QUOTAS, "pilot_ids": [r["instance_id"] for r in rows[:4]], "tasks": []}
    for i, r in enumerate(rows):
        ident = r["instance_id"]
        d = a.output / "tasks" / ident
        d.mkdir(parents=True, exist_ok=True)
        public = {k: r[k] for k in ("instance_id", "repo", "base_commit", "version", "environment_setup_commit", "family")}
        public.update(sequence=i, base=f"sv-{i:02d}", socket=f"/run/crate-sv-{i:02d}.sock",
                      image="docker.io/swebench/sweb.eval.x86_64." + ident.lower().replace("__", "_1776_") + ":latest")
        manifest["tasks"].append(public)
        (d / "task.txt").write_text(r["problem_statement"] + "\n")
        (d / "metadata.json").write_text(json.dumps(public, indent=2) + "\n")
        # Not mounted inside agent workspaces or sent in model prompts.
        e = a.output / "evaluator-only" / ident
        e.mkdir(parents=True, exist_ok=True)
        (e / "record.json").write_text(json.dumps(r, indent=2) + "\n")
    (a.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"tasks": len(rows), "pilot": manifest["pilot_ids"]}, indent=2))


if __name__ == "__main__":
    main()
