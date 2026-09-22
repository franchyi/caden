#!/usr/bin/env python3
"""Offline-default cold-store qualification; never attach devices or activate swap.

Hardware writes require explicit opt-in and an operator-confirmed cross-host
reservation. Local checks cannot establish cross-host exclusion or reserve a
quiet interval against experiments started concurrently by another operator.
"""
import argparse
import hashlib
import json
import mmap
import os
from pathlib import Path
import platform
import re
import resource
import socket
import stat
import subprocess
import time


UPPER_START = 256 << 30
UPPER_END = 512 << 30
GUARD_BYTES = 2 << 20
DAX_OFFSET = UPPER_START + GUARD_BYTES  # 274880004096
CAPACITY = 64 << 20
LOGICAL_BYTES = 32 << 20
DAX_PATH = Path("/dev/dax0.0")
TASK_CTL = Path("/sandboxfs/crate-swebench-20260919/bin/sandboxfsctl")
DOCKER_EXECSTART = [
    "/usr/bin/dockerd", "--config-file", "/sandboxfs/crate-swebench-20260919/scripts/docker.json",
    "--data-root", "/sandboxfs/crate-swebench-20260919/docker", "--exec-root", "/run/crate-sv-docker",
    "--pidfile", "/run/crate-sv-docker.pid", "--host", "unix:///run/crate-sv-docker.sock",
    "--bridge", "none", "--iptables=false", "--ip-forward=false", "--ip-masq=false",
    "--containerd-namespace", "crate-sv", "--containerd-plugins-namespace", "crate-sv-plugins",
]
DOCKER_PS_QUERY = ["docker", "--host", "unix:///run/crate-sv-docker.sock", "ps", "-q"]
SYSTEMCTL_QUERY = ["systemctl", "list-units", "--all", "--no-legend", "--no-pager",
                   "--plain", "--state=active,activating,reloading,deactivating", "crate-sv-*"]
SWAP_QUERY = ["swapon", "--show=NAME,TYPE,SIZE,PRIO", "--raw", "--noheadings", "--bytes"]
PATTERNS = ("zeros", "mixed", "random")
SOURCE_FILES = ("Makefile", "README.md", "coldstore.c", "coldstore.h", "nbd_transport.c",
                "nbd_transport.h", "coldstore_probe.c", "test_coldstore.c",
                "test_nbd_transport.c", "test_probe.py", "qualify.py", "test_qualify.py",
                "qualify_remote.py", "test_qualify_remote.py")


class QualificationError(RuntimeError):
    pass


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


class Recorder:
    """Run only explicit argv arrays and retain every command's raw output."""
    def __init__(self, output):
        self.output = output
        self.index = 0

    def run(self, argv, *, cwd=None, timeout=30, allowed=(0,)):
        self.index += 1
        prefix = self.output / f"command-{self.index:03d}"
        argv = [str(arg) for arg in argv]
        record = {"argv": argv, "cwd": str(cwd) if cwd else None, "started_unix": time.time()}
        environment = dict(os.environ, LC_ALL="C", LANG="C")
        # Query tools commonly reside in sbin for non-root file-emulation runs.
        environment["PATH"] = os.environ.get("PATH", "") + ":/usr/sbin:/sbin"
        try:
            result = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=timeout,
                                    env=environment)
            prefix.with_suffix(".stdout").write_text(result.stdout)
            prefix.with_suffix(".stderr").write_text(result.stderr)
            record["returncode"] = result.returncode
            record["finished_unix"] = time.time()
            write_json(prefix.with_suffix(".json"), record)
            if result.returncode not in allowed:
                raise QualificationError(f"command failed ({result.returncode}): {argv}; see {prefix.name}")
            return result
        except (OSError, subprocess.TimeoutExpired) as error:
            record["error"] = f"{type(error).__name__}: {error}"
            if isinstance(error, subprocess.TimeoutExpired):
                for suffix, value in (("stdout", error.stdout), ("stderr", error.stderr)):
                    if value is not None:
                        prefix.with_suffix("." + suffix).write_text(
                            value.decode(errors="replace") if isinstance(value, bytes) else value)
            write_json(prefix.with_suffix(".json"), record)
            raise QualificationError(record["error"]) from error


