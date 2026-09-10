#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly backup_cli="${SZS_HUB_BACKUP_CLI:-/opt/szs-hub/current/.venv/bin/szs-hub}"
readonly local_dir="${SZS_HUB_BACKUP_DIR:-/var/lib/szs-hub-backup/objects}"
readonly status_file="${SZS_HUB_BACKUP_STATUS:-/var/lib/szs-hub-backup/last-success}"
readonly runtime_dir="${RUNTIME_DIRECTORY:-/run/szs-hub-backup}"
readonly age_recipient="${SZS_HUB_AGE_RECIPIENT:?SZS_HUB_AGE_RECIPIENT is required}"
readonly remote="${SZS_HUB_RCLONE_REMOTE:?SZS_HUB_RCLONE_REMOTE is required}"

if [[ "${local_dir}" != "/var/lib/szs-hub-backup/objects" || \
      "${status_file}" != "/var/lib/szs-hub-backup/last-success" ]]; then
  echo "Refusing an unexpected local backup directory" >&2
  exit 64
fi

remote_name="${remote%%:*}"
remote_path="${remote#*:}"
if [[ "${remote}" != *:* || -z "${remote_name}" || -z "${remote_path}" || \
      "${remote_path}" == "/" || "${remote_path}" == *".."* ]]; then
  echo "Refusing an empty or broad rclone destination" >&2
  exit 64
fi

for executable in "${backup_cli}" age rclone sha256sum flock sed sort; do
  if ! command -v "${executable}" >/dev/null 2>&1; then
    echo "Required backup command is unavailable" >&2
    exit 69
  fi
done

install -d -m 0700 "${local_dir}" "${runtime_dir}"
exec 9>"${runtime_dir}/backup.lock"
if ! flock -n 9; then
  echo "Another backup is already running" >&2
  exit 75
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
day="$(TZ=Europe/Moscow date +%F)"
week="$(TZ=Europe/Moscow date +%G-W%V)"
month="$(TZ=Europe/Moscow date +%Y-%m)"
weekday="$(TZ=Europe/Moscow date +%u)"
monthday="$(TZ=Europe/Moscow date +%d)"
filename="szs-hub-${stamp}.sqlite3.age"
plain="$(mktemp "${runtime_dir}/szs-hub-${stamp}.XXXXXX.sqlite3")"
cipher_tmp="$(mktemp "${local_dir}/.${filename}.XXXXXX")"
cipher="${local_dir}/${filename}"
checksum="${cipher}.sha256"
status_tmp=""

cleanup() {
  rm -f -- "${plain}" "${cipher_tmp}"
  if [[ -n "${status_tmp}" ]]; then
    rm -f -- "${status_tmp}"
  fi
}
trap cleanup EXIT

"${backup_cli}" backup --output "${plain}"
age --recipient "${age_recipient}" --output "${cipher_tmp}" "${plain}"
mv -- "${cipher_tmp}" "${cipher}"
digest="$(sha256sum "${cipher}" | cut -d ' ' -f 1)"
printf '%s  %s\n' "${digest}" "${filename}" >"${checksum}"

upload_pair() {
  local class="$1"
  local bucket="$2"
  local destination="${remote}/${class}/${bucket}"
  rclone copyto --checksum --immutable "${cipher}" "${destination}/${filename}"
  rclone copyto --checksum --immutable "${checksum}" "${destination}/${filename}.sha256"

  local remote_file
  local remote_files=()
  mapfile -t remote_files < <(rclone lsf --files-only "${destination}")
  for remote_file in "${remote_files[@]}"; do
    if [[ "${remote_file}" == "${filename}" || \
          "${remote_file}" == "${filename}.sha256" ]]; then
      continue
    fi
    if [[ "${remote_file}" =~ ^szs-hub-[0-9]{8}T[0-9]{6}Z\.sqlite3\.age(\.sha256)?$ ]]; then
      rclone deletefile "${destination}/${remote_file}"
    else
      echo "Skipping an unexpected remote backup object name" >&2
    fi
  done
}

prune_remote() {
  local class="$1"
  local keep="$2"
  local pattern="$3"
  local buckets=()
  local index

  mapfile -t buckets < <(
    rclone lsf --dirs-only "${remote}/${class}" | sed 's:/$::' | LC_ALL=C sort -r
  )
  for ((index = keep; index < ${#buckets[@]}; index++)); do
    if [[ ! "${buckets[index]}" =~ ${pattern} ]]; then
      echo "Skipping an unexpected remote backup directory name" >&2
      continue
    fi
    rclone purge "${remote}/${class}/${buckets[index]}"
  done
}

upload_pair daily "${day}"
prune_remote daily 7 '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
if [[ "${weekday}" == "7" ]]; then
  upload_pair weekly "${week}"
  prune_remote weekly 4 '^[0-9]{4}-W[0-9]{2}$'
fi
if [[ "${monthday}" == "01" ]]; then
  upload_pair monthly "${month}"
  prune_remote monthly 3 '^[0-9]{4}-[0-9]{2}$'
fi

# Keep exactly seven ciphertexts in the local cache. No plaintext backup survives.
shopt -s nullglob
local_backups=("${local_dir}"/*.age)
mapfile -t local_backups < <(printf '%s\n' "${local_backups[@]}" | LC_ALL=C sort -r)
for ((index = 7; index < ${#local_backups[@]}; index++)); do
  rm -f -- "${local_backups[index]}" "${local_backups[index]}.sha256"
done

status_tmp="$(mktemp "/var/lib/szs-hub-backup/.last-success.XXXXXX")"
printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"${status_tmp}"
chmod 0644 "${status_tmp}"
mv -- "${status_tmp}" "${status_file}"
status_tmp=""

echo "Encrypted backup uploaded and local checksum recorded: ${filename}"
