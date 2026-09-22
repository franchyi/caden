# CoWStart: XFS-Backed OverlayFS Provisioning for Agent Sandboxes

Status: engineering design proposal  
Date: 2026-07-17  
Target: at least 50% lower sandbox create-to-first-command latency than an
up-front full-copy workspace baseline

## 1. Decision summary

CoWStart provisions a fresh writable agent workspace by mounting a shared,
immutable prepared tree as the lower layer of stock Linux OverlayFS and an
empty, sandbox-private directory as the upper layer. Both lower and upper live
on the same reflink-enabled XFS filesystem so OverlayFS copy-up can clone file
extents and allocate private blocks only as files are changed.

The initial implementation will use Bubblewrap as a regular process sandbox.
The host-side provisioner performs the privileged OverlayFS mount, and
Bubblewrap bind-mounts the merged workspace at `/workspace` in a new mount,
PID, IPC, UTS, and network namespace. A small `sandboxd` process remains alive
inside the sandbox and serves commands over a per-sandbox Unix socket.

CoWStart is a cold-start system, not a live checkpoint/rollback system. It
borrows the fixed OverlayFS plus XFS-reflink data-sharing principles used by
DeltaBox, but it does not implement DeltaFS's live layer-stack mutation, open
file generation switching, CRIU integration, or process templates.

## 2. Problem and scope

An agent sandbox often needs a private writable view of a large prepared
workspace containing a repository, language environment, dependency trees,
tooling, and caches. A straightforward implementation recursively copies the
prepared tree before starting the sandbox. That path scales with bytes and
inode count even though an agent typically changes only a small fraction of
the tree.

The primary scope is:

- A cold sandbox on an already-running Linux host.
- The prepared workspace is already materialized on local storage.
- The agent controller is outside the sandbox and executes tools through a
  sandbox API.
- Readiness means a command has successfully completed through the same API
  used by the agent.
- The initial system creates fresh state only; it does not preserve or restore
  intermediate process state.

The following are explicitly outside the primary claim:

- Pulling an image or repository onto a cold host.
- Host or microVM boot time.
- Live checkpointing and arbitrary historical rollback.
- Process-memory restoration.
- Hiding work behind LLM inference.

Host-cold and remote-image cases may be reported separately, but must not be
mixed into the primary warm-host result.

## 3. Goals and non-goals

### 3.1 Goals

1. Reduce p50 and p95 create-to-first-command latency by at least 50% against
   the full-copy baseline on the selected representative workload.
2. Make startup work largely independent of prepared-workspace byte size and
   inode count.
3. Preserve per-sandbox write isolation.
4. Reduce first-write amplification for large lower-layer files with XFS
   reflink.
5. Use an unmodified mainline Linux kernel and standard runtime interfaces.
6. Measure deferred costs so startup improvement is not obtained by silently
   moving excessive work into the first tool action.

### 3.2 Non-goals

1. DeltaBox-compatible checkpoint or restore semantics.
2. Transparent rollback of a running process.
3. Deduplication of independently created identical data.
4. Production-grade hostile multi-tenant isolation in the first prototype.
5. A new filesystem or kernel module.

## 4. Architecture

### 4.1 Prepared base

An offline builder materializes a versioned base tree:

```text
/agent-xfs-<variant>/bases/base-<digest>/
├── repository/
├── .venv/ or conda environment/
├── dependency caches/
├── agent tools/
└── base metadata
```

The base is immutable while any sandbox references it. It must not contain
tenant credentials or mutable per-run data. A content digest or monotonically
versioned identifier selects the base; updates produce a new base rather than
mutating the active one.

### 4.2 Per-sandbox directories

For sandbox `S`, the provisioner creates:

```text
/agent-xfs-<variant>/sandboxes/S/
├── upper/
├── work/
├── merged/
├── control/
└── state.json
```

