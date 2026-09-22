import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from experiments.cxl_tiering.run_tiering_suite import sweep_plan
from experiments.swebench_verified import run_cold_concurrent as cold


def test_fixed_work_alternating_order():
    plan = sweep_plan([32, 16, 8, 4, 2, 1], ["baseline", "crate-ssd"], 32)
    assert len(plan) == 12
    assert plan[:4] == [("n32-baseline", "baseline", 32), ("n32-crate-ssd", "crate-ssd", 32),
                        ("n16-crate-ssd", "crate-ssd", 16), ("n16-baseline", "baseline", 16)]
    assert {n for _, _, n in plan} == {1, 2, 4, 8, 16, 32}


@pytest.mark.parametrize("ns,labels", [([0], ["baseline", "crate-ssd"]),
    ([3], ["baseline", "crate-ssd"]), ([8, 8], ["baseline", "crate-ssd"]),
    ([8], ["baseline", "crate-cxl"]), ([], ["baseline", "crate-ssd"])])
def test_sweep_rejects_invalid(ns, labels):
    with pytest.raises(ValueError):
        sweep_plan(ns, labels, 32)


def test_cold_batches_keep_same_tasks_in_both_modes():
    tasks = [{"sequence": i} for i in range(32)]
    for n in (1, 2, 4, 8, 16, 32):
        plan = list(cold.batch_plan(tasks, n))
        assert len(plan) == 64 // n
        for mode in ("baseline", "t1"):
            assert [t["sequence"] for _, m, group in plan if m == mode for t in group] == list(range(32))
        assert plan[0][1] == "baseline" and plan[1][1] == "t1"
        if n < 32:
            assert plan[2][1] == "t1" and plan[3][1] == "baseline"


def test_cold_barrier_endpoint_and_failure_retention(monkeypatch):
    calls = []

    def api(prefix, args):
        calls.append(args[0])
        if args[0] == "create":
            return {"timings": {"request_received_at": "2026-09-20T00:00:00Z",
                                "workspace_ready_ns": 11, "workspace_start_ns": 1, "total_ns": 20}}
        return {"exit_code": 1 if args[1] == "bad" else 0}

    monkeypatch.setattr(cold, "api_call", api)
    barrier = threading.Barrier(2)
    task = {"socket": "/run/unit-test.sock", "base": "test"}
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(cold.measure, task, "t1", ident, barrier, "ctl") for ident in ("ok", "bad")]
        good, bad = [f.result() for f in futures]
    assert good["cold_start_ns"] > 0 and good["filesystem_provision_ns"] == 10
    assert "error" in bad and bad["first_command"]["exit_code"] == 1
    assert "destroy" not in calls  # Parent cleans up only after every endpoint.
