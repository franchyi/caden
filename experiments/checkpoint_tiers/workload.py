#!/usr/bin/env python3
"""Synthetic idle-agent: allocate and fault in N MiB of anonymous memory, print READY, then idle.
On SIGUSR1 it times a full read-scan of that memory (the cost of faulting pages back after a
reclaim) and appends the duration to the result file passed as argv[2]."""

import os
import signal
import sys
import time

PAGE = 4096
mib = int(sys.argv[1])
result_path = sys.argv[2]
fill = sys.argv[3] if len(sys.argv) > 3 else "zero"  # "zero" (compressible) or "rand" (incompressible)

buf = bytearray(mib * 1024 * 1024)
if fill == "rand":
    block = os.urandom(16 * 1024 * 1024)  # tile an incompressible block so every page resists lz4
    for off in range(0, len(buf), len(block)):
        end = min(off + len(block), len(buf))
        buf[off:end] = block[:end - off]
else:
    for i in range(0, len(buf), PAGE):
        buf[i] = 1  # fault every page in so it counts against RSS


def scan(_sig=None, _frm=None):
    t0 = time.monotonic_ns()
    s = 0
    for i in range(0, len(buf), PAGE):
        s += buf[i]
    dt_ms = (time.monotonic_ns() - t0) / 1e6
    with open(result_path, "a") as f:
        f.write(f"{time.time():.6f} scan_ms={dt_ms:.3f} checksum={s}\n")


signal.signal(signal.SIGUSR1, scan)
sys.stdout.write(f"READY rss_target_mib={mib} pid_in_ns={os.getpid()}\n")
sys.stdout.flush()
while True:
    time.sleep(3600)