`upper` and `work` are unique to the sandbox. `work` is empty at mount time
and resides on the same filesystem as `upper`, as required by OverlayFS.

### 4.3 Merged workspace

The host provisioner mounts:

```text
upper-S                 private and writable
─────────────────────────────────────────────
base-<digest>           shared and immutable
```

Conceptually:

```bash
mount -t overlay overlay \
  -o lowerdir=/agent-xfs-<variant>/bases/base-<digest>,\
upperdir=/agent-xfs-<variant>/sandboxes/S/upper,\
workdir=/agent-xfs-<variant>/sandboxes/S/work \
  /agent-xfs-<variant>/sandboxes/S/merged
```

The merged mount is bind-mounted into the Bubblewrap namespace at
`/workspace`. The agent process sees an ordinary writable directory.

### 4.4 File operations

Reads of unchanged files resolve to the lower tree and create no private
copy. Newly created files are written directly to `upper-S`. Deletions are
represented by OverlayFS whiteouts in `upper-S` and do not alter the base.

On the first modification of a lower regular file, OverlayFS creates an upper
file. Current Linux OverlayFS first attempts `vfs_clone_file_range` when the
source and destination are on the same filesystem, then falls back to a data
copy if cloning is unavailable. On reflink-enabled XFS, the clone shares
physical extents; writes allocate private blocks for changed regions.

This creates two levels of copy-on-write:

```text
OverlayFS: chooses lower or private file version
XFS:       shares unchanged physical extents between those versions
```

Reflink is not content-based deduplication. It explicitly clones extent
references from a known source; it does not scan for unrelated identical
blocks.

### 4.5 Lifecycle

Creation:

1. Receive `CreateSandbox(base_digest, limits)`.
2. Validate that the selected base exists and is immutable.
3. Create unique `upper`, `work`, `merged`, and `control` directories.
4. Mount the OverlayFS workspace.
5. Construct the Bubblewrap invocation with a read-only prepared root, private
   `/tmp` and `/run`, the merged workspace, and a private control directory.
6. Start `sandboxd` inside Bubblewrap and place it in the experiment cgroup.
7. Probe `sandboxd` through the normal command socket.
8. Return ready only after the probe succeeds.

Destruction:

1. Stop all sandbox processes and wait for exit.
2. Remove the Bubblewrap control socket and cgroup state.
3. Unmount `merged`.
4. Remove only the explicitly resolved directory for sandbox `S`.
5. Record leaked-mount or cleanup failures for reconciliation.

Cleanup must never operate on an unresolved environment variable, glob,
workspace root, or shared base path.

## 5. Sandbox runtime decision

### 5.1 MVP: Bubblewrap

Use Bubblewrap directly as the regular sandbox. Bubblewrap begins with an
empty mount namespace and constructs the filesystem view from explicit bind,
read-only bind, proc, device, and tmpfs mounts. It has no resident image daemon
or separate snapshotter, so the measured storage difference remains the
full-copy workspace versus the two OverlayFS treatments.

The host prepares a minimal Ubuntu root tree once and exposes it read-only.
For sandbox `S`, the invocation is conceptually:

```bash
bwrap \
  --unshare-all \
  --die-with-parent \
  --new-session \
  --ro-bind /opt/cowstart/rootfs / \
  --proc /proc \
  --dev /dev \
  --tmpfs /tmp \
  --tmpfs /run \
  --bind /agent-xfs-<variant>/sandboxes/S/merged /workspace \
  --bind /agent-xfs-<variant>/sandboxes/S/control /run/cowstart \
  --chdir /workspace \
  -- /usr/local/bin/sandboxd --socket /run/cowstart/control.sock
```

The workspace source path changes between T0 and T1, but the prepared root
contents and other Bubblewrap arguments do not. The provisioner launches
Bubblewrap as an unprivileged sandbox UID after it completes the host-side
OverlayFS mount. Bubblewrap does not receive host `CAP_SYS_ADMIN`.

