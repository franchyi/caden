#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 2 ]] || {
  echo "usage: sudo $0 <active-xfs-mount-root> <sandbox-rootfs>" >&2
  exit 2
}
[[ ${EUID} -eq 0 ]] || { echo "install-service.sh must run as root" >&2; exit 1; }

mount_root=$(readlink -f -- "$1")
findmnt -n -t xfs --target "$mount_root" >/dev/null
rootfs=$(readlink -f -- "$2")
[[ -d "$rootfs" ]] || { echo "sandbox rootfs is not a directory: $rootfs" >&2; exit 1; }
[[ -x "$rootfs/usr/local/libexec/sandboxfs/sandboxd" ]] || {
  echo "sandboxd is missing from rootfs: $rootfs/usr/local/libexec/sandboxfs/sandboxd" >&2
  exit 1
}

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
make -C "$repo_root" install
install -d -m 0755 /etc/sandboxfs /var/lib/sandboxfs

config_temp=$(mktemp /etc/sandboxfs/.sandboxfsd.env.XXXXXX)
printf 'SANDBOXFS_ROOT=%s\nSANDBOXFS_ROOTFS=%s\n' "$mount_root" "$rootfs" > "$config_temp"
chmod 0644 "$config_temp"
mv -f -- "$config_temp" /etc/sandboxfs/sandboxfsd.env

systemctl daemon-reload
systemctl enable sandboxfsd.service
systemctl restart sandboxfsd.service

for _ in $(seq 1 100); do
  if sandboxfsctl system >/dev/null 2>&1; then
    sandboxfsctl system
    exit 0
  fi
  sleep 0.05
done

systemctl status --no-pager sandboxfsd.service >&2 || true
exit 1
