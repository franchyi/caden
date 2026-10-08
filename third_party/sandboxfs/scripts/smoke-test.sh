#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 1 ]] || {
  echo "usage: $0 <xfs-mount-root>" >&2
  exit 2
}

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mount_root=$(readlink -f -- "$1")
test_id="smoke-$$"
base_dir="$mount_root/bases/$test_id"
sandbox_root="$mount_root/sandboxes/$test_id"

cleanup() {
  if [[ -d "$sandbox_root" ]]; then
    sudo "$repo_root/scripts/destroy-workspace.sh" "$mount_root" "$test_id" || true
  fi
  sudo chmod -R u+w "$base_dir" 2>/dev/null || true
  sudo rm -rf --one-file-system -- "$base_dir"
}
trap cleanup EXIT

mkdir -p "$base_dir"
printf 'immutable-base\n' > "$base_dir/sentinel.txt"

sudo "$repo_root/scripts/create-workspace.sh" "$mount_root" "$base_dir" "$test_id" >/dev/null
"$repo_root/scripts/exec-workspace.sh" "$mount_root" "$test_id" \
  sh -lc 'grep -qx immutable-base sentinel.txt; printf "private-write\n" > sentinel.txt; printf "new\n" > agent.txt'

grep -qx immutable-base "$base_dir/sentinel.txt"
grep -qx private-write "$sandbox_root/upper/sentinel.txt"
grep -qx new "$sandbox_root/upper/agent.txt"

echo "Smoke test passed: private writes did not mutate the prepared base."