def parse_units(text):
    units = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split(None, 4)
        if len(fields) < 4 or not fields[0].startswith("crate-sv-"):
            raise QualificationError(f"unrecognized systemctl row: {line!r}")
        name, loaded, active, sub = fields[:4]
        if loaded != "loaded" or active not in {"active", "activating", "reloading", "deactivating"}:
            raise QualificationError(f"unexpected project unit state: {line!r}")
        units.append({"unit": name, "active": active, "sub": sub})
    return units


def quiet_preflight(run, system):
    if system != "Linux":
        return {"available": False, "reason": "non-Linux local emulation; not the nsl17 measurement host"}
    units = parse_units(run(SYSTEMCTL_QUERY).stdout)
    for unit in units:
        if unit["active"] != "active" or unit["sub"] != "running":
            raise QualificationError(f"active project work overlaps qualification: {unit}")
        if unit["unit"] == "crate-sv-docker.service":
            command = run(["systemctl", "show", unit["unit"], "--property=ExecStart", "--value"]).stdout
            # One exact executable/argv block only; never exempt another Docker
            # daemon, a wrapper, changed data/socket paths, or extra commands.
            match = re.fullmatch(r"\{\s*path=/usr/bin/dockerd\s*;\s*argv\[\]=(.*?)\s*;[^{}]*\}",
                                 command.strip(), flags=re.DOTALL)
            if command.count("argv[]=") != 1 or not match or match.group(1).split() != DOCKER_EXECSTART:
                raise QualificationError("unrecognized isolated Docker daemon command")
            containers = run(DOCKER_PS_QUERY)
            if containers.stdout.strip() or containers.stderr.strip():
                raise QualificationError("isolated Docker daemon is busy or inspection was incomplete")
            unit["empty_running_container_list"] = True
            continue
        match = re.fullmatch(r"crate-sv-([0-9]{2})\.service", unit["unit"])
        if not match:
            raise QualificationError(f"active project work overlaps qualification: {unit}")
        index = match.group(1)
        expected = ["/sandboxfs/crate-swebench-20260919/scripts/launch-daemon.sh", index]
        command = run(["systemctl", "show", unit["unit"], "--property=ExecStart", "--value"]).stdout
        match = re.fullmatch(r"\{\s*path=(/(?:usr/)?bin/bash)\s*;\s*argv\[\]=(.*?)\s*;[^{}]*\}",
                             command.strip(), flags=re.DOTALL)
        if (int(index) >= 32 or command.count("argv[]=") != 1 or not match or
                match.group(2).split() != [match.group(1), *expected]):
            raise QualificationError(f"unrecognized task daemon command: {unit['unit']}")
        response = json.loads(run([TASK_CTL, "--socket", f"/run/crate-sv-{index}.sock", "list"]).stdout)
        if not isinstance(response, dict) or response.get("sandboxes") != []:
            raise QualificationError(f"task daemon is not idle: {unit['unit']}")
        unit["empty_sandbox_list"] = True
    return {"available": True, "project_units": units}


def parse_proc_swaps(text):
    lines = text.splitlines()
    if not lines or lines[0].split() != ["Filename", "Type", "Size", "Used", "Priority"]:
        raise QualificationError("unrecognized /proc/swaps header")
    configuration = []
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 5:
            raise QualificationError("unrecognized /proc/swaps row")
        name, kind, size, used, priority = fields
        if int(size) < 0 or int(used) < 0:
            raise QualificationError("negative swap size/usage")
        configuration.append((name, kind, int(size), int(priority)))
    return sorted(configuration)  # Deliberately exclude changing Used values.


def parse_swap_show(text):
    configuration = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 4:
            raise QualificationError("unrecognized swapon --show row")
        name, kind, size, priority = fields
        if int(size) < 0:
            raise QualificationError("negative swap size")
        configuration.append((name, kind, int(size), int(priority)))
    return sorted(configuration)


def swap_snapshot(run, system):
    if system != "Linux":
        return {"available": False, "reason": "Linux swap configuration unavailable"}
    raw_proc = Path("/proc/swaps").read_text()
    raw_show = run(SWAP_QUERY).stdout
    return {"available": True, "proc_raw": raw_proc, "show_raw": raw_show,
            "proc_configuration_kib": parse_proc_swaps(raw_proc),
            "show_configuration_bytes": parse_swap_show(raw_show)}


