#!/usr/bin/env python3
"""Detach this campaign's controller with durable logs; never launch duplicates."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", type=Path, required=True)
    ap.add_argument("--resume-prepare32", action="store_true")
    a = ap.parse_args()
    record = a.package / "controller.json"
    if record.exists():
        previous = json.loads(record.read_text())
        try:
            os.kill(previous["pid"], 0)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError("controller PID still exists; inspect exact command before any restart")
    argv = [sys.executable, "-u", str(Path(__file__).with_name("continue_campaign.py")), "--package", str(a.package)]
    if a.resume_prepare32:
        argv.append("--resume-prepare32")
    with (a.package / "controller.log").open("a") as log:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    result = {"pid": process.pid, "argv": argv, "started_unix": time.time(), "log": "controller.log"}
    record.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
