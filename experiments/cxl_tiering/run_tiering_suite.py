#!/usr/bin/env python3
"""Isolated 32-task tiering comparison on nsl17: Baseline, Crate DRAM-SSD, Crate DRAM-CXL.

Runs as root on nsl17 inside one private run directory. The September 19
campaign tree is a read-only input (prepared bases, root filesystems, binaries,
recorded traces); nothing is written there and none of its units is touched.

What this runner will NOT do: swapon/swapoff or swap-priority changes, DAX
conversion or memory onlining, global cache purges, package installation, or
stopping any unit it did not start. It pauses a configuration instead of
exceeding the declared host limits.

One ordered run per configuration is descriptive. It is not a density-at-SLO
result, a replication, or a causal SSD-versus-CXL hardware comparison: the two
Crate backends cover different page populations (see REPORT.md).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PARENT = Path("/data/chaoyi/crate-tiering")
DAX = "/dev/dax0.0"
GUARD = 2 << 20
UPPER_HALF = (256 << 30, 512 << 30)

POLICY = ["--mode", "t1", "--policy", "request-aware", "--pool-target", "1", "--pool-max", "1",
          "--max-admissions", "4", "--max-wakes", "8", "--speculative-restore",
          "--hot-reserve-mib", "32", "--minimum-cold-ms", "100",
          "--max-early-wake-probability", "0.20", "--confirmed-movement-reserve", "1",
          "--wake-reserve-safety-mib", "256", "--restore-mode", "thaw-only", "--queue-aware-pool"]
CONFIGS = {
    # The eager-copy Baseline is the only overall baseline.
    "baseline": ["--mode", "baseline", "--policy", "static", "--constant-cpu",
                 "--max-admissions", "8", "--max-wakes", "8"],
    # Same Crate policy; only the injected memory-tier backend differs.
    "crate-ssd": POLICY + ["--memory-tier-backend", "ssd", "--reclaim-tier", "ssd",
                           "--allow-process-madvise-restore", "--speculative-madvise-mib", "256"],
    "crate-cxl": POLICY + ["--memory-tier-backend", "cxl", "--reclaim-tier", "cxl"],
}
CONFIGS["crate-ssd-cache"] = list(CONFIGS["crate-ssd"])
CONFIGS["crate-ssd-cache-hot16"] = list(CONFIGS["crate-ssd"]) + ["--hot-reserve-mib", "16"]
CONFIGS["crate-cxl-cache"] = list(CONFIGS["crate-cxl"])
CXL_CONFIGS = frozenset({"crate-cxl", "crate-cxl-cache"})


def replay_extras(label: str, *, cache_roots: Path, cache_budget_mib: int,
                  session_serving: bool, pager_socket: str) -> list[str]:
    """Compose orthogonal options; a pager must not erase arrival/cache flags."""
    extra: list[str] = []
    if "cache" in label:
        extra += ["--service-cache-roots", str(cache_roots),
                  "--service-cache-budget-mib", str(cache_budget_mib)]
    if session_serving:
        extra += ["--arrival-driven"]
    if label in CXL_CONFIGS:
        extra += ["--cxl-pager-socket", pager_socket]
    return extra


def sweep_plan(concurrencies: list[int], labels: list[str], limit: int) -> list[tuple[str, str, int]]:
    """Fixed work, alternating system order, no change to the policy knobs."""
    if (not concurrencies or len(set(concurrencies)) != len(concurrencies)
            or any(n < 1 or n > limit or limit % n for n in concurrencies)):
        raise ValueError("unique positive concurrency values must divide the task count")
    if labels != ["baseline", "crate-ssd"]:
        raise ValueError("concurrency campaign is the explicit Vanilla/Crate-SSD comparison")
    return [(f"n{n:02d}-{label}", label, n) for i, n in enumerate(concurrencies)
            for label in (labels if i % 2 == 0 else list(reversed(labels)))]


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def output(argv: list[str], **options: object) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True, **options).stdout  # type: ignore[call-overload]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def host_state() -> dict[str, object]:
    meminfo = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in {"MemTotal", "MemFree", "MemAvailable", "Cached", "SwapTotal", "SwapFree"}:
            meminfo[key] = int(rest.split()[0]) * 1024
    holders = subprocess.run(["fuser", "-v", DAX], capture_output=True, text=True)
    return {
        "observed_unix": time.time(),
        "hostname": os.uname().nodename,
        "kernel": os.uname().release,
        "loadavg": Path("/proc/loadavg").read_text().split()[:3],
        "meminfo_bytes": meminfo,
        "swaps": Path("/proc/swaps").read_text().splitlines(),
        "pressure_memory": Path("/proc/pressure/memory").read_text().splitlines(),
        "pressure_io": Path("/proc/pressure/io").read_text().splitlines(),
        "dax_holders": (holders.stdout + holders.stderr).strip(),
        "dax_target_node_online_as_ram": False,
        "zswap_enabled": Path("/sys/module/zswap/parameters/enabled").read_text().strip(),
        "crate_units": output(["systemctl", "list-units", "--all", "--no-legend", "--plain",
                               "crate-*"]).splitlines(),
        "users": output(["who"]).splitlines(),
    }


def swap_configuration() -> list[tuple[str, str, str, str]]:
    rows = []
    for line in Path("/proc/swaps").read_text().splitlines()[1:]:
        fields = line.split()
        rows.append((fields[0], fields[1], fields[2], fields[4]))  # usage churn ignored
    return rows


def dax_guards(offset: int, capacity: int) -> dict[str, str]:
    """Read-only SHA256 of the 2 MiB immediately before and after the range."""
    hashes = {}
    descriptor = os.open(DAX, os.O_RDONLY)
    try:
        for name, start in (("before", offset - GUARD), ("after", offset + capacity)):
            with mmap.mmap(descriptor, GUARD, mmap.MAP_SHARED, mmap.PROT_READ, offset=start) as view:
                hashes[name] = hashlib.sha256(view).hexdigest()
    finally:
        os.close(descriptor)
    return hashes


def start_pager(a: argparse.Namespace, run_root: Path, log: Path, unit: str, socket_path: str) -> dict[str, object]:
    """Start this run's store owner. Returns its argv and the guard hashes taken first."""
    pagerd = [str(ROOT / "native/cxl_coldstore/build/crate_pagerd"), "--socket", socket_path,
              "--offset", str(a.dax_offset if a.cxl_medium == "dax" else 0),
              "--capacity", str(a.dax_capacity), "--logical-bytes", str(a.dax_capacity - GUARD),
              "--codec", "none", "--lock-buffers", "--per-sandbox-max-bytes", str(1 << 30)]
    guards: dict[str, str] = {}
    if a.cxl_medium == "dax":
        guards = dax_guards(a.dax_offset, a.dax_capacity)
        pagerd += ["--reserved-dax", DAX]
    else:
        store = run_root / "cxl-emulation-store.bin"
        with store.open("wb") as handle:
            handle.truncate(a.dax_capacity)
        pagerd += ["--emulate-file", str(store)]
    subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)
    subprocess.run(["systemd-run", f"--unit={unit}", "--property=Type=exec", "--property=AllowedCPUs=0-7",
                    f"--property=StandardError=append:{log}", "--property=LimitMEMLOCK=infinity",
                    *pagerd], check=True)
    for _ in range(100):
        if Path(socket_path).is_socket():
            return {"pagerd_argv": pagerd, "guards_before": guards}
        time.sleep(0.1)
    raise RuntimeError("crate_pagerd did not start")


