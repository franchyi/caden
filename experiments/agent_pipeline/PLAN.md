# Claude + Bubblewrap SWE-rebench Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run 1–2 SWE-rebench coding tasks with a hand-written "mini agent" that drives Claude, executes each shell command inside a bubblewrap sandbox, and scores the result against the instance's gold tests — tested on the `nsl7s` node.

**Architecture:** A mini-agent loop (modeled on SWE-agent/mini-swe-agent's text-based protocol) runs on the `nsl7s` host. Each turn it calls `claude -p` (backend "B": reuses the node's existing Claude Code auth, no API key) constrained to emit exactly one `THOUGHT` + one ```` ```bash ```` block; the loop parses that single command and executes it in a fresh bubblewrap sandbox (mini-swe-agent's per-command `BubblewrapEnvironment` pattern). Pure-Python units (action parsing, loop mechanics, scoring logic) are TDD-tested locally on macOS; the bubblewrap / `claude -p` / real-task pieces are verified on `nsl7s` via an rsync test loop. Code lives in `caden/experiments/agent_pipeline/`.

**Tech Stack:** Python 3.11 (provisioned by `uv`), `claude` CLI (Claude Code, already configured on nsl7s), bubblewrap 0.4.0, `jinja2` (prompt templates), `datasets`/`huggingface_hub` (SWE-rebench fetch), `pytest` (our unit tests + task scoring).

**Execution environment note:** development is local (macOS — cannot run bwrap); the integration target is `nsl7s` (`ssh nsl7s`, bubblewrap + uv + git + node + claude already present, HuggingFace + Anthropic egress confirmed, no `ANTHROPIC_API_KEY` — hence backend B). AWS EC2 is explicitly **deferred** (see "Out of scope").

**Architecture caveat (recorded, not blocking):** Backend B puts the agent loop *on the host* and sandboxes each command (mini-swe-agent's model). This gives correct `LLM_WAIT`/`TOOL_BURST` stage *timing* but not the "whole agent inside one cgroup" placement Caden's freeze/reclaim *enforcement* eventually wants. The agent-inside-sandbox placement is a later upgrade that pairs with backend A (Anthropic SDK + API key). v1 targets running tasks + measuring stages.

---

## File Structure

All under `caden/experiments/agent_pipeline/`:

| File | Responsibility |
|---|---|
| `pyproject.toml` | uv project + deps (`jinja2`, `datasets`); dev dep `pytest`. |
| `agent_pipeline/__init__.py` | package marker |
| `agent_pipeline/parsing.py` | `parse_action(text)` — extract exactly one bash command; `FormatError`. **Pure, TDD.** |
| `agent_pipeline/prompts.py` | system / instance / observation / format-error Jinja templates + `render()`. **Pure, TDD.** |
| `agent_pipeline/agent.py` | the loop: `Agent.run(task)` → result dict; records stage events. Backend-agnostic (takes `model`, `env`). **Loop mechanics TDD with fakes.** |
| `agent_pipeline/sandbox.py` | `BubblewrapEnvironment.execute(command)` → `{output, returncode}` via per-command `bwrap`. **argv construction TDD; real exec on nsl7s.** |
| `agent_pipeline/model_claude_cli.py` | `ClaudeCliModel.query(messages)` → assistant text, by shelling `claude -p`. **Flags fixed by the nsl7s spike (Task 7).** |
| `agent_pipeline/scoring.py` | `resolved(fail_to_pass_rcs, pass_to_pass_rcs)` + pytest-per-test runner. **Resolution logic TDD.** |
| `fetch_task.py` | load one SWE-rebench instance from HF → `task.json` + clone repo @ base_commit + build uv venv. **nsl7s.** |
| `evaluate.py` | apply gold `test_patch`; run FAIL_TO_PASS / PASS_TO_PASS; emit verdict. **nsl7s.** |
| `pipeline.py` | orchestrate fetch → agent.run (Bubblewrap env + ClaudeCli model) → evaluate → `results/<id>.json` (incl. stage timings). |
| `tasks.txt` | 1–2 curated easy instance ids. |
| `sync.sh` | `rsync` to `nsl7s:~/caden-pipeline/` + run remotely. |
| `tests/` | pytest unit tests for the pure modules. |
| `results/` | per-run output JSON. |
| `README.md` | how to run locally (tests) and on nsl7s. |

---

## Out of scope (this plan)

- AWS EC2 provisioning / parity run (deferred — "M3" in the design discussion).
- Backend A (Anthropic SDK + API key) and agent-inside-sandbox placement.
- Multi-sandbox density, cgroup freeze/reclaim, sched_ext (that is the Caden harness proper, downstream of this).
- More than 2 tasks; non-pure-Python tasks.

---

## Task 1: Project scaffold

**Files:**
- Create: `caden/experiments/agent_pipeline/pyproject.toml`
- Create: `caden/experiments/agent_pipeline/agent_pipeline/__init__.py`
- Create: `caden/experiments/agent_pipeline/README.md`
- Create: `caden/experiments/agent_pipeline/.gitignore`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "agent-pipeline"
version = "0.1.0"
description = "Claude + bubblewrap SWE-rebench mini-agent pipeline"
requires-python = ">=3.11"
dependencies = [
    "jinja2>=3.1",
    "datasets>=2.19",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.setuptools.packages.find]
include = ["agent_pipeline*"]
```

