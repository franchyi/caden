# agent_pipeline

A minimal **"mini-agent"** (modeled on SWE-agent/mini-swe-agent's text-based protocol) that drives
a no-tools model backend to solve coding tasks, executes every shell command in an owned
sandbox, and records model-wait versus tool-burst timing. The original pipeline uses Claude plus
bubblewrap for SWE-rebench; the remote capture path uses Pi plus a persistent SandboxFS sandbox.

Built as a precursor to Caden's stage-aware scheduling: each run records per-turn
`LLM_WAIT` (waiting on the model) vs `TOOL_BURST` (executing in the sandbox) timing.

---

## What one run does

For each SWE-rebench instance id:

1. **fetch** — look the id up in the HF dataset, clone the repo at its base commit, build a venv.
2. **run the agent** — a loop where `claude -p` proposes exactly one ```` ```bash ```` command per
   turn; each command runs in a fresh bubblewrap sandbox; repeat until the agent submits.
3. **evaluate** — apply the instance's gold `test_patch`, run its `FAIL_TO_PASS` + `PASS_TO_PASS`
   tests, decide `resolved`.
4. **write** `results/<id>.json` — verdict, steps, wall time, and `stage_seconds`.

## Architecture (v1)

- **Agent backend = no-tools CLI.** `claude -p` remains the SWE-rebench default. `PiCliModel`
  invokes an exact Pi provider/model with `--no-tools`, no extensions, no context files, and no
  session persistence; the mini-agent alone parses and executes one bash action per turn.
- **Sandbox = per-command bubblewrap** — the agent loop runs on the host; each command executes in a
  fresh `bwrap` with the repo bind-mounted. (This captures stage *timing*; the "whole agent inside one
  cgroup" placement that Caden's freeze/reclaim *enforcement* wants is a later upgrade — see `PLAN.md`.)
- Pure-Python units are unit-tested anywhere; the bubblewrap / `claude` / fetch parts need a Linux node.

---

## Data flow — how an `instance_id` becomes a task

`tasks.txt` is **only an index** (a list of instance ids). The actual task data lives in the
HuggingFace dataset and is materialized per-id at fetch time:

```
tasks.txt                     pipeline.py reads it, loops over each instance_id
  simonw__files-to-prompt-30  ─┐
  simonw__files-to-prompt-16  ─┤  fetch_task.py: fetch(instance_id, …)
                               ▼
THE REAL DATASET ── HuggingFace `nebius/SWE-rebench`, split "test" (~21,336 instances)
  cached at:  ~/.cache/huggingface/datasets/nebius___swe-rebench/…/*.arrow   (first load downloads it)
                               │  ds.filter(lambda r: r["instance_id"] == "<id>")  → ONE row
                               ▼  row = {repo, base_commit, problem_statement, patch, test_patch,
                               │         FAIL_TO_PASS, PASS_TO_PASS, …}
                               ▼
work/<instance_id>/           the task, materialized to run
  repo/                        git clone github.com/<repo> @ base_commit  (+ uv venv, pip install -e .)
  task.json                    {problem_statement, test_patch, fail_to_pass, pass_to_pass,
                                repo_dir, venv_python}
```

| Layer | What it is | Where |
|---|---|---|
| `tasks.txt` | index — *which* instances to run (ids only) | this directory |
| the dataset | *all* task definitions | HF `nebius/SWE-rebench`, cached under `~/.cache/huggingface/datasets/` |
| `work/<id>/` | *one* task materialized to run | cloned repo + venv + `task.json` |

`instance_id` is the primary key into the dataset; nothing in `tasks.txt` carries task content.
The lookup is one line in `fetch_task.py`:

```python
ds = datasets.load_dataset("nebius/SWE-rebench", split="test")     # the real data (cached .arrow)
row = ds.filter(lambda r: r["instance_id"] == instance_id)[0]      # id → one row
```

Inspect any instance without running it:
```sh
uv run python -c "import datasets; ds=datasets.load_dataset('nebius/SWE-rebench',split='test'); \
  r=ds.filter(lambda r: r['instance_id']=='simonw__files-to-prompt-30')[0]; \
  print(r['problem_statement']); print(r['FAIL_TO_PASS'])"
```

---

## Prerequisites (a new Linux node)

bubblewrap is **Linux-only** — the full pipeline cannot run on macOS/Windows (unit tests can; see below).

- **Linux with unprivileged user namespaces enabled.** Verify both:
  ```sh
  cat /proc/sys/kernel/unprivileged_userns_clone     # 1 on Debian/Ubuntu (may be absent elsewhere)
  bwrap --ro-bind / / --tmpfs /tmp --proc /proc --dev /dev --unshare-user --new-session echo ok
  ```
- **bubblewrap** (`bwrap --version`), **uv**, **git**, **curl**, **jq** (jq only for ad-hoc dataset queries).
- A **system `python3`** — the per-task venv is built from `/usr/bin/python3` on purpose, so the
  interpreter + stdlib live under `/usr` (which the sandbox bind-mounts). A `uv`-managed Python would
  sit outside the binds and be a dangling symlink inside the sandbox.
- **Claude Code installed and authenticated**, so headless mode works (this is the agent's model
  backend; no API key required). Verify:
  ```sh
  claude -p "reply with one bash code block that runs: echo ok" --output-format json \
    --disallowedTools Bash Edit Write Read Glob Grep WebFetch WebSearch TodoWrite Task NotebookEdit \
    | jq -r .result
  ```
- **Network egress** to `huggingface.co` (dataset), `github.com` (repo clones), and Anthropic
  (`api.anthropic.com`, for `claude -p`).
- **Disk**: the SWE-rebench test split is ~200 MB of parquet; the first dataset load downloads it to
  `~/.cache/huggingface`.

---

## Exact Pi provider/model capture on remote SandboxFS

`capture_pi_remote.py` keeps model credentials on the client while every proposed command runs in
one fresh persistent sandbox on a remote Linux host. For example:

```bash
PYTHONPATH=. uv run python capture_pi_remote.py \
  --host nsl17 --ctl /path/to/sandboxfsctl-wrapper \
  --socket /run/campaign.sock --base task-base --sandbox-id trace-1 \
  --instance-id task-1 --task-file task.txt --grader-file grader.sh \
  --output-dir /durable/trace-1 \
  --provider openai-codex --model gpt-5.6-terra --thinking high
```

The ctl wrapper may use narrowly configured passwordless `sudo` when the isolated daemon socket is
root-owned. The runner records every model call's returned provider, model, API, usage, cost, and
response ID, runs an independent grader, always destroys the sandbox, and fails the process when
the task does not resolve. `agent_pipeline/remote_sandboxfs.py` uses `exec-json`; streamed `exec`
output is not a receipt. Preserve failed captures and exclude them explicitly rather than silently
replacing them.

## Run the Claude SWE-rebench pipeline

### A) Directly on the Linux node (simplest)

```sh
cd agent_pipeline
uv run python pipeline.py tasks.txt          # uv auto-creates the venv + installs deps on first run
# or a single instance, bypassing tasks.txt:
uv run python pipeline.py simonw__files-to-prompt-30
```

Results are written to `results/<instance_id>.json`.

### B) Develop on one machine, run on a remote Linux node

From a dev box that can't run bwrap (e.g. macOS), `sync.sh` rsyncs this directory to a node and runs
it there. Requires passwordless `ssh <host>`.

```sh
REMOTE_HOST=mynode ./sync.sh tasks.txt
```

Defaults: `REMOTE_HOST=nsl7s`, `REMOTE_DIR=caden-pipeline`.

### Knobs (environment variables)

- `AGENT_MODEL` — pin the model, e.g. `AGENT_MODEL=opus` (default: Claude Code's configured default).
- `STEP_LIMIT` — max agent turns before giving up (default `40`).

```sh
AGENT_MODEL=opus ./sync.sh tasks.txt                       # remote
AGENT_MODEL=opus uv run python pipeline.py tasks.txt        # on the node
```

> Re-running an instance reuses `work/<id>/` if `task.json` exists. To force a clean checkout
> (recommended between agent runs), delete it first: `rm -rf work/<id>`.

---

## Local unit tests (no Linux / bubblewrap needed)

The pure-Python units (parser, prompts, loop mechanics, sandbox argv, scoring) run anywhere:

```sh
uv venv && uv pip install -e ".[dev]"
uv run pytest -q
```

---

## Output

`results/<instance_id>.json`:

```json
{
  "instance_id": "simonw__files-to-prompt-30",
  "resolved": false,
  "verdict": { "fail_to_pass": {"<test id>": 0|1}, "pass_to_pass": {"<test id>": 0|1}, "resolved": false },
  "exit_status": "submitted" | "step_limit",
  "steps": 6,
  "wall_seconds": 55.8,
  "stage_seconds": { "LLM_WAIT": 54.9, "TOOL_BURST": 0.9 },
  "stage_events": [ {"stage": "...", "start": ..., "end": ..., "step": ...}, ... ],
  "messages": [ ...full agent transcript... ]
}
```

`resolved = every FAIL_TO_PASS test passes AND every PASS_TO_PASS test still passes` (the SWE-bench
rule). **PASS_TO_PASS passing alone only means "no regressions"** — the feature/fix itself is what the
hidden `FAIL_TO_PASS` tests check.

---

## Validate an instance (gold check)

Confirm an instance is actually solvable and that the scoring harness can reach `resolved`, by applying
the gold solution to a fresh checkout:

```sh
uv run python gold_check.py simonw__files-to-prompt-30 simonw__files-to-prompt-16
# -> {"instance": "...", "applied": {...}, "gold_resolves": true, "f2p_failed": [], "p2p_failed": []}
```

If `gold_resolves` is **false**, the instance is broken/mismatched. (Real example: `files-to-prompt-36`
gold-resolves, but its `problem_statement` is a Windows-UTF-8 fix while its `FAIL_TO_PASS` is
`test_line_numbers` — so an agent reading the prompt cannot pass the test. Prefer instances where the
problem statement matches the tests.)

---

## Key files

| File | Responsibility |
|---|---|
| `tasks.txt` | index of instance ids to run |
| `pipeline.py` | orchestrate fetch → agent run → evaluate → `results/<id>.json` |
| `fetch_task.py` | resolve an `instance_id` against `nebius/SWE-rebench`; clone repo + build venv |
| `evaluate.py` | apply gold `test_patch`; run FAIL_TO_PASS / PASS_TO_PASS |
| `gold_check.py` | validate an instance (gold patch + tests → should resolve) |
| `sync.sh` | rsync this dir to a Linux node and run it there |
| `agent_pipeline/agent.py` | the loop (parse one command, run it, observe), with stage marks |
| `agent_pipeline/model_claude_cli.py` | `claude -p` no-tools model backend |
| `agent_pipeline/model_pi_cli.py` | exact Pi provider/model no-tools backend with JSONL receipts |
| `agent_pipeline/remote_sandboxfs.py` | persistent remote SandboxFS command environment |
| `capture_pi_remote.py` | capture and grade one Pi-driven remote trace |
| `agent_pipeline/sandbox.py` | per-command bubblewrap execution |
| `agent_pipeline/prompts.py` | system / instance / observation / format-error prompts |
| `agent_pipeline/parsing.py` | extract the single bash command from a model reply |
| `agent_pipeline/scoring.py` | gold-test resolution logic |
| `PLAN.md` | full design + task breakdown + the architecture caveat |

## Notes / limitations

- The v1 agent is deliberately minimal; **solve-rate is a tuning axis** (model, prompt, step budget),
  separate from pipeline correctness.
- Stick to small, pure-Python instances for clean installs (we used `simonw/files-to-prompt`); arbitrary
  SWE-rebench repos may need system deps or specific Python versions the simple venv setup won't provide.
- Network is left **on** inside the sandbox for v1 (so `pip`/tooling work); tighten with `--unshare-net`
  later if you want a stronger boundary.
- AWS EC2 port is deferred — see `PLAN.md`.
