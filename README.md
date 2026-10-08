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

## Clone build and test

The source delivery contains Caden, SandboxFS, native tier backends, harnesses
and tests in one repository. No Git submodule operation or second private
repository permission is needed. Runtime component boundaries stay intact:
Caden uses the SandboxFS public API, not its implementation internals.

Use Python 3.13+, Go 1.24+, a C compiler, make and zlib headers. Live sandbox
execution requires Linux, cgroup v2, Bubblewrap and an existing XFS volume
(`ftype=1`, `reflink=1` for T1). Build/test commands do not install a service,
format storage, change swap or access real Device-DAX.

```bash
git clone --branch crate-mlsys https://github.com/franchyi/caden.git
cd caden
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pip install .
make build PYTHON=.venv/bin/python
make test PYTHON=.venv/bin/python
make sandboxfs-test PYTHON=.venv/bin/python
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

## Included SandboxFS source

SandboxFS is vendored as ordinary tracked files in `third_party/sandboxfs`, from
`652aa279bbb2afb4068d4b838e2df8e103b247fe` from
https://github.com/franchyi/sandboxfs. All 43 selected source/build/support
files are byte-identical to that commit. Historical `results/` and Git metadata
are excluded. The Go module path remains unchanged; this local Go module is
not an external Git dependency and uses only the Go standard library.

```bash
make verify-vendor PYTHON=.venv/bin/python
make sandboxfs-build PYTHON=.venv/bin/python
third_party/sandboxfs/bin/sandboxfsctl --help
```

`third_party/sandboxfs.provenance.json` records upstream identity, the included
paths and every source hash. No license file exists at that upstream commit;
this import does not invent a license. Confirm licensing before public
redistribution. The existing LZ4 license is retained under the native backend.

Build outputs are `third_party/sandboxfs/bin/{sandboxd,sandboxfsd,sandboxfsctl,
sandboxfsbench,sandboxfscorpus}` and `native/cxl_coldstore/build/`. The Linux
build additionally produces `crate_pagerd` and `coop_holder`. The Python wheel
installs the scheduler library only; the full product source delivery is the
repository or source archive, which also includes Go/C and experiment code.

## Deliver without a Git checkout

From a clean committed checkout:

```bash
make source-dist PYTHON=.venv/bin/python
tar -tzf dist/caden-source.tar.gz
```

The archive includes the complete tracked source, tests and vendored SandboxFS,
plus `SOURCE_PROVENANCE.json`. It excludes `.git`, ignored inputs, credentials,
build output and virtual environments. Extract it in a fresh directory, then
run the build/test commands above. Export refuses to overwrite an existing
archive. Python package/test dependencies and host tools still need to be
installed; this is not an air-gapped binary appliance or a bundled SWE dataset.

For a fresh experiment snapshot accepted by the measurement runners:

```bash
# RUN is an existing, empty, operator-selected campaign directory.
.venv/bin/python scripts/export_source.py --directory "$RUN/source"
make -C "$RUN/source" build
```

Pass `--sandboxfs-bin "$RUN/source/third_party/sandboxfs/bin"` to
`run_tiering_suite.py` or `run_cxl_cold.py`. This selects the built host daemon,
CLI and in-sandbox daemon together and records their hashes. The launcher
read-only bind-mounts the selected sandboxd into its private mount namespace;
it does not rewrite the prepared rootfs. Omitting the option preserves the
historical input binaries for archival experiments. Do not claim that a new
build reproduces the exact source identity of the September measurements.

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

The self-contained setup and measurement guide is in
`doc/caden-sandboxfs-report.docx`. Its build commands use this repository;
measurement commands state the separate prepared-input and reservation
requirements. The historical source snapshots remain the authority for the
reported old measurements. This packaging change does not rerun those results.

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
