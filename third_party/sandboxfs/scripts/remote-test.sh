#!/usr/bin/env bash
set -euo pipefail

: "${SANDBOXFS_HOST:?set SANDBOXFS_HOST to the EC2 public IP or DNS name}"

ssh_args=(
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
)
if [[ -n ${SANDBOXFS_SSH_KEY:-} ]]; then
  ssh_args+=(-i "$SANDBOXFS_SSH_KEY")
fi

ssh "${ssh_args[@]}" "ubuntu@$SANDBOXFS_HOST" \
  'git -C /home/ubuntu/sandboxfs pull --ff-only &&
   make -C /home/ubuntu/sandboxfs test &&
   sudo /home/ubuntu/sandboxfs/scripts/install-service.sh /agent-xfs-t1 >/dev/null &&
   sudo /home/ubuntu/sandboxfs/scripts/preflight.sh /agent-xfs-t1 1 &&
   sudo /home/ubuntu/sandboxfs/scripts/integration-test.sh'
