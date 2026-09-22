"""Stateless no-tools model backend for Pi's provider/model adapters.

The mini-agent owns command execution.  Pi is therefore launched with all tools,
extensions, skills, context files, and session persistence disabled; each query
receives the rendered transcript on stdin and returns only assistant text.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

from agent_pipeline.model_claude_cli import render_prompt


class PiError(RuntimeError):
    """A Pi model invocation failed."""

    def __init__(self, message: str, *, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


_FATAL_HINTS = (
    "authentication",
    "unauthorized",
    "forbidden",
    "unknown model",
    "model not found",
    "context length",
    "prompt is too long",
    "invalid request",
)


def build_pi_argv(
    *,
    pi: str = "pi",
    provider: str,
    model: str,
    thinking: str = "high",
) -> list[str]:
    return [
        pi,
        "--provider",
        provider,
        "--model",
        model,
        "--thinking",
        thinking,
        "--no-tools",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-session",
        "--mode",
        "json",
        "--print",
    ]


def parse_pi_jsonl(stdout: str, *, provider: str, model: str) -> tuple[str, dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise PiError(f"Pi emitted invalid JSONL on line {line_number}: {error}") from error
        if isinstance(value, dict):
            records.append(value)

    assistant: dict[str, Any] | None = None
    for record in records:
        if record.get("type") != "message_end":
            continue
        message = record.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            assistant = message
    if assistant is None:
        raise PiError("Pi JSONL contained no completed assistant message")
    if assistant.get("provider") != provider or assistant.get("model") != model:
        raise PiError(
            "Pi returned unexpected provider/model "
            f"{assistant.get('provider')!r}/{assistant.get('model')!r}",
            fatal=True,
        )
    if assistant.get("stopReason") not in {"stop", "end_turn"}:
        raise PiError(
            f"Pi stopped with {assistant.get('stopReason')!r}: "
            f"{assistant.get('error') or assistant.get('rawStopReason')}",
            fatal=True,
        )
    content = assistant.get("content")
    if not isinstance(content, list):
        raise PiError("Pi assistant content is malformed")
    text = "".join(
        str(part.get("text", ""))
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )
    if not text:
        raise PiError("Pi assistant response contained no text")
    metadata = {
        "provider": assistant.get("provider"),
        "model": assistant.get("model"),
        "api": assistant.get("api"),
        "usage": assistant.get("usage"),
        "response_id": assistant.get("responseId"),
        "stop_reason": assistant.get("stopReason"),
        "raw_stop_reason": assistant.get("rawStopReason"),
    }
    return text, metadata


class PiCliModel:
    def __init__(
        self,
        *,
        pi: str = "pi",
        provider: str = "openai-codex",
        model: str = "gpt-5.6-terra",
        thinking: str = "high",
        timeout: int = 300,
        retries: int = 3,
    ):
        self.pi = pi
        self.provider = provider
        self.model = model
        self.thinking = thinking
        self.timeout = timeout
        self.retries = retries
        self.calls: list[dict[str, Any]] = []

    def _run_once(self, argv: list[str], prompt: str, timeout: float) -> str:
        started = time.monotonic()
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        llm_cgroup = os.environ.get("LLM_CGROUP")
        if llm_cgroup:
            try:
                with open(os.path.join(llm_cgroup, "cgroup.procs"), "w") as handle:
                    handle.write(str(process.pid))
            except OSError:
                pass
        try:
            stdout, stderr = process.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        duration_ms = round((time.monotonic() - started) * 1000)
        if process.returncode != 0:
            detail = (stderr or stdout or "(no output)").strip()[:800]
            fatal = any(hint in detail.lower() for hint in _FATAL_HINTS)
            raise PiError(f"Pi failed (rc={process.returncode}): {detail}", fatal=fatal)
        text, metadata = parse_pi_jsonl(
            stdout, provider=self.provider, model=self.model
        )
        usage = metadata.get("usage")
        cost = usage.get("cost", {}).get("total") if isinstance(usage, dict) else None
        self.calls.append(
            metadata
            | {
                "duration_ms": duration_ms,
                "duration_api_ms": duration_ms,
                "total_cost_usd": cost,
                "num_turns": 1,
            }
        )
        return text

    def query(self, messages: list[dict], budget: float | None = None) -> str:
        prompt = render_prompt(messages)
        argv = build_pi_argv(
            pi=self.pi,
            provider=self.provider,
            model=self.model,
            thinking=self.thinking,
        )
        query_started = time.monotonic()
        last: BaseException | None = None
        attempts = 0
        for attempt in range(self.retries):
            attempts = attempt + 1
            left = None if budget is None else budget - (time.monotonic() - query_started)
            timeout = self.timeout if left is None else min(self.timeout, max(10.0, left))
            try:
                return self._run_once(argv, prompt, timeout)
            except subprocess.TimeoutExpired as error:
                last, reason, fatal = error, f"timeout after {timeout:.0f}s", False
            except PiError as error:
                last, reason, fatal = error, str(error), error.fatal
            print(
                f"[pi attempt {attempts}/{self.retries} failed: {reason[:200]}]",
                file=sys.stderr,
                flush=True,
            )
            if fatal:
                raise last
            if budget is not None and time.monotonic() - query_started >= budget:
                break
            if attempt < self.retries - 1:
                time.sleep(min(30, 5 * 2**attempt))
        raise RuntimeError(f"Pi failed after {attempts} attempts: {last}")
