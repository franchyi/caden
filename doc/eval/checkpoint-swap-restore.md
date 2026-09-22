# Checkpoint, swap, and restore on bubblewrap

*2026-06-08. Companion experiment: `caden/experiments/checkpoint_tiers/` (measured on AWS EC2
m7i.xlarge, Ubuntu 26.04, kernel 7.0, criu 4.2).*

Caden's headline verb is "park an idle agent and admit an active one." That needs a cheap, *partial*
way to swap a sandbox's state out and bring it back. This note works out how to do that on
bubblewrap, and reports measurements of the tiers that matter.

## Reframe: bubblewrap does not checkpoint — it constructs a checkpoint-friendly container

Bubblewrap has no snapshot/restore/freeze of its own. It sets up namespaces (user/mount/pid/ipc/uts),
does the double `pivot_root`, drops privileges, and `exec`s the workload; after that it is a tiny
monitor. The actual swap is done by **the host kernel (cgroup v2) and CRIU**. Bubblewrap's only job
is to make the sandbox *shaped* like a container the kernel can manipulate: its own pid/mount/user
namespaces, with host-visible processes in a cgroup. That shape — which Firecracker gives up — is the
whole enabler.

## The key move: decompose sandbox state, reclaim only what the stage requires

A microVM snapshot is monolithic: guest RAM + vCPU + device state, all-or-nothing, opaque to the
host. Shared-kernel lets us split sandbox state into four independent pieces and reclaim only what a
given idle period actually needs:

| Piece | Where it lives | Reclaim mechanism | Cost (measured / known) |
|---|---|---|---|
| **Filesystem delta** | host bind / overlay upperdir | already on disk — nothing to do | $0 |
| **CPU** | scheduler | `cgroup.freeze` | **~8.6 µs** |
| **RAM** | anon pages | `memory.reclaim` → zram | swap-out ~363 MB/s (off critical path); wake refault ~1.7 GB/s, lazy |
| **Process tree** | kernel task structs | CRIU dump → image | dump ~180 ms (512 MiB) / ~700 ms (2 GiB), restore ~200 / ~800 ms; **image ≈ RSS**. Needs in-namespace invocation — see below |

Caden's stage tracking (LLM_WAIT → RESPONSE_WAKE → TOOL_BURST → RESULT_PACK) is what selects the tier:
it knows whether the sandbox is empty or holds a warm process tree, and roughly how long the idle
will last. That is exactly the knowledge a VM snapshotter lacks from outside the guest.

## The tiered ladder

1. **Short idle (typical LLM_WAIT, seconds): `cgroup.freeze`.** CPU → 0, memory resident, thaw in µs.
2. **Medium idle: freeze + `memory.reclaim` to zram.** Idle agents' anon pages compress, clean file
   pages drop; RAM is reclaimed for overcommit and refaults lazily on wake.
3. **Cold idle / eviction / migration: CRIU.** Dump the process tree to disk, free everything (RAM +
   PIDs), enabling overcommit beyond RAM+swap and cross-host migration.

Stage-awareness also *hides* the restore latency: kick off the thaw/refault (or CRIU restore) at
RESPONSE_WAKE, when the model's first token is dispatched. The wake cost then overlaps model TTFT
(hundreds of ms to seconds) instead of the user's turn latency. You spend the model's wait, not the
agent's.

## Measured tiers (2 GiB idle agent, kernel 7.0)

Raw data in `experiments/checkpoint_tiers/results/tiers.json`. The driver is a privileged "Caden
daemon" operating the cgroup levers against an *unprivileged* bwrap sandbox holding 2 GiB.

- **Tier 1 — freeze/thaw: median 8.6 µs / 8.5 µs** (p90 0.25 ms / 0.024 ms) over 20 iterations.
  Freezing an idle agent to give its CPU to a neighbor is effectively free.
- **Tier 2 — reclaim:** after freezing, `memory.reclaim` evicted **2.151 GB of 2.156 GB to zram**,
  leaving 5 MB resident, in **5.9 s** (~363 MB/s lz4 swap-out). This is the *swap-out* cost, paid
  while the agent is frozen and idle — it overlaps LLM_WAIT, which is seconds of model thinking
  anyway, so it is off the interactive critical path.
- **Wake / refault:** scanning the full 2 GiB resident took 35 ms; the first scan *after* reclaim
  faulted everything back from zram in **1.28 s** (~1.7 GB/s), a **1.24 s** refault penalty. This is
  the worst case (every page touched at once); real wakes touch a fraction, refault lazily, and can
  be prefetched under TTFT.

