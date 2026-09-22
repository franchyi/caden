#!/usr/bin/env python3
"""CXL-attached, no-pool cold sandboxes; warm host, 32 local prepared bases.

One sample per selected SWE-bench task. The measured interval uses the original
create-handler-to-first-command endpoint, now including Caden's real CXL attach.
Pager/service startup is untimed host preparation. There is no replay, demotion,
global cache purge, swap activation, base mutation or unrelated service cleanup.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from caden.cxl_tier import CXLTierBackend, CXLTierConfig
from caden.sandboxfs_backend import SandboxFSConfig, SandboxFSExecution
from caden.types import AgentTask
from experiments.cxl_tiering.run_tiering_suite import (
    PARENT, HostMonitor, host_state, output, sha256_file, start_pager,
    stop_pager, swap_configuration, write_json,
)
from experiments.swebench_verified.run_cold import api_call, first_touch_probe, timestamp_ns
from experiments.swebench_verified.run_suite import verify_source


def timings(state, start_mono, start_wall, end_mono, end_wall):
    phase = state["timings"]
    values = {
        "cold_start_ns": end_wall - timestamp_ns(phase["request_received_at"]),
        "client_submission_to_ready_ns": end_mono - start_mono,
        "wall_monotonic_discrepancy_ns": (end_wall - start_wall) - (end_mono - start_mono),
        "filesystem_provision_ns": phase["workspace_ready_ns"] - phase["workspace_start_ns"],
        "daemon_ready_ns": phase["total_ns"],
    }
    if not (0 < values["filesystem_provision_ns"] <= values["daemon_ready_ns"]
            <= values["cold_start_ns"] <= values["client_submission_to_ready_ns"] + 1_000_000):
        raise RuntimeError(f"inconsistent cold timing: {values}")
    if abs(values["wall_monotonic_discrepancy_ns"]) > 1_000_000:
        raise RuntimeError("wall clock changed during cold sample")
    return values


class TimedCXL(CXLTierBackend):
    """Measure, but do not change, the normal backend attachment."""
    def attach(self, sandbox):
        start = time.monotonic_ns()
        super().attach(sandbox)
        self.last_attach_ns = time.monotonic_ns() - start


def safety_check():
    available = next(int(s.split()[1]) * 1024 for s in Path("/proc/meminfo").read_text().splitlines()
                     if s.startswith("MemAvailable:"))
    psi = float(re.search(r"some avg10=([\d.]+)", Path("/proc/pressure/memory").read_text())[1])
    if available < 8 << 30 or psi > 30:
        raise RuntimeError(f"host safety limit: MemAvailable={available}, PSI={psi}")
    return {"mem_available_bytes": available, "memory_psi_some_avg10": psi}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", type=Path, required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--reservation", type=Path, required=True)
    ap.add_argument("--inputs", type=Path, default=Path("/sandboxfs/crate-swebench-20260919"))
    a = ap.parse_args()
    if os.geteuid() != 0 or os.uname().nodename not in {"nsl17", "nsl-node17"}:
        raise SystemExit("root on nsl17 required")
    if (a.run_root.is_symlink() or not a.run_root.is_dir() or a.run_root.parent != PARENT
            or PARENT.is_symlink() or ROOT != a.run_root / "source"
            or not re.fullmatch(r"[a-z0-9]{4,16}", a.tag)):
        raise SystemExit("fresh isolated run root and short unique tag required")
    verify_source()
    reservation = json.loads(a.reservation.read_text())
    a.cxl_medium, a.dax_offset, a.dax_capacity = "dax", 384 << 30, 4 << 30
    if (reservation.get("device") != "/dev/dax0.0" or not reservation.get("confirmed_by_user")
            or reservation.get("offset") != a.dax_offset or reservation.get("capacity") != a.dax_capacity):
        raise SystemExit("requires the existing user-confirmed 384–388 GiB reservation")
    results = a.run_root / "results"
    results.mkdir()  # no reuse or overwrite
    for name in ("raw", "preflight", "configs"):
        (results / name).mkdir()
    before = host_state()
    write_json(results / "preflight/host-before.json", before)
    if before["dax_holders"] or before["meminfo_bytes"]["MemAvailable"] < 24 << 30:
        raise RuntimeError("DAX holder or inadequate host headroom")
    foreign = [s for s in before["crate_units"] if " active " in f" {s} "
               and not s.startswith(("crate-sv-docker.service", f"crate-tier-{a.tag}-"))]
    if foreign:
        raise RuntimeError(f"active foreign measurement services: {foreign}")
    swaps = swap_configuration()
    selection_path = a.inputs / "selection/manifest.json"
    selection = json.loads(selection_path.read_text())
    tasks = selection["tasks"][:32]
    assert len(tasks) == len({t["instance_id"] for t in tasks}) == 32
    assert len({t["sequence"] for t in tasks}) == 32
    ctl = str(a.inputs / "bin/sandboxfsctl")
    launcher = str(ROOT / "experiments/cxl_tiering/launch-daemon.sh")
    status = {"stage": "preparing", "completed_samples": 0, "started_unix": time.time()}
    write_json(results / "PLAN.json", {
        "schema": "crate-cxl-cold-v1", "samples": 32, "repetitions": 1, "concurrency": 1,
        "mode": "OverlayFS plus actual CXL backend attach", "pool": False,
        "endpoint": "SandboxFS create-handler entry to first successful /bin/true API response, including Caden CXL attach",
        "untimed": "daemon/pager startup and normal base registration; local prepared bases, warm host",
        "first_touch": "separate post-endpoint normal-API tracked-file read and same-byte copy-up write",
        "tier_payload_expected_bytes": 0, "demotion": False,
        "scope": "descriptive later run, not paired hardware comparison or CXL offloading evidence",
        "historical_comparison": "September 19 uses the same 32 task IDs and timing formula but no Caden backend attach",
        "selection_sha256": sha256_file(selection_path),
        "source_provenance_sha256": sha256_file(ROOT / "SOURCE_PROVENANCE.json"),
        "binary_sha256": {str(p): sha256_file(p) for p in (
            Path(ctl), a.inputs / "bin/sandboxfsd", ROOT / "native/cxl_coldstore/build/crate_pagerd")},
        "dax_offset": a.dax_offset, "dax_capacity": a.dax_capacity,
        "safety": "8 GiB MemAvailable floor, 30% memory PSI ceiling; no host setting changes",
    })
    write_json(results / "STATUS.json", status)
    units, records, failure = [], [], ""
    pager_unit = f"crate-tier-{a.tag}-pagerd.service"
    pager_socket = f"/run/crate-tier-{a.tag}-pagerd.sock"
    pager, backend = None, None
    monitor = HostMonitor(results / "preflight/host-monitor.jsonl", 8 << 30, 30)
    monitor.start()
    def terminated(_signum, _frame):
        raise RuntimeError("controller termination requested")
    signal.signal(signal.SIGTERM, terminated)
    try:
        for task in tasks:
            safety_check()
            index = f"{task['sequence']:02d}"
            task["socket"] = f"/run/crate-tier-{a.tag}-{index}.sock"
            unit = f"crate-tier-{a.tag}-{index}.service"
            units.append((unit, task))
            subprocess.run(["systemd-run", f"--unit={unit}", "--property=Type=exec",
                "--property=PrivateMounts=yes", "--property=AllowedCPUs=0-7", "--property=KillMode=mixed",
                "/bin/bash", launcher, str(a.run_root), str(a.inputs), a.tag, index], check=True)
        deadline = time.monotonic() + 90
        while not all(Path(t["socket"]).is_socket() for t in tasks):
            if time.monotonic() > deadline:
                raise RuntimeError("sandbox daemon socket deadline")
            time.sleep(0.2)
        def register(task):
            index = f"{task['sequence']:02d}"
            result = api_call([ctl, "--socket", task["socket"], "--timeout", "600s"],
                             ["base-register", task["base"], f"/mnt/crate-tier/{index}/bases/prepared"])
            original = json.loads((a.inputs / "artifacts" / f"prepare-{index}.json").read_text())
            if result["digest"] != original["register"]["digest"]:
                raise RuntimeError(f"historical base digest changed: {index}")
            return {"task": task, "registration": result, "historical_digest_matches": True}
        with ThreadPoolExecutor(max_workers=8) as pool:
            bases = list(pool.map(register, tasks))
        write_json(results / "preflight/bases.json", bases)
        write_json(results / "configs/selection.json", selection | {"tasks": tasks})
        pager = start_pager(a, a.run_root, results / "raw/pagerd.log", pager_unit, pager_socket)
        write_json(results / "preflight/pager-start.json", pager)
        backend = TimedCXL(CXLTierConfig(socket_path=pager_socket, restore_mode="eager"))
        caps = backend.capabilities().as_json()
        hello = caps["details"]["pager"]
        if (caps["medium"] != "device-dax" or hello["path"] != "/dev/dax0.0"
                or int(hello["offset"]) != a.dax_offset or int(hello["capacity"]) != a.dax_capacity):
            raise RuntimeError(f"incorrect CXL mapping: {hello}")
        write_json(results / "preflight/backend.json", caps)
        for task in tasks:
            safety = safety_check()
            record = {"task": task, "mode": "crate-cxl", "repeat": 0, "safety_before": safety}
            sandbox = None
            execution = SandboxFSExecution(SandboxFSConfig(ctl_path=ctl, socket_path=task["socket"],
                mode="t1", restore_mode="thaw-only"), memory_tier=backend,
                id_factory=lambda: f"cc-{task['sequence']:02d}")
            try:
                start_mono, start_wall = time.monotonic_ns(), time.time_ns()
                sandbox = execution.run(AgentTask([], task["base"]), lambda *_: None)
                response = execution.exec(sandbox, ("/bin/true",))
                end_mono, end_wall = time.monotonic_ns(), time.time_ns()
                if response.get("exit_code") != 0:
                    raise RuntimeError("first normal-API command failed")
                state = execution._record(sandbox).state
                record.update(state=state, sandbox_id=sandbox, first_command=response,
                              cxl_attach_ns=backend.last_attach_ns,
                              **timings(state, start_mono, start_wall, end_mono, end_wall))
                cgroup = execution._record(sandbox).cgroup_path
                record["swap_max"] = (cgroup / "memory.swap.max").read_text().strip()
                record["store_after_first_command"] = backend.store_stats()
                if record["swap_max"] != "0" or int(record["store_after_first_command"]["sandboxes"]) != 1:
                    raise RuntimeError("CXL attachment/swap prohibition not established")
                record["first_touch"] = {}
                prefix = [ctl, "--socket", task["socket"], "--timeout", "600s"]
                first_touch_probe(lambda argv: api_call(prefix, argv), sandbox, record["first_touch"])
            except Exception as error:
                record["error"] = f"{type(error).__name__}: {error}"
            finally:
                if sandbox is not None:
                    try:
                        execution.revoke(sandbox)
                        record["store_after_revoke"] = backend.store_stats()
                        if any(int(record["store_after_revoke"][k]) != 0 for k in ("sandboxes", "cold")):
                            raise RuntimeError("pager has residual consumers or cold state")
                        record["destroyed"] = True
                    except Exception as error:
                        record["cleanup_error"] = str(error)
                        record.setdefault("error", str(error))
                records.append(record)
                with (results / "raw/samples.jsonl").open("a") as handle:
                    handle.write(json.dumps(record) + "\n")
                status.update(stage="measuring", completed_samples=sum("error" not in r for r in records),
                              last_task=task["instance_id"])
                write_json(results / "STATUS.json", status)
            print(task["instance_id"], record.get("cold_start_ns"), record.get("error", "OK"), flush=True)
            if "error" in record:
                raise RuntimeError(record["error"])
            safety_check()
    except Exception as error:
        failure = f"{type(error).__name__}: {error}"
    finally:
        cleanup = {"units": [], "errors": []}
        if backend is not None:
            try:
                cleanup["store_final"] = backend.store_stats()
                backend.close()
            except Exception as error:
                cleanup["errors"].append(str(error))
        for unit, task in units:
            try:
                command = output(["systemctl", "show", unit, "-p", "ExecStart", "--value"])
                if str(a.run_root) not in command:
                    raise RuntimeError(f"unit ownership mismatch: {unit}")
                active = api_call([ctl, "--socket", task["socket"]], ["list"])["sandboxes"]
                if active:
                    raise RuntimeError(f"sandbox consumers remain: {unit}")
                subprocess.run(["systemctl", "stop", unit], check=True)
                state = output(["systemctl", "show", unit, "-p", "ActiveState", "--value"]).strip()
                cleanup["units"].append({"unit": unit, "state": state})
                if state != "inactive":
                    raise RuntimeError(f"unit remains active: {unit}")
            except Exception as error:
                cleanup["errors"].append(str(error))
        if pager is not None and not cleanup["errors"]:
            cleanup["pager"] = stop_pager(a, pager_unit, pager["guards_before"])
            if (cleanup["pager"]["state"] != "inactive"
                    or not cleanup["pager"]["dax_guards"]["unchanged"]):
                cleanup["errors"].append("pager cleanup or DAX guard failure")
        monitor.running = False
        monitor.join(timeout=6)
        cleanup["swap_configuration_unchanged"] = swaps == swap_configuration()
        if not cleanup["swap_configuration_unchanged"]:
            cleanup["errors"].append("host swap configuration changed")
        write_json(results / "preflight/cleanup.json", cleanup)
        write_json(results / "preflight/host-after.json", host_state())
        failure = failure or (str(cleanup["errors"]) if cleanup["errors"] else "")
        status.update(stage="failed" if failure else "finished", error=failure, finished_unix=time.time())
        write_json(results / "STATUS.json", status)
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
