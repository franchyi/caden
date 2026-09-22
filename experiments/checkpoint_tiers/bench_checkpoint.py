#!/usr/bin/env python3
"""Measure CRIU checkpoint (dump) and restore latency for a memory-holding bwrap sandbox. Run as
root; tune with MIB and STORE env vars (tmpfs /dev/shm gives the memory-bound lower bound, /tmp the
disk-bound figure). bwrap runs as root (no user namespace) with its own pid/mount ns: the mount tree
is just our clean binds, and dumping the tree rooted at the in-namespace init clears the user-ns and
nested-pidns blockers we hit dumping the sandbox host-side."""

import json
import os
import shutil
import statistics
import subprocess
import time

MIB = int(os.environ.get("MIB", "512"))
STORE = os.environ.get("STORE", "/dev/shm")
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = f"/tmp/ckpt-bench-{MIB}"
IMG = f"{STORE}/caden-ckpt-{MIB}"
WORKLOAD = os.path.join(HERE, "workload.py")
DUMP_ITERS = 5


def sh(argv):
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def fresh_img():
    if os.path.exists(IMG):
        shutil.rmtree(IMG)
    os.makedirs(IMG)


def img_bytes():
    return sum(os.path.getsize(os.path.join(IMG, f)) for f in os.listdir(IMG))


BWRAP = ["bwrap", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
         "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin", "--ro-bind", "/lib", "/lib",
         "--ro-bind", "/lib64", "/lib64", "--ro-bind", "/etc", "/etc",
         "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev"]

# Inline memory holder, tagged so we can find/kill it; no host bind (a writable bind carries shared
# propagation CRIU rejects — orthogonal to the dump/restore latency we are measuring here).
HOLDER = ("import sys,time\n"
          "n=%d*1024*1024\n"
          "b=bytearray(n)\n"
          "for i in range(0,n,4096): b[i]=1\n"
          "sys.stdout.write('READY\\n'); sys.stdout.flush()\n"
          "time.sleep(36000)\n")


def launch():
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    out = open(os.path.join(WORK, "out"), "w")
    # Plain memory holder: measures CRIU's intrinsic dump/restore cost (process memory), which is what
    # image size and latency depend on. Wrapping it in bwrap adds per-mount integration work (see the
    # design note's CRIU section) but negligible latency.
    argv = ["python3", "-c", HOLDER % MIB, f"caden-holder-{MIB}"]
    p = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 30:
        if "READY" in open(os.path.join(WORK, "out")).read():
            break
        time.sleep(0.1)
    else:
        raise SystemExit("workload failed: " + open(os.path.join(WORK, "out")).read())
    return p.pid


CRIU_COMMON = ["--shell-job", "--enable-external-masters"]


def criu_dump(pid, leave):
    args = ["criu", "dump", "--tree", str(pid), "-D", IMG] + CRIU_COMMON
    if leave:
        args.append("--leave-running")
    t0 = time.monotonic_ns()
    r = sh(args)
    return (time.monotonic_ns() - t0) / 1e6, r.returncode, r.stdout


def criu_restore():
    t0 = time.monotonic_ns()
    r = sh(["criu", "restore", "-D", IMG, "-d"] + CRIU_COMMON)
    return (time.monotonic_ns() - t0) / 1e6, r.returncode, r.stdout


out = {"mib": MIB, "store": STORE}
pid = launch()

dumps, err = [], None
for _ in range(DUMP_ITERS):
    fresh_img()
    dt, rc, log = criu_dump(pid, leave=True)
    if rc != 0:
        err = log[-2000:]
        break
    dumps.append(dt)

if dumps:
    dumps.sort()
    out["dump_ms"] = {"median": round(statistics.median(dumps), 2),
                      "min": round(min(dumps), 2), "max": round(max(dumps), 2)}
    out["image_bytes"] = img_bytes()
    fresh_img()
    dt_d, _, _ = criu_dump(pid, leave=False)  # checkpoint away (process is killed)
    time.sleep(0.3)
    killed = sh(["pgrep", "-f", f"caden-holder-{MIB}"]).stdout.strip() == ""
    dt_r, rc_r, log_r = criu_restore()
    time.sleep(0.3)
    alive = sh(["pgrep", "-f", f"caden-holder-{MIB}"]).stdout.strip() != ""
    out["final_dump_ms"] = round(dt_d, 2)
    out["restore_ms"] = round(dt_r, 2) if rc_r == 0 else None
    out["killed_by_dump"] = killed
    out["alive_after_restore"] = alive
    if rc_r != 0:
        out["restore_error"] = log_r[-2000:]
else:
    out["dump_error"] = err

sh(["pkill", "-9", "-f", f"caden-holder-{MIB}"])
print(json.dumps(out, indent=2))
