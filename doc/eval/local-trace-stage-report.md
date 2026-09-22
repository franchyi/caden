# SWE-Rebench Claude Code Stage Pattern Analysis

Generated: 2026-06-01 11:48:20 UTC

Input root: `agentcgroup/experiments`

Workload source: local `agentcgroup` experiment traces from SWE-rebench/SWE-bench-style coding tasks.

## Result

Analyzed 157 valid timestamped Claude Code traces. Across observed tool and LLM-wait stages, LLM wait accounts for 66.3% and local tool execution accounts for 33.7%.

134 traces (85.4%) show the recurring `tool -> tool_result -> LLM_WAIT -> assistant` pattern under the conservative detector used here.

`Task` is counted as tool time in the main ratio. If treated as a nested-agent wrapper rather than local sandbox work, the ratio becomes 68.0% LLM wait and 32.0% local tool time.

## Aggregate Summary

| Metric | Value |
| --- | --- |
| Trace files found | 190 |
| Valid traces with observed stages | 157 |
| Traces with `tool_calls.json` | 180 |
| Tool calls | 7701 |
| Post-tool LLM waits | 7698 |
| Total LLM-wait time | 12.78h |
| Total local tool time | 6.50h |
| Median per-trace LLM fraction | 69.3% |
| P90 per-trace LLM fraction | 91.6% |
| Traces with LLM fraction >= 80% | 24.8% |
| Median alternation coverage | 100.0% |

## By Workload

| Workload | Traces | LLM wait | Tool time | Stage-pattern traces | Tool calls | LLM wait total | Tool total |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SWE-rebench Claude Haiku | 45 | 50.1% | 49.9% | 64.4% | 1151 | 1.00h | 59.77m |
| SWE-rebench local GLM | 112 | 68.1% | 31.9% | 93.8% | 6550 | 11.78h | 5.51h |
| SWE-bench 18-task batch | 0 | n/a | n/a | n/a | 0 | 0.00s | 0.00s |

## Tool-Time Contributors

| Tool | Calls | Total | Mean | P50 | P95 | Max |
| --- | --- | --- | --- | --- | --- | --- |
| Bash | 3727 | 5.83h | 5.63s | 5.18s | 13.19s | 5.13m |
| Task | 18 | 29.59m | 1.64m | 1.33m | 3.74m | 6.03m |
| Read | 1490 | 2.94m | 0.12s | 0.01s | 0.34s | 10.91s |
| WebFetch | 14 | 2.43m | 10.41s | 8.72s | 20.31s | 23.50s |
| WebSearch | 11 | 2.04m | 11.10s | 12.17s | 16.48s | 17.12s |
| Grep | 694 | 1.01m | 0.09s | 0.03s | 0.10s | 7.78s |
| TodoWrite | 620 | 54.31s | 0.09s | 0.01s | 0.05s | 11.29s |
| Glob | 268 | 40.91s | 0.15s | 0.03s | 0.77s | 7.75s |
| Edit | 755 | 32.62s | 0.04s | 0.03s | 0.07s | 7.34s |
| Write | 100 | 8.20s | 0.08s | 0.03s | 0.07s | 4.65s |
| BashOutput | 2 | 0.05s | 0.02s | 0.02s | 0.03s | 0.03s |
| ExitPlanMode | 1 | 0.04s | 0.04s | 0.04s | 0.04s | 0.04s |

## LLM-Dominant Multi-Step Examples

| Workload | Task | LLM wait | Tool time | LLM fraction | Waits/calls |
| --- | --- | --- | --- | --- | --- |
| SWE-rebench local GLM | simonw__files-to-prompt-16 | 2.36m | 0.45s | 99.7% | 17/17 |
| SWE-rebench local GLM | pre-commit__pre-commit-mirror-maker-64 | 1.44m | 0.60s | 99.3% | 23/23 |
| SWE-rebench local GLM | Azure__azure-cli-2955 | 4.47m | 9.89s | 96.4% | 38/38 |
| SWE-rebench local GLM | AzureAD__microsoft-authentication-library-for-python-280 | 1.49m | 5.24s | 94.5% | 15/15 |
| SWE-rebench Claude Haiku | streamlink__streamlink-3485 | 34.89s | 2.79s | 92.6% | 13/13 |
| SWE-rebench Claude Haiku | dask__dask-2205 | 3.38m | 17.56s | 92.0% | 35/35 |
| SWE-rebench Claude Haiku | facelessuser__soupsieve-147 | 41.76s | 3.94s | 91.4% | 13/13 |
| SWE-rebench Claude Haiku | simonw__files-to-prompt-44 | 46.21s | 4.71s | 90.7% | 22/22 |

## Tool-Dominant Multi-Step Examples

| Workload | Task | LLM wait | Tool time | Tool fraction | Waits/calls |
| --- | --- | --- | --- | --- | --- |
| SWE-rebench Claude Haiku | getsentry__sentry-python-2148 | 1.26m | 8.59m | 87.3% | 37/37 |
| SWE-rebench local GLM | AzureAD__azure-activedirectory-library-for-python-227 | 2.32m | 12.10m | 83.9% | 37/37 |
| SWE-rebench Claude Haiku | pre-commit__pre-commit-2524 | 53.11s | 4.16m | 82.4% | 24/24 |
| SWE-rebench Claude Haiku | sqlfluff__sqlfluff-5362 | 1.27m | 2.86m | 69.2% | 34/34 |
| SWE-rebench Claude Haiku | beeware__briefcase-2212 | 4.22m | 8.98m | 68.0% | 77/77 |
| SWE-rebench local GLM | encode__httpx-2701 | 4.24m | 8.28m | 66.2% | 50/50 |
| SWE-rebench local GLM | AzureAD__microsoft-authentication-library-for-python-530 | 3.02m | 5.45m | 64.4% | 43/43 |
| SWE-rebench Claude Haiku | tobymao__sqlglot-4014 | 1.49m | 2.63m | 63.9% | 32/33 |

## Method

- Tool stage: `tool_calls.json` interval from `timestamp` to `end_timestamp`.
- LLM wait stage: interval from a `user` event containing `tool_result` to the next `assistant` event.
- Initial LLM wait: interval from the first non-tool user prompt to the next assistant event.
- The ratio is `LLM wait / (LLM wait + local tool time)`, so it measures classified stage time rather than every byte of end-to-end wall time.

## Caveats

- The workload is SWE-rebench/SWE-bench-style coding tasks, not a generic sample of all agent workloads.
- The LLM-wait interval includes provider latency, queueing, network time, and model inference. It is still the interval where the sandbox is mostly waiting on the LLM side.
- Tool duration is wall-clock tool wrapper time. Long `Bash` calls can include test execution, build time, or command wait time.
- `Task` tool calls can contain nested agent activity, so treating them as pure local tool work is conservative for Caden.
- External SWE-agent trajectories without timestamps are useful for action sequences, but not for this timing ratio.
