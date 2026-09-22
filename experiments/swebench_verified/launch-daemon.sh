#!/bin/bash
set -euo pipefail
campaign=/sandboxfs/crate-swebench-20260919
index=$1
[[ "$index" =~ ^[0-9][0-9]$ ]]
root=$campaign/runtime/$index
mount --make-rprivate /
mount --bind "$root" "$root"
mkdir -p "$root/bases/prepared"
mount --bind "$campaign/bases/$index" "$root/bases/prepared"
mount -o remount,bind,ro "$root/bases/prepared"
mount --bind "$campaign/scripts/bwrap-wrapper" /usr/bin/bwrap
exec "$campaign/bin/sandboxfsd" \
  --root "$root" --state-dir "$root/state" --socket "/run/crate-sv-$index.sock" \
  --sandbox-user chaoyi --rootfs "$campaign/rootfs/$index" \
  --sandboxd /usr/local/libexec/sandboxfs/sandboxd \
  --startup-timeout 60s --command-timeout 300s --shutdown-timeout 10s
