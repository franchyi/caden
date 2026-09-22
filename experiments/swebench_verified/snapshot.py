#!/usr/bin/env python3
"""Generate a deployable exact-source artifact without committing user work."""
import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    if a.output.exists():
        raise SystemExit("refusing existing source snapshot")
    a.output.mkdir(parents=True)
    hashes = {}
    for relative in ["src", "experiments/agent_pipeline", "experiments/sandboxfs_memory", "experiments/trajectory_replay", "experiments/swebench_verified", "experiments/cxl_tiering", "tests", "native/cxl_coldstore"]:
        for file in sorted((ROOT / relative).rglob("*")):
            if not file.is_file() or (file.suffix not in {".py", ".sh", ".json", ".c", ".h", ".md"} and file.name not in {"bwrap-wrapper", "Makefile", "LICENSE"}):
                continue
            if any(p in {"__pycache__", "traces", ".pytest_cache", "build", ".venv", "venv", ".git"} for p in file.parts):
                continue
            rel = file.relative_to(ROOT)
            target = a.output / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
            hashes[str(rel)] = hashlib.sha256(file.read_bytes()).hexdigest()
    patch = subprocess.check_output(["git", "-C", str(ROOT), "diff", "HEAD", "--"])
    (a.output / "tracked-changes.patch").write_bytes(patch)
    provenance = {"caden_base_commit": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
                  "branch": subprocess.check_output(["git", "-C", str(ROOT), "branch", "--show-current"], text=True).strip(),
                  "sandboxfs_commit": "652aa279bbb2afb4068d4b838e2df8e103b247fe",
                  "uncommitted_changes": True, "source_sha256": hashes,
                  "source_manifest_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}
    (a.output / "SOURCE_PROVENANCE.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(provenance["source_manifest_sha256"])


if __name__ == "__main__":
    main()