The Tier-1 vs Tier-2 gap is the density/latency dial the scheduler turns per predicted idle: keep
hot for µs-resumable but RAM-resident, or reclaim for overcommit at a bounded, hide-able wake cost.

zram matters here: anon pages compress into RAM, so reclaim/refault are CPU-bound (GB/s), not
disk-bound. And because many agents share a read-only base image, their base file pages are one copy
in the page cache — a density win microVMs (separate guest page caches) cannot get.

## Implementation: the kernel surface

Each tier is a real low-level kernel feature, but the prototype reaches it through the feature's
*interface* — cgroup-v2 pseudo-files, signals, and a few setuid-capable CLIs — not through raw
syscalls. It writes no C, makes no direct syscalls, and ships no kernel code; the harness is Python
`open().write()` on `/sys/fs/cgroup/...`, `os.kill`, and `subprocess` to `bwrap`/`criu`/`unshare`.
This is the standard, correct way to drive cgroup v2 and CRIU — the same surface systemd, runc, and
podman use.

| Code path | Interface | Kernel mechanism | Since |
|---|---|---|---|
| `write cgroup.freeze` | cgroup-v2 pseudo-file | freezer (`kernel/cgroup/freezer.c`) | 5.2 |
| `write memory.reclaim` | cgroup-v2 pseudo-file | `try_to_free_mem_cgroup_pages()` (`mm/memcontrol.c`) | 5.19 |
| `mkswap` / `swapon /dev/zram0` | block dev + `swapon(2)` | zram compressed swap (`drivers/block/zram`) | — |
| `write cgroup.procs` | cgroup-v2 pseudo-file | task migration into a cgroup | v2 |
| `bwrap --unshare-*` | CLI | `unshare(2)`/`clone(2)` `CLONE_NEWUSER\|PID\|NS\|IPC\|UTS`, `pivot_root(2)`, `mount(2)` `MS_BIND`, `prctl(PR_SET_NO_NEW_PRIVS)` | — |
| `setpriv` | CLI | `setuid`/`setgid`/`setgroups(2)` | — |
| `unshare --propagation private` | CLI | `unshare(CLONE_NEWNS)` + `mount` `MS_PRIVATE\|MS_REC` | — |
| `criu dump` / `criu restore` | CLI | `ptrace(2)` `PTRACE_SEIZE`, `process_vm_readv(2)`, `/proc/<pid>/{maps,pagemap,fd}`, `clone3` `set_tid` | — |
| `os.kill`, `pkill` | `kill(2)` | signal delivery | — |

The freeze tier is the clearest example of why interface-level is enough: writing `1` to
`cgroup.freeze` quiesces every task at the next kernel↔user boundary with no signal and no ptrace,
which is why it costs 8.6 µs. A production Caden controller would push lower in three places: a
`sched_ext` BPF scheduler for the stage/criticality CPU-boost policy (the only genuinely
kernel-level component); CRIU lazy-restore via `userfaultfd(2)` so a sandbox resumes before its pages
are back; and long-lived cgroup file descriptors with `inotify` on `cgroup.events` instead of
re-`open()`/poll.

## v1 special case: swap is already free

