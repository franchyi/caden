import pytest

from experiments.cxl_tiering.run_cxl_cold import timings


def state():
    return {"timings": {"request_received_at": "1970-01-01T00:00:01Z",
                        "workspace_start_ns": 10, "workspace_ready_ns": 110,
                        "total_ns": 500}}


def test_endpoint_includes_post_create_attach_and_first_command():
    result = timings(state(), 0, 999_999_900, 1100, 1_000_001_000)
    assert result["cold_start_ns"] == 1000
    assert result["filesystem_provision_ns"] == 100
    assert result["daemon_ready_ns"] == 500
    assert result["client_submission_to_ready_ns"] == 1100
    assert result["wall_monotonic_discrepancy_ns"] == 0


def test_invalid_phase_order_is_not_reported_as_cold_result():
    bad = state()
    bad["timings"]["total_ns"] = 2000
    with pytest.raises(RuntimeError, match="inconsistent"):
        timings(bad, 0, 999_999_900, 1100, 1_000_001_000)


def test_wall_clock_jump_is_rejected():
    with pytest.raises(RuntimeError, match="wall clock"):
        timings(state(), 0, 995_000_000, 1100, 1_000_001_000)
