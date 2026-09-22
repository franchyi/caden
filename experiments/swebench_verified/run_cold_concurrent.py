#!/usr/bin/env python3
"""Matched cold creates in barrier-released batches; no ready pool or replay.

All creates in a batch finish the normal-API /bin/true endpoint before any
workspace is destroyed. Filesystem preparation is reported separately. Failed
samples are retained; an incomplete batch is never silently omitted.
"""
from __future__ import annotations

import argparse
import json
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if __package__:
    from .run_cold import api_call, timestamp_ns
else:
    from run_cold import api_call, timestamp_ns


def batch_plan(tasks, concurrency, repeat=0):
    if concurrency < 1 or not tasks or len(tasks) % concurrency:
        raise ValueError("cold concurrency must divide the fixed task count")
    for batch, start in enumerate(range(0, len(tasks), concurrency)):
        modes = ["baseline", "t1"] if (batch + repeat) % 2 == 0 else ["t1", "baseline"]
        for mode in modes:
            yield batch, mode, tasks[start:start + concurrency]


def measure(task, mode, ident, barrier, ctl):
    prefix = [ctl, "--socket", task["socket"], "--timeout", "600s"]
    record = {"task": task, "mode": mode, "sandbox_id": ident}
    try:
        barrier.wait(timeout=30)
        start_mono, start_wall = time.monotonic_ns(), time.time_ns()
        record["client_start_monotonic_ns"] = start_mono
        state = api_call(prefix, ["create", "--id", ident, "--base", task["base"], "--mode", mode])
        record["state"] = state
        response = api_call(prefix, ["exec-json", ident, "--", "/bin/true"])
        end_mono, end_wall = time.monotonic_ns(), time.time_ns()
        record["first_command"] = response
        if response["exit_code"] != 0:
            raise RuntimeError("first command failed")
        phase = state["timings"]
        record.update(cold_start_ns=end_wall - timestamp_ns(phase["request_received_at"]),
                      client_submission_to_ready_ns=end_mono - start_mono,
                      client_end_monotonic_ns=end_mono,
                      wall_monotonic_discrepancy_ns=(end_wall - start_wall) - (end_mono - start_mono),
                      filesystem_provision_ns=phase["workspace_ready_ns"] - phase["workspace_start_ns"],
                      daemon_ready_ns=phase["total_ns"])
        if record["cold_start_ns"] <= 0:
            raise RuntimeError("invalid cold endpoint")
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}"
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--ctl", required=True)
    ap.add_argument("--concurrency", type=int, required=True)
    ap.add_argument("--limit", type=int, default=32)
    a = ap.parse_args()
    if a.output.exists():
        raise SystemExit("refusing existing cold results")
    tasks = json.loads(a.selection.read_text())["tasks"][:a.limit]
    plan = list(batch_plan(tasks, a.concurrency))
    if len(tasks) != a.limit or len({t["socket"] for t in tasks}) != len(tasks):
        raise SystemExit("incomplete tasks or non-unique daemon sockets")
    # Dedicated run-owned daemons must not contain anybody else's sandbox.
    for task in tasks:
        if api_call([a.ctl, "--socket", task["socket"]], ["list"])["sandboxes"]:
            raise SystemExit("cold daemons are not empty")
    a.output.mkdir(parents=True)
    aborted = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: aborted.set())
    records = []
    for batch, mode, group in plan:
        if aborted.is_set():
            raise RuntimeError("cold campaign aborted by host monitor")
        barrier = threading.Barrier(len(group))
        with ThreadPoolExecutor(max_workers=len(group)) as pool:
            futures = [pool.submit(measure, task, mode,
                f"sweep-c{a.concurrency}-{task['sequence']:02d}-{mode}", barrier, a.ctl) for task in group]
            measured = [future.result() for future in futures]
        # Cleanup is strictly outside every sample's cold endpoint.
        for record in measured:
            record.update(concurrency=a.concurrency, batch=batch, repeat=0)
            prefix = [a.ctl, "--socket", record["task"]["socket"], "--timeout", "600s"]
            try:
                listed = api_call(prefix, ["list"])["sandboxes"]
                if any(s["id"] == record["sandbox_id"] for s in listed):
                    record["destroy"] = api_call(prefix, ["destroy", record["sandbox_id"]])
                elif "state" in record:
                    raise RuntimeError("created sandbox missing before cleanup")
            except Exception as error:
                record["cleanup_error"] = f"{type(error).__name__}: {error}"
                record.setdefault("error", record["cleanup_error"])
            records.append(record)
            with (a.output / "samples.jsonl").open("a") as handle:
                handle.write(json.dumps(record) + "\n")
            print(a.concurrency, batch, mode, record["task"]["instance_id"],
                  record.get("cold_start_ns", record.get("error")), flush=True)
        if any("error" in record for record in measured) or aborted.is_set():
            raise RuntimeError("cold batch failed/aborted; all samples and cleanup outcomes retained")
    (a.output / "completed.json").write_text(json.dumps({"samples": len(records),
        "tasks": len(tasks), "concurrency": a.concurrency, "repeats": 1,
        "endpoint": "create receipt to successful normal-API /bin/true; no pool",
        "scope": "cold sandbox, warm host, immutable prepared local base"}) + "\n")


if __name__ == "__main__":
    main()