In the shipped pipeline (`experiments/agent_pipeline/`) the agent loop is **host-side** and each
command runs in a fresh ephemeral bwrap. Between commands nothing is resident: the repo is on a host
bind mount and the conversation is host memory. So "swap out" is just letting bwrap exit (RAM freed
instantly, zero resident cost) and "restore" is re-running the same argv against the same on-disk
repo. Measured (`results/restart.json`, `restart-mem.json`): relaunching a fresh sandbox is **2.48 ms**
median (bwrap's namespace+bind setup is ~2.2 ms over a bare exec), and tearing down a sandbox that
*held* memory is **~11 ms whether it held 512 MiB or 2 GiB** — teardown is size-independent because
exit frees pages in bulk, and relaunch starts empty, so neither end moves the old memory. **Because
bwrap construction is so cheap, the stateless case needs no snapshot at all; destroy/reconstruct
(~13 ms, flat in footprint) is faster and simpler than any restore.** The contrast with CRIU is the
whole point: restart is ~13 ms flat *because it discards memory*, while CRIU preserves it and so
scales with RSS (380 ms → 1.5 s round-trip from 512 MiB → 2 GiB). Firecracker
cannot do this: tearing down a microVM loses the guest, and the only way back is a boot or a
snapshot-restore — the very machinery one was trying to avoid.

## Filesystem: the overlay upperdir *is* the FS checkpoint

The backbone of cheap swap is decoupling filesystem state from process state. Mount the base
repo/image as a shared read-only lowerdir (one copy, page-cache shared across all agents on that
base) plus a per-agent writable upperdir holding the delta. The upperdir is durable on disk by
construction, so "swap out the FS" is a no-op and "restore" is "mount the same lower+upper." Branch
or snapshot = copy the upperdir, or `cp --reflink` / btrfs-XFS CoW for instant clones. That is
DeltaBox-style checkpoint/rollback at the overlay layer — shared-kernel, no microVM. (bwrap 0.11 on
the test node has `--overlay`; the v1 `BASE_ARGS` are pinned 0.4.0-safe and lack it, so the
agent-in-sandbox build needs the newer flag or an externally-mounted overlay.)

## The CRIU cold tier: measured gotchas and the integration path

CRIU is the only tier that touches the process tree, and it is the hard one. Trying to dump the bwrap
sandbox surfaced **one blocker per isolation feature**, in sequence (full table in
`results/criu-bwrap-gotchas.md`): nested-pidns dump host-side → child-userns ownership → host
mount-table copy (`/boot/efi` vfat) → writable bind's `MS_SHARED` master → `--dev` device-node binds.
Each is solvable, but together they are precisely the work crun/runc's CRIU integration already does.

The conclusion: **host-side criu cannot dump a faithful bwrap sandbox; criu must be invoked from
*inside* the sandbox's namespaces** (the runc/podman model), with the host binds declared `--external`
and the bind given private mount propagation. So Caden's cold tier likely wants the persistent sandbox
wrapped by a thin CRIU-integrated OCI runtime (crun/runc) — or a small in-namespace criu helper —
rather than raw bwrap. This complexity lives only in the *rare* cold tier; the common cheap tiers
(freeze, reclaim) work directly from the host with the trivial effort measured above.

**Measured cost** (`results/checkpoint.json`, CRIU's intrinsic dump/restore of a memory holder, since
the mount handling above is orthogonal to latency): dump+restore scales ~linearly with resident
memory — **512 MiB: ~180 ms dump / ~200 ms restore; 2 GiB: ~700 ms / ~800 ms** (≈350 ms/GB dump,
≈400 ms/GB restore), and tmpfs vs disk image store barely differed. The **image is ≈ RSS** (540 MB for
a 512 MiB holder, 2.15 GB for 2 GiB — under 1% overhead). That last point matters: CRIU does *not*
save memory relative to the reclaim tier — it writes the entire footprint out to free RAM **and** the
process slot, at a cost of ~RSS in I/O and ~350–400 ms/GB. So CRIU earns its keep only for genuine
eviction or cross-host migration; for parking-and-resuming, freeze+reclaim (compressed in zram, lazy
refault) is strictly cheaper. The cold-tier latency can be hidden the same way as reclaim — CRIU
lazy-restore (`--lazy-pages`) resumes the process before its pages are back, faulting them in over a
page server, so the ~800 ms overlaps model TTFT at RESPONSE_WAKE.

A favorable structural point: Caden only ever checkpoints at the **LLM_WAIT** boundary, where the
model connection is held host-side and the sandbox has no live socket. CRIU's most fragile area
(in-flight TCP) is therefore an architectural non-issue, not a matter of luck. GPU/device state is
likewise moot — the model is remote and agents hold no accelerator in-sandbox.

## Why this is the Caden argument, not a workaround

Park-idle-to-admit-active needs the swap to be cheap and partial. bwrap + the host kernel give a
**decomposed** checkpoint where you almost never pay for a full snapshot: most parks are freeze
(8.6 µs) or freeze+reclaim (lazy, hide-able wake), the FS delta is always already durable, and CRIU
is the rare cold-eviction tier. Firecracker forces a monolithic snapshot for every park, cannot share
page cache across agents, and cannot let the host selectively freeze or reclaim one agent. The
decomposition *is* the density story.

## Honest caveats and next steps

- The 5.9 s reclaim and 1.24 s refault are a worst case: a fully-touched 2 GiB working set evicted in
  one shot. Real idle agents have smaller, partially-clean working sets and refault lazily. Measuring
  realistic working sets and partial reclaim (`memory.high` pressure vs explicit `memory.reclaim`) is
  the next experiment.
- CRIU's intrinsic dump **and restore** are now measured and verified (process killed by dump, revived
  by restore). What remains is the *faithful* path — dumping the full bwrap sandbox via in-namespace
  invocation with `--external` binds (or crun/runc), per the gotcha table — plus a lazy-restore
  (`--lazy-pages`) measurement to confirm the wake hides under TTFT.
- Multi-agent density at an interactive turn-latency SLO — the actual headline metric — needs many
  sandboxes co-resident with reclaim under load, not a single 2 GiB holder. That is the experiment
  this harness is a building block for.
