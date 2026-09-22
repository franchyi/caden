"""Fail-closed, same-work paired checkpoints; not a final inferential analysis.

Real replay reports require the immutable normalized workloads and source tree.
Path overrides support copies of remote artifacts without changing the reports.
Tiny schema-less unit fixtures retain metric-only compatibility and are labeled
as unverified: they are not suitable for a formal campaign acceptance gate.
"""
import hashlib
import json
import math
from pathlib import Path


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _hash(payload):
    return hashlib.sha256(payload).hexdigest()


def _safe_file(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    _require(root in path.parents, f"unsafe evidence path: {relative}")
    return path


def _nonnegative(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _source(report, source_root):
    commits = report["commits"]
    hashes = commits["source_sha256"]
    _require(isinstance(hashes, dict) and hashes, "missing frozen source file manifest")
    _require(_hash(json.dumps(hashes, sort_keys=True).encode()) == commits["source_manifest_sha256"],
             "source manifest hash mismatch")
    root = Path(source_root) if source_root is not None else Path(report["config"]["source_provenance"]).parent
    provenance = json.loads((root / "SOURCE_PROVENANCE.json").read_text())
    _require(provenance == commits, "report differs from frozen source provenance")
    for relative, expected in hashes.items():
        _require(_hash(_safe_file(root, relative).read_bytes()) == expected,
                 f"frozen source drift: {relative}")
    return commits


def _workloads(report, workloads_dir):
    root = Path(workloads_dir) if workloads_dir is not None else Path(report["config"]["workloads_dir"])
    payload = (root / "manifest.json").read_bytes()
    _require(_hash(payload) == report["workload"]["manifest_sha256"], "workload manifest hash mismatch")
    manifest = json.loads(payload)
    _require(manifest["schema"] in {"caden-tool-trajectory-manifest-v1", "orca-tool-trajectory-manifest-v1"}, "invalid workload manifest schema")
    _require(manifest["source"] == report["workload"]["source"] and
             manifest["conversion"] == report["workload"]["conversion"], "workload provenance mismatch")
    _require(manifest["conversion"].get("wait_scale") == 1 and
             manifest["conversion"].get("success_filter") is False and
             manifest["conversion"].get("kind") == "original-commands", "changed workload conversion")
    entries = manifest["workloads"]
    _require(isinstance(entries, list) and entries, "empty workload manifest")
    limit = report["config"]["requests"]
    _require(type(limit) is int and limit >= 0, "invalid requested task count")
    selected = entries[:limit] if limit else entries
    _require(not limit or len(selected) == limit, "incomplete requested workload")
    workloads = []
    for entry in selected:
        payload = _safe_file(root, entry["path"]).read_bytes()
        _require(_hash(payload) == entry["sha256"], f"workload checksum mismatch: {entry['path']}")
        workload = json.loads(payload)
        _require(workload["schema"] in {"caden-tool-trajectory-v1", "orca-tool-trajectory-v1"}, "invalid task workload schema")
        _require(workload["tool_execution"].get("kind") == "original-commands" and
                 workload["tool_execution"].get("proxy") is False and
                 workload["tool_execution"].get("anonymous_memory_injection") is False,
                 "changed tool execution contract")
        _require(workload["tool_count"] == entry["tool_count"], "manifest tool count mismatch")
        workloads.append(workload)
    if manifest.get("orchestration"):
        from experiments.swebench_verified.session_workloads import validate_arrivals
        validate_arrivals(workloads, manifest, root)
        _require(report["config"].get("arrival_driven") is True, "serving workload used wave replay")
        _require(report["workload"].get("orchestration") == manifest["orchestration"],
                 "changed orchestration metadata")
    return manifest, workloads


def _fingerprint(replay):
    result = replay["fingerprint"]
    _require(result["exit_code"] == 0, "failed final workspace fingerprint")
    value = json.loads(result["stdout"])
    _require(isinstance(value, dict) and isinstance(value.get("diff_sha256"), str) and
             isinstance(value.get("untracked"), dict), "invalid final workspace fingerprint")
    return value


def _timing(tool, strict):
    _require(_nonnegative(tool["turn_ns"]) and tool["turn_ns"] > 0, "invalid tool latency")
    components = ("wake_restore_ns", "command_ns", "exec_rpc_ns", "server_command_ns",
                  "result_validation_ns", "result_pack_ns")
    if strict or any(field in tool for field in components):
        _require(all(_nonnegative(tool[field]) for field in components), "invalid timing component")
        _require(tool["turn_ns"] == tool["wake_restore_ns"] + tool["command_ns"], "latency endpoint mismatch")
        _require(tool["server_command_ns"] <= tool["exec_rpc_ns"], "server duration exceeds client RPC")
        _require(tool["command_ns"] == tool["exec_rpc_ns"] + tool["result_validation_ns"] +
                 tool["result_pack_ns"], "latency decomposition mismatch")


def _rows(report, strict, workloads_dir, source_root):
    _require(report["summary"]["success"] is True and not report["summary"].get("errors") and
             report["config"]["wait_scale"] == 1, "failed replay or changed waits")
    manifest, workloads, commits = None, None, None
    if strict:
        _require(report["schema"] in {"caden-trajectory-replay-v1", "orca-trajectory-replay-v1"}, "missing or invalid replay schema")
        commits = _source(report, source_root)
        manifest, workloads = _workloads(report, workloads_dir)
        expected_count = len(workloads)
        _require(expected_count == len(report["replays"]) == len(report["requests"]) ==
                 report["workload"]["workload_count"] == report["summary"]["requests"] ==
                 report["summary"]["completed_requests"], "incomplete task count")
    signature, tools, tasks, identities, request_signature = [], [], {}, set(), []
    planned_waits = observed_waits = 0
    for index, replay in enumerate(report["replays"]):
        _require(not replay.get("error") and not replay.get("fidelity_errors"), "fidelity error")
        task = replay["trajectory_id"]
        _require(isinstance(task, str) and task and task not in identities, "duplicate or invalid task identity")
        identities.add(task)
        planned = None
        if strict:
            workload, request = workloads[index], report["requests"][index]
            _require(replay["sequence"] == request["sequence"] == index, "changed request/replay order")
            _require(task == request["trajectory_id"] == workload["source"]["trajectory_id"] and
                     request["instance_id"] == workload["source"]["instance_id"] and
                     request["base"] == workload["base"] and
                     replay["sandbox_id"] == request["sandbox_id"], "changed task/base identity")
            for key in ("wave", "wave_position"):
                if key in replay:
                    _require(request.get(key) == replay[key], f"changed request/replay {key}")
            request_signature.append({key: request[key] for key in
                ("sequence", "trajectory_id", "instance_id", "base", "wave", "wave_position",
                 "arrival_offset_ms", "replica") if key in request})
            if report["config"].get("arrival_driven"):
                _require(request["arrival_offset_ms"] == workload["arrival_offset_ms"], "changed arrival schedule")
                _require(request["planned_arrival_ns"] == request["serving_origin_ns"] +
                         round(workload["arrival_offset_ms"] * 1e6), "invalid arrival clock")
                _require(request["ready_ns"] - request["planned_arrival_ns"] == request["request_to_ready_ns"],
                         "admission queue omitted from request-to-ready")
            _require(all(event["type"] in {"tool", "wait"} for event in workload["events"]), "unknown workload event")
            planned = [event for event in workload["events"] if event["type"] == "tool"]
            waits = [event for event in workload["events"] if event["type"] == "wait"]
            _require(len(planned) == len(replay["tools"]) == workload["tool_count"], "incomplete task tool count")
            _require(all(_nonnegative(event["duration_ms"]) for event in waits), "invalid planned wait")
            elapsed_waits = replay.get("waits_ns", [])
            _require(isinstance(elapsed_waits, list) and len(elapsed_waits) == len(waits) and
                     all(_nonnegative(value) for value in elapsed_waits), "changed wait-event count or invalid wait")
            planned_waits += len(waits)
            if "requested_waits_ms" in replay:
                _require(replay["requested_waits_ms"] == [event["duration_ms"] for event in waits],
                         "runtime requested waits differ from trace")
            observed_waits += len(elapsed_waits)
            _require(_fingerprint(replay) == workload["fingerprint_expected"], "unexpected final workspace fingerprint")
        elif "fingerprint" in replay:
            _fingerprint(replay)
        task_signature, task_tools, seen = [], [], set()
        for number, tool in enumerate(replay["tools"]):
            _require(tool["exit_code"] == tool["expected_exit_code"], "changed command outcome")
            _require(type(tool["sequence"]) is int and tool["sequence"] >= 0 and tool["sequence"] not in seen,
                     "duplicate or invalid task/tool key")
            seen.add(tool["sequence"])
            _timing(tool, strict)
            fields = ("sequence", "source_arguments_sha256", "expected_exit_code", "source_tool", "operation", "argv")
            if planned is not None:
                event = planned[number]
                _require(all(tool[field] == event[field] for field in fields if field != "argv"),
                         "unequal work: executed tool differs from planned tool")
                if "argv" in tool:
                    _require(tool["argv"] == event["argv"], "unequal work: changed argv")
            task_signature.append({field: tool[field] for field in fields if field in tool})
            tools.append(tool)
            task_tools.append(tool)
        _require(task_tools, "empty task tool population")
        detail = {"task": task, "tools": task_signature}
        for key in ("sequence", "wave", "planned_waits", "wait_events"):
            if key in replay:
                detail[key] = replay[key]
        if "fingerprint" in replay:
            detail["fingerprint"] = _fingerprint(replay)
        if not strict and "waits_ns" in replay:
            detail["observed_wait_count"] = len(replay["waits_ns"])
        signature.append(detail)
        tasks[task] = task_tools
    _require(tools, "empty tool population")
    for field in ("expected_tool_calls", "completed_tool_calls"):
        if strict or field in report["summary"]:
            _require(report["summary"][field] == len(tools), "incomplete completed tool count")
    return {"signature": signature, "tools": tools, "tasks": tasks, "manifest": manifest,
            "workloads": workloads, "commits": commits, "planned_waits": planned_waits,
            "observed_waits": observed_waits, "request_signature": request_signature}


def quantile(values, fraction):
    values = sorted(values)
    if not values:
        raise ValueError("empty tools")
    return values[max(0, math.ceil(len(values) * fraction) - 1)]


def compare(baseline, treatment, *, workloads_dir=None, source_root=None):
    """Compare same-revision reports; overrides point at unchanged local copies.

    Elapsed sleeps include scheduling jitter and are not required to be equal.
    The exact planned interleaving/durations are verified from hash-checked
    workloads, while runtime reports independently establish wait-event counts.
    """
    strict = any((set(report) - {"summary", "config", "workload", "replays"}) or
                 (set(report.get("config", {})) - {"wait_scale"}) or
                 (set(report.get("summary", {})) - {"success"}) or
                 (set(report.get("workload", {})) - {"manifest_sha256"})
                 for report in (baseline, treatment))
    try:
        old = _rows(baseline, strict, workloads_dir, source_root)
        new = _rows(treatment, strict, workloads_dir, source_root)
        _require(old["signature"] == new["signature"] and baseline["workload"] == treatment["workload"], "unequal work")
        _require(old["manifest"] == new["manifest"] and old["workloads"] == new["workloads"], "unequal workload manifests")
        _require(old["request_signature"] == new["request_signature"], "unequal request/wave signatures")
        _require(old["commits"] == new["commits"], "unequal frozen source manifests")
        for field in ("requests", "active_sandboxes", "synchronize_response_commits", "drop_caches"):
            _require(baseline["config"].get(field) == treatment["config"].get(field), f"changed fixed workload setting: {field}")
    except (KeyError, TypeError, OSError, IndexError) as error:
        raise ValueError(f"missing or invalid checkpoint evidence: {error}") from error
    bt, tt = old["tools"], new["tools"]
    b = [t["turn_ns"] for t in bt]
    t = [t["turn_ns"] for t in tt]
    ratios = {f"p{p}_ratio": quantile(t, p / 100) / quantile(b, p / 100) for p in (50, 95, 99)}
    deadlines = sum(v > 180_000_000_000 for v in t)
    result = {"status": "paired checkpoint diagnostic; not proof of non-inferiority",
            "matched_tools": len(bt), "mean_ratio": sum(t) / sum(b), **ratios,
            "deadline_violations": deadlines,
            "guards_pass": ratios["p95_ratio"] <= 1.10 and ratios["p99_ratio"] <= 1.10 and deadlines == 0,
            "baseline_deadline_violations": sum(v > 180_000_000_000 for v in b)}
    result["fidelity"] = {"mode": "artifact-verified" if strict else "minimal-fixture-unverified",
        "formal_evidence_verified": strict,
        "source_files_verified": strict, "normalized_workload_files_verified": strict,
        "final_fingerprints_verified_against_workload": strict,
        "planned_wait_events": old["planned_waits"] if strict else None,
        "observed_wait_events_per_configuration": old["observed_waits"] if strict else None,
        "runtime_requested_wait_sequence_logged": False,
        "raw_capture_files_verified": False,
        "limitations": ["Elapsed sleep times include scheduler jitter; exact planned waits are verified from immutable workloads, not independently logged requested waits.",
                        "Raw captures are identified by normalized-manifest hashes but are not reread by this gate."] if strict else
                       ["Minimal fixture lacks full source/workload/fingerprint evidence and cannot justify formal acceptance."]}
    result["per_task"] = {}
    for task, baseline_tools in old["tasks"].items():
        base_values = [tool["turn_ns"] for tool in baseline_tools]
        treatment_values = [tool["turn_ns"] for tool in new["tasks"][task]]
        result["per_task"][task] = {"calls": len(base_values),
            "mean_ratio": sum(treatment_values) / sum(base_values),
            **{f"p{p}_ratio": quantile(treatment_values, p / 100) / quantile(base_values, p / 100)
               for p in (50, 95, 99)}}
    for name, report in (("baseline", baseline), ("treatment", treatment)):
        samples = report.get("samples", [])
        metrics = {}
        for field in ("task_service_cgroup_sum_bytes", "task_service_anon_bytes",
                      "task_service_file_bytes", "task_service_kernel_bytes"):
            area, duration = 0, 0
            for left, right in zip(samples, samples[1:]):
                if left["phase"] in {"pool_prepare", "create", "trace_replay"} and field in left:
                    dt = right["monotonic_ns"] - left["monotonic_ns"]
                    if dt <= 0:
                        raise ValueError("invalid sample timestamps")
                    area += left[field] * dt
                    duration += dt
            if duration:
                metrics[field] = area / duration / 2**20
        result[name + "_memory_charge_mean_mib"] = metrics
        result[name + "_diagnostics"] = {key: report["summary"].get(key) for key in
            ("pool_hits", "pool_misses", "reclaim_events", "reclaim_errors",
             "speculative_prepared_bytes", "completed_turns_per_second")}
    return result
