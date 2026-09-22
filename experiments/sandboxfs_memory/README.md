# Caden + SandboxFS memory campaign

This harness measures the deterministic mechanism before any internal
Multi-Agent business claim.

## Required host state

- Linux cgroup v2 with `cgroup.freeze` and `memory.reclaim`.
- SandboxFS installed and running on T1 XFS.
- A registered `bench-medium` base.
- An explicitly identified, unshared local-SSD swapfile if anonymous memory is
  expected to leave physical host DRAM. Record the actual device class; on
  `nsl17`, `/dev/sdc` is local RAID-attached SSD, not NVMe.

The existing node helper checks the cgroup controls. It also creates zram;
replace that with the measured SSD tier before the memory campaign:

```bash
sudo experiments/checkpoint_tiers/setup_node.sh
sudo experiments/sandboxfs_memory/setup_nvme_swap.sh /agent-xfs-t1 16G
```

zram/zswap is a separate compression treatment, not the primary SSD result,
because its compressed payload still occupies host DRAM. The runner supports a
fail-closed zswap treatment with `--allow-zswap-compression --reclaim-tier
compressed`; it records `memory.zswap.current` separately. `--hot-reserve-mib`
bounds requested reclaim, and `--reclaim-mode anon` selects anonymous-only
reclaim. These controls are mechanism probes, not request-aware evidence.

## F0-S0 baseline

```bash
sudo PYTHONPATH=src python3 experiments/sandboxfs_memory/run_campaign.py \
  --base bench-medium \
  --mode baseline \
  --policy static \
  --sandboxes 32 \
  --wss-mib 128 \
  --wss-pattern mixed \
  --wake-stride-kib 64 \
  --llm-wait-seconds 15 \
  --max-admissions 4 \
  --max-wakes 1 \
  --drop-caches \
  --output /durable-ebs/caden-sandboxfs/f0-s0.json
```

## T1-S2 treatment

```bash
sudo PYTHONPATH=src python3 experiments/sandboxfs_memory/run_campaign.py \
  --base bench-medium \
  --mode t1 \
  --policy caden \
  --sandboxes 32 \
  --wss-mib 128 \
  --wss-pattern mixed \
  --wake-stride-kib 64 \
  --llm-wait-seconds 15 \
  --max-admissions 4 \
  --max-wakes 1 \
  --max-movements 2 \
  --confirmed-movement-reserve 1 \
  --pool-target 1 \
  --pool-max 2 \
  --drop-caches \
  --output /durable-ebs/caden-sandboxfs/t1-s2.json
```

Run the pair in both orders and repeat each configuration at least three times
for a formal result. The same corpus, wait trace, WSS, host, kernel, swap state,
and commit pair are required.

Compare one smoke pair:

```bash
python3 experiments/sandboxfs_memory/summarize_campaigns.py \
  --baseline /durable-ebs/caden-sandboxfs/f0-s0.json \
  --treatment /durable-ebs/caden-sandboxfs/t1-s2.json \
  --output /durable-ebs/caden-sandboxfs/comparison.json
```

`mechanism_claim_pass` can validate the synthetic holder mechanism.
`business_claim_pass` intentionally remains false: it requires the registered
internal workload, repetitions, and turn-latency SLO.

The harness submits the sandbox burst asynchronously, includes a successful
`/bin/true` API call in cold-start latency, uses a fixed LLM-wait duration, and
wakes all agents concurrently. Reclaim that misses the wait boundary is charged
to wake latency rather than extending the treatment's idle period.

`mixed` makes 25% of each anonymous working set deterministic pseudo-random
and leaves the remainder zero-heavy. Also run `random` as a zram-compression
guardrail. Any result depends on the stated pattern; neither substitutes for
the internal workload.

The primary wake probe reads one page per 64 KiB, modeling a tool burst that
touches a subset of a larger retained heap. Run `--wake-stride-kib 4` as a
worst-case full-refault stress test. Do not compare reports with different
wake strides, and always disclose the stride with restore latency.
