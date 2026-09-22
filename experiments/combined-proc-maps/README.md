# Claude Code /proc/<pid>/maps record (1 Hz) + process tree

1 Hz sampling of the claude process's virtual-memory layout + RSS over one session, plus the
process tree showing the subprocesses the `Bash` tool forks.

> Companion bundle **`combined-tool-trace`** parses the *same run's* tool-call stream. Both
> share the same elapsed clock, so they cross-reference (e.g. RSS/subprocess at t16 here ↔
> `Bash` tool call at 16.3 s there).

## Files
| file | what it is |
|---|---|
| `series.tsv` | per-second summary. Columns: `elapsed` · `vma` · `rss_kb` · `anon_kb` |
| `maps/tNNNN.maps` | raw `/proc/<pid>/maps` snapshots, one per second (23) |
| `procs.txt` | process-tree transitions (`claude → bash → python3 → sleep` on Bash runs) |
| `mapsample.py` / `procsample.py` | the samplers (`python3 <script> <claude_pid> [out]`) |
| `agent_workspace/` | the files the agent wrote (`fib.py`, `worker.py`) |

## Key observations
- **Cold start** (`series.tsv`): 1.2 MB → 168 MB (t1) → 238 MB (t2) as the JSC heaps map in;
  climbs to ~268 MB. VMA count oscillates 200–900 (JIT/GC, subprocess forks).
- **Resident set ~250 MB**, mostly anonymous JSC heaps — the working set a freeze/demote
  policy would move. (Maps are *virtual* sizes; RSS is from `smaps_rollup`.)
- **Forked subprocesses** (`procs.txt`): `bash → python3` @8.6 s (`python3 fib.py`) and
  `bash → python3 → sleep` @17.0 s (held ~3 s) — the real `TOOL_BURST`s, lining up with the
  `Bash` tool calls in `combined-tool-trace/trace.txt` (8.4 s / 16.3 s).

Produced on AWS EC2 `m7i.xlarge`, Linux 7.0.0-1006-aws, `claude` v2.1.169 (`/proc` needs Linux).
