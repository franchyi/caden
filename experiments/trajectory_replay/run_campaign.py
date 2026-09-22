#!/usr/bin/env python3
"""Replay normalized agent tool trajectories through Caden and SandboxFS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROGRESS_LOCK = threading.Lock()
# Set by SIGTERM (for example a host-limit monitor): replay threads stop at the
# next wait or tool boundary so the normal cleanup path destroys every sandbox.
ABORT = threading.Event()


def _terminate(signum: int, frame: object) -> None:
    ABORT.set()
    raise SystemExit(f"terminated by signal {signum}; preserving partial results")


def emit_progress(path: Path | None, event: dict[str, Any]) -> None:
    if path is None:
        return
    with PROGRESS_LOCK:
        with path.open("a") as output:
            output.write(json.dumps({"observed_unix": time.time(), **event}) + "\n")

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from caden.policy import CadenPolicyConfig, StageAwareCaden  # noqa: E402
from caden.pool import ElasticReadyPool, ReadyPoolConfig  # noqa: E402
from caden.sandboxfs_backend import SandboxFSConfig, SandboxFSExecution  # noqa: E402
from caden.scheduler import ReqState  # noqa: E402
from caden.types import (  # noqa: E402
    AgentTask,
    CpuClass,
    ReclaimMode,
    ResidencyMode,
    Stage,
    StageContext,
    Tier,
)

from experiments.sandboxfs_memory.run_campaign import (  # noqa: E402
    Sampler,
    event_json,
    git_commit,
    host_manifest,
    percentile,
    phase_duration,
    require_success,
    time_weighted_mean,
    wait_until_running,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workloads-dir", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--base-socket-map", type=Path)
    parser.add_argument("--source-provenance", type=Path)
    parser.add_argument("--constant-cpu", action="store_true")
    parser.add_argument("--mode", choices=("baseline", "t1"), required=True)
    parser.add_argument(
        "--policy",
        type=lambda value: "caden" if value == "orca" else value,
        choices=("static", "caden", "fixed", "elapsed", "request-aware"),
        required=True,
    )
    parser.add_argument(
        "--requests",
        type=nonnegative_int,
        default=0,
        help="fixed total workload count; zero uses the whole manifest",
    )
    parser.add_argument(
        "--active-sandboxes",
        type=nonnegative_int,
        default=0,
        help="maximum concurrent population; zero launches one full-population wave",
    )
    parser.add_argument("--wait-scale", type=positive_float, default=1.0)
    parser.add_argument("--arrival-driven", action="store_true",
                        help="independent scheduled sessions; no wave/create barrier")
    parser.add_argument("--sample-ms", type=positive_float, default=100.0)
    parser.add_argument("--reclaim-grace-ms", type=nonnegative_float, default=100.0)
    parser.add_argument("--reclaim-tier", choices=("ssd", "compressed", "cxl"), default="ssd")
    parser.add_argument(
        "--memory-tier-backend", choices=("ssd", "cxl"), default="ssd",
        help="explicit byte-movement backend; never substituted for another",
    )
    parser.add_argument("--cxl-pager-socket", help="control socket of a running crate_pagerd")
    parser.add_argument("--cxl-restore-mode", choices=("eager", "lazy"), default="eager")
    parser.add_argument("--cxl-per-sandbox-max-mib", type=nonnegative_int, default=0)
    parser.add_argument("--cxl-no-file-cache-reclaim", action="store_true",
                        help="disable the companion file-cache-only kernel reclaim")
    parser.add_argument(
        "--reclaim-mode", choices=("balanced", "file", "anon"), default="balanced"
    )
    parser.add_argument("--hot-reserve-mib", type=nonnegative_int, default=0)
    parser.add_argument("--service-cache-roots", type=Path,
                        help="explicit base -> delegated daemon cgroup JSON; opt-in")
    parser.add_argument("--service-cache-reserve-mib", type=positive_int, default=128)
    parser.add_argument("--service-cache-chunk-mib", type=positive_int, default=16)
    parser.add_argument("--service-cache-interval-ms", type=positive_float, default=100)
    parser.add_argument("--service-cache-budget-mib", type=positive_int, default=16)
    parser.add_argument(
        "--restore-profile-hot-reserve",
        type=profile_hot_reserve,
        action="append",
        default=[],
        metavar="PROFILE=MIB",
    )
    parser.add_argument("--minimum-reclaim-mib", type=nonnegative_int, default=1)
    parser.add_argument("--minimum-cold-ms", type=nonnegative_float, default=50.0)
    parser.add_argument(
        "--minimum-memory-time-mib-ms", type=nonnegative_float, default=0.0
    )
    parser.add_argument("--estimated-demote-ms", type=nonnegative_float, default=60.0)
    parser.add_argument("--estimated-restore-ms", type=nonnegative_float, default=30.0)
    parser.add_argument("--max-early-wake-probability", type=probability, default=0.25)
    parser.add_argument("--prediction-interval-ms", type=positive_float, default=50.0)
    parser.add_argument("--prediction-min-samples", type=positive_int, default=3)
    parser.add_argument("--prediction-prior-ms", type=positive_float, default=1000.0)
    parser.add_argument("--speculative-restore", action="store_true")
    parser.add_argument(
        "--speculative-restore-probability", type=probability, default=0.5
    )
    parser.add_argument(
        "--speculative-restore-lead-ms", type=nonnegative_float, default=100.0
    )
    parser.add_argument(
        "--speculative-queue-margin-ms", type=nonnegative_float, default=20.0
    )
    parser.add_argument("--max-speculative-restores", type=positive_int, default=1)
    parser.add_argument(
        "--compression-max-wait-ms", type=nonnegative_float, default=0.0
    )
    parser.add_argument("--allow-zswap-compression", action="store_true")
    parser.add_argument("--zswap-max-mib", type=nonnegative_int, default=0)
    parser.add_argument(
        "--speculative-prefetch-file", type=Path, action="append", default=[]
    )
    parser.add_argument(
        "--speculative-prefetch-root", type=Path, action="append", default=[]
    )
    parser.add_argument("--allow-process-madvise-restore", action="store_true")
    parser.add_argument("--speculative-madvise-mib", type=positive_int, default=256)
    parser.add_argument("--speculative-madvise-passes", type=positive_int, default=1)
    parser.add_argument(
        "--speculative-madvise-settle-ms", type=nonnegative_float, default=0.0
    )
    parser.add_argument(
        "--speculative-madvise-advice",
        choices=("willneed", "populate-read"),
        default="willneed",
    )
    parser.add_argument(
        "--wake-reserve-per-waiter-mib", type=nonnegative_int, default=0
    )
    parser.add_argument("--wake-reserve-safety-mib", type=nonnegative_int, default=0)
    parser.add_argument(
        "--wake-reserve-horizon-ms", type=nonnegative_float, default=250.0
    )
    parser.add_argument("--wake-slo-ms", type=nonnegative_float, default=0.0)
    parser.add_argument("--wake-p99-slo-ms", type=nonnegative_float, default=0.0)
    parser.add_argument("--turn-slo-ms", type=nonnegative_float, default=0.0)
    parser.add_argument("--turn-p99-slo-ms", type=nonnegative_float, default=0.0)
    parser.add_argument("--slo-min-samples", type=positive_int, default=20)
    parser.add_argument("--slo-window-samples", type=positive_int, default=100)
    parser.add_argument("--slo-cooldown-seconds", type=nonnegative_float, default=30.0)
    parser.add_argument("--synchronize-response-commits", action="store_true")
    parser.add_argument("--max-reclaims", type=positive_int, default=2)
    parser.add_argument("--max-movements", type=positive_int, default=2)
    parser.add_argument("--confirmed-movement-reserve", type=nonnegative_int, default=0)
    parser.add_argument("--max-admissions", type=positive_int, default=4)
    parser.add_argument("--max-wakes", type=positive_int, default=2)
    parser.add_argument("--pool-target", type=nonnegative_int, default=0)
    parser.add_argument("--pool-max", type=nonnegative_int, default=0)
    parser.add_argument("--queue-aware-pool", action="store_true",
                        help="precreate only bases with already-arrived pending requests")
    parser.add_argument("--restore-mode", choices=("prewarm", "thaw-only"), default="prewarm",
                        help="confirmed wake: legacy probe or verified thaw followed by the timed real tool")
    parser.add_argument("--estimated-wss-mib", type=positive_int, default=128)
    parser.add_argument("--dram-reserve-mib", type=nonnegative_int, default=2048)
    parser.add_argument("--ctl", default="sandboxfsctl")
    parser.add_argument("--socket", default="/run/sandboxfsd.sock")
    parser.add_argument("--drop-caches", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run_campaign.py must run as root")
    if args.pool_target > args.pool_max:
        raise SystemExit("--pool-target must not exceed --pool-max")
    if args.speculative_prefetch_file and not args.speculative_prefetch_root:
        raise SystemExit(
            "--speculative-prefetch-file requires --speculative-prefetch-root"
        )
    signal.signal(signal.SIGTERM, _terminate)
    memory_tier = make_memory_tier(args)
    workloads, manifest = load_workloads(args.workloads_dir, args.requests)
    active_limit = args.active_sandboxes or len(workloads)
    if args.arrival_driven:
        from experiments.swebench_verified.session_workloads import validate_arrivals
        validate_arrivals(workloads, manifest)
        if args.synchronize_response_commits:
            raise SystemExit("arrival-driven sessions cannot use a response barrier")
    if active_limit > len(workloads):
        raise SystemExit("--active-sandboxes must not exceed the fixed workload count")
    if (
        args.synchronize_response_commits
        and len({workload["tool_count"] for workload in workloads}) != 1
    ):
        raise SystemExit("--synchronize-response-commits requires equal tool counts")
    if args.drop_caches:
        os.sync()
        Path("/proc/sys/vm/drop_caches").write_text("3\n")

    backend_options = {}
    if args.base_socket_map:
        from experiments.swebench_verified.routing import RoutedCtlRunner
        backend_options["runner"] = RoutedCtlRunner(json.loads(args.base_socket_map.read_text()))
    config_options = {}
    if args.service_cache_roots:
        config_options.update(
            service_cache_roots={base: Path(path) for base, path in
                                 json.loads(args.service_cache_roots.read_text()).items()},
            service_cache_reserve_bytes=args.service_cache_reserve_mib << 20,
            service_cache_chunk_bytes=args.service_cache_chunk_mib << 20,
            service_cache_interval_seconds=args.service_cache_interval_ms / 1000,
            service_cache_bytes_per_interval=args.service_cache_budget_mib << 20,
        )
    if args.constant_cpu:
        config_options["cpu_weights"] = {stage: 100 for stage in CpuClass}
    execution = SandboxFSExecution(
        SandboxFSConfig(
            ctl_path=args.ctl,
            socket_path=args.socket,
            mode=args.mode,
            restore_mode=args.restore_mode,
            **config_options,
            allow_zswap_compression=args.allow_zswap_compression,
            zswap_max_bytes=args.zswap_max_mib << 20,
            speculative_prefetch_profiles={
                "default": tuple(args.speculative_prefetch_file)
            },
            speculative_prefetch_roots=tuple(args.speculative_prefetch_root),
            allow_process_madvise_restore=args.allow_process_madvise_restore,
            speculative_madvise_max_bytes=args.speculative_madvise_mib << 20,
            speculative_madvise_settle_seconds=(
                args.speculative_madvise_settle_ms / 1000
            ),
            speculative_madvise_passes=args.speculative_madvise_passes,
            speculative_madvise_advice=args.speculative_madvise_advice,
        ),
        **backend_options,
        memory_tier=memory_tier,
    )
    pool = make_pool(args, execution, pending_bases=(
        [] if args.arrival_driven else
        [str(w.get("base", args.base)) for w in workloads] if args.queue_aware_pool else None))
    scheduler = StageAwareCaden(
        execution,
        CadenPolicyConfig(
            estimated_wss_bytes=args.estimated_wss_mib << 20,
            fixed_dram_reserve_bytes=args.dram_reserve_mib << 20,
            wake_reserve_per_waiter_bytes=args.wake_reserve_per_waiter_mib << 20,
            wake_reserve_safety_bytes=args.wake_reserve_safety_mib << 20,
            wake_reserve_horizon_seconds=args.wake_reserve_horizon_ms / 1000,
            reclaim_grace_seconds=args.reclaim_grace_ms / 1000,
            reclaim_tier={
                "ssd": Tier.SSD, "compressed": Tier.COMPRESSED, "cxl": Tier.CXL,
            }[args.reclaim_tier],
            reclaim_mode={
                "balanced": ReclaimMode.BALANCED,
                "file": ReclaimMode.FILE_ONLY,
                "anon": ReclaimMode.ANON_ONLY,
            }[args.reclaim_mode],
            residency_mode={
                "static": ResidencyMode.FIXED_GRACE,
                "caden": ResidencyMode.FIXED_GRACE,
                "fixed": ResidencyMode.FIXED_GRACE,
                "elapsed": ResidencyMode.ELAPSED,
                "request-aware": ResidencyMode.REQUEST_AWARE,
            }[args.policy],
            sandbox_hot_reserve_bytes=args.hot_reserve_mib << 20,
            restore_profile_hot_reserve_bytes=tuple(args.restore_profile_hot_reserve),
            minimum_reclaim_bytes=args.minimum_reclaim_mib << 20,
            minimum_cold_residency_seconds=args.minimum_cold_ms / 1000,
            minimum_memory_time_byte_seconds=(
                args.minimum_memory_time_mib_ms * (1 << 20) / 1000
            ),
            estimated_demote_seconds=args.estimated_demote_ms / 1000,
            estimated_restore_seconds=args.estimated_restore_ms / 1000,
            maximum_early_wake_probability=args.max_early_wake_probability,
            prediction_interval_seconds=args.prediction_interval_ms / 1000,
            prediction_minimum_class_samples=args.prediction_min_samples,
            prediction_prior_mean_seconds=args.prediction_prior_ms / 1000,
            speculative_restore_enabled=args.speculative_restore,
            speculative_restore_probability=args.speculative_restore_probability,
            speculative_restore_lead_seconds=args.speculative_restore_lead_ms / 1000,
            speculative_restore_queue_margin_seconds=args.speculative_queue_margin_ms
            / 1000,
            max_concurrent_speculative_restores=args.max_speculative_restores,
            compression_max_wait_seconds=args.compression_max_wait_ms / 1000,
            wake_latency_slo_seconds=args.wake_slo_ms / 1000,
            wake_latency_p99_slo_seconds=args.wake_p99_slo_ms / 1000,
            turn_latency_slo_seconds=args.turn_slo_ms / 1000,
            turn_latency_p99_slo_seconds=args.turn_p99_slo_ms / 1000,
            slo_minimum_samples=args.slo_min_samples,
            slo_window_samples=args.slo_window_samples,
            slo_cooldown_seconds=args.slo_cooldown_seconds,
            max_concurrent_admissions=args.max_admissions,
            max_concurrent_reclaims=args.max_reclaims,
            max_concurrent_wakes=args.max_wakes,
            max_concurrent_movements=args.max_movements,
            confirmed_movement_reserve=args.confirmed_movement_reserve,
            reclaim_enabled=args.policy != "static",
        ),
        ready_pool=pool,
    )
    service_names = ({Path(socket).stem + ".service" for socket in
                      json.loads(args.base_socket_map.read_text()).values()}
                     if args.base_socket_map else None)
    sampler_options = {}
    if service_names and not all(re.fullmatch(r"crate-sv-\d{2}\.service", name) for name in service_names):
        # An isolated run owns differently named task daemons; sample exactly those.
        sampler_options = {
            "service_glob": os.path.commonprefix(sorted(service_names)).rstrip("0123456789") + "*.service",
            "service_regex": "|".join(re.escape(name) for name in sorted(service_names)),
        }
    sampler = Sampler(execution, args.sample_ms / 1000, service_names=service_names, **sampler_options)
    progress_path = args.output.with_suffix(".progress.jsonl")
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    last_live = [0]
    def live_sample(sample):
        if sample['monotonic_ns'] - last_live[0] < 5_000_000_000:
            return
        last_live[0] = sample['monotonic_ns']
        emit_progress(progress_path, {'type': 'memory_progress',
            'monotonic_ns': sample['monotonic_ns'], 'phase': sample['phase'],
            'service_bytes': sample['task_service_cgroup_sum_bytes'],
            'daemon_bytes': sample['task_daemon_cgroup_sum_bytes'],
            'sandbox_bytes': sample['sandbox_memory_current_bytes'],
            'observer_bytes': sample['observer_cgroup_current_bytes'],
            'host_available_bytes': sample['host_available_bytes'],
            'live_sandboxes': len(sample['sandboxes']),
            'waiting_sandboxes': sum(s['stage'] == Stage.LLM_WAIT.value for s in sample['sandboxes'])})
    if args.arrival_driven:
        sampler.sample_callback = live_sample
    sampler.start()
    if args.base_socket_map:
        time.sleep(1.0)  # pre-work idle reference, excluded from workload integrals
    request_ids: list[str] = []
    requests: list[dict[str, Any]] = []
    replay_results: list[dict[str, Any]] = []
    replay_lock = threading.Lock()
    errors: list[str] = []
    raised: BaseException | None = None
    try:
        # Cache management is part of the timed lifecycle, never hidden in
        # preflight. Baseline has no controller unless explicitly requested.
        if execution.service_cache:
            sampler.set_phase("pool_prepare" if pool is not None else "create")
            execution.service_cache.start()
        if pool is not None and args.pool_target > 0 and not args.arrival_driven:
            sampler.set_phase("pool_prepare")
            pool.start(str(workloads[0].get("base", args.base)))
            pool.wait_for_refill(600)

        if args.arrival_driven:
            sampler.set_phase("trace_replay")  # Includes arrivals, provisioning and tools.
            run_arrival_sessions(scheduler, execution, workloads, active_limit,
                                 args.wait_scale, requests, replay_results, request_ids, progress_path, pool)
        waves = [] if args.arrival_driven else workload_waves(workloads, active_limit)
        for wave_index, wave in enumerate(waves):
            sampler.set_phase("create")
            pending: list[tuple[int, dict[str, Any], str, int]] = []
            wave_start = wave_index * active_limit
            for wave_position, workload in enumerate(wave):
                sequence = wave_start + wave_position
                submitted = time.monotonic_ns()
                request_id = scheduler.submit(AgentTask([], str(workload.get("base", args.base))))
                request_ids.append(request_id)
                pending.append((sequence, workload, request_id, submitted))
            wave_requests: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=len(wave)) as clients:
                futures = [
                    clients.submit(
                        finish_create,
                        scheduler,
                        execution,
                        sequence,
                        workload,
                        request_id,
                        submitted,
                    )
                    for sequence, workload, request_id, submitted in pending
                ]
                for future in as_completed(futures):
                    request = future.result()
                    request["wave"] = wave_index
                    request["wave_position"] = request["sequence"] - wave_start
                    wave_requests.append(request)
            wave_requests.sort(key=lambda item: item["sequence"])
            requests.extend(wave_requests)

            sampler.set_phase("trace_replay")
            barrier = threading.Barrier(len(wave_requests))
            commit_barrier = (
                threading.Barrier(len(wave_requests))
                if args.synchronize_response_commits
                else None
            )
            with ThreadPoolExecutor(max_workers=len(wave_requests)) as workers:
                futures = [
                    workers.submit(
                        replay_one,
                        scheduler,
                        execution,
                        request,
                        args.wait_scale,
                        barrier,
                        commit_barrier,
                        progress_path,
                    )
                    for request in wave_requests
                ]
                for future in as_completed(futures):
                    result = future.result()
                    result["wave"] = wave_index
                    with replay_lock:
                        replay_results.append(result)
                    emit_progress(progress_path, {"type": "task_complete", "sequence": result["sequence"],
                        "trajectory_id": result["trajectory_id"], "tools": len(result["tools"]),
                        "error": result["error"], "fidelity_errors": result.get("fidelity_errors", [])})
            # Completed sandboxes must be destroyed or cleanly returned to the
            # bounded pool before the next wave admits more work.
            scheduler.wait_for_background(timeout=600)
        requests.sort(key=lambda item: item["sequence"])
        replay_results.sort(key=lambda item: item["sequence"])
        sampler.set_phase("cleanup")
        if pool is not None:
            pool.trim(0)
    except BaseException as error:
        errors.append(f"{type(error).__name__}: {error}")
        raised = error
    finally:
        if execution.service_cache:
            try:
                execution.service_cache.close()
                errors.extend("service cache: " + e["error"] for e in
                              execution.service_cache.events if "error" in e)
            except Exception as error:
                errors.append(f"service cache cleanup: {error}")
        for request_id in request_ids:
            if scheduler.poll(request_id).state in {ReqState.QUEUED, ReqState.RUNNING}:
                try:
                    scheduler.cancel(request_id)
                except Exception as error:
                    errors.append(f"cleanup {request_id}: {error}")
        scheduler.close()
        sampler.stop()
        report = build_report(
            args,
            manifest,
            execution,
            sampler.samples,
            requests,
            replay_results,
            scheduler,
            pool,
            errors,
        )
        report["service_cache_events"] = execution.service_cache.events if execution.service_cache else []
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        print(json.dumps(report["summary"], indent=2))
        print(f"report written to {args.output}")
    if raised is not None:
        raise raised
    return 0


def workload_waves(
    workloads: list[dict[str, Any]], active_limit: int
) -> list[list[dict[str, Any]]]:
    if active_limit <= 0:
        raise ValueError("active_limit must be positive")
    return [
        workloads[start : start + active_limit]
        for start in range(0, len(workloads), active_limit)
    ]


def run_arrival_sessions(scheduler, execution, workloads, active_limit, wait_scale,
                         requests, results, request_ids, progress_path, pool=None):
    """Fixed open-loop session arrivals, closed-loop recorded waits within each.

    Admission queueing is included in request-to-ready. No look-ahead is passed
    to Caden. A permit covers create through normal lifecycle destruction.
    """
    origin = time.monotonic_ns()
    permits = threading.BoundedSemaphore(active_limit)
    lock = threading.Lock()

    def session(sequence, workload):
        planned = origin + round(workload["arrival_offset_ms"] * 1e6)
        if ABORT.wait(max(0, (planned - time.monotonic_ns()) / 1e9)):
            raise RuntimeError("aborted before scheduled arrival")
        arrived = time.monotonic_ns()
        if pool is not None:
            pool.add_pending(str(workload['base']))
        emit_progress(progress_path, {"type": "session_arrival", "request_sequence": sequence,
            "planned_arrival_ns": planned, "observed_arrival_ns": arrived})
        while not permits.acquire(timeout=.1):
            if ABORT.is_set():
                raise RuntimeError("aborted in admission queue")
        try:
            submitted = time.monotonic_ns()
            request_id = scheduler.submit(AgentTask([], str(workload["base"])))
            with lock:
                request_ids.append(request_id)
            request = finish_create(scheduler, execution, sequence, workload, request_id, planned)
            request.update(arrival_offset_ms=workload["arrival_offset_ms"],
                           serving_origin_ns=origin, planned_arrival_ns=planned,
                           observed_arrival_ns=arrived, submitted_ns=submitted,
                           ready_ns=planned + request["request_to_ready_ns"],
                           replica=workload.get("replica"))
            with lock:
                requests.append(request)
            emit_progress(progress_path, {"type": "session_ready", "request_sequence": sequence,
                "request_to_ready_ns": request["request_to_ready_ns"], "ready_ns": request["ready_ns"]})
            result = replay_one(scheduler, execution, request, wait_scale, None, None, progress_path)
            with lock:
                results.append(result)
            emit_progress(progress_path, {"type": "task_complete", "sequence": sequence,
                "trajectory_id": result["trajectory_id"], "tools": len(result["tools"]),
                "error": result["error"], "fidelity_errors": result.get("fidelity_errors", [])})
        finally:
            permits.release()

    with ThreadPoolExecutor(max_workers=len(workloads)) as workers:
        futures = [workers.submit(session, i, w) for i, w in enumerate(workloads)]
        try:
            for future in as_completed(futures):
                future.result()
        except BaseException:
            ABORT.set()
            raise
    scheduler.wait_for_background(timeout=600)


def load_workloads(
    root: Path, limit: int = 0
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema") not in {"caden-tool-trajectory-manifest-v1", "orca-tool-trajectory-manifest-v1"}:
        raise ValueError(f"invalid workload manifest: {manifest_path}")
    entries = manifest.get("workloads")
    if not isinstance(entries, list) or not entries:
        raise ValueError("workload manifest is empty")
    if limit:
        entries = entries[:limit]
    workloads: list[dict[str, Any]] = []
    for entry in entries:
        path = (root / entry["path"]).resolve()
        if root.resolve() not in path.parents:
            raise ValueError(f"unsafe workload path: {path}")
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != entry["sha256"]:
            raise ValueError(f"workload checksum mismatch: {path}")
        workload = json.loads(payload)
        if workload.get("schema") not in {"caden-tool-trajectory-v1", "orca-tool-trajectory-v1"}:
            raise ValueError(f"invalid workload: {path}")
        workloads.append(workload)
    return workloads, manifest


def finish_create(
    scheduler: StageAwareCaden,
    execution: SandboxFSExecution,
    sequence: int,
    workload: dict[str, Any],
    request_id: str,
    submitted: int,
) -> dict[str, Any]:
    wait_until_running(scheduler, request_id, timeout=600)
    sandbox = scheduler.request_sandbox(request_id)
    if sandbox is None:
        raise RuntimeError(f"request {request_id} has no sandbox")
    state = execution.state(sandbox)
    timings = state.get("timings", {})
    command_started = time.monotonic_ns()
    require_success(execution.exec(sandbox, ("/bin/true",)), "ready command")
    ready = time.monotonic_ns()
    return {
        "sequence": sequence,
        "request_id": request_id,
        "sandbox_id": sandbox,
        "trajectory_id": workload["source"]["trajectory_id"],
        "instance_id": workload["source"]["instance_id"],
        "workload": workload,
        "request_to_ready_ns": ready - submitted,
        "ready_command_ns": ready - command_started,
        "sandboxfs_ready_ns": timings.get("total_ns", 0)
        if isinstance(timings, dict)
        else 0,
        "sandboxfs_timings": timings,
        "base": workload.get("base"),
    }


def replay_one(
    scheduler: StageAwareCaden,
    execution: SandboxFSExecution,
    request: dict[str, Any],
    wait_scale: float,
    barrier: threading.Barrier | None,
    commit_barrier: threading.Barrier | None,
    progress_path: Path | None = None,
) -> dict[str, Any]:
    sandbox = request["sandbox_id"]
    result: dict[str, Any] = {
        "sequence": request["sequence"],
        "trajectory_id": request["trajectory_id"],
        "sandbox_id": sandbox,
        "tools": [],
        "error": "",
    }
    if barrier is not None:
        barrier.wait(timeout=600)
    try:
        for event in request["workload"]["events"]:
            if event["type"] == "wait":
                execution.report(
                    sandbox,
                    Stage.LLM_WAIT,
                    StageContext(
                        request_class=str(event.get("request_class", "default")),
                        restore_profile=str(event.get("restore_profile", "default")),
                    ),
                )
                started = time.monotonic_ns()
                result.setdefault("requested_waits_ms", []).append(event["duration_ms"] * wait_scale)
                if ABORT.wait((event["duration_ms"] / 1000) * wait_scale):
                    raise RuntimeError("campaign aborted during a model wait")
                result.setdefault("waits_ns", []).append(time.monotonic_ns() - started)
                continue
            if ABORT.is_set():
                raise RuntimeError("campaign aborted before a tool call")
            turn_started = time.monotonic_ns()
            command_started = 0
            rpc_ended = 0
            response: dict[str, object] = {}
            try:
                if commit_barrier is not None:
                    commit_barrier.wait(timeout=600)
                execution.report(sandbox, Stage.RESPONSE_WAKE)
                execution.report(sandbox, Stage.TOOL_BURST)
                command_started = time.monotonic_ns()
                response = execution.exec(sandbox, tuple(event["argv"]))
                rpc_ended = time.monotonic_ns()
                expected = event.get("expected_exit_code", 0)
                if response.get("exit_code") != expected:
                    result.setdefault("fidelity_errors", []).append(
                        {"sequence": event["sequence"], "expected": expected,
                         "actual": response.get("exit_code")}
                    )
                    if "expected_exit_code" not in event:
                        require_success(response, f"proxy {event['operation']}")
            finally:
                pack_started = time.monotonic_ns()
                execution.report(sandbox, Stage.RESULT_PACK)
            ended = time.monotonic_ns()
            result["tools"].append(
                {
                    "sequence": event["sequence"],
                    "source_tool": event["source_tool"],
                    "operation": event["operation"],
                    "source_arguments_sha256": event["source_arguments_sha256"],
                    "exit_code": response.get("exit_code"),
                    "wake_restore_ns": command_started - turn_started,
                    "command_ns": ended - command_started,
                    "exec_rpc_ns": rpc_ended - command_started,
                    "server_command_ns": response.get("duration_ns"),
                    "result_validation_ns": pack_started - rpc_ended,
                    "result_pack_ns": ended - pack_started,
                    "turn_ns": ended - turn_started,
                    "stdout_bytes": len(str(response.get("stdout", "")).encode()),
                    "stderr_bytes": len(str(response.get("stderr", "")).encode()),
                    "expected_exit_code": event.get("expected_exit_code", 0),
                    "stdout_sha256": hashlib.sha256(str(response.get("stdout", "")).encode()).hexdigest(),
                    "stderr_sha256": hashlib.sha256(str(response.get("stderr", "")).encode()).hexdigest(),
                }
            )
            emit_progress(progress_path, {"type": "tool_complete", "request_sequence": request["sequence"],
                "trajectory_id": request["trajectory_id"], **result["tools"][-1]})
        if "fingerprint_expected" in request["workload"]:
            import shlex
            from experiments.swebench_verified.capture import FINGERPRINT, argv_for
            audit_started = time.monotonic_ns()
            execution.report(sandbox, Stage.RESPONSE_WAKE)
            execution.report(sandbox, Stage.TOOL_BURST)
            fingerprint = execution.exec(sandbox, argv_for("python -c " + shlex.quote(FINGERPRINT)))
            execution.report(sandbox, Stage.RESULT_PACK)
            result["validation_audit_ns"] = time.monotonic_ns() - audit_started
            result["fingerprint"] = fingerprint
            observed = json.loads(str(fingerprint.get("stdout", "{}")))
            if observed != request["workload"]["fingerprint_expected"]:
                result.setdefault("fidelity_errors", []).append({"final_workspace": "mismatch"})
        final_stat = execution.stat(sandbox)
        result["final_major_faults"] = final_stat.major_faults
        result["final_swap_bytes"] = final_stat.mem_swap_bytes
        result["final_memory_stat"] = memory_stat_split(execution, sandbox)
        result["tier_accounting"] = execution.tier_accounting(sandbox).as_json()
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
        if commit_barrier is not None:
            commit_barrier.abort()
    finally:
        scheduler.complete(sandbox, "ok" if not result["error"] else "error")
    return result


def make_memory_tier(args: argparse.Namespace):
    """Select the byte-movement backend explicitly; None keeps the SSD default."""
    if args.memory_tier_backend == "ssd":
        if args.reclaim_tier == "cxl":
            raise SystemExit("--reclaim-tier cxl requires --memory-tier-backend cxl")
        return None
    if args.reclaim_tier != "cxl":
        raise SystemExit("--memory-tier-backend cxl requires --reclaim-tier cxl")
    if args.compression_max_wait_ms > 0 or args.allow_zswap_compression:
        raise SystemExit("zswap compression is an SSD-backend option, not a CXL codec")
    if not args.cxl_pager_socket:
        raise SystemExit("--memory-tier-backend cxl requires --cxl-pager-socket")
    from caden.cxl_tier import CXLTierBackend, CXLTierConfig

    return CXLTierBackend(
        CXLTierConfig(
            socket_path=args.cxl_pager_socket,
            restore_mode=args.cxl_restore_mode,
            per_sandbox_max_bytes=args.cxl_per_sandbox_max_mib << 20,
            file_cache_reclaim=not args.cxl_no_file_cache_reclaim,
        )
    )


def tier_store_stats(execution: SandboxFSExecution) -> dict[str, str]:
    store_stats = getattr(execution.memory_tier, "store_stats", None)
    if store_stats is None:
        return {}
    try:
        return dict(store_stats())
    except Exception as error:  # noqa: BLE001 - the report must still be written
        return {"unavailable": f"{type(error).__name__}: {error}"}


def memory_stat_split(execution: SandboxFSExecution, sandbox: str) -> dict[str, int]:
    """Anonymous, file and tmpfs charge of one sandbox; they are not interchangeable."""
    wanted = {"anon", "file", "shmem", "file_mapped", "kernel", "pgmajfault"}
    try:
        lines = (execution._record(sandbox).cgroup_path / "memory.stat").read_text().splitlines()
    except OSError:
        return {}
    return {key: int(value) for key, value in (line.split() for line in lines) if key in wanted}


def make_pool(
    args: argparse.Namespace, execution: SandboxFSExecution,
    pending_bases: list[str] | None = None,
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
        pending_bases=pending_bases,
    )


def build_report(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    execution: SandboxFSExecution,
    samples: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    replays: list[dict[str, Any]],
    scheduler: StageAwareCaden,
    pool: ElasticReadyPool | None,
    errors: list[str],
) -> dict[str, Any]:
    idle_samples = [s for s in samples if s["phase"] == "idle"] or samples[:1]
    idle_used = statistics.median(s["host_used_bytes"] for s in idle_samples) if idle_samples else 0
    idle_physical = statistics.median(s.get("host_total_minus_free_bytes", 0) for s in idle_samples) if idle_samples else 0
    for sample in samples:
        sample["attributable_host_dram_bytes"] = max(
            0, sample["host_used_bytes"] - idle_used
        )
        sample["signed_host_available_delta_bytes"] = sample["host_used_bytes"] - idle_used
        sample["signed_host_physical_delta_bytes"] = sample.get("host_total_minus_free_bytes", 0) - idle_physical
        sample["daemon_plus_sandbox_cgroup_bytes"] = sample.get("task_daemon_cgroup_sum_bytes", 0) + sample["sandbox_memory_current_bytes"]
        sample["active_sandbox_count"] = len(sample["sandboxes"])
        sample["waiting_sandboxes"] = sum(
            sandbox["stage"] == Stage.LLM_WAIT.value for sandbox in sample["sandboxes"]
        )
        sample["waiting_sandbox_memory_bytes"] = sum(
            sandbox["memory_current_bytes"]
            for sandbox in sample["sandboxes"]
            if sandbox["stage"] == Stage.LLM_WAIT.value
        )
    active_phases = {"pool_prepare", "create", "trace_replay"}
    active_samples = [sample for sample in samples if sample["phase"] in active_phases]
    host_values = [sample["attributable_host_dram_bytes"] for sample in active_samples]
    sandbox_values = [
        sample["sandbox_memory_current_bytes"] for sample in active_samples
    ]
    swap_values = [sample["sandbox_memory_swap_bytes"] for sample in active_samples]
    compressed_values = [
        sample["sandbox_memory_compressed_bytes"] for sample in active_samples
    ]
    faults = [sample["sandbox_major_faults"] for sample in active_samples]
    tools = [tool for replay in replays for tool in replay["tools"]]
    ready_values = [request["request_to_ready_ns"] for request in requests]
    operations = Counter(tool["operation"] for tool in tools)
    decision_events = scheduler.events()
    reclaim_events = [event for event in decision_events if event.action == "reclaim"]
    reclaim_errors = [
        event for event in decision_events if event.action == "reclaim_error"
    ]
    speculation = speculation_accounting(decision_events)
    pool_events = pool.events() if pool else []
    manifest_path = args.workloads_dir / "manifest.json"
    expected_tools = sum(request["workload"]["tool_count"] for request in requests)
    active_duration_ns = phase_duration(samples, active_phases)
    trace_replay_duration_ns = phase_duration(samples, {"trace_replay"})
    active_limit = args.active_sandboxes or max(1, len(requests))
    wave_count = 0 if getattr(args, 'arrival_driven', False) else (
        (len(requests) + active_limit - 1) // active_limit if requests else 0
    )
    summary = {
        "success": (
            not errors
            and len(replays) == len(requests)
            and len(tools) == expected_tools
            and all(not replay["error"] for replay in replays)
            and all(not replay.get("fidelity_errors") for replay in replays)
        ),
        "requests": len(requests),
        "completed_requests": len(replays),
        "active_sandbox_limit": active_limit,
        "wave_count": wave_count,
        "peak_observed_sandboxes": max(
            (sample["active_sandbox_count"] for sample in active_samples), default=0
        ),
        "expected_tool_calls": expected_tools,
        "completed_tool_calls": len(tools),
        "completed_turns_per_second": (
            len(tools) * 1e9 / active_duration_ns if active_duration_ns else 0.0
        ),
        "trace_turns_per_second": (
            len(tools) * 1e9 / trace_replay_duration_ns
            if trace_replay_duration_ns
            else 0.0
        ),
        "operation_counts": dict(sorted(operations.items())),
        "attributable_dram_active_mean_bytes": time_weighted_mean(
            samples, "attributable_host_dram_bytes", active_phases
        ),
        "signed_host_available_delta_mean_bytes": time_weighted_mean(
            samples, "signed_host_available_delta_bytes", active_phases
        ),
        "host_attribution_caveat": "shared host; MemAvailable delta is not uniquely attributable DRAM",
        "signed_host_physical_delta_mean_bytes": time_weighted_mean(samples, "signed_host_physical_delta_bytes", active_phases),
        "daemon_plus_sandbox_cgroup_mean_bytes": time_weighted_mean(samples, "daemon_plus_sandbox_cgroup_bytes", active_phases),
        "attributable_dram_active_p95_bytes": percentile(host_values, 0.95),
        "attributable_dram_active_peak_bytes": max(host_values, default=0),
        "sandbox_dram_active_mean_bytes": time_weighted_mean(
            samples, "sandbox_memory_current_bytes", active_phases
        ),
        "sandbox_dram_active_p95_bytes": percentile(sandbox_values, 0.95),
        "sandbox_swap_active_mean_bytes": time_weighted_mean(
            samples, "sandbox_memory_swap_bytes", active_phases
        ),
        "sandbox_swap_active_peak_bytes": max(swap_values, default=0),
        "sandbox_compressed_active_mean_bytes": time_weighted_mean(
            samples, "sandbox_memory_compressed_bytes", active_phases
        ),
        "sandbox_compressed_active_peak_bytes": max(compressed_values, default=0),
        "waiting_sandbox_dram_mean_bytes": time_weighted_mean(
            samples, "waiting_sandbox_memory_bytes", {"trace_replay"}
        ),
        "active_duration_ns": active_duration_ns,
        "trace_replay_duration_ns": trace_replay_duration_ns,
        "request_ready_p50_ns": percentile(ready_values, 0.50),
        "request_ready_mean_ns": sum(ready_values) / len(ready_values) if ready_values else 0,
        "request_ready_p95_ns": percentile(ready_values, 0.95),
        "request_ready_p99_ns": percentile(ready_values, 0.99),
        "turn_p50_ns": percentile([tool["turn_ns"] for tool in tools], 0.50),
        "turn_mean_ns": sum(tool["turn_ns"] for tool in tools) / len(tools) if tools else 0,
        "turn_p95_ns": percentile([tool["turn_ns"] for tool in tools], 0.95),
        "turn_p99_ns": percentile([tool["turn_ns"] for tool in tools], 0.99),
        "command_p50_ns": percentile([tool["command_ns"] for tool in tools], 0.50),
        "command_p95_ns": percentile([tool["command_ns"] for tool in tools], 0.95),
        "wake_restore_p95_ns": percentile(
            [tool["wake_restore_ns"] for tool in tools], 0.95
        ),
        "reclaimed_bytes": sum(event.bytes for event in reclaim_events),
        "reclaim_events": len(reclaim_events),
        "reclaim_errors": len(reclaim_errors),
        "speculative_restore_events": sum(
            event.action == "spec_restore" for event in decision_events
        ),
        "speculative_restore_errors": sum(
            event.action == "spec_restore_error" for event in decision_events
        ),
        "speculative_prepared_bytes": sum(
            event.bytes for event in decision_events if event.action == "spec_restore"
        ),
        "speculative_restore_skips": sum(
            event.action == "spec_restore_skip" for event in decision_events
        ),
        "speculative_hold_p50_ns": speculation["hold_p50_ns"],
        "speculative_hold_p95_ns": speculation["hold_p95_ns"],
        "speculative_prepared_byte_seconds": speculation["prepared_byte_seconds"],
        "speculative_unconsumed_events": speculation["unconsumed_events"],
        "slo_breaker_opens": sum(
            event.action == "slo_breaker_open" for event in decision_events
        ),
        "major_faults_active_delta": max(faults, default=0) - min(faults, default=0),
        "major_faults_completed_sandboxes_total": sum(r.get("final_major_faults", 0) for r in replays),
        "pool_hits": sum(event.action == "hit" for event in pool_events),
        "pool_misses": sum(event.action == "miss" for event in pool_events),
        "errors": errors,
    }
    request_records = [
        {key: value for key, value in request.items() if key != "workload"}
        for request in requests
    ]
    return {
        "schema": "caden-trajectory-replay-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "config": vars(args)
        | {
            "workloads_dir": str(args.workloads_dir),
            "output": str(args.output),
            "base_socket_map": str(args.base_socket_map) if args.base_socket_map else None,
            "source_provenance": str(args.source_provenance) if args.source_provenance else None,
            "service_cache_roots": str(getattr(args, "service_cache_roots", None))
                if getattr(args, "service_cache_roots", None) else None,
            "speculative_prefetch_file": [
                str(path) for path in args.speculative_prefetch_file
            ],
            "speculative_prefetch_root": [
                str(path) for path in args.speculative_prefetch_root
            ],
        },
        "workload": {
            "manifest_sha256": sha256_file(manifest_path),
            "source": manifest["source"],
            "conversion": manifest["conversion"],
            "workload_count": len(requests),
            "orchestration": manifest.get("orchestration"),
        },
        "host": host_manifest(execution),
        "memory_capabilities": execution.memory_capabilities(),
        "memory_tier": {
            "backend": execution.memory_tier.name,
            "capabilities": execution.memory_tier.capabilities().as_json(),
            "store": tier_store_stats(execution),
        },
        "commits": json.loads(args.source_provenance.read_text()) if args.source_provenance else {
            "caden": git_commit(REPOSITORY_ROOT),
            "sandboxfs": git_commit(REPOSITORY_ROOT / "third_party" / "sandboxfs"),
        },
        "requests": request_records,
        "replays": replays,
        "samples": samples,
        "scheduler_events": [event_json(event) for event in decision_events],
        "prediction_state": scheduler.estimator_snapshot(),
        "pool_events": [event_json(event) for event in pool.events()] if pool else [],
        "summary": summary,
    }


def speculation_accounting(events: list[object]) -> dict[str, float | int]:
    pending: dict[str, tuple[int, int]] = {}
    holds: list[int] = []
    byte_nanoseconds = 0
    for event in events:
        sandbox = getattr(event, "sandbox_id", None)
        if not sandbox:
            continue
        action = getattr(event, "action", "")
        if action == "spec_restore":
            pending[sandbox] = (
                int(getattr(event, "timestamp_ns")),
                int(getattr(event, "bytes", 0)),
            )
            continue
        if (
            action != "stage"
            or getattr(event, "stage", None) is not Stage.RESPONSE_WAKE
        ):
            continue
        prepared = pending.pop(sandbox, None)
        if prepared is None:
            continue
        prepared_at, prepared_bytes = prepared
        hold = max(0, int(getattr(event, "timestamp_ns")) - prepared_at)
        holds.append(hold)
        byte_nanoseconds += prepared_bytes * hold
    return {
        "hold_p50_ns": percentile(holds, 0.50),
        "hold_p95_ns": percentile(holds, 0.95),
        "prepared_byte_seconds": byte_nanoseconds / 1e9,
        "unconsumed_events": len(pending),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def profile_hot_reserve(value: str) -> tuple[str, int]:
    profile, separator, mib_text = value.partition("=")
    if not separator or not profile:
        raise argparse.ArgumentTypeError("must be PROFILE=MIB")
    try:
        mib = int(mib_text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("MIB must be an integer") from error
    if mib < 0:
        raise argparse.ArgumentTypeError("MIB must not be negative")
    return profile, mib << 20


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


def probability(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be in [0, 1]")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
