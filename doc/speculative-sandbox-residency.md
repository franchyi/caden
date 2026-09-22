# Sandbox-only predictive residency

Status: implemented and unit-tested; complete proxy/sparse qualifications failed, absolute-SLO campaign pending

Scope: sandbox cgroups only; no model-server or KV-cache scheduling

## Purpose

The fixed-grace policy can save DRAM but puts reclaim and restore on the tool
turn's critical path. A synchronized response burst can also queue confirmed
wakes behind background movement. This implementation adds a sandbox-only
control path that decides whether reclaim is profitable and prepares likely
returns before `RESPONSE_WAKE`, while preserving a strict execution fence.

The claim gate remains experimental. A complete 32-instance proxy queue selects
zero reclaim and misses at least p95 through N=16; N=32 passes same-N relative
guards only after FullCopy collapses. Matched fixed/elapsed controls and a
reset-corrected sparse frontier find no nonzero point inside both tails. The
code does not establish a lifecycle win until request-aware policy improves
memory under an unscaled, preregistered absolute turn-latency SLO.

## Information boundary

Caden accepts only:

- an opaque sandbox/request identity and stage generation;
- a bounded `request_class` and opaque restore-profile name;
- elapsed time, past completed wait durations, byte counts, and deadlines; and
- generation-fenced probability/horizon hints.

It does not require prompts, tokens, tool arguments, model hidden state, or any
KV tensor. The estimator learns only when a real `RESPONSE_WAKE` commits the
corresponding model wait. A workload's declared future wait duration is never
passed to the policy.

`StageContext` carries the class/profile at `LLM_WAIT`. `RestoreHint` is
reversible and rejected when its generation is stale. Sandbox dispatch remains
forbidden during speculative preparation.

## Policy modes

`CadenPolicyConfig.residency_mode` selects one of three controls:

- `FIXED_GRACE`: reclaim after one configured grace; this preserves the old
  behavior and is the fixed-grace ablation.
- `ELAPSED`: use the global empirical conditional return distribution and
  current elapsed time, without a request class.
- `REQUEST_AWARE`: use class-specific completed history after a minimum sample
  count, with global history and a smooth prior as sparse-data fallbacks.

For elapsed time `e` and horizon `d`, the empirical estimator reports
`P(T-e <= d | T > e)` and conditional expected remaining time. The estimate,
source, sample count, and survivor count are emitted with every `hazard` event.

## Reclaim decision

After the grace interval, Caden observes the sandbox cgroup and computes:

```text
target = max(0, memory.current - resident_hot_floor)
cold_window = E[remaining] - p95(demote) - restore_lead
utility = target * max(0, cold_window)
```

Reclaim is admitted only if:

1. `target` exceeds the configured minimum;
2. the predicted cold window exceeds the minimum useful residency;
3. return probability during demote plus restore is below the early-wake cap;
4. byte-seconds saved exceed the configured utility floor; and
5. the SLO breaker is closed.

The hot floor bounds the requested reclaim amount; Linux reclaim is best effort,
so reports retain requested, before, after, and actual reclaimed bytes.
Observed per-tier demote/restore p95 replaces configured startup costs as
samples accumulate.

## Selective reclaim and compression

`DemotionRequest` supports:

- `BALANCED`: ordinary cgroup-v2 `memory.reclaim`;
- `FILE_ONLY`: `memory.reclaim` with `swappiness=0`; and
- `ANON_ONLY`: `memory.reclaim` with the documented maximum `swappiness=200`.

SSD demotion uses the host's explicitly configured swap backend. The optional
`COMPRESSED` tier uses anonymous-only reclaim into zswap. It is fail-closed:
`allow_zswap_compression` must be set, global zswap must be enabled, and the
sandbox cgroup must expose `memory.zswap.current` and
`memory.zswap.writeback`. Caden writes `memory.zswap.writeback=0` while parked so
a compression treatment cannot silently become SSD writeback. Compressed bytes
remain host DRAM and are reported separately; they are never credited as SSD
residency.

