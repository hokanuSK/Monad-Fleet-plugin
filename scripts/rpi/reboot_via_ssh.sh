#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PI_USER="${PI_USER:-admin}"
PI_HOSTS_CSV="${PI_HOSTS_CSV:-${1:-}}"
PI_HOST="${PI_HOST:-monad-rpi5.local}"
SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE:-}"
USE_DEFAULT_SSH_KEY="${USE_DEFAULT_SSH_KEY:-false}"
DEFAULT_SSH_KEY="${ROOT_DIR}/scripts/rpi/keys/monad_rpi5_ed25519"
SSH_CONNECT_TIMEOUT_S="${SSH_CONNECT_TIMEOUT_S:-6}"
SSH_SERVER_ALIVE_INTERVAL_S="${SSH_SERVER_ALIVE_INTERVAL_S:-3}"
SSH_SERVER_ALIVE_COUNT_MAX="${SSH_SERVER_ALIVE_COUNT_MAX:-2}"
WAIT_UP_AFTER_REBOOT="${WAIT_UP_AFTER_REBOOT:-true}"
WAIT_TIMEOUT_S="${WAIT_TIMEOUT_S:-180}"
WAIT_INTERVAL_S="${WAIT_INTERVAL_S:-5}"
PI_SUDO_PASSWORD="${PI_SUDO_PASSWORD:-}"
AGENT_MAC="${AGENT_MAC:-}"

if [[ -z "${SSH_IDENTITY_FILE}" && "${USE_DEFAULT_SSH_KEY}" == "true" && -f "${DEFAULT_SSH_KEY}" ]]; then
  SSH_IDENTITY_FILE="${DEFAULT_SSH_KEY}"
fi

SSH_ARGS=(
  -o "ConnectTimeout=${SSH_CONNECT_TIMEOUT_S}"
  -o "ServerAliveInterval=${SSH_SERVER_ALIVE_INTERVAL_S}"
  -o "ServerAliveCountMax=${SSH_SERVER_ALIVE_COUNT_MAX}"
  -o StrictHostKeyChecking=accept-new
)
if [[ -n "${SSH_IDENTITY_FILE}" ]]; then
  SSH_ARGS+=(-i "${SSH_IDENTITY_FILE}" -o IdentitiesOnly=yes)
fi

run_ssh() {
  ssh "${SSH_ARGS[@]}" "$@"
}

trim() {
  local v="$1"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  printf "%s" "${v}"
}

csv_to_lines() {
  local raw="$1"
  local part=""
  local old_ifs="$IFS"
  IFS=','
  for part in ${raw}; do
    part="$(trim "${part}")"
    if [[ -n "${part}" ]]; then
      printf "%s\n" "${part}"
    fi
  done
  IFS="${old_ifs}"
}

discover_hosts_from_ndp() {
  local mac_lc
  mac_lc="$(printf "%s" "${AGENT_MAC}" | tr '[:upper:]' '[:lower:]')"
  if [[ -z "${mac_lc}" ]]; then
    return 0
  fi
  ndp -a 2>/dev/null | awk -v mac="${mac_lc}" '
    {
      line=tolower($0)
      if (index(line, mac) == 0) next
      host=$1
      if (host ~ /^fe80::/) print host
    }
  ' | sort -u
}

declare -a HOSTS=()
if [[ -n "${PI_HOSTS_CSV}" ]]; then
  while IFS= read -r row; do
    HOSTS+=("${row}")
  done < <(csv_to_lines "${PI_HOSTS_CSV}")
else
  HOSTS+=("${PI_HOST}")
fi

while IFS= read -r row; do
  HOSTS+=("${row}")
done < <(discover_hosts_from_ndp || true)

declare -a CANDIDATES=()
host_seen() {
  local probe="$1"
  local row=""
  for row in "${CANDIDATES[@]-}"; do
    if [[ "${row}" == "${probe}" ]]; then
      return 0
    fi
  done
  return 1
}

for host in "${HOSTS[@]-}"; do
  host="$(trim "${host}")"
  if [[ -z "${host}" ]]; then
    continue
  fi
  if host_seen "${host}"; then
    continue
  fi
  CANDIDATES+=("${host}")
done

if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
  echo "No reboot candidates available." >&2
  exit 1
fi

REBOOT_HOST=""
for host in "${CANDIDATES[@]}"; do
  if run_ssh "${PI_USER}@${host}" "echo SSH_OK" >/dev/null 2>&1; then
    REBOOT_HOST="${host}"
    break
  fi
done

if [[ -z "${REBOOT_HOST}" ]]; then
  echo "No reachable SSH host for reboot." >&2
  printf "Tried: %s\n" "${CANDIDATES[*]}" >&2
  exit 2
fi

echo "Reboot via ${PI_USER}@${REBOOT_HOST}"
if [[ -n "${PI_SUDO_PASSWORD}" ]]; then
  run_ssh "${PI_USER}@${REBOOT_HOST}" "printf '%s\n' '${PI_SUDO_PASSWORD}' | sudo -S -p '' /sbin/reboot" || true
else
  run_ssh "${PI_USER}@${REBOOT_HOST}" "sudo -n /sbin/reboot || sudo /sbin/reboot" || true
fi

if [[ "${WAIT_UP_AFTER_REBOOT}" != "true" ]]; then
  exit 0
fi

echo "Waiting for reboot cycle (${WAIT_TIMEOUT_S}s timeout)..."
sleep 3
started="$(date +%s)"
seen_down=false

while true; do
  now="$(date +%s)"
  elapsed=$((now - started))
  if (( elapsed >= WAIT_TIMEOUT_S )); then
    echo "Timed out waiting for reboot recovery (${WAIT_TIMEOUT_S}s)." >&2
    exit 3
  fi

  if run_ssh "${PI_USER}@${REBOOT_HOST}" "echo SSH_OK" >/dev/null 2>&1; then
    if [[ "${seen_down}" == "true" ]]; then
      echo "Pi is reachable again: ${PI_USER}@${REBOOT_HOST}"
      exit 0
    fi
  else
    seen_down=true
  fi
  sleep "${WAIT_INTERVAL_S}"
done
