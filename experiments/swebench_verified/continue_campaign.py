#!/usr/bin/env python3
"""Durable controller after pilot qualification; stops on every infrastructure error."""
import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REMOTE = "/sandboxfs/crate-swebench-20260919"


def ssh(command, check=True):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "nsl17", command],
                          text=True, capture_output=True, check=check, timeout=90)


def wait_service(unit, marker):
    while True:
        if ssh("test -f " + shlex.quote(marker), check=False).returncode == 0:
            return
        status = ssh("systemctl show " + shlex.quote(unit) + " --property=ActiveState --property=Result --property=ExecMainStatus").stdout
        if "ActiveState=failed" in status or "ActiveState=inactive" in status:
            raise RuntimeError(f"{unit} stopped without completion marker: {status}")
        print("WAIT", unit, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), flush=True)
        time.sleep(30)


def run(argv):
    print("RUN", shlex.join(list(map(str, argv))), flush=True)
    subprocess.run(list(map(str, argv)), check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", type=Path, required=True)
    ap.add_argument("--resume-prepare32", action="store_true", help="Reuse the already-running exact preparation unit; do not launch a duplicate")
    a = ap.parse_args()
    p = a.package.resolve()
    wait_service("crate-sv-pilot.service", REMOTE + "/pilot/COMPLETED.json")
    run(["rsync", "-a", "nsl17:" + REMOTE + "/pilot/", str(p / "pilot") + "/"])
    run([sys.executable, ROOT / "experiments/swebench_verified/analyze.py", "--campaign", p / "pilot", "--output", p / "pilot-analysis"])
    # Prepare the remaining fixed tasks, after the pilot passed matching and cold checks.
    run(["rsync", "-a", str(ROOT / "experiments/swebench_verified") + "/", "nsl17:" + REMOTE + "/scripts/"])
    command = ["sudo", "-n", "systemd-run", "--unit=crate-sv-prepare32.service", "--property=Type=exec", "--property=AllowedCPUs=0-7",
               "--property=StandardOutput=append:" + REMOTE + "/logs/prepare32.log",
               "--property=StandardError=append:" + REMOTE + "/logs/prepare32.log",
               "/usr/bin/python3", REMOTE + "/scripts/prepare_remote.py", "--limit", "32"]
    if a.resume_prepare32:
        current = ssh("systemctl show crate-sv-prepare32.service --property=ExecStart").stdout
        if REMOTE + "/scripts/prepare_remote.py --limit 32" not in current:
            raise RuntimeError("cannot resume an unrecognized preparation unit")
    else:
        print(ssh(shlex.join(command)).stdout, flush=True)
    wait_service("crate-sv-prepare32.service", REMOTE + "/artifacts/PREPARE_32_COMPLETE.json")
    command = ["sudo", "-n", "systemd-run", "--unit=crate-sv-isolation32.service", "--property=Type=exec", "--property=AllowedCPUs=0-7",
               "--property=StandardOutput=append:" + REMOTE + "/logs/isolation32.log",
               "--property=StandardError=append:" + REMOTE + "/logs/isolation32.log",
               "/usr/bin/python3", REMOTE + "/scripts/verify_isolation.py", "--limit", "32"]
    print(ssh(shlex.join(command)).stdout, flush=True)
    wait_service("crate-sv-isolation32.service", REMOTE + "/artifacts/ISOLATION_32_COMPLETE.json")
    run([sys.executable, ROOT / "experiments/swebench_verified/capture_batch.py", "--selection", p / "selection",
         "--output", p / "captures-v2", "--start", "4", "--limit", "32"])
    run([sys.executable, ROOT / "experiments/swebench_verified/convert.py", "--captures", p / "captures-v2",
         "--selection", p / "selection/manifest.json", "--output", p / "normalized-formal", "--limit", "32"])
    run([sys.executable, ROOT / "experiments/swebench_verified/snapshot.py", "--output", p / "source-formal"])
    for name in ("normalized-formal", "source-formal", "captures-v2"):
        run(["rsync", "-a", str(p / name) + "/", "nsl17:" + REMOTE + "/" + name + "/"])
    command = ["sudo", "-n", "systemd-run", "--unit=crate-sv-formal.service", "--property=Type=exec", "--property=AllowedCPUs=0-7",
               "--property=StandardOutput=append:" + REMOTE + "/logs/formal.log",
               "--property=StandardError=append:" + REMOTE + "/logs/formal.log",
               "/usr/bin/python3", REMOTE + "/source-formal/experiments/swebench_verified/run_suite.py",
               "--kind", "formal", "--output", REMOTE + "/formal"]
    print(ssh(shlex.join(command)).stdout, flush=True)
    wait_service("crate-sv-formal.service", REMOTE + "/formal/COMPLETED.json")
    for name in ("formal", "artifacts", "logs"):
        run(["rsync", "-a", "nsl17:" + REMOTE + "/" + name + "/", str(p / name) + "/"])
    run([sys.executable, ROOT / "experiments/swebench_verified/analyze.py", "--campaign", p / "formal", "--output", p / "formal-analysis"])
    print("FORMAL_MEASUREMENTS_AND_LOCAL_VALIDATION_COMPLETE", flush=True)


if __name__ == "__main__":
    main()
