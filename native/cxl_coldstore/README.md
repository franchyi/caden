# Experimental mmap cold-store backend

This is a **single-host memory-tiering building block**, not CXLGen cross-host
migration. It has no dependency on the CXLGen ownership, Generation filesystem,
handoff, or reusable-shell stack. It can be developed on `crate-mlsys` without
merging the separate migration branch. Preserving a copy on that branch does
not change this component's scope or make it transparent sandbox paging.

This directory implements a bounded native store for offloading **explicitly
supplied bytes** into an mmap-backed region and reading them back. The default
`codec=none` stores raw pages; `codec=lz4` opts into compression. It is not yet
connected to Caden residency policy or transparent paging of existing sandbox
processes. File-emulation and socketpair tests do not establish CXL performance,
SWE-bench memory savings, or sandbox wake latency. A bounded real-DAX backend
qualification of the earlier LZ4-only version passed on nsl17 on 2026-09-19: nine synthetic owned-buffer probes,
not transparent sandbox paging. Exact source snapshots, raw logs and the success
receipt are in `crate-paper/docs/performance/swebench-verified-2026-09-19/`
`coldstore-backend/reserved-dax-v3/` in the sibling paper repository.

**Current authorization: implement/test the backend; no global `swapon`.** Nothing
here attaches a kernel NBD device, changes swap priorities, onlines CXL memory,
or changes another process's mappings. The real-DAX probe is a separate, explicit
write operation, not part of `make check`.

## Sandbox paging integration

The store is now connected to a real, opt-in page-out/page-in path:
`crate_pagerd` (`pagerd.c`) plus the cooperative client in `coop.[ch]`, driven by
`caden.cxl_tier.CXLTierBackend`. **Read [`PAGER.md`](PAGER.md) for the paging
contract and its limits before citing any result**: the pager covers only
registered cooperative mappings and is not transparent paging of unmodified
tools. `make check` builds the pager on Linux only; its tests are in the
repository's `tests/` directory and skip explicitly elsewhere.

## Components and lifetime

- `coldstore.[ch]`: 4-KiB logical pages and CRC32-checked reads. `CS_CODEC_NONE`
  (the zero-initialized default) stores every written page verbatim, including
  zeros, with no compressor/decompressor calls or codec-state mapping.
  `CS_CODEC_LZ4` enables LZ4 compression, zero-page elision and incompressible-page
  raw fallback. Untouched/trimmed logical pages read as zero in either mode.
  The selected codec is immutable for a store's lifetime. Payloads occupy
  256/512/1024/2048/4096-byte allocation classes in the mapped region. Hierarchical
  availability bitmaps avoid a linear search through full or fragmented storage.
  Reads/writes may be partial-page; core trim requires whole pages.
- `nbd_transport.[ch]`: sequential NBD transmission over an already connected
  Unix stream socket. It is a library adapter, not a listening server, negotiation
  implementation, or kernel-device setup utility.
- `coldstore_probe.c`: a cooperative, self-contained owned-buffer experiment.
  It stores synthetic bytes, unmaps its own source buffer, checks on Linux that
  the mapping is absent, reads into a newly allocated buffer, and checks CRC32.
  The independent mapping-absence field is `null` on non-Linux hosts.

The store is **ephemeral and single-owner**: its page index, checksums and
allocator metadata reside in host DRAM. Closing or crashing the owner loses the
index; reopening does not recover prior contents. Do not restart/close it while
any consumer depends on its pages. CRC32 detects accidental corruption, not
malicious modification, and is not encryption. Payload bytes can remain in the
backing region after trim/close; secure erasure is not implemented.

Operations are mutex-serialized. A page replacement commits only after its new
payload is complete; a multi-page write is **not** a transaction and can leave
earlier pages committed after an error. Logical capacity never relies on a
promised compression ratio: `logical_bytes <= capacity - 4096`, retaining a spare
physical frame for replacement even when every page is incompressible.

## DAX safety boundary

