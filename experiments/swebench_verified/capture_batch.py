#!/usr/bin/env python3
"""Bounded parallel model capture. Logs every task; never retries by solve outcome."""
import argparse
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int, required=True)
    a = ap.parse_args()
    tasks = json.loads((a.selection / "manifest.json").read_text())["tasks"][a.start:a.limit]
    a.output.mkdir(parents=True, exist_ok=True)
    logs = a.output / "logs"
    logs.mkdir(exist_ok=True)
    def capture(task):
        ident = task["instance_id"]
        argv = [sys.executable, str(Path(__file__).with_name("capture.py")), "--selection", str(a.selection),
                "--instance-id", ident, "--output", str(a.output / ident)]
        print("START", ident, flush=True)
        with (logs / (ident + ".log")).open("x") as log:
            result = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT)
        print("DONE", ident, result.returncode, flush=True)
        return {"instance_id": ident, "returncode": result.returncode}
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for f in concurrent.futures.as_completed([pool.submit(capture, t) for t in tasks]):
            results.append(f.result())
    (a.output / f"batch-{a.start}-{a.limit}.json").write_text(json.dumps(results, indent=2) + "\n")
    if any(r["returncode"] for r in results):
        raise SystemExit("capture infrastructure failures retained; do not silently exclude")


if __name__ == "__main__":
    main()
