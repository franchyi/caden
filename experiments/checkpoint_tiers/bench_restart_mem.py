#!/usr/bin/env python3
"""Restart latency for a sandbox that HELD N MiB: teardown (SIGKILL bwrap -> holder gone, memory freed)
plus the size-independent relaunch. Shows destroy/reconstruct is cheap because it discards memory
rather than moving it — contrast with CRIU, which must write/read every page. Run unprivileged."""

import json
import os
import statistics
import subprocess
import sys
import time

MIB = int(sys.argv[1]) if len(sys.argv) > 1 else 512
WORK = f"/tmp/caden-restartmem-{MIB}"
N = 20

BASE = ["bwrap", "--unshare-user-try", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin", "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64", "--ro-bind", "/etc", "/etc",
        "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev", "--new-session"]
HOLDER = ("import sys,time\n"
          "n=%d*1024*1024\n"
          "b=bytearray(n)\n"
          "for i in range(0,n,4096): b[i]=1\n"
          "sys.stdout.write('READY\\n'); sys.stdout.flush()\n"
          "time.sleep(36000)\n")

os.makedirs(WORK, exist_ok=True)
TAG = f"rstmem-{MIB}"


def gone():
    return subprocess.run(["pgrep", "-f", TAG], stdout=subprocess.DEVNULL).returncode != 0


def one_cycle():
    out = open(f"{WORK}/out", "w")
    t0 = time.monotonic_ns()
    p = subprocess.Popen(BASE + ["python3", "-c", HOLDER % MIB, TAG], stdout=out, stderr=subprocess.STDOUT)
    while "READY" not in open(f"{WORK}/out").read():
        if time.monotonic_ns() - t0 > 30e9:
            raise SystemExit("no READY")
    t_ready = time.monotonic_ns()
    # Kill the whole sandbox: TAG is in both the bwrap monitor's and the holder's cmdline (killing the
    # monitor alone leaves the holder reparented and alive).
    subprocess.run(["pkill", "-9", "-f", TAG], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.wait()
    while not gone():
        time.sleep(0.0005)
        if time.monotonic_ns() - t_ready > 10e9:
            break
    t_dead = time.monotonic_ns()
    return (t_ready - t0) / 1e6, (t_dead - t_ready) / 1e6


def stat(xs):
    xs = sorted(xs)
    return {"median": round(statistics.median(xs), 2), "p90": round(xs[int(len(xs) * 0.9)], 2),
            "min": round(min(xs), 2), "max": round(max(xs), 2)}


la, td = [], []
for _ in range(N):
    a, b = one_cycle()
    la.append(a)
    td.append(b)
print(json.dumps({"mib": MIB, "relaunch_plus_realloc_ms": stat(la), "teardown_ms": stat(td)}, indent=2))
