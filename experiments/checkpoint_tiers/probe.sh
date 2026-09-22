#!/usr/bin/env bash
# Read-only environment probe: discover which checkpoint/swap tiers nsl7s actually supports
# (cgroup version + freeze/reclaim interfaces, swap backend, criu, privilege).
set -uo pipefail

line() { printf '\n### %s\n' "$1"; }

line "kernel"; uname -r
line "cgroup fs type at /sys/fs/cgroup"; stat -fc '%T' /sys/fs/cgroup
line "/sys/fs/cgroup top entries"; ls -1 /sys/fs/cgroup | head -60
line "my own cgroup (/proc/self/cgroup)"; cat /proc/self/cgroup

line "v2 unified controllers"; cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null || echo "(no unified cgroup.controllers -> not pure v2)"
line "v1 freezer hierarchy"; ls -d /sys/fs/cgroup/freezer 2>/dev/null && ls /sys/fs/cgroup/freezer | head || echo "(no v1 freezer)"
line "v1 memory hierarchy"; ls -d /sys/fs/cgroup/memory 2>/dev/null && ls /sys/fs/cgroup/memory | grep -E 'limit_in_bytes|usage_in_bytes|force_empty|soft_limit' || echo "(no v1 memory)"
line "v2 memory.reclaim (needs >=5.19)"; ls /sys/fs/cgroup/memory.reclaim 2>/dev/null || echo "(no top-level memory.reclaim)"

line "swap backends"; swapon --show 2>/dev/null || true; echo "--- /proc/swaps ---"; cat /proc/swaps
line "zram"; (ls /sys/block/ | grep -i zram) || echo "(no zram block dev)"; command -v zramctl >/dev/null && zramctl 2>/dev/null || echo "(no zramctl)"
line "zswap enabled?"; cat /sys/module/zswap/parameters/enabled 2>/dev/null || echo "(no zswap module)"
line "meminfo"; head -5 /proc/meminfo

line "criu"; (command -v criu && criu --version 2>&1 | head -3) || echo "(criu not installed)"
line "passwordless sudo?"; (sudo -n true 2>/dev/null && echo PASSWORDLESS_SUDO_OK) || echo "(no passwordless sudo)"
line "systemd --user"; systemctl --user is-system-running 2>&1 | head -1; (systemd-run --user --version 2>/dev/null | head -1) || echo "(no systemd-run --user)"
line "bwrap"; (command -v bwrap && bwrap --version) || echo "(no bwrap)"
line "python3"; (command -v python3 && python3 --version) || echo "(no python3)"

line "delegated-cgroup writability test"
mycg=$(awk -F: '/^0::/{print $3}' /proc/self/cgroup 2>/dev/null)
echo "my v2 (unified) cgroup path: ${mycg:-none}"
if [ -n "${mycg:-}" ] && [ -d "/sys/fs/cgroup${mycg}" ]; then
  t="/sys/fs/cgroup${mycg}/caden_probe_test.$$"
  if mkdir "$t" 2>/dev/null; then
    echo "CAN create a child v2 cgroup under my delegated subtree"
    echo "child controllers: $(cat "$t/cgroup.controllers" 2>/dev/null)"
    echo "freeze file: $([ -e "$t/cgroup.freeze" ] && echo yes || echo no) | memory.high: $([ -e "$t/memory.high" ] && echo yes || echo no) | memory.reclaim: $([ -e "$t/memory.reclaim" ] && echo yes || echo no)"
    rmdir "$t" 2>/dev/null
  else
    echo "CANNOT create child v2 cgroup here (no delegation)"
  fi
fi
echo
echo "### PROBE DONE"
