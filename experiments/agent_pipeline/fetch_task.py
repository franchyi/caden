"""Materialize a SWE-rebench instance into work/<id>/: clone the repo at its base commit,
build a venv, and write task.json. Run on a Linux node (needs git, uv, and network)."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import datasets

DATASET = "nebius/SWE-rebench"
SPLIT = "test"


def _run(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True)


def _as_list(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return [v]
    return list(v)


def fetch(instance_id: str, workdir: Path) -> dict:
    workdir = Path(workdir).resolve()  # absolute so cwd-relative subprocs resolve
    # Non-streaming load caches the split once then filters instantly (a streaming find would
    # scan nearly all rows, since 'simonw/...' sorts late in the dataset).
    ds = datasets.load_dataset(DATASET, split=SPLIT)
    matches = ds.filter(lambda r: r["instance_id"] == instance_id)
    if len(matches) == 0:
        raise SystemExit(f"instance {instance_id} not found in {DATASET}:{SPLIT}")
    row = matches[0]

    if workdir.exists():
        shutil.rmtree(workdir)  # idempotent: clear any partial prior fetch
    workdir.mkdir(parents=True, exist_ok=True)
    repo_dir = workdir / "repo"
    _run(["git", "clone", f"https://github.com/{row['repo']}.git", str(repo_dir)])
    _run(["git", "checkout", row["base_commit"]], cwd=repo_dir)

    venv = repo_dir / ".venv"
    # Build from a system python under /usr (the sandbox bind-mounts /usr; a uv-managed python would
    # be a dangling symlink inside the sandbox). Override with VENV_PYTHON_BIN for a specific version.
    python_bin = os.environ.get("VENV_PYTHON_BIN", "/usr/bin/python3")
    _run(["uv", "venv", "--python", python_bin, str(venv)])
    py = str(venv / "bin" / "python")
    pip = ["uv", "pip", "install", "--python", py]
    # EXTRA_PIP pins era-appropriate deps the instance was authored against (e.g. "click<8.2"),
    # since the latest deps on a modern Python can break the instance's own tests.
    extra = os.environ.get("EXTRA_PIP", "").split()
    _run(pip + ["-e", "."] + extra, cwd=repo_dir)
    _run(pip + ["pytest"], cwd=repo_dir)

    task = {
        "instance_id": instance_id,
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "problem_statement": row["problem_statement"],
        "test_patch": row["test_patch"],
        "fail_to_pass": _as_list(row["FAIL_TO_PASS"]),
        "pass_to_pass": _as_list(row["PASS_TO_PASS"]),
        "repo_dir": str(repo_dir),
        "venv_python": py,
    }
    (workdir / "task.json").write_text(json.dumps(task, indent=2))
    return task


if __name__ == "__main__":
    fetch(sys.argv[1], Path(sys.argv[2]))
    print(f"fetched {sys.argv[1]} -> {sys.argv[2]}")