`compression_max_wait_seconds` can select compression for shorter predicted
waits and SSD for longer waits. This is a latency/space tier choice, not a claim
that compression itself hides restore latency.

## Speculative preparation and commit fence

When the conditional return hazard crosses the threshold (or an accepted hint's
deadline approaches), Caden submits a bounded speculative restore. The stock
SandboxFS adapter performs only host-side preparation:

1. the sandbox cgroup remains frozen;
2. files resolving under explicitly configured operator-controlled immutable
   roots may be read into page cache (final symlinks are rejected); and
3. when separately enabled after host preflight,
   `process_madvise(MADV_WILLNEED)` may be applied to readable anonymous ranges
   of the exact PIDs listed by that sandbox's `cgroup.procs`.

The adapter opens a pidfd before validation, then checks every PID against the
sandbox's resolved cgroup before advising it, preventing PID-reuse redirection.
`process_madvise` exposes address metadata to the backend but no
page contents to Caden. Both host-file prefetch and process advice have explicit
byte caps and receipts. Unsupported kernels, missing privilege, moved PIDs, and
unavailable cgroup controls fail closed.

Speculation does **not** thaw the cgroup, invoke `sandboxfsctl exec`, or dispatch
an agent command. `RestoreResult.ready_for_dispatch` is therefore false for the
stock speculative path. Only the real `RESPONSE_WAKE` may thaw and perform the
configured confirmed prewarm. A legacy execution plugin without the selective
no-dispatch API is never used for speculative restore.

## Burst control and reserves

A shared priority movement gate bounds reclaim and restore together:

1. confirmed restores have highest priority;
2. speculative restores are next; and
3. reclaims are lowest priority.

`confirmed_movement_reserve` can keep part of the movement capacity unavailable
to background work. Before submission, speculation also reserves its estimated
restore footprint and requires `MemAvailable` to cover that footprint plus the
fixed and hazard-weighted confirmed-wake reserves; queued speculations are
counted against later checks. Confirmed wakes retain the existing wake-slot
bound through `RESULT_PACK`. Speculation never overtakes an uncommitted reclaim for
the same sandbox; a confirmed wake instead changes the stage generation, making
the queued reclaim stale.

Predictive admission computes a hazard-weighted wake reserve from each waiting
sandbox's restore footprint. Sparse-history requests retain the configured
per-waiter floor, and an independent safety reserve remains available for burst
error. This reserve is separate from movement concurrency.

## SLO breaker

The scheduler records wake latency at `RESPONSE_WAKE` and full tool-turn latency
at `RESULT_PACK`. If the rolling p95 or p99 exceeds its explicitly registered
absolute SLO after the minimum sample count, the breaker:

- rejects new reclaim while open;
- schedules non-dispatching preparation for already reclaimed waiters;
- preserves confirmed-wake movement priority; and
- remains open for the configured cooldown.

A breaker event includes metric, quantile, observed value, threshold, sample
count, and cooldown. A disabled (`0`) threshold preserves prior behavior.
Campaigns must register the threshold from the evaluation protocol; the breaker
does not infer a favorable baseline after seeing treatment results.

`restore_profile_hot_reserve_bytes` adds a content-free per-profile safety
floor. A profile whose expected first touch spans its full WSS can retain all
estimated resident bytes and fail the profitability test before any PTEs are
removed, while profiles with smaller hot sets remain reclaim-eligible. The
profile is an opaque bounded label, not a tool argument or memory-content
inspection.

## Physical-Linux mechanism qualification

A 2026-08-30 shared-host `nsl17` run exercised balanced reclaim and pidfd-fenced
`process_madvise` on a persistent synthetic 256-MiB working set. Across three
repetitions and 36 trials per treatment, response-to-full-scan p50/p95 was
1,050.8/1,063.0 ms with reactive refault and 175.5/183.3 ms with speculative
preparation, including 23.2/27.7 ms of work that crossed the fixed response
boundary. The hot-resident no-reclaim floor was 34.3/42.0 ms; the reactive
number is not the formal `F0-S0`/FullCopy baseline, which this mechanism run did
not include. Preparation took 423.1/427.6 ms and reduced the p50
demote-to-scan major-fault delta from 8,050 to 51; that v1 counter includes
faults incurred during preparation. The Ubuntu 6.8 kernel rejected swappiness selectors, so this run
qualifies `BALANCED`, not `ANON_ONLY`, and zswap was disabled.