- [ ] **Step 2: Create the package marker + .gitignore**

`agent_pipeline/__init__.py`: empty file.

`.gitignore`:
```
.venv/
results/*.json
__pycache__/
work/
```

- [ ] **Step 3: Write a short `README.md`**

Document: purpose (one paragraph), `uv sync --extra dev && uv run pytest` for local unit tests, and `./sync.sh` to run on nsl7s. (Full content filled when commands are finalized in later tasks.)

- [ ] **Step 4: Create the local venv and verify**

Run: `cd caden/experiments/agent_pipeline && uv venv --python 3.11 && uv pip install -e ".[dev]"`
Expected: venv created, jinja2 + datasets + pytest installed, no errors.

- [ ] **Step 5: Commit**

```bash
git add caden/experiments/agent_pipeline/pyproject.toml caden/experiments/agent_pipeline/agent_pipeline/__init__.py caden/experiments/agent_pipeline/README.md caden/experiments/agent_pipeline/.gitignore
git commit -m "feat(agent-pipeline): project scaffold"
```

---

## Task 2: Action parsing (`parsing.py`) — TDD

The agent protocol: the model replies with prose + exactly one ```` ```bash ```` fenced block. We extract that one command. Zero or many → `FormatError` (which the loop turns into a corrective re-prompt).

**Files:**
- Create: `agent_pipeline/agent_pipeline/parsing.py`
- Test: `agent_pipeline/tests/test_parsing.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_parsing.py
import pytest
from agent_pipeline.parsing import parse_action, FormatError

def test_extracts_single_command():
    text = "THOUGHT: list files\n\n```bash\nls -la\n```"
    assert parse_action(text) == "ls -la"

def test_strips_whitespace_and_keeps_multiline():
    text = "do this\n```bash\ncd src && \\\npytest -q\n```\n"
    assert parse_action(text) == "cd src && \\\npytest -q"

def test_zero_blocks_raises():
    with pytest.raises(FormatError):
        parse_action("I think we are done.")

def test_multiple_blocks_raises():
    with pytest.raises(FormatError):
        parse_action("```bash\nls\n```\nand\n```bash\npwd\n```")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_parsing.py -v`
Expected: FAIL — `ModuleNotFoundError: agent_pipeline.parsing`.

- [ ] **Step 3: Implement `parsing.py`**

```python
# agent_pipeline/parsing.py
import re

ACTION_RE = re.compile(r"```bash\s*\n(.*?)```", re.DOTALL)


class FormatError(Exception):
    """Model response did not contain exactly one bash command block."""


def parse_action(text: str) -> str:
    """Return the single bash command from a ```bash block. Raise FormatError if != 1."""
    blocks = [m.strip() for m in ACTION_RE.findall(text)]
    if len(blocks) != 1:
        raise FormatError(f"Expected exactly 1 bash block, found {len(blocks)}.")
    return blocks[0]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_parsing.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add agent_pipeline/agent_pipeline/parsing.py agent_pipeline/tests/test_parsing.py
git commit -m "feat(agent-pipeline): single-bash-command action parser"
```

---

## Task 3: Prompt templates (`prompts.py`) — TDD

Adapted (and trimmed) from mini-swe-agent's `default.yaml`. Key contract baked into the system prompt: respond with **exactly one** bash block; **state does not persist** between commands (each runs in a fresh subshell — chain with `&&`, use absolute/repo-relative paths); finish by running `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` **alone**.

**Files:**
- Create: `agent_pipeline/agent_pipeline/prompts.py`
- Test: `agent_pipeline/tests/test_prompts.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prompts.py
from agent_pipeline.prompts import SYSTEM, render_instance, render_observation, SUBMIT_SENTINEL

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
    assert len(out) < 25000          # truncated
    assert "elided" in out.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_prompts.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `prompts.py`**

```python
# agent_pipeline/prompts.py
from jinja2 import Template

SUBMIT_SENTINEL = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
_TRUNC = 20000  # chars; keep head+tail when exceeded

SYSTEM = f"""You are an autonomous software engineer working in a sandboxed Linux shell.

Respond with a short THOUGHT explaining your reasoning, then EXACTLY ONE bash command
in a single ```bash code block. No more than one command block per response
(chain steps with && or || if needed).

