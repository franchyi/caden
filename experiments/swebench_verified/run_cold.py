#!/usr/bin/env python3
"""Matched no-pool cold-sandbox measurement through normal SandboxFS APIs."""
import argparse
import calendar
import json
import re
import subprocess
import time
from pathlib import Path


FIRST_TOUCH_PROGRAM = r'''
import hashlib, json, os, stat, subprocess, sys, time
ROOT = "/workspace/repository"
MAX_BYTES = 1048576
CLOCK_SOURCE = "time.monotonic_ns" if hasattr(time, "monotonic_ns") else "time.monotonic_float_seconds"
def monotonic_ns():
    # Some pinned SWE-bench environments use Python < 3.7. Keep their runtime
    # unchanged and record the clock used; float-seconds fallback is not a
    # claim of nanosecond clock resolution.
    if hasattr(time, "monotonic_ns"):
        return time.monotonic_ns()
    return int(time.monotonic() * 1000000000)

def file_path(relative):
    if not isinstance(relative, str) or not relative or relative.startswith("/") or "\0" in relative:
        raise RuntimeError("unsafe tracked relative path")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError("unsafe tracked path component")
    path = ROOT
    for part in parts:
        path = os.path.join(path, part)
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise RuntimeError("tracked path contains symlink")
    return path

def metadata(relative):
    s = os.lstat(file_path(relative))
    if not stat.S_ISREG(s.st_mode) or not 0 < s.st_size <= MAX_BYTES:
        raise RuntimeError("tracked target is not a bounded nonempty regular file")
    return {"path": relative, "size_bytes": s.st_size, "device": s.st_dev,
            "inode": s.st_ino, "mtime_ns": s.st_mtime_ns, "mode": s.st_mode}

def select_file():
    # ls-files reads the Git index; lstat reads only filesystem metadata.
    # No selected repository-file payload is opened during selection.
    names = subprocess.check_output(["/usr/bin/git", "-c", "safe.directory=" + ROOT,
        "-C", ROOT, "ls-files", "--cached", "-z"],
        env=dict(os.environ, GIT_OPTIONAL_LOCKS="0")).split(b"\0")
    candidates = []
    for name in names:
        if not name:
            continue
        relative = os.fsdecode(name)
        try:
            item = metadata(relative)
        except (OSError, RuntimeError):
            continue
        candidates.append(item)
    if not candidates:
        raise RuntimeError("no eligible tracked regular file")
    python = [item for item in candidates if item["path"].endswith(".py")]
    chosen = sorted(python or candidates,
        key=lambda item: (-item["size_bytes"], os.fsencode(item["path"])))[0]
    return {"file": chosen, "preference": "python" if python else "tracked-regular",
            "selection_payload_bytes_read": 0}

def checked_file(info):
    expected = info["file"]
    if metadata(expected["path"]) != expected:
        raise RuntimeError("selected file metadata changed before probe")
    return file_path(expected["path"]), expected

def read_file(info):
    path, expected = checked_file(info)
    start = monotonic_ns()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        if (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) != (
                expected["device"], expected["inode"], expected["size_bytes"], expected["mtime_ns"]):
            raise RuntimeError("selected file changed during open")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            payload = stream.read(MAX_BYTES + 1)
    finally:
        os.close(fd)
    operation_ns = monotonic_ns() - start
    if len(payload) != expected["size_bytes"]:
        raise RuntimeError("selected file changed during first read")
    return {"file": expected, "bytes_read": len(payload),
            "source_sha256": hashlib.sha256(payload).hexdigest(),
            "first_byte_hex": payload[:1].hex(), "operation_ns": operation_ns,
            "operation": "open+fstat+read+close; SHA-256 computed afterward"}

def write_file(info):
    path, expected = checked_file(info)
    byte = bytes.fromhex(info["first_byte_hex"])
    if len(byte) != 1:
        raise RuntimeError("same-byte write requires exactly one source byte")
    start = monotonic_ns()
    # Opening writable can itself trigger OverlayFS copy-up: include it.
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        # Copy-up may change device/inode; metadata/content must be preserved.
        if not stat.S_ISREG(s.st_mode) or (s.st_size, s.st_mtime_ns) != (
                expected["size_bytes"], expected["mtime_ns"]):
            raise RuntimeError("selected file changed during writable open")
        if os.write(fd, byte) != 1:
            raise RuntimeError("short same-byte write")
        os.fsync(fd)
    finally:
        os.close(fd)
    operation_ns = monotonic_ns() - start
    # Verification is outside the inner operation, but inside API command time.
    with open(path, "rb") as stream:
        payload = stream.read(MAX_BYTES + 1)
    after = hashlib.sha256(payload).hexdigest()
    if len(payload) != expected["size_bytes"] or after != info["source_sha256"]:
        raise RuntimeError("same-byte write did not preserve complete file contents")
    return {"file_path": expected["path"], "size_bytes": len(payload), "bytes_written": 1,
            "source_sha256": info["source_sha256"], "after_sha256": after,
            "operation_ns": operation_ns, "operation": "open-writable+fstat+write-same-byte+fsync+close",
            "verification_payload_bytes_read": len(payload), "contents_unchanged": True}

action = sys.argv[1]
info = json.loads(sys.argv[2])
result = {"select": select_file, "read": lambda: read_file(info),
          "write": lambda: write_file(info)}[action]()
result.update(clock_source=CLOCK_SOURCE, clock_resolution_ns=time.get_clock_info("monotonic").resolution * 1e9,
              python_version=sys.version)
print(json.dumps(result, sort_keys=True))
'''


