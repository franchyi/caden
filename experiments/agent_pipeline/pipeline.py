"""Orchestrate: fetch -> sandboxed agent run -> evaluate -> results/<id>.json.
Run on nsl7s."""

import json
import os
import sys
import time
from pathlib import Path

from agent_pipeline.agent import Agent
from agent_pipeline.model_claude_cli import ClaudeCliModel
from agent_pipeline.sandbox import BubblewrapEnvironment
from evaluate import evaluate
from fetch_task import fetch

ROOT = Path(__file__).parent


def run_instance(instance_id: str) -> dict:
    workdir = ROOT / "work" / instance_id
    if (workdir / "task.json").exists():
        task = json.loads((workdir / "task.json").read_text())
    else:
        task = fetch(instance_id, workdir)

    venv_bin = str(Path(task["venv_python"]).parent)
    venv_root = str(Path(task["venv_python"]).parent.parent)
    env = BubblewrapEnvironment(repo=task["repo_dir"], env={
        "PATH": f"{venv_bin}:/usr/local/bin:/usr/bin:/bin",
        "VIRTUAL_ENV": venv_root,
        "HOME": "/tmp",
    })
    model = ClaudeCliModel(model=os.environ.get("AGENT_MODEL") or None)
    agent = Agent(model=model, env=env,
                  step_limit=int(os.environ.get("STEP_LIMIT", "40")),
                  clock=time.monotonic)

    t0 = time.monotonic()
    agent_result = agent.run(task["problem_statement"])
    wall = time.monotonic() - t0

    verdict = evaluate(task)

    def stage_total(name):
        return sum(e["end"] - e["start"] for e in agent_result["stage_events"]
                   if e["stage"] == name)

    summary = {
        "instance_id": instance_id,
        "resolved": verdict["resolved"],
        "verdict": verdict,
        "exit_status": agent_result["exit_status"],
        "steps": agent_result["steps"],
        "wall_seconds": round(wall, 1),
        "stage_seconds": {"LLM_WAIT": round(stage_total("LLM_WAIT"), 1),
                          "TOOL_BURST": round(stage_total("TOOL_BURST"), 1)},
    }
    res_dir = ROOT / "results"
    res_dir.mkdir(exist_ok=True)
    (res_dir / f"{instance_id}.json").write_text(json.dumps(
        {**summary, "stage_events": agent_result["stage_events"],
         "messages": agent_result["messages"]}, indent=2))
    print(json.dumps(summary, indent=2))
    return summary


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "tasks.txt"
    p = ROOT / arg
    instance_ids = [ln.strip() for ln in p.read_text().splitlines()
                    if ln.strip() and not ln.startswith("#")] if p.exists() else [arg]
    for instance_id in instance_ids:
        run_instance(instance_id)


if __name__ == "__main__":
    main()
