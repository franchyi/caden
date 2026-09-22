#!/usr/bin/env python3
"""Recompute all reported metrics from raw measured events and memory samples."""
import argparse
import collections
import hashlib
import json
import math
import re
import stat
import statistics
from pathlib import Path

if __package__:
    from .checkpoints import compare as compare_checkpoint
else:  # Keep direct script execution used by the existing controllers working.
    from checkpoints import compare as compare_checkpoint

ACTIVE = {"pool_prepare", "create", "trace_replay"}
FIRST_TOUCH_SCHEMA = "crate-post-cold-first-touch-v1"
COHORT_SCHEMA = "crate-formal-cohorts-v1"
FIRST_TOUCH_SELECTION = "git tracked regular nonempty file; .py preferred; largest <=1 MiB; bytewise path tie-break"
FIRST_TOUCH_OPERATIONS = {
    "read": "open+fstat+read+close; SHA-256 computed afterward",
    "write": "open-writable+fstat+write-same-byte+fsync+close",
}


def distribution(values, scale=1):
    if not values:
        raise ValueError("empty measurement population")
    v = sorted(x / scale for x in values)
    return {"n": len(v), "mean": statistics.mean(v),
            **{f"p{p}": v[max(0, math.ceil(p / 100 * len(v))-1)] for p in (50, 95, 99)}, "max": max(v)}


def integral(samples, metric):
    duration, area = 0, 0
    for left, right in zip(samples, samples[1:]):
        dt = right["monotonic_ns"] - left["monotonic_ns"]
        if dt <= 0:
            raise ValueError("non-monotonic memory sample timestamps")
        if left["phase"] in ACTIVE:
            duration += dt
            area += left[metric] * dt
    if not duration:
        raise ValueError("empty memory observation window")
    return {"area_byte_ns": area, "duration_ns": duration, "mean_bytes": area / duration}


def _positive_first_touch_ns(value):
    # Actual collector timestamps are integer nanoseconds. This also excludes
    # booleans, NaN, infinity and unit-converted floating-point fields.
    if type(value) is not int or value <= 0:
        raise ValueError("first-touch timing must be positive finite integer nanoseconds")
    return value


def _first_touch_metadata(file):
    if not isinstance(file, dict):
        raise ValueError("missing first-touch file metadata")
    path = file.get("path")
    if (not isinstance(path, str) or not path or path.startswith("/") or "\0" in path or
            any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ValueError("unsafe first-touch relative file identity")
    size = file.get("size_bytes")
    if type(size) is not int or not 0 < size <= 1 << 20:
        raise ValueError("invalid first-touch file size")
    for key in ("device", "inode", "mtime_ns", "mode"):
        if type(file.get(key)) is not int or file[key] < 0:
            raise ValueError("invalid first-touch file metadata: " + key)
    if not stat.S_ISREG(file["mode"]):
        raise ValueError("first-touch target is not a regular file")
    return path, size


def _validate_first_touch(row):
    touch = row.get("first_touch")
    if (not isinstance(touch, dict) or touch.get("schema") != FIRST_TOUCH_SCHEMA or
            touch.get("success") is not True or touch.get("error")):
        raise ValueError("missing, failed or wrong-schema first-touch record")
    sandbox = row.get("sandbox_id")
    if not isinstance(sandbox, str) or not sandbox or touch.get("sandbox_id") != sandbox:
        raise ValueError("first-touch sandbox identity mismatch")
    if (touch.get("endpoint") != "post-cold microprobe, excluded from original cold-start endpoint" or
            touch.get("selection") != FIRST_TOUCH_SELECTION):
        raise ValueError("first-touch endpoint or deterministic selection mismatch")
    results = {}
    for action in ("select", "read", "write"):
        event = touch.get(action)
        if not isinstance(event, dict):
            raise ValueError("missing first-touch action: " + action)
        client = _positive_first_touch_ns(event.get("client_rpc_ns"))
        server = _positive_first_touch_ns(event.get("server_command_ns"))
        response, result = event.get("response"), event.get("result")
        if (not isinstance(response, dict) or type(response.get("exit_code")) is not int or
                response["exit_code"] != 0 or not isinstance(result, dict)):
            raise ValueError("failed or missing first-touch normal-API response")
        if (response.get("sandbox_id", sandbox) != sandbox or
                _positive_first_touch_ns(response.get("duration_ns")) != server or server > client):
            raise ValueError("first-touch API timing or sandbox mismatch")
        if not isinstance(response.get("stdout"), str):
            raise ValueError("missing first-touch raw API stdout")
        try:
            parsed = json.loads(response["stdout"])
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("invalid first-touch raw API JSON") from error
        if parsed != result:
            raise ValueError("first-touch parsed result differs from raw API stdout")
        if action in FIRST_TOUCH_OPERATIONS:
            if (_positive_first_touch_ns(result.get("operation_ns")) > server or
                    result.get("operation") != FIRST_TOUCH_OPERATIONS[action]):
                raise ValueError("first-touch inner operation or endpoint mismatch")
        results[action] = result
    selected, read, written = (results[action] for action in ("select", "read", "write"))
    selected_file = selected.get("file")
    path, size = _first_touch_metadata(selected_file)
    if (type(selected.get("selection_payload_bytes_read")) is not int or selected["selection_payload_bytes_read"] != 0 or
            selected.get("preference") not in {"python", "tracked-regular"} or
            (selected["preference"] == "python") != path.endswith(".py") or
            "source_sha256" in selected_file):
        raise ValueError("first-touch selection was not the registered metadata-only operation")
    source_hash = read.get("source_sha256")
    if (read.get("file") != selected_file or type(read.get("bytes_read")) is not int or read["bytes_read"] != size or
            not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash) or
            not isinstance(read.get("first_byte_hex"), str) or not re.fullmatch(r"[0-9a-f]{2}", read["first_byte_hex"]) or
            touch.get("file") != {**selected_file, "source_sha256": source_hash}):
        raise ValueError("first-touch read identity, byte count or source hash mismatch")
    if (written.get("file_path") != path or type(written.get("size_bytes")) is not int or written["size_bytes"] != size or
            type(written.get("bytes_written")) is not int or written["bytes_written"] != 1 or
            written.get("source_sha256") != source_hash or written.get("after_sha256") != source_hash or
            written.get("contents_unchanged") is not True or
            type(written.get("verification_payload_bytes_read")) is not int or written["verification_payload_bytes_read"] != size):
        raise ValueError("first-touch write changed contents, hash or registered byte counts")
    return {"path": path, "size_bytes": size, "source_sha256": source_hash}


