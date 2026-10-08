#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: sudo $0 <new-rootfs-directory> <sandboxd-binary> [ubuntu-mirror]" >&2
  exit 2
fi

rootfs=$(readlink -m -- "$1")
sandboxd=$(readlink -f -- "$2")
mirror=${3:-http://ap-southeast-1.ec2.archive.ubuntu.com/ubuntu}

[[ $EUID -eq 0 ]] || { echo "must run as root" >&2; exit 1; }
[[ -x "$sandboxd" ]] || { echo "sandboxd binary is not executable: $sandboxd" >&2; exit 1; }
[[ "$rootfs" == /opt/sandboxfs/rootfs/* ]] || {
  echo "rootfs must be a new child of /opt/sandboxfs/rootfs: $rootfs" >&2
  exit 1
}
if [[ -e "$rootfs" ]]; then
  echo "refusing existing rootfs path: $rootfs" >&2
  exit 1
fi

packages=(
  ubuntu-standard
  python3 python3-dev python3-pip python3-venv
  git ripgrep curl wget jq patch unzip zip openssh-client
  build-essential clang cmake ninja-build gdb
  nodejs npm
  golang-go
  rustc cargo
  openjdk-21-jdk-headless
)

mounted=()
cleanup() {
  local index
  for ((index=${#mounted[@]}-1; index>=0; index--)); do
    umount -R -- "${mounted[$index]}" 2>/dev/null || true
  done
  mounted=()
}
trap cleanup EXIT

install -d -m 0755 "$(dirname "$rootfs")"
debootstrap \
  --arch=amd64 \
  --variant=minbase \
  --components=main,universe \
  resolute "$rootfs" "$mirror"

install -m 0755 /dev/null "$rootfs/usr/sbin/policy-rc.d"
printf '%s\n' '#!/bin/sh' 'exit 101' > "$rootfs/usr/sbin/policy-rc.d"

mount --rbind /dev "$rootfs/dev"
mount --make-rslave "$rootfs/dev"
mounted+=("$rootfs/dev")
mount -t proc proc "$rootfs/proc"
mounted+=("$rootfs/proc")
mount -t sysfs sysfs "$rootfs/sys"
mounted+=("$rootfs/sys")
cp -L -- /etc/resolv.conf "$rootfs/etc/resolv.conf"

chroot "$rootfs" /usr/bin/env DEBIAN_FRONTEND=noninteractive \
  apt-get update
chroot "$rootfs" /usr/bin/env DEBIAN_FRONTEND=noninteractive \
  apt-get install -y "${packages[@]}"
chroot "$rootfs" /usr/bin/env DEBIAN_FRONTEND=noninteractive \
  apt-get clean

install -d -m 0755 "$rootfs/usr/local/libexec/sandboxfs"
install -m 0755 "$sandboxd" "$rootfs/usr/local/libexec/sandboxfs/sandboxd"
# Bubblewrap cannot create this bind target after the rootfs is mounted
# read-only, so make it part of the immutable image.
install -d -m 0755 "$rootfs/workspace"

find "$rootfs/var/cache/apt/archives" -mindepth 1 -maxdepth 1 -type f -delete
find "$rootfs/var/lib/apt/lists" -mindepth 1 -delete
rm -f -- "$rootfs/usr/sbin/policy-rc.d"

# Exclude bind-mounted host pseudo-filesystems from the immutable artifact and
# its byte count. The EXIT trap remains as a fallback for earlier failures.
cleanup
chroot "$rootfs" dpkg-query -W -f='${binary:Package}\t${Version}\n' \
  > "${rootfs}.packages.tsv"
du -sb -- "$rootfs" > "${rootfs}.size.txt"

echo "Built agent rootfs: $rootfs"
echo "Packages: $(wc -l < "${rootfs}.packages.tsv")"
du -sh -- "$rootfs"