def first_touch_probe(call, sandbox, record):
    """Populate partial evidence even on failure; all filesystem work is API-side."""
    record.update(schema="crate-post-cold-first-touch-v1", sandbox_id=sandbox,
        endpoint="post-cold microprobe, excluded from original cold-start endpoint",
        selection="git tracked regular nonempty file; .py preferred; largest <=1 MiB; bytewise path tie-break",
        caveat="metadata selection precedes first payload read; host/base cache is not cold; write follows read",
        timings="client_rpc_ns includes CLI/API; server_command_ns includes Python startup and validation; operation_ns is inner file operation")
    def probe(action, payload):
        argv = ["exec-json", sandbox, "--", "/opt/miniconda3/envs/testbed/bin/python",
                "-I", "-S", "-B", "-c", FIRST_TOUCH_PROGRAM, action, json.dumps(payload)]
        start = time.monotonic_ns()
        response = call(argv)
        elapsed = time.monotonic_ns() - start
        result = record[action] = {"client_rpc_ns": elapsed,
                                  "server_command_ns": response.get("duration_ns"), "response": response}
        if response.get("exit_code") != 0:
            raise RuntimeError(f"first-touch {action} command failed: {response}")
        if (type(response.get("duration_ns")) is not int or
                not 0 < response["duration_ns"] <= elapsed):
            raise ValueError(f"invalid first-touch {action} server/RPC timing")
        parsed = json.loads(response["stdout"])
        if not isinstance(parsed, dict):
            raise ValueError(f"invalid first-touch {action} result")
        result["result"] = parsed
        return parsed
    selected = probe("select", {})
    file = selected["file"]
    relative = file["path"]
    if (not isinstance(relative, str) or relative.startswith("/") or "\0" in relative or
            any(part in {"", ".", ".."} for part in relative.split("/")) or
            type(file["size_bytes"]) is not int or not 0 < file["size_bytes"] <= 1 << 20 or
            selected.get("selection_payload_bytes_read") != 0):
        raise ValueError("invalid metadata-only first-touch selection")
    read = probe("read", {"file": file})
    if (read.get("file") != file or read.get("bytes_read") != file["size_bytes"] or
            not re.fullmatch(r"[0-9a-f]{64}", read.get("source_sha256", "")) or
            not re.fullmatch(r"[0-9a-f]{2}", read.get("first_byte_hex", "")) or
            type(read.get("operation_ns")) is not int or
            not 0 < read["operation_ns"] <= record["read"]["server_command_ns"]):
        raise ValueError("invalid first-read identity/hash/timing")
    record["file"] = {**file, "source_sha256": read["source_sha256"]}
    written = probe("write", {"file": file, "source_sha256": read["source_sha256"],
                              "first_byte_hex": read["first_byte_hex"]})
    if (written.get("file_path") != relative or written.get("size_bytes") != file["size_bytes"] or
            written.get("source_sha256") != read["source_sha256"] or
            written.get("after_sha256") != read["source_sha256"] or
            written.get("contents_unchanged") is not True or written.get("bytes_written") != 1 or
            type(written.get("operation_ns")) is not int or
            not 0 < written["operation_ns"] <= record["write"]["server_command_ns"]):
        raise ValueError("first-write content preservation or timing check failed")
    record["success"] = True