def stop_pager(a: argparse.Namespace, unit: str, guards_before: dict[str, str]) -> dict[str, object]:
    # SIGTERM is deferred by the daemon while consumers still hold cold pages.
    stop = subprocess.run(["systemctl", "stop", unit], capture_output=True, text=True)
    record: dict[str, object] = {"returncode": stop.returncode, "stderr": stop.stderr.strip(),
                                 "state": output(["systemctl", "show", unit, "-p", "ActiveState", "--value"]).strip()}
    if guards_before:
        after = dax_guards(a.dax_offset, a.dax_capacity)
        record["dax_guards"] = {"before": guards_before, "after": after, "unchanged": guards_before == after}
    return record


class HostMonitor(threading.Thread):
    """Pauses the current configuration rather than exceed declared host limits."""

    def __init__(self, log: Path, minimum_available: int, maximum_pressure: float) -> None:
        super().__init__(daemon=True)
        self.log, self.minimum_available, self.maximum_pressure = log, minimum_available, maximum_pressure
        self.process: subprocess.Popen[bytes] | None = None
        self.tripped = ""
        self.running = True

    def run(self) -> None:
        while self.running:
            available = pressure = 0.0
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
            match = re.search(r"some avg10=([\d.]+)", Path("/proc/pressure/memory").read_text())
            if match:
                pressure = float(match.group(1))
            record = {"observed_unix": time.time(), "mem_available_bytes": available,
                      "memory_pressure_some_avg10": pressure,
                      "loadavg_1m": float(Path("/proc/loadavg").read_text().split()[0]),
                      "swap_free_kib": next((int(line.split()[1]) for line in
                                             Path("/proc/meminfo").read_text().splitlines()
                                             if line.startswith("SwapFree:")), 0)}
            with self.log.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
            reason = ""
            if available < self.minimum_available:
                reason = f"MemAvailable {available} below declared floor {self.minimum_available}"
            elif pressure > self.maximum_pressure:
                reason = f"memory pressure avg10 {pressure} above declared ceiling {self.maximum_pressure}"
            if reason and self.process is not None and self.process.poll() is None and not self.tripped:
                self.tripped = reason
                self.process.send_signal(signal.SIGTERM)  # the runner cleans up its sandboxes
            time.sleep(5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", type=Path, required=True)
    ap.add_argument("--inputs", type=Path, default=Path("/sandboxfs/crate-swebench-20260919"))
    ap.add_argument("--workloads", type=Path, required=True, help="run-local copy of normalized-formal")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--configs", default="baseline,crate-ssd,crate-cxl")
    ap.add_argument("--limit", type=int, default=32)
    ap.add_argument("--active", type=int, default=8)
    ap.add_argument("--concurrencies", help="single descriptive sweep, e.g. 32,16,8,4,2,1; cold sweep first")
    ap.add_argument("--cxl-medium", choices=("dax", "file"), default="dax")
    ap.add_argument("--dax-offset", type=int, default=384 << 30)
    ap.add_argument("--dax-capacity", type=int, default=4 << 30)
    ap.add_argument("--reservation", type=Path, help="recorded cross-host reservation (required for dax)")
    ap.add_argument("--cold-repeats", type=int, default=1)
    ap.add_argument("--skip-cold", action="store_true")
    ap.add_argument("--skip-paging-check", action="store_true",
                    help="skip the normal-API integration gate that precedes the comparison")
    ap.add_argument("--minimum-available-gib", type=float, default=8.0)
    ap.add_argument("--maximum-memory-pressure", type=float, default=30.0)
    ap.add_argument("--private-bases", action="store_true",
                    help="prepare private immutable input inodes; never evict shared historical inputs")
    ap.add_argument("--private-base-source", type=Path,
                    help="read-only source for fresh private-inode preparation (digests still checked)")
    ap.add_argument("--session-serving", action="store_true")
    ap.add_argument("--service-cache-budget-mib", type=int, default=16)
    a = ap.parse_args()

    labels = a.configs.split(",")
    ns = [int(n) for n in a.concurrencies.split(",")] if a.concurrencies else []
    runs = sweep_plan(ns, labels, a.limit) if ns else [(label, label, a.active) for label in labels]
    if a.session_serving and (ns or not a.skip_cold):
        raise SystemExit("serving runs require explicit configs and skip the unrelated cold campaign")
    if a.service_cache_budget_mib < 16:
        raise SystemExit("cache byte budget must cover one 16 MiB chunk")
    if ns and (not a.skip_paging_check or a.skip_cold):
        raise SystemExit("SSD sweep requires --skip-paging-check and its independent cold sweep")
    if os.geteuid() != 0 or os.uname().nodename not in {"nsl17", "nsl-node17"}:
        raise SystemExit("must run as root on nsl17")
    if any(label not in CONFIGS for label in labels) or len(set(labels)) != len(labels):
        raise SystemExit("unknown or repeated configuration")
    if any("cache" in label for label in labels) and (not a.private_bases or ns):
        raise SystemExit("cache development runs require --private-bases and explicit configurations")
    if not re.fullmatch(r"[a-z0-9]{4,16}", a.tag):
        raise SystemExit("tag must be 4-16 lowercase alphanumerics")
    run_root = a.run_root
    if (run_root.is_symlink() or not run_root.is_dir() or run_root.parent != PARENT
            or PARENT.is_symlink() or ROOT != run_root / "source"):
        raise SystemExit("run root must be a real directory under /data/chaoyi/crate-tiering holding source/")
    results = run_root / "results"
    for name in ("raw", "preflight", "configs"):
        (results / name).mkdir(parents=True, exist_ok=True)
    for key, _, _ in runs:
        if (results / "raw" / key).exists():
            raise SystemExit(f"refusing existing raw output: {key}")
    if ns:
        from experiments.swebench_verified.run_suite import verify_source
        verify_source()
        if (results / "PLAN.json").exists():
            raise SystemExit("refusing an existing sweep plan")
        write_json(results / "PLAN.json", {
            "schema": "crate-swe-concurrency-v1", "created_unix": time.time(),
            "tasks": a.limit, "concurrencies": ns, "repetitions": 1,
            "runs": runs, "cold_first": True, "cold_order": sorted(ns),
            "wait_scale": 1, "sample_ms": 100, "relative_latency_target": 1.10,
            "latency_statistics": ["mean", "p95", "p99"],
            "operational_deadline_ms": 180000,
            "inference": "descriptive single runs; not density-at-SLO or noninferiority proof",
            "memory": "time-weighted service-tree cgroup charge; sandbox leaf and host deltas supplementary",
            "scope": "shared nsl17; closed-loop waves; warm host, prepared local bases; XFS workspace; existing swap unchanged",
            "safety": {"minimum_available_gib": a.minimum_available_gib,
                       "maximum_memory_pressure": a.maximum_memory_pressure},
            "source_provenance_sha256": sha256_file(ROOT / "SOURCE_PROVENANCE.json"),
            "workload_manifest_sha256": sha256_file(a.workloads / "manifest.json"),
            "policy_argv": {label: CONFIGS[label] for label in labels}})
    if (any(label in CXL_CONFIGS for label in labels) or not a.skip_paging_check) and a.cxl_medium == "dax":
        if a.reservation is None or not a.reservation.is_file():
            raise SystemExit("real DAX needs the recorded cross-host reservation")
        reservation = json.loads(a.reservation.read_text())
        end = a.dax_offset + a.dax_capacity
        if (reservation.get("offset") != a.dax_offset or reservation.get("capacity") != a.dax_capacity
                or not reservation.get("confirmed_by_user")
                or a.dax_offset - GUARD < UPPER_HALF[0] or end + GUARD > UPPER_HALF[1]
                or a.dax_offset % GUARD or a.dax_capacity % GUARD):
            raise SystemExit("DAX range does not match the confirmed upper-half reservation")

    before = host_state()
    write_json(results / "preflight" / "host-before.json", before)
    foreign = [line for line in before["crate_units"]  # type: ignore[union-attr]
               if " active " in f" {line} " and not line.startswith(("crate-sv-docker.service", f"crate-tier-{a.tag}-"))]
    if foreign:
        raise SystemExit(f"other active crate units overlap this measurement: {foreign}")
    available = before["meminfo_bytes"]["MemAvailable"]  # type: ignore[index]
    if available < 24 << 30 or float(before["loadavg"][0]) > 16:  # type: ignore[index]
        raise SystemExit("shared host lacks the declared headroom (24 GiB available, load < 16)")
    swaps_before = swap_configuration()

    selection = json.loads((a.inputs / "selection/manifest.json").read_text())
    tasks = selection["tasks"][: a.limit]
    if a.session_serving:
        from experiments.swebench_verified.run_suite import verify_source
        from experiments.trajectory_replay.run_campaign import load_workloads
        from experiments.swebench_verified.session_workloads import validate_arrivals
        verify_source()
        workloads, manifest = load_workloads(a.workloads, a.limit)
        validate_arrivals(workloads, manifest, a.workloads)
        bases_needed = {workload["base"] for workload in workloads}
        tasks = [task for task in selection["tasks"] if task["base"] in bases_needed]
        if {task["base"] for task in tasks} != bases_needed:
            raise RuntimeError("serving source bases missing from prepared input selection")
        originals = {task["base"]: task["instance_id"] for task in tasks}
        if any(originals[w["base"]] != w["source"]["instance_id"] for w in workloads):
            raise RuntimeError("replicated task does not match its prepared base")
        write_json(results / "PLAN.json", {"orchestration": manifest["orchestration"],
            "configurations": labels, "live_session_cap": a.active, "repetitions": 1,
            "cache_budget_mib_per_100ms": a.service_cache_budget_mib,
            "source_manifest_sha256": sha256_file(ROOT / "SOURCE_PROVENANCE.json"),
            "workload_manifest_sha256": sha256_file(a.workloads / "manifest.json"),
            "relative_latency_target": 1.10, "inference": "single-pair development evidence"})
    ctl = str(a.inputs / "bin/sandboxfsctl")
    units: list[str] = []
    status: dict[str, object] = {"run_root": str(run_root), "tag": a.tag, "stage": "daemons",
                                 "configurations": {key: {"state": "not-run"} for key, _, _ in runs}}
    write_json(results / "STATUS.json", status)
    monitor = HostMonitor(results / "preflight" / "host-monitor.jsonl",
                          int(a.minimum_available_gib * (1 << 30)), a.maximum_memory_pressure)
    monitor.start()
    pager_unit = f"crate-tier-{a.tag}-pagerd.service"
    pager_socket = f"/run/crate-tier-{a.tag}-pagerd.sock"
    failure = ""
    try:
        if a.private_bases:
            # Preparation is outside the warm-host/local-base timed endpoint.
            # Fresh inodes prevent daemon-cache reclamation from evicting old
            # experiments' shared prepared workspace pages. No global purge.
            from experiments.swebench_verified.private_bases import prepare
            prepare(a.private_base_source or a.inputs / "bases", run_root / "inputs/private-bases", tasks,
                    results / "preflight/private-bases.json")
        # ---- task daemons (owned, exact ExecStart recorded) --------------------
        launcher = str(ROOT / "experiments/cxl_tiering/launch-daemon.sh")
        for task in tasks:
            index = f"{task['sequence']:02d}"
            unit = f"crate-tier-{a.tag}-{index}.service"
            task["socket"] = f"/run/crate-tier-{a.tag}-{index}.sock"
            subprocess.run(["systemd-run", f"--unit={unit}", "--property=Type=simple",
                            "--property=PrivateMounts=yes", "--property=AllowedCPUs=0-7",
                            "--property=KillMode=mixed", "/bin/bash", launcher,
                            str(run_root), str(a.inputs), a.tag, index], check=True)
            units.append(unit)
        deadline = time.monotonic() + 120
        while not all(Path(task["socket"]).is_socket() for task in tasks):
            if time.monotonic() > deadline:
                raise RuntimeError("task daemons did not create their sockets")
            time.sleep(0.5)

        def register(task: dict[str, object]) -> dict[str, object]:
            index = f"{task['sequence']:02d}"
            prefix = [ctl, "--socket", str(task["socket"]), "--timeout", "3600s"]
            # The path is resolved inside the daemon's private mount namespace
            # (see launch-daemon.sh); on disk it is <run-root>/runtime/<index>.
            registered = json.loads(output(prefix + ["base-register", str(task["base"]),
                                                     f"/mnt/crate-tier/{index}/bases/prepared"]))
            original = json.loads((a.inputs / "artifacts" / f"prepare-{index}.json").read_text())
            historical_match = registered["digest"] == original["register"]["digest"]
            logical_copy: dict[str, object] = {}
            if not historical_match and a.private_bases:
                # TreeDigest includes directory st_size, which can change on
                # a byte-identical XFS copy. Do not waive content verification:
                # first authenticate the original against its recorded digest,
                # then checksum the private copy (including names, modes and
                # symlinks) with a strictly read-only rsync comparison.
                reference_path = a.inputs / "bases" / index
                reference = json.loads(output(prefix + ["base-register",
                    f"{task['base']}-reference", f"/mnt/crate-tier/{index}/bases/reference"]))
                if reference["digest"] != original["register"]["digest"]:
                    raise RuntimeError(f"historical source base changed: {index}")
                check = ["rsync", "-anic", "--delete", "--out-format=%i %n",
                         str(reference_path) + "/",
                         str(run_root / "inputs/private-bases" / index) + "/"]
                differences = output(check)
                if differences:
                    raise RuntimeError(f"private base differs logically: {index}: {differences[:2000]}")
                logical_copy = {"reference_digest": reference["digest"],
                    "comparison_argv": check, "comparison_output": differences,
                    "logically_identical": True,
                    "reason": "directory allocation size is not a portable tree-content identity"}
            return {"base": task["base"], "digest": registered["digest"],
                    "september_19_digest": original["register"]["digest"],
                    "historical_digest_match": historical_match,
                    "private_copy_verification": logical_copy,
                    "identical_prepared_base": historical_match or bool(logical_copy),
                    "files": registered["files"], "logical_bytes": registered["logical_bytes"]}

        with ThreadPoolExecutor(max_workers=8) as pool:
            bases = list(pool.map(register, tasks))
        write_json(results / "preflight" / "bases.json", bases)
        if not all(base["identical_prepared_base"] for base in bases):
            raise RuntimeError("a prepared base differs from the September 19 registration")
        sockets = results / "configs" / "sockets.json"
        write_json(sockets, {task["base"]: task["socket"] for task in tasks})
        run_selection = results / "configs" / "selection.json"
        write_json(run_selection, selection | {"tasks": tasks})
        cache_roots = results / "configs/daemon-cache-roots.json"
        write_json(cache_roots, {task["base"]:
            f"/sys/fs/cgroup/system.slice/crate-tier-{a.tag}-{task['sequence']:02d}.service/daemon"
            for task in tasks})

        # ---- integration gate: both backends through the normal sandbox API ----
        if not a.skip_paging_check:
            status["stage"] = "paging-check"
            write_json(results / "STATUS.json", status)
            tests = results / "tests"
            tests.mkdir(exist_ok=True)
            pager = start_pager(a, run_root, tests / f"pagerd-paging-check-{a.cxl_medium}.log",
                                pager_unit, pager_socket)
            try:
                with (tests / f"sandbox-paging-check-{a.cxl_medium}.log").open("w") as log:
                    gate = subprocess.run(
                        [sys.executable, str(ROOT / "experiments/cxl_tiering/sandbox_paging_check.py"),
                         "--ctl", ctl, "--socket", tasks[0]["socket"], "--base", tasks[0]["base"],
                         "--pager-socket", pager_socket,
                         "--holder", str(ROOT / "native/cxl_coldstore/build/coop_holder"),
                         "--holder-mib", "256",
                         "--output", str(tests / f"sandbox-paging-check-{a.cxl_medium}.json")],
                        stdout=log, stderr=subprocess.STDOUT)
            finally:
                status["paging_check"] = {"pagerd_argv": pager["pagerd_argv"],
                                          "pagerd_stop": stop_pager(a, pager_unit, pager["guards_before"])}  # type: ignore[arg-type]
            status["paging_check"]["returncode"] = gate.returncode  # type: ignore[index]
            write_json(results / "STATUS.json", status)
            if gate.returncode:
                raise RuntimeError("sandbox paging integration gate failed; comparison not started")

        if ns:
            for n in sorted(ns):
                status["stage"] = f"cold-n{n:02d}"
                write_json(results / "STATUS.json", status)
                cold_argv = [sys.executable, str(ROOT / "experiments/swebench_verified/run_cold_concurrent.py"),
                             "--selection", str(run_selection), "--limit", str(a.limit),
                             "--concurrency", str(n), "--ctl", ctl,
                             "--output", str(results / "raw" / f"cold-n{n:02d}")]
                with (results / "raw" / f"cold-n{n:02d}.log").open("w") as log:
                    process = subprocess.Popen(cold_argv, stdout=log, stderr=subprocess.STDOUT)
                    monitor.process = process
                    returncode = process.wait()
                    monitor.process = None
                if returncode or monitor.tripped:
                    raise RuntimeError(f"cold point {n} failed: rc={returncode}, {monitor.tripped}")

        # ---- one ordered run per configuration ---------------------------------
        for key, label, active in runs:
            raw = results / "raw" / key
            raw.mkdir(parents=True)
            entry: dict[str, object] = {"state": "running", "started_unix": time.time()}
            status["stage"] = key
            status["configurations"][key] = entry  # type: ignore[index]
            write_json(results / "STATUS.json", status)
            extra: list[str] = []
            guards_before: dict[str, str] = {}
            pager_running = False
            try:
                # Re-read each registered local base before every configuration,
                # including Vanilla. This is the identical declared warm-base
                # preparation; record its cost outside the lifecycle endpoint.
                if a.private_bases:
                    warm_started = time.time()
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        warmed = list(pool.map(register, tasks))
                    if not all(base["identical_prepared_base"] for base in warmed):
                        raise RuntimeError("prepared base changed during warm-up")
                    entry["base_warmup_seconds"] = time.time() - warm_started
                extra = replay_extras(label, cache_roots=cache_roots,
                                      cache_budget_mib=a.service_cache_budget_mib,
                                      session_serving=a.session_serving, pager_socket=pager_socket)
                if label in CXL_CONFIGS:
                    pager = start_pager(a, run_root, raw / "pagerd.log", pager_unit, pager_socket)
                    guards_before = pager["guards_before"]  # type: ignore[assignment]
                    entry["pagerd_argv"] = pager["pagerd_argv"]
                    pager_running = True
                argv = [sys.executable, str(ROOT / "experiments/trajectory_replay/run_campaign.py"),
                        "--workloads-dir", str(a.workloads), "--base", tasks[0]["base"],
                        "--base-socket-map", str(sockets),
                        "--source-provenance", str(ROOT / "SOURCE_PROVENANCE.json"),
                        "--socket", tasks[0]["socket"], "--ctl", ctl,
                        "--requests", str(a.limit), "--active-sandboxes", str(active),
                        "--wait-scale", "1", "--sample-ms", "100", "--estimated-wss-mib", "256",
                        "--dram-reserve-mib", "2048", "--turn-slo-ms", "180000",
                        "--turn-p99-slo-ms", "180000", "--output", str(raw / "replay.json"),
                        *CONFIGS[label], *extra]
                (results / "configs" / f"{key}.argv.json").write_text(json.dumps(argv, indent=2) + "\n")
                (results / "configs" / f"{key}.sh").write_text(shlex.join(argv) + "\n")
                with (raw / "replay.log").open("w") as log:
                    process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
                    monitor.process = process
                    returncode = process.wait()
                    monitor.process = None
                entry["returncode"] = returncode
                if monitor.tripped:
                    entry["paused_unsafe"] = monitor.tripped
                report = raw / "replay.json"
                if report.exists():
                    summary = json.loads(report.read_text())["summary"]
                    entry["summary"] = {key: summary[key] for key in (
                        "success", "requests", "completed_requests", "expected_tool_calls",
                        "completed_tool_calls", "reclaim_events", "reclaim_errors", "errors")}
                    entry["report_sha256"] = sha256_file(report)
                entry["state"] = ("completed" if returncode == 0 and report.exists()
                                  and not monitor.tripped else "failed")
                if entry["state"] == "completed":
                    from experiments.swebench_verified.checkpoints import compare
                    from experiments.cxl_tiering.analyze_tiering import analyze
                    result = json.loads(report.read_text())
                    if result["config"]["active_sandboxes"] != active:
                        raise RuntimeError("measured concurrency differs from plan")
                    standalone = compare(result, result)
                    write_json(raw / "validation.json", standalone)
                    write_json(raw / "metrics.json", analyze(result))
                    del result
                    if not ns and label != "baseline":
                        base_path = results / "raw/baseline/replay.json"
                        if base_path.exists():
                            paired = compare(json.loads(base_path.read_text()), json.loads(report.read_text()))
                            paired["mean_guard_pass"] = paired["mean_ratio"] <= 1.10
                            write_json(results / f"comparison-{label}.json", paired)
                    base_path = results / "raw" / f"n{active:02d}-baseline" / "replay.json"
                    crate_path = results / "raw" / f"n{active:02d}-crate-ssd" / "replay.json"
                    if base_path.exists() and crate_path.exists():
                        paired = compare(json.loads(base_path.read_text()), json.loads(crate_path.read_text()))
                        paired["mean_guard_pass"] = paired["mean_ratio"] <= 1.10
                        # A guard miss is a result, not a reason to drop this N.
                        write_json(results / f"comparison-n{active:02d}.json", paired)
                    print("CONFIGURATION", key, entry["state"], flush=True)
            except Exception as error:  # noqa: BLE001 - preserve the failure, keep going
                entry["state"] = "failed"
                entry["error"] = f"{type(error).__name__}: {error}"
            finally:
                if pager_running:
                    entry["pagerd_stop"] = stop_pager(a, pager_unit, guards_before)
                entry["finished_unix"] = time.time()
                monitor.tripped = ""
                write_json(results / "STATUS.json", status)
            if entry["state"] != "completed":
                raise RuntimeError(f"{key} failed safety/fidelity; preserve evidence and stop sweep")
            if entry["state"] != "completed" and label == "baseline":
                raise RuntimeError("Baseline failed; later configurations would have no reference")

        if not a.skip_cold and not ns:
            status["stage"] = "cold"
            write_json(results / "STATUS.json", status)
            subprocess.run([sys.executable, str(ROOT / "experiments/swebench_verified/run_cold.py"),
                            "--selection", str(run_selection), "--limit", str(a.limit),
                            "--repeats", str(a.cold_repeats), "--ctl", ctl,
                            "--output", str(results / "raw" / "cold")], check=True)
    except Exception as error:  # noqa: BLE001 - recorded, cleanup still runs
        failure = f"{type(error).__name__}: {error}"
    finally:
        monitor.running = False
        cleanup: dict[str, object] = {"owned_units": units, "verified_inactive": [], "left_running": []}
        for unit, task in zip(units, tasks):
            exec_start = output(["systemctl", "show", unit, "--property=ExecStart", "--value"])
            if str(run_root) not in exec_start:
                cleanup["left_running"].append({"unit": unit, "reason": "ExecStart is not this run's"})  # type: ignore[union-attr]
                continue
            try:
                listed = json.loads(output([ctl, "--socket", task["socket"], "list"]))["sandboxes"]
            except Exception:  # noqa: BLE001 - a dead daemon has no sandboxes to protect
                listed = []
            if listed:
                cleanup["left_running"].append({"unit": unit, "reason": f"sandboxes remain: {listed}"})  # type: ignore[union-attr]
                continue
            subprocess.run(["systemctl", "stop", unit], check=False)
            state = output(["systemctl", "show", unit, "--property=ActiveState", "--value"]).strip()
            (cleanup["verified_inactive"] if state in {"inactive", "failed"} else cleanup["left_running"]).append(  # type: ignore[union-attr]
                unit if state in {"inactive", "failed"} else {"unit": unit, "reason": state})
        swaps_after = swap_configuration()
        cleanup["swap_configuration_unchanged"] = swaps_before == swaps_after
        write_json(results / "preflight" / "cleanup.json", cleanup)
        write_json(results / "preflight" / "host-after.json", host_state())
        status["stage"] = "failed" if failure else "finished"
        if failure:
            status["error"] = failure
        status["finished_unix"] = time.time()
        write_json(results / "STATUS.json", status)
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
