#!/usr/bin/env bash
# Configure an unshared swapfile on the active instance-store XFS filesystem.
set -euo pipefail

[[ ${EUID} -eq 0 ]] || { echo "setup_nvme_swap.sh must run as root" >&2; exit 1; }
[[ $# -ge 1 && $# -le 2 ]] || {
  echo "usage: sudo $0 <xfs-mount-root> [size, default: 16G]" >&2
  exit 2
}

mount_root=$(readlink -f -- "$1")
size=${2:-16G}
findmnt -n -t xfs --target "$mount_root" >/dev/null
swapfile="$mount_root/caden.swap"

# zram is useful for a compression experiment, but it consumes host DRAM and
# is therefore the wrong tier for the physical-memory-footprint campaign.
while read -r device; do
  [[ $device == /dev/zram* ]] || continue
  swapoff "$device"
done < <(swapon --show=NAME --noheadings 2>/dev/null || true)

if [[ ! -e $swapfile ]]; then
  fallocate -l "$size" "$swapfile"
  chmod 0600 "$swapfile"
  mkswap "$swapfile" >/dev/null
else
  [[ -f $swapfile && ! -L $swapfile ]] || {
    echo "refusing unexpected existing swap path: $swapfile" >&2
    exit 1
  }
  swap_type=$(blkid -p -s TYPE -o value "$swapfile" 2>/dev/null || true)
  [[ $swap_type == swap ]] || {
    echo "refusing existing non-swap file: $swapfile" >&2
    exit 1
  }
  chmod 0600 "$swapfile"
fi

if ! swapon --show=NAME --noheadings | grep -Fxq "$swapfile"; then
  swapon --priority 100 "$swapfile"
fi

swapon --show --bytes
