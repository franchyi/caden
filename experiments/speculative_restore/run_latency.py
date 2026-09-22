#!/usr/bin/env python3
"""Measure reactive versus non-dispatching speculative sandbox restore latency."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import platform
import shlex
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from caden.sandboxfs_backend import (
    SandboxFSConfig,
    SandboxFSExecution,
)
from caden.types import (
    AgentTask,
    DemotionRequest,
    ReclaimMode,
    RestoreRequest,
    Stage,
    Tier,
)

HOLDER_CODE = """
import random
import signal

size = {size}
pattern = {pattern!r}
scan_stride = {scan_stride}
data = bytearray(size)
random_bytes = size if pattern == 'random' else (size // 4 if pattern == 'mixed' else 0)
generator = random.Random(20260830)
chunk_size = 1 << 20
for offset in range(0, random_bytes, chunk_size):
    end = min(random_bytes, offset + chunk_size)
    data[offset:end] = generator.randbytes(end - offset)
for offset in range(0, size, 4096):
    data[offset] ^= (offset // 4096) & 255

scan_count = 0
reset_count = 0

def scan(_signal, _frame):
    global scan_count
    checksum = 0
    for offset in range(0, len(data), scan_stride):
        checksum += data[offset]
    scan_count += 1
    with open('/tmp/crate-restore-scan', 'w') as handle:
        handle.write(f'{{scan_count}} {{checksum}}\\n')

def reset(_signal, _frame):
    global reset_count
    checksum = 0
    for offset in range(0, len(data), 4096):
        checksum += data[offset]
    reset_count += 1
    with open('/tmp/crate-restore-reset', 'w') as handle:
        handle.write(f'{{reset_count}} {{checksum}}\\n')

signal.signal(signal.SIGUSR1, scan)
signal.signal(signal.SIGUSR2, reset)
with open('/tmp/crate-restore-ready', 'w') as handle:
    handle.write('ready\\n')
while True:
    signal.pause()
""".strip()

HOLDER_FIFO_CODE = """
import os
import random

size = {size}
pattern = {pattern!r}
scan_stride = {scan_stride}
data = bytearray(size)
random_bytes = size if pattern == 'random' else (size // 4 if pattern == 'mixed' else 0)
generator = random.Random(20260830)
chunk_size = 1 << 20
for offset in range(0, random_bytes, chunk_size):
    end = min(random_bytes, offset + chunk_size)
    data[offset:end] = generator.randbytes(end - offset)
for offset in range(0, size, 4096):
    data[offset] ^= (offset // 4096) & 255

trigger = '/tmp/crate-restore-trigger'
result = '/tmp/crate-restore-result'
for path in (trigger, result):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    os.mkfifo(path, 0o600)
with open('/tmp/crate-restore-ready', 'w') as handle:
    handle.write('ready\\n')

scan_count = 0
while True:
    with open(trigger) as handle:
        command = handle.read().strip()
    if command not in ('scan', 'reset'):
        continue
    stride = 4096 if command == 'reset' else scan_stride
    checksum = 0
    for offset in range(0, len(data), stride):
        checksum += data[offset]
    scan_count += 1
    with open(result, 'w') as handle:
        handle.write(f'{{scan_count}} {{checksum}}\\n')
""".strip()

SCAN_SHELL = """
before=$(awk '{print $1}' /tmp/crate-restore-scan 2>/dev/null || echo 0)
target=$((before + 1))
kill -USR1 "$(cat /tmp/crate-restore-holder.pid)"
for i in $(seq 1 3000); do
  current=$(awk '{print $1}' /tmp/crate-restore-scan 2>/dev/null || echo 0)
  [ "$current" -ge "$target" ] && cat /tmp/crate-restore-scan && exit 0
  sleep 0.005
done
exit 1
""".strip()

SCAN_FIFO_SHELL = """
printf 'scan\\n' > /tmp/crate-restore-trigger
IFS=' ' read -r scan_count checksum < /tmp/crate-restore-result
printf '%s %s\\n' "$scan_count" "$checksum"
""".strip()

RESET_SHELL = """
before=$(awk '{print $1}' /tmp/crate-restore-reset 2>/dev/null || echo 0)
target=$((before + 1))
kill -USR2 "$(cat /tmp/crate-restore-holder.pid)"
for i in $(seq 1 3000); do
  current=$(awk '{print $1}' /tmp/crate-restore-reset 2>/dev/null || echo 0)
  [ "$current" -ge "$target" ] && cat /tmp/crate-restore-reset && exit 0
  sleep 0.005
done
exit 1
""".strip()

RESET_FIFO_SHELL = """
printf 'reset\\n' > /tmp/crate-restore-trigger
IFS=' ' read -r scan_count checksum < /tmp/crate-restore-result
printf '%s %s\\n' "$scan_count" "$checksum"
""".strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--mode", choices=("t1", "baseline"), default="t1")
    parser.add_argument("--wss-mib", type=positive_int, default=256)
    parser.add_argument(
        "--pattern", choices=("zeros", "mixed", "random"), default="mixed"
    )
    parser.add_argument("--scan-stride-kib", type=positive_int, default=4)
    parser.add_argument("--scan-transport", choices=("poll", "fifo"), default="poll")
    parser.add_argument("--hot-reserve-mib", type=nonnegative_int, default=16)
    parser.add_argument(
        "--reclaim-mode",
        choices=("balanced", "anon", "file"),
        default="balanced",
    )
    parser.add_argument("--madvise-mib", type=positive_int, default=320)
    parser.add_argument("--madvise-passes", type=positive_int, default=1)
    parser.add_argument(
        "--madvise-advice",
        choices=("willneed", "populate-read"),
        default="willneed",
    )
    parser.add_argument("--prefetch-root", type=Path, action="append", default=[])
    parser.add_argument("--prefetch-file", type=Path, action="append", default=[])
    parser.add_argument("--prefetch-mib", type=positive_int, default=256)
    parser.add_argument("--skip-confirmed-prewarm", action="store_true")
    parser.add_argument("--wait-ms", type=positive_float, default=1000.0)
    parser.add_argument("--spec-lead-ms", type=positive_float, default=300.0)
    parser.add_argument("--trials-per-treatment", type=positive_int, default=12)
    parser.add_argument("--hot-scans", type=positive_int, default=3)
    parser.add_argument(
        "--reset-between-trials",
        action="store_true",
        help="fully touch the holder before each trial to equalize sparse scans",
    )
    parser.add_argument("--reclaim-settle-ms", type=nonnegative_float, default=50.0)
    parser.add_argument("--madvise-settle-ms", type=nonnegative_float, default=0.0)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run_latency.py must run as root")
    if args.spec_lead_ms >= args.wait_ms:
        raise SystemExit("--spec-lead-ms must be smaller than --wait-ms")
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")

    execution = SandboxFSExecution(
        SandboxFSConfig(
            socket_path=args.socket,
            mode=args.mode,
            reclaim_settle_seconds=args.reclaim_settle_ms / 1000,
            prewarm_argv=() if args.skip_confirmed_prewarm else ("/bin/true",),
            speculative_prefetch_profiles={"default": tuple(args.prefetch_file)},
            speculative_prefetch_roots=tuple(args.prefetch_root),
            speculative_prefetch_max_bytes=args.prefetch_mib << 20,
            allow_process_madvise_restore=True,
            speculative_madvise_profiles=("default",),
            speculative_madvise_max_bytes=args.madvise_mib << 20,
            speculative_madvise_settle_seconds=args.madvise_settle_ms / 1000,
            speculative_madvise_passes=args.madvise_passes,
            speculative_madvise_advice=args.madvise_advice,
        ),
        id_factory=lambda: f"crate-restore-{os.getpid()}",
    )
    sandbox = execution.run(AgentTask([], args.base), lambda *_: None)
    errors: list[str] = []
    trials: list[dict[str, Any]] = []
    hot_scans: list[int] = []
    checksums: list[int] = []
    started_at = datetime.now(UTC).isoformat()
    try:
        launch_holder(
            execution,
            sandbox,
            args.wss_mib << 20,
            args.pattern,
            args.scan_stride_kib << 10,
            args.scan_transport,
        )
        charged = execution.stat(sandbox).mem_dram_bytes
        if charged < int((args.wss_mib << 20) * 0.85):
            raise RuntimeError(f"holder charge {charged} is below 85% of requested WSS")
        for _ in range(args.hot_scans):
            scan_ns, checksum = scan_holder(execution, sandbox, args.scan_transport)
            hot_scans.append(scan_ns)
            checksums.append(checksum)

        for sequence, treatment in enumerate(
            counterbalanced_order(args.trials_per_treatment)
        ):
            reset_ns = 0
            if args.reset_between_trials:
                reset_ns, _reset_checksum = reset_holder(
                    execution, sandbox, args.scan_transport
                )
            trial = run_trial(execution, sandbox, args, sequence, treatment)
            trial["reset_ns"] = reset_ns
            trials.append(trial)
            checksums.append(trial["checksum"])
            print(
                json.dumps(
                    {
                        "sequence": sequence,
                        "treatment": treatment,
                        "demote_ms": trial["demote_ns"] / 1e6,
                        "prepare_ms": trial["spec_prepare_ns"] / 1e6,
                        "wake_to_scan_ms": trial["wake_to_scan_ns"] / 1e6,
                        "swap_mib": trial["swap_after_demote_bytes"] / (1 << 20),
                        "advised_mib": trial["advised_bytes"] / (1 << 20),
                        "prefetched_mib": trial["prefetched_bytes"] / (1 << 20),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        if len(set(checksums)) != 1:
            raise RuntimeError(f"holder checksum changed: {sorted(set(checksums))}")
    except BaseException as error:
        errors.append(f"{type(error).__name__}: {error}")
        raise
    finally:
        execution.revoke(sandbox)
        report = build_report(args, execution, started_at, hot_scans, trials, errors)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report["summary"], indent=2), flush=True)
        print(f"report written to {args.output}", flush=True)
    return 0


def launch_holder(
    execution: SandboxFSExecution,
    sandbox: str,
    size: int,
    pattern: str,
    scan_stride: int,
    scan_transport: str,
) -> None:
    template = HOLDER_FIFO_CODE if scan_transport == "fifo" else HOLDER_CODE
    code = template.format(
        size=size,
        pattern=pattern,
        scan_stride=scan_stride,
    )
    shell = (
        "rm -f /tmp/crate-restore-ready /tmp/crate-restore-scan "
        "/tmp/crate-restore-reset "
        "/tmp/crate-restore-trigger /tmp/crate-restore-result; "
        f"nohup python3 -c {shlex.quote(code)} </dev/null "
        ">/tmp/crate-restore-holder.log 2>&1 & "
        "echo $! >/tmp/crate-restore-holder.pid"
    )
    require_success(execution.exec(sandbox, ("sh", "-lc", shell)), "launch holder")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        response = execution.exec(
            sandbox, ("sh", "-lc", "test -s /tmp/crate-restore-ready")
        )
        if response.get("exit_code") == 0:
            return
        time.sleep(0.05)
    raise RuntimeError("memory holder did not become ready")


def scan_holder(
    execution: SandboxFSExecution, sandbox: str, scan_transport: str
) -> tuple[int, int]:
    command = SCAN_FIFO_SHELL if scan_transport == "fifo" else SCAN_SHELL
    started = time.monotonic_ns()
    response = execution.exec(sandbox, ("sh", "-lc", command))
    require_success(response, "scan holder")
    elapsed = time.monotonic_ns() - started
    fields = str(response.get("stdout", "")).strip().split()
    if len(fields) != 2:
        raise RuntimeError(f"invalid holder scan receipt: {response!r}")
    return elapsed, int(fields[1])


def reset_holder(
    execution: SandboxFSExecution, sandbox: str, scan_transport: str
) -> tuple[int, int]:
    command = RESET_FIFO_SHELL if scan_transport == "fifo" else RESET_SHELL
    started = time.monotonic_ns()
    response = execution.exec(sandbox, ("sh", "-lc", command))
    require_success(response, "reset holder")
    elapsed = time.monotonic_ns() - started
    fields = str(response.get("stdout", "")).strip().split()
    if len(fields) != 2:
        raise RuntimeError(f"invalid holder reset receipt: {response!r}")
    return elapsed, int(fields[1])


def run_trial(
    execution: SandboxFSExecution,
    sandbox: str,
    args: argparse.Namespace,
    sequence: int,
    treatment: str,
) -> dict[str, Any]:
    wait_started = time.monotonic_ns()
    wait_boundary = wait_started + round(args.wait_ms * 1e6)
    execution.report(sandbox, Stage.LLM_WAIT)
    before = execution.stat(sandbox)
    demote_started = time.monotonic_ns()
    demotion = execution.demote_selective(
        sandbox,
        DemotionRequest(
            tier=Tier.SSD,
            target_bytes=max(
                0,
                before.mem_dram_bytes - (args.hot_reserve_mib << 20),
            ),
            min_resident_bytes=args.hot_reserve_mib << 20,
            reclaim_mode={
                "balanced": ReclaimMode.BALANCED,
                "anon": ReclaimMode.ANON_ONLY,
                "file": ReclaimMode.FILE_ONLY,
            }[args.reclaim_mode],
        ),
    )
    demote_ns = time.monotonic_ns() - demote_started
    after_demote = execution.stat(sandbox)

    speculative = None
    after_prepare = None
    spec_prepare_ns = 0
    if treatment == "speculative":
        sleep_until(wait_boundary - round(args.spec_lead_ms * 1e6))
        prepare_started = time.monotonic_ns()
        speculative = execution.restore_selective(
            sandbox,
            RestoreRequest(profile="default", speculative=True),
        )
        spec_prepare_ns = time.monotonic_ns() - prepare_started
        after_prepare = execution.stat(sandbox)
    sleep_until(wait_boundary)
    response_arrival = time.monotonic_ns()
    boundary_overrun_ns = max(0, response_arrival - wait_boundary)

    execution.report(sandbox, Stage.RESPONSE_WAKE)
    confirmed_started = time.monotonic_ns()
    confirmed = execution.restore_selective(
        sandbox,
        RestoreRequest(profile="default", speculative=False),
    )
    confirmed_restore_ns = time.monotonic_ns() - confirmed_started
    execution.report(sandbox, Stage.TOOL_BURST)
    scan_ns, checksum = scan_holder(execution, sandbox, args.scan_transport)
    command_completed = time.monotonic_ns()
    execution.report(sandbox, Stage.RESULT_PACK)
    after_scan = execution.stat(sandbox)

    return {
        "sequence": sequence,
        "treatment": treatment,
        "wait_target_ns": round(args.wait_ms * 1e6),
        "boundary_overrun_ns": boundary_overrun_ns,
        "demote_ns": demote_ns,
        "demotion": dataclasses.asdict(demotion)
        | {
            "tier": demotion.tier.value,
            "reclaim_mode": demotion.reclaim_mode.value,
        },
        "dram_before_bytes": before.mem_dram_bytes,
        "dram_after_demote_bytes": after_demote.mem_dram_bytes,
        "swap_after_demote_bytes": after_demote.mem_swap_bytes,
        "dram_after_prepare_bytes": (
            after_prepare.mem_dram_bytes if after_prepare else 0
        ),
        "swap_after_prepare_bytes": (
            after_prepare.mem_swap_bytes if after_prepare else 0
        ),
        "major_faults_after_prepare": (
            after_prepare.major_faults if after_prepare else 0
        ),
        "page_faults_before": before.page_faults,
        "page_faults_after_demote": after_demote.page_faults,
        "page_faults_after_prepare": after_prepare.page_faults if after_prepare else 0,
        "major_faults_before": before.major_faults,
        "major_faults_after_demote": after_demote.major_faults,
        "spec_prepare_ns": spec_prepare_ns,
        "speculative": dataclasses.asdict(speculative) if speculative else None,
        "advised_bytes": speculative.advised_bytes if speculative else 0,
        "prefetched_bytes": speculative.prefetched_bytes if speculative else 0,
        "confirmed_restore_ns": confirmed_restore_ns,
        "confirmed": dataclasses.asdict(confirmed),
        "scan_ns": scan_ns,
        "wake_to_scan_ns": command_completed - response_arrival,
        "response_path_overhead_ns": max(
            0,
            command_completed - response_arrival - confirmed_restore_ns - scan_ns,
        ),
        # This direct boundary-to-completion clock includes lifecycle-report
        # and dispatch setup, as well as any preparation boundary overrun.
        "response_to_scan_ns": command_completed - wait_boundary,
        "dram_after_scan_bytes": after_scan.mem_dram_bytes,
        "swap_after_scan_bytes": after_scan.mem_swap_bytes,
        "major_faults_after_scan": after_scan.major_faults,
        "major_faults_wake_delta": max(
            0,
            after_scan.major_faults
            - (
                after_prepare.major_faults
                if after_prepare
                else after_demote.major_faults
            ),
        ),
        "page_faults_wake_delta": max(
            0,
            after_scan.page_faults
            - (
                after_prepare.page_faults if after_prepare else after_demote.page_faults
            ),
        ),
        "checksum": checksum,
    }


def build_report(
    args: argparse.Namespace,
    execution: SandboxFSExecution,
    started_at: str,
    hot_scans: list[int],
    trials: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    summaries: dict[str, dict[str, Any]] = {}
    for treatment in ("reactive", "speculative"):
        selected = [trial for trial in trials if trial["treatment"] == treatment]
        summaries[treatment] = {
            "trials": len(selected),
            "reset_p50_ns": percentile(
                [trial.get("reset_ns", 0) for trial in selected], 0.50
            ),
            "reset_p95_ns": percentile(
                [trial.get("reset_ns", 0) for trial in selected], 0.95
            ),
            "demote_p50_ns": percentile(
                [trial["demote_ns"] for trial in selected], 0.50
            ),
            "demote_p95_ns": percentile(
                [trial["demote_ns"] for trial in selected], 0.95
            ),
            "prepare_p50_ns": percentile(
                [trial["spec_prepare_ns"] for trial in selected], 0.50
            ),
            "prepare_p95_ns": percentile(
                [trial["spec_prepare_ns"] for trial in selected], 0.95
            ),
            "confirmed_restore_p50_ns": percentile(
                [trial["confirmed_restore_ns"] for trial in selected], 0.50
            ),
            "confirmed_restore_p95_ns": percentile(
                [trial["confirmed_restore_ns"] for trial in selected], 0.95
            ),
            "scan_p50_ns": percentile([trial["scan_ns"] for trial in selected], 0.50),
            "scan_p95_ns": percentile([trial["scan_ns"] for trial in selected], 0.95),
            "wake_to_scan_p50_ns": percentile(
                [trial["wake_to_scan_ns"] for trial in selected], 0.50
            ),
            "wake_to_scan_p95_ns": percentile(
                [trial["wake_to_scan_ns"] for trial in selected], 0.95
            ),
            "response_to_scan_p50_ns": percentile(
                [trial["response_to_scan_ns"] for trial in selected], 0.50
            ),
            "response_to_scan_p95_ns": percentile(
                [trial["response_to_scan_ns"] for trial in selected], 0.95
            ),
            "boundary_overrun_p95_ns": percentile(
                [trial["boundary_overrun_ns"] for trial in selected], 0.95
            ),
            "response_path_overhead_p50_ns": percentile(
                [trial["response_path_overhead_ns"] for trial in selected], 0.50
            ),
            "response_path_overhead_p95_ns": percentile(
                [trial["response_path_overhead_ns"] for trial in selected], 0.95
            ),
            "reclaimed_p50_bytes": percentile(
                [trial["demotion"]["reclaimed_bytes"] for trial in selected],
                0.50,
            ),
            "swap_after_demote_p50_bytes": percentile(
                [trial["swap_after_demote_bytes"] for trial in selected], 0.50
            ),
            "advised_p50_bytes": percentile(
                [trial["advised_bytes"] for trial in selected], 0.50
            ),
            "prefetched_p50_bytes": percentile(
                [trial["prefetched_bytes"] for trial in selected], 0.50
            ),
            "swap_after_prepare_p50_bytes": percentile(
                [trial["swap_after_prepare_bytes"] for trial in selected], 0.50
            ),
            "dram_after_prepare_p50_bytes": percentile(
                [trial["dram_after_prepare_bytes"] for trial in selected], 0.50
            ),
            "wake_major_faults_p50": percentile(
                [trial["major_faults_wake_delta"] for trial in selected], 0.50
            ),
            "wake_major_faults_p95": percentile(
                [trial["major_faults_wake_delta"] for trial in selected], 0.95
            ),
            "wake_page_faults_p50": percentile(
                [trial["page_faults_wake_delta"] for trial in selected], 0.50
            ),
            "wake_page_faults_p95": percentile(
                [trial["page_faults_wake_delta"] for trial in selected], 0.95
            ),
        }
    reactive = summaries["reactive"]
    speculative = summaries["speculative"]
    wake_speedup_p50 = ratio(
        reactive["wake_to_scan_p50_ns"], speculative["wake_to_scan_p50_ns"]
    )
    wake_speedup_p95 = ratio(
        reactive["wake_to_scan_p95_ns"], speculative["wake_to_scan_p95_ns"]
    )
    response_speedup_p50 = ratio(
        reactive["response_to_scan_p50_ns"],
        speculative["response_to_scan_p50_ns"],
    )
    response_speedup_p95 = ratio(
        reactive["response_to_scan_p95_ns"],
        speculative["response_to_scan_p95_ns"],
    )
    return {
        "schema": "caden-speculative-restore-latency-v6",
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "config": serializable_config(args),
        "host": {
            "hostname": platform.node(),
            "kernel": platform.release(),
            "python": platform.python_version(),
            "cpu_affinity": sorted(os.sched_getaffinity(0)),
            "memory_capabilities": execution.memory_capabilities(),
            "swap": Path("/proc/swaps").read_text(),
        },
        "source": {
            "caden_head": command_output(
                ("git", "-C", str(REPOSITORY_ROOT), "rev-parse", "HEAD")
            ),
            "caden_diff_sha256": working_diff_sha256(),
        },
        "hot_scan_ns": hot_scans,
        "trials": trials,
        "summary": {
            "success": not errors
            and all(
                summaries[name]["trials"] == args.trials_per_treatment
                for name in summaries
            ),
            "errors": errors,
            "hot_scan_p50_ns": percentile(hot_scans, 0.50),
            "hot_scan_p95_ns": percentile(hot_scans, 0.95),
            "treatments": summaries,
            "wake_to_scan_speedup_p50": wake_speedup_p50,
            "wake_to_scan_speedup_p95": wake_speedup_p95,
            "response_to_scan_speedup_p50": response_speedup_p50,
            "response_to_scan_speedup_p95": response_speedup_p95,
            "response_to_scan_reduction_p50_fraction": (
                1 - 1 / response_speedup_p50 if response_speedup_p50 else 0.0
            ),
            "response_to_scan_reduction_p95_fraction": (
                1 - 1 / response_speedup_p95 if response_speedup_p95 else 0.0
            ),
            "claim_scope": (
                "shared-host synthetic persistent-anonymous-memory mechanism; "
                "no LLM, no KV cache, no global cache drop"
            ),
        },
    }


def serializable_config(args: argparse.Namespace) -> dict[str, object]:
    config = vars(args).copy()
    config["output"] = str(args.output)
    config["prefetch_root"] = [str(path) for path in args.prefetch_root]
    config["prefetch_file"] = [str(path) for path in args.prefetch_file]
    return config


def counterbalanced_order(trials_per_treatment: int) -> list[str]:
    counts: Counter[str] = Counter()
    order: list[str] = []
    blocks = (
        ("reactive", "speculative", "speculative", "reactive"),
        ("speculative", "reactive", "reactive", "speculative"),
    )
    block = 0
    while any(counts[name] < trials_per_treatment for name in blocks[0][:2]):
        for treatment in blocks[block % len(blocks)]:
            if counts[treatment] >= trials_per_treatment:
                continue
            order.append(treatment)
            counts[treatment] += 1
        block += 1
    return order


def sleep_until(deadline_ns: int) -> None:
    while True:
        remaining = (deadline_ns - time.monotonic_ns()) / 1e9
        if remaining <= 0:
            return
        time.sleep(remaining)


def require_success(response: dict[str, object], operation: str) -> None:
    if response.get("exit_code") != 0:
        raise RuntimeError(f"{operation} failed: {response!r}")


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5)))
    return ordered[index]


def ratio(original: int, optimized: int) -> float:
    return original / optimized if optimized > 0 else 0.0


def command_output(argv: tuple[str, ...]) -> str:
    import subprocess

    completed = subprocess.run(argv, check=False, capture_output=True, text=True)
    return (
        completed.stdout.strip()
        if completed.returncode == 0
        else completed.stderr.strip()
    )


def working_diff_sha256() -> str:
    import subprocess

    diff = subprocess.run(
        ("git", "-C", str(REPOSITORY_ROOT), "diff", "--binary", "HEAD"),
        check=True,
        capture_output=True,
    ).stdout
    untracked = subprocess.run(
        (
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ),
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    digest = hashlib.sha256(diff)
    for raw_path in sorted(path for path in untracked if path):
        path = REPOSITORY_ROOT / os.fsdecode(raw_path)
        digest.update(b"\0UNTRACKED\0")
        digest.update(raw_path)
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


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
