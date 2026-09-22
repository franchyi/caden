# Caden Novelty Re-Check

Status: draft, started 2026-06-06. Tracks AC-12 / task t1.

Scope of this revision: this file currently covers only the **isolation-substrate
axis** — shared-kernel containers vs microVMs, and recent agent
checkpoint/restore work — because that axis decides Caden's sandbox backbone.
It records why Caden builds on shared-kernel containers (bubblewrap) rather than
Firecracker microVMs. The remaining AC-12 survey areas (eBPF / `sched_ext`
scheduling, container CPU/memory overcommit, serverless cold-start, and
agent-loop scheduling) are listed as TODO at the end and are **not** yet a
completed novelty survey.

## Isolation substrate: why Caden uses shared-kernel containers, not microVMs

The 2025–26 AI-agent sandbox landscape splits along one axis: shared-kernel
containers versus hardware-isolated microVMs. Firecracker is the dominant microVM
choice (Vercel Sandbox and much of the "run untrusted agent code" market sit on
it). It adds only ~5 MiB of VMM overhead per instance and boots in ~125 ms, but
each microVM carries its **own guest kernel** and a configurable minimum of guest
RAM (≥128 MB), and its strongest feature is turnkey snapshot/restore (pause a VM,
serialize full RAM + device state, resume in ~5–30 ms, or sub-400 ms cold-start
with userfaultfd page-faulting). Shared-kernel containers (bubblewrap, runc,
podman) instead share the host kernel and page cache, so the per-instance memory
floor is far lower and achievable density far higher — the trade-off every source
names is density (containers) versus a hard isolation boundary for untrusted code
(microVMs).

Caden sits squarely on the density side, and the choice is not a close call,
because **every Caden enforcement lever is a host-cgroup operation or a host
scheduling decision** and therefore requires the workload to be host-visible
processes inside a per-sandbox cgroup v2 subtree. A microVM boundary defeats each
lever in turn, as summarized below.

### Substrate vs Caden's enforcement levers

| Caden lever (from the design) | Requires | Shared-kernel container | Firecracker microVM |
| --- | --- | --- | --- |
| `sched_ext` stage/class/burst-age CPU policy | host scheduler sees the agent's tasks | works: tasks are host processes | fails: host sees only vCPU threads; guest scheduling is the guest kernel's job |
| `memory.reclaim` of cold `LLM_WAIT` pages | host cgroup owns the pages; shared page cache | works: reclaim to swap/zswap frees host RAM for admission | poor fit: guest RAM is anonymous to the host; needs ballooning/UFFD, no shared page cache |
| `cgroup.freeze` per waiting sandbox | cgroup contains the workload | works: per-sandbox freeze | degrades to whole-VM pause, not per-stage cgroup control |
| Transparent stage detection (runnable transitions, cgroup CPU) | host-visible runqueue / cgroup signals | works | fails: hidden behind the VM boundary |
| Headline metric: agents/host at SLO | low per-sandbox memory floor | favored: shared kernel + page cache | penalized: per-VM kernel raises the floor, lowers the ceiling |

Put behind a Firecracker boundary, Caden would be scheduling opaque boxes — which
is functionally the B1 static-provisioning baseline — and the per-VM kernel would
raise the very memory floor the agents/host metric is trying to lower. The
substrate and the contribution are structurally opposed.

### DeltaBox and the Firecracker-snapshot route

DeltaBox (arXiv:2605.22781, 2026) is the strongest recent agent
checkpoint/restore system and the clearest example of the microVM-snapshot route.
It runs sandboxes as **Firecracker microVMs on a custom Linux 6.8 kernel**, pairs
an overlay/reflink filesystem layer (DeltaFS, XFS reflink at 4 KB granularity)
with an incremental-CRIU plus frozen-process-template layer (DeltaCR), and reports
checkpoint at 14.57 ms, template-fork restore at 5.14 ms (CRIU lazy-pages slow path
8.04 ms), and fork throughput from 0.57 ms (N=1) to 5.47 ms (N=64) — cutting
state-management overhead from 47–77% of trajectory time on coupled-filesystem
baselines to 3–6%.

DeltaBox is related work to cite and differentiate, **not a baseline Caden must
beat**, for two reasons:

