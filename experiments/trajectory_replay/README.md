# Trace-derived SandboxFS workload

This experiment converts public Nebius OpenHands trajectories into deterministic
sandbox tool requests. It does not run an LLM or agent runtime and does not
inject an anonymous-memory holder.

The converter preserves trajectory IDs, source tool types, argument hashes, and
the ordering/mix of executable calls. Because the source rows do not contain
reliable per-turn timestamps or portable repository environments, each call is
preceded by a declared synthetic wait and executes a deterministic proxy against
the prepared `bench-medium` corpus:

| Normalized operation | Proxy |
|---|---|
| `view` | read 512 prepared dependency files |
| `search` | recursively search prepared dependency files |
| `test` | read 64 MiB from a prepared large file |
| `build` | copy 8 MiB into the private writable workspace |
| `edit` | append metadata and create one private 4 KiB file |
| `shell` | enumerate and hash the prepared file manifest |

This is an action-mix-derived tool/sandbox workload, not a faithful task replay.
Its valid claim scope is sandbox/tool DRAM and latency under a public trajectory
shape. It provides no LLM or GPU/KV-cache evidence.

## Convert

```bash
python3 experiments/trajectory_replay/convert_nebius.py \
  --input /path/to/hf-rows-1.json \
  --input /path/to/hf-rows-2.json \
  --source-revision <hugging-face-commit> \
  --max-workloads 32 \
  --max-tools 12 \
  --wait-profile-ms 250,1000,4000 \
  --output-dir /path/outside/repository/normalized
```

A measured mini-agent bundle can instead be converted with exact provider/model validation:

```bash
python3 experiments/trajectory_replay/convert_agent_pipeline.py \
  --trace-dir /durable/trace-1 --trace-dir /durable/trace-2 \
  --provider openai-codex --model gpt-5.6-terra \
  --request-class opaque-task-v1 --restore-profile full-touch \
  --output-dir /durable/normalized-agent
```

The converter requires successful independent graders by default, preserves trace IDs, command
hashes, operation order, and measured query wall times, and merges format-retry waits into the next
command boundary. Commands still execute as deterministic proxies in the matched replay. Any
`--wait-scale` other than one must be declared as time-compressed external-validity evidence.

`--wait-ms 1000` retains the old fixed-wait conversion. A wait profile creates
a deterministic, trajectory-offset sequence and labels each bucket with an
opaque `synthetic-class-N`; this is a synthetic mechanism trace and must not be
presented as measured LLM timing. A formal request-aware campaign must prepare
and checksum one fixed heterogeneous manifest. Optional `request_class` and
`restore_profile` fields on a wait event are runtime metadata; the policy never
receives that event's future `duration_ms`.

## Policies

- `static`: no proactive reclaim.
- `fixed` (and legacy alias `caden`): grace-delayed reclaim.
- `elapsed`: past global durations plus elapsed time and profitability gating.
- `request-aware`: past class-conditioned durations plus elapsed time.

Enable pre-restore separately with `--speculative-restore`; leaving it off gives
the reactive request-aware ablation. See `doc/speculative-sandbox-residency.md`
for the estimator, generation fence, movement reserve, and SLO breaker.

## Replay

Required FullCopy/static baseline:

```bash
sudo PYTHONPATH=src python3 experiments/trajectory_replay/run_campaign.py \
  --workloads-dir /path/outside/repository/normalized \
  --base bench-medium --mode baseline --policy static \
  --max-admissions 4 --max-wakes 2 \
  --output /durable-ebs/trajectory-replay/f0-s0.json
```

Fixed-grace control:

```bash
sudo PYTHONPATH=src python3 experiments/trajectory_replay/run_campaign.py \
  --workloads-dir /path/outside/repository/normalized \
  --base bench-medium --mode t1 --policy fixed \
  --reclaim-grace-ms 100 --max-reclaims 2 --max-wakes 2 \
  --output /durable-ebs/trajectory-replay/fixed.json
```

Request-aware speculative treatment (illustrative settings; preregister formal
values rather than tuning on the reported treatment):

