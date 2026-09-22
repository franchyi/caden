#!/usr/bin/env python3
"""Verify CoW and /tmp isolation outside measured workload intervals."""
import argparse
import json
import time
from pathlib import Path

from prepare_remote import C, ctl


def command(task, sandbox, script):
    result = ctl(task, "exec-json", sandbox, "--", "/bin/bash", "-ec", script)
    if result.get("exit_code") != 0:
        raise RuntimeError(f"isolation command failed: {result}")
    return result


def verify(task):
    index = f"{task['sequence']:02d}"
    ids = [f"sv-isolation-{index}-{side}" for side in ("a", "b")]
    existing = ctl(task, "list")["sandboxes"]
    if existing:
        raise RuntimeError(f"unexpected active sandboxes: {existing}")
    created, records = [], []
    try:
        for sid in ids:
            records.append(ctl(task, "create", "--id", sid, "--base", task["base"], "--mode", "t1"))
            created.append(sid)
        history = "/workspace/dependencies/miniconda3/conda-meta/history"
        original = command(task, ids[1], f"sha256sum {history}")["stdout"].split()[0]
        records.append(command(task, ids[0], f"printf 'private-a\\n' > /workspace/.crate-isolation; printf 'private-a\\n' > /tmp/.crate-isolation; printf '\\n# private-a\\n' >> {history}"))
        records.append(command(task, ids[1], f"test ! -e /workspace/.crate-isolation; test ! -e /tmp/.crate-isolation; test \"$(sha256sum {history} | cut -d' ' -f1)\" = {original}; printf 'private-b\\n' > /workspace/.crate-isolation; printf 'private-b\\n' > /tmp/.crate-isolation"))
        records.append(command(task, ids[0], f"test \"$(cat /workspace/.crate-isolation)\" = private-a; test \"$(cat /tmp/.crate-isolation)\" = private-a; test \"$(sha256sum {history} | cut -d' ' -f1)\" != {original}"))
    finally:
        for sid in reversed(created):
            records.append(ctl(task, "destroy", sid))
    base = ctl(task, "base-verify", task["base"])
    if not base.get("match"):
        raise RuntimeError(f"base changed: {task['instance_id']}")
    return {"task": task, "records": records, "base_after": base, "success": True, "finished_unix": time.time()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=32)
    ap.add_argument("--output-dir", type=Path, default=C / "artifacts")
    a = ap.parse_args()
    a.output_dir.mkdir(parents=True, exist_ok=True)
    tasks = json.loads((C / "selection/manifest.json").read_text())["tasks"][:a.limit]
    for task in tasks:
        receipt = a.output_dir / f"isolation-{task['sequence']:02d}.json"
        if receipt.exists():
            raise RuntimeError(f"refusing to overwrite previous isolation evidence: {receipt}")
        result = verify(task)
        receipt.write_text(json.dumps(result, indent=2) + "\n")
        print("ISOLATION_OK", task["instance_id"], flush=True)
    (a.output_dir / f"ISOLATION_{len(tasks)}_COMPLETE.json").write_text(json.dumps({"tasks": len(tasks), "finished_unix": time.time()}) + "\n")


if __name__ == "__main__":
    main()
