"""Serialize a finished agent run into the SWE-bench trace-corpus bundle: trace.jsonl (summary /
user / assistant events with text + Bash tool_use / tool_result blocks), tool_calls.json, and
results.json. The mini-agent's loop maps 1:1 onto assistant(tool_use)->user(tool_result) pairs;
timestamps come from the agent's stage_events (run with a wall-clock clock)."""

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent_pipeline.parsing import FormatError, parse_action
from agent_pipeline.prompts import SUBMIT_SENTINEL

_BASH_BLOCK = re.compile(r"```bash\s*\n.*?```", re.DOTALL)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _uid() -> str:
    return str(uuid.uuid4())


def build_trace(agent_result: dict, model_calls: list[dict], model_name: str,
                instance_id: str) -> tuple[list[dict], list[dict]]:
    messages = agent_result["messages"]
    llm_by_step, tb_by_step = {}, {}
    for e in agent_result["stage_events"]:
        (llm_by_step if e["stage"] == "LLM_WAIT" else tb_by_step)[e["step"]] = e

    session_id, leaf = _uid(), _uid()
    events: list[dict] = [{"type": "summary", "summary": f"agent-pipeline: {instance_id}",
                           "leafUuid": leaf}]
    tool_calls: list[dict] = []

    def base(ev_type, epoch, message, parent):
        return {"type": ev_type, "uuid": _uid(), "parentUuid": parent,
                "isSidechain": False, "sessionId": session_id, "userType": "external",
                "timestamp": _iso(epoch), "message": message}

    # Initial user prompt (messages[0]=system, messages[1]=user instance).
    t_start = llm_by_step[0]["start"]
    u0 = base("user", t_start, {"role": "user", "content": messages[1]["content"]}, None)
    events.append(u0)
    parent = u0["uuid"]

    step, mi = 0, 2
    while mi < len(messages):
        reply = messages[mi]["content"]
        mi += 1
        llm = llm_by_step.get(step)
        t_asst = llm["end"] if llm else t_start
        usage = model_calls[step].get("usage") if step < len(model_calls) else None

        try:
            command = parse_action(reply)
        except FormatError:
            command = None

        if command is None:  # format error: text-only assistant, then the reprompt user message
            a = base("assistant", t_asst, {"id": f"msg_{_uid()[:24]}", "role": "assistant",
                     "model": model_name, "stop_reason": "end_turn", "stop_sequence": None,
                     "type": "message", "content": [{"type": "text", "text": reply}],
                     "usage": usage}, parent)
            events.append(a); parent = a["uuid"]
            if mi < len(messages) and messages[mi]["role"] == "user":
                fu = base("user", t_asst, {"role": "user", "content": messages[mi]["content"]}, parent)
                mi += 1; events.append(fu); parent = fu["uuid"]
            step += 1
            continue

        thought = _BASH_BLOCK.sub("", reply).strip() or "(running command)"
        tool_id = f"toolu_{_uid().replace('-', '')[:24]}"
        a = base("assistant", t_asst, {"id": f"msg_{_uid()[:24]}", "role": "assistant",
                 "model": model_name, "stop_reason": "tool_use", "stop_sequence": None,
                 "type": "message", "usage": usage, "content": [
                     {"type": "text", "text": thought},
                     {"type": "tool_use", "id": tool_id, "name": "Bash",
                      "input": {"command": command}}]}, parent)
        events.append(a); parent = a["uuid"]

        tb = tb_by_step.get(step)
        t_obs = tb["end"] if tb else t_asst
        is_submit = command.strip() == f"echo {SUBMIT_SENTINEL}"
        result_content = SUBMIT_SENTINEL if is_submit else (
            messages[mi]["content"] if mi < len(messages) and messages[mi]["role"] == "user" else "")
        if not is_submit and result_content:
            mi += 1

        tr = base("user", t_obs, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": result_content}]}, parent)
        tr["toolUseResult"] = {"content": result_content}
        events.append(tr); parent = tr["uuid"]

        tool_calls.append({"timestamp": _iso(t_asst), "tool": "Bash", "id": tool_id,
                           "input": {"command": command}, "end_timestamp": _iso(t_obs),
                           "result_preview": result_content[:500]})
        step += 1
        if is_submit:
            break

    return events, tool_calls


def write_bundle(out_dir: Path, *, agent_result: dict, model_calls: list[dict], model_name: str,
                 instance_id: str, resources: dict, results: dict):
    out_dir.mkdir(parents=True, exist_ok=True)
    events, tool_calls = build_trace(agent_result, model_calls, model_name, instance_id)
    with open(out_dir / "trace.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    (out_dir / "tool_calls.json").write_text(json.dumps(tool_calls, indent=2))
    (out_dir / "resources.json").write_text(json.dumps(resources, indent=2))
    (out_dir / "results.json").write_text(json.dumps(results, indent=2))
    return {"trace_events": len(events), "tool_calls": len(tool_calls),
            "resource_samples": resources["summary"]["sample_count"]}
