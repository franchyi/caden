"""Model backend driving `claude -p` (Claude Code headless) with its own tools disabled, so
it returns one THOUGHT + bash block(s) that we parse and run. History is stateless: the full
transcript is re-rendered into the prompt each call (`--resume` is a later optimization) and
piped via STDIN — a single argv element is capped at ~128KB by execve, which a multi-step
transcript exceeds long before the model's context does.
"""

import json
import os
import subprocess
import sys
import time

DISALLOWED_TOOLS = [
    "Bash", "Edit", "Write", "Read", "Glob", "Grep",
    "WebFetch", "WebSearch", "TodoWrite", "Task", "NotebookEdit",
]

_ROLE_TAG = {"system": "SYSTEM", "user": "USER", "assistant": "ASSISTANT"}

# Render-time cap on the re-sent transcript, in chars (~4 chars/token; the model context is 200K
# tokens). Without it a long run eventually dies fatal with "prompt is too long".
_CTX_BUDGET = int(os.environ.get("CTX_BUDGET", "400000"))
_ELIDED = "[old output elided to fit the context budget - re-run the command if you need it]"


def render_prompt(messages: list[dict]) -> str:
    """Flatten the message list into one prompt string (v1: stateless re-send). Past _CTX_BUDGET
    chars the OLDEST observation bodies are elided — at render time only, so self.messages and the
    exported trace keep the full text — never touching the system rules, the task statement, or the
    last two exchanges."""
    contents = [m["content"] for m in messages]
    total = sum(len(c) for c in contents)
    for i in range(2, len(messages) - 4):
        if total <= _CTX_BUDGET:
            break
        if messages[i]["role"] == "user" and len(contents[i]) > len(_ELIDED):
            total -= len(contents[i]) - len(_ELIDED)
            contents[i] = _ELIDED
    parts = [f"### {_ROLE_TAG.get(m['role'], m['role']).upper()}\n{c}"
             for m, c in zip(messages, contents)]
    parts.append("### ASSISTANT\n(Reply now with THOUGHT + one or more ```bash blocks.)")
    return "\n\n".join(parts)


def build_claude_argv(*, claude: str = "claude", model: str | None = None) -> list[str]:
    """argv WITHOUT the prompt — the prompt is piped on stdin (see module docstring)."""
    argv = [claude, "-p", "--output-format", "json"]
    if model:
        argv += ["--model", model]
    argv += ["--disallowedTools", *DISALLOWED_TOOLS]  # variadic — keep last
    return argv


class ClaudeError(RuntimeError):
    """A claude -p invocation failed. `fatal` marks non-transient errors (auth, bad request,
    context-length, ...) that retrying won't fix."""
    def __init__(self, message: str, *, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


# Non-transient claude/Anthropic error signatures -> abort fast. (rate_limit_error, overloaded_error,
# api_error and 5xx/timeouts are TRANSIENT and get retried.)
_FATAL_HINTS = (
    "authentication_error", "permission_error", "not_found_error", "invalid_request_error",
    "context_length_exceeded", "prompt is too long", "credit balance is too low",
    "invalid x-api-key", "exceed the maximum",
)


def _extract_error(stdout: str, stderr: str, rc: int) -> tuple[str, bool]:
    """Build a useful error string from claude -p output — the real error lives in the stdout JSON
    (is_error/subtype/api_error_status/result), not stderr — and decide if it is fatal."""
    detail = ""
    try:
        d = json.loads(stdout)
        if isinstance(d, dict):
            detail = "; ".join(
                f"{k}={d[k]}" for k in ("subtype", "is_error", "api_error_status", "result", "error")
                if d.get(k) not in (None, "", False)
            )
    except (json.JSONDecodeError, TypeError):
        pass
    if not detail:
        detail = (stdout[:400] or stderr[:400] or "(no output)").strip()
    fatal = any(h in detail.lower() for h in _FATAL_HINTS)
    return f"claude -p failed (rc={rc}): {detail[:600]}", fatal


class ClaudeCliModel:
    def __init__(self, *, claude: str = "claude", model: str | None = None,
                 timeout: int = 300, retries: int = 5):
        self.claude = claude
        self.model = model
        self.timeout = timeout
        self.retries = retries  # bounded retry on transient claude -p hangs/failures
        self.calls: list[dict] = []  # per-query API metadata (duration_ms, cost, usage)

    def _run_once(self, argv: list[str], prompt: str, llm_cgroup: str | None,
                  timeout: float) -> str:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace")
        if llm_cgroup:
            # Joined from the parent, NOT via preexec_fn: preexec runs between fork and exec,
            # where opening a file can deadlock while the sampler thread holds an allocator lock.
            try:
                with open(os.path.join(llm_cgroup, "cgroup.procs"), "w") as f:
                    f.write(str(p.pid))
            except OSError:
                pass  # cgroup gone / child already exited — measurement degrades, run continues
        try:
            stdout, stderr = p.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()  # communicate(), unlike subprocess.run(), does not kill on timeout
            p.communicate()
            raise
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = None
        # Success only if the process exited 0 AND returned a non-error JSON result. claude -p can
        # also signal an API failure via rc=0 + is_error=true, so check both.
        if p.returncode != 0 or not isinstance(data, dict) or data.get("is_error") or "result" not in data:
            msg, fatal = _extract_error(stdout, stderr, p.returncode)
            raise ClaudeError(msg, fatal=fatal)
        self.calls.append({k: data.get(k) for k in
                           ("duration_ms", "duration_api_ms", "total_cost_usd", "usage", "num_turns")})
        return data["result"]

    def query(self, messages: list[dict], budget: float | None = None) -> str:
        """`budget` is the caller's remaining wall-cap seconds: per-attempt timeouts are clamped to
        it and retrying stops once it is spent, so one throttled turn can't overshoot the wall cap
        by retries x timeout."""
        prompt = render_prompt(messages)
        argv = build_claude_argv(claude=self.claude, model=self.model)
        llm_cgroup = os.environ.get("LLM_CGROUP")
        t_q = time.monotonic()
        last = None
        for attempt in range(self.retries):  # transient overload/ratelimit/hang → retry w/ backoff
            left = None if budget is None else budget - (time.monotonic() - t_q)
            timeout = self.timeout if left is None else min(self.timeout, max(10.0, left))
            try:
                return self._run_once(argv, prompt, llm_cgroup, timeout)
            except subprocess.TimeoutExpired as e:
                last, reason, fatal = e, f"timeout after {timeout:.0f}s", False
            except ClaudeError as e:
                last, reason, fatal = e, str(e), e.fatal
            print(f"[claude -p attempt {attempt + 1}/{self.retries} failed: {reason[:200]}]",
                  file=sys.stderr, flush=True)
            if fatal:  # auth / invalid request / context-length → retrying won't help
                raise last
            if budget is not None and time.monotonic() - t_q >= budget:
                break  # wall budget spent — surface to the agent instead of retrying past the cap
            if attempt < self.retries - 1:
                time.sleep(min(60, 10 * 2 ** attempt))  # exp backoff: 10/20/40/60/60s
        raise RuntimeError(f"claude -p failed after {attempt + 1} attempts: {last}")