1. **Different objective.** DeltaBox optimizes millisecond
   checkpoint/rollback and fork-many-children to accelerate *tree search and RL
   training rollouts*. Caden optimizes *dense overcommit of concurrent live agents
   at an interactive turn-latency SLO* via stage-aware residency, admission
   headroom, and CPU policy. One makes branching cheap; the other makes a waiting
   tenant cheap while keeping it resumable.
2. **Different substrate assumptions.** DeltaBox's numbers depend on a custom
   kernel + CRIU + reflink stack. Caden's lower-bound path explicitly excludes CRIU
   and custom kernels (see the plan's Path Boundaries) and demonstrates residency
   and admission with stock cgroup v2 mechanisms on a stock Ubuntu 24.04 / Linux
   6.12+ host. DeltaBox therefore confirms that the high-performance
   microVM-snapshot route is a custom-kernel engineering story orthogonal to
   Caden's stock-kernel scheduling contribution; CRIU remains an optional Caden
   comparison (t19), not the backbone.

### Why bubblewrap specifically

Within the shared-kernel camp, bubblewrap, runc, and podman are interchangeable at
the substrate level; bubblewrap is the leanest. It is a ~5.4k-line, daemonless
launcher that creates the namespaced process tree (mount + optional user/pid/net/
ipc/uts/cgroup namespaces) in milliseconds and then `execve`s the workload, with no
image format, no copy-on-write storage layer, and only libc + libcap (and optional
libselinux) as dependencies. Caden drops that process tree into one cgroup v2 per
sandbox and applies freeze/reclaim as host-cgroup operations on the subtree, so
"swap state out" needs no support from the launcher itself — it is the controller's
`cgroup.freeze` + `memory.reclaim` (lower bound), or optionally CRIU (t19). The one
caveat is that bubblewrap does not manage cgroups itself, so per-sandbox placement
is done by the controller (e.g., a systemd scope/slice or a direct write to
`cgroup.procs` before exec); this is acceptable because Caden owns cgroup management
in any case. podman remains the convenient option for the optional CRIU
checkpoint/restore comparison, since it ships turnkey `checkpoint`/`restore`.

## Caden's claimed delta on this axis

Caden contributes stage-aware **scheduling and residency** for densely packed,
*live, concurrent* agent sandboxes on stock-kernel shared-kernel containers:
freeze + cgroup-local reclaim keyed on the off-host `LLM_WAIT` boundary,
wake-storm-safe admission headroom, and a `sched_ext` CPU policy over
`f(stage, class, burst_age)`, evaluated by agents/host at an interactive
turn-latency SLO. This is distinct from (a) snapshot/restore-latency systems
(DeltaBox, Firecracker snapshots) aimed at untrusted-code isolation or
search/RL fork throughput; (b) static container CPU/memory overcommit, which is
stage-unaware (Caden baseline B1); and (c) serverless cold-start pools, which
prewarm fresh instances rather than reclaiming and resuming the *same* waiting
tenant within a bounded wake latency.

## References

Accessed 2026-06-06; substrate figures are vendor/secondary-source numbers pending
local confirmation in E5 (overhead) runs.

- DeltaBox: Scaling Stateful AI Agents with Millisecond-Level Sandbox
  Checkpoint/Rollback — [arXiv:2605.22781](https://arxiv.org/html/2605.22781v1)
- Firecracker snapshot support (restore latency, UFFD) —
  [firecracker docs](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md)
- MicroVM vs container kernel boundary and density trade-off —
  [fly.io](https://fly.io/learn/microvm-vs-container/)
- Firecracker overhead/boot/density figures —
  [Northflank](https://northflank.com/blog/what-is-aws-firecracker)
- AI-agent sandboxing isolation strategies, 2026 —
  [manveerc guide](https://manveerc.substack.com/p/ai-agent-sandboxing-guide),
  [Luis Cardoso field guide](https://www.luiscardoso.dev/blog/sandboxes-for-ai)

## TODO — remaining AC-12 survey areas (not yet covered here)

- eBPF / `sched_ext` scheduling work (2025–26): scx schedulers, per-cgroup
  policy, relation to Caden's stage/class/burst-age function.
- Container CPU/memory overcommit and pressure-stall-driven reclaim.
- Serverless cold-start and snapshot pools (Lambda/SnapStart-style) vs Caden's
  resume-the-same-tenant residency model.
- Agent-loop / LLM-serving scheduling that exploits the inference-wait boundary.
- AgentCgroup positioning as Caden's isolation + tracing substrate.
