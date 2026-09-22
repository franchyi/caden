# Caden Scheduling System — Design Discussion Draft

Status: discussion draft for the meeting. Three layers — **thin client → Caden scheduler
→ execution layer** — where the execution layer is two independent paths (**CPU**,
**memory**) over a pluggable **`Sandbox`**. Extends `paper/sections/design.tex`.
**Bold open questions** are for the meeting; everything else is a proposal.

## 0. Premise (already evidenced)

- LLM-wait-dominated: ≥98% `LLM_WAIT` across our 10 agent-pipeline traces (mean 99.1%,
  upper bound), 66.3% on the larger 157-trace corpus → idle-but-resumable for almost all
  wall time, so its CPU/RAM can be reclaimed.
- Mechanism costs (`m7i.xlarge`): park/resume <10 µs; reclaim 2 GiB → 5.9 s out / 1.24 s
  back; restart 11 ms; CRIU 0.4–1.5 s → reclaim, not CRIU. (CPU realloc is instant;
  memory move is the slow, expensive lever.)

Lever: **where cold `LLM_WAIT` memory goes and how fast it returns**.

## 1. Architecture

```
   client(s) ─ submit / poll / cancel (I1) ─▶  Caden scheduler
        queue · admission · CPU policy · memory policy
           │ set_cpu(class)         │ demote / restore        ▲ report(stage)
           ▼  (I2-cpu)              ▼  (I2-mem)                │
    ┌────────────────────┐   ┌────────────────────┐
    │ CPU path            │   │ memory path         │
    │ reallocate CPU      │   │ demote a sandbox's  │
    │ across sandboxes    │   │ memory → CXL / SSD, │
    │ (by stage / class)  │   │ restore → DRAM      │
    └─────────┬──────────┘   └──────────┬──────────┘
              └─────────── act on ───────┘
               ┌───────────────────────────┐
               │  Sandbox (abstraction)     │  isolated agent unit;
               │  start() · kill()          │  runs the loop + tools,
               │  pluggable backend         │  emits stage
               └───────────────────────────┘
    backends (swappable): bubblewrap+cgroup now; container / microVM / gVisor later
    mechanisms:  CPU path ← eBPF (sched_ext);  memory path ← reclaim / DAMOS / migrate per tier
```

- **Client (thin)** — submits agent task requests, polls results. No stages, no sandboxes.
- **Caden scheduler** — request queue + admission, plus per-stage CPU and memory policy.
  Pure policy; acts only through I2.
- **Execution layer** — a pluggable **`Sandbox`** (runs the agent loop + tools, emits its
  stage) driven by two independent paths: a **CPU path** that reallocates CPU across
  sandboxes by stage, and a **memory path** that demotes a sandbox's memory to a slower
  tier and restores it. The paths share only the sandbox handle.

The LLM call lives in the runtime (never parked); only the *sandbox* is reallocated/demoted
— so the runtime always observes the stage and the wake.

**APIs (verbs):**

| seam | direction | calls |
|---|---|---|
| **I1** | client → Caden | **`submit(task) → req`** · **`poll(req)`** · **`cancel(req)`** |
| lifecycle | Caden → exec | **`run(task) → sandbox`** · **`revoke(sandbox)`** |
| **I2-cpu** | Caden → CPU path | **`set_cpu(sandbox, class)`** · ↑ **`report(sandbox, stage, context?)`** |
| **I2-mem** | Caden → memory path | compatibility: **`demote`/`restore`**; selective: **`demote_selective(request)`** / **`restore_selective(request)`** |
| observe | Caden ← exec | **`stat(sandbox)`** · **`host_stat()`** |

`task = {cmd, repo, klass}` · `tier ∈ {SSD, COMPRESSED, CXL, …}`. The pinned SandboxFS backend currently implements SSD and fail-closed zswap compression; CXL remains a future backend.

## 2. Caden scheduler (policy)

- **Queue / admission**: run requests in queue order while
  `host_stat().dram_free ≥ wss + reserve`, with `reserve = Σ_{LLM_WAIT} P(wake)·wss`.
- **Residency** = composing the two paths on each `report`, exploiting the cost gap (CPU
  realloc instant, movement slow):
  - `LLM_WAIT` → `set_cpu(idle)` now; fixed, elapsed-only, or request-aware
    profitability decides selective reclaim after a grace.
  - a past-only return hazard can prepare explicit pages while the cgroup stays frozen.
  - committed wake → generation-fence background work, restore/thaw, then boost.
- **Wake-storm safety**: use a hazard-weighted DRAM reserve, bounded wake slots, a shared
  priority movement gate with an optional confirmed-demand reserve, and an absolute-p95
  SLO breaker. See `speculative-sandbox-residency.md`.

