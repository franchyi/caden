"""Validate a SWE-rebench instance by applying its gold `patch` + `test_patch` to a fresh
checkout and checking FAIL_TO_PASS flips green (a false result means a broken/mismatched
instance). Run on a Linux node: `uv run python gold_check.py <instance_id> ...`.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import datasets

from agent_pipeline.scoring import is_resolved, run_tests
from fetch_task import DATASET, SPLIT, _as_list


def _run(cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def gold_check(row) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="gold-"))
    try:
        repo = tmp / "repo"
        _run(["git", "clone", f"https://github.com/{row['repo']}.git", str(repo)])
        _run(["git", "checkout", row["base_commit"]], cwd=repo)
        _run(["uv", "venv", "--python", "/usr/bin/python3", str(repo / ".venv")])
        py = str(repo / ".venv" / "bin" / "python")
        extra = os.environ.get("EXTRA_PIP", "").split()  # era-appropriate dep pins, e.g. "click<8.2"
        _run(["uv", "pip", "install", "--python", py, "-e", "."] + extra, cwd=repo)
        _run(["uv", "pip", "install", "--python", py, "pytest"], cwd=repo)

        applied = {}
        for field in ("patch", "test_patch"):  # gold solution, then gold tests
            (repo / ".gold.diff").write_text(row[field])
            r = _run(["git", "apply", str(repo / ".gold.diff")], cwd=repo, check=False)
            applied[field] = "ok" if r.returncode == 0 else r.stderr.strip()[:150]

        f2p = run_tests(str(repo), py, _as_list(row["FAIL_TO_PASS"]))
        p2p = run_tests(str(repo), py, _as_list(row["PASS_TO_PASS"]))
        return {
            "instance": row["instance_id"],
            "applied": applied,
            "gold_resolves": is_resolved(f2p, p2p),
            "f2p_failed": [k for k, v in f2p.items() if v != 0],
            "p2p_failed": [k for k, v in p2p.items() if v != 0],
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    ids = list(sys.argv[1:])
    ds = datasets.load_dataset(DATASET, split=SPLIT)
    rows = {r["instance_id"]: r for r in ds.filter(lambda r: r["instance_id"] in set(ids))}
    for iid in ids:
        print(json.dumps(gold_check(rows[iid])), flush=True)
