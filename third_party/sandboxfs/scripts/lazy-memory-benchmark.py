#!/usr/bin/env python3
"""Measure SandboxFS memory residency without ORCA or explicit reclaim."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence


class Sampler:
    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = interval_seconds
        self.samples: list[dict[str, Any]] = []
        self._phase = "idle"
        self._cgroups: dict[str, Path] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lazy-memory-sampler")

    def start(self) -> None:
        self.sample()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(2.0, self.interval_seconds * 4))
        self.sample()

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase
        self.sample()

    def add_sandbox(self, sandbox_id: str, pid: int) -> None:
        path = process_cgroup(pid)
        with self._lock:
            self._cgroups[sandbox_id] = path
        self.sample()

    def remove_sandbox(self, sandbox_id: str) -> None:
        with self._lock:
            self._cgroups.pop(sandbox_id, None)

    def sample(self) -> None:
        with self._lock:
            phase = self._phase
            cgroups = dict(self._cgroups)
        memory = meminfo()
        vm = vmstat()
        sandbox_stats = []
        for sandbox_id, path in cgroups.items():
            stat = cgroup_stat(path)
            if stat is not None:
                sandbox_stats.append({"id": sandbox_id, "path": str(path), **stat})
        self.samples.append(
            {
                "timestamp_ns": time.time_ns(),
                "monotonic_ns": time.monotonic_ns(),
                "phase": phase,
                "host": memory,
                "host_pgmajfault": vm.get("pgmajfault", 0),
                "sandbox_memory_current_bytes": sum(
                    stat["memory_current_bytes"] for stat in sandbox_stats
                ),
                "sandbox_memory_swap_bytes": sum(
                    stat["memory_swap_bytes"] for stat in sandbox_stats
                ),
                "sandbox_file_bytes": sum(stat["file_bytes"] for stat in sandbox_stats),
                "sandbox_anon_bytes": sum(stat["anon_bytes"] for stat in sandbox_stats),
                "sandbox_pgmajfault": sum(stat["pgmajfault"] for stat in sandbox_stats),
                "sandboxes": sandbox_stats,
            }
        )

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.sample()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--mode", choices=("baseline", "t1"), required=True)
    parser.add_argument("--sandboxes", type=positive_int, default=8)
    parser.add_argument("--sample-ms", type=positive_float, default=25.0)
    parser.add_argument("--idle-seconds", type=positive_float, default=2.0)
    parser.add_argument("--resident-seconds", type=positive_float, default=5.0)
    parser.add_argument(
        "--tool-command-json",
        default=r'["python3","-c","import json, pathlib; print(\"ready\")"]',
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--drop-caches", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.geteuid() != 0:
        raise SystemExit("lazy-memory-benchmark.py must run as root")
    command = json.loads(args.tool_command_json)
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        raise SystemExit("--tool-command-json must be a non-empty JSON argv")

    if args.drop_caches:
        subprocess.run(("sync",), check=True)
        Path("/proc/sys/vm/drop_caches").write_text("3\n")

    sampler = Sampler(args.sample_ms / 1000)
    sampler.start()
    time.sleep(args.idle_seconds)

    run_id = time.time_ns()
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    active_ids: list[str] = []
    try:
        sampler.set_phase("create")
        with ThreadPoolExecutor(max_workers=args.sandboxes) as workers:
            futures = [
                workers.submit(create_sandbox, args, run_id, sequence)
                for sequence in range(args.sandboxes)
            ]
            for future in as_completed(futures):
                record = future.result()
                records.append(record)
                active_ids.append(record["sandbox_id"])
                sampler.add_sandbox(record["sandbox_id"], record["pid"])
        records.sort(key=lambda record: record["sequence"])

        sampler.set_phase("tool")
        with ThreadPoolExecutor(max_workers=args.sandboxes) as workers:
            futures = {
                workers.submit(run_tool, record["sandbox_id"], command): record
                for record in records
            }
            for future in as_completed(futures):
                record = futures[future]
                result = future.result()
                record.update(result)

        sampler.set_phase("resident")
        time.sleep(args.resident_seconds)
    except BaseException as error:
        errors.append(f"{type(error).__name__}: {error}")
    finally:
        sampler.set_phase("cleanup")
        with ThreadPoolExecutor(max_workers=max(1, args.sandboxes)) as workers:
            futures = {
                workers.submit(destroy_sandbox, sandbox_id): sandbox_id
                for sandbox_id in active_ids
            }
            for future in as_completed(futures):
                sandbox_id = futures[future]
                try:
                    future.result()
                except BaseException as error:
                    errors.append(f"destroy {sandbox_id}: {error}")
                sampler.remove_sandbox(sandbox_id)
        sampler.stop()

    report = build_report(args, command, records, sampler.samples, errors)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2))
    return 0 if report["summary"]["success"] else 1


def create_sandbox(args: argparse.Namespace, run_id: int, sequence: int) -> dict[str, Any]:
    sandbox_id = f"lazy-{args.mode}-{run_id}-{sequence}"
    started = time.monotonic_ns()
    state = run_json(
        (
            "sandboxfsctl",
            "create",
            "--id",
            sandbox_id,
            "--base",
            args.base,
            "--mode",
            args.mode,
        )
    )
    finished = time.monotonic_ns()
    timings = state.get("timings", {})
    return {
        "sequence": sequence,
        "sandbox_id": sandbox_id,
        "pid": int(state["pid"]),
        "create_client_ns": finished - started,
        "ready_server_ns": int(timings.get("total_ns", 0)),
        "workspace_ns": int(timings.get("workspace_ready_ns", 0))
        - int(timings.get("workspace_start_ns", 0)),
    }


def run_tool(sandbox_id: str, command: list[str]) -> dict[str, Any]:
    started = time.monotonic_ns()
    response = run_json(("sandboxfsctl", "exec-json", sandbox_id, "--", *command))
    finished = time.monotonic_ns()
    if response.get("exit_code") != 0:
        raise RuntimeError(f"tool failed in {sandbox_id}: {response}")
    return {
        "tool_client_ns": finished - started,
        "tool_command_ns": int(response.get("duration_ns", 0)),
    }


def destroy_sandbox(sandbox_id: str) -> None:
    run_json(("sandboxfsctl", "destroy", sandbox_id))


def run_json(argv: Sequence[str]) -> dict[str, Any]:
    completed = subprocess.run(argv, check=True, capture_output=True, text=True)
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object from {argv}: {value!r}")
    return value


def process_cgroup(pid: int) -> Path:
    for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            return Path("/sys/fs/cgroup") / line.removeprefix("0::").lstrip("/")
    raise RuntimeError(f"unified cgroup not found for PID {pid}")


def cgroup_stat(path: Path) -> dict[str, int] | None:
    try:
        current = int((path / "memory.current").read_text())
        swap = int((path / "memory.swap.current").read_text())
        values = key_values(path / "memory.stat")
    except FileNotFoundError:
        return None
    return {
        "memory_current_bytes": current,
        "memory_swap_bytes": swap,
        "file_bytes": values.get("file", 0),
        "anon_bytes": values.get("anon", 0),
        "file_mapped_bytes": values.get("file_mapped", 0),
        "file_dirty_bytes": values.get("file_dirty", 0),
        "pgmajfault": values.get("pgmajfault", 0),
    }


def key_values(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in path.read_text().splitlines():
        key, value = line.split()
        result[key] = int(value)
    return result


def meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, payload = line.split(":", 1)
        fields = payload.split()
        values[key] = int(fields[0]) * 1024
    total = values["MemTotal"]
    free = values["MemFree"]
    available = values["MemAvailable"]
    file_cache = (
        values.get("Cached", 0)
        + values.get("SReclaimable", 0)
        - values.get("Shmem", 0)
    )
    return {
        "total_bytes": total,
        "free_bytes": free,
        "available_bytes": available,
        "occupied_bytes": total - free,
        "unavailable_bytes": total - available,
        "file_cache_bytes": file_cache,
        "anon_pages_bytes": values.get("AnonPages", 0),
        "dirty_bytes": values.get("Dirty", 0),
        "writeback_bytes": values.get("Writeback", 0),
        "slab_bytes": values.get("Slab", 0),
    }


def vmstat() -> dict[str, int]:
    return key_values(Path("/proc/vmstat"))


def build_report(
    args: argparse.Namespace,
    command: list[str],
    records: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    idle = [sample for sample in samples if sample["phase"] == "idle"]
    if not idle:
        errors.append("no idle memory samples")
    idle_occupied = mean(sample["host"]["occupied_bytes"] for sample in idle)
    idle_unavailable = mean(sample["host"]["unavailable_bytes"] for sample in idle)
    idle_file_cache = mean(sample["host"]["file_cache_bytes"] for sample in idle)
    for sample in samples:
        sample["attributable_occupied_bytes"] = max(
            0, sample["host"]["occupied_bytes"] - idle_occupied
        )
        sample["attributable_unavailable_bytes"] = max(
            0, sample["host"]["unavailable_bytes"] - idle_unavailable
        )
        sample["attributable_file_cache_bytes"] = max(
            0, sample["host"]["file_cache_bytes"] - idle_file_cache
        )
    active_phases = {"create", "tool", "resident"}
    active = [sample for sample in samples if sample["phase"] in active_phases]
    resident = [sample for sample in samples if sample["phase"] == "resident"]
    ready = [record["ready_server_ns"] for record in records]
    workspace = [record["workspace_ns"] for record in records]
    tool = [record.get("tool_client_ns", 0) for record in records if "tool_client_ns" in record]
    tool_command = [
        record.get("tool_command_ns", 0)
        for record in records
        if "tool_command_ns" in record
    ]
    host_faults = [sample["host_pgmajfault"] for sample in active]
    summary = {
        "success": not errors and len(records) == args.sandboxes,
        "sandboxes": len(records),
        "ready_p50_ns": percentile(ready, 0.50),
        "ready_p95_ns": percentile(ready, 0.95),
        "workspace_p50_ns": percentile(workspace, 0.50),
        "tool_client_p50_ns": percentile(tool, 0.50),
        "tool_client_p95_ns": percentile(tool, 0.95),
        "tool_command_p50_ns": percentile(tool_command, 0.50),
        "attributable_occupied_active_mean_bytes": time_weighted_mean(
            samples, "attributable_occupied_bytes", active_phases
        ),
        "attributable_occupied_active_p95_bytes": percentile(
            [sample["attributable_occupied_bytes"] for sample in active], 0.95
        ),
        "attributable_occupied_active_peak_bytes": max(
            (sample["attributable_occupied_bytes"] for sample in active), default=0
        ),
        "attributable_occupied_resident_mean_bytes": time_weighted_mean(
            samples, "attributable_occupied_bytes", {"resident"}
        ),
        "attributable_unavailable_active_mean_bytes": time_weighted_mean(
            samples, "attributable_unavailable_bytes", active_phases
        ),
        "attributable_file_cache_active_mean_bytes": time_weighted_mean(
            samples, "attributable_file_cache_bytes", active_phases
        ),
        "attributable_file_cache_resident_mean_bytes": time_weighted_mean(
            samples, "attributable_file_cache_bytes", {"resident"}
        ),
        "sandbox_memory_active_mean_bytes": time_weighted_mean(
            samples, "sandbox_memory_current_bytes", active_phases
        ),
        "sandbox_memory_active_p95_bytes": percentile(
            [sample["sandbox_memory_current_bytes"] for sample in active], 0.95
        ),
        "sandbox_memory_active_peak_bytes": max(
            (sample["sandbox_memory_current_bytes"] for sample in active), default=0
        ),
        "sandbox_memory_resident_mean_bytes": time_weighted_mean(
            samples, "sandbox_memory_current_bytes", {"resident"}
        ),
        "sandbox_file_resident_mean_bytes": time_weighted_mean(
            samples, "sandbox_file_bytes", {"resident"}
        ),
        "sandbox_anon_resident_mean_bytes": time_weighted_mean(
            samples, "sandbox_anon_bytes", {"resident"}
        ),
        "sandbox_swap_active_mean_bytes": time_weighted_mean(
            samples, "sandbox_memory_swap_bytes", active_phases
        ),
        "sandbox_swap_active_peak_bytes": max(
            (sample["sandbox_memory_swap_bytes"] for sample in active), default=0
        ),
        "host_major_faults_active_delta": (
            max(host_faults) - min(host_faults) if host_faults else 0
        ),
        "resident_samples": len(resident),
        "errors": errors,
    }
    return {
        "schema": "sandboxfs-lazy-memory-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "config": {
            "base": args.base,
            "mode": args.mode,
            "sandboxes": args.sandboxes,
            "sample_ms": args.sample_ms,
            "idle_seconds": args.idle_seconds,
            "resident_seconds": args.resident_seconds,
            "tool_command": command,
            "drop_caches": args.drop_caches,
        },
        "host": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "kernel": platform.release(),
            "memory": meminfo(),
        },
        "idle": {
            "occupied_mean_bytes": idle_occupied,
            "unavailable_mean_bytes": idle_unavailable,
            "file_cache_mean_bytes": idle_file_cache,
            "samples": len(idle),
        },
        "records": records,
        "samples": samples,
        "summary": summary,
    }


def mean(values: Any) -> int:
    collected = list(values)
    return round(statistics.fmean(collected)) if collected else 0


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def time_weighted_mean(
    samples: list[dict[str, Any]], metric: str, phases: set[str]
) -> int:
    intervals = [
        (left, right["monotonic_ns"] - left["monotonic_ns"])
        for left, right in zip(samples, samples[1:])
        if left["phase"] in phases
    ]
    duration = sum(interval for _, interval in intervals)
    if duration <= 0:
        return 0
    return round(sum(sample[metric] * interval for sample, interval in intervals) / duration)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