The host also places the Bubblewrap process in a cgroup v2 subtree with fixed
CPU, memory, PID, and I/O limits. Network remains unshared for microbenchmarks;
end-to-end tests may use a controlled network namespace. `$HOME` and language
environments reside under `/workspace`, while `/tmp` and `/run` are private
tmpfs mounts.

Readiness is the first successful command response from `sandboxd`, not merely
the existence of the Bubblewrap process.

### 5.2 Why Bubblewrap rather than one Firecracker VM per sandbox

Firecracker is technically usable, but its normal storage interface exposes
file-backed block devices to a guest. One microVM per sandbox would therefore
require per-VM disk images or another guest-sharing mechanism, changing the
experiment from host filesystem provisioning to virtual block-device
provisioning. It would also add VM boot and guest initialization to the metric.

Bubblewrap can bind the already-mounted host workspace directly, which keeps
the comparison focused on full copy, fixed OverlayFS, and XFS reflink. Because
hostile multi-tenant isolation is not a goal, this is the smaller and cleaner
regular sandbox for the project.

An optional later deployment can run one long-lived Firecracker guest and
place the XFS filesystem plus multiple Bubblewrap sandboxes inside that guest.
That retains a VM boundary without requiring one filesystem image per agent.
It is not part of the primary result.

### 5.3 Security qualification

Bubblewrap supplies namespace and mount isolation, but the project does not
claim a hostile multi-tenant security boundary. OverlayFS itself remains a
storage view rather than an isolation mechanism.

## 6. AWS EC2 testbed

### 6.1 Selected host

Use an on-demand `i7i.2xlarge` in `ap-southeast-1` (Asia Pacific, Singapore)
for the primary campaign:

| Property | Selection |
|---|---|
| Instance | `i7i.2xlarge` |
| CPU | 8 vCPUs, x86-64 |
| Memory | 64 GiB |
| Local storage | 1 × 1,875 GB AWS Nitro NVMe SSD |
| Region | `ap-southeast-1` |
| Purchase model | On-demand for repeatability |
| Root volume | 100 GiB gp3 EBS for OS, code, and durable results |

I7i uses newer PCIe Gen5 third-generation AWS Nitro SSDs and provides lower
storage latency and latency variability than the prior I4i generation. The
2xlarge is large enough for the primary single-sandbox and 8-way experiments
without paying for 32 vCPUs, 256 GiB of memory, or a second NVMe device that
the filesystem comparison does not require.

Use these size tiers deliberately rather than treating the largest host as the
default:

| Purpose | Instance | Resources | Qualification |
|---|---|---|---|
| Cost-minimum smoke test | `i7i.xlarge` | 4 vCPUs, 32 GiB, 937.5 GB NVMe | Functional testing; limited concurrency and page cache |
| Primary measurement | `i7i.2xlarge` | 8 vCPUs, 64 GiB, 1,875 GB NVMe | Recommended default |
| Optional scale validation | `i7i.4xlarge` | 16 vCPUs, 128 GiB, 3,750 GB NVMe | Use for 16- or 32-way contention tests |
| Optional 64-way stress | `i7i.8xlarge` | 32 vCPUs, 256 GiB, 2 × 3,750 GB NVMe | Not required for the 50% primary claim |

Do not compare latency collected on different sizes as though it came from one
experiment. A scale-host result is a separate dataset, and all three primary
configurations must be rerun on that host if they are compared there.

### 6.2 Operating system

Use the official Ubuntu Server 26.04 LTS amd64 AWS image with its distribution
kernel. Ubuntu 26.04 is now a released LTS and Canonical publishes its cloud
images in AWS regions. The earlier 24.04 choice was a conservative maturity
choice, not a requirement of OverlayFS, XFS reflink, or Bubblewrap.

Resolve the newest official image at campaign setup from Canonical's owner ID
and documented name pattern, then pin the resulting immutable AMI ID:

