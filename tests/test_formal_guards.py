"""Standalone formal guards and exact service cleanup; never contacts a host."""
import copy
import json

import pytest

from experiments.swebench_verified import run_suite as suite
from tests.test_checkpoint_fidelity import reports, write_json  # noqa: F401


def test_explicit_single_comparison_changes_counts_not_baseline_or_guards():
    assert suite.formal_orders(1, "comparison") == [["F0-S0", "T1-S2"]]
    assert suite.formal_orders(3, "full") == suite.FORMAL_ORDERS
    assert suite.formal_orders(1, "full") == [suite.FORMAL_ORDERS[0]]
    with pytest.raises(ValueError):
        suite.formal_orders(3, "comparison")


def test_treatment_before_baseline_has_strict_standalone_and_deferred_pair(tmp_path, reports):
    _, treatment, _, _ = reports
    write_json(tmp_path / "replay-r1-T1-S2.json", treatment)
    suite.checkpoint_configuration(tmp_path, 1, "T1-S2", ["T1-S2", "F0-S0"], "formal")
    standalone = json.loads((tmp_path / "standalone-r1-T1-S2.json").read_text())
    assert standalone["success"] and standalone["fidelity"]["formal_evidence_verified"]
    assert standalone["relative_guard_status"] == "deferred-until-same-repetition-baseline"
    assert not list(tmp_path.glob("checkpoint-*.json"))
    assert json.loads((tmp_path / "CONFIGURATION-r1-T1-S2.json").read_text())["success"]


def test_absolute_deadline_fails_before_baseline_and_preserves_evidence(tmp_path, reports):
    _, treatment, _, _ = reports
    tool = treatment["replays"][0]["tools"][0]
    for key in ("turn_ns", "command_ns", "exec_rpc_ns"):
        tool[key] += 180_000_000_000
    raw = tmp_path / "replay-r1-T1-S2.json"
    write_json(raw, treatment)
    with pytest.raises(RuntimeError, match="absolute deadline"):
        suite.checkpoint_configuration(tmp_path, 1, "T1-S2", ["T1-S2", "F0-S0"], "formal")
    assert raw.exists()
    standalone = json.loads((tmp_path / "standalone-r1-T1-S2.json").read_text())
    assert not standalone["success"] and standalone["deadline_violations"] == 1
    assert not json.loads((tmp_path / "CONFIGURATION-r1-T1-S2.json").read_text())["success"]


def test_fingerprint_fails_before_baseline(tmp_path, reports):
    _, treatment, _, _ = reports
    treatment["replays"][0]["fingerprint"]["exit_code"] = 1
    write_json(tmp_path / "replay-r1-T1-S2.json", treatment)
    with pytest.raises(ValueError, match="fingerprint"):
        suite.checkpoint_configuration(tmp_path, 1, "T1-S2", ["T1-S2", "F0-S0"], "formal")
    assert not json.loads((tmp_path / "standalone-r1-T1-S2.json").read_text())["success"]


def test_arriving_same_rep_baseline_checks_earlier_treatment_without_relaxation(tmp_path, reports):
    baseline, treatment, _, _ = reports
    for key in ("turn_ns", "command_ns", "exec_rpc_ns"):
        treatment["replays"][0]["tools"][0][key] += 101
    write_json(tmp_path / "replay-r1-T1-S2.json", treatment)
    suite.checkpoint_configuration(tmp_path, 1, "T1-S2", ["T1-S2", "F0-S0"], "formal")
    write_json(tmp_path / "replay-r1-F0-S0.json", baseline)
    with pytest.raises(RuntimeError, match="matched checkpoint failed"):
        suite.checkpoint_configuration(tmp_path, 1, "F0-S0", ["T1-S2", "F0-S0"], "formal")
    paired = json.loads((tmp_path / "checkpoint-r1-T1-S2.json").read_text())
    assert paired["p95_ratio"] == pytest.approx(1.101) and not paired["guards_pass"]


def test_campaign_identity_anchor_rejects_later_changed_fixed_setting(tmp_path, reports):
    baseline, treatment, _, _ = reports
    write_json(tmp_path / "replay-r0-F0-S0.json", baseline)
    treatment["config"]["synchronize_response_commits"] = True
    write_json(tmp_path / "replay-r1-T1-S2.json", treatment)
    with pytest.raises(ValueError, match="fixed workload setting"):
        suite.checkpoint_configuration(tmp_path, 1, "T1-S2", ["T1-S2", "F0-S0"], "formal")


def cleanup_mocks(monkeypatch, *, wrong_argv=False, active=False, nonempty=False):
    stops = []
    def output(argv, text=True):
        if "--property=ExecStart" in argv:
            args = f"/bin/bash {suite.C}/scripts/launch-daemon.sh 00"
            if wrong_argv:
                args += " --unowned-extra-argument"
            return "{ path=/bin/bash ; argv[]=" + args + " ; start_time=[n/a] ; }"
        if "--property=ActiveState" in argv:
            return "active" if active else "inactive"
        return json.dumps({"sandboxes": ["unowned"] if nonempty else []})
    monkeypatch.setattr(suite.subprocess, "check_output", output)
    monkeypatch.setattr(suite.subprocess, "run", lambda argv, check: stops.append(argv))
    return stops


@pytest.mark.parametrize("options", [{"wrong_argv": True}, {"nonempty": True}])
def test_cleanup_validates_all_targets_before_any_stop(tmp_path, monkeypatch, options):
    stops = cleanup_mocks(monkeypatch, **options)
    with pytest.raises(RuntimeError):
        suite.cleanup_owned_services([{"sequence": 0, "socket": "/run/crate-sv-00.sock"}], tmp_path)
    assert not stops
    assert not json.loads((tmp_path / "cleanup.json").read_text())["success"]


def test_cleanup_requires_verified_inactive_and_preserves_failure(tmp_path, monkeypatch):
    stops = cleanup_mocks(monkeypatch, active=True)
    with pytest.raises(RuntimeError, match="not verified inactive"):
        suite.cleanup_owned_services([{"sequence": 0, "socket": "/run/crate-sv-00.sock"}], tmp_path)
    assert stops == [["systemctl", "stop", "crate-sv-00.service"]]
    assert not json.loads((tmp_path / "cleanup.json").read_text())["success"]


def test_cleanup_success_exact_owned_service(tmp_path, monkeypatch):
    stops = cleanup_mocks(monkeypatch)
    suite.cleanup_owned_services([{"sequence": 0, "socket": "/run/crate-sv-00.sock"}], tmp_path)
    cleanup = json.loads((tmp_path / "cleanup.json").read_text())
    assert cleanup["success"] and cleanup["verified_inactive"] == ["crate-sv-00.service"]
    assert len(stops) == 1
