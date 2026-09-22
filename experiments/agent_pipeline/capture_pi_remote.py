#!/usr/bin/env python3
"""Capture a no-tools Pi mini-agent trace against a remote SandboxFS sandbox."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_pipeline.agent import Agent
from agent_pipeline.model_pi_cli import PiCliModel
from agent_pipeline.remote_sandboxfs import RemoteSandboxFSEnvironment
from trace_export import write_bundle

_EMPTY_RESOURCES = {
    "samples": [],
    "summary": {
        "sample_count": 0,
        "duration_seconds": 0,
        "memory_mb": {"min": 0, "max": 0, "avg": 0},
        "cpu_percent": {"min": 0, "max": 0, "avg": 0},
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--ctl", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--sandbox-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--grader-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--provider", default="openai-codex")
    parser.add_argument("--model", default="gpt-5.6-terra")
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--step-limit", type=int, default=12)
    parser.add_argument("--wall-limit", type=float, default=1200)
    parser.add_argument("--model-timeout", type=int, default=300)
    parser.add_argument("--command-timeout", type=int, default=180)
    return parser.parse_args()


def remote_ctl(args: argparse.Namespace, *ctl_args: str) -> dict[str, Any]:
    remote = shlex.join([args.ctl, "--socket", args.socket, *ctl_args])
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", args.host, remote],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"remote sandboxfsctl failed ({completed.returncode}): "
            f"{(completed.stdout + completed.stderr).strip()}"
        )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise TypeError("remote sandboxfsctl returned non-object JSON")
    return value


def main() -> int:
    args = parse_args()
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise SystemExit(f"refusing non-empty output directory: {args.output_dir}")
    task = args.task_file.read_text()
    grader = args.grader_file.read_text()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    create_receipt: dict[str, Any] | None = None
    destroy_receipt: dict[str, Any] | None = None
    started = datetime.now(UTC).isoformat()
    try:
        create_receipt = remote_ctl(
            args,
            "create",
            "--id",
            args.sandbox_id,
            "--base",
            args.base,
            "--mode",
            "t1",
        )
        environment = RemoteSandboxFSEnvironment(
            host=args.host,
            ctl=args.ctl,
            socket=args.socket,
            sandbox_id=args.sandbox_id,
            timeout=args.command_timeout,
        )
        model = PiCliModel(
            provider=args.provider,
            model=args.model,
            thinking=args.thinking,
            timeout=args.model_timeout,
        )
        agent = Agent(
            model=model,
            env=environment,
            step_limit=args.step_limit,
            wall_limit=args.wall_limit,
            clock=time.time,
        )
        monotonic_started = time.monotonic()
        agent_result = agent.run(task)
        wall_seconds = time.monotonic() - monotonic_started
        grader_result = environment.execute(grader)
        resolved = (
            agent_result["exit_status"] == "submitted"
            and grader_result["returncode"] == 0
        )
        def stage_total(stage: str) -> float:
            return sum(
                event["end"] - event["start"]
                for event in agent_result["stage_events"]
                if event["stage"] == stage
            )

        results = {
            "schema": "crate-pi-agent-trace-v1",
            "instance_id": args.instance_id,
            "provider": args.provider,
            "model": args.model,
            "thinking": args.thinking,
            "start_time": started,
            "end_time": datetime.now(UTC).isoformat(),
            "total_time": wall_seconds,
            "exit_status": agent_result["exit_status"],
            "steps": agent_result["steps"],
            "resolved": resolved,
            "grader": grader_result,
            "stage_seconds": {
                "LLM_WAIT": stage_total("LLM_WAIT"),
                "TOOL_BURST": stage_total("TOOL_BURST"),
            },
            "total_cost_usd": sum(
                float(call.get("total_cost_usd") or 0) for call in model.calls
            ),
            "num_llm_calls": len(model.calls),
            "model_calls": model.calls,
            "remote": {
                "host": args.host,
                "socket": args.socket,
                "base": args.base,
                "sandbox_id": args.sandbox_id,
                "create_receipt": create_receipt,
            },
            "resource_summary": _EMPTY_RESOURCES["summary"],
        }
        write_bundle(
            args.output_dir,
            agent_result=agent_result,
            model_calls=model.calls,
            model_name=f"{args.provider}/{args.model}",
            instance_id=args.instance_id,
            resources=_EMPTY_RESOURCES,
            results=results,
        )
        (args.output_dir / "agent_result.json").write_text(
            json.dumps(agent_result, indent=2) + "\n"
        )
    finally:
        if create_receipt is not None:
            destroy_receipt = remote_ctl(args, "destroy", args.sandbox_id)
            (args.output_dir / "destroy_receipt.json").write_text(
                json.dumps(destroy_receipt, indent=2) + "\n"
            )
    result_path = args.output_dir / "results.json"
    result = json.loads(result_path.read_text())
    print(
        json.dumps(
            {
                "instance_id": result["instance_id"],
                "provider": result["provider"],
                "model": result["model"],
                "resolved": result["resolved"],
                "steps": result["steps"],
                "stage_seconds": result["stage_seconds"],
                "out_dir": str(args.output_dir),
            },
            indent=2,
        )
    )
    return 0 if result["resolved"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