def swap_unchanged(before, after):
    return (before["available"] == after["available"] and
            before.get("proc_configuration_kib") == after.get("proc_configuration_kib") and
            before.get("show_configuration_bytes") == after.get("show_configuration_bytes"))


def require_hardware_host(system, hostname, euid, confirmed):
    if system != "Linux" or hostname.split(".", 1)[0] not in {"nsl17", "nsl-node17"}:
        raise QualificationError("reserved DAX qualification is restricted to Linux nsl17")
    if euid != 0:
        raise QualificationError("DAX preflight requires root visibility of all local holders")
    if not confirmed:
        raise QualificationError("operator must confirm cross-host reservation and a quiet interval")


def dax_identity():
    device = DAX_PATH.lstat()
    if not stat.S_ISCHR(device.st_mode):
        raise QualificationError("/dev/dax0.0 is not a direct character-device node")
    sysfs = Path("/sys/bus/dax/devices/dax0.0")
    major_minor = (sysfs / "dev").read_text().strip()
    size = int((sysfs / "size").read_text().strip())
    actual = f"{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}"
    if actual != major_minor or size != UPPER_END:
        raise QualificationError(f"unexpected DAX identity/size: {actual}, {major_minor}, {size}")
    return {"path": str(DAX_PATH), "major_minor": actual, "size_bytes": size}


def no_dax_holders(run):
    result = run(["fuser", str(DAX_PATH)], allowed=(0, 1))
    if result.returncode != 1 or result.stdout.strip() or result.stderr.strip():
        raise QualificationError("existing local DAX holders or incomplete fuser visibility")
    return {"local_holders": [], "cross_host_checked_by_runner": False}


def validate_arena(offset, capacity, lower, upper):
    if (offset % GUARD_BYTES or capacity % GUARD_BYTES or capacity <= 4096 or
            offset - GUARD_BYTES < lower or offset + capacity + GUARD_BYTES > upper):
        raise QualificationError("probe or read-only guards fall outside the approved arena")


def guard_hashes(path, offset, capacity):
    """Map only the two guard intervals, read-only; never clear or write them."""
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    result = {}
    try:
        for name, start in (("before", offset - GUARD_BYTES), ("after", offset + capacity)):
            with mmap.mmap(descriptor, GUARD_BYTES, flags=mmap.MAP_SHARED,
                           prot=mmap.PROT_READ, offset=start) as region:
                result[name] = {"offset": start, "length": GUARD_BYTES,
                                "sha256": hashlib.sha256(region).hexdigest()}
    finally:
        os.close(descriptor)
    return result


def validate_probe(result, *, hardware, offset, pattern, system, codec="none"):
    if codec not in {"none", "lz4"}:
        raise QualificationError("unknown codec")
    expected = {"schema": "crate-cold-tier-backend-probe-v2", "success": True, "codec": codec,
                "backend": "device-dax" if hardware else "regular-file-emulation",
                "pattern": pattern, "offset": offset, "mapped_capacity": CAPACITY,
                "source_bytes": LOGICAL_BYTES, "source_release_munmap_succeeded": True,
                "source_mapping_absent_after_release": True if system == "Linux" else None}
    if not isinstance(result, dict):
        raise QualificationError("probe JSON is not an object")
    for key, value in expected.items():
        if result.get(key) != value or type(result.get(key)) is not type(value):
            raise QualificationError(f"probe field mismatch: {key}")
    for key in ("crc32_expected", "crc32_restored", "payload_bytes", "allocator_bytes",
                "metadata_mapping_bytes", "store_ns", "restore_ns", "compression_calls",
                "decompression_calls", "codec_state_bytes", "raw_pages", "zero_pages"):
        if type(result.get(key)) is not int or result[key] < 0:
            raise QualificationError(f"invalid probe accounting: {key}")
    if not (result["crc32_expected"] == result["crc32_restored"] < 2**32):
        raise QualificationError("probe CRC32 mismatch")
    if not (result["payload_bytes"] <= result["allocator_bytes"] <= LOGICAL_BYTES and
            result["metadata_mapping_bytes"] > 0):
        raise QualificationError("invalid probe footprint accounting")
    pages = LOGICAL_BYTES // 4096
    if codec == "none":
        if (result["payload_bytes"] != LOGICAL_BYTES or result["allocator_bytes"] != LOGICAL_BYTES or
                result["raw_pages"] != pages or result["zero_pages"] != 0 or
                any(result[key] for key in ("compression_calls", "decompression_calls", "codec_state_bytes"))):
            raise QualificationError("raw codec unexpectedly compressed, elided zeros or allocated codec state")
    else:
        if not 0 < result["codec_state_bytes"] <= result["metadata_mapping_bytes"]:
            raise QualificationError("missing LZ4 state accounting")
        expected_calls = 0 if pattern == "zeros" else pages
        if result["compression_calls"] != expected_calls:
            raise QualificationError("LZ4 compression call count mismatch")
        if pattern == "zeros":
            if any(result[key] for key in ("payload_bytes", "allocator_bytes", "raw_pages", "decompression_calls")) or result["zero_pages"] != pages:
                raise QualificationError("LZ4 zero elision mismatch")
        elif (result["zero_pages"] or result["raw_pages"] > pages or
              result["decompression_calls"] != pages - result["raw_pages"]):
            raise QualificationError("LZ4 raw/decode accounting mismatch")