```bash
sudo PYTHONPATH=src python3 experiments/trajectory_replay/run_campaign.py \
  --workloads-dir /path/outside/repository/normalized \
  --base bench-medium --mode t1 --policy request-aware \
  --reclaim-grace-ms 100 --hot-reserve-mib 32 \
  --restore-profile-hot-reserve full-scan=512 \
  --minimum-cold-ms 100 --max-early-wake-probability 0.20 \
  --speculative-restore --speculative-restore-lead-ms 100 \
  --max-movements 2 --confirmed-movement-reserve 1 \
  --wake-reserve-safety-mib 256 \
  --turn-slo-ms <registered-p95-slo> \
  --turn-p99-slo-ms <registered-p99-slo> \
  --output /durable-ebs/trajectory-replay/request-aware-speculative.json
```

`--restore-profile-hot-reserve PROFILE=MIB` is repeatable and overrides the
global floor for an opaque restore-profile label. Use it to reject reclaim
before removing PTEs for a preregistered strict full-touch profile, not as an
after-the-fact treatment-tuning knob.

The stock speculative path does not dispatch commands. It keeps the cgroup
frozen and can prefetch explicit immutable host files:

```text
--speculative-prefetch-root /prepared/base \
--speculative-prefetch-file /prepared/base/path/to/hot-file
```

Every file must resolve under an explicitly declared operator-controlled root;
final-component symlinks are rejected. The campaign must separately record that
the root was immutable.

For live anonymous state, root may enable the separately preflighted
`process_madvise(MADV_WILLNEED)` path:

```text
--allow-process-madvise-restore --speculative-madvise-mib 256
```

The backend validates exact `cgroup.procs` PIDs and emits byte receipts. If the
kernel/libc, privilege, PID fence, or controls are unavailable, the operation
fails closed. Do not claim speculative restoration from runs reporting zero
prepared bytes.

Optional zswap compression is also fail-closed:

```text
--allow-zswap-compression --reclaim-tier compressed --zswap-max-mib 512
```

or use `--compression-max-wait-ms` to choose zswap only for shorter predicted
waits and SSD for longer waits. zswap remains host DRAM and is reported
separately from SSD swap.

Use `--synchronize-response-commits` for the burst robustness campaign; all
workloads must then have equal tool counts. Do not globally drop page caches on
a shared host. If cache-drop results are collected on an isolated host, report
them separately.

Use the same normalized manifest, request count, wait scale, host, base, CPU
pinning, and storage configuration for every comparison. Run fixed, elapsed,
request-aware reactive, and request-aware speculative in counterbalanced order
with at least three repetitions. `summarize_campaigns.py` rejects mismatched
manifests, but statistical aggregation and confidence intervals remain a
separate campaign step.

## Concurrency/density sweep

The final headline uses concurrently active sandboxes per host on x:
`N=1,2,4,8,16,32`. At each `N`, FullCopy/static and the preregistered Crate
policy must complete the same task IDs and tool-turn count. Report aligned
p95/guard, p99/guard, time-weighted host DRAM, and completed turns/s, with
run-level points and confidence intervals. Register a fixed absolute SLO as
well as FullCopy-relative guards before collection. Maximum density is the
largest measured `N` satisfying every registered guard with no excess failures;
do not interpolate a crossing.

Use `--requests TOTAL --active-sandboxes N` to hold the fixed trace queue constant while the
runner processes bounded waves of at most `N`. For example, `--requests 32
--active-sandboxes 4` executes eight waves while retaining one scheduler and bounded pool. Reports
include the configured cap, wave count, observed sandbox peak, active/trace duration, and completed
turns/s. `--requests N` without `--active-sandboxes` retains the one-wave behavior for backward
compatibility.

A ready reserve is additional resident state: it may make observed sandbox count exceed leased
population `N`, and its preparation is included in active-duration and memory accounting. Validate
equal IDs and turns at every point. Do not label a time-compressed or deterministic-proxy sweep as
the unscaled real-agent headline.

The 2026-08-30 fixed-queue qualification used 32 identities, 0.1x waits, and all
six N values. Crate selected zero reclaim and missed at least p95 through N=16;
it passed same-N relative guards only at N=32 after FullCopy collapsed. Because
that campaign did not preregister a fixed absolute SLO, it is preserved as a
load crossover rather than maximum density.
