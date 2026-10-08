#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: sudo $0 <instance-store-block-device> <t0|t1>" >&2
  exit 2
}

[[ $# -eq 2 ]] || usage
[[ ${EUID} -eq 0 ]] || { echo "prepare-xfs.sh must run as root" >&2; exit 1; }

target_device=$(readlink -f -- "$1")
variant=$2

case "$variant" in
  t0)
    reflink=0
    mount_root=/agent-xfs-t0
    ;;
  t1)
    reflink=1
    mount_root=/agent-xfs-t1
    ;;
  *) usage ;;
esac

[[ -b "$target_device" ]] || {
  echo "not a block device: $target_device" >&2
  exit 1
}

target_type=$(lsblk -dno TYPE "$target_device" | xargs)
# Do not use lsblk --raw for human-readable identity fields: util-linux 2.41
# escapes spaces as `x20`, which would make the exact EC2 model guard fail.
target_model=$(lsblk -dno MODEL "$target_device" | xargs)
target_serial=$(lsblk -dno SERIAL "$target_device" | xargs)
[[ "$target_type" == disk ]] || {
  echo "refusing non-disk device: $target_device ($target_type)" >&2
  exit 1
}
[[ "$target_model" == *"Amazon EC2 NVMe Instance Storage"* ]] || {
  echo "refusing device with unexpected model: $target_model" >&2
  exit 1
}

root_source=$(findmnt -nro SOURCE /)
root_parent=$(lsblk -nro PKNAME "$root_source" | head -n1)
if [[ -n "$root_parent" && "$target_device" == "/dev/$root_parent" ]]; then
  echo "refusing to format root disk: $target_device" >&2
  exit 1
fi

if lsblk -nrpo MOUNTPOINT "$target_device" | awk 'NF { found=1 } END { exit !found }'; then
  echo "refusing mounted device: $target_device" >&2
  lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS "$target_device" >&2
  exit 1
fi

echo "Formatting validated instance-store device:"
echo "  device:  $target_device"
echo "  model:   $target_model"
echo "  serial:  $target_serial"
echo "  reflink: $reflink"
echo "  mount:   $mount_root"

mkfs.xfs -f -m "reflink=$reflink" "$target_device"
mkdir -p "$mount_root"
mount "$target_device" "$mount_root"
mkdir -p "$mount_root"/{bases,sandboxes,measurements}

workspace_owner=${SUDO_USER:-ubuntu}
if id "$workspace_owner" >/dev/null 2>&1; then
  chown "$workspace_owner:$workspace_owner" \
    "$mount_root/bases" "$mount_root/sandboxes" "$mount_root/measurements"
fi

xfs_info "$mount_root"
