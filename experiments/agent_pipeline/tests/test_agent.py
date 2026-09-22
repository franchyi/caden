from agent_pipeline.agent import Agent
from agent_pipeline.prompts import SUBMIT_SENTINEL


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def query(self, messages, budget=None):
        r = self.replies[self.calls]
        self.calls += 1
        return r


class FakeEnv:
    def __init__(self):
        self.commands = []

    def execute(self, command):
        self.commands.append(command)
        if command.strip() == f"echo {SUBMIT_SENTINEL}":
            return {"output": SUBMIT_SENTINEL + "\n", "returncode": 0}
        return {"output": f"ran: {command}", "returncode": 0}


def _clock():
    t = [0.0]

    def now():
        t[0] += 1.0
        return t[0]

    return now


def test_runs_until_submit_and_records_stages():
    model = FakeModel([
        "THOUGHT: look\n```bash\nls\n```",
        f"THOUGHT: done\n```bash\necho {SUBMIT_SENTINEL}\n```",
    ])
    env = FakeEnv()
    agent = Agent(model=model, env=env, step_limit=10, clock=_clock())
    result = agent.run(task="do it")
    assert result["exit_status"] == "submitted"
    assert env.commands[0] == "ls"
    stages = [e["stage"] for e in result["stage_events"]]
    assert "LLM_WAIT" in stages and "TOOL_BURST" in stages


def test_format_error_reprompts_then_continues():
    model = FakeModel([
        "no command here",
        f"```bash\necho {SUBMIT_SENTINEL}\n```",
    ])
    agent = Agent(model=model, env=FakeEnv(), step_limit=10, clock=_clock())
    result = agent.run(task="t")
    assert result["exit_status"] == "submitted"
    # the reprompt carries the parse failure detail so the model can correct itself
    assert any(m["role"] == "user" and "No ```bash" in m["content"]
               for m in result["messages"])


def test_step_limit_exits():
    model = FakeModel(["```bash\nls\n```"] * 5)
    agent = Agent(model=model, env=FakeEnv(), step_limit=2, clock=_clock())
    result = agent.run(task="t")
    assert result["exit_status"] == "step_limit"


def test_submit_sentinel_recognized_on_any_output_line():
    class TailEnv(FakeEnv):
        def execute(self, command):
            self.commands.append(command)
            return {"output": f"3 passed\n{SUBMIT_SENTINEL}\n", "returncode": 0}

    model = FakeModel([f"```bash\npytest -q && echo {SUBMIT_SENTINEL}\n```"])
    agent = Agent(model=model, env=TailEnv(), step_limit=5, clock=_clock())
    assert agent.run(task="t")["exit_status"] == "submitted"


class BoomModel:
    def query(self, messages, budget=None):
        raise RuntimeError("claude -p failed after 5 attempts")


def test_model_failure_ends_gracefully_as_model_error():
    agent = Agent(model=BoomModel(), env=FakeEnv(), step_limit=5, clock=_clock())
    assert agent.run(task="t")["exit_status"] == "model_error"


def test_model_failure_past_wall_cap_is_wall_timeout():
    # the fake clock advances 1s per call, so a 2.5s wall cap is spent inside the first query
    agent = Agent(model=BoomModel(), env=FakeEnv(), step_limit=5, wall_limit=2.5,
                  clock=_clock())
    assert agent.run(task="t")["exit_status"] == "wall_timeout"
