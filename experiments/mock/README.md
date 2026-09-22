# Mock Server

## Instructions

This folder contains a mock Anthropic API server that can be used to replay traces of conversations with Claude.

First run the server on port 8000:

```bash
uv run mock_server.py
```

Then load the traces:

```bash
uv run load_server.py
```

Now run Claude Code with the following arguments:

```bash
export ANTHROPIC_BASE_URL="http://localhost:8000"
export ANTHROPIC_AUTH_TOKEN="<>"
export CLAUDE_CODE_ENABLE_TASKS=0
claude --dangerously-skip-permissions
```

To run Claude on a trace, pass in the `leafUuid` of the desired trace's first message as the auth token. We are currently doing a dirty hack of using the client's auth token to identify the trace.

## Notes

There are still issues with the mock server currently. The main goals are to ensure
high simulation fidelity for the LLM inference timing, and that the tool calls that Claude
Code executes will actually be done for real.

## TODOs

1. Run Claude Code in podman
2. Validate trace simulation