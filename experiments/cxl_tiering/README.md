# Isolated memory-tiering runs (nsl17)

`run_tiering_suite.py` runs the 32 recorded SWE-bench real-command traces at 1x
waits for the eager-copy **Baseline**, **Crate DRAM-SSD** and **Crate DRAM-CXL**
inside one private directory `/data/chaoyi/crate-tiering/<run-id>/`. The
September 19 campaign tree is read-only input. `sandbox_paging_check.py` checks
both backends through the normal sandbox API first.

Both scripts need root on nsl17 and change no host-wide setting. They start only
transient units named `crate-tier-<tag>-*` and stop only those.

The two Crate configurations share policy flags; only the injected
`MemoryTierBackend` differs. They do **not** cover the same pages (see
`native/cxl_coldstore/PAGER.md`), so their difference is not a hardware
comparison, and one ordered run per configuration is descriptive only.
