#!/usr/bin/env python3
"""Finish the fixed last eight captures after measurement; never launch formal.

Run as a durable local process. A persistent exclusive receipt intentionally
prevents automatic retries: a failed attempt must be reviewed, not overwritten.
Only the existing capture API and two scoped workload rsyncs can mutate remote
state. There are no host, mount, CXL, swap, or service-management writes.
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

ROOT = Path(__file__).resolve().parents[2]
REMOTE = "/sandboxfs/crate-swebench-20260919"
STEM = "finish-capture32"
PROTOCOL = {"capture_protocol_version": 2, "command_deadline_seconds": 180,
            "provider": "openai-codex", "model": "gpt-5.6-terra", "thinking": "high"}


def digest(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected ordinary evidence file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"expected ordinary evidence directory: {directory}")
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"refusing evidence symlink: {path}")
        if path.is_file():
            result[str(path.relative_to(directory))] = digest(path)
    return result


def source_hashes():
    files = [Path(__file__), *[Path(__file__).with_name(name) for name in
             ("capture.py", "capture_batch.py", "convert.py")],
             *sorted((ROOT / "experiments/agent_pipeline/agent_pipeline").glob("*.py"))]
    return {str(path.relative_to(ROOT)): digest(path) for path in files}


def read_selection(package):
    for name in ("selection", "selection/tasks", "captures-v2", "captures-v2/logs"):
        path = package / name
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"expected ordinary package directory: {path}")
    selection = json.loads((package / "selection/manifest.json").read_text())
    tasks = selection["tasks"]
    if len(tasks) != 32 or [task["sequence"] for task in tasks] != list(range(32)):
        raise ValueError("requires the fixed ordered 32-task selection")
    ids = [task["instance_id"] for task in tasks]
    if len(set(ids)) != 32 or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", x) for x in ids):
        raise ValueError("duplicate or unsafe task identity")
    return selection


def validate_capture(path, task, revision):
    digest(path)
    capture = json.loads(path.read_text())
    if capture.get("schema") != "crate-verified-real-capture-v1" or capture.get("task") != task:
        raise ValueError(f"capture does not match selected task: {task['instance_id']}")
    if capture.get("error") or capture.get("cleanup_error"):
        raise ValueError(f"capture/cleanup error retained: {task['instance_id']}")
    if any(capture.get(key) != value for key, value in PROTOCOL.items()):
        raise ValueError("capture protocol/model changed")
    if capture.get("dataset_revision") != revision:
        raise ValueError("capture dataset revision changed")
    if capture.get("fingerprint", {}).get("exit_code") != 0:
        raise ValueError("missing successful final workspace fingerprint")
    commands = capture.get("commands", [])
    if not commands or [c["sequence"] for c in commands] != list(range(len(commands))):
        raise ValueError("empty or discontinuous command capture")
    if capture.get("agent", {}).get("exit_status") == "infrastructure_error":
        raise ValueError("infrastructure failure is not an eligible capture")
    # Validation only: do not filter step-limit, wall-limit, or nonzero commands.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from experiments.swebench_verified.convert import convert
    convert(capture)
    return capture


def validate_captures(package, selection, limit):
    for task in selection["tasks"][:limit]:
        validate_capture(package / "captures-v2" / task["instance_id"] / "capture.json",
                         task, selection["revision"])


def immutable_snapshot(package, selection):
    files = {"selection/manifest.json": digest(package / "selection/manifest.json")}
    for task in selection["tasks"]:
        issue = "selection/tasks/" + task["instance_id"] + "/task.txt"
        files[issue] = digest(package / issue)
    for task in selection["tasks"][:24]:
        prefix = "captures-v2/" + task["instance_id"]
        files.update({prefix + "/" + key: value for key, value in tree_hashes(package / prefix).items()})
        log = "captures-v2/logs/" + task["instance_id"] + ".log"
        files[log] = digest(package / log)
    return files


def require_unchanged(package, selection, evidence, sources):
    if immutable_snapshot(package, selection) != evidence:
        raise ValueError("selection or first 24 immutable captures/logs changed")
    if source_hashes() != sources:
        raise ValueError("capture/controller/conversion source changed during wait or capture")


def require_fresh_outputs(package, selection):
    paths = [package / "normalized-formal", package / "captures-v2/batch-24-32.json",
             package / (STEM + "-batch.log")]
    for task in selection["tasks"][24:]:
        paths += [package / "captures-v2" / task["instance_id"],
                  package / "captures-v2/logs" / (task["instance_id"] + ".log")]
    for path in paths:
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"refusing existing output; preserve/review it: {path}")


def ssh(command, *, input=None):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=1", "nsl17", command],
        input=input, text=True, capture_output=True, timeout=30, check=True)


REMOTE_PROBE = r'''
import hashlib, json, subprocess, sys
from pathlib import Path
root, campaign, unit = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
p = subprocess.run(["systemctl", "show", unit, "-p", "LoadState", "-p", "ActiveState",
    "-p", "SubState", "-p", "Result", "-p", "ExecMainStatus", "-p", "ExecStart"], text=True,
    capture_output=True, check=False, timeout=15)
state = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
if p.returncode and state.get("LoadState") != "not-found":
    raise RuntimeError("systemctl show failed: " + p.stderr[:1000])
marker = root / campaign / "COMPLETED.json"
if marker.is_symlink(): raise RuntimeError("symlink completion marker")
completed = json.loads(marker.read_text()) if marker.exists() else None
plan_path = root / campaign / "PLAN.json"
source_root = root / ("source-" + campaign)
provenance_path = source_root / "SOURCE_PROVENANCE.json"
identity = None
if plan_path.exists() and provenance_path.exists():
    if plan_path.is_symlink() or provenance_path.is_symlink() or source_root.is_symlink():
        raise RuntimeError("symlink campaign identity")
    plan_bytes = plan_path.read_bytes()
    plan = json.loads(plan_bytes)
    if plan.get("kind") not in {"pilot", "development", "formal"} or not plan.get("orders"):
        raise RuntimeError("invalid measurement PLAN")
    provenance = json.loads(provenance_path.read_text())
    hashes = provenance["source_sha256"]
    manifest_hash = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    if not hashes or manifest_hash != provenance["source_manifest_sha256"]:
        raise RuntimeError("invalid frozen source manifest")
    for name, expected in hashes.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("unsafe frozen source path")
        path = source_root / relative
        if any(parent.is_symlink() for parent in [path, *list(path.parents)[:len(relative.parts)-1]]):
            raise RuntimeError("symlink frozen source path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("frozen source changed: " + name)
    identity = {"plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
                "source_manifest_sha256": manifest_hash, "source_root": str(source_root),
                "source_verified": True}
paths = {name: {"exists": (root / name).exists() or (root / name).is_symlink(),
                "symlink": (root / name).is_symlink()}
         for name in ("normalized-formal", "captures-v2")}
print(json.dumps({"unit": state, "completed": completed, "identity": identity, "paths": paths}))
'''


def remote_snapshot(campaign, unit):
    return json.loads(ssh(shlex.join(["python3", "-c", REMOTE_PROBE, REMOTE, campaign, unit])).stdout)


def remote_gate(snapshot):
    state, completed = snapshot["unit"], snapshot["completed"]
    if state.get("LoadState") not in {"loaded", "not-found"}:
        raise RuntimeError("measurement unit has unexpected load state")
    active = state.get("ActiveState")
    if active in {"active", "activating", "deactivating", "reloading"}:
        return False
    if active != "inactive" or state.get("Result") != "success" or state.get("ExecMainStatus") != "0":
        raise RuntimeError(f"measurement unit did not finish successfully: {state}")
    if not isinstance(completed, dict) or not isinstance(completed.get("finished_unix"), (int, float)):
        raise RuntimeError("inactive measurement unit lacks a valid completion marker")
    if completed.get("success", True) is not True or completed["finished_unix"] <= 0:
        raise RuntimeError("measurement completion marker reports failure")
    identity = snapshot.get("identity")
    if (not isinstance(identity, dict) or identity.get("source_verified") is not True or
            not all(re.fullmatch(r"[0-9a-f]{64}", identity.get(key, "")) for key in
                    ("plan_sha256", "source_manifest_sha256"))):
        raise RuntimeError("marker/unit state lacks verified campaign PLAN and frozen source identity")
    return True


def local_gate(marker):
    if marker is None:
        return True
    if marker.is_symlink():
        raise ValueError("refusing local receipt symlink")
    if not marker.exists():
        return False
    receipt = json.loads(marker.read_text())
    if not isinstance(receipt, dict) or receipt.get("success") is not True:
        raise ValueError("local qualification receipt must contain success: true")
    return True


def require_remote_fresh(snapshot):
    paths = snapshot["paths"]
    if paths["normalized-formal"]["exists"]:
        raise FileExistsError("remote normalized-formal already exists; refusing overwrite")
    if paths["captures-v2"]["symlink"]:
        raise ValueError("refusing remote captures-v2 symlink")


def write_json(path, value, *, exclusive=False):
    target = path if exclusive else path.with_suffix(path.suffix + ".tmp")
    with target.open("x" if exclusive else "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if not exclusive:
        target.replace(path)


def validate_batch(package, selection):
    rows = json.loads((package / "captures-v2/batch-24-32.json").read_text())
    expected = {task["instance_id"] for task in selection["tasks"][24:]}
    if (len(rows) != 8 or {row["instance_id"] for row in rows} != expected or
            any(row["returncode"] != 0 for row in rows)):
        raise ValueError("last-eight batch incomplete or failed; retain all evidence")


def validate_normalized(package, selection):
    normalized = package / "normalized-formal"
    manifest = json.loads((normalized / "manifest.json").read_text())
    if (manifest["source"] != {"dataset": selection["dataset"], "revision": selection["revision"]}
            or manifest["conversion"] != {"kind": "original-commands", "wait_scale": 1.0, "success_filter": False}):
        raise ValueError("normalized dataset, waits, or selection semantics changed")
    rows = manifest["workloads"]
    if [row["path"] for row in rows] != [t["instance_id"] + ".json" for t in selection["tasks"]]:
        raise ValueError("normalized task population/order changed")
    for task, row in zip(selection["tasks"], rows):
        if digest(normalized / row["path"]) != row["sha256"] or digest(
                package / "captures-v2" / task["instance_id"] / "capture.json") != row["capture_sha256"]:
            raise ValueError("normalized workload or capture hash mismatch")


REMOTE_VERIFY = r'''
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
expected = json.load(sys.stdin)
for name, sha in expected.items():
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[0] not in {"normalized-formal", "captures-v2"}:
        raise RuntimeError("invalid workload verification path")
    path = root / relative
    if any(parent.is_symlink() for parent in [path, *list(path.parents)[:len(relative.parts)-1]]):
        raise RuntimeError("workload symlink")
    if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        raise RuntimeError("remote workload differs: " + name)
print(json.dumps({"success": True, "verified_files": len(expected)}))
'''


def run_controller(args, argv):
    package = args.package.resolve(strict=True)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.after_campaign):
        raise ValueError("unsafe campaign name")
    if not re.fullmatch(r"crate-sv-[a-z0-9][a-z0-9-]*\.service", args.after_unit):
        raise ValueError("unsafe or unowned service name")
    if args.after_unit != "crate-sv-" + args.after_campaign + ".service":
        raise ValueError("campaign marker and measurement unit must identify the same campaign")
    receipt = {"pid": os.getpid(), "started_unix": time.time(), "argv": argv,
               "package": str(package), "source_root": str(ROOT), "after_campaign": args.after_campaign,
               "after_unit": args.after_unit, "wait_marker": str(args.wait_marker) if args.wait_marker else None}
    write_json(package / (STEM + "-controller.json"), receipt, exclusive=True)
    def status(stage, **fields):
        value = {**receipt, "stage": stage, "observed_unix": time.time(), **fields}
        write_json(package / (STEM + "-status.json"), value)
        print(json.dumps({key: value[key] for key in
            ("stage", "pid", "observed_unix", "success", "error", "capture_count") if key in value}), flush=True)
    try:
        selection = read_selection(package)
        validate_captures(package, selection, 24)
        require_fresh_outputs(package, selection)
        evidence, sources = immutable_snapshot(package, selection), source_hashes()
        receipt.update({"immutable_sha256": evidence, "capture_source_sha256": sources})
        # Keep the original exclusive receipt immutable; hashes accompany status.
        prerequisite_identity = None
        while True:
            snapshot = remote_snapshot(args.after_campaign, args.after_unit)
            ready = remote_gate(snapshot)
            identity = snapshot.get("identity")
            if prerequisite_identity is None and identity:
                prerequisite_identity = identity
                receipt["prerequisite_identity"] = identity
            elif identity and identity != prerequisite_identity:
                raise ValueError("prerequisite campaign/source identity changed while waiting")
            qualified = local_gate(args.wait_marker)
            status("waiting_gates", remote=snapshot, local_qualified=qualified)
            if ready and qualified:
                break
            time.sleep(30)
        require_unchanged(package, selection, evidence, sources)
        require_fresh_outputs(package, selection)
        require_remote_fresh(snapshot)
        status("capture_running", captured_before=24, capture_start=24, capture_limit=32)
        command = [sys.executable, str(Path(__file__).with_name("capture_batch.py")),
                   "--selection", str(package / "selection"), "--output", str(package / "captures-v2"),
                   "--start", "24", "--limit", "32"]
        with (package / (STEM + "-batch.log")).open("x") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        require_unchanged(package, selection, evidence, sources)
        validate_batch(package, selection)
        validate_captures(package, selection, 32)
        status("normalizing", capture_count=32)
        subprocess.run([sys.executable, str(Path(__file__).with_name("convert.py")),
            "--captures", str(package / "captures-v2"), "--selection", str(package / "selection/manifest.json"),
            "--output", str(package / "normalized-formal"), "--limit", "32"], check=True)
        validate_normalized(package, selection)
        require_unchanged(package, selection, evidence, sources)
        snapshot = remote_snapshot(args.after_campaign, args.after_unit)
        if not remote_gate(snapshot) or not local_gate(args.wait_marker):
            raise RuntimeError("measurement/qualification gate changed before synchronization")
        if snapshot.get("identity") != prerequisite_identity:
            raise ValueError("prerequisite campaign/source identity changed before synchronization")
        require_remote_fresh(snapshot)
        expected = {}
        for name in ("captures-v2", "normalized-formal"):
            expected.update({name + "/" + key: value for key, value in tree_hashes(package / name).items()})
        status("syncing_workloads", sync_sha256=expected)
        for name in ("captures-v2", "normalized-formal"):
            subprocess.run(["rsync", "-a", "--ignore-existing", "--protect-args", "--timeout=30",
                "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=1",
                str(package / name) + "/", "nsl17:" + REMOTE + "/" + name + "/"], check=True)
        verified = json.loads(ssh(shlex.join(["python3", "-c", REMOTE_VERIFY, REMOTE]),
                                  input=json.dumps(expected)).stdout)
        if verified != {"success": True, "verified_files": len(expected)}:
            raise RuntimeError("unexpected remote verification receipt")
        status("captures_32_ready_for_review", success=True, capture_count=32, sync_sha256=expected,
               remote_verified=verified, formal_started=False,
               note="No formal measurement, CXL operation, or host configuration change was launched.")
    except BaseException as error:
        status("failed_review_required", success=False, error=f"{type(error).__name__}: {error}",
               note="All captures, logs and partial outputs retained. No automatic retry.")
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--after-campaign", default="development-q1")
    parser.add_argument("--after-unit", default="crate-sv-development-q1.service")
    parser.add_argument("--wait-marker", type=Path,
                        help="Optional local JSON qualification receipt; require success: true")
    args = parser.parse_args()
    if args.wait_marker:
        args.wait_marker = args.wait_marker.absolute()
    run_controller(args, sys.argv)


if __name__ == "__main__":
    main()