def snapshot_source(source, output):
    native = output / "native"
    native.mkdir()
    hashes = {}
    relatives = list(SOURCE_FILES)
    vendor = source / "third_party/lz4"
    if vendor.is_symlink() or not vendor.resolve().is_relative_to(source.resolve()):
        raise QualificationError("vendor source directory must stay within the source tree")
    if vendor.exists():
        for original in sorted(vendor.rglob("*")):
            if original.is_symlink():
                raise QualificationError("vendor source must not contain symlinks")
            if original.is_file():
                relatives.append(str(original.relative_to(source)))
    for relative in relatives:
        original = source / relative
        if original.is_symlink() or not original.is_file():
            raise QualificationError(f"source missing or symlinked: {relative}")
        content = original.read_bytes()
        hashes[relative] = hashlib.sha256(content).hexdigest()
        destination = native / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    write_json(output / "SOURCE_SHA256.json", hashes)
    return native


def qualify(args):
    output = args.output.absolute()
    # Exclusive creation prevents reuse or overwrite, including symlink targets.
    output.mkdir(mode=0o700)
    run = Recorder(output).run
    system, hostname = platform.system(), socket.gethostname()
    hardware = args.reserved_dax
    offset = DAX_OFFSET if hardware else GUARD_BYTES
    path = DAX_PATH if hardware else output / "emulated-region.bin"
    uname = os.uname()
    host_platform = {field: getattr(uname, field) for field in ("sysname", "nodename", "release", "version", "machine")}
    report = {"schema": "crate-cold-tier-qualification-v2", "success": False, "codec": args.codec,
              "backend": "device-dax" if hardware else "regular-file-emulation",
              "scope": "cooperative owned-buffer backend only; not transparent sandbox paging",
              "started_unix": time.time(), "hostname": hostname, "platform": host_platform,
              "memlock_limit_bytes": list(resource.getrlimit(resource.RLIMIT_MEMLOCK)),
              "offset": offset, "mapped_capacity": CAPACITY, "logical_bytes": LOGICAL_BYTES,
              "repetitions": args.repetitions, "probes": [], "errors": [],
              "reservation_confirmed_by_operator": args.cross_host_reservation_confirmed,
              "reservation_note": args.reservation_note,
              "no_kernel_attachment": True, "no_swap_configuration_actions": True}
    before_swap = before_guards = before_identity = None
    write_json(output / "PLAN.json", report)
    try:
        if hardware:
            require_hardware_host(system, hostname, os.geteuid(), args.cross_host_reservation_confirmed)
            if not args.reservation_note.strip():
                raise QualificationError("reserved DAX requires a recorded reservation note")
            validate_arena(offset, CAPACITY, UPPER_START, UPPER_END)
        write_json(output / "quiet-before-build.json", quiet_preflight(run, system))
        before_swap = swap_snapshot(run, system)
        write_json(output / "swap-before.json", before_swap)
        if hardware:
            before_identity = dax_identity()
            write_json(output / "dax-before.json", before_identity)
            write_json(output / "holders-before.json", no_dax_holders(run))
        native = snapshot_source(Path(__file__).resolve().parent, output)
        run(["cc", "--version"])
        # A fresh source-only directory cannot reuse stale build products.
        run(["make", "check"], cwd=native, timeout=300)
        write_json(output / "quiet-after-build.json", quiet_preflight(run, system))
        if hardware:
            if dax_identity() != before_identity:
                raise QualificationError("DAX identity changed during build")
            no_dax_holders(run)
        else:
            # Exclusive fixture; no pre-existing file/device is ever truncated.
            with path.open("xb") as fixture:
                fixture.truncate(CAPACITY + 2 * GUARD_BYTES)
            validate_arena(offset, CAPACITY, 0, CAPACITY + 2 * GUARD_BYTES)
        before_guards = guard_hashes(path, offset, CAPACITY)
        write_json(output / "guards-before.json", before_guards)
        for repetition in range(args.repetitions):
            order = PATTERNS[repetition:] + PATTERNS[:repetition]
            for pattern in order:
                label = f"r{repetition}-{pattern}"
                write_json(output / f"quiet-before-{label}.json", quiet_preflight(run, system))
                if hardware:
                    no_dax_holders(run)
                    if dax_identity() != before_identity:
                        raise QualificationError("DAX identity changed before probe")
                completed = run([native / "build/coldstore_probe",
                                 "--write-reserved-dax" if hardware else "--emulate-file",
                                 path, str(offset), str(CAPACITY), str(LOGICAL_BYTES), pattern,
                                 "--codec", args.codec], timeout=120)
                result = json.loads(completed.stdout)
                write_json(output / f"probe-{label}.json", result)
                validate_probe(result, hardware=hardware, offset=offset, pattern=pattern, system=system, codec=args.codec)
                current_guards = guard_hashes(path, offset, CAPACITY)
                write_json(output / f"guards-after-{label}.json", current_guards)
                if current_guards != before_guards:
                    raise QualificationError(f"guard contents changed after {label}")
                report["probes"].append({"repetition": repetition, "pattern": pattern, "result": result})
                write_json(output / "progress.json", report)
                write_json(output / f"quiet-after-{label}.json", quiet_preflight(run, system))
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        # Preserve diagnostics and check invariants even when a probe failed.
        for name, inspect in (
            ("guards", lambda: guard_hashes(path, offset, CAPACITY) if before_guards is not None else None),
            ("swap", lambda: swap_snapshot(run, system) if before_swap is not None else None),
            ("dax", lambda: dax_identity() if before_identity is not None else None),
        ):
            try:
                after = inspect()
                if after is None:
                    continue
                write_json(output / f"{name}-after.json", after)
                unchanged = (after == before_guards if name == "guards" else
                             swap_unchanged(before_swap, after) if name == "swap" else after == before_identity)
                report[name + "_unchanged"] = unchanged
                if not unchanged:
                    report["errors"].append(name + " invariant changed")
            except Exception as error:
                report["errors"].append(f"final {name} check: {type(error).__name__}: {error}")
        if before_swap is not None:
            try:
                write_json(output / "quiet-final.json", quiet_preflight(run, system))
                if before_identity is not None:
                    write_json(output / "holders-after.json", no_dax_holders(run))
            except Exception as error:
                report["errors"].append(f"final exclusion check: {type(error).__name__}: {error}")
        report["finished_unix"] = time.time()
        report["success"] = not report["errors"] and len(report["probes"]) == 3 * args.repetitions
        write_json(output / "REPORT.json", report)
    return report


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", type=Path, required=True, help="fresh directory; its parent must already exist")
    result.add_argument("--repetitions", type=int, choices=(1, 3), default=1)
    result.add_argument("--codec", choices=("none", "lz4"), default="none")
    result.add_argument("--reserved-dax", action="store_true", help="explicitly write the fixed reserved 64-MiB DAX arena")
    result.add_argument("--cross-host-reservation-confirmed", action="store_true",
                        help="operator confirms cross-host exclusion and a quiet measurement interval")
    result.add_argument("--reservation-note", default="", help="record external reservation/exclusion checks; not checked by this runner")
    return result


def main():
    args = parser().parse_args()
    try:
        report = qualify(args)
    except (OSError, QualificationError) as error:
        print(json.dumps({"success": False, "error": f"{type(error).__name__}: {error}"}))
        return 1
    print(json.dumps({"success": report["success"], "report": str(args.output.absolute() / "REPORT.json"),
                      "backend": report["backend"], "errors": report["errors"]}))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
