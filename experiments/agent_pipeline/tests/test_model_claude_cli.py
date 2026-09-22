from agent_pipeline import model_claude_cli as mcc
from agent_pipeline.model_claude_cli import build_claude_argv, render_prompt


def test_render_prompt_includes_history():
    msgs = [
        {"role": "system", "content": "RULES"},
        {"role": "user", "content": "TASK"},
        {"role": "assistant", "content": "```bash\nls\n```"},
        {"role": "user", "content": "<returncode>0</returncode>"},
    ]
    p = render_prompt(msgs)
    assert "RULES" in p and "TASK" in p and "ls" in p and "returncode" in p


def test_render_prompt_elides_oldest_observations_past_budget(monkeypatch):
    monkeypatch.setattr(mcc, "_CTX_BUDGET", 5000)
    msgs = [{"role": "system", "content": "RULES"}, {"role": "user", "content": "TASK"}]
    for i in range(6):
        msgs += [{"role": "assistant", "content": f"act{i}"},
                 {"role": "user", "content": f"obs{i} " + "x" * 3000}]
    p = render_prompt(msgs)
    assert "obs0" not in p and "obs1" not in p and "elided" in p  # oldest dropped
    assert "obs4" in p and "obs5" in p                            # last 2 exchanges intact
    assert "RULES" in p and "TASK" in p                           # system + task pinned
    assert msgs[3]["content"].startswith("obs0")                  # stored messages untouched


def test_render_prompt_small_transcript_untouched():
    msgs = [{"role": "system", "content": "RULES"}, {"role": "user", "content": "TASK"}]
    assert "elided" not in render_prompt(msgs)


def test_argv_has_print_json_and_variadic_disallowed_tools_last():
    argv = build_claude_argv(claude="claude", model="sonnet")
    # the prompt is NOT in argv — it is piped via stdin (execve caps one arg at ~128KB)
    assert argv[:2] == ["claude", "-p"]
    assert "--output-format" in argv and "json" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    # disallowed tools are separate args (variadic), starting right after the flag
    i = argv.index("--disallowedTools")
    assert argv[i + 1] == "Bash" and "NotebookEdit" in argv[i + 1:]
    # nothing after the tool list (it must stay last)
    assert argv[-1] == "NotebookEdit"