## 3. Execution layer — Sandbox + two paths

**`Sandbox` (abstraction)** — an isolated agent unit (`start` / `kill`) that runs the loop,
executes tool calls, and emits its stage. Backend swappable (bubblewrap+cgroup now;
container / microVM / gVisor later); reuses the agent-pipeline loop. Two independent paths
act on it:

| | **CPU path** | **memory path** |
|---|---|---|
| does | reallocate CPU across sandboxes | move a sandbox's memory across tiers |
| Caden knob | **`set_cpu(sandbox, class)`** | selective demote / preparation / confirmed restore |
| on `LLM_WAIT` | → IDLE (yield CPU) | hot-floor reclaim → SSD/zswap; optional frozen pre-restore |
| on wake | → BOOST after restore | confirmed restore/thaw → DRAM |
| backend | eBPF / `sched_ext` | cgroup-v2 reclaim, zswap, explicit file prefetch/process_madvise |

Mechanisms are backend detail, not interface — a future non-cgroup `Sandbox` reuses the
same two paths.

## 4. Memory: two ways to reclaim a waiting sandbox

A waiting sandbox can be reclaimed at two granularities:

**(a) Demote / restore — keep state.** Move its cold pages to a slower tier, then bring
them back on wake (sub-second, resumable). The tier sets the wake cost:

| tier | refault | trade-off |
|---|---|---|
| SSD / swap | 10s–100s µs/page (I/O) | ubiquitous, but an I/O tail on wake → wake-storm risk |
| compressed zswap | host-DRAM decompression | lower bytes/latency, but compressed payload still consumes host DRAM |
| CXL (future adapter) | ~2–3× DRAM, byte-addressable | needs an explicit safe backend and matched hardware campaign |

SSD is the implemented primary tier. Compression is a separately reported treatment.
CXL is optional future robustness work and must not be claimed until a real `Tier.CXL`
backend and an explicitly reserved device arena exist.

**(b) Release / cold-start — drop state.** `revoke` the whole sandbox — free *all* its
memory (~11 ms, flat in footprint) — and `run` a fresh one when the agent is next scheduled.
Cheapest reclaim and frees the most DRAM, but the return is a **cold start**: rebuild the
working set (clone, venv, warm caches). Best for stateless sandboxes, very long waits, or
hard memory pressure.

Caden picks per agent: **demote/restore** when the sandbox is stateful or the wait is short;
**release/cold-start** when it's stateless, the wait is long, or under pressure (open Q6).

## 5. Implementation plan (two-person)

Lanes split by **memory/density (A)** vs **CPU + runtime (B)**; the only cross-person link
is A consuming B's `report` stream + sandbox handles — mockable from the trace corpus.

| layer | component | owner |
|---|---|---|
| client | `submit`/`poll`/`cancel` (I1) | **B** (small) |
| Caden | queue + admission + residency + wake predictor | **A** |
| Caden | CPU class policy (`f(stage, class, age)`) | **B** |
| exec | **`Sandbox`** abstraction + backend + runtime (loop, stage `report`) | **B** |
| exec | **CPU path** (eBPF reallocation) | **B** |
| exec | **memory path** (`demote`/`restore`, tiers) | **A** |
| shared | I1 / I2-cpu / I2-mem contracts; telemetry (`stat`/`host_stat`) | both |

**Build order**: (1) pin the contracts + the `Sandbox` abstraction (together); (2) parallel
— A: `demote`/`restore` to an emulated CXL tier; B: runtime that `run`s an agent + `report`s
stage + the CPU path; (3) parallel — A: admission/residency/predictor; B: CPU class policy +
client/queue. **First result**: park + demote-to-CXL on `LLM_WAIT` → DRAM freed + restore
latency (the premise gate, no experiment harness yet).

## 6. Open questions for the meeting

1. **Admission**: one-at-a-time vs fixed batches; FIFO vs priority by `klass`.
2. **Demote tier + mechanism**: CXL vs SSD fallback; DAMOS vs `reclaim`+demotion vs
   `migrate`; emulated vs real CXL for the prototype.
3. **Hot-set on `restore`**: everything, a prewarm set, or access-tracked hot pages?
4. **Grace threshold**: minimum `LLM_WAIT` worth a `demote` given the `restore` cost.
5. **Admission reserve sizing**: under- (wake-storm tail) vs over-provisioning (lost density).
6. **Pressure policy**: `revoke` (kill + restart, stateless) vs hold/queue; fairness across
   `klass`.
7. **SLO**: the interactive turn-latency target and the `restore` budget within it.

Baselines (from the paper): B0 default Linux · B1 static cgroups · B2 CPU-only Caden ·
B3 stage-aware Caden (CPU realloc + demote/restore + admission).
