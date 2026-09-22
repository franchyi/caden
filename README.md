# Caden

Private implementation repository: **https://github.com/franchyi/caden**.
The default development branch is **`crate-mlsys`**.

Caden is Crate's stage-aware scheduler for concurrent agent sandboxes.
SandboxFS owns workspace construction and sandbox lifecycle; Caden owns
admission, a bounded one-shot ready pool, CPU classes, and memory residency.
The goal is greater sandbox density with tool-turn latency as a guardrail.

## Implementation scope

- OverlayFS workspaces over local immutable prepared bases, through the pinned
  SandboxFS public API. The only cold-start baseline is a recursive private copy
  of the same prepared base with reflink disabled.
- Request-aware admission, queue-aware pool replenishment, model-wait-aware
  freeze/reclaim, predictive preparation, and confirmed-response wake fencing.
- Shared-base cache reclamation only when all consumers of a base are idle.
- A common `MemoryTierBackend` interface with SSD and optional CXL backends.
  SSD delegates to kernel reclaim and existing swap. CXL uses a native mmap
  pager for explicitly registered cooperative memory; it does **not**
  transparently offload arbitrary processes or OverlayFS state.
- Real-command SWE-bench trajectory capture/replay, independent cold-start
  probes, session-serving orchestration, accounting and evidence checks.

CXLGen migration is a separate project and is not imported as a branch here.
Cold-start claims cover a cold sandbox on a warm host with a prepared local
base. Zero CXL payload in a serving run is not evidence of CXL offloading.

## Clone and run local tests

Python 3.13 is the project version. The regression suites need only the test
dependencies below; live capture and legacy plotting tools have additional
dependencies described in their own guides.

```bash
git clone --branch crate-mlsys https://github.com/franchyi/caden.git
cd caden
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
make test PYTHON=.venv/bin/python
```

`make test` runs both the scheduler/harness suite and the agent-pipeline suite.
It does not call a live model, start a remote experiment, or change host swap.
Linux native-pager integration tests are retained and report skipped when the
platform or required native binaries are unavailable. A macOS pass does not
claim Linux paging or real CXL hardware coverage.

The portable native store/protocol tests require a C compiler, make and zlib
headers. They use temporary/file-backed stores, not the real DAX device:

```bash
make native-check
```

On Linux this also builds the pager and cooperative holder. Running the Python
suite again then exercises applicable file-emulated pager tests; that remains
separate from real-DAX qualification.

## SandboxFS dependency

SandboxFS remains a Git submodule, pinned to
`652aa279bbb2afb4068d4b838e2df8e103b247fe` from
https://github.com/franchyi/sandboxfs. Clone access to that repository is
required independently of access to Caden.

```bash
git submodule update --init --recursive
git submodule status
```

The Python regression suites do not require an initialized submodule. Building
and deploying SandboxFS does; use its README and the experiment preflight.

## Experiments and evidence

- [SWE-bench capture and cold-start harness](experiments/swebench_verified/README.md)
- [SSD/CXL tiering and serving](experiments/cxl_tiering/README.md)
- [Trajectory replay](experiments/trajectory_replay/README.md)
- [Native CXL pager contract](native/cxl_coldstore/PAGER.md)
- [Experiment index](experiments/README.md)

The experiment runners still contain reference-lab host, user and path checks.
They are not a turnkey installation for an arbitrary server. Prepared roots,
original captured traces and raw performance packages are separate inputs,
maintained with the paper/evidence repository at
https://github.com/franchyi/crate-paper. Repository access does not imply that
all large artifacts have been distributed. Do not run a historical preparation
controller over preserved inputs or modify frozen evidence to match new names.

Real CXL writes require a currently approved device/offset/length reservation,
including coordination with any sharing host. Ordinary regression tests do not
grant permission for Device-DAX writes, host-wide swapon or VM setup.

## Origin of this repository

This is a clean independent snapshot of the local `crate-mlsys` working tree
from `franchyi/orca`, including the latest uncommitted Caden rename, CXL pager,
policy fixes, serving harnesses and tests. The upstream base was
`5d1acbd640cfea4221ed09cdab0cf1861b69e367`; that old commit alone does not
represent the imported implementation.

[Import provenance](migration/README.md) records the scope and per-file hashes.
Old Git history, unrelated branches, raw traces, historical result packages,
environments and compiled output were not mirrored. They remain untouched in
their original locations. Historical documentation and trace schemas can still
contain `orca` identifiers; current implementation/package naming is Caden.

Use this repository's `crate-mlsys` branch for new work. The original repository
was not reset, rewritten, re-pointed or committed by this export.