Important rules:
- Every action runs in a NEW subshell. Directory changes and environment variables
  do NOT persist between commands. Use absolute paths, or prefix with
  `cd /path && ...` / `VAR=value cmd` within a single command.
- The repository is your working directory.
- When the task is complete, finish by running exactly this, alone, in its own block:
  `echo {SUBMIT_SENTINEL}`
  Do not combine it with any other command.
"""

_INSTANCE = Template("""Solve this issue:

{{ task }}

Recommended workflow: explore relevant files, reproduce the problem, edit the source,
run the tests, then finish with `echo {{ sentinel }}`.
""")

_OBSERVATION = Template("""<returncode>{{ rc }}</returncode>
{% if output|length < trunc -%}
<output>
{{ output }}
</output>
{%- else -%}
<output_head>
{{ output[:trunc//2] }}
</output_head>
<elided>{{ output|length - trunc }} characters elided. Use head/tail/grep to narrow output.</elided>
<output_tail>
{{ output[-trunc//2:] }}
</output_tail>
{%- endif %}""")


def render_instance(task: str) -> str:
    return _INSTANCE.render(task=task, sentinel=SUBMIT_SENTINEL)


def render_observation(result: dict) -> str:
    return _OBSERVATION.render(rc=result.get("returncode"),
                               output=result.get("output", ""), trunc=_TRUNC)


FORMAT_ERROR = (
    "Format error: provide EXACTLY ONE bash command in a single ```bash code block. "
    f"To finish, run `echo {SUBMIT_SENTINEL}` alone."
)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_prompts.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add agent_pipeline/agent_pipeline/prompts.py agent_pipeline/tests/test_prompts.py
git commit -m "feat(agent-pipeline): prompt templates + observation truncation"
```

---

## Task 4: The agent loop (`agent.py`) — TDD with fakes

The loop is backend-agnostic: it takes a `model` (`.query(messages) -> str`) and an `env` (`.execute(command) -> dict`). It records stage events: `LLM_WAIT` around `model.query`, `TOOL_BURST` around `env.execute`. Stops on the submit sentinel, a step limit, or `FormatError` re-prompts. Uses an injected `clock` so timing is testable.

**Files:**
- Create: `agent_pipeline/agent_pipeline/agent.py`
- Test: `agent_pipeline/tests/test_agent.py`

- [ ] **Step 1: Write the failing tests (loop mechanics, with fakes)**

```python
# tests/test_agent.py
from agent_pipeline.agent import Agent
from agent_pipeline.prompts import SUBMIT_SENTINEL

class FakeModel:
    def __init__(self, replies): self.replies = list(replies); self.calls = 0
    def query(self, messages): r = self.replies[self.calls]; self.calls += 1; return r

class FakeEnv:
    def __init__(self): self.commands = []
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
        "no command here",                                   # FormatError -> reprompt
        f"```bash\necho {SUBMIT_SENTINEL}\n```",
    ])
    agent = Agent(model=model, env=FakeEnv(), step_limit=10, clock=_clock())
    result = agent.run(task="t")
    assert result["exit_status"] == "submitted"

def test_step_limit_exits():
    model = FakeModel(["```bash\nls\n```"] * 5)
    agent = Agent(model=model, env=FakeEnv(), step_limit=2, clock=_clock())
    result = agent.run(task="t")
    assert result["exit_status"] == "step_limit"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_agent.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `agent.py`**

```python
# agent_pipeline/agent.py
import time
from agent_pipeline.parsing import parse_action, FormatError
from agent_pipeline import prompts


class Agent:
    def __init__(self, model, env, *, step_limit: int = 30, clock=time.monotonic):
        self.model = model
        self.env = env
        self.step_limit = step_limit
        self.clock = clock
        self.messages: list[dict] = []
        self.stage_events: list[dict] = []

    def _mark(self, stage, start, step):
        self.stage_events.append(
            {"stage": stage, "start": start, "end": self.clock(), "step": step}
        )

    def run(self, task: str) -> dict:
        self.messages = [
            {"role": "system", "content": prompts.SYSTEM},
            {"role": "user", "content": prompts.render_instance(task)},
        ]
        for step in range(self.step_limit):
            t0 = self.clock()
            reply = self.model.query(self.messages)        # LLM_WAIT
            self._mark("LLM_WAIT", t0, step)
            self.messages.append({"role": "assistant", "content": reply})

            try:
                command = parse_action(reply)
            except FormatError:
                self.messages.append({"role": "user", "content": prompts.FORMAT_ERROR})
                continue

            t1 = self.clock()
            result = self.env.execute(command)             # TOOL_BURST
            self._mark("TOOL_BURST", t1, step)

            first = result.get("output", "").strip().splitlines()
            if command.strip() == f"echo {prompts.SUBMIT_SENTINEL}" or (
                first and first[0].strip() == prompts.SUBMIT_SENTINEL
            ):
                return self._result("submitted", step)

            self.messages.append(
                {"role": "user", "content": prompts.render_observation(result)}
            )
        return self._result("step_limit", self.step_limit)

    def _result(self, exit_status, steps) -> dict:
        return {
            "exit_status": exit_status,
            "steps": steps,
            "stage_events": self.stage_events,
            "messages": self.messages,
        }
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_agent.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add agent_pipeline/agent_pipeline/agent.py agent_pipeline/tests/test_agent.py
git commit -m "feat(agent-pipeline): backend-agnostic agent loop with stage marks"
```

---

## Task 5: Bubblewrap environment (`sandbox.py`) — argv TDD, real exec on nsl7s

Per-command sandbox (mini-swe-agent pattern, 0.4.0-safe flags). The repo working dir is bind-mounted at its real path (so the prebuilt venv's absolute paths resolve) and `--chdir`'d into. Network is left **on** (no `--unshare-net`) for v1. Output is stdout+stderr merged; truncation is handled by the prompt layer.

**Files:**
- Create: `agent_pipeline/agent_pipeline/sandbox.py`
- Test: `agent_pipeline/tests/test_sandbox.py`

- [ ] **Step 1: Write the failing test (argv construction is pure + testable on macOS)**

```python
# tests/test_sandbox.py
from agent_pipeline.sandbox import build_bwrap_argv

def test_argv_binds_repo_and_chdir():
    argv = build_bwrap_argv(repo="/work/repo", command="pytest -q", bwrap="bwrap")
    assert argv[0] == "bwrap"
    assert "--bind" in argv and "/work/repo" in argv and "--chdir" in argv
    # repo bound at same path and chdir'd
    i = argv.index("--chdir"); assert argv[i + 1] == "/work/repo"
    # command passed to bash -c as the final args
    assert argv[-3:] == ["bash", "-c", "pytest -q"]
    # read-only system mounts present
    assert "--ro-bind" in argv and "/usr" in argv
    # network NOT unshared in v1
    assert "--unshare-net" not in argv
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_sandbox.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `sandbox.py`**

```python
# agent_pipeline/sandbox.py
import subprocess

# 0.4.0-safe flags. No --overlay/--bind-fd/--disable-userns (those are newer).
BASE_ARGS = [
    "--unshare-user-try",
    "--unshare-pid", "--unshare-ipc", "--unshare-uts",
    "--ro-bind", "/usr", "/usr",
    "--ro-bind", "/bin", "/bin",
    "--ro-bind", "/lib", "/lib",
    "--ro-bind", "/lib64", "/lib64",
    "--ro-bind", "/etc", "/etc",
    "--tmpfs", "/tmp",
    "--proc", "/proc",
    "--dev", "/dev",
    "--new-session",
    "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
]


def build_bwrap_argv(repo: str, command: str, *, bwrap: str = "bwrap",
                     env: dict | None = None) -> list[str]:
    argv = [bwrap, *BASE_ARGS, "--bind", repo, repo, "--chdir", repo]
    for k, v in (env or {}).items():
        argv += ["--setenv", k, v]
    argv += ["bash", "-c", command]
    return argv


class BubblewrapEnvironment:
    def __init__(self, repo: str, *, bwrap: str = "bwrap", timeout: int = 600,
                 env: dict | None = None):
        self.repo = repo
        self.bwrap = bwrap
        self.timeout = timeout
        self.env = env or {}

    def execute(self, command: str) -> dict:
        argv = build_bwrap_argv(self.repo, command, bwrap=self.bwrap, env=self.env)
        try:
            r = subprocess.run(argv, text=True, timeout=self.timeout,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               encoding="utf-8", errors="replace")
            return {"output": r.stdout, "returncode": r.returncode}
        except subprocess.TimeoutExpired as e:
            out = e.output or ""
            out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
            return {"output": out + f"\n[timeout after {self.timeout}s]", "returncode": -1}
```

- [ ] **Step 4: Run to verify pass (argv test, local)**

Run: `uv run pytest tests/test_sandbox.py -v`
Expected: 1 passed.

- [ ] **Step 5: Real execution smoke test — ON nsl7s (after Task 9 sync, or ad hoc)**

Run (on nsl7s, after rsync): `python3 -c "from agent_pipeline.sandbox import BubblewrapEnvironment as E; print(E('/tmp').execute('echo hi; whoami'))"`
Expected: `{'output': 'hi\\n<user>\\n', 'returncode': 0}` (a real sandboxed run).
If it fails, capture the bwrap error and adjust `BASE_ARGS` (e.g. drop `/lib64` ro-bind if that path is a symlink on the node).

- [ ] **Step 6: Commit**

```bash
git add agent_pipeline/agent_pipeline/sandbox.py agent_pipeline/tests/test_sandbox.py
git commit -m "feat(agent-pipeline): per-command bubblewrap environment"
```

---

## Task 6: Scoring logic (`scoring.py`) — TDD

Pure resolution rule + a pytest-per-test runner. `resolved = every FAIL_TO_PASS test now passes AND every PASS_TO_PASS test still passes`. Running each test id individually (rc==0 ⇒ pass) avoids parsing pytest summaries.

**Files:**
- Create: `agent_pipeline/agent_pipeline/scoring.py`
- Test: `agent_pipeline/tests/test_scoring.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_scoring.py
from agent_pipeline.scoring import is_resolved

def test_resolved_when_all_pass():
    assert is_resolved(fail_to_pass={"t_a": 0, "t_b": 0}, pass_to_pass={"t_c": 0}) is True

def test_not_resolved_if_a_fail_to_pass_still_fails():
    assert is_resolved(fail_to_pass={"t_a": 0, "t_b": 1}, pass_to_pass={"t_c": 0}) is False

def test_not_resolved_if_a_pass_to_pass_regresses():
    assert is_resolved(fail_to_pass={"t_a": 0}, pass_to_pass={"t_c": 1}) is False

def test_empty_lists_are_resolved():
    assert is_resolved(fail_to_pass={}, pass_to_pass={}) is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_scoring.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `scoring.py`**

```python
# agent_pipeline/scoring.py
import subprocess


def is_resolved(fail_to_pass: dict[str, int], pass_to_pass: dict[str, int]) -> bool:
    """Resolved iff every listed test returned exit code 0."""
    return all(rc == 0 for rc in fail_to_pass.values()) and \
           all(rc == 0 for rc in pass_to_pass.values())


def run_tests(repo: str, venv_python: str, test_ids: list[str],
              timeout: int = 600) -> dict[str, int]:
    """Run each pytest node id individually; return {test_id: returncode}."""
    rcs = {}
    for tid in test_ids:
        r = subprocess.run([venv_python, "-m", "pytest", "-x", "-q", tid],
                           cwd=repo, timeout=timeout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rcs[tid] = r.returncode
    return rcs
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_scoring.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add agent_pipeline/agent_pipeline/scoring.py agent_pipeline/tests/test_scoring.py
git commit -m "feat(agent-pipeline): gold-test resolution logic"
```

---

## Task 7: SPIKE — `claude -p` as a one-command generator (ON nsl7s)

**This de-risks backend B before we build the model adapter.** Goal: find the flag combo that makes `claude -p` return a single assistant message containing one ```bash block, WITHOUT Claude Code firing its own tools.

**Files:**
- Create: `agent_pipeline/agent_pipeline/model_claude_cli.py` (after the spike settles flags)
- Test: `agent_pipeline/tests/test_model_claude_cli.py` (renders messages → prompt; pure)

- [ ] **Step 1: Inspect available flags on nsl7s**

Run: `ssh nsl7s 'claude --help 2>&1 | grep -iE "output-format|tools|permission|model|system-prompt|max-turns|print"'`
Expected: confirm `-p/--print`, `--output-format`, `--allowedTools`/`--disallowedTools`, `--permission-mode`, `--append-system-prompt`, `--model` (record exact spellings).

- [ ] **Step 2: Probe text-only behavior**

Run (on nsl7s): a one-shot prompt that includes our SYSTEM rules and asks for the first command on a trivial task, e.g.
```bash
ssh nsl7s 'claude -p "You must reply with exactly one bash command in a ```bash block. Task: print the working directory. Then stop." --output-format json --disallowedTools "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,TodoWrite,Task" 2>&1 | head -40'
```
Expected: JSON whose `.result` contains a single ```bash block (e.g. `pwd`) and no tool execution. Record what actually disables tool use (disallow list vs `--permission-mode plan` vs empty `--allowedTools`). If Claude still narrates without a code block, tighten the system instruction.

- [ ] **Step 3: Decide the invocation and write it down**

In `model_claude_cli.py`, capture the chosen `CLAUDE_ARGS` as a module constant with a comment citing the spike result. Decide history strategy: **v1 = stateless, full transcript re-sent each call** (simplest); note `--resume <session_id>` as a future optimization.

- [ ] **Step 4: Write the pure prompt-rendering test**

```python
# tests/test_model_claude_cli.py
from agent_pipeline.model_claude_cli import render_prompt

def test_render_prompt_includes_history():
    msgs = [
        {"role": "system", "content": "RULES"},
        {"role": "user", "content": "TASK"},
        {"role": "assistant", "content": "```bash\nls\n```"},
        {"role": "user", "content": "<returncode>0</returncode>"},
    ]
    p = render_prompt(msgs)
    assert "RULES" in p and "TASK" in p and "ls" in p and "returncode" in p
```

- [ ] **Step 5: Implement `model_claude_cli.py` using the spike's flags**

```python
# agent_pipeline/model_claude_cli.py
import json
import subprocess

# Flags fixed by the Task 7 spike on nsl7s. Update the list to match what
# actually suppressed Claude Code's own tool use on the node.
CLAUDE_ARGS = [
    "--output-format", "json",
    "--disallowedTools", "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,TodoWrite,Task",
]

_ROLE_TAG = {"system": "SYSTEM", "user": "USER", "assistant": "ASSISTANT"}


def render_prompt(messages: list[dict]) -> str:
    """Flatten the message list into one prompt string (v1: stateless re-send)."""
    parts = []
    for m in messages:
        parts.append(f"### {_ROLE_TAG.get(m['role'], m['role']).upper()}\n{m['content']}")
    parts.append("### ASSISTANT\n(Reply now with THOUGHT + exactly one ```bash block.)")
    return "\n\n".join(parts)


class ClaudeCliModel:
    def __init__(self, *, claude: str = "claude", model: str | None = None, timeout: int = 300):
        self.claude = claude
        self.model = model
        self.timeout = timeout

    def query(self, messages: list[dict]) -> str:
        argv = [self.claude, "-p", render_prompt(messages), *CLAUDE_ARGS]
        if self.model:
            argv += ["--model", self.model]
        r = subprocess.run(argv, text=True, timeout=self.timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"claude -p failed (rc={r.returncode}): {r.stderr[:500]}")
        try:
            return json.loads(r.stdout)["result"]
        except (json.JSONDecodeError, KeyError) as e:
            raise RuntimeError(f"unexpected claude output: {r.stdout[:500]}") from e
```

- [ ] **Step 6: Run the pure test (local) + a live one-call check (nsl7s)**

Local: `uv run pytest tests/test_model_claude_cli.py -v` → PASS.
nsl7s (after sync): `python3 -c "from agent_pipeline.model_claude_cli import ClaudeCliModel; from agent_pipeline.parsing import parse_action; print(parse_action(ClaudeCliModel().query([{'role':'user','content':'reply with one bash block that runs: pwd'}])))"`
Expected: prints `pwd` (or similar) — proves end-to-end model→parse works on the node.

- [ ] **Step 7: Commit**

```bash
git add agent_pipeline/agent_pipeline/model_claude_cli.py agent_pipeline/tests/test_model_claude_cli.py
git commit -m "feat(agent-pipeline): claude -p model backend (spike-fixed flags)"
```

---

## Task 8: Fetch a SWE-rebench instance (`fetch_task.py`) — nsl7s

Load one instance, materialize `task.json` + a checked-out repo @ `base_commit` + a uv venv with the project installed. **Field names are verified empirically in Step 1** (SWE-rebench mirrors the SWE-bench schema, but confirm).

**Files:**
- Create: `agent_pipeline/fetch_task.py`
- Create: `agent_pipeline/tasks.txt`

- [ ] **Step 1: Confirm dataset id + schema (on nsl7s)**

Run: `ssh nsl7s 'cd ~/caden-pipeline && uv run python -c "import datasets,sys; d=datasets.load_dataset(\"nebius/SWE-rebench\", split=\"test\"); print(d.features); print(d[0][\"instance_id\"])"'`
Expected: prints the feature schema. Confirm the exact names for: `instance_id`, `repo`, `base_commit`, `patch`, `test_patch`, `problem_statement`, `FAIL_TO_PASS`, `PASS_TO_PASS`. If the dataset id or any field name differs, record the real ones and use them below. (If `nebius/SWE-rebench` is wrong, search HF for the SWE-rebench dataset and use its id.)

- [ ] **Step 2: Pick 1 easy instance → `tasks.txt`**

Choose a tiny, pure-Python, `pip install -e .`-able repo. Start with a `files-to-prompt` instance (small CLI, fast pytest). Put one `instance_id` per line in `tasks.txt`. (Confirm the id exists in the dataset from Step 1.)

- [ ] **Step 3: Implement `fetch_task.py`**

```python
# agent_pipeline/fetch_task.py
"""Materialize a SWE-rebench instance: task.json + repo@base_commit + uv venv.
Run on nsl7s (needs git + uv + network). Field names per Task 8 Step 1."""
import json
import subprocess
import sys
from pathlib import Path

import datasets

DATASET = "nebius/SWE-rebench"   # verified in Task 8 Step 1
SPLIT = "test"


def _run(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True)


def fetch(instance_id: str, workdir: Path) -> dict:
    ds = datasets.load_dataset(DATASET, split=SPLIT)
    row = next(r for r in ds if r["instance_id"] == instance_id)

    repo_url = f"https://github.com/{row['repo']}.git"
    repo_dir = workdir / "repo"
    workdir.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", repo_url, str(repo_dir)])
    _run(["git", "checkout", row["base_commit"]], cwd=repo_dir)

    # uv venv (modern Python) + install the project + pytest
    _run(["uv", "venv", "--python", "3.11", str(repo_dir / ".venv")])
    pip = ["uv", "pip", "install", "--python", str(repo_dir / ".venv/bin/python")]
    _run(pip + ["-e", "."], cwd=repo_dir)
    _run(pip + ["pytest"], cwd=repo_dir)

    task = {
        "instance_id": instance_id,
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "problem_statement": row["problem_statement"],
        "test_patch": row["test_patch"],
        "fail_to_pass": json.loads(row["FAIL_TO_PASS"]) if isinstance(row["FAIL_TO_PASS"], str) else row["FAIL_TO_PASS"],
        "pass_to_pass": json.loads(row["PASS_TO_PASS"]) if isinstance(row["PASS_TO_PASS"], str) else row["PASS_TO_PASS"],
        "repo_dir": str(repo_dir),
        "venv_python": str(repo_dir / ".venv/bin/python"),
    }
    (workdir / "task.json").write_text(json.dumps(task, indent=2))
    return task


if __name__ == "__main__":
    instance_id, out = sys.argv[1], Path(sys.argv[2])
    fetch(instance_id, out)
    print(f"fetched {instance_id} -> {out}")
```

- [ ] **Step 4: Run on nsl7s for instance #1**

Run: `ssh nsl7s 'cd ~/caden-pipeline && uv run python fetch_task.py <instance_id> work/<instance_id>'`
Expected: clone + checkout + venv + install succeed; `work/<id>/task.json` exists and lists non-empty `fail_to_pass`. If install fails (system deps), pick a different pure-Python instance and update `tasks.txt`.

- [ ] **Step 5: Commit**

```bash
git add agent_pipeline/fetch_task.py agent_pipeline/tasks.txt
git commit -m "feat(agent-pipeline): SWE-rebench instance fetcher"
```

---

## Task 9: Evaluator + sync script + pipeline wiring

**Files:**
- Create: `agent_pipeline/evaluate.py`
- Create: `agent_pipeline/pipeline.py`
- Create: `agent_pipeline/sync.sh`

- [ ] **Step 1: Implement `evaluate.py`**

```python
# agent_pipeline/evaluate.py
"""Apply the gold test_patch, then run FAIL_TO_PASS / PASS_TO_PASS. Run on nsl7s."""
import subprocess
from pathlib import Path
from agent_pipeline.scoring import run_tests, is_resolved


def evaluate(task: dict) -> dict:
    repo = task["repo_dir"]
    venv_python = task["venv_python"]
    # apply gold tests (test files only) on top of the agent's code changes
    patch_file = Path(repo) / ".gold_test.patch"
    patch_file.write_text(task["test_patch"])
    subprocess.run(["git", "apply", str(patch_file)], cwd=repo, check=True)

    f2p = run_tests(repo, venv_python, task["fail_to_pass"])
    p2p = run_tests(repo, venv_python, task["pass_to_pass"])
    return {"fail_to_pass": f2p, "pass_to_pass": p2p,
            "resolved": is_resolved(f2p, p2p)}
```

- [ ] **Step 2: Implement `pipeline.py`**

```python
# agent_pipeline/pipeline.py
"""fetch -> sandboxed agent run -> evaluate -> results/<id>.json. Run on nsl7s."""
import json
import sys
import time
from pathlib import Path

from agent_pipeline.agent import Agent
from agent_pipeline.sandbox import BubblewrapEnvironment
from agent_pipeline.model_claude_cli import ClaudeCliModel
from agent_pipeline.evaluate import evaluate
from fetch_task import fetch

ROOT = Path(__file__).parent


def run_instance(instance_id: str) -> dict:
    workdir = ROOT / "work" / instance_id
    task = fetch(instance_id, workdir) if not (workdir / "task.json").exists() \
        else json.loads((workdir / "task.json").read_text())

    env = BubblewrapEnvironment(repo=task["repo_dir"])
    model = ClaudeCliModel()
    agent = Agent(model=model, env=env, step_limit=40, clock=time.monotonic)

    t0 = time.monotonic()
    agent_result = agent.run(task["problem_statement"])
    wall = time.monotonic() - t0

    verdict = evaluate(task)

    def stage_total(name):
        return sum(e["end"] - e["start"] for e in agent_result["stage_events"]
                   if e["stage"] == name)

    out = {
        "instance_id": instance_id,
        "resolved": verdict["resolved"],
        "verdict": verdict,
        "exit_status": agent_result["exit_status"],
        "steps": agent_result["steps"],
        "wall_seconds": wall,
        "stage_seconds": {"LLM_WAIT": stage_total("LLM_WAIT"),
                          "TOOL_BURST": stage_total("TOOL_BURST")},
    }
    res = ROOT / "results" / f"{instance_id}.json"
    res.parent.mkdir(exist_ok=True)
    res.write_text(json.dumps({**out, "stage_events": agent_result["stage_events"],
                               "messages": agent_result["messages"]}, indent=2))
    print(json.dumps(out, indent=2))
    return out


if __name__ == "__main__":
    tasks_file = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "tasks.txt")
    for line in tasks_file.read_text().splitlines():
        instance_id = line.strip()
        if instance_id and not instance_id.startswith("#"):
            run_instance(instance_id)
```

- [ ] **Step 3: Implement `sync.sh`**

```bash
#!/usr/bin/env bash
# rsync the pipeline to nsl7s and run it there.
set -euo pipefail
REMOTE="nsl7s:~/caden-pipeline/"
rsync -az --delete \
  --exclude '.venv' --exclude 'work' --exclude 'results' --exclude '__pycache__' \
  "$(dirname "$0")/" "$REMOTE"
ssh nsl7s 'cd ~/caden-pipeline && (uv venv --python 3.11 .venv 2>/dev/null; \
  uv pip install -e ".[dev]" >/dev/null) && uv run python pipeline.py "$@"' -- "$@"
```
Then `chmod +x sync.sh`.

- [ ] **Step 4: Commit**

```bash
git add agent_pipeline/evaluate.py agent_pipeline/pipeline.py agent_pipeline/sync.sh
git commit -m "feat(agent-pipeline): evaluator, orchestrator, and nsl7s sync"
```

---

## Task 10: First end-to-end run on nsl7s (M1 gate)

- [ ] **Step 1: Sync + run instance #1**

Run: `cd caden/experiments/agent_pipeline && ./sync.sh tasks.txt`
Expected: pipeline fetches the instance, the agent loops (you'll see `claude -p` calls), commands run in bwrap, evaluation runs, and `results/<id>.json` is written with a `resolved` boolean and `stage_seconds`.

- [ ] **Step 2: Triage**

The first run may not *resolve* the task — that is acceptable for M1. The gate is: the pipeline completes end-to-end and produces a verdict + stage timings. Common fixes:
- claude narration without a command → tighten SYSTEM / spike flags (Task 7).
- bwrap path/symlink error → adjust `BASE_ARGS` (Task 5 Step 5).
- install failure → swap to a simpler instance (Task 8 Step 2).
- wrong test ids / patch apply error → re-check field names (Task 8 Step 1).

- [ ] **Step 3: Commit any fixes**

```bash
git add -A && git commit -m "fix(agent-pipeline): M1 end-to-end on nsl7s"
```

---

## Task 11: Second task + telemetry (M2 gate)

- [ ] **Step 1: Add a 2nd easy instance to `tasks.txt`** (e.g. a `soupsieve` instance; confirm install/test from Task 8 Step 1 schema).

- [ ] **Step 2: Run both**

Run: `./sync.sh tasks.txt`
Expected: two `results/*.json`, each with `resolved`, `steps`, `wall_seconds`, and `stage_seconds` (`LLM_WAIT` vs `TOOL_BURST`) — the Caden-relevant signal.

- [ ] **Step 3: Commit**

```bash
git add agent_pipeline/tasks.txt agent_pipeline/results/.gitkeep
git commit -m "feat(agent-pipeline): second task + stage telemetry (M2)"
```

---

## Self-review checklist (run after building)

- **Spec coverage:** mini-agent loop (T2,T3,T4) ✓; bubblewrap exec (T5) ✓; claude -p backend B (T7) ✓; 1–2 SWE-rebench tasks (T8,T11) ✓; scoring vs gold tests (T6,T9) ✓; nsl7s test loop (T9 sync, T10) ✓; AWS deferred ✓.
- **Placeholders:** none — code is complete; the only deliberately-empirical steps are the Task 7 spike and Task 8 Step 1 schema check, both with explicit verification commands.
- **Type consistency:** `model.query(messages)->str`, `env.execute(command)->{output,returncode}`, `task` dict keys (`repo_dir`, `venv_python`, `fail_to_pass`, `pass_to_pass`, `problem_statement`, `test_patch`) are used identically across `agent.py`, `sandbox.py`, `model_claude_cli.py`, `fetch_task.py`, `evaluate.py`, `pipeline.py`.
