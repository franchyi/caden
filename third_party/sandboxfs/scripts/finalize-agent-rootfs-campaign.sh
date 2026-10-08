#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: sudo $0 <result-directory> <agent-rootfs>" >&2
  exit 2
fi
[[ ${EUID} -eq 0 ]] || { echo "must run as root" >&2; exit 1; }

out=$(readlink -m -- "$1")
rootfs=$(readlink -f -- "$2")
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
rootfs_name=$(basename -- "$rootfs")

[[ -d "$out" ]] || { echo "result directory is missing: $out" >&2; exit 1; }
[[ -d "$rootfs" ]] || { echo "rootfs is missing: $rootfs" >&2; exit 1; }
[[ -f "${rootfs}.packages.tsv" ]] || { echo "package manifest is missing" >&2; exit 1; }

cp -- "${rootfs}.packages.tsv" "$out/rootfs-packages.tsv"
du -sb -- "$rootfs" > "$out/rootfs-size.txt"
find "$rootfs" -xdev -type f -print0 \
  | sort -z \
  | xargs -0 sha256sum \
  | sha256sum > "$out/rootfs-files.sha256"

package_manifest_sha=$(sha256sum "$out/rootfs-packages.tsv" | cut -d' ' -f1)
file_tree_sha=$(cut -d' ' -f1 "$out/rootfs-files.sha256")
sandboxd_sha=$(sha256sum "$rootfs/usr/local/libexec/sandboxfs/sandboxd" | cut -d' ' -f1)
size_bytes=$(cut -f1 "$out/rootfs-size.txt")
entries=$(find "$rootfs" -xdev | wc -l)
packages=$(wc -l < "$out/rootfs-packages.tsv")
commit=$(git -C "$repo_root" rev-parse HEAD)
git -C "$repo_root" diff --binary | sha256sum > "$out/benchmark-working-tree-diff.sha256"

jq -n \
  --arg name "$rootfs_name" \
  --arg path "$rootfs" \
  --arg release "Ubuntu 26.04 LTS" \
  --arg suite resolute \
  --arg arch amd64 \
  --arg source http://ap-southeast-1.ec2.archive.ubuntu.com/ubuntu \
  --arg build_tool "debootstrap --variant=minbase plus ubuntu-standard and pinned agent tool package set" \
  --arg file_tree_sha256 "$file_tree_sha" \
  --arg package_manifest_sha256 "$package_manifest_sha" \
  --arg sandboxd_sha256 "$sandboxd_sha" \
  --arg sandboxfs_base_commit "$commit" \
  --argjson size_bytes "$size_bytes" \
  --argjson entries "$entries" \
  --argjson packages "$packages" \
  '{name:$name,path:$path,release:$release,suite:$suite,architecture:$arch,source:$source,build_tool:$build_tool,file_tree_sha256:$file_tree_sha256,size_bytes:$size_bytes,entries:$entries,packages:$packages,package_manifest_sha256:$package_manifest_sha256,sandboxd_sha256:$sandboxd_sha256,sandboxfs_base_commit:$sandboxfs_base_commit,mount_policy:"one read-only bind shared by every sandbox; private workspace at /workspace"}' \
  > "$out/rootfs-metadata.json"

cp -- /etc/sandboxfs/sandboxfsd.env "$out/sandboxfsd.env"
systemctl show sandboxfsd.service -p ExecStart -p MainPID -p ActiveState \
  > "$out/sandboxfsd-service.txt"
swapon --show --bytes --output NAME,TYPE,SIZE,USED,PRIO \
  > "$out/swap.txt"

probe_id="rootfs-final-probe-$$"
cleanup_probe() {
  sandboxfsctl destroy "$probe_id" >/dev/null 2>&1 || true
}
trap cleanup_probe EXIT
sandboxfsctl create --id "$probe_id" --base bench-medium --mode t1 \
  > "$out/rootfs-probe-create.json"
sandboxfsctl exec-json "$probe_id" -- cat /etc/os-release \
  > "$out/rootfs-probe-release.json"
sandboxfsctl exec-json "$probe_id" -- python3 -c \
  'import json, pathlib; print(json.dumps({"tool":"ready","path":str(pathlib.Path.cwd())}))' \
  > "$out/rootfs-probe-python.json"
sandboxfsctl exec "$probe_id" -- sh -lc \
  'for tool in python3 node npm go rustc cargo javac clang cmake ninja git rg jq; do command -v "$tool" || exit 1; done' \
  > "$out/rootfs-probe-tools.txt"
sandboxfsctl destroy "$probe_id" > "$out/rootfs-probe-destroy.json"
trap - EXIT

sandboxfsctl list | jq -e '.sandboxes | length == 0' >/dev/null
if findmnt -rn -t overlay -o TARGET | grep -q '^/agent-xfs-t1/sandboxes/'; then
  echo "leaked OverlayFS mount" >&2
  exit 1
fi

(
  cd "$out"
  find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%f\0' \
    | sort -z \
    | xargs -0 sha256sum \
    > SHA256SUMS
)

owner=${SUDO_USER:-ubuntu}
if id "$owner" >/dev/null 2>&1; then
  chown -R "$owner:$owner" "$out"
fi
echo "Finalized campaign artifacts in $out"