```bash
aws ec2 describe-images \
  --owners 099720109477 \
  --region ap-southeast-1 \
  --filters \
    'Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-resolute-26.04-amd64-server-*' \
    'Name=state,Values=available' \
  --query 'sort_by(Images,&CreationDate)[-1].[ImageId,CreationDate,Name]' \
  --output table
```

Record the AMI ID, `uname -r`, XFS utilities version, Bubblewrap version, CPU
model, microcode, and NVMe firmware with every result. Do not change the AMI
or kernel within a comparison campaign. Before measurement, verify that the
selected AMI supports the required OverlayFS mount, XFS `reflink=0/1`, an
actual `FICLONE`, Bubblewrap namespaces, and the intended cgroup controls.
This preflight protects the experiment from regressions in a relatively new
distribution release.

### 6.3 Local NVMe layout

Discover instance-store devices by model and serial rather than assuming
Linux device names. Keep durable scripts and results on EBS; instance-store
data is ephemeral and must be recreated after stop or termination.

The primary host has one local NVMe device. Reuse that same physical device in
two sequential campaigns so device differences cannot confound the result:

| Campaign | XFS format | Mount | Configurations |
|---|---|---|---|
| A | `reflink=1` | `/agent-xfs-t1` | Full-copy baseline and T1 |
| B | `reflink=0` | `/agent-xfs-t0` | T0 |

Campaign A compares the headline baseline and T1 on one unchanged filesystem.
The baseline forces a real copy with `cp -a --reflink=never`. After Campaign A,
copy measurements to the durable EBS root volume, destroy the XFS filesystem,
reformat the same instance-store device with `reflink=0`, reconstruct the
identical prepared base, and run Campaign B.

Reformatting is destructive and must target only the explicitly discovered
ephemeral instance-store device. The harness must verify the device model,
serial, mount state, and base digest before proceeding. For stronger control
of cache and run-order effects, repeat on a fresh instance in reverse campaign
order; do not carry a performance cache across the reformat boundary.

## 7. XFS layout and validation

The base, upper, and work directories must reside on the same mounted XFS
filesystem for file-extents to be shared. Cross-filesystem reflink is not
possible.

The sequential campaigns use the following logical layouts on the same
physical device at different times:

```text
/agent-xfs-t0/
├── bases/
├── sandboxes/
└── measurements/

/agent-xfs-t1/
├── bases/
├── sandboxes/
└── measurements/
```

Create each filesystem with the feature setting explicit. These commands run
in separate campaigns and erase the selected instance-store device:

```bash
mkfs.xfs -f -m reflink=1 <validated-instance-store-device>  # Campaign A
mkfs.xfs -f -m reflink=0 <validated-instance-store-device>  # Campaign B
```

Verify the mounted filesystem in each campaign:

```bash
xfs_info /agent-xfs-t1  # Campaign A: must report reflink=1
xfs_info /agent-xfs-t0  # Campaign B: must report reflink=0
```

Only the command for the active campaign will succeed. A T1 setup preflight
must also prove that a real clone succeeds:

```bash
cp --reflink=always source probe-clone
```

Finally, the experiment must verify OverlayFS copy-up itself rather than
assuming it reflinks. Use a large lower file, change one aligned block through
the merged mount, and inspect allocated blocks/extents and device write
counters.

The DAX mount mode is incompatible with reflink-enabled XFS and is out of
scope.

## 8. Baseline and treatments

All configurations use the same instance type, base contents, read-only
Bubblewrap root, limits, command API, readiness probe, and physical NVMe
device. The baseline and T1 run in Campaign A without a reformat between them;
T0 runs after the same device is reformatted for Campaign B. A reverse-order
replication controls for campaign order. Only workspace provisioning and the
declared reflink ablation differ.

### Baseline: full copy

Recursively copy the prepared tree into a private directory with reflink
disabled:

