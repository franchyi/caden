#!/usr/bin/env python3
"""Lossless command conversion, with captured waits and expected error codes."""
import argparse
import hashlib
import json
from pathlib import Path


def convert(capture):
    if capture.get("schema") != "crate-verified-real-capture-v1" or capture.get("error"):
        raise ValueError("incomplete capture; preserve but do not silently benchmark")
    commands = iter(capture["commands"])
    events, pending = [], 0.0
    for stage in capture["agent"]["stage_events"]:
        if stage["stage"] == "LLM_WAIT":
            pending += 1000 * (stage["end"] - stage["start"])
        elif stage["stage"] == "TOOL_BURST":
            c = next(commands)
            if pending:
                events.append({"type": "wait", "duration_ms": pending,
                               "request_class": capture["task"]["family"], "restore_profile": "default"})
            pending = 0.0
            events.append({"type": "tool", "operation": "original-command", "source_tool": "bash",
                           "sequence": c["sequence"], "argv": c["argv"],
                           "source_arguments_sha256": hashlib.sha256(c["command"].encode()).hexdigest(),
                           "expected_exit_code": c["response"]["exit_code"]})
    if next(commands, None) is not None:
        raise ValueError("commands/stages mismatch")
    if pending:
        events.append({"type": "wait", "duration_ms": pending,
                       "request_class": capture["task"]["family"], "restore_profile": "default"})
    if not capture["commands"]:
        raise ValueError("no commands captured")
    return {"schema": "caden-tool-trajectory-v1", "base": capture["task"]["base"],
            "source": {"trajectory_id": capture["task"]["instance_id"],
                       "instance_id": capture["task"]["instance_id"], "repo": capture["task"]["repo"]},
            "tool_execution": {"kind": "original-commands", "proxy": False,
                               "anonymous_memory_injection": False},
            "tool_count": len(capture["commands"]), "events": events,
            "fingerprint_expected": json.loads(capture["fingerprint"]["stdout"]),
            "capture_exit_status": capture["agent"]["exit_status"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--captures", type=Path, required=True)
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--limit", type=int, required=True)
    a = ap.parse_args()
    if a.output.exists():
        raise SystemExit("refusing existing normalized workload")
    a.output.mkdir(parents=True)
    selection = json.loads(a.selection.read_text())
    manifest = {"schema": "caden-tool-trajectory-manifest-v1",
                "source": {"dataset": selection["dataset"], "revision": selection["revision"]},
                "conversion": {"kind": "original-commands", "wait_scale": 1.0, "success_filter": False},
                "workloads": []}
    for task in selection["tasks"][:a.limit]:
        source = a.captures / task["instance_id"] / "capture.json"
        workload = convert(json.loads(source.read_text()))
        payload = (json.dumps(workload, indent=2) + "\n").encode()
        name = task["instance_id"] + ".json"
        (a.output / name).write_bytes(payload)
        manifest["workloads"].append({"path": name, "sha256": hashlib.sha256(payload).hexdigest(),
            "capture_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "tool_count": workload["tool_count"]})
    (a.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
