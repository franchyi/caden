"""Pure/mocked controller checks: tests must never contact nsl17 or launch work."""
import hashlib
import json
import shlex
import subprocess
import sys

import pytest

from experiments.swebench_verified import development_controller as controller
from tests.test_finish_capture32 import package, put_capture  # noqa: F401


def options(package, *extra):
    return controller.parser().parse_args([
        "--package", str(package), "--source", "source-development-q2",
        "--campaign", "development-q2", *extra])


def make_source(package):
    root = package / "source-development-q2"
    root.mkdir()
    (root / "runner.py").write_text("# frozen source fixture\n")
    hashes = {"runner.py": hashlib.sha256((root / "runner.py").read_bytes()).hexdigest()}
    provenance = {"source_sha256": hashes,
                  "source_manifest_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}
    (root / "SOURCE_PROVENANCE.json").write_text(json.dumps(provenance))
    (root / "tracked-changes.patch").write_text("# preserved changes\n")
    return root


def finish_captures(package, limit):
    selection = controller.read_selection(package)
    for task in selection["tasks"][24:limit]:
        put_capture(package, task)
    rows = [{"instance_id": task["instance_id"], "returncode": 0}
            for task in selection["tasks"][limit-8:limit]]
    (package / "captures-v2" / f"batch-{limit-8}-{limit}.json").write_text(json.dumps(rows))
    return selection


def complete(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def remote_mocks(monkeypatch):
    events = []
    def ssh(command, check=True):
        tokens = shlex.split(command)
        events.append(("ssh", tokens))
        if tokens[:2] == ["python3", "-c"]:
            return complete(json.dumps({"success": True, "verified_files": len(json.loads(tokens[-1]))}))
        return complete()
    monkeypatch.setattr(controller, "ssh", ssh)
    monkeypatch.setattr(controller, "run", lambda argv: events.append(("run", list(map(str, argv)))))
    monkeypatch.setattr(controller, "read_unit", lambda unit: {
        "LoadState": "loaded", "ActiveState": "inactive", "Result": "success", "ExecMainStatus": "0"})
    def quiet():
        events.append(("quiet", []))
        return {"idle_project_units": []}
    monkeypatch.setattr(controller, "require_remote_quiet", quiet)
    monkeypatch.setattr(controller.time, "sleep", lambda _: (_ for _ in ()).throw(RuntimeError("test stopped waiting")))
    return events


def test_defaults_and_candidate_command_construction(package):
    default = options(package)
    assert (default.pool_strategy, default.development_profile, default.restore_mode,
            default.scheduler_wakes, default.capture_limit, default.deploy_source) == (
                "ablation", "full", "prewarm", 2, 24, False)
    candidate = options(package, "--pool-strategy", "queue", "--development-profile", "candidate",
                        "--restore-mode", "thaw-only", "--scheduler-wakes", "8", "--first-touch",
                        "--capture-limit", "32", "--wait-receipt", str(package / "receipt.json"), "--deploy-source")
    controller.validate_options(candidate)
    command = controller.suite_argv(candidate)
    assert command[command.index("--kind") + 1] == "development"
    assert command[command.index("--pool-strategy") + 1] == "queue"
    assert command[command.index("--development-profile") + 1] == "candidate"
    assert command[command.index("--restore-mode") + 1] == "thaw-only"
    assert command[command.index("--scheduler-wakes") + 1] == "8"
    assert "--first-touch" in command and "formal" not in command
    analysis = list(map(str, controller.analysis_argv(candidate, package)))
    assert analysis[analysis.index("--workloads-dir") + 1] == str(package / "normalized-development")
    assert analysis[analysis.index("--source-root") + 1] == str(package / candidate.source)


def test_missing_receipt_option_and_incompatible_profile_are_rejected(package):
    with pytest.raises(ValueError, match="requires --wait-receipt"):
        controller.validate_options(options(package, "--capture-limit", "32"))
    with pytest.raises(ValueError, match="selected pool strategy"):
        controller.validate_options(options(package, "--development-profile", "candidate"))
    args = options(package)
    args.source = "../old"
    with pytest.raises(ValueError, match="unsafe"):
        controller.validate_options(args)


def test_receipt_waits_for_explicit_boolean_success_and_rejects_failure(tmp_path):
    receipt = tmp_path / "receipt.json"
    assert not controller.receipt_ready(receipt)
    receipt.write_text('{"stage":"capture_running"}')
    assert not controller.receipt_ready(receipt)
    receipt.write_text('{"success":true}')
    assert controller.receipt_ready(receipt)
    for value in ({"success": False}, {"success": 1}, {"success": "true"}, {"stage": "failed_review_required"}):
        receipt.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            controller.receipt_ready(receipt)


def test_source_manifest_and_drift_checks(package):
    root = make_source(package)
    identity = controller.verify_local_source(root)
    assert set(identity["files"]) == {"runner.py", "SOURCE_PROVENANCE.json", "tracked-changes.patch"}
    (root / "__pycache__").mkdir()
    (root / "__pycache__/runner.pyc").write_bytes(b"runtime cache not deployed")
    assert controller.verify_local_source(root) == identity
    (root / "runner.py").write_text("# edited after freeze\n")
    with pytest.raises(ValueError, match="source drift"):
        controller.verify_local_source(root)


def test_source_rejects_unmanifested_files_and_symlinks(package):
    root = make_source(package)
    (root / "injected.py").write_text("unverified executable")
    with pytest.raises(ValueError, match="unmanifested"):
        controller.verify_local_source(root)
    (root / "injected.py").unlink()
    (root / "link.py").symlink_to(root / "runner.py")
    with pytest.raises(ValueError, match="symlink"):
        controller.verify_local_source(root)


def test_real_import_cache_cannot_break_local_or_remote_source_verification(package):
    source = make_source(package)
    identity = controller.verify_local_source(source)
    subprocess.run([sys.executable, "-c",
        "import sys; sys.dont_write_bytecode=False; sys.path.insert(0,sys.argv[1]); import runner",
        str(source)], check=True)
    assert (source / "__pycache__").is_dir()
    assert controller.verify_local_source(source) == identity
    # Execute the read-only remote verifier against a local fixture, never SSH.
    verified = subprocess.run([sys.executable, "-c", controller.REMOTE_VERIFY_SOURCE,
        str(source), json.dumps(identity["files"])], check=True, capture_output=True, text=True)
    assert json.loads(verified.stdout) == {"success": True, "verified_files": len(identity["files"])}


def test_exact_32_capture_and_batch_gate(package):
    selection = controller.read_selection(package)
    assert not controller.captures_ready(package, selection, 32)[0]
    finish_captures(package, 32)
    assert controller.captures_ready(package, selection, 32) == (True, [])
    path = package / "captures-v2/batch-24-32.json"
    rows = json.loads(path.read_text())
    rows[-1]["instance_id"] = rows[0]["instance_id"]
    path.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="exact selected"):
        controller.captures_ready(package, selection, 32)


def test_missing_receipt_blocks_all_remote_calls_and_uploads(package, monkeypatch):
    make_source(package)
    events = remote_mocks(monkeypatch)
    args = options(package, "--capture-limit", "32", "--wait-receipt", str(package / "missing.json"), "--deploy-source")
    with pytest.raises(RuntimeError, match="test stopped waiting"):
        controller.run_controller(args, ["controller-test"])
    assert events == []


def test_initial_source_drift_blocks_all_remote_calls(package, monkeypatch):
    source = make_source(package)
    (source / "runner.py").write_text("changed")
    events = remote_mocks(monkeypatch)
    with pytest.raises(ValueError, match="source drift"):
        controller.run_controller(options(package), ["controller-test"])
    assert events == []


def test_q2_deploys_only_after_gates_then_isolates_and_runs_candidate(package, monkeypatch):
    make_source(package)
    finish_captures(package, 32)
    receipt = package / "finish-capture32-status.json"
    receipt.write_text('{"success":true,"capture_count":32}')
    events = remote_mocks(monkeypatch)
    args = options(package, "--capture-limit", "32", "--wait-receipt", str(receipt), "--deploy-source",
                   "--pool-strategy", "queue", "--development-profile", "candidate",
                   "--restore-mode", "thaw-only", "--scheduler-wakes", "8", "--first-touch")
    controller.run_controller(args, ["controller-test"])
    upload = next(i for i, (kind, argv) in enumerate(events) if kind == "run" and argv[0] == "rsync" and argv[-1].startswith("nsl17:"))
    quiet = [i for i, (kind, _) in enumerate(events) if kind == "quiet"]
    assert len(quiet) == 3 and quiet[0] < upload < quiet[1] < quiet[2]
    launches = [(i, argv) for i, (kind, argv) in enumerate(events) if kind == "ssh" and "systemd-run" in argv]
    assert len(launches) == 2
    assert launches[0][0] > quiet[1] and "--unit=crate-sv-isolation-development-q2.service" in launches[0][1]
    assert launches[1][0] > quiet[2] and "--development-profile" in launches[1][1] and "candidate" in launches[1][1]
    assert "--kind" in launches[1][1] and "formal" not in launches[1][1]
    analysis = events[-1][1]
    assert "--workloads-dir" in analysis and "--source-root" in analysis
    status = json.loads((package / "development-q2-status.json").read_text())
    assert status["stage"] == "development_review_required" and status["formal_started"] is False


def test_default_controller_does_not_upload_source(package, monkeypatch):
    make_source(package)
    finish_captures(package, 24)
    events = remote_mocks(monkeypatch)
    controller.run_controller(options(package), ["controller-test"])
    assert not any(kind == "run" and argv[0] == "rsync" and argv[-1].startswith("nsl17:") for kind, argv in events)


def test_source_drift_while_waiting_cannot_deploy(package, monkeypatch):
    source = make_source(package)
    finish_captures(package, 24)
    events = remote_mocks(monkeypatch)
    def receipt(_):
        (source / "runner.py").write_text("source changed while waiting")
        return True
    monkeypatch.setattr(controller, "receipt_ready", receipt)
    with pytest.raises(ValueError, match="source drift"):
        controller.run_controller(options(package, "--deploy-source"), ["controller-test"])
    assert not any(kind == "run" or argv[:1] == ["mkdir"] for kind, argv in events)


def test_receipt_success_without_batch32_is_rejected(package, monkeypatch):
    make_source(package)
    receipt = package / "finish-capture32-status.json"
    receipt.write_text('{"success":true}')
    events = remote_mocks(monkeypatch)
    args = options(package, "--capture-limit", "32", "--wait-receipt", str(receipt), "--deploy-source")
    with pytest.raises(RuntimeError, match="complete 32-task capture"):
        controller.run_controller(args, ["controller-test"])
    assert not any(kind == "run" or argv[:1] == ["mkdir"] for kind, argv in events)


def test_quiet_gate_rejects_active_q1_before_other_queries(monkeypatch):
    monkeypatch.setattr(controller, "read_unit", lambda _: {"ActiveState": "active"})
    monkeypatch.setattr(controller, "ssh", lambda *_a, **_k: pytest.fail("unexpected extra query"))
    with pytest.raises(RuntimeError, match="q1 measurement is still active"):
        controller.require_remote_quiet()


def test_quiet_gate_rejects_unknown_campaign_and_nonempty_daemon(monkeypatch):
    monkeypatch.setattr(controller, "read_unit", lambda _: {"ActiveState": "inactive"})
    monkeypatch.setattr(controller, "ssh", lambda *_a, **_k: complete("crate-sv-formal.service loaded active running Experiment\n"))
    with pytest.raises(RuntimeError, match="campaign overlaps"):
        controller.require_remote_quiet()
    def ssh(command, check=True):
        if "list-units" in command:
            return complete("crate-sv-00.service loaded active running Task\n")
        return complete('{"sandboxes":[{"id":"active"}]}')
    monkeypatch.setattr(controller, "ssh", ssh)
    monkeypatch.setattr(controller, "exec_argv", lambda _: ["/bin/bash", controller.REMOTE + "/scripts/launch-daemon.sh", "00"])
    with pytest.raises(RuntimeError, match="active sandboxes"):
        controller.require_remote_quiet()


def test_quiet_gate_accepts_only_recognized_empty_services(monkeypatch):
    monkeypatch.setattr(controller, "read_unit", lambda _: {"ActiveState": "inactive"})
    def ssh(command, check=True):
        if "list-units" in command:
            return complete("crate-sv-docker.service loaded active running Docker\ncrate-sv-00.service loaded active running Task\n")
        if "sandboxfsctl" in command:
            return complete('{"sandboxes":[]}')
        return complete()
    monkeypatch.setattr(controller, "ssh", ssh)
    monkeypatch.setattr(controller, "exec_argv", lambda name: controller.DOCKER_ARGV if "docker" in name else
                        ["/bin/bash", controller.REMOTE + "/scripts/launch-daemon.sh", "00"])
    assert len(controller.require_remote_quiet()["idle_project_units"]) == 2


def test_existing_remote_source_is_never_overwritten(monkeypatch):
    monkeypatch.setattr(controller, "ssh", lambda *_a, **_k: complete(returncode=1))
    with pytest.raises(FileExistsError, match="existing remote artifact"):
        controller.require_remote_fresh(controller.REMOTE + "/source-existing")


def test_existing_local_campaign_is_never_overwritten(package, monkeypatch):
    events = remote_mocks(monkeypatch)
    (package / "development-q2").mkdir()
    with pytest.raises(FileExistsError, match="existing local campaign evidence"):
        controller.run_controller(options(package), ["controller-test"])
    assert events == []