def _cold_first_touch(plan, rows, expected_task_ids):
    """Validate a separate post-cold population; never alter cold/tool metrics."""
    if (type(plan.get("tasks")) is not int or plan["tasks"] <= 0 or
            type(plan.get("cold_repetitions")) is not int or plan["cold_repetitions"] <= 0):
        raise ValueError("invalid first-touch registered population")
    if (len(expected_task_ids) != plan["tasks"] or
            any(not isinstance(task, str) or not task for task in expected_task_ids)):
        raise ValueError("first-touch workload task identities do not match the plan")
    expected = {(task, repeat, mode) for task in expected_task_ids
                for repeat in range(plan["cold_repetitions"]) for mode in ("baseline", "t1")}
    seen, files = set(), {}
    for row in rows:
        task_record = row.get("task")
        task = task_record.get("instance_id") if isinstance(task_record, dict) else None
        repeat, mode = row.get("repeat"), row.get("mode")
        if (type(repeat) is not int or not isinstance(task, str) or
                not isinstance(mode, str) or mode not in {"baseline", "t1"}):
            raise ValueError("invalid first-touch task/repeat/mode identity")
        key = (task, repeat, mode)
        if key not in expected or key in seen or row.get("error") or row.get("cleanup_error"):
            raise ValueError("missing, duplicate, unexpected or failed first-touch cold sample")
        seen.add(key)
        identity = _validate_first_touch(row)
        if task in files and files[task] != identity:
            raise ValueError("first-touch selected path/size/source hash changed across modes or repeats")
        files[task] = identity  # Device/inode are intentionally not cross-run identities.
    if seen != expected:
        raise ValueError("incomplete first-touch population")
    result = {"schema": "crate-post-cold-first-touch-analysis-v1", "raw_schema": FIRST_TOUCH_SCHEMA,
              "tasks": plan["tasks"], "repetitions": plan["cold_repetitions"],
              "files": dict(sorted(files.items())), "modes": {},
              "limitations": [
                  "Post-cold microprobes on a metadata-selected real tracked file, not replayed agent tool commands; excluded from cold-start and real-tool populations.",
                  "Warm host and local prepared base; selected-file cache warmth is unspecified, no cache reset is claimed, and write follows the measured read.",
                  "Inner read includes open, fstat, payload read and close; its hash is computed afterward within the API command.",
                  "Inner same-byte write includes writable open/copy-up, fstat, write, fsync durability and close; complete-file hash verification is outside inner write but inside the API command.",
                  "Server command time includes Python startup and validation; client RPC time additionally includes CLI/API overhead. Metadata-selection commands are validated but not folded into read/write distributions."]}
    for mode in ("baseline", "t1"):
        population = [row["first_touch"] for row in rows if row["mode"] == mode]
        result["modes"][mode] = {"samples": len(population)}
        for action in ("read", "write"):
            events = [touch[action] for touch in population]
            result["modes"][mode][action] = {
                "operation_ms": distribution([event["result"]["operation_ns"] for event in events], 1e6),
                "server_command_ms": distribution([event["server_command_ns"] for event in events], 1e6),
                "client_rpc_ms": distribution([event["client_rpc_ns"] for event in events], 1e6)}
    return result


