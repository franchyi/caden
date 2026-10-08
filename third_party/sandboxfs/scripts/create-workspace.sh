#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 3 ]] || {
  echo "usage: sudo $0 <xfs-mount-root> <base-directory> <sandbox-id>" >&2
  exit 2
}
[[ ${EUID} -eq 0 ]] || { echo "create-workspace.sh must run as root" >&2; exit 1; }

mount_root=$(readlink -f -- "$1")
base_dir=$(readlink -f -- "$2")
sandbox_id=$3

[[ "$sandbox_id" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]] || {
  echo "invalid sandbox ID" >&2
  exit 1
}
[[ -d "$base_dir" ]] || { echo "base does not exist: $base_dir" >&2; exit 1; }
findmnt -n -t xfs --target "$mount_root" >/dev/null

mount_device=$(findmnt -nro SOURCE --target "$mount_root")
base_device=$(findmnt -nro SOURCE --target "$base_dir")
[[ "$mount_device" == "$base_device" ]] || {
  echo "base and sandbox directories must be on the same XFS filesystem" >&2
  exit 1
}

sandbox_root="$mount_root/sandboxes/$sandbox_id"
[[ ! -e "$sandbox_root" ]] || {
  echo "sandbox already exists: $sandbox_id" >&2
  exit 1
}

mkdir -p "$sandbox_root"/{upper,work,merged,control}
workspace_owner=${SUDO_USER:-ubuntu}
if id "$workspace_owner" >/dev/null 2>&1; then
  chown "$workspace_owner:$workspace_owner" \
    "$sandbox_root/upper" "$sandbox_root/control"
fi

mount -t overlay overlay \
  -o "lowerdir=$base_dir,upperdir=$sandbox_root/upper,workdir=$sandbox_root/work" \
  "$sandbox_root/merged"

printf '%s\n' "$base_dir" > "$sandbox_root/base"
printf '%s\n' "$sandbox_root/merged"
