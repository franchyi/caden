# Caden repository instructions

- Current project: Caden scheduler for Crate, branch `crate-mlsys`, package
  `caden`. Preserve historical `orca` trace schema compatibility.
- SandboxFS owns workspace construction and lifecycle; Caden owns admission,
  pools, CPU classes and residency. Use its pinned submodule/public API.
- Recursive copying of the same prepared local base with reflink disabled is
  the only cold-start baseline. OverlayFS variants are treatments.
- SSD and registered-memory CXL tiering are in scope. Cross-host CXLGen
  migration and arbitrary-process migration are separate work.
- Raw evidence is external and immutable. Do not rewrite traces, frozen source,
  or checksums to match naming. Record source/workload identity for new runs.
- Run `make test PYTHON=.venv/bin/python` for local Python regression tests.
  Native/file-backed tests and real DAX tests are distinct evidence classes.
- Do not run remote campaigns, modify host swap, format devices, purge global
  caches or write Device-DAX without task-specific authority and an exact
  reservation. Existing lab scripts are not authorization to execute them.
- Do not commit credentials, virtual environments, generated binaries or bulk
  experiment output. No upstream history or unrelated branch was mirrored here.