A follow-up direct-path campaign removed the external polling utilities and
redundant confirmed prewarm and used a 600-ms lead. Across another three
repetitions and 36 trials per configuration, fully reclaimed speculative
p50/p95/p99 was 100.56/108.28/126.38 ms. Preparation p99 was 465.08 ms and
maximum boundary overrun was only 0.114 ms, so unfinished preparation no longer
explains the result. The matched frozen resident control reached
44.46/48.63/49.97 ms with zero reclaim. Fully reclaimed preparation restored
cgroup DRAM but left 65,802/65,862 post-preparation page faults at p50/p95;
remote `MADV_WILLNEED` did not install missing PTEs, and Ubuntu 6.8 rejected
remote `MADV_POPULATE_READ` with `EINVAL`. Thus the safe 42--60 ms decision for
this full-WSS profile is to reject reclaim.

These fixed-boundary microbenchmarks validate only host actuation, direct-path
cost, and the need for profile-specific residency. They contain no past-only
prediction, equal-work trajectory, density result, LLM, KV cache, or formal
FullCopy treatment. The preserved packages are
`crate-paper/docs/performance/orca/orca-speculative-restore-2026-08-30/` and
`crate-paper/docs/performance/orca/orca-direct-restore-2026-08-30/` in the
companion paper repository.

A successor shared-host campaign expanded four successful exact
`openai-codex/gpt-5.6-terra` traces to a fixed 32-instance queue and replayed
0.1-scale waits at `N={1,2,4,8,16,32}`. Its 36 FullCopy/Crate reports completed
7,488/7,488 turns. Request-aware Crate selected zero reclaim and missed at least
p95 through N=16. It passed same-N relative guards only at N=32, where FullCopy
collapsed; p95/p99 ratios were 0.075/0.088 and throughput was 7.08x. Without a
fixed absolute SLO, this is CoW load resilience rather than density-at-SLO.
Matched N=8 fixed/elapsed controls saved about 58.5% local cgroup DRAM but
failed tails with about 7.9--8.0K major faults/run.

A corrected sparse mechanism frontier now requires
`--reset-between-trials`: before each timed trial the holder touches every
4-KiB page, and schema v6 reports reset time separately. E3-v1's 18 reports are
preserved but invalid for treatment comparison because sparse scans produced
starting-state carryover. E3-v2 kept all floors/timing fixed and completed 288
trials. No nonzero point passed both resident guards; even 4.67 MiB p50 reclaim
produced p95/p99 ratios 1.216/1.208, and preparation returned cgroup DRAM to
about 263 MiB. The combined package is
`crate-paper/docs/performance/orca/nsl17-figure-campaign-2026-08-30/`.

## Evaluation controls

`experiments/trajectory_replay/run_campaign.py` accepts `static`, `fixed`,
`elapsed`, and `request-aware` policies (`caden` remains a fixed-policy alias for
old scripts). It also exposes hot-floor, movement-cost, hazard, compression,
movement-reserve, prefetch/process-madvise, and SLO-breaker settings.

The formal sandbox gate should run the same heterogeneous wait trace and equal
work under:

1. fixed grace;
2. elapsed-only profitability;
3. request-aware reactive restore; and
4. request-aware speculative restore.

Use counterbalanced order, at least three runs, confidence intervals, and a
separate synchronized-response-commit campaign. Report host DRAM, cgroup DRAM,
zswap, swap, movement queues, prepared bytes, major faults, wake p50/p95/p99,
turn p50/p95/p99, throughput, failures, and breaker activity. No result from
this path supports a GPU or KV-cache claim.