```bash
cp -a --reflink=never <base>/ <private-workspace>/
```

Then bind-mount the private directory at `/workspace` and start the identical
Bubblewrap sandbox. This is the only baseline.

### T0: stock OverlayFS without block reflink

Use stock OverlayFS on XFS created with `reflink=0`. This isolates the benefit
of replacing up-front traversal with a mount, but may perform a full file
copy-up on first modification. T0 is part of our design evaluation, not a
baseline.

### T1: CoWStart

Use stock OverlayFS with base and upper on the same reflink-enabled XFS
filesystem. This is the proposed system.

### Comparison matrix

| Configuration | Up-front tree walk | Up-front file-data copy | Large-file first-write behavior | Stock kernel |
|---|---:|---:|---|---:|
| Baseline: full copy | Yes | Yes | Private already | Yes |
| T0 OverlayFS without reflink | No | No | May copy whole file | Yes |
| T1 CoWStart | No | No | Reflink then block CoW | Yes |

## 9. Measurement contract

### 9.1 Primary metric

End-to-end cold-start latency:

```text
T_ready = timestamp(first successful command response)
        - timestamp(CreateSandbox request received)
```

The readiness command must traverse the production sandbox API. Merely
completing the mount or observing a running PID is not sufficient.

### 9.2 Phase breakdown

Record monotonic timestamps for:

- request received;
- workspace preparation started and completed;
- Bubblewrap process spawn started and completed;
- `sandboxd` socket became reachable;
- readiness command sent and completed;
- destruction and unmount completed.

Report workspace provisioning separately from total readiness.

### 9.3 Required distributions

- p50, p95, and p99 over at least 100 iterations per configuration.
- Primary concurrency 1 and 8 on `i7i.2xlarge`; 16 is an oversubscription
  stress point on the same host.
- Optional 16- and 32-way scale validation on `i7i.4xlarge`; 64-way testing on
  `i7i.8xlarge` is separate from the primary claim.
- Warm page cache and explicitly controlled cold-page-cache runs.
- At least small, medium, and large prepared trees.
- A many-small-files dependency tree and a large-file workload.
- At least one representative SWE-bench-style repository/environment.

### 9.4 Deferred-cost tests

Measure after readiness:

1. Read an unchanged lower file.
2. Create a new small file.
3. Modify 4 KiB inside 1 MiB, 100 MiB, and 1 GiB lower files.
4. Replace an entire large file using temporary-file-plus-rename.
5. Rewrite a large file completely.
6. Create large new build artifacts.
7. Perform representative package-manager and patch-application operations.

Record command latency, logical bytes, allocated bytes, and physical device
I/O. Reflink helps in-place partial modifications, but it cannot share a newly
created temporary replacement or data that is eventually fully overwritten.

### 9.5 Capacity and correctness

Measure:

- disk allocation per live sandbox;
- upper-layer growth;
- mount and inode consumption;
- host CPU and I/O utilization under concurrent creation;
- cleanup latency and leaked mounts;
- base immutability;
- cross-sandbox visibility;
- create, modify, rename, hard-link, symlink, delete, and whiteout semantics.

## 10. Success criteria

1. At least 50% lower p50 and p95 `T_ready` than the full-copy baseline on the
   declared primary workload and concurrency.
2. T0 and T1 results are both reported so the no-tree-walk and reflink effects
   remain separate.
3. No cross-sandbox mutation visibility in correctness tests.
4. No base-tree mutations during or after tests.
5. No leaked mount or runtime state after successful destruction.
6. Representative end-to-end task time regresses by no more than 10%.
7. First-write latency and physical I/O are explicitly reported; no claim may
   rely only on faster mount completion.

The feasibility check uses:

```text
overall reduction
  = baseline filesystem-time fraction
    × (1 - optimized filesystem time / baseline filesystem time)
```

If workspace preparation is not at least roughly half of baseline readiness,
filesystem optimization alone cannot deliver a 50% total reduction.