Non-emulated operation accepts only the exact path `/dev/dax0.0`, verifies its
character-device identity against sysfs, and requires the complete mapping to
fit inside the device and this half-open interval:

```text
[256 GiB, 512 GiB) = [274877906944, 549755813888) bytes
```

Both DAX offset and capacity must be multiples of 2 MiB. Logical capacity must
be a positive multiple of 4096 bytes, with the spare-frame constraint above.
The core opens without following a final symlink, never truncates the backing
device, and maps only the requested interval. Opening initializes host metadata,
not the entire DAX range; actual writes can overwrite bytes inside that range.

These checks restrict placement; **they are not proof of exclusive ownership**.
Linux open-file-description range locks exclude cooperating owners on the same
host only. They cannot fence nsl18 or another host sharing the physical CXL
memory, nor a writer that ignores the lock. Confirm the exact subrange is
reserved on all sharing hosts before a DAX write. Historical data inside the
chosen subrange must be expendable; the lower half remains out of scope.

File emulation explicitly requires a regular file and an offset aligned to the
host mmap page size. It must never be described as a CXL hardware test.

## Build and safe local tests

Dependencies: a C11 compiler, `make`, zlib development headers/library, pthreads
and Python 3 for fixture/preflight tests. Pinned LZ4 v1.10.0 source is bundled
under `third_party/lz4/` with its license and SHA256 provenance; no pkg-config,
system LZ4 installation, or shared-host package change is needed. From this directory:

```sh
make check
```

`make check` runs core and NBD tests in both codecs, omitted/explicit probe-mode
tests, and mocked preflight/controller tests. The core tests use
temporary regular files, covering compression/zero/raw round-trips, randomized
partial I/O, bounded full-store replacement, corruption,
concurrency, ownership checks and out-of-range guards. The transport test uses
`socketpair` plus regular-file emulation, including fragmented I/O,
cookie echo, rejected flags/ranges, truncated writes without mutation, trim,
disconnect, and bounded stalled-request/reply handling. It opens no `/dev/nbd*`
or `/dev/dax*` device. Individual binaries are `build/test_coldstore` and
`build/test_nbd_transport`; the cooperative probe fixture is `test_probe.py`.

An optional **Linux file-emulation probe** uses only a newly created fixture:

```sh
probe_file=$(mktemp /tmp/crate-coldstore-probe.XXXXXX)
truncate -s 8388608 "$probe_file"
./build/coldstore_probe --emulate-file "$probe_file" 0 8388608 4194304 mixed
# Optional compression (a separate store lifetime):
./build/coldstore_probe --emulate-file "$probe_file" 0 8388608 4194304 mixed --codec lz4
```

The fixture is left at `$probe_file` for inspection; remove that exact file when
finished. The probe requires successful metadata `mlock`; inspect the Linux
process's `ulimit -l` (KiB), which must cover the metadata mappings. Insufficient
locked-memory allowance causes failure rather than silently using pageable
metadata. Larger runs require an explicitly approved process/service limit,
not an unreviewed host-wide setting change.

The exact probe interface is:

```text
coldstore_probe --emulate-file|--write-reserved-dax PATH OFFSET CAPACITY LOGICAL_BYTES zeros|mixed|random [--codec none|lz4]
```

Omitting `--codec` selects `none`; invalid codecs are rejected before opening
the store. All sizes/offsets are decimal bytes. The probe additionally caps mapped capacity
at 256 MiB and source size at 128 MiB; these are qualification limits, not evidence
of whole-tier scalability. `zeros`, `mixed` (1024 pseudorandom bytes and 3072
repeated bytes per page), and `random` are synthetic patterns, **not captured
SWE-bench sandbox state**. Do not substitute a device path into the file-emulation
example. Real-DAX execution requires a separately checked reservation and run
configuration.

## Qualification runner (offline by default)

