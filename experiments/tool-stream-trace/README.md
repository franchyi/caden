# Claude Code tool-call streaming trace — "tool name in advance"

A captured `claude -p` streaming session showing that **the tool *name* is available the
moment a tool call begins (`content_block_start`), before its arguments finish streaming**
(`input_json_delta` chunks). The gap — the **lead** — is the time the rest of the system
gets to anticipate the upcoming tool/`TOOL_BURST` before it's fully specified.

## What the run shows

Task given to Claude (Sonnet 4.6): *"Create calc.py with add(a,b); read it; then edit it to
add subtract(a,b)."* → it called three distinct tools. Lead time = `input complete` −
`name known`:

| tool  | name appears | input complete | **lead** | why |
|-------|--------------|----------------|----------|-----|
| Write | 5.42 s | 7.81 s | **2.39 s** | the whole file `content` streams in |
| Read  | 9.59 s | 9.84 s | **0.25 s** | tiny input (`file_path` only) |
| Edit  | 11.63 s | 12.64 s | **1.02 s** | `old_string` / `new_string` stream in |

So `name` is known at block start; bigger tool inputs (Write/Edit) buy more lead than small
ones (Read).

## Files

| file | what it is |
|---|---|
| `trace.txt` | the parsed timeline — start here. Columns: `L<n>` raw.jsonl line · elapsed s · event |
| `raw.jsonl` | the raw `stream-json` events, one per line — the actual token trace |
| `capture_stream.py` | the parser: reads the stream on stdin, writes `raw.jsonl` + `trace.txt` |
| `stderr.log` | claude's stderr (warnings); empty here |

## How it was produced

```
printf '<task>' | claude -p \
  --output-format stream-json --include-partial-messages --verbose \
  --model sonnet --permission-mode acceptEdits \
  2> stderr.log | python3 capture_stream.py raw.jsonl trace.txt
```
- `--output-format stream-json` → newline-delimited JSON events as the turn unfolds.
- `--include-partial-messages` → token/partial events (this is what surfaces the tool name
  early + the streaming args). Without it, the tool call is only visible in the final
  `assistant` message, after the block is already complete — no lead.
- Run locally; `claude` v2.1.169 (Bun-compiled native binary).

## Event schema (what `capture_stream.py` keys on)

Each `stream-json` line of `type:"stream_event"` wraps an Anthropic SSE event:
- `content_block_start` with `content_block:{type:"tool_use", id, name}` → **the tool name**.
- `content_block_delta` with `delta:{type:"input_json_delta", partial_json}` → the tool's
  arguments, streaming in as partial JSON.
- `content_block_stop` → arguments complete.

Other line types: `system` (init), `assistant` (the resolved message, incl. the full
`tool_use`), `user` (the `tool_result` coming back), `result` (final: cost, turns, usage).

## Why this matters (ORCA)

At `content_block_start` you know *which* tool is coming before you know *what* it does —
an anticipatory `TOOL_BURST` signal: classify by name (`Bash`/`pytest`/build = heavy compute
imminent → pre-thaw + CPU-boost the sandbox; `Read`/`Glob`/`Grep` = light → no action), and
optionally sniff the early `input_json_delta` for the command. The lead (here 0.25–2.4 s)
is the head start to schedule *before* the burst lands.
