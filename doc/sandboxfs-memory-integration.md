# Caden + SandboxFS: Elastic Cold-Start and Memory-Efficiency Design

Status: fixed-grace evidence preserved; predictive sandbox path implemented and awaiting formal campaign
Date: 2026-08-30

Packaging update on 2026-10-08: SandboxFS is now vendored as ordinary source
files at `third_party/sandboxfs`, pinned by `third_party/sandboxfs.provenance.json`.
The historical measurements below are unchanged by this packaging revision.

## 1. Decision

Integrate SandboxFS as Caden's default Bubblewrap workspace/lifecycle backend,
but keep their responsibilities separate:

- **SandboxFS** creates an isolated writable workspace quickly from one shared
  immutable lower tree. It removes recursive full-copy startup and allows
  unchanged lower files to share the host page cache.
- **Caden** observes agent stages, bounds and refills the ready pool, admits work against a
  DRAM reserve, and reclaims memory from sandboxes blocked in `LLM_WAIT`.

The combined system exceeded both mechanism targets in three synthetic EC2
pairs: 51.20%–56.83% lower time-weighted mean attributable DRAM and
89.70%–91.02% lower request-to-ready p50/p95. This does not yet satisfy the
internal Multi-Agent business claim because the workload was synthetic and
p95 wake latency regressed from about 0.56 s to 6.22 s. The memory result came
from the stage-aware policy and a reclaimable working set, not from adding a
dependency alone.

## 2. Why the combination is useful

There are three independent memory effects:

1. **Shared lower-file page cache.** T1 sandboxes read unchanged files through
   the same lower inode. Full-copy sandboxes have distinct inodes and can cache
   duplicate copies of identical data.
2. **Bounded ready capacity.** Caden can keep a small ready reserve rather than
   statically prewarming one sandbox for every possible concurrent request.
3. **Stage-aware DRAM reclaim.** During `LLM_WAIT`, Caden can lower CPU weight,
   freeze the cgroup, and invoke `memory.reclaim`. Clean file pages can be
   dropped and anonymous pages can move to an explicitly identified unshared
   local-SSD swapfile. On wake, Caden thaws the cgroup and lets the working set
   refault. The predictive path can prepare explicit file/anonymous profiles
   without command dispatch before the committed wake.

SandboxFS alone does not share anonymous process memory. Caden alone does not
remove workspace-copy latency. A large unbounded warm pool may improve latency
while increasing memory, so “pooling” is not credited as a memory optimization
unless the ready population is lower than the static comparison.

## 3. Feasibility bound

Let:

- `f_wait` be the fraction of time agents spend in `LLM_WAIT`;
- `r_wait` be the fraction of a waiting sandbox's DRAM that Caden reclaims; and
- `f_shared` be any additional host-memory fraction saved by shared lower-file
  page cache.

Ignoring interaction terms, the expected steady-state reduction is:

```text
memory reduction ≈ f_wait × r_wait + f_shared
```

The current Caden trace corpus reports 66.3% classified LLM wait. Reaching 50%
from stage reclaim alone would therefore require reclaiming at least 75.4% of
waiting memory. The existing synthetic 2 GiB reclaim experiment moved about
99.8% out of DRAM, so the target is feasible for a reclaimable working set. It
is not guaranteed for real agents with pinned, actively shared, or immediately
refaulted pages.

## 4. Architecture

```text
client
  │ submit / poll / cancel
  ▼
Caden policy
  ├── admission + hazard-weighted DRAM reserve
  ├── bounded ready capacity
  ├── stage → CPU class
  ├── fixed / elapsed / request-aware reclaim profitability
  └── bounded pre-restore + confirmed-wake priority + SLO breaker
          │
          ▼
SandboxFS execution adapter
  ├── sandboxfsctl create/exec/destroy
  ├── resolve cgroup from sandbox PID
  ├── selective memory.reclaim + optional fail-closed zswap
  ├── frozen host-file/process_madvise preparation (no dispatch)
  └── memory.current / memory.swap.current / memory.zswap.current / cpu.stat
          │
          ▼
sandboxfsd → Bubblewrap + persistent sandboxd
          │
          ▼
XFS T1: shared lower + private OverlayFS upper/work
```

SandboxFS is pinned as `third_party/sandboxfs`. Caden imports no Go implementation
internals; it uses the installed host API. The vendored source supplies the exact
daemon, CLI, scripts and tests. Historical benchmark evidence remains external.

