#!/usr/bin/env python3
"""Run one Caden + SandboxFS multi-agent memory campaign on Linux."""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import re
import platform
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from caden.policy import CadenPolicyConfig, StageAwareCaden  # noqa: E402
from caden.pool import ElasticReadyPool, ReadyPoolConfig  # noqa: E402
from caden.sandboxfs_backend import (  # noqa: E402
    SandboxFSConfig,
    SandboxFSError,
    SandboxFSExecution,
)
from caden.scheduler import ReqState  # noqa: E402
from caden.types import AgentTask, ReclaimMode, Stage, Tier  # noqa: E402

HOLDER_CODE = """
import os
import random
import signal

size = {size}
pattern = {pattern!r}
scan_stride = {scan_stride}
data = bytearray(size)
random_bytes = size if pattern == 'random' else (size // 4 if pattern == 'mixed' else 0)
generator = random.Random(20260718)
chunk_size = 1 << 20
for offset in range(0, random_bytes, chunk_size):
    end = min(random_bytes, offset + chunk_size)
    data[offset:end] = generator.randbytes(end - offset)
for offset in range(0, size, 4096):
    data[offset] ^= (offset // 4096) & 255

scan_count = 0

def scan(_signal, _frame):
    global scan_count
    checksum = 0
    for offset in range(0, len(data), scan_stride):
        checksum += data[offset]
    scan_count += 1
    with open('/tmp/caden-holder-scan', 'w') as handle:
        handle.write(f'{{scan_count}} {{checksum}}\\n')

signal.signal(signal.SIGUSR1, scan)
with open('/tmp/caden-holder-ready', 'w') as handle:
    handle.write(str(os.getpid()))
while True:
    signal.pause()
""".strip()


SCAN_SHELL = """
before=$(awk '{print $1}' /tmp/caden-holder-scan 2>/dev/null || echo 0)
target=$((before + 1))
kill -USR1 "$(cat /tmp/caden-holder.pid)"
for i in $(seq 1 1200); do
  current=$(awk '{print $1}' /tmp/caden-holder-scan 2>/dev/null || echo 0)
  [ "$current" -ge "$target" ] && exit 0
  sleep 0.01
done
exit 1
""".strip()


