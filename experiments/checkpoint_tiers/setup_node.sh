#!/usr/bin/env bash
# One-time setup for the checkpoint/swap/restore prototype on Ubuntu 26.04: install bubblewrap +
# criu, configure a zram swap device so anon reclaim has a fast backend, and confirm the cgroup v2
# levers exist. Idempotent; safe to re-run.
set -euo pipefail

echo "### kernel: $(uname -r)   cgroupfs: $(stat -fc '%T' /sys/fs/cgroup)"

echo "### installing bubblewrap, criu, jq"
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq bubblewrap criu jq >/dev/null
echo "bwrap: $(bwrap --version)   criu: $(criu --version | head -1)   python3: $(python3 --version)"

echo "### unprivileged user namespace check (bwrap needs it)"
BW_TEST=(bwrap --unshare-user-try --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib \
  --ro-bind /lib64 /lib64 --proc /proc --dev /dev --tmpfs /tmp /usr/bin/true)
if ! "${BW_TEST[@]}" 2>/dev/null; then
  echo "bwrap blocked; relaxing AppArmor unprivileged-userns restriction (throwaway box)"
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 2>/dev/null || true
fi
"${BW_TEST[@]}" && echo "bwrap OK"

echo "### zram swap (fast reclaim backend)"
if ! swapon --show=NAME --noheadings 2>/dev/null | grep -q zram; then
  sudo modprobe zram num_devices=1
  echo lz4 | sudo tee /sys/block/zram0/comp_algorithm >/dev/null || true
  echo 8G | sudo tee /sys/block/zram0/disksize >/dev/null
  sudo mkswap /dev/zram0 >/dev/null
  sudo swapon --priority 100 /dev/zram0
fi
swapon --show; zramctl 2>/dev/null || true

echo "### cgroup v2 levers"
grep -qw memory /sys/fs/cgroup/cgroup.subtree_control || \
  echo +memory | sudo tee /sys/fs/cgroup/cgroup.subtree_control >/dev/null
echo "root subtree_control: $(cat /sys/fs/cgroup/cgroup.subtree_control)"
probe=/sys/fs/cgroup/caden_setupprobe
sudo mkdir -p "$probe"
echo "  cgroup.freeze:   $([ -e $probe/cgroup.freeze ] && echo present || echo MISSING)"
echo "  memory.reclaim:  $([ -e $probe/memory.reclaim ] && echo present || echo MISSING)"
echo "  memory.swap.current: $([ -e $probe/memory.swap.current ] && echo present || echo MISSING)"
sudo rmdir "$probe"
echo "### SETUP DONE"