def timestamp_ns(value):
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z", value)
    if not match:
        raise ValueError(f"unexpected UTC timestamp: {value}")
    return calendar.timegm(time.strptime(match[1], "%Y-%m-%dT%H:%M:%S")) * 10**9 + int((match[2] or "").ljust(9, "0"))


def api_call(prefix, args):
    """Preserve exec-json's structured command failure instead of losing stderr."""
    completed = subprocess.run(prefix + args, check=False, capture_output=True, text=True)
    try:
        response = json.loads(completed.stdout)
    except (ValueError, TypeError) as error:
        raise RuntimeError(f"sandbox API {args[0]} returned invalid JSON: "
            f"returncode={completed.returncode}, stdout={completed.stdout!r}, stderr={completed.stderr!r}") from error
    if not isinstance(response, dict):
        raise RuntimeError(f"sandbox API {args[0]} response is not an object: {response!r}")
    # sandboxfsctl deliberately exits nonzero after printing a valid exec-json
    # response for a failing sandbox command. Let the endpoint-specific caller
    # retain that response and reject the failed command, never treat it as success.
    command_failure = (args[0] == "exec-json" and type(response.get("exit_code")) is int
                       and response["exit_code"] != 0)
    if completed.returncode and not command_failure:
        raise RuntimeError(f"sandbox API {args[0]} failed: returncode={completed.returncode}, "
                           f"response={response!r}, stderr={completed.stderr!r}")
    return response


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--limit", type=int, required=True)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--ctl", default="/sandboxfs/crate-swebench-20260919/bin/sandboxfsctl",
                    help="sandboxfsctl binary; the default is the September 19 campaign's")
    ap.add_argument("--first-touch", action="store_true",
                    help="After the unchanged cold endpoint, measure normal-API first repository-file read and same-byte CoW write")
    a = ap.parse_args()
    if a.output.exists():
        raise SystemExit("refusing existing cold results")
    a.output.mkdir(parents=True)
    tasks = json.loads(a.selection.read_text())["tasks"][:a.limit]
    ctl = a.ctl
    records = []
    for rep in range(a.repeats):
        for task in tasks:
            modes = ["baseline", "t1"] if (rep + task["sequence"]) % 2 == 0 else ["t1", "baseline"]
            for mode in modes:
                ident = f"sv-cold-{task['sequence']:02d}-{rep}-{mode}"
                prefix = [ctl, "--socket", task["socket"], "--timeout", "600s"]
                def call(args):
                    return api_call(prefix, args)
                record = {"task": task, "repeat": rep, "mode": mode, "sandbox_id": ident}
                created = False
                try:
                    start_mono = time.monotonic_ns()
                    start_wall = time.time_ns()
                    state = call(["create", "--id", ident, "--base", task["base"], "--mode", mode])
                    created = True
                    response = call(["exec-json", ident, "--", "/bin/true"])
                    end_mono, end_wall = time.monotonic_ns(), time.time_ns()
                    if response["exit_code"] != 0:
                        raise RuntimeError("first command failed")
                    phase = state["timings"]
                    record.update(state=state, first_command=response,
                        cold_start_ns=end_wall - timestamp_ns(phase["request_received_at"]),
                        client_submission_to_ready_ns=end_mono-start_mono,
                        wall_monotonic_discrepancy_ns=(end_wall-start_wall)-(end_mono-start_mono),
                        filesystem_provision_ns=phase["workspace_ready_ns"]-phase["workspace_start_ns"],
                        daemon_ready_ns=phase["total_ns"])
                    if a.first_touch:
                        record["first_touch"] = {}
                        first_touch_probe(call, ident, record["first_touch"])
                except Exception as e:
                    record["error"] = f"{type(e).__name__}: {e}"
                finally:
                    if created:
                        try:
                            record["destroy"] = call(["destroy", ident])
                        except Exception as error:
                            record["cleanup_error"] = f"{type(error).__name__}: {error}"
                            record.setdefault("error", record["cleanup_error"])
                    records.append(record)
                    with (a.output / "samples.jsonl").open("a") as f:
                        f.write(json.dumps(record) + "\n")
                print(task["instance_id"], rep, mode, record.get("cold_start_ns", record.get("error")), flush=True)
                if "error" in record:
                    raise RuntimeError(record["error"])
    completed = {"samples": len(records), "tasks": len(tasks), "repeats": a.repeats}
    if a.first_touch:
        completed["first_touch"] = "post-cold separate read/write microprobes; cold endpoint unchanged"
    (a.output / "completed.json").write_text(json.dumps(completed) + "\n")


if __name__ == "__main__":
    main()