class Sampler:
    def __init__(self, execution: SandboxFSExecution, interval_seconds: float,
                 service_names: set[str] | None = None,
                 service_glob: str = "crate-sv-*.service",
                 service_regex: str = r"crate-sv-\d{2}\.service") -> None:
        self.execution = execution
        self.interval_seconds = interval_seconds
        self.service_names = service_names
        # Defaults are the September 19 unit names; an isolated run passes its own.
        self.service_glob = service_glob
        self.service_regex = re.compile(service_regex)
        self.samples: list[dict[str, Any]] = []
        self.sample_callback = None
        self._phase = "idle"
        self._lock = threading.Lock()
        self._sample_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="caden-memory-sampler")

    def start(self) -> None:
        self.sample()
        self._thread.start()

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase
        self.sample()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(2.0, self.interval_seconds * 4))
        self.sample()

    def sample(self) -> None:
        with self._sample_lock:
            self._sample()

    def _sample(self) -> None:
        sample_started = time.monotonic_ns()
        with self._lock:
            phase = self._phase
        host = self.execution.host_stat()
        sandboxes = []
        for sandbox in self.execution.sandbox_ids():
            try:
                stat = self.execution.stat(sandbox)
            except (SandboxFSError, FileNotFoundError):
                continue
            sandboxes.append(
                {
                    "id": sandbox,
                    "stage": stat.stage.value,
                    "cpu_class": stat.cpu_class.value,
                    "memory_current_bytes": stat.mem_dram_bytes,
                    "memory_swap_bytes": stat.mem_swap_bytes,
                    "memory_compressed_bytes": stat.mem_compressed_bytes,
                    "major_faults": stat.major_faults,
                    "cpu_usage_usec": stat.cpu_usage_usec,
                }
            )
        physical = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            fields = line.split()
            if fields[0] in {"MemTotal:", "MemFree:", "MemAvailable:", "Cached:", "Buffers:", "SReclaimable:"}:
                physical[fields[0][:-1]] = int(fields[1]) * 1024
        services, daemon_leaves, memory_stats, all_services, daemon_stats = {}, {}, {}, {}, {}
        observer_charge = None
        # The suite/replay controller is separate from all task services.
        # Include it explicitly rather than silently excluding instrumentation.
        own_cgroup = next((line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines()
                           if line.startswith('0::')), '')
        if own_cgroup.endswith('-controller.service'):
            try:
                observer_charge = int((Path('/sys/fs/cgroup') / own_cgroup.lstrip('/') /
                                       'memory.current').read_text())
            except OSError:
                pass
        for path in Path("/sys/fs/cgroup/system.slice").glob(self.service_glob + "/memory.current"):
            if self.service_regex.fullmatch(path.parent.name):
                try:
                    current = int(path.read_text())
                    all_services[path.parent.name] = current
                    if self.service_names is not None and path.parent.name not in self.service_names:
                        continue
                    services[path.parent.name] = current
                    daemon_leaves[path.parent.name] = int((path.parent / "daemon/memory.current").read_text())
                    daemon_stats[path.parent.name] = dict(
                        (key, int(value)) for key, value in
                        (line.split() for line in (path.parent / "daemon/memory.stat").read_text().splitlines()))
                    memory_stats[path.parent.name] = dict(
                        (key, int(value)) for key, value in
                        (line.split() for line in (path.parent / "memory.stat").read_text().splitlines())
                    )
                except FileNotFoundError:
                    continue
        tier_store: dict[str, int] = {}
        store_stats = getattr(getattr(self.execution, "memory_tier", None), "store_stats", None)
        if store_stats is not None:
            try:
                tier_store = {key: int(value) for key, value in store_stats().items()}
            except Exception:  # noqa: BLE001 - sampling must not stop a campaign
                tier_store = {"unavailable": 1}
        self.samples.append(
            {
                "tier_store": tier_store,
                "timestamp_ns": time.time_ns(),
                "monotonic_ns": time.monotonic_ns(),
                "phase": phase,
                "sampling_duration_ns": time.monotonic_ns() - sample_started,
                "host_total_bytes": host.dram_total_bytes,
                "host_available_bytes": host.dram_free_bytes,
                "host_used_bytes": host.dram_total_bytes - host.dram_free_bytes,
                "host_physical_meminfo_bytes": physical,
                "host_total_minus_free_bytes": physical.get("MemTotal", 0) - physical.get("MemFree", 0),
                "task_service_cgroup_bytes": services,
                "task_service_cgroup_sum_bytes": sum(services.values()),
                "observer_cgroup_current_bytes": observer_charge,
                "all_prepared_service_cgroup_sum_bytes": sum(all_services.values()),
                "task_service_memory_stat": memory_stats,
                "task_service_anon_bytes": sum(s.get("anon", 0) for s in memory_stats.values()),
                "task_service_file_bytes": sum(s.get("file", 0) for s in memory_stats.values()),
                "task_service_kernel_bytes": sum(s.get("kernel", 0) for s in memory_stats.values()),
                "task_daemon_cgroup_bytes": daemon_leaves,
                "task_daemon_cgroup_sum_bytes": sum(daemon_leaves.values()),
                "task_daemon_stat_sum_bytes": {key: sum(s.get(key, 0) for s in daemon_stats.values())
                    for key in ("file", "anon", "kernel", "inactive_file", "active_file",
                                "file_mapped", "file_dirty", "file_writeback", "slab_reclaimable")},
                "runner_maxrss_kib": __import__("resource").getrusage(__import__("resource").RUSAGE_SELF).ru_maxrss,
                "sandbox_memory_current_bytes": sum(
                    item["memory_current_bytes"] for item in sandboxes
                ),
                "sandbox_memory_swap_bytes": sum(
                    item["memory_swap_bytes"] for item in sandboxes
                ),
                "sandbox_memory_compressed_bytes": sum(
                    item["memory_compressed_bytes"] for item in sandboxes
                ),
                "sandbox_major_faults": sum(item["major_faults"] for item in sandboxes),
                "sandboxes": sandboxes,
            }
        )
        if self.sample_callback is not None:
            self.sample_callback(self.samples[-1])

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.sample()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="registered SandboxFS base")
    parser.add_argument("--mode", choices=("baseline", "t1"), required=True)
    parser.add_argument("--policy", type=lambda value: "caden" if value == "orca" else value, choices=("static", "caden"), required=True)
    parser.add_argument("--sandboxes", type=positive_int, default=8)
    parser.add_argument("--wss-mib", type=positive_int, default=512)
    parser.add_argument(
        "--wss-pattern", choices=("zeros", "mixed", "random"), default="mixed"
    )
    parser.add_argument("--wake-stride-kib", type=positive_int, default=64)
    parser.add_argument("--llm-wait-seconds", type=positive_float, default=15.0)
    parser.add_argument("--hot-settle-seconds", type=positive_float, default=2.0)
    parser.add_argument("--sample-ms", type=positive_float, default=100.0)
    parser.add_argument("--reclaim-grace-ms", type=nonnegative_float, default=100.0)
    parser.add_argument("--reclaim-tier", choices=("ssd", "compressed"), default="ssd")
    parser.add_argument(
        "--reclaim-mode", choices=("balanced", "file", "anon"), default="balanced"
    )
    parser.add_argument("--hot-reserve-mib", type=nonnegative_int, default=0)
    parser.add_argument("--allow-zswap-compression", action="store_true")
    parser.add_argument("--zswap-max-mib", type=nonnegative_int, default=0)
    parser.add_argument("--max-reclaims", type=positive_int, default=2)
    parser.add_argument("--max-movements", type=positive_int, default=2)
    parser.add_argument("--confirmed-movement-reserve", type=nonnegative_int, default=0)
    parser.add_argument("--max-admissions", type=positive_int, default=4)
    parser.add_argument("--max-wakes", type=positive_int, default=2)
    parser.add_argument("--pool-target", type=nonnegative_int, default=0)
    parser.add_argument("--pool-max", type=nonnegative_int, default=0)
    parser.add_argument("--estimated-wss-mib", type=positive_int, default=640)
    parser.add_argument("--dram-reserve-mib", type=nonnegative_int, default=2048)
    parser.add_argument("--socket", default="/run/sandboxfsd.sock")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--drop-caches", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run_campaign.py must run as root")
    if args.pool_target > args.pool_max:
        raise SystemExit("--pool-target must not exceed --pool-max")
    if args.pool_target > 0 and args.pool_max == 0:
        raise SystemExit("--pool-max must be positive when pooling is enabled")
    if args.drop_caches:
        os.sync()
        Path("/proc/sys/vm/drop_caches").write_text("3\n")

    execution = SandboxFSExecution(
        SandboxFSConfig(
            socket_path=args.socket,
            mode=args.mode,
            allow_zswap_compression=args.allow_zswap_compression,
            zswap_max_bytes=args.zswap_max_mib << 20,
        )
    )
    pool = make_pool(args, execution)
    scheduler = StageAwareCaden(
        execution,
        CadenPolicyConfig(
            estimated_wss_bytes=args.estimated_wss_mib << 20,
            fixed_dram_reserve_bytes=args.dram_reserve_mib << 20,
            wake_reserve_per_waiter_bytes=0,
            reclaim_grace_seconds=args.reclaim_grace_ms / 1000,
            reclaim_tier=(
                Tier.COMPRESSED if args.reclaim_tier == "compressed" else Tier.SSD
            ),
            reclaim_mode={
                "balanced": ReclaimMode.BALANCED,
                "file": ReclaimMode.FILE_ONLY,
                "anon": ReclaimMode.ANON_ONLY,
            }[args.reclaim_mode],
            sandbox_hot_reserve_bytes=args.hot_reserve_mib << 20,
            max_concurrent_admissions=args.max_admissions,
            max_concurrent_reclaims=args.max_reclaims,
            max_concurrent_wakes=args.max_wakes,
            max_concurrent_movements=args.max_movements,
            confirmed_movement_reserve=args.confirmed_movement_reserve,
            reclaim_enabled=args.policy == "caden",
        ),
        ready_pool=pool,
    )
    sampler = Sampler(execution, args.sample_ms / 1000)
    sampler.start()

    requests: list[dict[str, Any]] = []
    request_ids: list[str] = []
    wake_latencies: list[int] = []
    errors: list[str] = []
    raised: BaseException | None = None
    try:
        if pool is not None and args.pool_target > 0:
            sampler.set_phase("pool_prepare")
            pool.start(args.base)
            pool.wait_for_refill(600)

        sampler.set_phase("create")
        pending: list[tuple[int, str, int]] = []
        for sequence in range(args.sandboxes):
            submitted = time.monotonic_ns()
            request_id = scheduler.submit(AgentTask([], args.base))
            request_ids.append(request_id)
            pending.append((sequence, request_id, submitted))
        with ThreadPoolExecutor(
            max_workers=args.sandboxes, thread_name_prefix="campaign-client"
        ) as clients:
            futures = [
                clients.submit(
                    finish_create_request,
                    scheduler,
                    execution,
                    sequence,
                    request_id,
                    submitted,
                )
                for sequence, request_id, submitted in pending
            ]
            for future in as_completed(futures):
                requests.append(future.result())
        requests.sort(key=lambda request: request["sequence"])

        sampler.set_phase("allocate_wss")
        with ThreadPoolExecutor(
            max_workers=args.sandboxes, thread_name_prefix="campaign-wss"
        ) as workers:
            futures = [
                workers.submit(
                    launch_holder,
                    execution,
                    request["sandbox_id"],
                    args.wss_mib << 20,
                    args.wss_pattern,
                    args.wake_stride_kib << 10,
                )
                for request in requests
            ]
            for future in as_completed(futures):
                future.result()
        for request in requests:
            charged = execution.stat(request["sandbox_id"]).mem_dram_bytes
            request["charged_after_allocate_bytes"] = charged
            minimum_charge = int((args.wss_mib << 20) * 0.85)
            if charged < minimum_charge:
                raise RuntimeError(
                    f"sandbox {request['sandbox_id']} charged only {charged} bytes "
                    f"after allocating {args.wss_mib} MiB; process-tree cgroup "
                    "placement or accounting is invalid"
                )

        sampler.set_phase("hot")
        time.sleep(args.hot_settle_seconds)

        sampler.set_phase("llm_wait")
        for request in requests:
            scheduler.on_report(request["sandbox_id"], Stage.LLM_WAIT)
        time.sleep(args.llm_wait_seconds)
        sampler.sample()

        sampler.set_phase("response_wake")
        with ThreadPoolExecutor(
            max_workers=args.sandboxes, thread_name_prefix="campaign-wake"
        ) as workers:
            futures = [
                workers.submit(
                    wake_request,
                    scheduler,
                    execution,
                    request["sandbox_id"],
                )
                for request in requests
            ]
            for future in as_completed(futures):
                wake_latencies.append(future.result())
        # Keep LLM_WAIT fixed across baseline and treatment. If reclaim is
        # still in flight when the model responds, RESPONSE_WAKE waits on the
        # residency lock and the delay is correctly charged to wake latency.
        scheduler.wait_for_background(timeout=600)

        sampler.set_phase("cleanup")
        for request in requests:
            scheduler.complete(request["sandbox_id"], "ok")
        if pool is not None:
            pool.trim(0)
    except BaseException as error:
        errors.append(f"{type(error).__name__}: {error}")
        raised = error
    finally:
        for request_id in request_ids:
            if scheduler.poll(request_id).state in {
                ReqState.QUEUED,
                ReqState.RUNNING,
            }:
                try:
                    scheduler.cancel(request_id)
                except Exception as error:
                    errors.append(f"cleanup {request_id}: {error}")
        scheduler.close()
        sampler.stop()
        report = build_report(
            args,
            execution,
            sampler.samples,
            requests,
            wake_latencies,
            scheduler,
            pool,
            errors,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report["summary"], indent=2))
        print(f"report written to {args.output}")
    if raised is not None:
        raise raised
    return 0


