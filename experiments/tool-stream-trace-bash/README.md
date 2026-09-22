# Claude Code tool-call streaming trace — Bash tool + real subprocess

Like `tool-stream-trace/`, but the agent calls the **`Bash`** tool, which **forks real
subprocesses** — the genuine `TOOL_BURST`. This bundle captures, side by side, (a) the
**streaming token trace** (the tool *name* is known at `content_block_start`, before the
tool runs) and (b) the **live process tree** of the claude process (the spawned
`bash → python3 → sleep` chain), so you can see "stream detects `Bash`" → "subprocess
appears".

## What the run shows

Task (Sonnet 4.6): write `fib.py` and run it; write `worker.py` that does
`subprocess.Popen(["sleep","3"])` and run it. → 5 turns, $0.06. Tool calls: Write, Write,
**Bash** (`python3 fib.py`), **Bash** (`python3 worker.py`).

**Stream (`trace.txt`)** — tool name at block start vs input complete:

| tool | name appears | input complete | lead | note |
|---|---|---|---|---|
| Write | 3.77 s | 5.04 s | 1.28 s | file `content` streams |
| Write | 5.04 s | 5.63 s | 0.59 s | |
| Bash  | 7.51 s | 7.52 s | 0.01 s | short command → tiny input, but **name/class known at block start** |
| Bash  | 9.78 s | 9.79 s | 0.01 s | (`python3 worker.py`) |

**Process tree (`procs.txt`)** — the second Bash spawned a real subprocess chain:
```
 0.02s  claude(1400)
 9.85s  claude(1400) | bash(1663) | python3(1698) | sleep(1702)   <- TOOL_BURST: Popen(sleep 3)
12.91s  claude(1400)                                              <- ~3s later, subprocess gone
```

So `Bash` is detected in the token stream at **9.78 s**, and the real subprocess tree is
live by **9.85 s** (~70 ms later), holding for ~3 s. The tool *class* (Bash = heavy/forking
vs Read = light) is known at `content_block_start` — the anticipatory signal — even though
a short command's argument `lead` is tiny.

## Files

| file | what it is |
|---|---|
| `trace.txt` | parsed stream timeline. Columns: `L<n>` raw.jsonl line · elapsed s · event |
| `raw.jsonl` | raw `stream-json` events (the token trace) |
| `procs.txt` | process-tree transitions of the claude process (subprocess spawns/exits) |
| `agent_workspace/` | the files the agent wrote (`fib.py`, `worker.py`) |
| `capture_stream.py` | the stream parser (stdin → raw.jsonl + trace.txt) |
| `procsample.py` | the process-tree sampler (`python3 procsample.py <claude_pid>`) |
| `stderr.log` | claude stderr (empty) |

## How it was produced (on AWS EC2)

```
printf '<task>' | claude -p \
  --output-format stream-json --include-partial-messages --verbose --model sonnet \
  --allowedTools "Bash" "Write" "Read" "Edit"        # pre-grant tools (no --dangerously-skip-permissions; works as root)
  > stream.fifo 2> stderr.log &
CL=$!
python3 capture_stream.py raw.jsonl trace.txt < stream.fifo &   # stream -> trace
python3 procsample.py $CL > procs.txt &                         # process tree of the claude pid
```
Host: AWS EC2 `m7i.xlarge`, Linux 7.0.0-1006-aws, `claude` v2.1.169 (Bun native binary).
`--allowedTools "Bash"` pre-approves the Bash tool in headless `-p` mode without the
root-blocked `--dangerously-skip-permissions`.

## Why this matters (ORCA)

At `content_block_start` you know the tool *class* before it executes: `Bash`
(esp. running builds/tests/scripts) forks a subprocess and is a heavy `TOOL_BURST` →
pre-thaw + CPU-boost the sandbox; `Read`/`Glob`/`Grep` are light → no action. `procs.txt`
is the ground truth that a real subprocess burst follows the stream signal.
