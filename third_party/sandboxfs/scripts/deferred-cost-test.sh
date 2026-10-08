#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 2 ]] || {
  echo "usage: sudo $0 <registered-base-name> <durable-output.json>" >&2
  exit 2
}
[[ ${EUID} -eq 0 ]] || { echo "deferred-cost-test.sh must run as root" >&2; exit 1; }

base_name=$1
output_path=$2
system_json=$(sandboxfsctl system)
mount_root=$(jq -r .root <<<"$system_json")
reflink=$(jq -r .reflink_enabled <<<"$system_json")
if [[ "$reflink" == true ]]; then
  overlay_mode=t1
else
  overlay_mode=t0
fi

mount_device=$(findmnt -nro SOURCE --target "$mount_root")
block_name=$(lsblk -nro PKNAME "$mount_device" | head -n1)
if [[ -z "$block_name" ]]; then
  block_name=$(basename "$mount_device")
fi
device_stat="/sys/block/$block_name/stat"
[[ -r "$device_stat" ]] || { echo "cannot read device stats: $device_stat" >&2; exit 1; }

result_temp=$(mktemp)
active_id=
cleanup() {
  if [[ -n "$active_id" ]]; then
    sandboxfsctl destroy "$active_id" >/dev/null 2>&1 || true
  fi
  rm -f -- "$result_temp"
}
trap cleanup EXIT

written_sectors() {
  awk '{print $7}' "$device_stat"
}

record_operation() {
  local mode=$1
  local operation=$2
  local host_target=$3
  shift 3

  local sectors_before sectors_after response duration_ns allocated_bytes
  sectors_before=$(written_sectors)
  response=$(sandboxfsctl exec-json "$active_id" -- "$@")
  sectors_after=$(written_sectors)
  duration_ns=$(jq -r .duration_ns <<<"$response")
  allocated_bytes=0
  if [[ "$host_target" != - && -e "$host_target" ]]; then
    allocated_bytes=$(( $(stat -c %b "$host_target") * 512 ))
  fi
  jq -nc \
    --arg mode "$mode" \
    --arg operation "$operation" \
    --arg target "$host_target" \
    --argjson duration_ns "$duration_ns" \
    --argjson device_write_bytes "$(( (sectors_after - sectors_before) * 512 ))" \
    --argjson allocated_bytes "$allocated_bytes" \
    '{mode:$mode,operation:$operation,target:$target,duration_ns:$duration_ns,device_write_bytes:$device_write_bytes,allocated_bytes:$allocated_bytes}' \
    >> "$result_temp"
}

run_mode() {
  local mode=$1
  active_id="deferred-$mode-$$"
  local state_json sandbox_root data_root
  state_json=$(sandboxfsctl create --id "$active_id" --base "$base_name" --mode "$mode")
  sandbox_root=$(jq -r .root <<<"$state_json")
  # Keep delayed baseline-copy writeback out of the first deferred operation.
  # This setup flush is intentionally outside every measured interval.
  sync -f "$sandbox_root"
  if [[ "$mode" == baseline ]]; then
    data_root="$sandbox_root/workspace"
  else
    data_root="$sandbox_root/upper"
  fi

  record_operation "$mode" read_unchanged - \
    sh -lc 'head -c 4096 repository/sentinel.txt >/dev/null'
  record_operation "$mode" create_small "$data_root/repository/new-small.txt" \
    sh -lc 'printf "new-small\n" > repository/new-small.txt && sync -f repository/new-small.txt'
  record_operation "$mode" partial_write_1m "$data_root/repository/large/large-1m.bin" \
    sh -lc 'dd if=/dev/zero of=repository/large/large-1m.bin bs=4096 count=1 seek=8 conv=notrunc status=none && sync -f repository/large/large-1m.bin'
  record_operation "$mode" partial_write_100m "$data_root/repository/large/large-100m.bin" \
    sh -lc 'dd if=/dev/zero of=repository/large/large-100m.bin bs=4096 count=1 seek=128 conv=notrunc status=none && sync -f repository/large/large-100m.bin'
  record_operation "$mode" partial_write_1024m "$data_root/repository/large/large-1024m.bin" \
    sh -lc 'dd if=/dev/zero of=repository/large/large-1024m.bin bs=4096 count=1 seek=1024 conv=notrunc status=none && sync -f repository/large/large-1024m.bin'
  record_operation "$mode" temp_replace_100m "$data_root/repository/large/large-100m.bin" \
    sh -lc 'dd if=/dev/zero of=repository/large/replacement.tmp bs=1M count=100 status=none && mv repository/large/replacement.tmp repository/large/large-100m.bin && sync -f repository/large/large-100m.bin'
  record_operation "$mode" full_rewrite_100m "$data_root/repository/large/large-100m.bin" \
    sh -lc 'dd if=/dev/zero of=repository/large/large-100m.bin bs=1M count=100 conv=notrunc status=none && sync -f repository/large/large-100m.bin'

  sandboxfsctl destroy "$active_id" >/dev/null
  active_id=
}

run_mode baseline
run_mode "$overlay_mode"
sandboxfsctl base-verify "$base_name" | jq -e .match >/dev/null

mkdir -p -- "$(dirname -- "$output_path")"
jq -s \
  --arg schema sandboxfs-deferred-v1 \
  --arg generated_at "$(date --iso-8601=ns)" \
  --arg root "$mount_root" \
  --arg device "$mount_device" \
  --arg overlay_mode "$overlay_mode" \
  '{schema:$schema,generated_at:$generated_at,root:$root,device:$device,overlay_mode:$overlay_mode,results:.}' \
  "$result_temp" > "$output_path"

echo "Deferred-cost report written to $output_path"