def make_pool(
    args: argparse.Namespace, execution: SandboxFSExecution
) -> ElasticReadyPool | None:
    if args.pool_max == 0:
        return None
    return ElasticReadyPool(
        execution,
        ReadyPoolConfig(
            minimum_ready=0,
            target_ready=args.pool_target,
            maximum_ready=args.pool_max,
            estimated_ready_bytes=32 << 20,
            dram_reserve_bytes=args.dram_reserve_mib << 20,
        ),
    )


def finish_create_request(
    scheduler: StageAwareCaden,
    execution: SandboxFSExecution,
    sequence: int,
    request_id: str,
    submitted: int,
) -> dict[str, Any]:
    wait_until_running(scheduler, request_id, timeout=600)
    sandbox = scheduler.request_sandbox(request_id)
    if sandbox is None:
        raise RuntimeError(f"request {request_id} has no sandbox")
    state = execution.state(sandbox)
    timings = state.get("timings", {})
    server_ready_ns = timings.get("total_ns", 0) if isinstance(timings, dict) else 0
    command_started = time.monotonic_ns()
    require_success(execution.exec(sandbox, ("/bin/true",)), "ready command")
    ready = time.monotonic_ns()
    return {
        "sequence": sequence,
        "request_id": request_id,
        "sandbox_id": sandbox,
        "request_to_ready_ns": ready - submitted,
        "ready_command_ns": ready - command_started,
        "sandboxfs_ready_ns": server_ready_ns,
    }


