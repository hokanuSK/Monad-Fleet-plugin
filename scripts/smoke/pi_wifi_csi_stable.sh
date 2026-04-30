#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"
DEFAULT_FLEET_MANAGER_HOST="$(bash "${ROOT_DIR}/scripts/rpi/default_fleet_manager_host.sh")"
DEFAULT_FLEET_MANAGER_PORT="$(bash "${ROOT_DIR}/scripts/rpi/default_fleet_manager_port.sh")"

PI_USER="${PI_USER:-admin}"
PI_HOST="${PI_HOST:-monad-rpi5.local}"
PI_HOSTS_CSV="${PI_HOSTS_CSV:-${PI_HOST}}"
PI_RECOVERY_HOSTS_CSV="${PI_RECOVERY_HOSTS_CSV:-${PI_HOSTS_CSV}}"

FLEET_MANAGER_HOST="${FLEET_MANAGER_HOST:-${DEFAULT_FLEET_MANAGER_HOST}}"
FLEET_MANAGER_PORT="${FLEET_MANAGER_PORT:-${DEFAULT_FLEET_MANAGER_PORT}}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-2}"
ATTEMPT_BACKOFF_S="${ATTEMPT_BACKOFF_S:-8}"
RECOVERY_WAIT_S="${RECOVERY_WAIT_S:-180}"
ATTEMPT_FAIL_FAST_TIMEOUT_S="${ATTEMPT_FAIL_FAST_TIMEOUT_S:-900}"
STOP_ON_RECOVERY_TIMEOUT="${STOP_ON_RECOVERY_TIMEOUT:-false}"
RESTART_HINT_ON_TIMEOUT="${RESTART_HINT_ON_TIMEOUT:-true}"
SSH_WAIT_TIMEOUT_S="${SSH_WAIT_TIMEOUT_S:-75}"
SSH_WAIT_INTERVAL_S="${SSH_WAIT_INTERVAL_S:-4}"
SSH_DROP_FAIL_FAST_S="${SSH_DROP_FAIL_FAST_S:-0}"
PRE_ATTEMPT_MANUAL_LOGS="${PRE_ATTEMPT_MANUAL_LOGS:-true}"
MANUAL_LOG_LINES="${MANUAL_LOG_LINES:-220}"

SSH_CONNECT_TIMEOUT_S="${SSH_CONNECT_TIMEOUT_S:-8}"
SSH_ARGS=(
  -o "ConnectTimeout=${SSH_CONNECT_TIMEOUT_S}"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
)

