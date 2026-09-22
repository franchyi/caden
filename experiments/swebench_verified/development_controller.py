#!/usr/bin/env python3
"""Monitor preparation/capture, qualify all environments, then inspect 8 tasks.

No automatic acceptance of a candidate policy and no automatic formal campaign:
the writing/research agent must review the measured checkpoints before freezing.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

if not __package__:
    sys.dont_write_bytecode = True
if __package__:
    from .continue_campaign import REMOTE, ssh, run
    from .finish_capture32 import read_selection, validate_capture
else:
    from continue_campaign import REMOTE, ssh, run
    from finish_capture32 import read_selection, validate_capture

ACTIVE_STATES = {"active", "activating", "reloading", "deactivating"}
DOCKER_ARGV = ["/usr/bin/dockerd", "--config-file", REMOTE + "/scripts/docker.json",
    "--data-root", REMOTE + "/docker", "--exec-root", "/run/crate-sv-docker",
    "--pidfile", "/run/crate-sv-docker.pid", "--host", "unix:///run/crate-sv-docker.sock",
    "--bridge", "none", "--iptables=false", "--ip-forward=false", "--ip-masq=false",
    "--containerd-namespace", "crate-sv", "--containerd-plugins-namespace", "crate-sv-plugins"]


def parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", type=Path, required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--pool-strategy", choices=["legacy", "queue", "ablation"], default="ablation")
    ap.add_argument("--development-profile", choices=["full", "candidate"], default="full")
    ap.add_argument("--restore-mode", choices=["prewarm", "thaw-only"], default="prewarm")
    ap.add_argument("--scheduler-wakes", type=int, choices=[2, 8], default=2)
    ap.add_argument("--first-touch", action="store_true")
    ap.add_argument("--cold-repetitions", type=int, choices=[1, 3], default=3)
    ap.add_argument("--capture-limit", type=int, choices=[24, 32], default=24)
    ap.add_argument("--wait-receipt", type=Path,
                    help="Local JSON prerequisite; wait for success:true before remote deployment/work")
    ap.add_argument("--deploy-source", action="store_true",
                    help="Deploy this locally verified frozen source into a fresh remote directory after gates")
    return ap


def validate_options(a):
    for value in (a.source, a.campaign):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
            raise ValueError("unsafe artifact name")
    if a.source == a.campaign:
        raise ValueError("source and campaign directories must differ")
    if a.development_profile == "candidate" and a.pool_strategy == "ablation":
        raise ValueError("candidate profile requires a selected pool strategy")
    if a.capture_limit == 32 and a.wait_receipt is None:
        raise ValueError("capture-limit32 requires --wait-receipt from the finish-capture32 worker")


def receipt_ready(path):
    if path is None:
        return True
    path = Path(path)
    if path.is_symlink():
        raise ValueError("refusing prerequisite receipt symlink")
    if not path.exists():
        return False
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("invalid prerequisite receipt")
    if value.get("success") is True:
        return True
    if value.get("success") is False or str(value.get("stage", "")).startswith("failed"):
        raise ValueError("prerequisite receipt reports failure; review retained results")
    if "success" in value:
        raise ValueError("prerequisite success must be a boolean")
    # A durable worker publishes progress before its final success receipt.
    return False


def verify_local_source(source):
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("frozen source must be an ordinary directory")
    files = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"frozen source symlink: {path}")
        if "__pycache__" in path.relative_to(source).parts:
            continue
        if path.is_file():
            files[str(path.relative_to(source))] = hashlib.sha256(path.read_bytes()).hexdigest()
    provenance = json.loads((source / "SOURCE_PROVENANCE.json").read_text())
    hashes = provenance["source_sha256"]
    expected_manifest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    if not hashes or expected_manifest != provenance["source_manifest_sha256"]:
        raise ValueError("invalid frozen source manifest hash")
    for name, expected in hashes.items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or files.get(name) != expected:
            raise ValueError(f"frozen source drift or unsafe path: {name}")
    if set(files) - set(hashes) - {"SOURCE_PROVENANCE.json", "tracked-changes.patch"}:
        raise ValueError("unmanifested files in frozen source; refuse to deploy executable drift")
    return {"source_manifest_sha256": expected_manifest, "files": files}


def captures_ready(package, selection, limit):
    batch_path = package / "captures-v2" / f"batch-{limit - 8}-{limit}.json"
    batch_exists = batch_path.exists()
    missing = []
    for task in selection["tasks"][:limit]:
        capture = package / "captures-v2" / task["instance_id"] / "capture.json"
        if not capture.exists():
            missing.append(task["instance_id"])
            continue
        try:
            validate_capture(capture, task, selection["revision"])
        except json.JSONDecodeError:
            if batch_exists:
                raise ValueError("completed capture batch contains malformed capture")
            missing.append(task["instance_id"])
    if not batch_exists:
        return False, missing
    if batch_path.is_symlink():
        raise ValueError("refusing capture batch symlink")
    batch = json.loads(batch_path.read_text())
    expected = {task["instance_id"] for task in selection["tasks"][limit - 8:limit]}
    if (not isinstance(batch, list) or len(batch) != 8 or
            {row["instance_id"] for row in batch} != expected or any(row["returncode"] != 0 for row in batch)):
        raise ValueError("capture batch did not complete the exact selected tasks cleanly")
    if missing:
        raise ValueError("completed capture batch is missing selected captures")
    return True, []


def read_unit(unit):
    command = ["systemctl", "show", unit, "-p", "LoadState", "-p", "ActiveState",
               "-p", "SubState", "-p", "Result", "-p", "ExecMainStatus"]
    result = ssh(shlex.join(command), check=False)
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode and state.get("LoadState") != "not-found":
        raise RuntimeError(f"cannot inspect {unit}: {result.stderr}")
    if state.get("LoadState") not in {"loaded", "not-found"} or "ActiveState" not in state:
        raise RuntimeError(f"unrecognized service state: {unit}: {state}")
    return state


def exec_argv(unit):
    text = ssh(shlex.join(["systemctl", "show", unit, "--property=ExecStart", "--value"])).stdout.strip()
    match = re.fullmatch(r"\{\s*path=([^\s;]+)\s*;\s*argv\[\]=(.*?)\s*;[^{}]*\}", text, flags=re.DOTALL)
    if text.count("argv[]=") != 1 or not match:
        raise RuntimeError(f"unrecognized service command: {unit}")
    argv = shlex.split(match.group(2))
    if not argv or argv[0] != match.group(1):
        raise RuntimeError(f"unrecognized service executable: {unit}")
    return argv


def require_remote_quiet():
    """Read-only, campaign-scoped checks; this is still a shared measurement host."""
    previous = read_unit("crate-sv-development-q1.service")
    if previous["ActiveState"] in ACTIVE_STATES:
        raise RuntimeError("previous development-q1 measurement is still active")
    if previous["ActiveState"] not in {"inactive"}:
        raise RuntimeError("previous development-q1 requires failure review")
    command = ["systemctl", "list-units", "--all", "--no-legend", "--no-pager", "--plain",
               "--state=active,activating,reloading,deactivating", "crate-sv-*"]
    rows = ssh(shlex.join(command)).stdout
    units = []
    for line in rows.splitlines():
        if not line.strip():
            continue
        fields = line.split(None, 4)
        if len(fields) < 4:
            raise RuntimeError(f"unrecognized service listing: {line}")
        name, loaded, active, sub = fields[:4]
        if loaded != "loaded" or active != "active" or sub != "running":
            raise RuntimeError(f"active project work overlaps campaign: {line}")
        if name == "crate-sv-docker.service":
            argv = exec_argv(name)
            if argv != DOCKER_ARGV:
                raise RuntimeError("unrecognized isolated Docker daemon")
            result = ssh(shlex.join(["sudo", "-n", "docker", "--host", "unix:///run/crate-sv-docker.sock", "ps", "-q"]))
            if result.stdout.strip() or result.stderr.strip():
                raise RuntimeError("isolated Docker daemon is busy or inspection incomplete")
        else:
            match = re.fullmatch(r"crate-sv-(\d{2})\.service", name)
            if not match or int(match.group(1)) >= 32:
                raise RuntimeError(f"active project campaign overlaps: {name}")
            index = match.group(1)
            argv = exec_argv(name)
            if argv[0] not in {"/bin/bash", "/usr/bin/bash"} or argv[1:] != [REMOTE + "/scripts/launch-daemon.sh", index]:
                raise RuntimeError(f"unrecognized task daemon: {name}")
            result = ssh(shlex.join(["sudo", "-n", REMOTE + "/bin/sandboxfsctl", "--socket", f"/run/crate-sv-{index}.sock", "list"]))
            if json.loads(result.stdout).get("sandboxes") != []:
                raise RuntimeError(f"task daemon still has active sandboxes: {name}")
        units.append(name)
    return {"previous_q1": previous, "idle_project_units": units,
            "limitation": "read-only snapshot, not host isolation or an inter-controller lock"}


REMOTE_VERIFY_SOURCE = r'''
import hashlib, json, sys
from pathlib import Path
root, expected = Path(sys.argv[1]), json.loads(sys.argv[2])
if root.is_symlink() or not root.is_dir(): raise ValueError("unsafe remote source directory")
actual = {}
for path in sorted(root.rglob("*")):
    if path.is_symlink(): raise ValueError("remote source symlink")
    if "__pycache__" in path.relative_to(root).parts: continue
    if path.is_file(): actual[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
if actual != expected: raise ValueError("remote source differs from frozen local artifact")
print(json.dumps({"success": True, "verified_files": len(actual)}))
'''


def verify_remote_source(remote_source, identity):
    command = ["python3", "-c", REMOTE_VERIFY_SOURCE, remote_source, json.dumps(identity["files"], sort_keys=True)]
    result = json.loads(ssh(shlex.join(command)).stdout)
    if result != {"success": True, "verified_files": len(identity["files"])}:
        raise RuntimeError("unexpected remote source verification result")


def require_remote_fresh(path):
    result = ssh("test ! -e " + shlex.quote(path) + " && test ! -L " + shlex.quote(path), check=False)
    if result.returncode:
        raise FileExistsError(f"refusing existing remote artifact: {path}")


def suite_argv(a):
    command = ["/usr/bin/python3", REMOTE + "/" + a.source + "/experiments/swebench_verified/run_suite.py",
               "--kind", "development", "--pool-strategy", a.pool_strategy,
               "--development-profile", a.development_profile, "--restore-mode", a.restore_mode,
               "--scheduler-wakes", str(a.scheduler_wakes), "--output", REMOTE + "/" + a.campaign]
    if a.first_touch:
        command.append("--first-touch")
    command += ["--cold-repetitions", str(a.cold_repetitions)]
    return command


def analysis_argv(a, package):
    return [sys.executable, Path(__file__).with_name("analyze.py"), "--campaign", package / a.campaign,
            "--output", package / (a.campaign + "-analysis"),
            "--workloads-dir", package / "normalized-development", "--source-root", package / a.source]


def run_controller(a, argv):
    validate_options(a)
    package = a.package.resolve()
    for name in (a.campaign, a.campaign + "-analysis", a.campaign + "-status.json", a.campaign + "-status.tmp"):
        path = package / name
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"refusing existing local campaign evidence: {path}")
    receipt = package / (a.campaign + "-controller.json")
    with receipt.open("x") as output:
        json.dump({"pid": os.getpid(), "started_unix": time.time(), "argv": argv}, output, indent=2)
    status_path = package / (a.campaign + "-status.json")
    def status(stage, **fields):
        value = {"stage": stage, "observed_unix": time.time(), **fields}
        temporary = status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n")
        temporary.replace(status_path)
        print(json.dumps(value), flush=True)
    def wait(unit, marker, stage):
        while True:
            state = read_unit(unit)
            progress = ssh("find " + shlex.quote(REMOTE + "/" + a.campaign) +
                           " -maxdepth 1 -name '*.progress.jsonl' -exec wc -l {} + 2>/dev/null", check=False).stdout.strip()
            status(stage, unit=unit, service=state, progress_lines=progress)
            marker_ready = ssh("test -f " + shlex.quote(marker) + " && test ! -L " + shlex.quote(marker), check=False).returncode == 0
            if marker_ready and state.get("ActiveState") == "inactive" and state.get("Result") == "success" and state.get("ExecMainStatus") == "0":
                return
            if state.get("ActiveState") not in ACTIVE_STATES:
                raise RuntimeError(f"{unit} stopped without completion marker: {state}")
            time.sleep(30)
    def launch(unit, argv, log):
        command = ["sudo", "-n", "systemd-run", "--unit=" + unit, "--property=Type=exec",
                   "--property=AllowedCPUs=0-7", "--property=StandardOutput=append:" + log,
                   "--property=StandardError=append:" + log, *argv]
        print(ssh(shlex.join(command)).stdout, flush=True)
    try:
        source = package / a.source
        identity = verify_local_source(source)
        status("waiting_prerequisites", source_manifest_sha256=identity["source_manifest_sha256"])
        while not receipt_ready(a.wait_receipt):
            status("waiting_receipt", receipt=str(a.wait_receipt))
            time.sleep(30)
        wait("crate-sv-prepare32.service", REMOTE + "/artifacts/PREPARE_32_COMPLETE.json", "waiting_preparation")
        selection = read_selection(package)
        while True:
            ready, missing = captures_ready(package, selection, a.capture_limit)
            if ready:
                break
            if a.capture_limit == 32:
                raise RuntimeError("successful prerequisite receipt lacks complete 32-task capture artifacts")
            status("waiting_capture_" + str(a.capture_limit), missing=missing)
            time.sleep(30)
        if not receipt_ready(a.wait_receipt) or verify_local_source(source) != identity:
            raise RuntimeError("prerequisite receipt or frozen source changed while waiting")
        status("gates_ready", quiet=require_remote_quiet(), capture_count=a.capture_limit)
        require_remote_fresh(REMOTE + "/" + a.campaign)
        if a.deploy_source:
            remote_source = REMOTE + "/" + a.source
            require_remote_fresh(remote_source)
            # mkdir is an exclusive claim, including dangling symlinks; failures
            # leave partial evidence for review rather than overwriting/retrying.
            ssh(shlex.join(["mkdir", "--", remote_source]))
            run(["rsync", "-a", "--protect-args", "--timeout=30", "--exclude=__pycache__/",
                 "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=1",
                 str(source) + "/", "nsl17:" + remote_source + "/"])
            if verify_local_source(source) != identity:
                raise RuntimeError("local frozen source changed during deployment")
        verify_remote_source(REMOTE + "/" + a.source, identity)
        isolation = REMOTE + "/artifacts/isolation-" + a.campaign
        require_remote_fresh(isolation)
        status("before_isolation", quiet=require_remote_quiet())
        unit = "crate-sv-isolation-" + a.campaign + ".service"
        launch(unit, ["/usr/bin/python3", REMOTE + "/" + a.source +
                      "/experiments/swebench_verified/verify_isolation.py", "--limit", "32",
                      "--output-dir", isolation], REMOTE + "/logs/isolation-" + a.campaign + ".log")
        wait(unit, isolation + "/ISOLATION_32_COMPLETE.json", "isolation")
        if not receipt_ready(a.wait_receipt) or verify_local_source(source) != identity:
            raise RuntimeError("receipt/source changed before measurement")
        if not captures_ready(package, selection, a.capture_limit)[0]:
            raise RuntimeError("capture gate changed before measurement")
        status("before_measurement", quiet=require_remote_quiet())
        unit = "crate-sv-" + a.campaign + ".service"
        launch(unit, suite_argv(a),
               REMOTE + "/logs/" + a.campaign + ".log")
        wait(unit, REMOTE + "/" + a.campaign + "/COMPLETED.json", "development_replay")
        run(["rsync", "-a", "nsl17:" + REMOTE + "/" + a.campaign + "/", str(package / a.campaign) + "/"])
        run(analysis_argv(a, package))
        status("development_review_required", success=True, formal_started=False,
               note="No formal campaign has been launched; inspect all paired checkpoints and choose/freeze a revision.")
    except BaseException as error:
        status("failed_review_required", success=False, error=f"{type(error).__name__}: {error}")
        raise


def main():
    a = parser().parse_args()
    run_controller(a, sys.argv)


if __name__ == "__main__":
    main()
