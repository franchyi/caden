# Speculative-restore latency microbenchmark

`run_latency.py` measures one narrow mechanism question: after reclaiming a
persistent anonymous working set to configured Linux swap, how long does a
confirmed tool wake take with reactive refault versus host-side
`process_madvise(MADV_WILLNEED)` while the sandbox remains frozen?

The runner counterbalances both treatments on one sandbox and reports:

- demotion, speculative preparation, confirmed restore, and full working-set
  scan p50/p95;
- direct fixed-boundary-to-command-completion latency, including lifecycle
  reporting, dispatch setup, and any preparation that crosses the boundary;
- response-boundary overrun, reclaimed/swap/advised bytes, and major faults;
- optional full-WSS setup reset time, reported outside the response boundary;
- a hot-resident scan floor and checksum consistency.

Example (Linux root, isolated SandboxFS daemon):

```bash
sudo taskset -c 0-7 env PYTHONPATH=src \
  python3 experiments/speculative_restore/run_latency.py \
  --base bench-medium \
  --socket /run/sandboxfsd-crate-restore.sock \
  --wss-mib 256 --scan-transport fifo \
  --hot-reserve-mib 16 --reclaim-mode balanced \
  --madvise-mib 320 --madvise-passes 2 --madvise-advice willneed \
  --wait-ms 1000 --spec-lead-ms 500 \
  --prefetch-root /immutable/rootfs \
  --prefetch-file /immutable/rootfs/usr/bin/dash \
  --reset-between-trials \
  --skip-confirmed-prewarm --trials-per-treatment 12 \
  --output /new/result/path/restore-latency.json
```

`--scan-transport fifo` uses a persistent holder and shell builtins so the
measured command does not add `seq`/`awk`/`sleep`/`cat` startup and polling
faults. The legacy `poll` transport remains available for preserved-report
comparability. For a sparse measured scan, `--reset-between-trials` is
mandatory: before each timed trial the holder touches every 4-KiB page, records
`reset_ns`, and restores a treatment-independent resident starting state. The
reset is outside the fixed response clock and cannot be credited as latency
hiding. Schema v6 reports per-trial and p50/p95 reset costs. Repeated bounded
`willneed` passes can reduce residual refaults,
but duplicate
accepted bytes are charged in the receipt. `--madvise-advice populate-read`
requests synchronous population and fails closed when the kernel does not
support it; `willneed` remains the portable default. Explicit immutable-root
file prefetch can move executable/library page-cache
work before the response boundary. `--skip-confirmed-prewarm` removes the
otherwise redundant `/bin/true` only when the persistent command agent was
already validated and the real command remains fenced by confirmed wake.

`balanced` is the portable SSD treatment. `anon` and `file` require the host
kernel to accept the corresponding `memory.reclaim` swappiness selector; the
runner fails closed if it does not.

The first 2026-08-30 `nsl17` qualification measured 256-MiB
response-to-full-scan p50/p95 at 1,050.8/1,063.0 ms for reactive refault and
175.5/183.3 ms for speculative preparation, after charging 23.2/27.7 ms of
boundary overrun. Its preserved package is
`docs/performance/orca/orca-speculative-restore-2026-08-30/` in the companion
paper repository.

A direct-path follow-up used FIFO completion, removed confirmed prewarm, and
started one `MADV_WILLNEED` pass 600 ms before the boundary. Across 36 trials
per configuration, fully reclaimed p50/p95/p99 was
100.56/108.28/126.38 ms, while a matched frozen resident control reached
44.46/48.63/49.97 ms with zero reclaim. Preparation had completed, but the
reclaimed path still incurred 65,802/65,862 page faults at p50/p95 because
remote `MADV_WILLNEED` did not install missing PTEs; Ubuntu 6.8 rejected remote
`MADV_POPULATE_READ` with `EINVAL`. The correct 42--60 ms decision for this
full-WSS profile is therefore no reclaim. The follow-up package is
`docs/performance/orca/orca-direct-restore-2026-08-30/`.

A later sparse-frontier E3-v1 exposed why the reset is required: reactive sparse
scans left cold pages swapped, so subsequent predictive trials often reclaimed
nearly zero bytes. Those 18 reports are preserved and excluded. E3-v2 added
only the preregistered full-WSS reset, kept the six floors and timing fixed, and
completed 18 schema-v6 reports. No nonzero point passed both 1.10x resident
guards. Even 4.67 MiB p50 predictive reclaim reached p95/p99 ratios
1.216/1.208; 243.87 MiB reached 1.300/1.298 and post-prepare DRAM returned to
about 263 MiB. The package is
`docs/performance/orca/nsl17-figure-campaign-2026-08-30/`.

This is a synthetic mechanism measurement, not the equal-work trajectory or
density-at-SLO claim gate. It runs no LLM and controls no KV cache. Never use
`--drop-caches`; the runner has no cache-dropping path. On a shared host, pin
both daemon and runner CPUs, use isolated paths/cgroups, and report host noise.
