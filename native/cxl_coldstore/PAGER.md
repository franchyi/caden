# Cooperative CXL pager: contract, capabilities and limits

`crate_pagerd` (`pagerd.c`) is the real page-out/page-in path beneath
`caden.cxl_tier.CXLTierBackend`. It combines the mmap cold store with **actual
release and restoration of a sandbox process's memory**. It is opt-in, selected
explicitly (`--memory-tier-backend cxl`), and DRAM-SSD remains the default.

## Chosen route and why

Of the two routes in the handoff, this is the **cooperative userspace pager**. It
needs no `swapon`, no kernel block device, no VM, no ptrace injection and no
host-wide setting. The guest-VM route (unmodified tools on guest-only CXL swap)
was not taken because VM creation and guest swap setup were not authorized.

The consequence is stated once, plainly: **this pager is not transparent.**
Unmodified tools (the 32 SWE-bench traces run `bash`, `python`, `git`, `pytest`
that exit before every model wait; the only survivors are `sandboxd`, bwrap's
init, tmpfs and file cache) register nothing, so the CXL tier places **zero** of
their bytes. Transparent coverage of private anonymous memory and tmpfs needs a
kernel swap path onto CXL, which is exactly the host-wide or VM change that was
out of scope.

## Paging contract

| Aspect | Contract |
|---|---|
| Eligible mappings | `MAP_SHARED` mappings of a memfd named `crate-tier.u<uffd>.k<0\|1>`, created through `coop.h` by a process inside the sandbox cgroup. Discovered from `/proc/<pid>/maps`; the daemon re-verifies inode, offset, `rw-s` permissions and cgroup membership after pinning the process with a pidfd. |
| Ineligible | Private anonymous memory, stacks, tmpfs, SysV/POSIX shm, file cache, hugetlb, write-sealed memfds, regions without a userfaultfd (unless a test opts in). Reported as ineligible, never relabelled. |
| Ownership | One owning process per region. `fork()` children share the mapping (no copy-on-write); that is the owner's responsibility. The daemon is the store's single owner. |
| Quiescence | The execution layer freezes the cgroup and waits for `cgroup.events frozen 1`; the daemon re-checks it before copying. A thawed cgroup gets `EBUSY` and nothing moves. |
| Source-page release | Per ≤1 MiB chunk: `pread` memfd → `cs_write` store → arm userfaultfd (`MISSING`) → `fallocate(PUNCH_HOLE)`. A page counts as demoted only after the punch. `released_bytes` is the memfd block-count delta, not the store write. A failed chunk is trimmed from the store and stays resident. |
| Restore, eager | Before thaw, every cold run is read from the store and installed with `UFFDIO_COPY` (charged to the owner's cgroup), then trimmed. Used for speculative preparation too, which never reports dispatch readiness. |
| Restore, lazy | Readiness = fault service armed for every cold page. Allowed only when the owner's userfaultfd also resolves kernel-mode faults (`k1`), or when the owner explicitly promises user-mode-only access. Demand-fault cost stays inside the timed tool call. Refusal falls back to eager (recorded) or fails, per configuration. |
| Dispatch ordering | Confirmed restore runs before thaw. Any restore error raises; the execution layer then neither thaws nor dispatches. |
| Overlap | Per-sandbox locks in the execution layer, backend and daemon: generation admission, freeze, movement, restore/thaw and dispatch are serialized; a wake waits for in-flight demotion and charges its remaining cost to wake latency. |
| Fencing | Python admits the caller's lifecycle generation before freeze. Each backend RPC has a strictly increasing per-incarnation wire sequence (at least the caller's generation), including compatibility calls without a generation. The daemon rejects stale wire sequences (`ESTALE`). After a lost reply, restore reconciles with the daemon even if the cached cold-byte count is zero. |
| Bounds | Per-sandbox logical allocation (`EDQUOT`), store logical space (`ENOSPC`), fixed sandbox/region tables, 1 MiB staging buffer per connection (≤64), optional `mlock` of staging and store metadata. |
| Teardown | Successful restore (or arm) permits thaw before normal-API destroy. Failed restore never thaws; SandboxFS destroys the frozen consumers. Successful destroy plus an empty/removed cgroup is required before `DETACH`. Cleanup failures retain a non-dispatchable record for retry. Shutdown excludes in-flight commands and faults while checking cold pages, wakes idle sockets, and drains all connection workers before closing the store. `SHUTDOWN` and the first `SIGTERM` are refused while any page is cold. |
| Failure | Store corruption (CRC) poisons the sandbox: eager restore fails closed; a demand fault is answered with `UFFDIO_POISON` (SIGBUS) instead of wrong bytes. If the owner dies, consumers of cold pages block on the fault (fail-stop). Without a userfaultfd they would read zeros, which is why such regions are ineligible by default. |

## No silent SSD substitution

The CXL backend supports `Tier.CXL` only and rejects every other tier. It sets
`memory.swap.max=0` on the sandbox's own cgroup for the sandbox's lifetime, so
the optional companion file-cache reclaim cannot push anonymous or tmpfs pages
to the SSD swap under a CXL label. That reclaim drops file cache; it stores
nothing. The legacy `file_reclaimed_bytes` counter is a residual of total
cgroup charge reduction minus measured cooperative source release, not an
independent measurement of dropped file pages. Any non-zero swap
delta is recorded as `swap_leak_bytes`.

Because the backends use different mechanisms and cover different page
populations, **a DRAM-SSD versus DRAM-CXL difference is not an SSD-versus-CXL
hardware difference.**

## Unresolved races and unsupported cases

- A process that maps a region and is *not* in the frozen cgroup can write
  between copy-out and punch; the contract forbids it, nothing enforces it.
- Store operations are serialized by one mutex; concurrent sandboxes queue.
- A forced daemon stop (second `SIGTERM`, `SIGKILL`, crash) strands cold pages.
- `UFFDIO_POISON` needs Linux ≥ 6.6; older kernels leave the faulting thread blocked.
- Huge pages, partially unmapped regions and resized memfds are unsupported.

## Evidence classes (keep them apart)

1. **Mock**: `tests/test_memory_tier_contract.py` (both backends, fake pager).
2. **Real paging, file-emulated store**: `tests/test_cxl_tier_real.py`.
3. **Real DAX store / actual sandbox paging**: `experiments/cxl_tiering/` runs on nsl17.

September 20 safety regressions are in `tests/test_tiering_safety.py` and
`tests/test_cxl_tier_real.py`; the latter uses a test-only `LD_PRELOAD`
interposer to pause before hole punch and race shutdown deterministically.
`experiments/cxl_tiering/check_safety_api.py` checks lost replies and corrupt
restore teardown through an isolated real SandboxFS API, using a file store.
These are correctness checks, not new application performance measurements.