## 11. Relationship to DeltaBox

Both systems use a shared lower, a writable upper, and extent sharing on XFS.
XFS reflink is an existing filesystem capability; DeltaBox does not invent
it.

DeltaBox adds mechanisms that CoWStart deliberately omits:

- a custom OverlayFS `ioctl` that changes the layer stack without unmounting;
- demotion of each current upper into a retained checkpoint layer;
- generation-aware handling of files opened across a checkpoint;
- arbitrary selection of historical layer stacks;
- CRIU checkpoint images and frozen process templates;
- coupled filesystem/process consistency and rollback;
- checkpoint work overlapped with LLM inference.

CoWStart and DeltaBox therefore optimize different transitions:

```text
CoWStart:
no sandbox → fixed CoW workspace → fresh process → first command

DeltaBox:
live checkpoint k → select historical filesystem → restore warm process
```

DeltaBox's millisecond restore numbers are not comparable to CoWStart's
create-to-first-command metric because DeltaBox starts from an existing live
VM, checkpoint image, and often a resident process template.

## 12. Can ext4 provide equivalent reflink behavior?

### 12.1 Native ext4: no

Current mainline ext4 does not implement the VFS file-range remap operation
used by `FICLONE`/`FICLONERANGE` to share regular-file extents. A genuine
ext4-native reflink implementation requires new on-disk reference-count
metadata, clone operations, CoW write handling, crash consistency, repair
support, and kernel filesystem changes.

Consequently:

- `cp --reflink=always` should fail on ext4 rather than create a clone;
- `cp --reflink=auto` falls back to a normal data copy;
- stock OverlayFS on ext4 still avoids up-front base copying, but first write
  to a lower file may copy the complete file;
- hard links are not a safe substitute because an in-place write changes the
  shared inode for every sandbox.

A userspace utility cannot add transparent extent sharing to an existing ext4
mount because ext4 itself must redirect writes and maintain block reference
counts.

### 12.2 Stock-kernel alternatives

| Alternative | Kernel modification | CoW granularity | Fit for this project |
|---|---:|---|---|
| XFS reflink | No | File extents/blocks | Recommended |
| Btrfs reflink | No | File extents/blocks | Strong alternative |
| Btrfs subvolume snapshot | No | Filesystem tree/blocks | Viable alternative design |
| LVM/dm-thin snapshot with ext4 inside | No custom kernel | Block device blocks | Possible, operationally heavier |
| qcow2 backing image | No custom kernel | Virtual disk clusters | Better suited to VM/block-image designs |
| Custom FUSE filesystem | No kernel patch | Implementation-defined | Too much complexity for MVP |
| OverlayFS on ext4 | No | Whole file on first write | Fast start, weaker write path |

Btrfs supports reflink as a shallow file-data copy whose blocks remain shared
until modification. It can replace XFS in the design, but must be benchmarked
for the target metadata and concurrency workload rather than assumed better.

LVM thin snapshots can place ext4 inside independently writable thin volumes
that initially share blocks. This achieves CoW below ext4 without changing
ext4, but requires a thin pool, per-sandbox block device and mount lifecycle,
capacity monitoring, and different failure recovery. It is a block-volume
design rather than per-file OverlayFS copy-up.

## 13. Risks and mitigations

