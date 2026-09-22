#!/usr/bin/env python3
"""Write RUN.json and the final SHA256SUMS for one result package (run last)."""
import argparse
import hashlib
import json
import os
import time
from pathlib import Path

if __package__:
    from .provenance import configuration_order
else:
    from provenance import configuration_order

ap = argparse.ArgumentParser()
ap.add_argument("--results", type=Path, required=True)
ap.add_argument("--run-id", required=True)
ap.add_argument("--local-package", required=True)
a = ap.parse_args()
results = a.results.resolve()
provenance = json.loads((results / "provenance" / "SOURCE_PROVENANCE.json").read_text())
status = json.loads((results / "STATUS.json").read_text()) if (results / "STATUS.json").exists() else {}
order = configuration_order(status)
run = {
    "schema": "crate-tiering-run-v1",
    "run_id": a.run_id,
    "host": os.uname().nodename,
    "kernel": os.uname().release,
    "remote_work_directory": str(results.parent),
    "remote_results": str(results),
    "local_package": a.local_package,
    "commits": {"caden_branch": provenance["branch"], "caden_head": provenance["caden"] if "caden" in provenance else provenance["orca"],
                "starting_commit": provenance["starting_commit"], "uncommitted_implementation": provenance["dirty"],
                "exact_diff": "provenance/implementation.diff",
                "source_manifest_sha256": provenance["source_manifest_sha256"],
                "sandboxfs_submodule": provenance["sandboxfs"]},
    "configuration_order": order,
    "configuration_argv": {label: f"configs/{label}.argv.json" for label in order},
    "inputs": {"traces": "32 recorded SWE-bench real-command traces (normalized-formal), 1x recorded waits, no model calls",
               "prepared_bases_and_rootfs": "/sandboxfs/crate-swebench-20260919 (read-only; digests in preflight/bases.json)"},
    "evidence_label": "one ordered run per configuration; descriptive, not density-at-SLO, replication or hardware causality",
    "finalized_unix": time.time(),
}
(results / "RUN.json").write_text(json.dumps(run, indent=2) + "\n")
lines = []
for path in sorted(results.rglob("*")):
    if path.is_file() and not path.is_symlink() and path.name != "SHA256SUMS":
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        lines.append(f"{digest.hexdigest()}  {path.relative_to(results)}")
(results / "SHA256SUMS").write_text("\n".join(lines) + "\n")
print(f"{len(lines)} files in SHA256SUMS")