def _fidelity_verification(plan, reports, workloads_dir, source_root):
    required = plan["kind"] == "formal" or workloads_dir is not None
    result = {"mode": "artifact-verified" if required else "legacy-not-artifact-verified",
              "scope": "replay fidelity; cold samples retain the existing count and endpoint checks",
              "artifact_verification_required": required,
              "formal_evidence_verified": False,
              "workloads_dir_override": str(workloads_dir) if workloads_dir is not None else None,
              "source_root_override": str(source_root) if source_root is not None else None,
              "checkpoint_source_sha256": hashlib.sha256(
                  Path(__file__).with_name("checkpoints.py").read_bytes()).hexdigest(),
              "checks": {}}
    if not required:
        result["limitations"] = [
            "Legacy pilot/development recomputation retains existing metric checks only; source files, exact normalized events, and expected final fingerprints were not artifact-verified by the strict checkpoint gate.",
            "Provide both --workloads-dir and --source-root to request strict verification; no absence of evidence is treated as a passing strict gate."]
        return result
    orders = plan["orders"]
    if not orders or len(orders) != plan["repetitions"]:
        raise ValueError("strict analysis requires all registered repetitions")
    reference_labels, reference_commits = None, None
    for rep, order in enumerate(orders):
        labels = set(order)
        if len(labels) != len(order) or "F0-S0" not in labels or len(labels) < 2:
            raise ValueError("strict analysis requires one baseline and treatments in every repetition")
        if reference_labels is None:
            reference_labels = labels
        elif labels != reference_labels:
            raise ValueError("strict analysis requires the same configurations in each repetition")
        baseline = reports[(rep, "F0-S0")]
        if reference_commits is None:
            reference_commits = baseline.get("commits")
        elif baseline.get("commits") != reference_commits:
            raise ValueError("frozen source changed across repetitions")
        for label in order:
            if label == "F0-S0":
                continue
            checkpoint = compare_checkpoint(
                baseline, reports[(rep, label)],
                workloads_dir=workloads_dir, source_root=source_root,
            )
            if checkpoint.get("fidelity", {}).get("formal_evidence_verified") is not True:
                raise ValueError("strict analysis requires artifact-verified checkpoint evidence")
            result["checks"][f"r{rep}-{label}"] = checkpoint
    result["formal_evidence_verified"] = True
    result["performance_guards_pass"] = all(
        check["guards_pass"] and check["baseline_deadline_violations"] == 0
        for check in result["checks"].values())
    result["limitations"] = sorted({limitation for check in result["checks"].values()
                                    for limitation in check["fidelity"].get("limitations", [])})
    result["limitations"].append(
        "Artifact verification establishes replay fidelity, not performance superiority, statistical non-inferiority, or uniquely attributable host DRAM.")
    return result


def _cohort_population(replays, expected_tools, deadline_ms):
    tools = [tool for replay in replays for tool in replay["tools"]]
    return {
        "requested_task_runs": len(replays),
        "completed_task_runs": sum(not replay.get("error") and not replay.get("fidelity_errors")
                                   for replay in replays),
        "expected_tool_calls": expected_tools,
        "completed_tool_calls": len(tools),
        "task_execution_failures": sum(bool(replay.get("error")) for replay in replays),
        "task_fidelity_failures": sum(bool(replay.get("fidelity_errors")) for replay in replays),
        "unexpected_tool_outcomes": sum(tool["exit_code"] != tool["expected_exit_code"] for tool in tools),
        "infrastructure_failures": 0,  # Strict complete-report gate rejects any summary errors.
        "expected_nonzero_shell_exits": sum(tool["expected_exit_code"] != 0 for tool in tools),
        "observed_nonzero_shell_exits": sum(tool["exit_code"] != 0 for tool in tools),
        "observed_timeout_like_shell_exits": sum(tool["exit_code"] in (124, 137) for tool in tools),
        "absolute_deadline_violations": sum(tool["turn_ns"] > deadline_ms * 1e6 for tool in tools),
        "tool_ms": distribution([tool["turn_ns"] for tool in tools], 1e6),
    }