| Risk | Consequence | Mitigation |
|---|---|---|
| Lower and upper are on different filesystems | Overlay copy-up falls back to data copy | Fail setup preflight; require same XFS mount |
| T1 XFS was created without reflink | Whole-file copy-up | Require T1 `xfs_info` to report `reflink=1` |
| Tool writes temp file then renames | New file cannot share lower extents | Measure real tools; report limitation |
| Base changes while mounted | Undefined OverlayFS behavior/corruption | Immutable versioned bases and reference tracking |
| Upper/work reused across sandboxes | Data leakage and undefined behavior | Unique IDs and exclusive ownership |
| Sandbox receives mount privilege | Base or host compromise | Mount only in host provisioner; do not delegate `CAP_SYS_ADMIN` |
| Bubblewrap root differs across treatments | Misattributed latency | Use one immutable root and identical arguments |
| Reformat targets the wrong block device | Loss of durable OS or results | Resolve and validate instance-store model/serial; refuse root/EBS devices; persist results before reformat |
| Ubuntu 26.04 package or kernel drift | Irreproducible or invalid comparison | Pin AMI and kernel per campaign; run full preflight; report later-image replications separately |
| Runtime initialization dominates | Missed 50% target | Profile first; treat process templates as separate follow-on |
| Mount leaks after crashes | Resource exhaustion | Persistent state records and reconciliation loop |
| XFS metadata pressure at high density | Tail latency | Concurrency tests, inode/mount monitoring, capacity limits |

## 14. Implementation plan

### Phase 0: feasibility profile

- Instrument the current full-copy path.
- Establish filesystem, runtime, and readiness fractions.
- Verify that the 50% target is physically possible.

### Phase 1: deterministic harness

- Build the prepared workspace corpus.
- Implement the full-copy baseline.
- Generate one reusable minimal root and Bubblewrap command template.
- Add phase timestamps and repeated-run reporting.

### Phase 2: CoWStart provisioner

- Implement XFS preflight checks.
- Implement mount/Bubblewrap/start/exec/destroy lifecycle.
- Add persistent sandbox state and crash reconciliation.
- Add filesystem correctness and isolation tests.

### Phase 3: evaluation

- Run baseline and T1 on the `reflink=1` campaign, persist results, then
  reformat the same NVMe device and run T0 on `reflink=0`.
- Repeat in reverse campaign order on a fresh instance if the result will be
  used for a formal claim.
- Run primary latency distributions and concurrency sweeps on `i7i.2xlarge`.
- Run deferred-read/write and storage tests.
- Run representative agent tasks.
- Attribute wins separately to no-tree-walk and XFS reflink.

### Phase 4: optional Firecracker enclosure

- Evaluate one long-lived Firecracker guest containing the XFS filesystem and
  multiple Bubblewrap sandboxes only after the filesystem result is
  established.
- Keep the result separate so VM overhead does not rewrite the primary
  baseline.

## 15. References

- [Linux OverlayFS documentation](https://docs.kernel.org/filesystems/overlayfs.html)
- [Linux OverlayFS copy-up implementation](https://github.com/torvalds/linux/blob/master/fs/overlayfs/copy_up.c)
- [XFS `mkfs.xfs` reflink documentation](https://man7.org/linux/man-pages/man8/mkfs.xfs.8.html)
- [Linux `FICLONE`/`FICLONERANGE` documentation](https://man7.org/linux/man-pages/man2/ioctl_ficlone.2.html)
- [Btrfs reflink documentation](https://btrfs.readthedocs.io/en/latest/Reflink.html)
- [LVM thin provisioning documentation](https://man7.org/linux/man-pages/man7/lvmthin.7.html)
- [Bubblewrap](https://github.com/containers/bubblewrap)
- [Firecracker](https://github.com/firecracker-microvm/firecracker)
- [Amazon EC2 I7i instances](https://aws.amazon.com/ec2/instance-types/i7i/)
- [I7i availability in Singapore](https://aws.amazon.com/about-aws/whats-new/2025/12/ec2-i7i-instances-additional-regions/)
- [Ubuntu 26.04 LTS release notes](https://documentation.ubuntu.com/release-notes/26.04/)
- [Ubuntu Server on AWS](https://ubuntu.com/aws)
- [Finding official Ubuntu AWS images](https://documentation.ubuntu.com/aws/en/latest/aws-how-to/instances/find-ubuntu-images/)
- [DeltaBox](https://arxiv.org/html/2605.22781v2)