`qualify.py` creates a **fresh** output directory, snapshots and hashes the native
sources and vendored LZ4/license, builds in that isolated snapshot, and runs
`make check` before the three-pattern probe. It retains raw command logs, source
hashes, per-probe JSON, guard hashes and `REPORT.json`, including failures. It
never deletes an existing output or cleans other experiment directories.

```sh
python3 test_qualify.py
python3 qualify.py --output /tmp/crate-coldstore-file-qualification-01 --repetitions 1
# Use a fresh output to qualify the optional codec:
python3 qualify.py --output /tmp/crate-coldstore-file-qualification-lz4-01 --repetitions 1 --codec lz4
```

The output's parent must exist and the output itself must not. Omit
`--reserved-dax` for regular-file emulation; this is the default. Repetitions can
be 1 or 3, with all three patterns in every repetition. The runner always uses
64 MiB mapped capacity and a 32 MiB synthetic source. It does not raise the
process's locked-memory limit; ensure the approved limit is sufficient first.
`qualify.py` and `qualify_remote.py` both accept `--codec none|lz4` (default
`none`), propagate it explicitly and validate the mode-specific footprint and
call counts. Probe, qualification and controller receipts use schema v2; older
v1 artifacts remain historical evidence, not evidence for the selectable codec.

Hardware mode additionally requires **all** of `--reserved-dax`,
`--cross-host-reservation-confirmed`, and a nonempty `--reservation-note`. The
operator must first verify cross-host exclusion and reserve a quiet interval;
the acknowledgement is recorded, not treated as a check performed by the runner.
Hardware execution is restricted to root on Linux `nsl17`/`nsl-node17`, the verified
512-GiB `/dev/dax0.0`, and this fixed probe placement:

```text
Probe offset:     274880004096 bytes (256 GiB + 2 MiB)
Probe capacity:       67108864 bytes (64 MiB)
Logical source:       33554432 bytes (32 MiB)
Read-only guards: 2 MiB immediately before and after the probe interval
```

Both guards lie inside the user-reserved upper half. Their SHA256 hashes must
remain unchanged; the runner never writes them. The target probe interval **is
overwritten**. The recorded v3 run used exactly this interval; its guards and
swap/device configuration remained unchanged. It does not establish exclusive
ownership for a future run, which must repeat the reservation/holder checks.

On Linux, the runner refuses active/transitioning `crate-sv-*` units except
recognized per-task `crate-sv-NN.service` daemons whose normal sandbox API lists
no sandboxes. It checks before building and around each probe, and fails closed
on unknown units or failed inspection. DAX mode also requires unambiguous
absence of local holders from `fuser`. These local snapshots cannot prevent
another operator from starting a job between checks, establish cross-host
ownership, or replace the externally reserved quiet interval. Do not run it
during comparative SWE-bench measurements. Non-Linux file emulation explicitly
records unavailable Linux host checks instead of claiming them.

Before/after `/proc/swaps` and read-only `swapon --show` configuration must match
by device/type/size/priority; normal usage churn is ignored. CRC32, source release,
and Linux mapping absence are required for successful probe validation. Final
guard/swap checks are attempted even after failure. **No kernel NBD attachment,
swap activation/deactivation, device conversion, or host-wide tuning is performed.**

## Accounting and interpretation

Probe JSON reports the backend label, exact range, source size, release check,
expected/restored CRC32, store/restore elapsed time, and these distinct costs:

| Field | Meaning |
| --- | --- |
| `codec` | Selected `none` or `lz4` mode. |
| `payload_bytes` | Stored compressed or raw bytes; only LZ4 mode elides written zero pages. |
| `allocator_bytes` | Occupied size-class bytes, including internal rounding. |
| `metadata_mapping_bytes` | Host-DRAM mappings for owner state, page index, frame ownership, availability hierarchy and optional codec state, including host-page rounding. |
| `codec_state_bytes` | Compressor scratch/state mapping, included in metadata; exactly zero in `none`. |
| `compression_calls`, `decompression_calls` | Actual LZ4 call counts; both exactly zero in `none`. |
| `raw_pages`, `zero_pages` | Current raw and elided-zero page counts; written zeros count as raw in `none`. |
| `hot_rss_anon_bytes`, `cold_rss_anon_bytes`, `restored_rss_anon_bytes` | Linux `/proc/self/status` anonymous RSS snapshots; `-1` means unavailable. |

