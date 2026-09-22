#!/usr/bin/env python3
"""Capture real commands and all outcomes; no gold solution and no success filter."""
import argparse
import hashlib
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/agent_pipeline"))
from agent_pipeline.agent import Agent
from agent_pipeline.model_pi_cli import PiCliModel
from agent_pipeline import prompts

PREFIX = "export PATH=/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/bin:/usr/bin:/bin; export PYTHONHASHSEED=0 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 USER=chaoyi LOGNAME=chaoyi; cd /testbed && "
FINGERPRINT = """import hashlib,json,os,subprocess
g=['git','-c','safe.directory=/workspace/repository']
d=subprocess.check_output(g+['diff','--binary','HEAD'])
u=subprocess.check_output(g+['ls-files','--others','--exclude-standard','-z']).split(b'\\0')
files={}
for p in sorted(u):
 if p and os.path.isfile(p):
  with open(p,'rb') as f: files[os.fsdecode(p)]=hashlib.sha256(f.read()).hexdigest()
print(json.dumps({'diff_sha256':hashlib.sha256(d).hexdigest(),'untracked':files},sort_keys=True))
"""


def argv_for(command):
    return ["/usr/bin/timeout", "--signal=TERM", "--kill-after=5s", "180s", "/bin/bash", "-c", PREFIX + command]


class Environment:
    def __init__(self, task, out, host="nsl17"):
        self.task, self.out, self.host = task, out, host
        self.sandbox = "sv-capture-" + f"{task['sequence']:02d}"
        self.commands = []

    def ctl(self, *args):
        remote = shlex.join(["sudo", "-n", "/sandboxfs/crate-swebench-20260919/bin/sandboxfsctl",
                             "--socket", self.task["socket"], "--timeout", "240s", *args])
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.host, remote],
                           capture_output=True, text=True, timeout=260)
        try:
            response = json.loads(p.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"control RPC failed ({p.returncode}): {p.stderr[:1000]}")
        if p.returncode and "exit_code" not in response:
            raise RuntimeError(f"control RPC failed: {response}")
        return response

    def execute(self, command):
        argv = argv_for(command)
        t = time.monotonic_ns()
        with (self.out / "command-attempts.jsonl").open("a") as f:
            f.write(json.dumps({"sequence": len(self.commands), "command": command,
                                "argv": argv, "started_monotonic_ns": t}) + "\n")
        response = self.ctl("exec-json", self.sandbox, "--", *argv)
        record = {"sequence": len(self.commands), "command": command, "argv": argv,
                  "wall_ns_including_ssh": time.monotonic_ns()-t, "response": response}
        self.commands.append(record)
        with (self.out / "commands.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
        return {"output": response.get("stdout", "") + response.get("stderr", ""),
                "returncode": response["exit_code"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--instance-id", required=True)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    if a.output.exists():
        raise SystemExit("refusing existing capture output; retain failures separately")
    a.output.mkdir(parents=True)
    manifest = json.loads((a.selection / "manifest.json").read_text())
    task = next(t for t in manifest["tasks"] if t["instance_id"] == a.instance_id)
    env = Environment(task, a.output)
    model = PiCliModel(timeout=240, retries=2)
    # One persistent sandbox, fresh shell per command; correct the old bwrap prompt.
    prompts.SYSTEM = prompts.SYSTEM.replace("each response runs in a fresh\n  sandbox back at the repository root.",
        "each response uses a fresh shell in the SAME sandbox; files and /tmp persist.")
    prompts.render_instance = lambda issue: (
        "Solve this GitHub issue in the pinned repository:\n\n" + issue +
        "\n\nThe official task environment is active: python and test dependencies are on PATH. "
        "The sandbox has no network. Do not install dependencies or alter tests. Inspect relevant source, "
        "implement a fix, and run targeted existing tests or a focused reproduction. Avoid running an "
        "entire large project suite: each command has a 180-second deadline. Files and /tmp persist "
        "between commands, but each command begins in /testbed with a fresh shell. "
        "Do not read or search for hidden evaluation data. Finish with echo " + prompts.SUBMIT_SENTINEL)
    (a.output / "prompt-system.txt").write_text(prompts.SYSTEM)
    issue = (a.selection / "tasks" / a.instance_id / "task.txt").read_text()
    result = {"schema": "crate-verified-real-capture-v1", "task": task,
              "capture_protocol_version": 2, "command_deadline_seconds": 180,
              "dataset_revision": manifest["revision"], "provider": model.provider,
              "model": model.model, "thinking": model.thinking,
              "resolved": None, "grading": "not evaluated; submission is not success",
              "started_unix": time.time()}
    created = False
    try:
        result["create"] = env.ctl("create", "--id", env.sandbox, "--base", task["base"], "--mode", "t1")
        created = True
        agent = Agent(model=model, env=env, step_limit=20, wall_limit=1200, clock=time.monotonic)
        result["agent"] = agent.run(issue)
        result["final_diff"] = env.ctl("exec-json", env.sandbox, "--", *argv_for("git -c safe.directory=/workspace/repository diff --binary HEAD"))
        result["fingerprint"] = env.ctl("exec-json", env.sandbox, "--", *argv_for("python -c " + shlex.quote(FINGERPRINT)))
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if "agent" in locals() and "agent" not in result:
            result["agent"] = agent._result("infrastructure_error", len(env.commands))
        result["commands"] = env.commands
        result["model_calls"] = model.calls
        result["ended_unix"] = time.time()
        if created:
            try:
                result["destroy"] = env.ctl("destroy", env.sandbox)
            except Exception as error:
                result["cleanup_error"] = str(error)
        (a.output / "capture.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"instance_id": a.instance_id, "commands": len(env.commands),
                      "exit_status": result["agent"]["exit_status"]}))


if __name__ == "__main__":
    main()
