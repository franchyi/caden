"""Prompt protocol for the mini-agent: reply with prose + one or more ```bash blocks (they run in
order in a single shell per response), and finish by running the submit sentinel. Shell state
persists within a response but resets between responses (each runs in a fresh sandbox)."""

SUBMIT_SENTINEL = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
_TRUNC = 20000  # chars; keep head+tail when exceeded

SYSTEM = f"""You are an autonomous software engineer working in a sandboxed Linux shell.

Respond with a short THOUGHT explaining your reasoning, then the shell command(s) to run in one
or more ```bash code blocks. All bash blocks in a single response execute IN ORDER in the SAME
shell, so a `cd` or variable set in one block carries into the next within that response.

Important rules:
- You start in the repository root — it is your current working directory. Begin by running `ls`
  to see the files, and prefer relative paths.
- Fix the SOURCE only. Do NOT create, modify, or delete tests (anything under `tests/` or named
  `test_*.py` / `*_test.py`, including `conftest.py`) — your work is graded by hidden tests, and
  editing tests will not help.
- Shell state persists WITHIN one response but NOT across responses: each response runs in a fresh
  sandbox back at the repository root.
- Never put a literal ``` line inside a bash block (e.g. writing Markdown via heredoc) — it ends
  the block early. Generate such file content with python instead.
- Do NOT submit until you have actually EDITED the source to implement the change AND run a
  reproduction (or the test suite) showing the new behavior works. If you have made no edits, you
  are not done — keep working.
- When the task is complete, finish with exactly this, alone in its own response:
  `echo {SUBMIT_SENTINEL}`
"""

FORMAT_ERROR = (
    "Format error: include at least one ```bash code block with the command(s) to run. "
    f"To finish, run `echo {SUBMIT_SENTINEL}`."
)


def render_instance(task: str) -> str:
    return (
        f"Solve this issue:\n\n{task}\n\n"
        "The project and pytest are installed in the active virtualenv, so `python` and "
        "`pytest` are already on PATH. The hidden grading tests are NOT in the repo, so "
        "implement EXACTLY the interface the issue describes — the same option/flag names, "
        "defaults, and output format it mentions.\n"
        "Workflow:\n"
        "1. Read the relevant source (e.g. the CLI module).\n"
        "2. Implement the change described in the issue.\n"
        "3. Write a quick reproduction that exercises the NEW behavior (create a temp file "
        "and invoke the CLI, or a short python snippet) and confirm it actually works.\n"
        "4. Run the existing suite (`python -m pytest -q`) to confirm no regressions.\n"
        f"5. Only finish with `echo {SUBMIT_SENTINEL}` once you have edited the source AND your "
        "reproduction confirms the new behavior — never submit after only reading or exploring.\n"
    )


def render_observation(result: dict) -> str:
    output = result.get("output", "")
    rc = result.get("returncode")
    if len(output) < _TRUNC:
        return f"<returncode>{rc}</returncode>\n<output>\n{output}\n</output>"
    half = _TRUNC // 2
    return (
        f"<returncode>{rc}</returncode>\n"
        f"<output_head>\n{output[:half]}\n</output_head>\n"
        f"<elided>{len(output) - _TRUNC} characters elided. "
        "Use head/tail/grep to narrow output.</elided>\n"
        f"<output_tail>\n{output[-half:]}\n</output_tail>"
    )
