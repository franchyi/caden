# Workspace Agent Instructions

## Response format

- After every substantive completed response, append a short `## Summary` section.
- Put the summary after the full response and keep it to one to three concise bullets.

## LLM agent sandbox cold-start project

- The current engineering direction is a stock-Linux copy-on-write workspace provisioner inspired by DeltaBox's DeltaFS design, not a full reproduction of DeltaBox or Crab.
- Use a locally available immutable prepared workspace as the shared OverlayFS lower layer and give every sandbox unique private `upperdir` and `workdir` directories.
- Treat copy-on-write as the primary cold-start optimization. Overlapping work with LLM inference is optional follow-on work and must not be credited as filesystem acceleration unless it is separately measured.
- Define the only baseline as recursively copying the same locally prepared workspace into a private ordinary filesystem tree with reflink disabled. T0 (OverlayFS on XFS `reflink=0`) and T1 (OverlayFS on XFS `reflink=1`) are design treatments, not additional baselines.
- Define end-to-end cold-start latency from receipt of the sandbox-create request to successful completion of a command through the normal sandbox API. Report provisioning time separately.
- Scope the primary claim as a cold sandbox on a warm host with the prepared base already local. Report host-cold or remote-image results separately.
- Before promising a 50% end-to-end reduction, profile the baseline and verify that filesystem preparation contributes enough latency. Use the estimate `overall reduction = filesystem fraction * (1 - optimized/original filesystem time)`.
- Evaluate p50, p95, and p99 latency, concurrency, first-read and first-write costs, storage use, cleanup, filesystem correctness, and cross-sandbox isolation.
- A full DeltaBox reproduction would require custom dynamic OverlayFS switching, Firecracker, CRIU, and process templates. Crab's eBPF-guided selective checkpointing targets runtime recovery rather than initial cold start; reserve both for later rollback/checkpoint work.
- Use `i7i.2xlarge` (8 vCPUs, 64 GiB, one 1,875 GB local NVMe SSD) as the default AWS measurement host. Run T0 and T1 as sequential XFS-format campaigns on the same local device; use a larger I7i size only for a separately reported high-concurrency scale test.
- Use the official Ubuntu Server 26.04 LTS amd64 AWS image, pin the exact AMI ID and kernel within each campaign, and run filesystem/runtime preflight tests before collecting results.
- After each EC2 experiment or campaign, first preserve reports on durable EBS and sync required artifacts to the repository, then stop the measurement instance and verify AWS reports it as `stopped`. Treat the I7i instance-store XFS volume as ephemeral and rebuild it, the prepared base, and service state after the next start.
