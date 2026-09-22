# SWE-bench Verified real-command harness

This is the scheduling/evaluation harness on `crate-mlsys`. It captures actual
agent tool commands and model waits, then replays those commands through the
normal SandboxFS API under matched configurations. Replay does not call the
model again and is not a SWE-bench solve-rate evaluation.

## Components

| Stage | Entry points | Responsibility |
|---|---|---|
| Selection and preparation | `sample_tasks.py`, `prepare_remote.py`, `verify_isolation.py` | Outcome-blind repository quotas, pinned task identities, prepared environments, isolation receipts |
| Original capture | `capture.py`, `capture_batch.py`, `finish_capture32.py` | Retain commands, model waits, expected exits, source fingerprints and failed-attempt records |
| Replay inputs and source | `convert.py`, `snapshot.py`, `routing.py` | Preserve captured commands/waits, freeze exact source, route each prepared base and sandbox to its daemon |
| Development and formal execution | `development_controller.py`, `formal_controller.py`, `run_suite.py`, `run_cold.py` | Gate deployment and execution on explicit review and fixed evidence; measure cold creation separately from recurring tool turns |
| Verification and reporting | `checkpoints.py`, `analyze.py`, `audit_development.py`, `report_artifact.py` | Check equal work, fingerprints, source identity, timing endpoints and registered guards without suppressing unfavorable results |

The shared trajectory driver in `../trajectory_replay/run_campaign.py` provides
queue-aware ready-pool and confirmed thaw-only options. The Caden implementation
remains in `src/caden/`; these scripts are experiment orchestration, not a second
scheduler implementation.

## Campaign and safety boundaries

The existing host paths, service names, source manifests and review receipts
belong to the September 19 nsl17 campaign. This code checkpoint does not
authorize restarting it. Future hardware runs need reviewed experiment paths,
host availability, source snapshots and a fixed measurement plan; do not reuse
completed output directories or modify original receipts to pass a gate.

`formal_controller.py` requires an explicit reviewed selection before deployment.
The single-comparison protocol must be selected and reviewed explicitly; it is
not three independent repetitions. `launch_controller.py` and
`continue_campaign.py` retain the earlier pilot-expansion workflow for provenance,
not the recommended entry point for a new formal run. Preparation and service
launch scripts can change host state and must not be run as local smoke tests.

Reader-facing comparisons use **Baseline** (the eager-copy baseline) and
**Crate**. Original configuration IDs remain in JSON and command-line interfaces
for compatibility. The baseline copies the same local prepared workspace with
reflink disabled, admits on demand, and uses no ready pool or proactive reclaim.
Cold-sandbox measurements assume a warm host with a prepared local base.

The mmap CXL backend is separate from this DRAM-SSD path. Snapshot tooling can
include that optional directory when present, but the scheduler and replay
implementation do not import it or depend on CXLGen migration.

## Local verification and original evidence

From the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m pytest -q -p no:cacheprovider tests
```

The suite uses temporary fixtures and mocked host operations. Native cold-tier
tests and real hardware measurements are outside this command.

The original captures, normalized inputs, raw measurements, measured-source
snapshots and checksum manifest are preserved in the sibling paper repository:
`crate-paper/docs/performance/swebench-verified-2026-09-19/`. Use that package's
README and `TRACE_PROVENANCE.md` to reproduce the published comparison; this
development branch can advance beyond its frozen measurement source.