## 5. Lifecycle policy

### 5.1 Admission

Caden admits a request only when:

```text
MemAvailable >= estimated_new_WSS + fixed_reserve + predicted_wake_reserve
```

Static mode uses configured WSS, reserve, and ready-target values. Elapsed and
request-aware modes now use past-only conditional return hazards to weight wake
reserve by each waiting sandbox's restore footprint, while retaining a fixed
safety reserve and sparse-history per-waiter floor. WSS itself remains
configured rather than learned.

Accepted requests enter a FIFO queue and are provisioned by a bounded
asynchronous admission executor. A burst therefore creates up to the configured
limit concurrently without blocking `submit()`, while in-flight WSS estimates
remain reserved against admission headroom.

### 5.2 Stage transitions

| Stage | CPU action | Memory action |
|---|---|---|
| `LLM_WAIT` | set idle weight immediately | profitability-gated selective reclaim; optionally prepare predicted return while still frozen |
| `RESPONSE_WAKE` | acquire a bounded wake slot; confirmed restore, then boost | generation-fence background work; thaw only after commitment |
| `TOOL_BURST` | retain the bounded wake slot | ensure restored |
| `RESULT_PACK` | normal weight | keep resident briefly |

Reclaim runs outside the scheduler lock and is concurrency-limited. A
generation number prevents an expired timer from reclaiming a sandbox that has
already woken. A shared priority movement gate orders confirmed restore before
speculative preparation and reclaim; it can reserve movement capacity for
confirmed demand. The wake slot remains held through the tool burst and is
released at `RESULT_PACK`. A rolling absolute-SLO breaker disables new reclaim
and prepares already-cold waiters when wake or turn p95 crosses its registered
threshold. Full details and claim limits are in
`doc/speculative-sandbox-residency.md`.

### 5.3 Ready pool

Prepared sandboxes are one-shot: once assigned, they are never returned to
another tenant. Destruction is the only reset operation in the MVP. The pool is
bounded by both a count and a memory budget. Its default minimum should be zero
or one because SandboxFS already creates T1 workspaces in roughly 10–30 ms on
the measured host.

The required `F0-S0` project baseline recursively copies the prepared tree on
request and then keeps each admitted sandbox resident; it has no ready pool.
The Caden treatment adds a small bounded ready reserve. `T1-S0` and `T1-S1`
separate filesystem and pooling effects, so the complete result is not
misattributed to the scheduler.

## 6. Metrics and claim boundary

### 6.1 Primary memory metric

“Memory footprint” is defined before the experiment as the time-weighted mean
host DRAM used above an idle-host reference during the fixed multi-agent
scenario:

```text
attributable_DRAM(t) = host_used_DRAM(t) - idle_host_used_DRAM
```

The report must also include p50, p95, and peak attributable DRAM so a lower
mean cannot hide a wake-storm peak.

### 6.2 Supporting memory metrics

- sum of sandbox `memory.current`;
- sum of `memory.swap.current` and `memory.zswap.current`, reported separately;
- per-process PSS from `smaps_rollup` when permission allows;
- file-cache and anonymous-memory fields from `memory.stat`;
- reclaimed bytes per event;
- major faults and restore latency;
- the declared post-wake page-touch stride or measured tool working set;
- ready, running, waiting, and reclaimed sandbox counts; and
- agents completed per host at the latency SLO.

Host DRAM is primary because shared page-cache pages are not meaningfully
represented by summing per-process RSS, and cgroup page-cache charging may be
assigned to only one of several readers.

### 6.3 Cold-start metric

Use the existing SandboxFS boundary: receipt of Caden `submit()` through
successful completion of `/bin/true` via the normal `sandboxd` API. Report
separately:

- queue delay;
- pool acquire or SandboxFS create time;
- workspace preparation;
- process/socket readiness; and
- total request-to-ready latency.

Work overlapped with LLM inference is reported as scheduling overlap, not as
filesystem acceleration.

### 6.4 Acceptance rule

The combined internal multi-agent claim passes only if, for the same task set,
arrival trace, completion count, host, and pre-registered latency SLO:

1. Caden + SandboxFS T1 reduces time-weighted mean attributable DRAM by more
   than 50% versus the static always-hot full-copy policy;
