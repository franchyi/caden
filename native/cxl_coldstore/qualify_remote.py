#!/usr/bin/env python3
"""Local, one-shot controller: wait for development-q1, then qualify reserved DAX.

Run only after review. This is an explicitly hardware-writing controller, unlike
qualify.py's file-emulation default. It never starts captures/formal campaigns,
attaches kernel devices, changes swap, stops services, or kills other processes.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time

SOURCE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("coldstore_qualifier", SOURCE / "qualify.py")
q = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(q)
REMOTE = "/sandboxfs/crate-swebench-20260919"
GATE_UNIT = "crate-sv-development-q1.service"
GATE_MARKER = REMOTE + "/development-q1/COMPLETED.json"
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]


def safe_name(value):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", value):
        raise argparse.ArgumentTypeError("name must be 1-48 lowercase letters/digits/hyphens, beginning with a letter")
    return value


def parse_service(text):
    keys = {"LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus"}
    state = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in keys or key in state:
            raise q.QualificationError("unexpected or duplicate service property")
        state[key] = value
    # Successful transient units can be garbage-collected. A not-found unit
    # is never sufficient by itself: completion_ready still requires the exact
    # marker plus inactive/dead, Result=success and ExecMainStatus=0.
    if set(state) != keys or state["LoadState"] not in {"loaded", "not-found"}:
        raise q.QualificationError("gate service state is incompletely inspected")
    if not re.fullmatch(r"[0-9]+", state["ExecMainStatus"]):
        raise q.QualificationError("invalid gate service exit status")
    return state


def completion_ready(state, marker):
    active = state["ActiveState"]
    if active in {"active", "activating", "reloading", "deactivating"}:
        return False  # A marker alone never overrides a running process.
    if active != "inactive" or state["SubState"] != "dead":
        raise q.QualificationError(f"gate service did not terminate cleanly: {state}")
    if state["Result"] != "success" or state["ExecMainStatus"] != "0":
        raise q.QualificationError(f"development-q1 failed: {state}")
    if not isinstance(marker, dict) or marker.get("kind") != "development":
        raise q.QualificationError("inactive gate service has no valid development completion marker")
    stamp = marker.get("finished_unix")
    if type(stamp) not in {int, float} or not 0 < stamp <= time.time() + 300:
        raise q.QualificationError("invalid development completion timestamp")
    return True


def source_hashes(source):
    relatives = list(q.SOURCE_FILES)
    vendor = source / "third_party/lz4"
    if vendor.is_symlink() or not vendor.resolve().is_relative_to(source.resolve()):
        raise q.QualificationError("vendor path escapes source")
    for path in sorted(vendor.rglob("*")):
        if path.is_symlink():
            raise q.QualificationError("symlink in source vendor tree")
        if path.is_file():
            relatives.append(str(path.relative_to(source)))
    hashes = {}
    for relative in relatives:
        path = source / relative
        if path.is_symlink() or not path.is_file():
            raise q.QualificationError(f"missing or symlinked source: {relative}")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def verify_snapshot(source, expected):
    if source_hashes(source) != expected:
        raise q.QualificationError("native source drift; stop and review a fresh controller run")


class Remote:
    def __init__(self, output):
        self.run = q.Recorder(output).run

    def ssh(self, host, argv, *, timeout=90, allowed=(0,)):
        if host not in {"nsl17", "nsl18"}:
            raise q.QualificationError("unexpected SSH host")
        return self.run(["ssh", *SSH_OPTIONS, host, shlex.join([str(arg) for arg in argv])],
                        timeout=timeout, allowed=allowed)

    def gate(self):
        state = parse_service(self.ssh("nsl17", ["systemctl", "show", GATE_UNIT,
            "--property=LoadState", "--property=ActiveState", "--property=SubState",
            "--property=Result", "--property=ExecMainStatus"]).stdout)
        code = ("import pathlib,sys; p=pathlib.Path(" + repr(GATE_MARKER) + "); "
                "sys.exit(3) if not p.is_file() else print(p.read_text())")
        response = self.ssh("nsl17", ["python3", "-c", code], allowed=(0, 3))
        marker = json.loads(response.stdout) if response.returncode == 0 else None
        return state, marker

    def host_preflight(self, host):
        # Inspection only: no mappings, writes, device conversion or remote kill.
        holders = self.ssh(host, ["sudo", "-n", "fuser", "/dev/dax0.0"], allowed=(0, 1))
        if holders.returncode != 1 or holders.stdout.strip() or holders.stderr.strip():
            raise q.QualificationError(f"{host}: DAX holder or incomplete privileged inspection")
        code = (
            "import json,os,pathlib,socket,stat; p=pathlib.Path('/sys/bus/dax/devices/dax0.0'); "
            "s=os.lstat('/dev/dax0.0'); r=p/'resource'; "
            "print(json.dumps(dict(hostname=socket.gethostname(),size_bytes=int((p/'size').read_text()),"
            "sysfs_dev=(p/'dev').read_text().strip(),device_dev=f'{os.major(s.st_rdev)}:{os.minor(s.st_rdev)}',"
            "character_device=stat.S_ISCHR(s.st_mode),resource=r.read_text().strip() if r.is_file() else None)))"
        )
        # The sysfs resource address is root-readable on these hosts. Elevate
        # this bounded read-only inspection, not the controller/deployment.
        identity = json.loads(self.ssh(host, ["sudo", "-n", "python3", "-c", code]).stdout)
        validate_host_identity(host, identity)
        return {"host": host, "checked_unix": time.time(), "no_local_holders": True, "identity": identity}

    def create_remote_source(self, name):
        native, output = REMOTE + "/native-" + name, REMOTE + "/" + name
        code = ("import os,sys; paths=" + repr([native, output]) + "; "
                "sys.exit('remote artifact path already exists') if any(os.path.lexists(p) for p in paths) else None; "
                "os.mkdir(paths[0],0o700)")
        self.ssh("nsl17", ["python3", "-c", code])
        return native, output

    def verify_deployed(self, native, expected):
        code = ("import hashlib,json,pathlib,sys\n"
                "root=pathlib.Path(" + repr(native) + ")\n"
                "expected=" + repr(expected) + "\n"
                "for name,digest in expected.items():\n"
                " p=root/name\n"
                " if p.is_symlink() or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:\n"
                "  sys.exit('deployed source hash mismatch: '+name)\n"
                "print(json.dumps(dict(verified=True,files=len(expected))))\n")
        response = self.ssh("nsl17", ["python3", "-c", code])
        result = json.loads(response.stdout)
        if result != {"verified": True, "files": len(expected)}:
            raise q.QualificationError("remote source verification was incomplete")
        return result


def validate_host_identity(host, identity):
    aliases = {"nsl17": {"nsl17", "nsl-node17"}, "nsl18": {"nsl18", "nsl-node18"}}
    if (not isinstance(identity, dict) or identity.get("hostname", "").split(".", 1)[0] not in aliases[host]
            or type(identity.get("size_bytes")) is not int or identity["size_bytes"] != q.UPPER_END
            or identity.get("character_device") is not True
            or not re.fullmatch(r"[0-9]+:[0-9]+", identity.get("device_dev", ""))
            or identity["device_dev"] != identity.get("sysfs_dev")):
        raise q.QualificationError(f"unexpected {host} DAX identity/size")


def load_object(path):
    if path.is_symlink() or not path.is_file():
        raise q.QualificationError(f"missing/unsafe result artifact: {path.name}")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise q.QualificationError(f"result artifact is not an object: {path.name}")
    return value


def validate_download(download, expected_source, nsl17_identity, codec="none"):
    manifest = load_object(download / "SOURCE_SHA256.json")
    if manifest != expected_source:
        raise q.QualificationError("downloaded source manifest differs from pinned local snapshot")
    verify_snapshot(download / "native", expected_source)
    report = load_object(download / "REPORT.json")
    expected = {"schema": "crate-cold-tier-qualification-v2", "success": True, "codec": codec,
                "backend": "device-dax", "offset": q.DAX_OFFSET, "mapped_capacity": q.CAPACITY,
                "logical_bytes": q.LOGICAL_BYTES, "repetitions": 3, "errors": [],
                "guards_unchanged": True, "swap_unchanged": True, "dax_unchanged": True,
                "no_kernel_attachment": True, "no_swap_configuration_actions": True,
                "reservation_confirmed_by_operator": True}
    for key, value in expected.items():
        if report.get(key) != value or type(report.get(key)) is not type(value):
            raise q.QualificationError(f"qualification report mismatch: {key}")
    if report.get("hostname", "").split(".", 1)[0] not in {"nsl17", "nsl-node17"}:
        raise q.QualificationError("unexpected qualification host")
    guards = load_object(download / "guards-before.json")
    if set(guards) != {"before", "after"}:
        raise q.QualificationError("missing guard records")
    for label, offset in (("before", q.UPPER_START), ("after", q.DAX_OFFSET + q.CAPACITY)):
        guard = guards[label]
        if (not isinstance(guard, dict) or guard.get("offset") != offset or
                guard.get("length") != q.GUARD_BYTES or
                not re.fullmatch(r"[0-9a-f]{64}", guard.get("sha256", ""))):
            raise q.QualificationError("guard range/hash mismatch")
    if load_object(download / "guards-after.json") != guards:
        raise q.QualificationError("final guard hashes changed")
    seen = set()
    if not isinstance(report.get("probes"), list) or len(report["probes"]) != 9:
        raise q.QualificationError("expected nine qualification probes")
    for probe in report["probes"]:
        if not isinstance(probe, dict) or type(probe.get("repetition")) is not int:
            raise q.QualificationError("invalid repetition record")
        pair = (probe["repetition"], probe.get("pattern"))
        if pair[0] not in range(3) or pair[1] not in q.PATTERNS or pair in seen:
            raise q.QualificationError("missing/duplicate qualification pattern")
        seen.add(pair)
        result = probe.get("result")
        q.validate_probe(result, hardware=True, offset=q.DAX_OFFSET, pattern=pair[1], system="Linux", codec=codec)
        suffix = f"r{pair[0]}-{pair[1]}"
        if load_object(download / f"probe-{suffix}.json") != result:
            raise q.QualificationError("probe result differs from its raw artifact")
        if load_object(download / f"guards-after-{suffix}.json") != guards:
            raise q.QualificationError("per-probe guard hashes changed")
    swaps = [load_object(download / f"swap-{phase}.json") for phase in ("before", "after")]
    for swap in swaps:
        if swap.get("available") is not True:
            raise q.QualificationError("Linux swap configuration was not inspected")
        for field, parsed in (("proc_configuration_kib", q.parse_proc_swaps(swap["proc_raw"])),
                              ("show_configuration_bytes", q.parse_swap_show(swap["show_raw"]))):
            if swap.get(field) != json.loads(json.dumps(parsed)):
                raise q.QualificationError("swap configuration differs from raw inspection")
    if not q.swap_unchanged(*swaps):
        raise q.QualificationError("swap configuration changed")
    identity = load_object(download / "dax-before.json")
    if (identity != load_object(download / "dax-after.json") or
            identity != {"path": "/dev/dax0.0", "major_minor": nsl17_identity["device_dev"], "size_bytes": q.UPPER_END}):
        raise q.QualificationError("DAX identity changed across qualification")
    for phase in ("before", "after"):
        if load_object(download / f"holders-{phase}.json").get("local_holders") != []:
            raise q.QualificationError("DAX holder exclusion not recorded")
    if load_object(download / "quiet-final.json").get("available") is not True:
        raise q.QualificationError("final project exclusion check unavailable")
    return report


def controller(args):
    safe_name(args.name)
    package = args.package.resolve(strict=True)
    if not package.is_dir():
        raise q.QualificationError("package must be an existing directory")
    base = package / "coldstore-backend"
    if base.is_symlink():
        raise q.QualificationError("artifact parent must not be a symlink")
    base.mkdir(exist_ok=True)
    output = base / args.name
    output.mkdir(mode=0o700)  # Never resume/overwrite an earlier run.
    remote = Remote(output)
    def status(stage, **fields):
        value = {"stage": stage, "observed_unix": time.time(), **fields}
        temporary = output / "STATUS.tmp"
        q.write_json(temporary, value)
        temporary.replace(output / "STATUS.json")
        print(json.dumps(value), flush=True)
    receipt = {"schema": "crate-cold-tier-controller-v2", "codec": args.codec, "pid": os.getpid(), "started_unix": time.time(),
               "argv": sys.argv, "name": args.name, "controller_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "gate_unit": GATE_UNIT, "gate_marker": GATE_MARKER,
               "user_authorized_arena": [q.UPPER_START, q.UPPER_END],
               "no_automatic_formal_or_capture": True}
    q.write_json(output / "RECEIPT.json", receipt)
    launched = False
    downloaded = False
    remote_output = REMOTE + "/" + args.name
    download = output / "remote"
    try:
        pinned = source_hashes(SOURCE)
        q.write_json(output / "PINNED_SOURCE_SHA256.json", pinned)
        while True:
            state, marker = remote.gate()
            ready = completion_ready(state, marker)
            status("development_gate_ready" if ready else "waiting_development_q1", service=state, marker=marker)
            if ready:
                q.write_json(output / "DEVELOPMENT_GATE.json", {"state": state, "marker": marker})
                break
            time.sleep(30)
        verify_snapshot(SOURCE, pinned)
        native = q.snapshot_source(SOURCE, output)
        verify_snapshot(native, pinned)
        verify_snapshot(SOURCE, pinned)
        preflights = {host: remote.host_preflight(host) for host in ("nsl17", "nsl18")}
        q.write_json(output / "CROSS_HOST_BEFORE.json", preflights)
        status("deploying_pinned_source", source_sha256=pinned)
        remote_native, remote_output = remote.create_remote_source(args.name)
        verify_snapshot(native, pinned)
        remote.run(["rsync", "-a", "--safe-links", "--exclude=build/", "--exclude=__pycache__/",
                    "-e", "ssh " + shlex.join(SSH_OPTIONS), str(native) + "/", "nsl17:" + remote_native + "/"], timeout=180)
        q.write_json(output / "DEPLOYED_SOURCE_CHECK.json", remote.verify_deployed(remote_native, pinned))
        state, marker = remote.gate()
        if not completion_ready(state, marker):
            raise q.QualificationError("development gate reopened before hardware qualification")
        latest_nsl17 = remote.host_preflight("nsl17")
        q.write_json(output / "NSL17_IMMEDIATELY_BEFORE.json", latest_nsl17)
        latest_nsl18 = remote.host_preflight("nsl18")
        q.write_json(output / "NSL18_IMMEDIATELY_BEFORE.json", latest_nsl18)
        for latest in (latest_nsl17, latest_nsl18):
            if latest["identity"] != preflights[latest["host"]]["identity"]:
                raise q.QualificationError("cross-host DAX identity changed during deployment")
        note = ("User reserved upper half [256GiB,512GiB). Controller observed no privileged local DAX holders "
                f"on nsl17 at {latest_nsl17['checked_unix']:.6f} and nsl18 at {latest_nsl18['checked_unix']:.6f}; "
                "development-q1 is completed/inactive/success/exit0. Checks are snapshots, not distributed fencing.")
        argv = ["sudo", "-n", "taskset", "-c", "0-7", "python3", remote_native + "/qualify.py",
                "--output", remote_output, "--reserved-dax", "--cross-host-reservation-confirmed",
                "--reservation-note", note, "--repetitions", "3", "--codec", args.codec]
        q.write_json(output / "LAUNCH.json", {"host": "nsl17", "argv": argv, "timeout_seconds": 600})
        status("running_reserved_dax_backend_qualification")
        launched = True
        result = remote.ssh("nsl17", argv, timeout=600, allowed=(0, 1))
        status("downloading_raw_results", qualifier_returncode=result.returncode)
        download.mkdir()
        remote.run(["rsync", "-a", "--safe-links", "--rsync-path=sudo -n rsync", "--exclude=build/",
                    "--exclude=__pycache__/", "-e", "ssh " + shlex.join(SSH_OPTIONS),
                    "nsl17:" + remote_output + "/", str(download) + "/"], timeout=180)
        downloaded = True
        if result.returncode != 0:
            raise q.QualificationError("remote qualification failed; raw results preserved")
        report = validate_download(download, pinned, preflights["nsl17"]["identity"], codec=args.codec)
        success = {"schema": "crate-cold-tier-controller-success-v2", "success": True, "codec": args.codec,
                   "finished_unix": time.time(), "scope": report["scope"],
                   "offset": q.DAX_OFFSET, "capacity": q.CAPACITY, "logical_bytes": q.LOGICAL_BYTES,
                   "repetitions": 3, "probes": 9, "source_sha256": pinned,
                   "report_sha256": hashlib.sha256((download / "REPORT.json").read_bytes()).hexdigest(),
                   "cross_host_before": preflights, "nsl17_immediately_before": latest_nsl17,
                   "nsl18_immediately_before": latest_nsl18,
                   "no_automatic_formal_or_capture": True}
        q.write_json(output / "SUCCESS.json", success)
        status("backend_qualification_complete_review_required", scope=success["scope"])
        return 0
    except BaseException as error:
        # A disconnected/expired SSH does not prove that its remote child exited.
        # Never relaunch, kill it, stop services, or assume the arena is free.
        recovery_error = None
        if launched and not downloaded:
            try:
                download.mkdir(exist_ok=True)
                remote.run(["rsync", "-a", "--safe-links", "--rsync-path=sudo -n rsync", "--exclude=build/",
                            "--exclude=__pycache__/", "-e", "ssh " + shlex.join(SSH_OPTIONS),
                            "nsl17:" + remote_output + "/", str(download) + "/"], timeout=180)
            except BaseException as recovery:
                recovery_error = f"{type(recovery).__name__}: {recovery}"
        status("failed_review_required", error=f"{type(error).__name__}: {error}",
               retrieval_error=recovery_error, remote_started=launched,
               caution="No automatic retry; on SSH loss the remote process may still be running.")
        return 1


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--package", type=Path, required=True)
    result.add_argument("--name", type=safe_name, default="reserved-dax-v1")
    result.add_argument("--codec", choices=("none", "lz4"), default="none")
    return result


def main():
    try:
        return controller(parser().parse_args())
    except (OSError, q.QualificationError) as error:
        print(json.dumps({"stage": "failed_before_controller_start", "error": str(error)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
