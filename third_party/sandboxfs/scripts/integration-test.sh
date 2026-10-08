#!/usr/bin/env bash
set -euo pipefail

[[ ${EUID} -eq 0 ]] || { echo "integration-test.sh must run as root" >&2; exit 1; }

system_json=$(sandboxfsctl system)
mount_root=$(jq -r .root <<<"$system_json")
reflink=$(jq -r .reflink_enabled <<<"$system_json")
if [[ "$reflink" == true ]]; then
  overlay_mode=t1
else
  overlay_mode=t0
fi

run_id="integration-$$"
base_name="integration-base"
base_path="$mount_root/bases/$base_name"
overlay_a="$run_id-overlay-a"
overlay_b="$run_id-overlay-b"
baseline="$run_id-baseline"

cleanup() {
  for sandbox_id in "$overlay_a" "$overlay_b" "$baseline"; do
    sandboxfsctl destroy "$sandbox_id" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

if [[ ! -d "$base_path" ]]; then
  runuser --user ubuntu -- sandboxfscorpus \
    --root "$base_path" \
    --small-files 100 \
    --small-bytes 256 \
    --large-mib 1,4
fi
sandboxfsctl base-register "$base_name" "$base_path" >/dev/null

sandboxfsctl create --id "$overlay_a" --base "$base_name" --mode "$overlay_mode" >/dev/null
find /sys/fs/cgroup -type d -path "*/sandboxfs/$overlay_a" -print -quit | grep -q .
sandboxfsctl exec "$overlay_a" -- sh -lc '
  grep -qx immutable-base repository/sentinel.txt
  printf "private-overlay\n" > repository/sentinel.txt
  printf "new-file\n" > repository/agent.txt
  ln repository/agent.txt repository/agent-hardlink.txt
  ln -s agent.txt repository/agent-symlink.txt
  mv repository/agent-hardlink.txt repository/agent-renamed.txt
  rm repository/dependencies/d0000/file-000000.dat
  test "$(cat repository/agent-renamed.txt)" = new-file
  test "$(readlink repository/agent-symlink.txt)" = agent.txt
'
grep -qx immutable-base "$base_path/repository/sentinel.txt"
test ! -e "$base_path/repository/agent.txt"
test -e "$base_path/repository/dependencies/d0000/file-000000.dat"

sandboxfsctl create --id "$overlay_b" --base "$base_name" --mode "$overlay_mode" >/dev/null
sandboxfsctl exec "$overlay_b" -- sh -lc '
  grep -qx immutable-base repository/sentinel.txt
  test ! -e repository/agent.txt
  test -e repository/dependencies/d0000/file-000000.dat
'

sandboxfsctl create --id "$baseline" --base "$base_name" --mode baseline >/dev/null
sandboxfsctl exec "$baseline" -- sh -lc '
  grep -qx immutable-base repository/sentinel.txt
  printf "private-baseline\n" > repository/sentinel.txt
  printf "baseline-new\n" > repository/baseline.txt
'
grep -qx immutable-base "$base_path/repository/sentinel.txt"
test ! -e "$base_path/repository/baseline.txt"

sandboxfsctl destroy "$overlay_a" >/dev/null
sandboxfsctl destroy "$overlay_b" >/dev/null
sandboxfsctl destroy "$baseline" >/dev/null

test -z "$(find /sys/fs/cgroup -type d -path "*/sandboxfs/$overlay_a" -print -quit)"
test -z "$(find /sys/fs/cgroup -type d -path "*/sandboxfs/$overlay_b" -print -quit)"
test -z "$(find /sys/fs/cgroup -type d -path "*/sandboxfs/$baseline" -print -quit)"
if findmnt -rn -t overlay -o TARGET | grep -q "^$mount_root/sandboxes/"; then
  echo "leaked OverlayFS mount" >&2
  exit 1
fi
test -z "$(find "$mount_root/sandboxes" -mindepth 1 -maxdepth 1 -print -quit)"
sandboxfsctl base-verify "$base_name" | jq -e .match >/dev/null

echo "Integration test passed for baseline and $overlay_mode."
