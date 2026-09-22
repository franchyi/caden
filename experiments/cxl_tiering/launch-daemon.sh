#!/bin/bash
# One per-task SandboxFS daemon for an isolated tiering run.
#
# Runs inside a transient unit with PrivateMounts=yes, so every mount below
# disappears with the unit. The September 19 campaign tree is used strictly as
# read-only input (prepared base, root filesystem, binaries); all runtime state
# lives under this run's own directory.
set -euo pipefail
run_root=$1   # /data/chaoyi/crate-tiering/<run-id>
inputs=$2     # /sandboxfs/crate-swebench-20260919 (read-only inputs)
tag=$3
index=$4
[[ "$index" =~ ^[0-9][0-9]$ ]]
[[ "$tag" =~ ^[a-z0-9]{4,16}$ ]]
[[ "$run_root" == /data/chaoyi/crate-tiering/* && -d "$run_root" && ! -L "$run_root" ]]
root=$run_root/runtime/$index
mkdir -p "$root/sandboxes" "$root/state" "$root/measurements" "$root/bases/prepared"
mount --make-rprivate /
# SandboxFS dials <root>/sandboxes/<id>/control/control.sock; a Unix socket
# path is limited to 107 bytes and the required run-directory layout alone
# would exceed it. The daemon therefore sees this run's runtime directory at a
# short path on a tmpfs that exists only in this unit's private mount
# namespace. All data still lives under $run_root; the host sees no new path.
mount -t tmpfs -o mode=0755,size=1m tmpfs /mnt
short=/mnt/crate-tier/$index
mkdir -p "$short"
mount --bind "$root" "$short"
prepared=$inputs/bases/$index
if [[ -d "$run_root/inputs/private-bases/$index" && ! -L "$run_root/inputs/private-bases/$index" ]]; then
    prepared=$run_root/inputs/private-bases/$index
fi
mount --bind "$prepared" "$short/bases/prepared"
mount -o remount,bind,ro "$short/bases/prepared"
# Read-only historical reference for authenticating private-clone content.
# SandboxFS accepts registrations only beneath its own bases directory.
mkdir -p "$short/bases/reference"
mount --bind "$inputs/bases/$index" "$short/bases/reference"
mount -o remount,bind,ro "$short/bases/reference"
mount --bind "$inputs/scripts/bwrap-wrapper" /usr/bin/bwrap
exec "$inputs/bin/sandboxfsd" \
  --root "$short" --state-dir "$short/state" --socket "/run/crate-tier-$tag-$index.sock" \
  --sandbox-user chaoyi --rootfs "$inputs/rootfs/$index" \
  --sandboxd /usr/local/libexec/sandboxfs/sandboxd \
  --startup-timeout 60s --command-timeout 300s --shutdown-timeout 10s
