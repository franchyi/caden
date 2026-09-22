# checkpoint_tiers — measuring Caden's swap-out/restore tiers on bubblewrap

Measures the three tiers Caden would use to park an idle agent sandbox and bring it back, on a
**modern kernel** (cgroup v2 unified, `cgroup.freeze`, `memory.reclaim`, criu). The faithful model:
a privileged "Caden daemon" drives kernel levers against an **unprivileged bwrap sandbox** that
holds memory like an idle agent.

| Tier | Mechanism | What it reclaims |
|------|-----------|------------------|
| 1 freeze | `cgroup.freeze` | CPU only; memory stays resident |
| 2 reclaim | freeze + `memory.reclaim` → zram | RAM (anon → zram, clean file dropped); lazy refault on wake |
| 3 cold | criu dump/restore | everything (RAM + PIDs); for eviction/migration |

## Why a fresh node, not nsl7s

nsl7s runs Linux 5.4: no `memory.reclaim` (needs 5.19), no `CAP_CHECKPOINT_RESTORE` (needs 5.9),
and likely cgroup v1. We measure on AWS EC2 Ubuntu 26.04 (kernel 7.0), which has every lever Caden
actually targets.

## Run it on a fresh Ubuntu 24.04+/26.04 node

```bash
rsync -az ./ <node>:~/ckpt-tiers/
ssh <node> 'bash ~/ckpt-tiers/setup_node.sh'                 # bubblewrap+criu, zram swap, verify levers
ssh <node> 'MIB=2048 bash ~/ckpt-tiers/run_tiers.sh'         # tier 1+2: freeze/thaw + reclaim
ssh <node> 'python3 ~/ckpt-tiers/bench_restart.py'           # destroy/reconstruct (restart) latency
ssh <node> 'sudo env MIB=512 STORE=/dev/shm python3 ~/ckpt-tiers/bench_checkpoint.py'  # CRIU dump/restore
```

`probe.sh` is a read-only environment probe (handles both cgroup v1 and v2) if you want to inspect a
node before committing.

## Files

| File | Role |
|------|------|
| `setup_node.sh` | one-time: apt install bubblewrap+criu, configure zram swap, confirm cgroup v2 levers |
| `workload.py` | synthetic idle agent: faults in `MIB` of anon memory, idles, times a refault scan on SIGUSR1 |
| `run_tiers.sh` | launches the sandbox in a cgroup (unprivileged via setpriv), runs the driver, criu smoke test |
| `measure_tiers.py` | privileged driver: times freeze/thaw, `memory.reclaim`, and wake refault; emits JSON |
| `bench_restart.py` | restart latency: bwrap construct/run/teardown round-trip vs bare exec |
| `bench_checkpoint.py` | CRIU dump/restore latency + image size vs RSS and image store |
| `probe.sh` | read-only environment detection |
| `results/` | committed measurements (`tiers.json`, `restart.json`, `checkpoint.json`, criu logs + gotcha table) |

## Measured (m7i.xlarge, kernel 7.0)

| Tier | Park cost | Wake cost | Notes |
|------|-----------|-----------|-------|
| freeze | **8.6 µs** | 8.5 µs thaw | memory stays resident |
| reclaim (2 GiB) | 2.15 GB → zram in 5.9 s (off critical path) | **1.24 s** worst-case full refault | lazy + prefetchable; `tiers.json` |
| restart (destroy/reconstruct) | **11 ms** teardown (512 & 2 GiB alike) | **2.5 ms** relaunch (empty) | size-independent — memory discarded; `restart.json`, `restart-mem.json` |
| CRIU (512 MiB / 2 GiB) | **180 / 700 ms** dump | **200 / 800 ms** restore | image ≈ RSS; needs in-ns invocation; `checkpoint.json`, `criu-bwrap-gotchas.md` |

Takeaway: park cost spans **8.6 µs → ~700 ms** across tiers; Caden's stage tracking picks the cheapest
that fits the predicted idle. CRIU's image ≈ RSS, so it frees RAM+slot for eviction/migration but is
strictly costlier than reclaim for plain park-and-resume.
