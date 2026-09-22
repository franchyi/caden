#!/usr/bin/env python3
"""Privileged 'Caden daemon' side: drive the cgroup v2 levers on a running sandbox cgroup and time
them. Emits JSON with freeze/thaw latency, memory.reclaim throughput, and the wake refault cost."""

import json
import os
import signal
import statistics
import sys
import time

cg = sys.argv[1]             # sandbox cgroup dir, e.g. /sys/fs/cgroup/caden_agent0
host_pid = int(sys.argv[2])  # host-visible pid of the workload, for SIGUSR1
result_path = sys.argv[3]    # workload's scan-result file (bind-mounted, host-readable)

FREEZE_ITERS = 20


def rd(name):
    with open(os.path.join(cg, name)) as f:
        return f.read().strip()


def wr(name, val):
    with open(os.path.join(cg, name), "w") as f:
        f.write(val)


def frozen():
    for line in rd("cgroup.events").splitlines():
        k, v = line.split()
        if k == "frozen":
            return v == "1"
    return False


def wait_frozen(target, timeout=5.0):
    """Spin until cgroup.events reports the target frozen state; return elapsed ms."""
    t0 = time.monotonic_ns()
    while frozen() != target:
        if (time.monotonic_ns() - t0) / 1e9 > timeout:
            raise TimeoutError(f"freeze->{target} timed out")
    return (time.monotonic_ns() - t0) / 1e6


def trigger_scan(timeout=60.0):
    """Signal the workload to time a full memory scan; return its reported scan_ms."""
    n0 = sum(1 for _ in open(result_path)) if os.path.exists(result_path) else 0
    os.kill(host_pid, signal.SIGUSR1)
    t0 = time.monotonic_ns()
    while True:
        lines = open(result_path).readlines() if os.path.exists(result_path) else []
        if len(lines) > n0:
            return float(lines[-1].split("scan_ms=")[1].split()[0])
        if (time.monotonic_ns() - t0) / 1e9 > timeout:
            raise TimeoutError("scan result timed out")


def pct(xs, p):
    return sorted(xs)[min(len(xs) - 1, int(len(xs) * p))]


out = {"cgroup": cg, "host_pid": host_pid}

# Tier 0 baseline: scan cost with everything resident.
out["resident_scan_ms"] = trigger_scan()

# Tier 1: freeze/thaw latency distribution (CPU reclaim only, memory stays resident).
fz, th = [], []
for _ in range(FREEZE_ITERS):
    wr("cgroup.freeze", "1"); fz.append(wait_frozen(True))
    wr("cgroup.freeze", "0"); th.append(wait_frozen(False))
out["freeze_ms"] = {"median": statistics.median(fz), "p90": pct(fz, 0.9), "min": min(fz), "max": max(fz)}
out["thaw_ms"] = {"median": statistics.median(th), "p90": pct(th, 0.9), "min": min(th), "max": max(th)}

# Tier 2: freeze, then memory.reclaim to swap/zram, then thaw; measure bytes + time + wake cost.
cur0, swp0 = int(rd("memory.current")), int(rd("memory.swap.current"))
wr("cgroup.freeze", "1"); wait_frozen(True)
t0 = time.monotonic_ns()
reclaim_err = None
try:
    wr("memory.reclaim", str(cur0))  # ask to reclaim ~all current usage
except OSError as e:
    reclaim_err = str(e)  # EAGAIN just means it reclaimed less than asked; deltas still hold
reclaim_ms = (time.monotonic_ns() - t0) / 1e6
cur1, swp1 = int(rd("memory.current")), int(rd("memory.swap.current"))
wr("cgroup.freeze", "0"); wait_frozen(False)
out["reclaim"] = {
    "reclaim_error": reclaim_err,
    "current_before": cur0, "current_after": cur1, "reclaimed_bytes": cur0 - cur1,
    "swap_before": swp0, "swap_after": swp1, "swapped_out_bytes": swp1 - swp0,
    "time_ms": reclaim_ms,
}
out["post_reclaim_scan_ms"] = trigger_scan()
out["refault_penalty_ms"] = out["post_reclaim_scan_ms"] - out["resident_scan_ms"]

print(json.dumps(out, indent=2))
