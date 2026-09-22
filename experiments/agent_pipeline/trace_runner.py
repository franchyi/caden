"""Run the mini-agent on one SWE-rebench instance with real cgroup-v2 resource sampling, and emit a
SWE-bench-corpus-style bundle (trace.jsonl, tool_calls.json, resources.json, results.json) under
work/<id>/attempt_1/. Launch via run_trace.sh, which creates the cgroups and exports AGENT_CGROUP
(the agent's own cgroup, sampled) and LLM_CGROUP (sibling cgroup the claude client is moved into, so
LLM_WAIT reads as idle). The agent runs with a wall-clock clock so stage_events carry real epochs."""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from agent_pipeline.agent import Agent
from agent_pipeline.model_claude_cli import ClaudeCliModel
from agent_pipeline.sandbox import BubblewrapEnvironment
from fetch_task import fetch
from resource_sampler import CgroupSampler
from trace_export import write_bundle

ROOT = Path(__file__).parent
MODEL_ID = {"sonnet": "claude-sonnet-4-6", "opus": "claude-opus-4-8", "haiku": "claude-haiku-4-5"}
_EMPTY_RES = {"samples": [], "summary": {"sample_count": 0, "duration_seconds": 0,
              "memory_mb": {"min": 0, "max": 0, "avg": 0},
              "cpu_percent": {"min": 0, "max": 0, "avg": 0}}}


def run(instance_id: str, model_alias: str) -> dict:
    workdir = ROOT / "work" / instance_id
    if (workdir / "task.json").exists():
        task = json.loads((workdir / "task.json").read_text())
    else:
        task = fetch(instance_id, workdir)

    # Reset the repo to a pristine base checkout before the agent runs, so it never sees prior runs'
    # source edits, the applied gold tests, or evaluate()'s leftover .gold_test.patch — any of which
    # would leak the answer. The venv is preserved.
    repo = task["repo_dir"]
    subprocess.run(["git", "checkout", task["base_commit"], "--", "."], cwd=repo,
                   check=False, capture_output=True)
    subprocess.run(["git", "clean", "-fd", "-e", ".venv", "-e", "*.egg-info"], cwd=repo,
                   check=False, capture_output=True)

    venv_bin = str(Path(task["venv_python"]).parent)
    venv_root = str(Path(task["venv_python"]).parent.parent)
    env = BubblewrapEnvironment(
        repo=task["repo_dir"], timeout=int(os.environ.get("CMD_TIMEOUT", "180")), env={
            "PATH": f"{venv_bin}:/usr/local/bin:/usr/bin:/bin",
            "VIRTUAL_ENV": venv_root, "HOME": "/tmp"})
    # claude -p accepts the alias (sonnet/opus); bump the per-call timeout (default 300s is tight for
    # large issues like datasette) and allow override via CLAUDE_TIMEOUT.
    model = ClaudeCliModel(model=model_alias,
                           timeout=int(os.environ.get("CLAUDE_TIMEOUT", "300")),
                           retries=int(os.environ.get("CLAUDE_RETRIES", "5")))
    agent = Agent(model=model, env=env, step_limit=int(os.environ.get("STEP_LIMIT", "40")),
                  wall_limit=float(os.environ.get("WALL_LIMIT", "1200")), clock=time.time)

    agent_cgroup = os.environ.get("AGENT_CGROUP")
    sampler = CgroupSampler(agent_cgroup, float(os.environ.get("SAMPLE_INTERVAL", "1.0"))) \
        if agent_cgroup else None

    start_iso = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    if sampler:
        sampler.start()
    agent_result = agent.run(task["problem_statement"])
    if sampler:
        sampler.stop()
    wall = time.time() - t0
    resources = sampler.to_resources() if sampler else _EMPTY_RES

    try:
        from evaluate import evaluate
        verdict = evaluate(task)
    except Exception as e:
        verdict = {"error": str(e)}

    def stage_total(name):
        return sum(e["end"] - e["start"] for e in agent_result["stage_events"] if e["stage"] == name)

    claude_time = sum((c.get("duration_ms") or 0) for c in model.calls) / 1000.0
    cost = sum((c.get("total_cost_usd") or 0) for c in model.calls)
    model_id = MODEL_ID.get(model_alias, model_alias)

    results = {
        "instance_id": instance_id, "model": model_id, "model_alias": model_alias,
        "start_time": start_iso, "end_time": datetime.now(timezone.utc).isoformat(),
        "total_time": round(wall, 1), "claude_time": round(claude_time, 1),
        "exit_status": agent_result["exit_status"], "steps": agent_result["steps"],
        "resolved": (verdict or {}).get("resolved"), "verdict": verdict,
        "stage_seconds": {"LLM_WAIT": round(stage_total("LLM_WAIT"), 1),
                          "TOOL_BURST": round(stage_total("TOOL_BURST"), 1)},
        "total_cost_usd": round(cost, 4), "num_llm_calls": len(model.calls),
        "resource_summary": resources["summary"], "cgroup": agent_cgroup,
        "memory_limit": None, "cpu_limit": None, "image": None,
    }

    info = write_bundle(workdir / "attempt_1", agent_result=agent_result, model_calls=model.calls,
                        model_name=model_id, instance_id=instance_id, resources=resources,
                        results=results)
    print(json.dumps({"instance_id": instance_id, "model": model_id,
                      "resolved": results["resolved"], "steps": results["steps"],
                      "exit_status": results["exit_status"], "total_time": results["total_time"],
                      "claude_time": results["claude_time"], "cost_usd": results["total_cost_usd"],
                      "stage_seconds": results["stage_seconds"], **info,
                      "out_dir": str(workdir / "attempt_1")}, indent=2))
    return results


if __name__ == "__main__":
    inst = sys.argv[1]
    alias = sys.argv[2] if len(sys.argv) > 2 else (os.environ.get("AGENT_MODEL") or "sonnet")
    run(inst, alias)
