# Claude Code tool-call stream trace

Parses the `claude -p --output-format stream-json --include-partial-messages` event stream to
show **which tool the model is about to call** — the tool *name* appears at
`content_block_start`, before its arguments finish streaming.

> Companion bundle **`combined-proc-maps`** records the *same run's* `/proc/<pid>/maps` at
> 1 Hz + the process tree. Both share the same elapsed clock, so they cross-reference
> (e.g. `Bash` at 16.3 s here ↔ subprocess + RSS at t16 there).

## Run
Sonnet 4.6, 5 turns, $0.07. Task: write+run `fib.py`, write+run `worker.py`
(`worker.py` does `subprocess.Popen(["sleep","3"])`). Tool calls: `Write`, **`Bash`**
(`python3 fib.py`), `Write`, **`Bash`** (`python3 worker.py`).

## Files
| file | what it is |
|---|---|
| `trace.txt` | parsed timeline. Columns: `L<n>` raw.jsonl line · elapsed s · event |
| `raw.jsonl` | raw `stream-json` events (the token trace) |
| `capture_stream.py` | the parser (stdin → raw.jsonl + trace.txt) |
| `agent_workspace/` | the files the agent wrote (`fib.py`, `worker.py`) |

## Key point
The tool *name/class* is known at `content_block_start` (`Bash` at 8.4 s, 16.3 s) before the
tool runs, and the command is readable as it streams — so you can anticipate the upcoming
`TOOL_BURST` (`Bash` running a build/test = heavy → pre-thaw + boost; `Read` = light).
Produced on AWS EC2 (Linux 7.0) with `--allowedTools "Bash" "Write" …`; `claude` v2.1.169.
