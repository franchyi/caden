# sandboxfs

`sandboxfs` is a working stock-Linux implementation for fast LLM-agent
workspace startup. It mounts an immutable prepared tree as an OverlayFS lower
layer and gives every sandbox a private writable upper layer. With XFS
`reflink=1`, first modification of a large lower file shares its unchanged
extents instead of copying the whole file.

The implementation deliberately has no custom kernel code, filesystem module,
or container image runtime. Bubblewrap is the regular process sandbox; a
narrow privileged host daemon owns mounts and cgroups.

## Implemented components

- `sandboxfsd`: prepared-base registry, full-copy/OverlayFS workspace
  provisioning, Bubblewrap lifecycle, cgroup v2 limits, recovery, cleanup, and
  timing instrumentation.
- `sandboxd`: persistent unprivileged command server inside each sandbox,
  reached through a private Unix socket.
- `sandboxfsctl`: JSON control CLI for base, sandbox, and command operations.
- `sandboxfsbench`: concurrent cold-start benchmark with p50/p95/p99 output.
- `sandboxfscorpus`: deterministic mixed file-count/large-file corpus builder.
- Shell tooling: safe XFS campaign setup, host bootstrap, preflight,
  integration/correctness tests, deferred-write tests, and EC2 automation.

See [the engineering design](docs/design.md), the
[design and implementation reflection](docs/design-implementation-reflection.md),
and the [EC2 evaluation](results/ec2-2026-07-17/README.md).

## Testbed

- EC2 `i7i.2xlarge`, Ubuntu Server 26.04 LTS amd64.
- Local instance-store NVMe formatted as XFS `reflink=1` for T1.
- `/agent-xfs-t1/bases` contains immutable prepared bases.
- `/agent-xfs-t1/sandboxes` contains private upper/work/merged directories.
- Full-copy baseline uses `cp -a --reflink=never` on the same filesystem.

T0 is a separate destructive campaign: preserve results on EBS, reformat the
same instance-store disk as XFS `reflink=0`, and mount it at `/agent-xfs-t0`.

## Bootstrap

Install host dependencies:

```bash
sudo ./scripts/bootstrap-host.sh
```

Identify the local instance-store disk before formatting:

```bash
lsblk -d -o NAME,SIZE,MODEL,SERIAL,TYPE
sudo ./scripts/prepare-xfs.sh /dev/nvme1n1 t1
```

`prepare-xfs.sh` is destructive. It refuses the root disk, a mounted device,
and a device whose model is not `Amazon EC2 NVMe Instance Storage`.

Run preflight and end-to-end smoke tests:

```bash
sudo ./scripts/preflight.sh /agent-xfs-t1 1
sudo -u ubuntu ./scripts/smoke-test.sh /agent-xfs-t1
```

## Build and install

```bash
make test
make build

sudo ./scripts/build-agent-rootfs.sh \
  /opt/sandboxfs/rootfs/ubuntu-26.04-agent-<version> \
  ./bin/sandboxd

sudo ./scripts/install-service.sh \
  /agent-xfs-t1 \
  /opt/sandboxfs/rootfs/<versioned-rootfs>
sudo sandboxfsctl system
```

The builder creates a new Ubuntu 26.04 rootfs with Ubuntu Standard and the
multi-language agent toolchain, then emits adjacent package-manifest and size
files. It refuses an existing rootfs path. The service installer requires an
explicit rootfs containing the installed `sandboxd` binary, so deployments
cannot silently borrow the host userspace.

The service reconciles stale state on startup. Its systemd unit delegates a
cgroup v2 subtree so each sandbox receives CPU, memory, and PID limits. The
host control socket is root-only in the MVP, so CLI examples use `sudo`.

## Workspace operations

Create and register a prepared base. Its lifecycle is immutable, while ordinary
writable file modes are retained so OverlayFS can authorize copy-up for the
sandbox user:

```bash
sudo mkdir -p /agent-xfs-t1/bases/example/repository
echo hello | sudo tee /agent-xfs-t1/bases/example/repository/hello.txt
sudo chown -R ubuntu:ubuntu /agent-xfs-t1/bases/example
sudo sandboxfsctl base-register example /agent-xfs-t1/bases/example
```

Create a private T1 workspace, execute through the persistent sandbox API, and
destroy it:

```bash
sudo sandboxfsctl create --id demo --base example --mode t1
sudo sandboxfsctl exec demo -- sh -lc 'cat repository/hello.txt; echo private > agent.txt'
sudo sandboxfsctl destroy demo
```

The full-copy baseline uses the same API with `--mode baseline` and explicitly
disables reflink via `cp -a --reflink=never`. Use `--mode t0` only when the
active XFS filesystem reports `reflink=0`; use `--mode t1` only for
`reflink=1`.

## Validation and measurement

```bash
sudo ./scripts/preflight.sh /agent-xfs-t1 1
sudo ./scripts/integration-test.sh

sudo sandboxfsbench \
  --base bench-medium \
  --modes baseline,t1 \
  --iterations 100 \
  --concurrency 1,8 \
  --output /path/on/durable-ebs/campaign-t1.json

sudo ./scripts/deferred-cost-test.sh \
  bench-medium /path/on/durable-ebs/deferred-t1.json
```

Cold-start latency is measured from receipt of `CreateSandbox` until a command
successfully completes through `sandboxd`; workspace provisioning is also
reported separately. The committed EC2 T1 campaign achieved 94.4–96.4% p95
reduction against full copy, with 400/400 successful cold starts across the
treatment and paired baseline at the two measured concurrency levels. T0
achieved similar startup latency but copied an entire lower file on its first
partial write; T1's XFS reflink copy-up avoided that deferred amplification.

## Development loop

GitHub `origin` is the durable source repository. The bare `ec2` remote is the
deployment/test target. After making and checking a change, push both before
running the remote test:

```bash
git add <files>
git commit -m 'Describe the change'
git push origin main
git push ec2 main

SANDBOXFS_HOST=<public-ip-or-dns> \
SANDBOXFS_SSH_KEY=<private-key-path> \
  ./scripts/remote-test.sh
```

`remote-test.sh` fast-forwards the EC2 working tree, runs unit/static checks,
reinstalls the service, checks XFS/OverlayFS/reflink prerequisites, and runs the
baseline plus active-treatment integration suite. The instance-store volume is
ephemeral; keep reports on the root EBS volume before switching T0/T1 formats.
