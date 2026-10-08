#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "bootstrap-host.sh must run as root" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  acl \
  attr \
  bubblewrap \
  fio \
  git \
  golang-go \
  jq \
  make \
  nvme-cli \
  util-linux \
  xfsprogs

install -d -m 0755 /workspace
install -d -m 0755 /etc/sandboxfs /var/lib/sandboxfs

echo "Host dependencies installed."
