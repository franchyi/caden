#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 1 ]] || {
  echo "usage: sudo $0 <durable-output.json>" >&2
  exit 2
}

output_path=$1
system_json=$(sandboxfsctl system)
mount_root=$(jq -r .root <<<"$system_json")
mount_device=$(findmnt -nro SOURCE --target "$mount_root")

metadata_token=$(curl -fsS -X PUT \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
  http://169.254.169.254/latest/api/token)
metadata_get() {
  curl -fsS \
    -H "X-aws-ec2-metadata-token: $metadata_token" \
    "http://169.254.169.254/latest/meta-data/$1"
}

instance_id=$(metadata_get instance-id)
ami_id=$(metadata_get ami-id)
instance_type=$(metadata_get instance-type)
availability_zone=$(metadata_get placement/availability-zone)
kernel=$(uname -r)
ubuntu=$(source /etc/os-release && printf '%s' "$PRETTY_NAME")
go_version=$(go version)
bubblewrap_version=$(bwrap --version)
xfsprogs_version=$(mkfs.xfs -V 2>&1)
cpu_model=$(lscpu | awk -F: '/Model name/ {sub(/^[[:space:]]+/, "", $2); print $2; exit}')
microcode=$(grep -m1 '^microcode' /proc/cpuinfo | awk -F: '{sub(/^[[:space:]]+/, "", $2); print $2}')
nvme_model=$(lsblk -dno MODEL "$mount_device" | xargs)
nvme_serial=$(lsblk -dno SERIAL "$mount_device" | xargs)
nvme_firmware=$(nvme id-ctrl -o json "$mount_device" | jq -r .fr)
metadata_capture_commit=$(git -C /home/ubuntu/sandboxfs rev-parse HEAD)
benchmark_commit=${SANDBOXFS_BENCHMARK_COMMIT:-$metadata_capture_commit}
git -C /home/ubuntu/sandboxfs cat-file -e "$benchmark_commit^{commit}"
benchmark_commit=$(git -C /home/ubuntu/sandboxfs rev-parse "$benchmark_commit^{commit}")
captured_at=$(date --iso-8601=ns)

mkdir -p -- "$(dirname -- "$output_path")"
jq -n \
  --arg captured_at "$captured_at" \
  --arg instance_id "$instance_id" \
  --arg ami_id "$ami_id" \
  --arg instance_type "$instance_type" \
  --arg availability_zone "$availability_zone" \
  --arg ubuntu "$ubuntu" \
  --arg kernel "$kernel" \
  --arg go_version "$go_version" \
  --arg bubblewrap_version "$bubblewrap_version" \
  --arg xfsprogs_version "$xfsprogs_version" \
  --arg cpu_model "$cpu_model" \
  --arg microcode "$microcode" \
  --arg mount_root "$mount_root" \
  --arg mount_device "$mount_device" \
  --arg nvme_model "$nvme_model" \
  --arg nvme_serial "$nvme_serial" \
  --arg nvme_firmware "$nvme_firmware" \
  --arg benchmark_commit "$benchmark_commit" \
  --arg metadata_capture_commit "$metadata_capture_commit" \
  '{captured_at:$captured_at,instance_id:$instance_id,ami_id:$ami_id,instance_type:$instance_type,availability_zone:$availability_zone,ubuntu:$ubuntu,kernel:$kernel,go_version:$go_version,bubblewrap_version:$bubblewrap_version,xfsprogs_version:$xfsprogs_version,cpu_model:$cpu_model,microcode:$microcode,mount_root:$mount_root,mount_device:$mount_device,nvme_model:$nvme_model,nvme_serial:$nvme_serial,nvme_firmware:$nvme_firmware,benchmark_commit:$benchmark_commit,metadata_capture_commit:$metadata_capture_commit}' \
  > "$output_path"

echo "Host metadata written to $output_path"
