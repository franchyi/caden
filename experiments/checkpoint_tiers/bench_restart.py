#!/usr/bin/env python3
"""Measure sandbox restart latency: the wall-clock to construct a fresh bwrap sandbox, run a trivial
command, and tear it down. This is Caden's destroy/reconstruct 'restore' cost for a stateless sandbox,
and isolates bwrap's overhead by comparing against the bare command."""

import json
import os
import statistics
import subprocess
import sys
import time

WORK = sys.argv[1] if len(sys.argv) > 1 else "/tmp/caden-restart"
N = 50
WARMUP = 5

BASE = [
    "bwrap", "--unshare-user-try", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
    "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
    "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64", "--ro-bind", "/etc", "/etc",
    "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev", "--new-session",
    "--bind", WORK, WORK, "--chdir", WORK,
]


def bench(argv, n=N):
    for _ in range(WARMUP):
        subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ts = []
    for _ in range(n):
        t0 = time.monotonic_ns()
        subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ts.append((time.monotonic_ns() - t0) / 1e6)
    ts.sort()
    return {"median": round(statistics.median(ts), 3), "p90": round(ts[int(n * 0.9)], 3),
            "min": round(min(ts), 3), "max": round(max(ts), 3), "mean": round(statistics.mean(ts), 3)}


os.makedirs(WORK, exist_ok=True)
out = {
    "bare_true": bench(["/usr/bin/true"]),
    "bare_python": bench(["python3", "-c", "pass"]),
    "bwrap_true": bench(BASE + ["/usr/bin/true"]),
    "bwrap_python": bench(BASE + ["python3", "-c", "pass"]),
}
out["bwrap_overhead_true_ms"] = round(out["bwrap_true"]["median"] - out["bare_true"]["median"], 3)
out["bwrap_overhead_python_ms"] = round(out["bwrap_python"]["median"] - out["bare_python"]["median"], 3)
print(json.dumps(out, indent=2))
