#!/usr/bin/env python3
"""Normal-sandbox-API integration checks for both memory-tier backends (nsl17, root).

Evidence class: ACTUAL SANDBOX PAGING through SandboxFS, Caden's execution layer
and the injected backend - creation, real commands, model-wait demotion,
confirmed wake and cleanup. The CXL part pages a *cooperative holder process*
that was started inside a real sandbox through the normal exec API; it shows
the mechanism works for sandbox processes that register memory. It is not
SWE-bench tool state and not a density result: unmodified tools register
nothing, which the same check also records.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from caden.cxl_tier import CXLTierBackend, CXLTierConfig  # noqa: E402
from caden.sandboxfs_backend import SandboxFSConfig, SandboxFSExecution  # noqa: E402
from caden.types import AgentTask, DemotionRequest, RestoreRequest, Tier  # noqa: E402

MIB = 1 << 20
PATH_PREFIX = "export PATH=/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/bin:/bin; "


def memory_stat(execution: SandboxFSExecution, sandbox: str) -> dict[str, int]:
    cgroup = execution._record(sandbox).cgroup_path
    wanted = {"anon", "file", "shmem", "pgmajfault"}
    values = {key: int(value) for key, value in
              (line.split() for line in (cgroup / "memory.stat").read_text().splitlines())
              if key in wanted}
    values["memory_current"] = int((cgroup / "memory.current").read_text())
    values["swap_current"] = int((cgroup / "memory.swap.current").read_text())
    return values


def shell(execution: SandboxFSExecution, sandbox: str, command: str) -> dict[str, object]:
    started = time.monotonic_ns()
    response = execution.exec(sandbox, ("/bin/bash", "-c", PATH_PREFIX + command))
    response["client_ns"] = time.monotonic_ns() - started
    return response


def install_holder(execution: SandboxFSExecution, sandbox: str, binary: Path) -> None:
    """Deliver the fixture through the normal exec API, in argv-sized pieces."""
    encoded = base64.b64encode(binary.read_bytes()).decode()
    for start in range(0, len(encoded), 90_000):
        response = execution.exec(sandbox, (
            "/bin/sh", "-c", 'printf %s "$1" | base64 -d >> /tmp/coop_holder', "sh",
            encoded[start:start + 90_000]))
        if response.get("exit_code") != 0:
            raise RuntimeError(f"holder install failed: {response!r}")
    if shell(execution, sandbox, "chmod +x /tmp/coop_holder").get("exit_code") != 0:
        raise RuntimeError("holder chmod failed")


def holder(execution: SandboxFSExecution, sandbox: str, verb: str) -> dict[str, object]:
    response = shell(execution, sandbox, f"/tmp/coop_holder {verb} --socket /tmp/coop.sock")
    parsed = json.loads(str(response.get("stdout") or "{}"))
    parsed["exec_client_ns"] = response["client_ns"]
    parsed["exec_server_ns"] = response.get("duration_ns")
    return parsed


def cycle(execution: SandboxFSExecution, sandbox: str, tier: Tier, generation: int,
          check) -> dict[str, object]:
    record: dict[str, object] = {"before": memory_stat(execution, sandbox)}
    started = time.monotonic_ns()
    demotion = execution.demote_selective(
        sandbox, DemotionRequest(tier, min_resident_bytes=0, generation=generation))
    record["demote_ns"] = time.monotonic_ns() - started
    record["demotion"] = demotion.__dict__ | {"tier": demotion.tier.value,
                                              "reclaim_mode": demotion.reclaim_mode.value}
    record["cold"] = memory_stat(execution, sandbox)
    time.sleep(1.0)  # a model wait
    started = time.monotonic_ns()
    restore = execution.restore_selective(sandbox, RestoreRequest(generation=generation + 1))
    record["restore_ns"] = time.monotonic_ns() - started
    record["restore"] = restore.__dict__
    record["first_tool"] = check()
    record["after"] = memory_stat(execution, sandbox)
    record["tier_accounting"] = execution.tier_accounting(sandbox).as_json()
    return record


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ctl", required=True)
    ap.add_argument("--socket", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--pager-socket", required=True)
    ap.add_argument("--holder", type=Path, required=True)
    ap.add_argument("--holder-mib", type=int, default=256)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("root required (sandbox cgroups, pidfd_getfd)")
    if a.output.exists():
        raise SystemExit("refusing existing output")
    config = SandboxFSConfig(ctl_path=a.ctl, socket_path=a.socket, mode="t1", restore_mode="thaw-only")
    report: dict[str, object] = {"schema": "crate-tiering-sandbox-paging-check-v1",
                                 "evidence_class": "actual sandbox paging via normal SandboxFS API",
                                 "started_unix": time.time(), "checks": {}}
    checks: dict[str, object] = report["checks"]  # type: ignore[assignment]
    ok = True

    # ---- extracted SSD backend, unchanged default --------------------------------
    execution = SandboxFSExecution(config, id_factory=lambda: f"tc-ssd-{os.getpid()}")
    sandbox = execution.run(AgentTask([], a.base), lambda *_: None)
    try:
        def real_tool() -> dict[str, object]:
            response = shell(execution, sandbox,
                             "cd /testbed && python -V && git -c safe.directory=/workspace/repository status --short | head -3")
            return {"exit_code": response.get("exit_code"), "exec_client_ns": response["client_ns"],
                    "stdout": str(response.get("stdout"))[:200]}
        first = real_tool()
        record = cycle(execution, sandbox, Tier.SSD, 1, real_tool)
        record["first_command"] = first
        record["capabilities"] = execution.memory_tier.capabilities().as_json()
        try:
            execution.demote_selective(sandbox, DemotionRequest(Tier.CXL))
            record["cxl_request_rejected"] = False
        except Exception as error:  # noqa: BLE001 - the explicit rejection is the check
            record["cxl_request_rejected"] = type(error).__name__
        record["ok"] = (first["exit_code"] == 0 and record["first_tool"]["exit_code"] == 0  # type: ignore[index]
                        and record["cxl_request_rejected"] == "SandboxFSTierUnsupported"
                        and record["demotion"]["reclaimed_bytes"] > 0)  # type: ignore[index]
        checks["ssd_backend_normal_api"] = record
        ok &= bool(record["ok"])
    finally:
        execution.revoke(sandbox)

    # ---- CXL backend: unmodified tools register nothing; a cooperative process pages
    for mode, allow_user_only in (("eager", False), ("lazy", False), ("lazy", True)):
        name = f"cxl_backend_{mode}" + ("_user_mode_faults" if allow_user_only else "")
        backend = CXLTierBackend(CXLTierConfig(socket_path=a.pager_socket, restore_mode=mode,
                                               allow_user_mode_only_lazy=allow_user_only))
        execution = SandboxFSExecution(config, memory_tier=backend,
                                       # Short identifiers: SandboxFS embeds them in a Unix socket path.
                                       id_factory=lambda: f"tc-{mode}{int(allow_user_only)}-{os.getpid()}")
        sandbox = execution.run(AgentTask([], a.base), lambda *_: None)
        record = {"capabilities": backend.capabilities().as_json()}
        try:
            tool = shell(execution, sandbox, "cd /testbed && python -c 'import sys; print(sys.version_info[:2])'")
            unmodified = cycle(execution, sandbox, Tier.CXL, 1,
                               lambda: {"exit_code": shell(execution, sandbox, "cd /testbed && ls | head -2").get("exit_code")})
            record["unmodified_tools_only"] = unmodified
            install_holder(execution, sandbox, a.holder)
            ready = shell(execution, sandbox,
                          f"/tmp/coop_holder serve --socket /tmp/coop.sock --size-mib {a.holder_mib} --daemonize")
            record["holder_ready"] = json.loads(str(ready.get("stdout") or "{}"))
            record["hot_check"] = holder(execution, sandbox, "check")
            first = cycle(execution, sandbox, Tier.CXL, 3, lambda: holder(execution, sandbox, "check"))
            record["cooperative_cycle"] = first
            record["mutate"] = holder(execution, sandbox, "mutate")
            second = cycle(execution, sandbox, Tier.CXL, 5, lambda: holder(execution, sandbox, "check"))
            record["cooperative_cycle_after_mutation"] = second
            try:
                execution.demote_selective(sandbox, DemotionRequest(Tier.SSD))
                record["ssd_request_rejected"] = False
            except Exception as error:  # noqa: BLE001 - the explicit rejection is the check
                record["ssd_request_rejected"] = type(error).__name__
            size = a.holder_mib * MIB
            record["ok"] = (
                tool.get("exit_code") == 0
                and unmodified["demotion"]["stored_bytes"] == 0  # type: ignore[index]
                and unmodified["demotion"]["swap_delta_bytes"] == 0  # type: ignore[index]
                and record["hot_check"]["ok"] is True  # type: ignore[index]
                and first["demotion"]["stored_bytes"] == size  # type: ignore[index]
                and first["demotion"]["released_bytes"] == size  # type: ignore[index]
                and first["demotion"]["swap_delta_bytes"] == 0  # type: ignore[index]
                and first["cold"]["shmem"] <= first["before"]["shmem"] - size * 0.95  # type: ignore[index]
                and first["first_tool"]["ok"] is True  # type: ignore[index]
                and second["first_tool"]["ok"] is True  # type: ignore[index]
                and record["ssd_request_rejected"] == "UnsupportedTierError"
            )
        except Exception as error:  # noqa: BLE001 - recorded as a failed check
            record["error"] = f"{type(error).__name__}: {error}"
            record["ok"] = False
        finally:
            execution.revoke(sandbox)
            record["store_after_revoke"] = backend.store_stats()
            record["ok"] = bool(record.get("ok")) and record["store_after_revoke"]["cold"] == "0" \
                and record["store_after_revoke"]["sandboxes"] == "0"
            backend.close()
        checks[name] = record
        ok &= bool(record["ok"])
    report["ok"] = ok
    report["finished_unix"] = time.time()
    a.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps({name: check.get("ok") for name, check in checks.items()}))  # type: ignore[union-attr]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
