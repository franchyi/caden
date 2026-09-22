from experiments.swebench_verified.audit_development import MEMORY, markdown


def audit_fixture(complete, calls):
    return {"audited_at": "2026-09-19", "campaign_complete": complete,
            "runs": {"r0-F0-S0": {"tool_ms": {"n": calls, "mean": 1, "p95": 2, "p99": 3},
                "memory": {field: {"time_weighted_mean_mib": 1} for field in MEMORY[:4]}}},
            "comparisons": {}, "treatment_ablations": {}}


def test_formal_audit_reports_actual_population_and_completion():
    text = markdown(audit_fixture(True, 282))
    assert "Completed campaign" in text
    assert "Partial campaign" not in text
    assert "n=282, rank=280" in text
    assert "With 77 calls" not in text


def test_partial_development_audit_does_not_claim_completion():
    text = markdown(audit_fixture(False, 77))
    assert "Partial campaign" in text
    assert "Completed campaign" not in text
    assert "n=77, rank=77" in text
