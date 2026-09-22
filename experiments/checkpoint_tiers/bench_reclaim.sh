#!/usr/bin/env bash
# Independent reclaim-backend comparison: measure memory.reclaim Park (eviction) and Resume (refault)
# for an MIB workload, varying the swap backend (zram-compressed vs an uncompressed disk swapfile) and
# the data compressibility (zero vs rand). Usage: bash bench_reclaim.sh <zram|disk> <zero|rand>
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
BACKEND=${1:-zram}; FILL=${2:-zero}; MIB=${MIB:-2048}
CG=/sys/fs/cgroup/caden_reclaim
WORK=/tmp/caden-reclaim; RESULT=$WORK/scan.log
SWAPFILE=/swapfile.caden

cleanup() {
  sudo pkill -9 -f "workload.py $MIB" 2>/dev/null || true
  sleep 0.3
  sudo rmdir "$CG" 2>/dev/null || true
  sudo swapoff "$SWAPFILE" 2>/dev/null || true; sudo rm -f "$SWAPFILE" 2>/dev/null || true
  sudo swapoff /dev/zram0 2>/dev/null || true
}
trap cleanup EXIT

echo "### backend=$BACKEND fill=$FILL MIB=$MIB"
sudo swapoff -a 2>/dev/null || true
if [ "$BACKEND" = zram ]; then
  sudo modprobe zram num_devices=1 2>/dev/null || true
  sudo swapoff /dev/zram0 2>/dev/null || true
  echo 1 | sudo tee /sys/block/zram0/reset >/dev/null 2>&1 || true
  echo lz4 | sudo tee /sys/block/zram0/comp_algorithm >/dev/null
  echo 8G  | sudo tee /sys/block/zram0/disksize >/dev/null
  sudo mkswap /dev/zram0 >/dev/null
  sudo swapon --priority 100 /dev/zram0
else
  sudo rm -f "$SWAPFILE"
  sudo fallocate -l 8G "$SWAPFILE" 2>/dev/null || sudo dd if=/dev/zero of="$SWAPFILE" bs=1M count=8192 status=none
  sudo chmod 600 "$SWAPFILE"; sudo mkswap "$SWAPFILE" >/dev/null
  sudo swapon --priority 100 "$SWAPFILE"
fi
swapon --show

rm -rf "$WORK"; mkdir -p "$WORK"; : > "$RESULT"; cp "$HERE/workload.py" "$WORK/"
grep -qw memory /sys/fs/cgroup/cgroup.subtree_control || echo +memory | sudo tee /sys/fs/cgroup/cgroup.subtree_control >/dev/null
sudo mkdir -p "$CG"
# Put the shell in the cgroup BEFORE exec so every faulted page is charged here.
sudo bash -c "echo \$\$ > $CG/cgroup.procs; exec python3 $WORK/workload.py $MIB $RESULT $FILL" >"$WORK/out" 2>&1 &
for _ in $(seq 1 240); do grep -q READY "$WORK/out" && break; sleep 0.5; done
grep -q READY "$WORK/out" || { echo "workload failed:"; cat "$WORK/out"; exit 1; }
WPID=$(pgrep -f "workload.py $MIB" | head -1)
echo "workload pid=$WPID  memory.current=$(cat $CG/memory.current)"

sudo python3 "$HERE/measure_tiers.py" "$CG" "$WPID" "$RESULT"
if [ "$BACKEND" = zram ]; then echo "--- zramctl (actual RAM used by zram) ---"; zramctl; fi
echo "### DONE $BACKEND/$FILL"