csv_to_lines() {
  local raw="$1"
  local part=""
  local old_ifs="$IFS"
  IFS=','
  for part in $raw; do
    part="${part#"${part%%[![:space:]]*}"}"
    part="${part%"${part##*[![:space:]]}"}"
    if [[ -n "${part}" ]]; then
      printf '%s\n' "${part}"
    fi
  done
  IFS="$old_ifs"
}

HOSTS=()
while IFS= read -r host; do
  HOSTS+=("${host}")
done < <(csv_to_lines "${PI_HOSTS_CSV}")
if [[ ${#HOSTS[@]} -eq 0 ]]; then
  HOSTS=("${PI_HOST}")
fi

RECOVERY_HOSTS=()
append_recovery_host() {
  local candidate="$1"
  local existing=""
  if [[ -z "${candidate}" ]]; then
    return 0
  fi
  for existing in "${RECOVERY_HOSTS[@]:-}"; do
    if [[ "${existing}" == "${candidate}" ]]; then
      return 0
    fi
  done
  RECOVERY_HOSTS+=("${candidate}")
}
for host in "${HOSTS[@]}"; do
  append_recovery_host "${host}"
done
while IFS= read -r host; do
  append_recovery_host "${host}"
done < <(csv_to_lines "${PI_RECOVERY_HOSTS_CSV}")
PI_RECOVERY_HOSTS_CSV_EFFECTIVE="$(IFS=,; printf '%s' "${RECOVERY_HOSTS[*]}")"

wait_for_any_ssh() {
  local timeout_s="$1"
  local started=0
  local now=0
  local elapsed=0
  local host=""

  started="$(date +%s)"
  while true; do
    for host in "${RECOVERY_HOSTS[@]}"; do
      if ssh "${SSH_ARGS[@]}" "${PI_USER}@${host}" "echo SSH_OK" >/dev/null 2>&1; then
        echo "Recovery SSH reachable on ${PI_USER}@${host}"
        return 0
      fi
    done
    now="$(date +%s)"
    elapsed=$((now - started))
    if (( elapsed >= timeout_s )); then
      echo "Recovery wait timeout (${timeout_s}s). Hosts=${PI_RECOVERY_HOSTS_CSV_EFFECTIVE}"
      if [[ "${RESTART_HINT_ON_TIMEOUT}" == "true" ]]; then
        echo "ACTION: RESTART NOW (Pi power-cycle), then rerun this smoke profile."
      fi
      return 1
    fi
    sleep 5
  done
}

collect_manual_logs() {
  local attempt_label="$1"
  local host=""
  if [[ "${PRE_ATTEMPT_MANUAL_LOGS}" != "true" ]]; then
    return 0
  fi
  for host in "${HOSTS[@]}"; do
    echo "Manual pre-attempt logs (${attempt_label}) from ${PI_USER}@${host}..."
    if ! ssh "${SSH_ARGS[@]}" "${PI_USER}@${host}" "bash -lc '
set -euo pipefail
echo HOST=\$(hostname)
date -Is
ip -brief addr || true
/usr/sbin/iw dev wlan0 link 2>/dev/null || true
base=/home/${PI_USER}/monad-fleet-agent/data
echo PENDING:
ls -1t \"\$base/pending\" 2>/dev/null | head -n 5 || true
echo SENT:
ls -1t \"\$base/sent\" 2>/dev/null | head -n 5 || true
echo AGENT_LOG_TAIL:
journalctl -u monad-fleet-agent.service -n ${MANUAL_LOG_LINES} --no-pager || true
echo DMESG_FEITCSI_TAIL:
dmesg | grep -Ei \"feitcsi|iwlwifi|monitor|csi|out of memory|killed process\" | tail -n 120 || true
'"; then
      echo "WARNING: failed to collect manual logs from ${host} (continuing)."
    fi
  done
}

if [[ -z "${AGENT_IDS_CSV:-}" ]]; then
  AGENTS=()
  for host in "${HOSTS[@]}"; do
    echo "Detecting AGENT_ID from ${PI_USER}@${host} (wlan0 MAC)..."
    mac="$(ssh "${SSH_ARGS[@]}" "${PI_USER}@${host}" "cat /sys/class/net/wlan0/address 2>/dev/null || true" | tr -d '\r\n' | tr '[:upper:]' '[:lower:]')"
    if [[ -z "${mac}" ]]; then
      echo "ERROR: Could not detect wlan0 MAC on ${host}. Set AGENT_IDS_CSV explicitly."
      exit 2
    fi
    AGENTS+=("${mac}")
  done
  AGENT_IDS_CSV="$(IFS=,; printf '%s' "${AGENTS[*]}")"
  export AGENT_IDS_CSV
  echo "Resolved AGENT_IDS_CSV=${AGENT_IDS_CSV}"
fi

if [[ "${SYNC_PI_TIME:-true}" == "true" ]]; then
  for host in "${HOSTS[@]}"; do
    echo "Syncing time on ${PI_USER}@${host}..."
    ssh "${SSH_ARGS[@]}" "${PI_USER}@${host}" "sudo -n timedatectl set-ntp true && sudo -n systemctl restart systemd-timesyncd || true"
    ssh "${SSH_ARGS[@]}" "${PI_USER}@${host}" "date -u +%FT%TZ"
  done
fi

echo "Running stable Wi-Fi + CSI smoke profile..."
echo "Hosts=${PI_HOSTS_CSV}"
echo "Recovery hosts=${PI_RECOVERY_HOSTS_CSV_EFFECTIVE}"
echo "Fleet=${FLEET_MANAGER_HOST}:${FLEET_MANAGER_PORT}"

attempt=1
last_rc=1
while (( attempt <= MAX_ATTEMPTS )); do
  collect_manual_logs "attempt-${attempt}" || true
  echo "Attempt ${attempt}/${MAX_ATTEMPTS}: running Wi-Fi+CSI smoke profile..."
  set +e
  RUN_PI=true \
  PI_HOSTS_CSV="${PI_HOSTS_CSV}" \
  PI_RECOVERY_HOSTS_CSV="${PI_RECOVERY_HOSTS_CSV_EFFECTIVE}" \
  FLEET_MANAGER_HOST="${FLEET_MANAGER_HOST}" \
  FLEET_MANAGER_PORT="${FLEET_MANAGER_PORT}" \
  SSH_WAIT_TIMEOUT_S="${SSH_WAIT_TIMEOUT_S}" \
  SSH_WAIT_INTERVAL_S="${SSH_WAIT_INTERVAL_S}" \
  SSH_DROP_FAIL_FAST_S="${SSH_DROP_FAIL_FAST_S}" \
  RESTART_NOW_HINT_ON_SSH_DROP="${RESTART_NOW_HINT_ON_SSH_DROP:-true}" \
  MANAGE_SYSTEMD_AGENT="${MANAGE_SYSTEMD_AGENT:-true}" \
  SYSTEMD_AGENT_SERVICE="${SYSTEMD_AGENT_SERVICE:-monad-fleet-agent.service}" \
  SMOKE_PROFILE="${SMOKE_PROFILE:-full}" \
  FULL_DURATION_S="${FULL_DURATION_S:-30}" \
  FULL_INTERVAL_S_PI="${FULL_INTERVAL_S_PI:-5}" \
  FULL_BLE_TIMEOUT_S="${FULL_BLE_TIMEOUT_S:-15}" \
  FULL_CSI_TIMEOUT_S="${FULL_CSI_TIMEOUT_S:-45}" \
  DO_BUILD="${DO_BUILD:-false}" \
  RESET_STATE="${RESET_STATE:-false}" \
  RESET_METRICS="${RESET_METRICS:-false}" \
  CLEANUP="${CLEANUP:-false}" \
  INJECT_HYPOTHETICAL_METRICS="${INJECT_HYPOTHETICAL_METRICS:-false}" \
  REAL_DATA_ENFORCE="${REAL_DATA_ENFORCE:-true}" \
  REQUIRE_REAL_WIFI="${REQUIRE_REAL_WIFI:-true}" \
  REQUIRE_REAL_BLE="${REQUIRE_REAL_BLE:-false}" \
  REQUIRE_REAL_CSI="${REQUIRE_REAL_CSI:-true}" \
  REQUIRE_CSI_FRAMES_MIN="${REQUIRE_CSI_FRAMES_MIN:-1}" \
  ALLOW_WIFI_DISRUPTIVE_CSI="${ALLOW_WIFI_DISRUPTIVE_CSI:-true}" \
  SINGLE_RADIO_MODE="${SINGLE_RADIO_MODE:-true}" \
  SINGLE_RADIO_WIFI_PROFILE="${SINGLE_RADIO_WIFI_PROFILE:-auto}" \
  SINGLE_RADIO_RECOVERY_RETRIES="${SINGLE_RADIO_RECOVERY_RETRIES:-2}" \
  SINGLE_RADIO_RECOVERY_ROUTE_WAIT_S="${SINGLE_RADIO_RECOVERY_ROUTE_WAIT_S:-25}" \
  SINGLE_RADIO_RECOVERY_CMD="${SINGLE_RADIO_RECOVERY_CMD:-}" \
  SINGLE_RADIO_ROUTE_GATE="${SINGLE_RADIO_ROUTE_GATE:-true}" \
  SINGLE_RADIO_ROUTE_HEALTH_TIMEOUT_S="${SINGLE_RADIO_ROUTE_HEALTH_TIMEOUT_S:-5}" \
  SINGLE_RADIO_REBOOT_ON_RECOVERY_FAIL="${SINGLE_RADIO_REBOOT_ON_RECOVERY_FAIL:-true}" \
  SEPARATE_MEASURE_AND_REPORT="${SEPARATE_MEASURE_AND_REPORT:-true}" \
  RESTART_CONTROL_PLANE_BEFORE_REPORT="${RESTART_CONTROL_PLANE_BEFORE_REPORT:-true}" \
  ALLOW_WIFI_CONTROL_PLANE_RESTART="${ALLOW_WIFI_CONTROL_PLANE_RESTART:-false}" \
  CONTROL_PLANE_RESTART_IFACE="${CONTROL_PLANE_RESTART_IFACE:-wlan0}" \
  CONTROL_PLANE_RESTART_DOWN_S="${CONTROL_PLANE_RESTART_DOWN_S:-2}" \
  CONTROL_PLANE_RESTART_WAIT_S="${CONTROL_PLANE_RESTART_WAIT_S:-45}" \
  CONTROL_RPC_TIMEOUT_S="${CONTROL_RPC_TIMEOUT_S:-180}" \
  REPORT_RPC_TIMEOUT_S="${REPORT_RPC_TIMEOUT_S:-180}" \
  AGENT_RUN_TIMEOUT_S="${AGENT_RUN_TIMEOUT_S:-${ATTEMPT_FAIL_FAST_TIMEOUT_S}}" \
  CSI_REQUIRE_EVIDENCE="${CSI_REQUIRE_EVIDENCE:-true}" \
  CSI_MIN_OUTPUT_FILES="${CSI_MIN_OUTPUT_FILES:-1}" \
  CSI_HARD_TIMEOUT_S="${CSI_HARD_TIMEOUT_S:-45}" \
  CSI_CLEANUP_BEFORE_CAPTURE="${CSI_CLEANUP_BEFORE_CAPTURE:-true}" \
  CSI_CLEANUP_AFTER_CAPTURE="${CSI_CLEANUP_AFTER_CAPTURE:-true}" \
  CSI_KILL_PATTERNS="${CSI_KILL_PATTERNS:-feitcsi}" \
  CSI_DEFAULT_FEIT_CHANNEL_WIDTH_MHZ="${CSI_DEFAULT_FEIT_CHANNEL_WIDTH_MHZ:-40}" \
  scripts/smoke/v3_end_to_end_smoke.sh
  rc=$?
  set -e

  if [[ "${rc}" == "0" ]]; then
    echo "Wi-Fi+CSI smoke passed on attempt ${attempt}/${MAX_ATTEMPTS}."
    exit 0
  fi

  last_rc="${rc}"
  echo "Attempt ${attempt}/${MAX_ATTEMPTS} failed with rc=${rc}."
  if (( attempt >= MAX_ATTEMPTS )); then
    break
  fi
  echo "Waiting up to ${RECOVERY_WAIT_S}s for Pi SSH recovery before retry..."
  if ! wait_for_any_ssh "${RECOVERY_WAIT_S}"; then
    if [[ "${STOP_ON_RECOVERY_TIMEOUT}" == "true" ]]; then
      echo "Stopping retries early due to recovery timeout."
      break
    fi
  fi
  echo "Retry backoff: sleeping ${ATTEMPT_BACKOFF_S}s..."
  sleep "${ATTEMPT_BACKOFF_S}"
  attempt=$((attempt + 1))
done

echo "Wi-Fi+CSI smoke failed after ${MAX_ATTEMPTS} attempts."
exit "${last_rc}"
