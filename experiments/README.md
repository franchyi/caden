# Caden experiments

- `sandboxfs_memory/`: synthetic anonymous-working-set mechanism campaign,
  including selective SSD/zswap reclaim and wake stress.
- `agent_pipeline/`: no-tools model trace capture with bubblewrap or persistent
  remote SandboxFS command execution and exact provider/model receipts.
- `trajectory_replay/`: fixed-total wave replay of public- or measured-agent-shaped
  sandbox/tool workloads with static, fixed-grace, elapsed-only, and request-aware policies.
- `checkpoint_tiers/`: lower-level tier measurements.
- `speculative_restore/`: persistent-working-set microbenchmark for reactive
  refault versus frozen, non-dispatching speculative preparation.

A complete time-compressed fixed-queue sweep now covers N=1--32: it selects
zero reclaim, misses at least p95 through N=16, and crosses only when FullCopy
collapses at N=32. Matched lifecycle controls and a reset-corrected sparse
frontier find no nonzero memory point inside both guards. The next claim gate is
therefore an unscaled, counterbalanced dedicated-host crossing with a fixed
absolute p95/p99 SLO, followed by synchronized-commit robustness. These
harnesses exercise sandbox memory only; they contain no
cooperative model server, GPU telemetry, or KV-cache control.
