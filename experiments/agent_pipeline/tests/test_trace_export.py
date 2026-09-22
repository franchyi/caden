from agent_pipeline.agent import Agent
from agent_pipeline.prompts import SUBMIT_SENTINEL
from trace_export import build_trace


def _clock():
    t = [0.0]

    def now():
        t[0] += 1.0
        return t[0]

    return now


class OneShotModel:
    def __init__(self, reply):
        self.reply = reply

    def query(self, messages, budget=None):
        return self.reply


def _trace(agent_result):
    return build_trace(agent_result, model_calls=[{"usage": {}}], model_name="m", instance_id="i")


def test_tail_echo_submit_preserves_final_observation_in_trace():
    # `pytest && echo SENTINEL` — the submit rides on the tail of a real command. Its output (the
    # test results) must survive into the exported trace, not be dropped on the submit path.
    class TailEnv:
        def execute(self, command):
            return {"output": f"3 passed in 0.1s\n{SUBMIT_SENTINEL}\n", "returncode": 0}

    reply = f"THOUGHT done\n```bash\npytest -q && echo {SUBMIT_SENTINEL}\n```"
    res = Agent(model=OneShotModel(reply), env=TailEnv(), step_limit=5, clock=_clock()).run("t")
    assert res["exit_status"] == "submitted"
    assert any(m["role"] == "user" and "3 passed" in m["content"] for m in res["messages"])
    _events, tool_calls = _trace(res)
    assert tool_calls and "3 passed" in tool_calls[-1]["result_preview"]


def test_canonical_echo_submit_trace_has_one_clean_tool_call():
    # The appended observation must not spawn a spurious extra turn on the canonical submit path.
    class EchoEnv:
        def execute(self, command):
            return {"output": SUBMIT_SENTINEL + "\n", "returncode": 0}

    reply = f"```bash\necho {SUBMIT_SENTINEL}\n```"
    res = Agent(model=OneShotModel(reply), env=EchoEnv(), step_limit=5, clock=_clock()).run("t")
    assert res["exit_status"] == "submitted"
    _events, tool_calls = _trace(res)
    assert len(tool_calls) == 1
    assert tool_calls[-1]["input"]["command"].strip() == f"echo {SUBMIT_SENTINEL}"
