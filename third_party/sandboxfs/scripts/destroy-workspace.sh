#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 2 ]] || {
  echo "usage: sudo $0 <xfs-mount-root> <sandbox-id>" >&2
  exit 2
}
[[ ${EUID} -eq 0 ]] || { echo "destroy-workspace.sh must run as root" >&2; exit 1; }

mount_root=$(readlink -f -- "$1")
sandbox_id=$2
[[ "$sandbox_id" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]] || {
  echo "invalid sandbox ID" >&2
  exit 1
}

sandbox_root="$mount_root/sandboxes/$sandbox_id"
expected_prefix="$mount_root/sandboxes/"
[[ "$sandbox_root" == "$expected_prefix"* && -d "$sandbox_root" ]] || {
  echo "resolved sandbox path is unsafe or absent: $sandbox_root" >&2
  exit 1
}

if mountpoint -q "$sandbox_root/merged"; then
  umount "$sandbox_root/merged"
fi
rm -rf --one-file-system -- "$sandbox_root"