Metadata scales with both logical page count and mapped physical frame count,
even when few pages contain data. NBD adds its separate bounded I/O buffer;
libraries, stacks, page tables, other process memory and file-emulation cache
costs are not included in `metadata_mapping_bytes`. Report them separately when
measuring total host DRAM. RSS snapshots are not time-weighted workload footprint,
and a CRC32 round-trip is not a sandbox execution-correctness evaluation.

Compression can reduce occupied backing bytes; it does not itself release the
source mapping. In this probe, explicit `munmap` performs that release. The
store/restore timings are backend-copy timings, not page-fault service, Caden wake,
normal sandbox API latency, or SWE-bench results. The core `encode_ns` and
`decode_ns` include the entire per-page operation, not just codec CPU time;
nonzero values in raw mode do not imply compression. Sequential synthetic
qualification runs are correctness evidence, not a counterbalanced performance
comparison. Compression is an optional CXL-capacity optimization, not a
prerequisite for releasing the owned source DRAM mapping.

## NBD adapter contract and future integration

Call `cs_nbd_serve(store, connected_unix_fd, &options, &stats)` with a connected
`AF_UNIX`/`SOCK_STREAM` descriptor. The caller owns and closes the descriptor;
the serving loop shuts it down on exit. Return 0 means a valid disconnect,
otherwise -1 with `errno`. This does not perform the NBD handshake.

Advertise at most `HAS_FLAGS | SEND_TRIM` (33), a 512-byte minimum block size and
the configured maximum request size (default 1 MiB; hard cap 16 MiB). READ/WRITE
require sector alignment. TRIM discards only complete 4-KiB pages within its
range. Unsupported commands, including FLUSH, and all command flags, including
FUA, receive wire `EINVAL`: this backend cannot promise nonvolatile persistence.
Do not advertise FLUSH, FUA, multi-connection, extended headers or structured
replies. See the [NBD protocol](https://github.com/NetworkBlockDevice/nbd/blob/master/doc/proto.md)
and [Linux UAPI layout](https://github.com/torvalds/linux/blob/master/include/uapi/linux/nbd.h).

The adapter allocates/faults in one dedicated, page-rounded mmap buffer before
its serving loop; `io_buffer_bytes` reports that full mapping, and
`lock_io_buffer` optionally requires successful `mlock`. Cleanup unmaps only its
own buffer. Requests have a total deadline starting with their first received
header bytes (default 30 seconds, maximum 10 minutes), covering the remaining
header, payload, processing and reply. The separate `idle_timeout_ms` defaults
to zero: indefinite idle waiting in blocking poll, with EOF or caller shutdown
able to cancel it. A long LLM wait is not a partial-request timeout.
Oversized requests disconnect without draining unbounded payload;
invalid bounded writes are drained before an error reply to preserve framing.

Before using this as a sandbox cold tier, integration still needs:

- A real page-out/page-in path, safe process quiescence, ownership/fault handling,
  failure recovery and restoration correctness. mmap plus this byte API does
  not transparently evict or restore another process's anonymous memory.
- Verified cross-host arena reservation and locked, bounded control/data buffers
  to avoid reclaim recursion or deadlock; lifecycle isolation from unrelated
  processes and a safe shutdown contract.
- Separate authorization for any kernel-device or swap integration. **Global
  `swapon` is not authorized**, and no kernel attachment is implemented here.
- Independent validation on actual CXL hardware, then matched real-workload
  experiments reporting host DRAM, CXL allocation, compression/metadata overhead,
  throughput, failures, and tool/wake tail-latency guardrails. Backend probes
  must remain separate from the existing DRAM + SSD SWE-bench campaign.
