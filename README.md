# Caden

Caden is a stage-aware host scheduler for dense AI-agent sandboxes.

Formerly ORCA. Current code and documents use Caden; immutable experiment
packages retain their original names and hashes. See the
[September 21 naming migration](doc/naming-migration-2026-09-21.md).

The project goal is to increase agents per host at an interactive turn-latency
SLO by treating sandboxes blocked on off-host LLM inference as cold/resumable
tenants, while giving local response wakeups and tool bursts bounded CPU and
memory priority.

## Active Crate implementation

`crate-mlsys` is the primary engineering branch for Crate's sandbox scheduling
and density work. The active local checkout is `caden/`. The former
`caden-swebench-verified/` worktree is retained as a detached reference; do not
continue implementation there. This branch contains the committed scheduling
checkpoint and the independent native CXL cold-store building block.

SandboxFS owns copy-on-write workspace construction and sandbox lifecycle.
Caden owns admission, bounded one-shot ready pools, stage-aware CPU allocation,
and wait-aware freeze/reclaim/restore. The latest implementation includes:

- Queue-aware pool replenishment using already-arrived pending requests,
  with bounded capacity and disposal of unnecessary in-flight precreations.
- An explicit confirmed thaw-only restore mode; demand faults remain charged
  to the subsequent real tool command, and speculative preparation cannot thaw.
- Per-task SandboxFS routing, real-command SWE-bench replay with recorded model
  waits, cold/first-touch timing, memory sampling, and fail-closed evidence checks.

Higher sandbox density is the objective; tool latency is a guardrail. This
checkpoint does not establish a density result or universal latency improvement.
Cross-host CXLGen migration remains on the separate `cxlgen-stage-ab` branch.
The tiering direction is single-host CXL memory tiering, not migration. The
committed base contains the mmap store in `native/cxl_coldstore/`; the current
uncommitted implementation adds the registered-memory pager, shared backend
interface, safety fixes, and serving/cache policy. CXL is optional and is not a
dependency of the DRAM-SSD implementation. It does not transparently page
arbitrary sandbox processes or OverlayFS state.
Claude's implementation handoff and Codex's review criteria are maintained in
`../crate-paper/docs/CXL_TIERING_HANDOFF.md`: implement a common memory-tier
interface with SSD/CXL backends, then test in isolated runs on nsl17. Remote login
and scoped project tests are authorized; host-wide swap changes and VM setup are
not. Verify the exact shared-DAX reservation before any CXL writes.

**Memory-tier backends (implemented; scoped safety fixes validated).** Byte movement beneath
a residency decision now goes through one interface, `caden.memory_tier.MemoryTierBackend`,
injected into `SandboxFSExecution`. `SSDTierBackend` (the default) is the
unchanged cgroup-reclaim path; `CXLTierBackend` (`caden.cxl_tier`, opt-in) drives
the native pager `native/cxl_coldstore/pagerd.c`, which copies registered
cooperative mappings into the mmap store, releases their source pages and
restores them eagerly or on demand. The pager is **not transparent**: unmodified
tools register nothing, so it places none of their memory. Read
[`native/cxl_coldstore/PAGER.md`](native/cxl_coldstore/PAGER.md) before citing
any DRAM-CXL number; isolated nsl17 runs live in `experiments/cxl_tiering/`.

See the [SWE-bench harness guide](experiments/swebench_verified/README.md).
The completed campaign's original traces, frozen measured source and analysis
remain in the sibling `crate-paper/docs/performance/swebench-verified-2026-09-19/`
package. Its historical branch names and hashes are not rewritten when this
engineering branch advances. The SandboxFS submodule remains pinned at
`652aa279bbb2afb4068d4b838e2df8e103b247fe`.

Run the local regression suite with Python and `pytest` installed:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m pytest -q -p no:cacheprovider tests
```

These tests use fixtures and mocked remote operations; they do not launch a
measurement campaign, invoke a live model, or enable host-wide swap.

## Layout

- `src/`: Caden scheduler/controller implementation.
- `experiments/`: harnesses, replay drivers, and evaluation scripts.
- `doc/`: design plan, trace analysis, and historical notes.
- `third_party/sandboxfs/`: pinned stock-Linux sandbox/workspace backend.

Active plan: `doc/2026-05-29-plan.md`.
Trace evidence: `doc/eval/local-trace-stage-report.md`.

The active SandboxFS integration, memory metric, baselines, acceptance rule,
and test plan are in `doc/sandboxfs-memory-integration.md`. The sandbox-only
predictive residency implementation—selective reclaim/compression, past-only
return hazards, non-dispatching pre-restore, movement reserves, and the SLO
breaker—is specified in `doc/speculative-sandbox-residency.md`. No KV-cache
control is implemented. Runnable campaigns live in `experiments/sandboxfs_memory/`,
`experiments/trajectory_replay/`, and `experiments/speculative_restore/`.

A 2026-08-30 direct-path physical-Linux synthetic qualification completed
preparation before the response boundary and removed polling/prewarm artifacts.
For a 256-MiB full scan, fully reclaimed speculative p50/p95/p99 remained
100.56/108.28/126.38 ms, while a matched frozen resident control reached
44.46/48.63/49.97 ms only with zero reclaim. Remote `MADV_WILLNEED` restored
cgroup DRAM but did not install missing PTEs. The correct 42--60 ms decision for
this profile is therefore profile-specific reclaim rejection. This validates
the host mechanism only; neither control is the formal FullCopy baseline.

A successor 2026-08-30 shared-host qualification expanded four captured
`openai-codex/gpt-5.6-terra` boundaries to a fixed 32-instance queue and covered
`N={1,2,4,8,16,32}` in 36 FullCopy/Crate reports (7,488/7,488 proxy turns).
Crate selected zero reclaim and missed at least p95 through N=16. At N=32,
FullCopy collapsed: Crate reached p95/p99 ratios 0.075/0.088, 0.432x
sandbox-cgroup DRAM, and 7.08x throughput. This is a 0.1x-wait CoW load
crossover, not fixed-SLO density. A matched N=8 attribution found that
fixed/elapsed reclaim cut local cgroup DRAM about 58.5% but failed tails with
about 7.9--8.0K major faults/run. Corrected sparse E3-v2 reset the full 256-MiB
holder before every trial; no nonzero point passed both resident guards, even
at 4.67 MiB p50 reclaimed. E3-v1 remains preserved and excluded for starting-
state carryover. The companion paper repository preserves these under
`docs/performance/orca/nsl17-figure-campaign-2026-08-30/`, alongside the earlier
speculative, direct, and Terra packages.

The 2026-07-18 EC2 mechanism campaign and raw evidence are in
`results/orca-sandboxfs-2026-07-18/REPORT.md`. It passed the synthetic cold-start
and mean-DRAM thresholds, but not the internal-workload/turn-latency claim.

The public-trajectory and measured-mini-agent converters plus deterministic
fixed-total tool/sandbox replay are in `experiments/trajectory_replay/`. Replay
measures real filesystem cache, private writes, subprocess, and reclaim
behavior; model calls occur only during separately labeled live trace capture,
not once per matched treatment.

The trace-derived EC2 report is in
`results/orca-trajectory-replay-2026-07-19/REPORT.md`. Its DRAM and cold-start
targets passed, but aggressive reclaim failed the tool-turn latency guardrail.
