import json

import pytest
from agent_pipeline.model_pi_cli import PiError, build_pi_argv, parse_pi_jsonl


def assistant_event(provider="openai-codex", model="gpt-5.6-terra"):
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "THOUGHT\n```bash\nls\n```"}],
            "api": "openai-codex-responses",
            "provider": provider,
            "model": model,
            "usage": {"input": 100, "output": 20, "cost": {"total": 0.01}},
            "stopReason": "stop",
            "responseId": "response-1",
            "rawStopReason": "completed",
        },
    }


def test_build_pi_argv_disables_agent_tools_and_persistence():
    argv = build_pi_argv(
        pi="/usr/bin/pi",
        provider="openai-codex",
        model="gpt-5.6-terra",
        thinking="high",
    )
    assert argv[:2] == ["/usr/bin/pi", "--provider"]
    assert "--no-tools" in argv
    assert "--no-session" in argv
    assert argv[argv.index("--model") + 1] == "gpt-5.6-terra"


def test_parse_pi_jsonl_requires_exact_provider_and_model():
    stdout = "\n".join(
        [json.dumps({"type": "agent_start"}), json.dumps(assistant_event())]
    )
    text, metadata = parse_pi_jsonl(
        stdout, provider="openai-codex", model="gpt-5.6-terra"
    )
    assert text.endswith("```")
    assert metadata["provider"] == "openai-codex"
    assert metadata["usage"]["input"] == 100

    with pytest.raises(PiError, match="unexpected provider/model"):
        parse_pi_jsonl(stdout, provider="openrouter", model="gpt-5.6-terra")


def test_parse_pi_jsonl_rejects_missing_assistant_message():
    with pytest.raises(PiError, match="no completed assistant"):
        parse_pi_jsonl(
            json.dumps({"type": "agent_end"}),
            provider="openai-codex",
            model="gpt-5.6-terra",
        )