def _formal_cohorts(plan, reports, root, workloads_dir, fidelity):
    """Honor an explicit pre-measurement partition; never infer it for old plans."""
    if "selection" not in plan:
        return None
    registration = plan["selection"]
    if (plan["kind"] != "formal" or not isinstance(registration, dict) or
            registration.get("schema") != COHORT_SCHEMA or
            type(plan.get("tasks")) is not int or plan["tasks"] != 32 or
            fidelity.get("formal_evidence_verified") is not True):
        raise ValueError("registered cohorts require formal, strict-verified, exactly 32-task evidence")
    if registration.get("manifest_path") != "selection.json":
        raise ValueError("cohort selection must use the copied selection.json artifact")
    selection_path = Path(root) / "selection.json"
    if selection_path.resolve().parent != Path(root).resolve():
        raise ValueError("unsafe cohort selection artifact path")
    try:
        payload = selection_path.read_bytes()
        selection = json.loads(payload)
    except (OSError, ValueError) as error:
        raise ValueError("missing or invalid cohort selection artifact") from error
    digest = hashlib.sha256(payload).hexdigest()
    if digest != registration.get("manifest_sha256"):
        raise ValueError("cohort selection manifest hash mismatch")
    tasks = selection.get("tasks") if isinstance(selection, dict) else None
    if (not isinstance(selection, dict) or
            selection.get("schema") != "crate-swebench-verified-selection-v1" or
            selection.get("dataset") != "princeton-nlp/SWE-bench_Verified" or
            not isinstance(selection.get("revision"), str) or not selection["revision"] or
            not isinstance(tasks, list) or len(tasks) != 32):
        raise ValueError("invalid fixed 32-task SWE-bench selection evidence")
    ids = []
    for sequence, task in enumerate(tasks):
        if (not isinstance(task, dict) or type(task.get("sequence")) is not int or
                task["sequence"] != sequence or
                any(not isinstance(task.get(key), str) or not task[key]
                    for key in ("instance_id", "base", "repo"))):
            raise ValueError("cohort selection task identity/order mismatch")
        ids.append(task["instance_id"])
    if (len(set(ids)) != 32 or registration.get("task_ids") != ids or
            registration.get("sequences") != list(range(32)) or
            any(type(value) is not int for value in registration["sequences"]) or
            registration.get("development_task_ids") != ids[:8] or
            registration.get("heldout_task_ids") != ids[8:]):
        raise ValueError("cohort registration differs from the fixed first-8/remaining-24 selection")
    reference = reports[(0, "F0-S0")]
    workload_root = Path(workloads_dir) if workloads_dir is not None else Path(reference["config"]["workloads_dir"])
    try:
        manifest_payload = (workload_root / "manifest.json").read_bytes()
        manifest = json.loads(manifest_payload)
        entries = manifest["workloads"]
        if (hashlib.sha256(manifest_payload).hexdigest() != reference["workload"]["manifest_sha256"] or
                manifest["source"] != {"dataset": selection["dataset"], "revision": selection["revision"]} or
                not isinstance(entries, list) or len(entries) != 32):
            raise ValueError("cohort workload manifest differs from fixed selection evidence")
        expected_tools = []
        for task, entry in zip(tasks, entries):
            path = (workload_root / entry["path"]).resolve()
            if workload_root.resolve() not in path.parents:
                raise ValueError("unsafe cohort workload path")
            payload = path.read_bytes()
            workload = json.loads(payload)
            if (hashlib.sha256(payload).hexdigest() != entry["sha256"] or
                    workload["source"] != {"trajectory_id": task["instance_id"],
                                           "instance_id": task["instance_id"], "repo": task["repo"]} or
                    workload["base"] != task["base"] or type(workload["tool_count"]) is not int or
                    workload["tool_count"] <= 0 or workload["tool_count"] != entry["tool_count"]):
                raise ValueError("cohort workload identity/order/count differs from fixed selection")
            expected_tools.append(workload["tool_count"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("missing or invalid cohort workload evidence") from error
    deadline = plan.get("turn_latency_absolute_deadline_ms")
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= 0:
        raise ValueError("invalid cohort tool deadline")
    for report in reports.values():
        if (len(report["requests"]) != 32 or len(report["replays"]) != 32 or
                report["summary"].get("success") is not True or report["summary"].get("errors")):
            raise ValueError("incomplete or failed cohort work; no task may be silently omitted")
        for sequence, (task, request, replay) in enumerate(zip(tasks, report["requests"], report["replays"])):
            if (type(request.get("sequence")) is not int or type(replay.get("sequence")) is not int or
                    request["sequence"] != sequence or replay["sequence"] != sequence or
                    request.get("instance_id") != task["instance_id"] or
                    request.get("trajectory_id") != task["instance_id"] or
                    replay.get("trajectory_id") != task["instance_id"] or request.get("base") != task["base"] or
                    len(replay["tools"]) != expected_tools[sequence] or
                    replay.get("error") or replay.get("fidelity_errors") or
                    any(type(tool.get(key)) is not int for tool in replay["tools"]
                        for key in ("exit_code", "expected_exit_code"))):
                raise ValueError("cohort request/replay identity, order, completion or outcome mismatch")
    result = {"schema": COHORT_SCHEMA, "selection_manifest_sha256": digest,
              "workload_manifest_sha256": reference["workload"]["manifest_sha256"],
              "partition": "fixed selection sequence 0-7 development; 8-31 heldout",
              "absolute_deadline_ms": deadline, "cohorts": {},
              "limitations": [
                  "Development and heldout are fixed task-identity cohorts in the same campaign, not independent experimental groups or independent host trials; the first eight tasks were used during development.",
                  "Pooled latency is tool-call weighted over all repetitions; no slow task, expected nonzero shell exit, or deadline violation is dropped. Repeated calls are not independent statistical replicates.",
                  "Work-completion and infrastructure failures are distinct from captured expected nonzero shell exits; exit codes 124/137 are timeout-like shell outcomes, not independently proven infrastructure timeouts.",
                  "Only complete artifact-verified reports enter this analysis; incomplete or failed infrastructure/fidelity evidence rejects the campaign instead of becoming a successful-subset report. Infrastructure failure counts are zero only because the full-report gate passed.",
                  "No cohort DRAM or memory attribution is computed: simultaneous aggregate service/cgroup samples cannot be partitioned by task identity or subtracted to infer cohort memory.",
                  "Agent submitted/resolved status is not a performance pass count; cohort latency does not establish solve rate, density at SLO, or statistical non-inferiority."]}
    labels = sorted({label for _, label in reports})
    for name, indices in (("development", range(8)), ("heldout", range(8, 32))):
        indices = list(indices)
        cohort = {"task_ids": [ids[index] for index in indices], "sequences": indices,
                  "distinct_tasks": len(indices), "systems": {}}
        for label in labels:
            runs, pooled = [], []
            count = sum(expected_tools[index] for index in indices)
            for repeat in range(plan["repetitions"]):
                replays = [reports[(repeat, label)]["replays"][index] for index in indices]
                pooled.extend(replays)
                runs.append({"repeat": repeat, **_cohort_population(replays, count, deadline)})
            cohort["systems"][label] = {"runs": runs,
                "pooled": _cohort_population(pooled, count * plan["repetitions"], deadline)}
        result["cohorts"][name] = cohort
    return result


def analyze(root, *, workloads_dir=None, source_root=None):
    if (workloads_dir is None) != (source_root is None):
        raise ValueError("--workloads-dir and --source-root must be supplied together")
    root = Path(root)
    plan = json.loads((root / "PLAN.json").read_text())
    if plan["kind"] not in {"pilot", "development", "formal"}:
        raise ValueError("unknown campaign kind")
    if "post_cold_first_touch" in plan and type(plan["post_cold_first_touch"]) is not bool:
        raise ValueError("post_cold_first_touch must be an explicit Boolean plan flag")
    if not (root / "COMPLETED.json").exists():
        raise ValueError("campaign lacks completion marker; not a complete result")
    expected = {(i, c) for i, order in enumerate(plan["orders"]) for c in order}
    groups = collections.defaultdict(list)
    hashes, signatures, manifest, reports = {}, None, None, {}
    for rep, label in sorted(expected):
        path = root / f"replay-r{rep}-{label}.json"
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        report = json.loads(path.read_text())
        reports[(rep, label)] = report
        if not report["summary"]["success"]:
            raise ValueError(f"failed replay: {path}")
        if report["config"]["wait_scale"] != 1:
            raise ValueError("compressed wait found")
        tools = [t for r in report["replays"] for t in r["tools"]]
        if any(t["exit_code"] != t["expected_exit_code"] for t in tools):
            raise ValueError("exit-code fidelity failure")
        if any(t["turn_ns"] != t["wake_restore_ns"] + t["command_ns"] or t["turn_ns"] <= 0 for t in tools):
            raise ValueError("latency endpoint arithmetic mismatch")
        sig = [(r["trajectory_id"], [(t["sequence"], t["source_arguments_sha256"]) for t in r["tools"]]) for r in report["replays"]]
        if signatures is None:
            signatures, manifest = sig, report["workload"]["manifest_sha256"]
        if sig != signatures or manifest != report["workload"]["manifest_sha256"]:
            raise ValueError("unequal completed task/tool work")
        if len(report["replays"]) != plan["tasks"]:
            raise ValueError("task count mismatch")
        memory = {}
        for field in ("sandbox_memory_current_bytes", "task_daemon_cgroup_sum_bytes",
                      "task_service_cgroup_sum_bytes", "daemon_plus_sandbox_cgroup_bytes",
                      "signed_host_physical_delta_bytes", "signed_host_available_delta_bytes",
                      "sandbox_memory_swap_bytes"):
            memory[field] = integral(report["samples"], field)
            vals = [s[field] for s in report["samples"] if s["phase"] in ACTIVE]
            memory[field]["sample_distribution_mib"] = distribution(vals, 2**20)
        groups[label].append({"repeat": rep, "tools": tools, "memory": memory,
            "task_macro_mean_ms": statistics.mean(statistics.mean(t["turn_ns"] for t in r["tools"])/1e6 for r in report["replays"]),
            "reclaim_events": report["summary"]["reclaim_events"],
            "reclaim_errors": report["summary"]["reclaim_errors"],
            "speculative_prepared_bytes": report["summary"]["speculative_prepared_bytes"],
            "speculative_restore_errors": report["summary"]["speculative_restore_errors"],
            "major_faults_completed_sandboxes_total": report["summary"].get("major_faults_completed_sandboxes_total"),
            "pool_hits": report["summary"]["pool_hits"], "pool_misses": report["summary"]["pool_misses"],
            "throughput": report["summary"]["completed_turns_per_second"]})
    fidelity = _fidelity_verification(plan, reports, workloads_dir, source_root)
    result = {"schema": "crate-swebench-real-analysis-v1", "plan": plan, "raw_sha256": hashes,
              "analysis_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "fidelity_verification": fidelity,
              "workload_manifest_sha256": manifest, "systems": {}, "cold": {},
              "limitations": ["shared host: not isolated or uniquely attributable host DRAM",
                  "real-command trace replay, not live model execution during matched measurements",
                  "sampled balanced task subset, not a SWE-bench solve-rate result",
                  "p99 from limited samples is descriptive; inspect per-run points",
                  "bounded closed-loop waves; concurrency is a cap, not continuous occupancy or an open-loop arrival-rate sweep",
                  "validation-only final workspace audit is outside tool-call latency but within the lifecycle memory/throughput window"]}
    cohorts = _formal_cohorts(plan, reports, root, workloads_dir, fidelity)
    if cohorts is not None:
        result["formal_cohorts"] = cohorts
        hashes["selection.json"] = cohorts["selection_manifest_sha256"]
    elif plan["kind"] == "formal":
        result["limitations"].append(
            "Legacy formal plan has no registered selection/cohort artifact; no development-versus-heldout partition is inferred retroactively.")
    for label, runs in groups.items():
        tools = [t for r in runs for t in r["tools"]]
        mem = {}
        for field in runs[0]["memory"]:
            mem[field] = {"time_weighted_mean_mib": sum(r["memory"][field]["area_byte_ns"] for r in runs) /
                sum(r["memory"][field]["duration_ns"] for r in runs) / 2**20,
                "run_means_mib": [r["memory"][field]["mean_bytes"] / 2**20 for r in runs],
                "run_p95_mib": [r["memory"][field]["sample_distribution_mib"]["p95"] for r in runs],
                "peak_mib": max(r["memory"][field]["sample_distribution_mib"]["max"] for r in runs)}
        result["systems"][label] = {"tool_ms": distribution([t["turn_ns"] for t in tools], 1e6),
            "wake_ms": distribution([t["wake_restore_ns"] for t in tools], 1e6),
            "execution_ms": distribution([t["command_ns"] for t in tools], 1e6),
            "run_tool_means_ms": [statistics.mean(t["turn_ns"] for t in r["tools"])/1e6 for r in runs],
            "run_tool_distributions_ms": [distribution([t["turn_ns"] for t in r["tools"]], 1e6) for r in runs],
            "run_task_macro_means_ms": [r["task_macro_mean_ms"] for r in runs],
            "absolute_deadline_violations": sum(t["turn_ns"] > plan["turn_latency_absolute_deadline_ms"] * 1e6 for t in tools),
            "nonzero_tool_exits": sum(t["exit_code"] != 0 for t in tools),
            "timeout_tool_exits": sum(t["exit_code"] in (124,137) for t in tools),
            "reclaim_events": sum(r["reclaim_events"] for r in runs),
            "reclaim_errors": sum(r["reclaim_errors"] for r in runs),
            "pool_hits": sum(r["pool_hits"] for r in runs), "pool_misses": sum(r["pool_misses"] for r in runs),
            "speculative_prepared_bytes": sum(r["speculative_prepared_bytes"] for r in runs),
            "speculative_restore_errors": sum(r["speculative_restore_errors"] for r in runs),
            "run_major_faults": [r["major_faults_completed_sandboxes_total"] for r in runs],
            "run_throughput": [r["throughput"] for r in runs], "memory": mem}
    coldpath = root / "cold/samples.jsonl"
    hashes["cold/samples.jsonl"] = hashlib.sha256(coldpath.read_bytes()).hexdigest()
    cold = [json.loads(line) for line in coldpath.read_text().splitlines()]
    if len(cold) != plan["tasks"] * plan["cold_repetitions"] * 2:
        raise ValueError("cold sample count mismatch")
    seen = set()
    for r in cold:
        key = (r["task"]["instance_id"], r["repeat"], r["mode"])
        if key in seen or r.get("error") or r["cold_start_ns"] <= 0:
            raise ValueError("bad cold measurement")
        seen.add(key)
        if abs(r["wall_monotonic_discrepancy_ns"]) > 1_000_000:
            raise ValueError("wall-clock movement invalidates cold endpoint")
    if plan.get("post_cold_first_touch") is True:
        requests = reports[(0, "F0-S0")].get("requests")
        if not isinstance(requests, list) or len(requests) != plan["tasks"]:
            raise ValueError("first-touch population requires registered replay task identities")
        task_ids = [request.get("instance_id") if isinstance(request, dict) else None for request in requests]
        if any(not isinstance(task, str) or not task for task in task_ids):
            raise ValueError("first-touch population requires valid replay task identities")
        result["cold_first_touch"] = _cold_first_touch(plan, cold, set(task_ids))
        result["limitations"].append(
            "Opt-in post-cold first-read/write microprobes are separate from unchanged cold-start and real-tool latency populations; see cold_first_touch for cache, fsync and verification boundaries.")
    for mode in ("baseline", "t1"):
        rows = [r for r in cold if r["mode"] == mode]
        result["cold"][mode] = {"cold_start_ms": distribution([r["cold_start_ns"] for r in rows], 1e6),
             "provision_ms": distribution([r["filesystem_provision_ns"] for r in rows], 1e6),
             "client_ready_ms": distribution([r["client_submission_to_ready_ns"] for r in rows], 1e6)}
    f0 = result["systems"]["F0-S0"]["tool_ms"]
    result["tool_vs_fullcopy"] = {k: {
        "mean_ratio": v["tool_ms"]["mean"] / f0["mean"],
        "p95_ratio": v["tool_ms"]["p95"] / f0["p95"],
        "p99_ratio": v["tool_ms"]["p99"] / f0["p99"],
        "both_relative_tail_guards_pass": all(v["tool_ms"][p] <= plan["relative_p95_p99_guard"] * f0[p] for p in ("p95", "p99"))}
        for k, v in result["systems"].items() if k != "F0-S0"}
    for label, comparison in result["tool_vs_fullcopy"].items():
        baseline_runs = result["systems"]["F0-S0"]["run_tool_distributions_ms"]
        treatment_runs = result["systems"][label]["run_tool_distributions_ms"]
        comparison["paired_runs"] = [{"repeat": i, **{p + "_ratio": t[p] / b[p] for p in ("mean", "p95", "p99")},
            "both_relative_tail_guards_pass": all(t[p] <= plan["relative_p95_p99_guard"] * b[p] for p in ("p95", "p99"))}
            for i, (b, t) in enumerate(zip(baseline_runs, treatment_runs))]
    return result


def markdown(data):
    p = data["plan"]
    lines = ["# SWE-bench Verified real-command results", "",
        f"Status: {p['kind']}; {p['tasks']} distinct tasks, concurrency cap {p['active_sandboxes']}, "
        f"{p['repetitions']} replay repetition(s), waits 1.0x. No task-success claim.", "",
        "## Tool-call latency", "",
        "Includes wake/queue/restore and command/output path; excludes LLM wait and cold creation.", "",
        "| System | Calls | Mean ms | p50 ms | p95 ms | p99 ms | Nonzero exits |",
        "|---|---:|---:|---:|---:|---:|---:|"]
    for label, r in sorted(data["systems"].items()):
        d = r["tool_ms"]
        lines.append(f"| {label} | {d['n']} | {d['mean']:.2f} | {d['p50']:.2f} | {d['p95']:.2f} | {d['p99']:.2f} | {r['nonzero_tool_exits']} |")
    if "formal_cohorts" in data:
        cohorts = data["formal_cohorts"]
        lines += ["", "## Registered development and heldout task cohorts", "",
                  "Fixed selection sequence 0–7 (development, 8 tasks) and 8–31 (heldout, 24 tasks). "
                  "Same campaign, not independent experimental groups. Per-repetition rows and pooled tool-call-weighted "
                  "rows retain every measured call; pooled task counts count task-runs, not distinct tasks.", ""]
        for name, cohort in cohorts["cohorts"].items():
            lines += [f"### {name.capitalize()}: {cohort['distinct_tasks']} distinct tasks", "",
                      "| System | Repeat | Completed/planned task-runs | Completed/planned calls | Mean ms | p95 ms | p99 ms | Task/outcome/infra failures | Expected/observed nonzero exits | Deadline misses |",
                      "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
            for label, values in sorted(cohort["systems"].items()):
                for row in [*values["runs"], {"repeat": "pooled", **values["pooled"]}]:
                    d = row["tool_ms"]
                    task_failures = row["task_execution_failures"] + row["task_fidelity_failures"]
                    lines.append(f"| {label} | {row['repeat']} | {row['completed_task_runs']}/{row['requested_task_runs']} | "
                                 f"{row['completed_tool_calls']}/{row['expected_tool_calls']} | {d['mean']:.2f} | "
                                 f"{d['p95']:.2f} | {d['p99']:.2f} | {task_failures}/{row['unexpected_tool_outcomes']}/"
                                 f"{row['infrastructure_failures']} | {row['expected_nonzero_shell_exits']}/"
                                 f"{row['observed_nonzero_shell_exits']} | {row['absolute_deadline_violations']} |")
            lines.append("")
        lines += ["Expected nonzero shell exits are replayed workload outcomes, not failed tasks. "
                  "Failure columns are task execution/fidelity, unexpected tool outcome, and infrastructure counts. "
                  "No cohort DRAM is inferred from aggregate concurrent memory samples. Full task identities, "
                  "timeout-like shell counts and evidence hashes are in `formal_cohorts` in analysis.json.", ""]
        lines.extend("- " + item for item in cohorts["limitations"])
    lines += ["", "## Memory", "", "Time-weighted cgroup charge, including file cache; these are NOT uniquely attributable host DRAM totals.", "",
        "| System | Sandbox MiB | Daemon-leaf MiB | Service-tree MiB | Service-tree peak MiB | Swap MiB |",
        "|---|---:|---:|---:|---:|---:|"]
    for label, r in sorted(data["systems"].items()):
        m = r["memory"]
        fields = ["sandbox_memory_current_bytes", "task_daemon_cgroup_sum_bytes", "task_service_cgroup_sum_bytes"]
        vals = [m[f]["time_weighted_mean_mib"] for f in fields]
        lines.append(f"| {label} | {vals[0]:.2f} | {vals[1]:.2f} | {vals[2]:.2f} | {m['task_service_cgroup_sum_bytes']['peak_mib']:.2f} | {m['sandbox_memory_swap_bytes']['time_weighted_mean_mib']:.2f} |")
    lines += ["", "## Cold sandbox startup", "", "No ready pool; warm host, same locally prepared repository and conda base. "
        "SandboxFS Manager.Create entry to client-observed first successful normal-API /bin/true command, including "
        "CLI completion and response JSON parsing; socket ingress and initial server request JSON decoding are outside this endpoint.", "",
        "| Mode | Samples | Mean ms | p50 ms | p95 ms | p99 ms | Provision mean ms |",
        "|---|---:|---:|---:|---:|---:|---:|"]
    for mode, r in data["cold"].items():
        d = r["cold_start_ms"]
        lines.append(f"| {mode} | {d['n']} | {d['mean']:.2f} | {d['p50']:.2f} | {d['p95']:.2f} | {d['p99']:.2f} | {r['provision_ms']['mean']:.2f} |")
    if "cold_first_touch" in data:
        touch = data["cold_first_touch"]
        lines += ["", "## Separate post-cold first-touch microprobes", "",
                  "Metadata-selected real tracked file; cache warmth is unspecified. The write follows the read. "
                  "These samples are not added to cold-start or real-tool latency.", "",
                  "| Mode | Probe | Samples | Inner mean ms | Server mean ms | API/RPC mean ms | API/RPC p95 ms |",
                  "|---|---|---:|---:|---:|---:|---:|"]
        for mode, values in touch["modes"].items():
            for action, label in (("read", "Read"), ("write", "Same-byte write + fsync")):
                measured = values[action]
                lines.append(f"| {mode} | {label} | {values['samples']} | "
                             f"{measured['operation_ms']['mean']:.4f} | {measured['server_command_ms']['mean']:.4f} | "
                             f"{measured['client_rpc_ms']['mean']:.4f} | {measured['client_rpc_ms']['p95']:.4f} |")
        lines += ["", "Inner write timing includes writable open/copy-up and fsync durability; complete-file hash "
                  "verification is outside the inner write but inside the server command and API/RPC latency. "
                  "Matched path, size and source SHA-256 are fixed per task across modes and repetitions; device/inode "
                  "need not match. Full per-mode read/write distributions and file identities are in `cold_first_touch` "
                  "in analysis.json.", ""]
        lines.extend("- " + item for item in touch["limitations"])
    fidelity = data.get("fidelity_verification", {})
    lines += ["", "## Replay fidelity verification", "",
              f"Verification mode: `{fidelity.get('mode', 'legacy-not-artifact-verified')}`. "
              f"Strict replay evidence verified: {fidelity.get('formal_evidence_verified', False)}.", ""]
    if fidelity.get("formal_evidence_verified"):
        lines += ["Each registered baseline/treatment repetition was checked against the normalized workload "
                  "and frozen source artifacts. Performance guard outcomes below remain descriptive; "
                  "verification does not turn a regression into a success.", "",
                  f"Aggregate registered performance guards pass: {fidelity['performance_guards_pass']}. "
                  "This requires both treatment-side checkpoint guards and zero baseline absolute-deadline violations; "
                  "the retained raw checkpoint `guards_pass` field alone describes the treatment-side checks.", ""]
        for name, check in fidelity["checks"].items():
            guards_pass = check["guards_pass"] and check["baseline_deadline_violations"] == 0
            lines.append(f"- {name}: {check['matched_tools']} matched tools; "
                         f"planned wait events {check['fidelity']['planned_wait_events']}; "
                         f"observed events per configuration {check['fidelity']['observed_wait_events_per_configuration']}; "
                         f"registered performance guards pass: {guards_pass}; "
                         f"baseline absolute-deadline violations: {check['baseline_deadline_violations']}; "
                         f"treatment absolute-deadline violations: {check['deadline_violations']}.")
            # Preserve every evidence flag and qualification in the durable text
            # as well as the full checkpoint dictionaries in analysis.json.
            lines += ["", "```json", json.dumps(check["fidelity"], indent=2, sort_keys=True), "```", ""]
    else:
        lines += ["This legacy pilot/development analysis is not artifact-verified by the strict checkpoint gate. "
                  "No new strict-gate success is claimed.", ""]
    lines.extend("- " + item for item in fidelity.get("limitations", []))
    lines += ["", "## Qualifications", ""]
    lines.extend("- " + s for s in data["limitations"])
    for label, r in sorted(data["systems"].items()):
        lines.append(f"- {label}: {r['reclaim_events']} reclaim events; {r['speculative_prepared_bytes']} speculative prepared bytes; "
                     f"{r['reclaim_errors']} reclaim errors; {r['speculative_restore_errors']} speculative-restore errors; "
                     f"{r['absolute_deadline_violations']} tool-turn deadline violations; {r['pool_hits']} ready-pool hits / {r['pool_misses']} misses.")
    for label, r in sorted(data["tool_vs_fullcopy"].items()):
        lines.append(f"- {label} / FullCopy: mean {r['mean_ratio']:.3f}x, p95 {r['p95_ratio']:.3f}x, p99 {r['p99_ratio']:.3f}x; "
                     f"pooled relative tail guards pass: {r['both_relative_tail_guards_pass']}. See analysis.json for each paired repetition.")
    lines += ["- All measured configurations must retain the same task IDs, tool hashes, expected exit codes and final source fingerprints.",
              "- Raw signed host deltas, run points, hashes and detailed distributions are in analysis.json. Do not cite the old proxy means as these results.", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--workloads-dir", type=Path,
                    help="local copy of immutable normalized workloads; requires --source-root")
    ap.add_argument("--source-root", type=Path,
                    help="local frozen source tree containing SOURCE_PROVENANCE.json; requires --workloads-dir")
    a = ap.parse_args()
    if (a.workloads_dir is None) != (a.source_root is None):
        ap.error("--workloads-dir and --source-root must be supplied together")
    data = analyze(a.campaign, workloads_dir=a.workloads_dir, source_root=a.source_root)
    a.output.mkdir(parents=True, exist_ok=True)
    (a.output / "analysis.json").write_text(json.dumps(data, indent=2) + "\n")
    (a.output / "RESULTS.md").write_text(markdown(data))
    print(markdown(data))


if __name__ == "__main__":
    main()
