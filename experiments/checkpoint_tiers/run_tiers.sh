#!/usr/bin/env bash
# Launch one unprivileged bwrap "agent" sandbox holding MIB of memory inside a dedicated cgroup,
# then run the privileged driver to measure the freeze / reclaim tiers and a CRIU dump smoke test.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
CG=/sys/fs/cgroup/caden_agent0
WORK=/tmp/caden-agent0
MIB=${MIB:-2048}
RESULT=$WORK/scan.log
U=$(id -u); G=$(id -g)

cleanup() {
  sudo pkill -f "workload.py $MIB" 2>/dev/null || true
  sleep 0.3
  sudo rmdir "$CG" 2>/dev/null || true
}
trap cleanup EXIT

echo "### prepare workdir + cgroup (MIB=$MIB)"
rm -rf "$WORK"; mkdir -p "$WORK"; cp "$HERE/workload.py" "$WORK/"; : > "$RESULT"
grep -qw memory /sys/fs/cgroup/cgroup.subtree_control || \
  echo +memory | sudo tee /sys/fs/cgroup/cgroup.subtree_control >/dev/null
sudo mkdir -p "$CG"
[ -e "$CG/memory.reclaim" ] || { echo "FATAL: no memory.reclaim in $CG"; exit 1; }

echo "### launch sandbox (root joins cgroup, drops to uid $U, execs unprivileged bwrap)"
BWRAP="bwrap --unshare-user-try --unshare-pid --unshare-ipc --unshare-uts \
 --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib --ro-bind /lib64 /lib64 --ro-bind /etc /etc \
 --tmpfs /tmp --proc /proc --dev /dev --new-session --bind $WORK $WORK --chdir $WORK"
sudo bash -c "echo \$\$ > $CG/cgroup.procs; exec setpriv --reuid=$U --regid=$G --init-groups \
  $BWRAP python3 $WORK/workload.py $MIB $RESULT" >"$WORK/sandbox.out" 2>&1 &

echo "### wait for READY"
for _ in $(seq 1 60); do grep -q READY "$WORK/sandbox.out" && break; sleep 0.5; done
grep -q READY "$WORK/sandbox.out" || { echo "sandbox failed to start:"; cat "$WORK/sandbox.out"; exit 1; }
cat "$WORK/sandbox.out"

WPID=$(ps -eo pid,comm,args | awk '/workload.py/ && $2=="python3"{print $1; exit}')
BPID=$(ps -eo pid,comm,args | awk '/workload.py/ && $2=="bwrap"{print $1; exit}')
echo "workload host pid=$WPID  bwrap host pid=$BPID"
echo "cgroup members: $(cat $CG/cgroup.procs | tr '\n' ' ')"

echo "### measure tiers (freeze/thaw + reclaim)"
sudo python3 "$HERE/measure_tiers.py" "$CG" "$WPID" "$RESULT" | tee "$WORK/tiers.json"

echo "### CRIU dump smoke test (root of tree = bwrap pid $BPID)"
sudo rm -rf /tmp/ckpt; sudo mkdir -p /tmp/ckpt
sudo criu dump --tree "$BPID" --images-dir /tmp/ckpt --shell-job --leave-running \
  >"$WORK/criu-dump.log" 2>&1 && echo "CRIU dump SUCCEEDED" || echo "CRIU dump FAILED (see criu-dump.log)"
echo "--- last 25 lines of criu-dump.log ---"
tail -25 "$WORK/criu-dump.log"
echo "### RUN DONE"