def launch_holder(
    execution: SandboxFSExecution,
    sandbox: str,
    size: int,
    pattern: str,
    scan_stride: int,
) -> None:
    code = HOLDER_CODE.format(
        size=size,
        pattern=pattern,
        scan_stride=scan_stride,
    )
    shell = (
        "rm -f /tmp/caden-holder-ready /tmp/caden-holder-scan; "
        f"nohup python3 -c {shlex.quote(code)} </dev/null "
        ">/tmp/caden-holder.log 2>&1 & "
        "echo $! >/tmp/caden-holder.pid"
    )
    require_success(execution.exec(sandbox, ("sh", "-lc", shell)), "launch holder")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = execution.exec(
            sandbox, ("sh", "-lc", "test -s /tmp/caden-holder-ready")
        )
        if response.get("exit_code") == 0:
            return
        time.sleep(0.05)
    raise RuntimeError(f"memory holder did not become ready in {sandbox}")


def scan_holder(execution: SandboxFSExecution, sandbox: str) -> None:
    require_success(execution.exec(sandbox, ("sh", "-lc", SCAN_SHELL)), "scan holder")


def wake_request(
    scheduler: StageAwareCaden,
    execution: SandboxFSExecution,
    sandbox: str,
) -> int:
    started = time.monotonic_ns()
    try:
        scheduler.on_report(sandbox, Stage.RESPONSE_WAKE)
        scheduler.on_report(sandbox, Stage.TOOL_BURST)
        scan_holder(execution, sandbox)
    finally:
        scheduler.on_report(sandbox, Stage.RESULT_PACK)
    return time.monotonic_ns() - started


