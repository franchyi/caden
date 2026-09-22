# Caden experiments

The active Crate harnesses are:

- `swebench_verified/`: real-command trace capture, conversion, paired cold
  startup, first-touch probes and fidelity checks.
- `cxl_tiering/`: isolated SSD/CXL controllers, session-serving comparisons,
  DAX guards, the registered-memory gate and independent CXL cold startup.
- `trajectory_replay/`: command/model-wait replay, both fixed-total waves and
  explicitly scheduled repeated sessions. Check the manifest for which one ran.
- `agent_pipeline/`: trace capture and its separate regression suite.
- `sandboxfs_memory/`: synthetic anonymous-memory mechanism experiments.
- `speculative_restore/`: reactive versus non-dispatching predictive preparation.

Older capture and lower-level probes are retained as source utilities. Their
generated traces, archives and performance outputs were not imported into this
repository. Historical result references in older documents identify external
evidence, not bundled input files.

## Running tests versus measurements

`make test` at the repository root runs non-campaign regression tests. Performance
controllers require prepared bases, captured inputs, an isolated output directory
and the documented host permissions. Current controllers enforce lab-specific
host/user/path constraints; port them explicitly for a new server, record the
new source revision and validate it before measuring.

Use the same commands, model waits, arrival schedule and completed-work checks
across systems. Report memory accounting boundaries and latency endpoints.
Keep cold startup separate from serving arrival-to-ready. A configured CXL
backend with zero stored payload is not a CXL-offloading result. A single
ordered campaign is descriptive, not a maximum-density or non-inferiority proof.

Never purge host caches, change swap, overwrite preserved inputs or touch
unreserved DAX ranges as an implicit setup step.
