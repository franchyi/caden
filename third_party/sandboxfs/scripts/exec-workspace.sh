#!/usr/bin/env bash
set -euo pipefail

[[ $# -ge 3 ]] || {
  echo "usage: $0 <xfs-mount-root> <sandbox-id> <command> [args...]" >&2
  exit 2
}

mount_root=$(readlink -f -- "$1")
sandbox_id=$2
shift 2

[[ "$sandbox_id" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]] || {
  echo "invalid sandbox ID" >&2
  exit 1
}

sandbox_root="$mount_root/sandboxes/$sandbox_id"
merged="$sandbox_root/merged"
control="$sandbox_root/control"
mountpoint -q "$merged" || { echo "workspace is not mounted: $merged" >&2; exit 1; }

exec bwrap \
  --unshare-all \
  --die-with-parent \
  --new-session \
  --ro-bind /usr /usr \
  --ro-bind /etc /etc \
  --symlink usr/bin /bin \
  --symlink usr/sbin /sbin \
  --symlink usr/lib /lib \
  --symlink usr/lib64 /lib64 \
  --dir /home \
  --dir /opt \
  --dir /var \
  --dir /workspace \
  --proc /proc \
  --dev /dev \
  --tmpfs /tmp \
  --tmpfs /run \
  --bind "$merged" /workspace \
  --dir /run/sandboxfs \
  --bind "$control" /run/sandboxfs \
  --chdir /workspace \
  -- "$@"