def require_success(response: dict[str, object], operation: str) -> None:
    if response.get("exit_code") != 0:
        raise RuntimeError(f"{operation} failed: {response}")


def wait_until_running(
    scheduler: StageAwareCaden, request_id: str, *, timeout: float
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = scheduler.poll(request_id)
        if status.state is ReqState.RUNNING:
            return
        if status.state in {ReqState.REJECTED, ReqState.CANCELLED}:
            raise RuntimeError(
                f"request {request_id}: {status.state.value}: {status.result}"
            )
        scheduler.refresh_admission()
        time.sleep(0.01)
    raise TimeoutError(f"request {request_id} did not become running")


def build_report(
    args: argparse.Namespace,
    execution: SandboxFSExecution,
    samples: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    wake_latencies: list[int],
    scheduler: StageAwareCaden,
    pool: ElasticReadyPool | None,
    errors: list[str],
) -> dict[str, Any]:
    idle_used = samples[0]["host_used_bytes"] if samples else 0
    for sample in samples:
        sample["attributable_host_dram_bytes"] = max(
            0, sample["host_used_bytes"] - idle_used
        )
    active_phases = {
        "pool_prepare",
        "create",
        "allocate_wss",
        "hot",
        "llm_wait",
        "response_wake",
    }
    active = [
        sample["attributable_host_dram_bytes"]
        for sample in samples
        if sample["phase"] in active_phases
    ]
    waiting = [
        sample["attributable_host_dram_bytes"]
        for sample in samples
        if sample["phase"] == "llm_wait"
    ]
    waiting_cgroup = [
        sample["sandbox_memory_current_bytes"]
        for sample in samples
        if sample["phase"] == "llm_wait"
    ]
    waiting_swap = [
        sample["sandbox_memory_swap_bytes"]
        for sample in samples
        if sample["phase"] == "llm_wait"
    ]
    waiting_compressed = [
        sample["sandbox_memory_compressed_bytes"]
        for sample in samples
        if sample["phase"] == "llm_wait"
    ]
    ready_values = [request["request_to_ready_ns"] for request in requests]
    command_values = [request["ready_command_ns"] for request in requests]
    server_ready = [request["sandboxfs_ready_ns"] for request in requests]
    reclaim_events = [
        event for event in scheduler.events() if event.action == "reclaim"
    ]
    reclaim_errors = [
        event for event in scheduler.events() if event.action == "reclaim_error"
    ]
    pool_events = pool.events() if pool else []
    active_faults = [
        sample["sandbox_major_faults"]
        for sample in samples
        if sample["phase"] in active_phases
    ]
    summary = {
        "success": not errors and len(requests) == args.sandboxes,
        "requests": len(requests),
        "attributable_dram_active_mean_bytes": time_weighted_mean(
            samples, "attributable_host_dram_bytes", active_phases
        ),
        "attributable_dram_active_p95_bytes": percentile(active, 0.95),
        "attributable_dram_active_peak_bytes": max(active, default=0),
        "attributable_dram_wait_mean_bytes": time_weighted_mean(
            samples, "attributable_host_dram_bytes", {"llm_wait"}
        ),
        "attributable_dram_wait_p95_bytes": percentile(waiting, 0.95),
        "sandbox_dram_wait_mean_bytes": time_weighted_mean(
            samples, "sandbox_memory_current_bytes", {"llm_wait"}
        ),
        "sandbox_dram_wait_p95_bytes": percentile(waiting_cgroup, 0.95),
        "sandbox_swap_wait_mean_bytes": time_weighted_mean(
            samples, "sandbox_memory_swap_bytes", {"llm_wait"}
        ),
        "sandbox_swap_wait_peak_bytes": max(waiting_swap, default=0),
        "sandbox_compressed_wait_mean_bytes": time_weighted_mean(
            samples, "sandbox_memory_compressed_bytes", {"llm_wait"}
        ),
        "sandbox_compressed_wait_peak_bytes": max(waiting_compressed, default=0),
        "active_duration_ns": phase_duration(samples, active_phases),
        "llm_wait_observed_duration_ns": phase_duration(samples, {"llm_wait"}),
        "request_ready_p50_ns": percentile(ready_values, 0.50),
        "request_ready_p95_ns": percentile(ready_values, 0.95),
        "ready_command_p50_ns": percentile(command_values, 0.50),
        "ready_command_p95_ns": percentile(command_values, 0.95),
        "sandboxfs_ready_p50_ns": percentile(server_ready, 0.50),
        "sandboxfs_ready_p95_ns": percentile(server_ready, 0.95),
        "wake_p50_ns": percentile(wake_latencies, 0.50),
        "wake_p95_ns": percentile(wake_latencies, 0.95),
        "reclaimed_bytes": sum(event.bytes for event in reclaim_events),
        "reclaim_events": len(reclaim_events),
        "reclaim_errors": len(reclaim_errors),
        "major_faults_active_delta": (
            max(active_faults) - min(active_faults) if active_faults else 0
        ),
        "pool_hits": sum(event.action == "hit" for event in pool_events),
        "pool_misses": sum(event.action == "miss" for event in pool_events),
        "errors": errors,
    }
    return {
        "schema": "caden-sandboxfs-memory-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "config": vars(args) | {"output": str(args.output)},
        "host": host_manifest(execution),
        "memory_capabilities": execution.memory_capabilities(),
        "commits": {
            "caden": git_commit(REPOSITORY_ROOT),
            "sandboxfs": git_commit(REPOSITORY_ROOT / "third_party" / "sandboxfs"),
        },
        "requests": requests,
        "samples": samples,
        "scheduler_events": [event_json(event) for event in scheduler.events()],
        "pool_events": [event_json(event) for event in pool.events()] if pool else [],
        "wake_latencies_ns": wake_latencies,
        "summary": summary,
    }


def event_json(event: object) -> dict[str, object]:
    value = dataclasses.asdict(event)
    for key, item in list(value.items()):
        if hasattr(item, "value"):
            value[key] = item.value
    return value


def host_manifest(execution: SandboxFSExecution) -> dict[str, object]:
    system = run_json(
        (
            execution.config.ctl_path,
            "--socket",
            execution.config.socket_path,
            "system",
        )
    )
    return {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "sandboxfs": system,
        "swap": command_output(("swapon", "--show", "--bytes")),
        "zram": command_output(("zramctl", "--output-all")),
        "memory": dataclasses.asdict(execution.host_stat()),
    }


def run_json(argv: Sequence[str]) -> dict[str, object]:
    completed = subprocess.run(argv, check=True, capture_output=True, text=True)
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError(f"expected object from {argv}: {value!r}")
    return value


def command_output(argv: Sequence[str]) -> str:
    completed = subprocess.run(argv, check=False, capture_output=True, text=True)
    return (
        completed.stdout.strip()
        if completed.returncode == 0
        else completed.stderr.strip()
    )


def git_commit(path: Path) -> str:
    completed = subprocess.run(
        (
            "git",
            "-c",
            f"safe.directory={path}",
            "-C",
            str(path),
            "rev-parse",
            "HEAD",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def phase_duration(samples: list[dict[str, Any]], phases: set[str]) -> int:
    return sum(
        right["monotonic_ns"] - left["monotonic_ns"]
        for left, right in zip(samples, samples[1:])
        if left["phase"] in phases
    )


def time_weighted_mean(
    samples: list[dict[str, Any]], metric: str, phases: set[str]
) -> int:
    duration = phase_duration(samples, phases)
    if duration <= 0:
        return 0
    area = sum(
        left[metric] * (right["monotonic_ns"] - left["monotonic_ns"])
        for left, right in zip(samples, samples[1:])
        if left["phase"] in phases
    )
    return round(area / duration)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