2. p95 and peak memory are reported and no OOM occurs;
3. request-to-ready p50 and p95 improve by more than 50%;
4. interactive turn p95/p99 remain within the registered SLO; and
5. each configuration has at least three independent runs.

If only the synthetic memory-holder workload passes, the result must be called
a mechanism result, not an internal Multi-Agent business result.

## 7. Comparison matrix

The filesystem baseline remains the one required full copy. Scheduler variants
are policy comparisons, not additional filesystem baselines.

| Label | Filesystem | Pool/residency policy | Question |
|---|---|---|---|
| `F0-S0` | full copy, reflink disabled | on-demand; keep admitted sandboxes resident | required combined baseline |
| `T1-S0` | OverlayFS + XFS reflink | on-demand; keep admitted sandboxes resident | page-cache + cold-start contribution |
| `T1-S1` | OverlayFS + XFS reflink | bounded elastic pool, no reclaim | pool contribution |
| `T1-S2` | OverlayFS + XFS reflink | bounded pool + stage reclaim | complete treatment |

An optional `T0-S2` ablation can distinguish extent-sharing first-write effects,
but it is not necessary for the primary memory-policy claim.

## 8. Experiment phases

### Phase A: deterministic mechanism test

- Launch N SandboxFS sandboxes with a known anonymous working set.
- Alternate `LLM_WAIT` and `TOOL_BURST` from a deterministic trace.
- Compare static residency with Caden reclaim.
- Validate reclaimed bytes, swap, major faults, wake latency, and no data loss.

### Phase B: shared page-cache test

- Drop caches before each configuration.
- Have N sandboxes read the same prepared-base files.
- Compare full-copy private inodes with T1's shared lower inodes.
- Measure host cache growth and subsequent read latency.

### Phase C: internal Multi-Agent workload

- Use the same tasks and arrival trace for all four matrix configurations.
- Record explicit cooperative stage transitions.
- Run at least three repetitions in counterbalanced order.
- Report memory distributions, cold start, turn latency, throughput, faults,
  and failures.

## 9. Risks

- Reclaim without swap mostly drops clean file pages and may not reduce
  anonymous DRAM enough. The host manifest must record swap/zram/zswap state.
  zram/zswap remains physical DRAM and is reported only as a separate
  compression treatment, not as the primary SSD-tier memory result.
- Aggressive reclaim can convert memory savings into major-fault tail latency.
- A synchronized response burst can restore many sandboxes at once. Admission
  must reserve wake headroom and limit concurrent restores.
- The current SandboxFS process is lightweight during LLM wait. Real memory
  savings depend on which persistent agent/runtime processes live inside its
  cgroup.
- Shared lower pages stop being shared through the lower inode after a file is
  copied up and modified.
- `memory.current` is not PSS. Multiple memory views are required.
- SandboxFS destruction removes the private upper, so a “cold release” cannot
  preserve workspace state yet. The first integration uses freeze/reclaim,
  not destroy/recreate, for stateful waiting sandboxes.

## 10. Implementation deliverables

1. SandboxFS source vendored at the evaluated upstream commit.
2. `SandboxFSExecution` adapter implementing create, exec, destroy, CPU class,
   freeze/reclaim, restore, sandbox stats, and host stats.
3. `StageAwareCaden` with queueing, admission, fixed/elapsed/request-aware
   residency, selective reclaim, hazard-weighted reserves, prioritized movement,
   generation-fenced non-dispatching preparation, an SLO breaker, and decision
   telemetry.
4. Unit tests with a deterministic fake execution backend.
5. A Linux multi-agent memory harness that emits machine-readable JSON.
6. An EC2 report containing the exact host manifest, commits, raw samples,
   summaries, failures, and claim verdict.

## 11. Current claim

The deterministic mechanism target passed in all three paired repetitions.
The accurate wording is:

> Caden + SandboxFS demonstrated 51.20%–56.83% lower time-weighted mean
> attributable DRAM and 89.70%–91.02% lower request-to-ready p50/p95 on a
> synthetic 32-agent reclaim workload.

Do not append “during internal Multi-Agent business test” yet. Phase C remains
required, and its interactive latency SLO must account for the measured restore
cost. See `results/orca-sandboxfs-2026-07-18/REPORT.md` for the complete result,
limitations, exact host, commits, and raw-evidence manifest.
