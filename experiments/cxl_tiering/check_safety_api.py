#!/usr/bin/env python3
"""Safety-only regression via an already isolated SandboxFS daemon.

Uses a regular-file store, one cooperative holder, and only the supplied API
socket. No DAX, swapon, cache purge, or other service manipulation.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from caden.cxl_tier import CXLTierBackend, CXLTierConfig, CXLTierError, PagerDaemon
from caden.sandboxfs_backend import SandboxFSConfig, SandboxFSExecution
from caden.types import AgentTask, DemotionRequest, RestoreRequest, Tier
from experiments.cxl_tiering.sandbox_paging_check import holder, install_holder, shell


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ctl", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(exist_ok=False)
    store = args.output / "store.bin"
    with store.open("xb") as stream:
        stream.truncate(64 << 20)
    daemon = PagerDaemon(ROOT / "native/cxl_coldstore/build/crate_pagerd",
                         args.output / "pager.sock", store_path=store, offset=0,
                         capacity=64 << 20, logical_bytes=63 << 20,
                         log_path=args.output / "pager.log")
    daemon.start()
    backend = CXLTierBackend(CXLTierConfig(socket_path=str(daemon.socket_path),
                                          file_cache_reclaim=False, reclaim_settle_seconds=0))
    execution = SandboxFSExecution(SandboxFSConfig(ctl_path=args.ctl, socket_path=args.socket,
                                                    mode="t1", restore_mode="thaw-only"),
                                   memory_tier=backend, id_factory=lambda: f"cf-{os.getpid()}")
    receipt = {"evidence_class": "normal-SandboxFS-API safety regression; file-emulated store",
               "performance_measurement": False, "success": False, "cleanup_errors": []}
    try:
        sandbox = execution.run(AgentTask([], args.base), lambda *_: None)
        record = execution._record(sandbox)
        install_holder(execution, sandbox, ROOT / "native/cxl_coldstore/build/coop_holder")
        response = shell(execution, sandbox, "/tmp/coop_holder serve --socket /tmp/coop.sock --size-mib 16 --daemonize")
        assert response["exit_code"] == 0, response
        original = backend._client.call

        def lost(*tokens):
            response = original(*tokens)
            if tokens[0] == "DEMOTE":
                raise CXLTierError("injected lost reply after native demotion")
            return response

        with patch.object(backend._client, "call", side_effect=lost):
            try:
                execution.demote_selective(sandbox, DemotionRequest(Tier.CXL, generation=1))
            except CXLTierError:
                pass
            else:
                raise AssertionError("lost reply was not injected")
        receipt["cold_after_lost_reply"] = backend.accounting(record).resident_in_tier_bytes
        assert receipt["cold_after_lost_reply"] == 16 << 20
        restored = execution.restore_selective(sandbox, RestoreRequest(generation=2))
        assert restored.ready_for_dispatch and restored.tier_restored_bytes == 16 << 20
        receipt["restored_content"] = holder(execution, sandbox, "check")
        assert receipt["restored_content"]["ok"]
        execution.demote_selective(sandbox, DemotionRequest(Tier.CXL, generation=3))
        with store.open("r+b") as stream:
            stream.write(b"\x5a" * (8 << 20))  # Test-owned payload corruption.
        with patch.object(execution, "_set_frozen", wraps=execution._set_frozen) as freezer:
            execution.revoke(sandbox)
        freezer.assert_not_called()
        receipt["teardown_restore_error"] = record.state.get("teardown_restore_error")
        assert receipt["teardown_restore_error"]
        receipt["cgroup_removed"] = not record.cgroup_path.exists()
        assert receipt["cgroup_removed"]
        receipt["store_after_destroy"] = backend.store_stats()
        assert receipt["store_after_destroy"]["cold"] == "0"
        assert receipt["store_after_destroy"]["sandboxes"] == "0"
        receipt["success"] = True
    except Exception as error:
        receipt["error"] = repr(error)
    finally:
        for sandbox in execution.sandbox_ids():
            try:
                execution.revoke(sandbox)
            except Exception as error:
                receipt["cleanup_errors"].append(repr(error))
        try:
            backend.close()
            daemon.stop()
        except Exception as error:
            receipt["cleanup_errors"].append(repr(error))
        receipt["pager_exited"] = daemon.process is None or daemon.process.poll() is not None
        receipt["success"] &= not receipt["cleanup_errors"] and receipt["pager_exited"]
        (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    return 0 if receipt["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
