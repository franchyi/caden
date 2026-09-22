from agent_pipeline.prompts import (
    SUBMIT_SENTINEL,
    render_instance,
    render_observation,
)


def test_instance_includes_task_and_sentinel():
    out = render_instance(task="Fix the bug in foo.py")
    assert "Fix the bug in foo.py" in out
    assert SUBMIT_SENTINEL in out


def test_observation_includes_returncode_and_output():
    out = render_observation({"output": "hello", "returncode": 0})
    assert "hello" in out
    assert "0" in out


def test_observation_truncates_long_output():
    big = "x" * 30000
    out = render_observation({"output": big, "returncode": 0})
    assert len(out) < 25000
    assert "elided" in out.lower()
