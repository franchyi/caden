#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 2 ]] || {
  echo "usage: sudo $0 <xfs-mount-root> <expected-reflink:0|1>" >&2
  exit 2
}
[[ ${EUID} -eq 0 ]] || { echo "preflight.sh must run as root" >&2; exit 1; }

mount_root=$(readlink -f -- "$1")
expected_reflink=$2
[[ "$expected_reflink" =~ ^[01]$ ]] || { echo "expected reflink must be 0 or 1" >&2; exit 2; }

findmnt -n -t xfs --target "$mount_root" >/dev/null
xfs_info_output=$(xfs_info "$mount_root")
grep -Eq "reflink[=:]$expected_reflink" <<<"$xfs_info_output" || {
  echo "XFS reflink setting does not match expected value $expected_reflink" >&2
  echo "$xfs_info_output" >&2
  exit 1
}

probe_dir=$(mktemp -d "$mount_root/measurements/preflight.XXXXXX")
overlay_mounted=0
cleanup() {
  if [[ $overlay_mounted -eq 1 ]]; then
    umount "$probe_dir/merged"
  fi
  rm -rf -- "$probe_dir"
}
trap cleanup EXIT

mkdir -p "$probe_dir"/{lower,upper,work,merged}
dd if=/dev/zero of="$probe_dir/lower/large.bin" bs=1M count=8 status=none
printf 'base\n' > "$probe_dir/lower/sentinel.txt"

if [[ "$expected_reflink" == 1 ]]; then
  cp --reflink=always "$probe_dir/lower/large.bin" "$probe_dir/reflink-clone.bin"
else
  if cp --reflink=always "$probe_dir/lower/large.bin" "$probe_dir/should-not-clone.bin" 2>/dev/null; then
    echo "unexpected reflink success on reflink=0 XFS" >&2
    exit 1
  fi
fi

mount -t overlay overlay \
  -o "lowerdir=$probe_dir/lower,upperdir=$probe_dir/upper,workdir=$probe_dir/work" \
  "$probe_dir/merged"
overlay_mounted=1
printf 'private\n' > "$probe_dir/merged/sentinel.txt"
grep -qx base "$probe_dir/lower/sentinel.txt"
grep -qx private "$probe_dir/upper/sentinel.txt"

bwrap \
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
  --bind "$probe_dir/merged" /workspace \
  --chdir /workspace \
  -- sh -lc 'test "$(cat sentinel.txt)" = private'

echo "Preflight passed for $mount_root with reflink=$expected_reflink."
