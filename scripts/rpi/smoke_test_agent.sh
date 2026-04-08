#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PI_HOST="${PI_HOST:-${1:-monad-rpi5.local}}"
PI_HOSTS_CSV="${PI_HOSTS_CSV:-}"
PI_RECOVERY_HOSTS_CSV="${PI_RECOVERY_HOSTS_CSV:-${PI_HOSTS_CSV:-${PI_HOST}}}"
PI_USER="${PI_USER:-admin}"
PI_DIR="${PI_DIR:-/home/${PI_USER}/monad-fleet-agent}"
SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE:-}"
USE_DEFAULT_SSH_KEY="${USE_DEFAULT_SSH_KEY:-false}"
DEFAULT_SSH_KEY="${ROOT_DIR}/scripts/rpi/keys/monad_rpi5_ed25519"
SSH_CONNECT_TIMEOUT_S="${SSH_CONNECT_TIMEOUT_S:-10}"
SSH_SERVER_ALIVE_INTERVAL_S="${SSH_SERVER_ALIVE_INTERVAL_S:-5}"
SSH_SERVER_ALIVE_COUNT_MAX="${SSH_SERVER_ALIVE_COUNT_MAX:-3}"
SSH_WAIT_TIMEOUT_S="${SSH_WAIT_TIMEOUT_S:-180}"
SSH_WAIT_INTERVAL_S="${SSH_WAIT_INTERVAL_S:-5}"
PRECHECK_ONLY="${PRECHECK_ONLY:-false}"
if [[ -z "${SSH_IDENTITY_FILE}" && "${USE_DEFAULT_SSH_KEY}" == "true" && -f "${DEFAULT_SSH_KEY}" ]]; then
  SSH_IDENTITY_FILE="${DEFAULT_SSH_KEY}"
fi

SSH_ARGS=(
  -o "ConnectTimeout=${SSH_CONNECT_TIMEOUT_S}"
  -o "ServerAliveInterval=${SSH_SERVER_ALIVE_INTERVAL_S}"
  -o "ServerAliveCountMax=${SSH_SERVER_ALIVE_COUNT_MAX}"
  -o StrictHostKeyChecking=accept-new
)
FALLBACK_SSH_ARGS=(
  -o "ConnectTimeout=${SSH_CONNECT_TIMEOUT_S}"
  -o StrictHostKeyChecking=accept-new
)
if [[ -n "${SSH_IDENTITY_FILE}" ]]; then
  SSH_ARGS+=(-i "${SSH_IDENTITY_FILE}" -o IdentitiesOnly=yes)
  FALLBACK_SSH_ARGS+=(-i "${SSH_IDENTITY_FILE}" -o IdentitiesOnly=yes)
fi

run_ssh() {
  if [[ ${#SSH_ARGS[@]} -gt 0 ]]; then
    ssh "${SSH_ARGS[@]}" "$@"
  else
    ssh "$@"
  fi
}

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

RECOVERY_HOSTS=()
init_recovery_hosts() {
  local host=""
  local existing=""
  local seen="false"
  local -a candidates=()

  RECOVERY_HOSTS=()
  candidates+=("${PI_HOST}")
  while IFS= read -r host; do
    candidates+=("${host}")
  done < <(csv_to_lines "${PI_RECOVERY_HOSTS_CSV}")

  for host in "${candidates[@]:-}"; do
    [[ -n "${host}" ]] || continue
    seen="false"
    for existing in "${RECOVERY_HOSTS[@]:-}"; do
      if [[ "${existing}" == "${host}" ]]; then
        seen="true"
        break
      fi
    done
    if [[ "${seen}" != "true" ]]; then
      RECOVERY_HOSTS+=("${host}")
    fi
  done
}

wait_for_ssh() {
  local timeout_s="${SSH_WAIT_TIMEOUT_S}"
  local interval_s="${SSH_WAIT_INTERVAL_S}"
  local started=0
  local now=0
  local elapsed=0
  local attempt=0
  local out=""
  local host=""
  local selected=""
  local -a host_candidates=()

  while IFS= read -r host; do
    host_candidates+=("${host}")
  done < <(csv_to_lines "${PI_HOSTS_CSV}")
  if [[ ${#host_candidates[@]} -eq 0 ]]; then
    host_candidates=("${PI_HOST}")
  fi

  started="$(date +%s)"
  while true; do
    attempt=$((attempt + 1))
    for host in "${host_candidates[@]}"; do
      if out="$(run_ssh "${PI_USER}@${host}" "echo SSH_OK" 2>&1)"; then
        selected="${host}"
        PI_HOST="${selected}"
        now="$(date +%s)"
        elapsed=$((now - started))
        if (( ${#host_candidates[@]} > 1 )); then
          echo "Selected reachable Pi host: ${PI_HOST}"
        fi
        if (( attempt > 1 )); then
          echo "SSH reachable on ${PI_USER}@${PI_HOST} after ${elapsed}s (attempt ${attempt})."
        fi
        return 0
      fi
    done
    now="$(date +%s)"
    elapsed=$((now - started))
    if (( elapsed >= timeout_s )); then
      echo "ERROR: SSH to Pi host candidates not reachable after ${elapsed}s."
      echo "Candidates: ${host_candidates[*]}"
      if [[ -n "${out}" ]]; then
        echo "Last SSH error: ${out}"
      fi
      return 1
    fi
    if (( ${#host_candidates[@]} == 1 )); then
      echo "Waiting for SSH ${PI_USER}@${PI_HOST} (${elapsed}s/${timeout_s}s)..."
    else
      echo "Waiting for SSH ${PI_USER}@{${host_candidates[*]}} (${elapsed}s/${timeout_s}s)..."
    fi
    sleep "${interval_s}"
  done
}

FLEET_MANAGER_HOST="${FLEET_MANAGER_HOST:-192.168.0.70}"
FLEET_MANAGER_PORT="${FLEET_MANAGER_PORT:-50060}"
AGENT_ID="${AGENT_ID:-}"
CONTROL_PLANE_MODE="${CONTROL_PLANE_MODE:-RF_SHARING}"
CONTROL_PLANE_IFACE="${CONTROL_PLANE_IFACE:-wlan0}"
WIFI_SCAN_IFACE="${WIFI_SCAN_IFACE:-${CONTROL_PLANE_IFACE}}"
CONTROL_PLANE_BAND_EXPECT="${CONTROL_PLANE_BAND_EXPECT:-}"
CSI_MEASURE_IFACE="${CSI_MEASURE_IFACE:-}"
CSI_MEASURE_BAND_EXPECT="${CSI_MEASURE_BAND_EXPECT:-}"
CSI_REQUIRE_SEPARATE_IFACE="${CSI_REQUIRE_SEPARATE_IFACE:-false}"
CSI_IFACE_FLAG="${CSI_IFACE_FLAG:-auto}"
AUTO_SWITCH_CONTROL_PLANE_IFACE="${AUTO_SWITCH_CONTROL_PLANE_IFACE:-false}"
AUTO_SWITCH_CSI_MEASURE_IFACE="${AUTO_SWITCH_CSI_MEASURE_IFACE:-true}"
CONTROL_PLANE_IPV4="${CONTROL_PLANE_IPV4:-}"
CONTROL_PLANE_IPV4_PREFIX="${CONTROL_PLANE_IPV4_PREFIX:-16}"
AUTO_LINKLOCAL_ETHERNET_IPV4="${AUTO_LINKLOCAL_ETHERNET_IPV4:-true}"
EXECUTE_POLICY="${EXECUTE_POLICY:-true}"
MAX_SYNC_CYCLES="${MAX_SYNC_CYCLES:-1}"
WIFI_SAMPLE_MIN_INTERVAL_S="${WIFI_SAMPLE_MIN_INTERVAL_S:-1}"
WIFI_SAMPLE_MAX_DURATION_S="${WIFI_SAMPLE_MAX_DURATION_S:-7200}"
WIFI_SAMPLE_MAX_POINTS="${WIFI_SAMPLE_MAX_POINTS:-7200}"
ENABLE_HTTP_SAMPLE_INGEST="${ENABLE_HTTP_SAMPLE_INGEST:-true}"
ENABLE_ELAB_ARTIFACT_UPLOAD="${ENABLE_ELAB_ARTIFACT_UPLOAD:-true}"
ARTIFACT_UPLOAD_MAX_BYTES="${ARTIFACT_UPLOAD_MAX_BYTES:-20971520}"
ARTIFACT_UPLOAD_TARGET="${ARTIFACT_UPLOAD_TARGET:-elabftw}"
ARTIFACT_UPLOAD_DURING_MEASURE="${ARTIFACT_UPLOAD_DURING_MEASURE:-false}"
ARTIFACT_EVICT_AFTER_UPLOAD="${ARTIFACT_EVICT_AFTER_UPLOAD:-false}"
ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S="${ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S:-3}"
ARTIFACT_UPLOAD_BACKOFF_S="${ARTIFACT_UPLOAD_BACKOFF_S:-15}"
RUN_ARTIFACT_SOFT_LIMIT_BYTES="${RUN_ARTIFACT_SOFT_LIMIT_BYTES:-0}"
UPLOAD_WINDOW_FROM="${UPLOAD_WINDOW_FROM:-}"
UPLOAD_WINDOW_TO="${UPLOAD_WINDOW_TO:-}"
UPLOAD_WINDOW_REQUIRED="${UPLOAD_WINDOW_REQUIRED:-false}"
UPLOAD_SLOT_COUNT="${UPLOAD_SLOT_COUNT:-1}"
UPLOAD_SLOT_JITTER_S="${UPLOAD_SLOT_JITTER_S:-0}"
PROM_REMOTE_WRITE_ON_CMD="${PROM_REMOTE_WRITE_ON_CMD:-}"
PROM_REMOTE_WRITE_OFF_CMD="${PROM_REMOTE_WRITE_OFF_CMD:-}"
PROM_REMOTE_WRITE_CMD_TIMEOUT_S="${PROM_REMOTE_WRITE_CMD_TIMEOUT_S:-12}"
REPORT_RPC_TIMEOUT_S="${REPORT_RPC_TIMEOUT_S:-120}"
CONTROL_RPC_TIMEOUT_S="${CONTROL_RPC_TIMEOUT_S:-30}"
AGENT_RUN_TIMEOUT_S="${AGENT_RUN_TIMEOUT_S:-900}"
AGENT_STATUS_POLL_S="${AGENT_STATUS_POLL_S:-5}"
SSH_DROP_FAIL_FAST_S="${SSH_DROP_FAIL_FAST_S:-0}"
RESTART_NOW_HINT_ON_SSH_DROP="${RESTART_NOW_HINT_ON_SSH_DROP:-true}"
MANAGE_SYSTEMD_AGENT="${MANAGE_SYSTEMD_AGENT:-true}"
SYSTEMD_AGENT_SERVICE="${SYSTEMD_AGENT_SERVICE:-monad-fleet-agent.service}"
POST_RUN_AUDIT="${POST_RUN_AUDIT:-true}"
SEPARATE_MEASURE_AND_REPORT="${SEPARATE_MEASURE_AND_REPORT:-false}"
RESTART_CONTROL_PLANE_BEFORE_REPORT="${RESTART_CONTROL_PLANE_BEFORE_REPORT:-false}"
CONTROL_PLANE_RESTART_IFACE="${CONTROL_PLANE_RESTART_IFACE:-${CONTROL_PLANE_IFACE}}"
CONTROL_PLANE_RESTART_DOWN_S="${CONTROL_PLANE_RESTART_DOWN_S:-2}"
CONTROL_PLANE_RESTART_WAIT_S="${CONTROL_PLANE_RESTART_WAIT_S:-45}"
CONTROL_PLANE_RESTART_CMD="${CONTROL_PLANE_RESTART_CMD:-}"
ALLOW_WIFI_CONTROL_PLANE_RESTART="${ALLOW_WIFI_CONTROL_PLANE_RESTART:-false}"
ALLOW_WIFI_DISRUPTIVE_CSI="${ALLOW_WIFI_DISRUPTIVE_CSI:-false}"
DISABLE_CSI_CAPTURE="${DISABLE_CSI_CAPTURE:-}"
REQUIRE_REAL_CSI="${REQUIRE_REAL_CSI:-false}"
REQUIRE_ETHERNET_CONTROL="${REQUIRE_ETHERNET_CONTROL:-false}"
REQUIRE_WIFI_CONTROL="${REQUIRE_WIFI_CONTROL:-false}"
SINGLE_RADIO_MODE="${SINGLE_RADIO_MODE:-false}"
SINGLE_RADIO_WIFI_PROFILE="${SINGLE_RADIO_WIFI_PROFILE:-auto}"
SINGLE_RADIO_RECOVERY_RETRIES="${SINGLE_RADIO_RECOVERY_RETRIES:-3}"
SINGLE_RADIO_RECOVERY_ROUTE_WAIT_S="${SINGLE_RADIO_RECOVERY_ROUTE_WAIT_S:-45}"
SINGLE_RADIO_RECOVERY_CMD="${SINGLE_RADIO_RECOVERY_CMD:-}"
SINGLE_RADIO_ROUTE_GATE="${SINGLE_RADIO_ROUTE_GATE:-true}"
SINGLE_RADIO_ROUTE_HEALTH_TIMEOUT_S="${SINGLE_RADIO_ROUTE_HEALTH_TIMEOUT_S:-5}"
SINGLE_RADIO_REBOOT_ON_RECOVERY_FAIL="${SINGLE_RADIO_REBOOT_ON_RECOVERY_FAIL:-false}"
STRICT_CONTROL_PLANE_IFACE="${STRICT_CONTROL_PLANE_IFACE:-false}"
STRICT_WIFI_SCAN_IFACE="${STRICT_WIFI_SCAN_IFACE:-false}"
ENFORCE_CONTROL_PLANE_ROUTE="${ENFORCE_CONTROL_PLANE_ROUTE:-false}"
CSI_COLLECTOR_CMD="${CSI_COLLECTOR_CMD:-}"
CSI_OUTPUT_PATH="${CSI_OUTPUT_PATH:-}"
CSI_OUTPUT_GLOB="${CSI_OUTPUT_GLOB:-}"
CSI_FRAMES_REGEX="${CSI_FRAMES_REGEX:-}"
CSI_OUTPUT_MAX_FILES="${CSI_OUTPUT_MAX_FILES:-}"
CSI_PARSE_MAX_BYTES="${CSI_PARSE_MAX_BYTES:-}"
CSI_REQUIRE_EVIDENCE="${CSI_REQUIRE_EVIDENCE:-}"
CSI_MIN_FRAMES="${CSI_MIN_FRAMES:-}"
CSI_MIN_OUTPUT_FILES="${CSI_MIN_OUTPUT_FILES:-}"
CSI_SUDO_NONINTERACTIVE="${CSI_SUDO_NONINTERACTIVE:-true}"
CSI_TRAFFIC_ENABLE="${CSI_TRAFFIC_ENABLE:-}"
CSI_TRAFFIC_TARGET="${CSI_TRAFFIC_TARGET:-}"
CSI_TRAFFIC_IFACE="${CSI_TRAFFIC_IFACE:-}"
CSI_TRAFFIC_INTERVAL_MS="${CSI_TRAFFIC_INTERVAL_MS:-}"
CSI_ALLOW_THROTTLED="${CSI_ALLOW_THROTTLED:-false}"
CSI_HARD_TIMEOUT_S="${CSI_HARD_TIMEOUT_S:-0}"
CSI_CLEANUP_BEFORE_CAPTURE="${CSI_CLEANUP_BEFORE_CAPTURE:-true}"
CSI_CLEANUP_AFTER_CAPTURE="${CSI_CLEANUP_AFTER_CAPTURE:-true}"
CSI_KILL_PATTERNS="${CSI_KILL_PATTERNS:-feitcsi}"
CSI_DEFAULT_FEIT_FREQ_MHZ="${CSI_DEFAULT_FEIT_FREQ_MHZ:-5240}"
CSI_DEFAULT_FEIT_CHANNEL_WIDTH_MHZ="${CSI_DEFAULT_FEIT_CHANNEL_WIDTH_MHZ:-80}"
CSI_DEFAULT_FEIT_FORMAT="${CSI_DEFAULT_FEIT_FORMAT:-VHT}"
CSI_DEFAULT_OUTPUT_PATH="${CSI_DEFAULT_OUTPUT_PATH:-/tmp/csi.dat}"
CSI_COLLECTOR_VERBOSE="${CSI_COLLECTOR_VERBOSE:-false}"
CSI_LIVE_MONITOR_ENABLE="${CSI_LIVE_MONITOR_ENABLE:-true}"
CSI_LIVE_MONITOR_PATH="${CSI_LIVE_MONITOR_PATH:-}"

if [[ "${SINGLE_RADIO_MODE}" == "true" ]]; then
  SEPARATE_MEASURE_AND_REPORT="true"
  RESTART_CONTROL_PLANE_BEFORE_REPORT="true"
  if [[ -z "${SINGLE_RADIO_RECOVERY_CMD}" ]]; then
    SINGLE_RADIO_RECOVERY_CMD="${CONTROL_PLANE_RESTART_CMD}"
  fi
fi

if [[ -z "${CSI_COLLECTOR_CMD}" ]]; then
  CSI_VERBOSE_FLAG=""
  if [[ "${CSI_COLLECTOR_VERBOSE}" == "true" ]]; then
    CSI_VERBOSE_FLAG=" -v"
  fi
  CSI_COLLECTOR_CMD="sudo feitcsi --frequency ${CSI_DEFAULT_FEIT_FREQ_MHZ} --channel-width ${CSI_DEFAULT_FEIT_CHANNEL_WIDTH_MHZ} --format ${CSI_DEFAULT_FEIT_FORMAT} --output-file ${CSI_DEFAULT_OUTPUT_PATH}${CSI_VERBOSE_FLAG}"
  echo "CSI default: using collector command '${CSI_COLLECTOR_CMD}'."
fi
if [[ -z "${CSI_OUTPUT_PATH}" && -z "${CSI_OUTPUT_GLOB}" ]]; then
  CSI_OUTPUT_PATH="${CSI_DEFAULT_OUTPUT_PATH}"
fi
if [[ "${REQUIRE_REAL_CSI}" == "true" ]]; then
  if [[ -z "${CSI_REQUIRE_EVIDENCE}" ]]; then
    CSI_REQUIRE_EVIDENCE="true"
  fi
  if [[ -z "${CSI_MIN_FRAMES}" ]]; then
    CSI_MIN_FRAMES="1"
  fi
  if [[ -z "${CSI_MIN_OUTPUT_FILES}" ]]; then
    CSI_MIN_OUTPUT_FILES="1"
  fi
  if [[ -z "${CSI_TRAFFIC_ENABLE}" ]]; then
    CSI_TRAFFIC_ENABLE="true"
  fi
fi
if [[ "${CSI_TRAFFIC_ENABLE}" == "true" ]]; then
  if [[ -z "${CSI_TRAFFIC_IFACE}" ]]; then
    if [[ -n "${CSI_MEASURE_IFACE}" ]]; then
      CSI_TRAFFIC_IFACE="${CSI_MEASURE_IFACE}"
    elif [[ "${CONTROL_PLANE_IFACE}" == eth* || "${CONTROL_PLANE_IFACE}" == en* || "${CONTROL_PLANE_IFACE}" == enx* ]]; then
      CSI_TRAFFIC_IFACE="${WIFI_SCAN_IFACE}"
    else
      CSI_TRAFFIC_IFACE="${CONTROL_PLANE_IFACE}"
    fi
  fi
  if [[ -z "${CSI_TRAFFIC_TARGET}" ]]; then
    if [[ "${CSI_TRAFFIC_IFACE}" == "${CONTROL_PLANE_IFACE}" ]]; then
      CSI_TRAFFIC_TARGET="${FLEET_MANAGER_HOST}"
    fi
  fi
  if [[ -z "${CSI_TRAFFIC_INTERVAL_MS}" ]]; then
    CSI_TRAFFIC_INTERVAL_MS="100"
  fi
fi
if [[ -z "${CSI_LIVE_MONITOR_PATH}" ]]; then
  if [[ -n "${CSI_OUTPUT_PATH}" ]]; then
    CSI_LIVE_MONITOR_PATH="${CSI_OUTPUT_PATH}"
  else
    CSI_LIVE_MONITOR_PATH="${CSI_DEFAULT_OUTPUT_PATH}"
  fi
fi

SYSTEMD_AGENT_WAS_ACTIVE="false"

prepare_systemd_agent_for_manual_run() {
  local state=""
  if [[ "${MANAGE_SYSTEMD_AGENT}" != "true" ]]; then
    return 0
  fi
  state="$(
    run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
systemctl is-active \"${SYSTEMD_AGENT_SERVICE}\" 2>/dev/null || true
'" || true
  )"
  state="${state//$'\r'/}"
  state="${state//$'\n'/}"
  case "${state}" in
    active|activating|reloading)
      SYSTEMD_AGENT_WAS_ACTIVE="true"
      echo "Stopping ${SYSTEMD_AGENT_SERVICE} on ${PI_HOST} to avoid dual-agent contention..."
      run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
sudo -n systemctl stop \"${SYSTEMD_AGENT_SERVICE}\" || true
sudo -n systemctl reset-failed \"${SYSTEMD_AGENT_SERVICE}\" || true
'" || true
      ;;
    *)
      SYSTEMD_AGENT_WAS_ACTIVE="false"
      ;;
  esac
}

restore_systemd_agent_after_manual_run() {
  if [[ "${MANAGE_SYSTEMD_AGENT}" != "true" ]]; then
    return 0
  fi
  if [[ "${SYSTEMD_AGENT_WAS_ACTIVE}" != "true" ]]; then
    return 0
  fi
  echo "Restoring ${SYSTEMD_AGENT_SERVICE} on ${PI_HOST}..."
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
sudo -n systemctl start \"${SYSTEMD_AGENT_SERVICE}\" || true
'" || true
  SYSTEMD_AGENT_WAS_ACTIVE="false"
}

wait_for_ssh
init_recovery_hosts
if [[ "${PRECHECK_ONLY}" == "true" ]]; then
  echo "SSH precheck OK for ${PI_USER}@${PI_HOST}"
  exit 0
fi
trap restore_systemd_agent_after_manual_run EXIT

remote_iface_exists() {
  local iface="$1"
  if [[ -z "${iface}" ]]; then
    return 1
  fi
  if run_ssh "${PI_USER}@${PI_HOST}" "test -d /sys/class/net/${iface}" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

detect_active_wifi_profile() {
  local iface="$1"
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
iface=\"${iface}\"
if ! command -v nmcli >/dev/null 2>&1; then
  exit 0
fi
if [[ -n \"\${iface}\" ]]; then
  nmcli -t -f NAME,DEVICE connection show --active \
    | awk -F: -v ifc=\"\${iface}\" \"\\\$2==ifc {print \\\$1; exit}\" || true
else
  nmcli -t -f NAME,TYPE connection show --active \
    | awk -F: \"\\\$2==\\\"wifi\\\" {print \\\$1; exit}\" || true
fi
'"
}

resolve_control_plane_iface() {
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
target=\"${FLEET_MANAGER_HOST}\"
requested=\"${CONTROL_PLANE_IFACE}\"

if [[ -n \"\${requested}\" && -d /sys/class/net/\"\${requested}\" ]]; then
  printf \"%s\" \"\${requested}\"
  exit 0
fi

line=\$(ip route get \"\${target}\" 2>/dev/null | head -n1 || true)
route_iface=\$(printf \"%s\\n\" \"\${line}\" | sed -n \"s/.* dev \\([^ ]*\\).*/\\1/p\")
if [[ -n \"\${route_iface}\" && -d /sys/class/net/\"\${route_iface}\" ]]; then
  printf \"%s\" \"\${route_iface}\"
  exit 0
fi

for cand in eth0 en0 wlan0 wlan1; do
  if [[ -d /sys/class/net/\"\${cand}\" ]]; then
    printf \"%s\" \"\${cand}\"
    exit 0
  fi
done

fallback=\$(ls /sys/class/net 2>/dev/null | grep -v \"^lo$\" | head -n1 || true)
printf \"%s\" \"\${fallback}\"
'"
}

resolve_wifi_scan_iface() {
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
requested=\"${WIFI_SCAN_IFACE}\"
control_iface=\"${CONTROL_PLANE_IFACE}\"
wifi_rows=\$(iw dev 2>/dev/null | awk \"\\\$1==\\\"Interface\\\"{iface=\\\$2} \\\$1==\\\"type\\\"{print iface \\\" \\\" \\\$2}\" || true)
wifi_ifaces=\$(printf \"%s\\n\" \"\${wifi_rows}\" | awk \"{print \\\$1}\")

if [[ -n \"\${requested}\" ]]; then
  while IFS= read -r cand; do
    [[ -n \"\${cand}\" ]] || continue
    if [[ \"\${cand}\" == \"\${requested}\" ]]; then
      printf \"%s\" \"\${cand}\"
      exit 0
    fi
  done <<< \"\${wifi_ifaces}\"
fi

for pref in wlan0 wlan1; do
  while IFS= read -r cand; do
    [[ -n \"\${cand}\" ]] || continue
    if [[ \"\${cand}\" == \"\${pref}\" ]]; then
      printf \"%s\" \"\${cand}\"
      exit 0
    fi
  done <<< \"\${wifi_ifaces}\"
done

for want in managed station client; do
  cand=\$(printf \"%s\\n\" \"\${wifi_rows}\" | awk -v want=\"\${want}\" \"{t=tolower(\\\$2); if (t==want) {print \\\$1; exit}}\" || true)
  if [[ -n \"\${cand}\" ]]; then
    printf \"%s\" \"\${cand}\"
    exit 0
  fi
done

cand=\$(printf \"%s\\n\" \"\${wifi_rows}\" | awk \"{t=tolower(\\\$2); if (t==\\\"monitor\\\") {print \\\$1; exit}}\" || true)
if [[ -n \"\${cand}\" ]]; then
  printf \"%s\" \"\${cand}\"
  exit 0
fi

cand=\$(printf \"%s\\n\" \"\${wifi_rows}\" | awk \"{t=tolower(\\\$2); if (t!=\\\"ap\\\") {print \\\$1; exit}}\" || true)
if [[ -n \"\${cand}\" ]]; then
  printf \"%s\" \"\${cand}\"
  exit 0
fi

first_wifi=\$(printf \"%s\\n\" \"\${wifi_ifaces}\" | head -n1 || true)
if [[ -n \"\${first_wifi}\" ]]; then
  printf \"%s\" \"\${first_wifi}\"
  exit 0
fi

printf \"%s\" \"\${control_iface}\"
'"
}

resolve_agent_id() {
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
control_iface=\"${CONTROL_PLANE_IFACE}\"

if [[ -n \"\${control_iface}\" && -f /sys/class/net/\"\${control_iface}\"/address ]]; then
  cat /sys/class/net/\"\${control_iface}\"/address
  exit 0
fi

for cand in eth0 en0 wlan0 wlan1; do
  if [[ -f /sys/class/net/\"\${cand}\"/address ]]; then
    cat /sys/class/net/\"\${cand}\"/address
    exit 0
  fi
done

fallback=\$(ls /sys/class/net 2>/dev/null | grep -v \"^lo$\" | head -n1 || true)
if [[ -n \"\${fallback}\" && -f /sys/class/net/\"\${fallback}\"/address ]]; then
  cat /sys/class/net/\"\${fallback}\"/address
  exit 0
fi

if [[ -f /etc/machine-id ]]; then
  mid=\$(cat /etc/machine-id | tr -d \"\\r\\n\")
  printf \"machine-%s\" \"\${mid}\"
  exit 0
fi

hostname
'"
}

normalize_band_expect() {
  local raw="${1:-}"
  raw="$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  case "${raw}" in
    ""|"any")
      printf ''
      ;;
    "2.4"|"2.4ghz"|"2g"|"2ghz"|"24")
      printf '2.4'
      ;;
    "5"|"5ghz"|"5g")
      printf '5'
      ;;
    *)
      printf '%s' "${raw}"
      ;;
  esac
}

band_matches_expectation() {
  local freq="${1:-0}"
  local expect="${2:-}"
  expect="$(normalize_band_expect "${expect}")"
  if [[ -z "${expect}" ]]; then
    return 0
  fi
  if ! [[ "${freq}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  case "${expect}" in
    "2.4")
      (( freq >= 2400 && freq < 2500 ))
      ;;
    "5")
      (( freq >= 4900 && freq < 6000 ))
      ;;
    *)
      return 1
      ;;
  esac
}

remote_iface_freq_mhz() {
  local iface="${1:-}"
  if [[ -z "${iface}" ]]; then
    return 0
  fi
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
iface=\"${iface}\"
freq=\"\"
if [[ -n \"\${iface}\" ]]; then
  link_line=\$(iw dev \"\${iface}\" link 2>/dev/null | sed -n \"s/^\\s*freq: \\([0-9]\\+\\).*/\\1/p\" | head -n1 || true)
  if [[ -n \"\${link_line}\" ]]; then
    freq=\"\${link_line}\"
  fi
  if [[ -z \"\${freq}\" ]]; then
    info_line=\$(iw dev \"\${iface}\" info 2>/dev/null | sed -n \"s/^\\s*channel [0-9]\\+ (\\([0-9]\\+\\) MHz).*/\\1/p\" | head -n1 || true)
    if [[ -n \"\${info_line}\" ]]; then
      freq=\"\${info_line}\"
    fi
  fi
fi
printf \"%s\" \"\${freq}\"
'"
}

list_wifi_ifaces_with_freq() {
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
for iface in \$(iw dev 2>/dev/null | awk \"\\\$1==\\\"Interface\\\"{print \\\$2}\" || true); do
  freq=\$(iw dev \"\${iface}\" link 2>/dev/null | sed -n \"s/^\\s*freq: \\([0-9]\\+\\).*/\\1/p\" | head -n1 || true)
  if [[ -z \"\${freq}\" ]]; then
    freq=\$(iw dev \"\${iface}\" info 2>/dev/null | sed -n \"s/^\\s*channel [0-9]\\+ (\\([0-9]\\+\\) MHz).*/\\1/p\" | head -n1 || true)
  fi
  if [[ -z \"\${freq}\" ]]; then
    freq=\"unknown\"
  fi
  printf \"%s %s\\n\" \"\${iface}\" \"\${freq}\"
done
'"
}

resolve_wifi_iface_for_band() {
  local expect="${1:-}"
  local exclude_iface="${2:-}"
  expect="$(normalize_band_expect "${expect}")"
  if [[ -z "${expect}" ]]; then
    printf ''
    return 0
  fi
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
expect=\"${expect}\"
exclude_iface=\"${exclude_iface}\"
for iface in \$(iw dev 2>/dev/null | awk \"\\\$1==\\\"Interface\\\"{print \\\$2}\" || true); do
  if [[ -n \"\${exclude_iface}\" && \"\${iface}\" == \"\${exclude_iface}\" ]]; then
    continue
  fi
  freq=\$(iw dev \"\${iface}\" link 2>/dev/null | sed -n \"s/^\\s*freq: \\([0-9]\\+\\).*/\\1/p\" | head -n1 || true)
  if [[ -z \"\${freq}\" ]]; then
    freq=\$(iw dev \"\${iface}\" info 2>/dev/null | sed -n \"s/^\\s*channel [0-9]\\+ (\\([0-9]\\+\\) MHz).*/\\1/p\" | head -n1 || true)
  fi
  if ! [[ \"\${freq}\" =~ ^[0-9]+$ ]]; then
    continue
  fi
  case \"\${expect}\" in
    2.4)
      if (( freq >= 2400 && freq < 2500 )); then
        printf \"%s\" \"\${iface}\"
        exit 0
      fi
      ;;
    5)
      if (( freq >= 4900 && freq < 6000 )); then
        printf \"%s\" \"\${iface}\"
        exit 0
      fi
      ;;
  esac
done
printf \"\"
'"
}

print_wifi_iface_diag() {
  local rows=""
  rows="$(list_wifi_ifaces_with_freq || true)"
  if [[ -z "${rows}" ]]; then
    return 0
  fi
  echo "Detected Wi-Fi interfaces (iface freqMHz):"
  while IFS= read -r row; do
    [[ -n "${row}" ]] || continue
    echo "  - ${row}"
  done <<< "${rows}"
}

resolve_csi_measure_iface() {
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
requested=\"${CSI_MEASURE_IFACE}\"
control_iface=\"${CONTROL_PLANE_IFACE}\"
rows=\$(iw dev 2>/dev/null | awk \"\\\$1==\\\"Interface\\\"{iface=\\\$2} \\\$1==\\\"type\\\"{print iface \\\" \\\" tolower(\\\$2)}\" || true)

if [[ -n \"\${requested}\" && \"\${requested}\" != \"\${control_iface}\" && -d /sys/class/net/\"\${requested}\" ]]; then
  printf \"%s\" \"\${requested}\"
  exit 0
fi

cand=\"\"
for want in managed station monitor; do
  cand=\$(printf \"%s\\n\" \"\${rows}\" | awk -v control=\"\${control_iface}\" -v want=\"\${want}\" \"{if (\\\$1!=control && \\\$2==want) {print \\\$1; exit}}\" || true)
  if [[ -n \"\${cand}\" ]]; then
    printf \"%s\" \"\${cand}\"
    exit 0
  fi
done

cand=\$(printf \"%s\\n\" \"\${rows}\" | awk -v control=\"\${control_iface}\" \"{if (\\\$1!=control && \\\$1!=\"\") {print \\\$1; exit}}\" || true)
if [[ -n \"\${cand}\" ]]; then
  printf \"%s\" \"\${cand}\"
  exit 0
fi

printf \"\"
'"
}

detect_csi_iface_flag() {
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
if ! command -v feitcsi >/dev/null 2>&1; then
  echo none
  exit 0
fi
help=\$(feitcsi --help 2>&1 || true)
if printf \"%s\\n\" \"\${help}\" | grep -q -- \"--interface\"; then
  echo --interface
  exit 0
fi
if printf \"%s\\n\" \"\${help}\" | grep -q -- \"--device\"; then
  echo --device
  exit 0
fi
echo none
'"
}

ORIGINAL_CONTROL_PLANE_IFACE="${CONTROL_PLANE_IFACE}"
ORIGINAL_WIFI_SCAN_IFACE="${WIFI_SCAN_IFACE}"

if ! remote_iface_exists "${CONTROL_PLANE_IFACE}"; then
  if [[ "${STRICT_CONTROL_PLANE_IFACE}" == "true" ]]; then
    echo "ERROR: CONTROL_PLANE_IFACE=${CONTROL_PLANE_IFACE} not found on Pi and STRICT_CONTROL_PLANE_IFACE=true."
    exit 2
  else
    RESOLVED_CONTROL_PLANE_IFACE="$(resolve_control_plane_iface || true)"
    if [[ -n "${RESOLVED_CONTROL_PLANE_IFACE}" ]]; then
      echo "WARNING: CONTROL_PLANE_IFACE=${CONTROL_PLANE_IFACE} not found on Pi; using ${RESOLVED_CONTROL_PLANE_IFACE}."
      CONTROL_PLANE_IFACE="${RESOLVED_CONTROL_PLANE_IFACE}"
      if [[ "${CONTROL_PLANE_RESTART_IFACE}" == "${ORIGINAL_CONTROL_PLANE_IFACE}" ]]; then
        CONTROL_PLANE_RESTART_IFACE="${CONTROL_PLANE_IFACE}"
      fi
    fi
  fi
fi

if ! remote_iface_exists "${WIFI_SCAN_IFACE}"; then
  if [[ "${STRICT_WIFI_SCAN_IFACE}" == "true" ]]; then
    echo "ERROR: WIFI_SCAN_IFACE=${WIFI_SCAN_IFACE} not found on Pi and STRICT_WIFI_SCAN_IFACE=true."
    exit 2
  else
    RESOLVED_WIFI_SCAN_IFACE="$(resolve_wifi_scan_iface || true)"
    if [[ -n "${RESOLVED_WIFI_SCAN_IFACE}" ]]; then
      echo "WARNING: WIFI_SCAN_IFACE=${WIFI_SCAN_IFACE} not found on Pi; using ${RESOLVED_WIFI_SCAN_IFACE}."
      WIFI_SCAN_IFACE="${RESOLVED_WIFI_SCAN_IFACE}"
    fi
  fi
fi

if ! remote_iface_exists "${CONTROL_PLANE_IFACE}"; then
  echo "ERROR: CONTROL_PLANE_IFACE=${CONTROL_PLANE_IFACE} does not exist on Pi after resolution."
  exit 2
fi

if ! remote_iface_exists "${WIFI_SCAN_IFACE}"; then
  echo "ERROR: WIFI_SCAN_IFACE=${WIFI_SCAN_IFACE} does not exist on Pi after resolution."
  exit 2
fi

if [[ -n "${CONTROL_PLANE_BAND_EXPECT}" && "${AUTO_SWITCH_CONTROL_PLANE_IFACE}" == "true" ]]; then
  CURRENT_CONTROL_FREQ_MHZ="$(remote_iface_freq_mhz "${CONTROL_PLANE_IFACE}" || true)"
  if [[ -z "${CURRENT_CONTROL_FREQ_MHZ}" ]] || ! band_matches_expectation "${CURRENT_CONTROL_FREQ_MHZ}" "${CONTROL_PLANE_BAND_EXPECT}"; then
    RESOLVED_CONTROL_FOR_BAND="$(resolve_wifi_iface_for_band "${CONTROL_PLANE_BAND_EXPECT}" "" || true)"
    if [[ -n "${RESOLVED_CONTROL_FOR_BAND}" && "${RESOLVED_CONTROL_FOR_BAND}" != "${CONTROL_PLANE_IFACE}" ]]; then
      echo "WARNING: switching CONTROL_PLANE_IFACE from ${CONTROL_PLANE_IFACE} to ${RESOLVED_CONTROL_FOR_BAND} (expected band ${CONTROL_PLANE_BAND_EXPECT})."
      if [[ "${WIFI_SCAN_IFACE}" == "${CONTROL_PLANE_IFACE}" ]]; then
        WIFI_SCAN_IFACE="${RESOLVED_CONTROL_FOR_BAND}"
      fi
      if [[ "${CONTROL_PLANE_RESTART_IFACE}" == "${CONTROL_PLANE_IFACE}" ]]; then
        CONTROL_PLANE_RESTART_IFACE="${RESOLVED_CONTROL_FOR_BAND}"
      fi
      CONTROL_PLANE_IFACE="${RESOLVED_CONTROL_FOR_BAND}"
    fi
  fi
fi

if [[ "${CONTROL_PLANE_IFACE}" == eth* || "${CONTROL_PLANE_IFACE}" == en* || "${CONTROL_PLANE_IFACE}" == enx* ]]; then
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
iface=\"${CONTROL_PLANE_IFACE}\"
desired_ip=\"${CONTROL_PLANE_IPV4}\"
prefix=\"${CONTROL_PLANE_IPV4_PREFIX}\"
auto_linklocal=\"${AUTO_LINKLOCAL_ETHERNET_IPV4}\"

has_ipv4=false
if ip -4 -o addr show dev \"\${iface}\" | grep -q \"inet \"; then
  has_ipv4=true
fi

if [[ \"\${has_ipv4}\" == false ]]; then
  if [[ -z \"\${desired_ip}\" && \"\${auto_linklocal}\" == \"true\" ]]; then
    mac=\$(cat /sys/class/net/\"\${iface}\"/address 2>/dev/null || true)
    h1=\$(echo \"\${mac}\" | awk -F: \"{print \\\$5}\")
    h2=\$(echo \"\${mac}\" | awk -F: \"{print \\\$6}\")
    if [[ -n \"\${h1}\" && -n \"\${h2}\" ]]; then
      d1=\$((16#\${h1}))
      d2=\$((16#\${h2}))
      desired_ip=\"169.254.\${d1}.\${d2}\"
    fi
  fi
  if [[ -n \"\${desired_ip}\" ]]; then
    sudo ip addr replace \"\${desired_ip}/\${prefix}\" dev \"\${iface}\"
  fi
fi

ip -4 -o addr show dev \"\${iface}\" || true
'"
fi

if [[ "${ENFORCE_CONTROL_PLANE_ROUTE}" == "true" ]]; then
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
target=\"${FLEET_MANAGER_HOST}\"
iface=\"${CONTROL_PLANE_IFACE}\"
if [[ -n \"\${target}\" && -n \"\${iface}\" ]]; then
  if ip -4 addr show dev \"\${iface}\" | grep -q \"inet \"; then
    src=\$(ip -4 -o addr show dev \"\${iface}\" | awk \"{print \\\$4}\" | head -n1 | cut -d/ -f1)
    if [[ -n \"\${src}\" ]]; then
      sudo ip route replace \"\${target}\" dev \"\${iface}\" src \"\${src}\" metric 5 || true
    else
      sudo ip route replace \"\${target}\" dev \"\${iface}\" metric 5 || true
    fi
  else
    sudo ip route replace \"\${target}\" dev \"\${iface}\" metric 5 || true
  fi
fi
'"
fi

if [[ -z "${AGENT_ID}" ]]; then
  AGENT_ID="$(resolve_agent_id || true)"
fi

if [[ -z "${AGENT_ID}" ]]; then
  echo "ERROR: Failed to resolve AGENT_ID from Pi interfaces or machine id."
  exit 2
fi

ROUTE_IFACE="$(
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
target=\"${FLEET_MANAGER_HOST}\"
line=\$(ip route get \"\${target}\" 2>/dev/null | head -n1 || true)
iface=\$(printf \"%s\\n\" \"\${line}\" | sed -n \"s/.* dev \\([^ ]*\\).*/\\1/p\")
printf \"%s\" \"\${iface}\"
'"
)"

if [[ -n "${CONTROL_PLANE_BAND_EXPECT}" ]]; then
  CONTROL_FREQ_MHZ="$(remote_iface_freq_mhz "${CONTROL_PLANE_IFACE}" || true)"
  if [[ -z "${CONTROL_FREQ_MHZ}" ]]; then
    echo "ERROR: CONTROL_PLANE_BAND_EXPECT=${CONTROL_PLANE_BAND_EXPECT} but could not determine freq for ${CONTROL_PLANE_IFACE}."
    print_wifi_iface_diag
    exit 2
  fi
  if ! band_matches_expectation "${CONTROL_FREQ_MHZ}" "${CONTROL_PLANE_BAND_EXPECT}"; then
    echo "ERROR: control iface ${CONTROL_PLANE_IFACE} frequency ${CONTROL_FREQ_MHZ} MHz does not match expected band ${CONTROL_PLANE_BAND_EXPECT}."
    if [[ "${AUTO_SWITCH_CONTROL_PLANE_IFACE}" != "true" ]]; then
      echo "Hint: set AUTO_SWITCH_CONTROL_PLANE_IFACE=true to auto-select a Wi-Fi iface for the expected band when available."
    fi
    print_wifi_iface_diag
    exit 2
  fi
  echo "Control-plane band check OK: iface=${CONTROL_PLANE_IFACE} freq=${CONTROL_FREQ_MHZ}MHz expect=${CONTROL_PLANE_BAND_EXPECT}"
fi

if [[ "${CSI_REQUIRE_SEPARATE_IFACE}" == "true" || -n "${CSI_MEASURE_IFACE}" ]]; then
  if [[ -n "${CSI_MEASURE_IFACE}" ]] && ! remote_iface_exists "${CSI_MEASURE_IFACE}"; then
    echo "WARNING: requested CSI_MEASURE_IFACE=${CSI_MEASURE_IFACE} does not exist on Pi; attempting auto-resolution."
    CSI_MEASURE_IFACE=""
  fi
  if [[ -z "${CSI_MEASURE_IFACE}" && -n "${CSI_MEASURE_BAND_EXPECT}" ]]; then
    CSI_MEASURE_IFACE="$(resolve_wifi_iface_for_band "${CSI_MEASURE_BAND_EXPECT}" "${CONTROL_PLANE_IFACE}" || true)"
  fi
  if [[ -z "${CSI_MEASURE_IFACE}" ]]; then
    CSI_MEASURE_IFACE="$(resolve_csi_measure_iface || true)"
  fi
  if [[ -n "${CSI_MEASURE_IFACE}" ]] && ! remote_iface_exists "${CSI_MEASURE_IFACE}"; then
    echo "ERROR: resolved CSI_MEASURE_IFACE=${CSI_MEASURE_IFACE} does not exist on Pi."
    print_wifi_iface_diag
    exit 2
  fi
  if [[ -z "${CSI_MEASURE_IFACE}" ]]; then
    echo "ERROR: CSI requires separate iface but no alternate Wi-Fi iface was found."
    print_wifi_iface_diag
    echo "Recommended options: add second Wi-Fi adapter, or use Ethernet control-plane for stable CSI."
    exit 2
  fi
  if [[ "${CSI_MEASURE_IFACE}" == "${CONTROL_PLANE_IFACE}" ]]; then
    echo "ERROR: CSI_MEASURE_IFACE (${CSI_MEASURE_IFACE}) must differ from CONTROL_PLANE_IFACE (${CONTROL_PLANE_IFACE})."
    print_wifi_iface_diag
    echo "Recommended options: add second Wi-Fi adapter, or run CSI with Ethernet control-plane."
    exit 2
  fi
fi

if [[ -n "${CSI_MEASURE_IFACE}" && -n "${CSI_MEASURE_BAND_EXPECT}" ]]; then
  CSI_FREQ_MHZ="$(remote_iface_freq_mhz "${CSI_MEASURE_IFACE}" || true)"
  if [[ -z "${CSI_FREQ_MHZ}" ]]; then
    echo "WARNING: could not determine current frequency for CSI iface ${CSI_MEASURE_IFACE} before capture."
  elif ! band_matches_expectation "${CSI_FREQ_MHZ}" "${CSI_MEASURE_BAND_EXPECT}"; then
    if [[ "${AUTO_SWITCH_CSI_MEASURE_IFACE}" == "true" ]]; then
      RESOLVED_CSI_FOR_BAND="$(resolve_wifi_iface_for_band "${CSI_MEASURE_BAND_EXPECT}" "${CONTROL_PLANE_IFACE}" || true)"
      if [[ -n "${RESOLVED_CSI_FOR_BAND}" && "${RESOLVED_CSI_FOR_BAND}" != "${CSI_MEASURE_IFACE}" ]]; then
        echo "WARNING: switching CSI_MEASURE_IFACE from ${CSI_MEASURE_IFACE} to ${RESOLVED_CSI_FOR_BAND} (expected band ${CSI_MEASURE_BAND_EXPECT})."
        CSI_MEASURE_IFACE="${RESOLVED_CSI_FOR_BAND}"
        CSI_FREQ_MHZ="$(remote_iface_freq_mhz "${CSI_MEASURE_IFACE}" || true)"
      fi
    fi
    if [[ -n "${CSI_FREQ_MHZ}" ]] && ! band_matches_expectation "${CSI_FREQ_MHZ}" "${CSI_MEASURE_BAND_EXPECT}"; then
      if [[ "${CSI_REQUIRE_SEPARATE_IFACE}" == "true" ]]; then
        echo "ERROR: CSI iface ${CSI_MEASURE_IFACE} currently at ${CSI_FREQ_MHZ} MHz, expected ${CSI_MEASURE_BAND_EXPECT} band."
        print_wifi_iface_diag
        exit 2
      fi
      echo "WARNING: CSI iface ${CSI_MEASURE_IFACE} currently at ${CSI_FREQ_MHZ} MHz, expected ${CSI_MEASURE_BAND_EXPECT} band."
    fi
  else
    echo "CSI iface band check OK: iface=${CSI_MEASURE_IFACE} freq=${CSI_FREQ_MHZ}MHz expect=${CSI_MEASURE_BAND_EXPECT}"
  fi
fi

if [[ -n "${CSI_MEASURE_IFACE}" ]]; then
  if [[ "${CSI_COLLECTOR_CMD}" != *"--interface "* && "${CSI_COLLECTOR_CMD}" != *"--device "* ]]; then
    CSI_IFACE_FLAG_RESOLVED="${CSI_IFACE_FLAG}"
    if [[ "${CSI_IFACE_FLAG_RESOLVED}" == "auto" ]]; then
      CSI_IFACE_FLAG_RESOLVED="$(detect_csi_iface_flag || true)"
    fi
    case "${CSI_IFACE_FLAG_RESOLVED}" in
      --interface|--device)
        CSI_COLLECTOR_CMD="${CSI_COLLECTOR_CMD} ${CSI_IFACE_FLAG_RESOLVED} ${CSI_MEASURE_IFACE}"
        echo "CSI collector iface pinning: ${CSI_IFACE_FLAG_RESOLVED} ${CSI_MEASURE_IFACE}"
        ;;
      none|"")
        if [[ "${CSI_REQUIRE_SEPARATE_IFACE}" == "true" ]]; then
          echo "ERROR: CSI requires separate iface (${CSI_MEASURE_IFACE}) but feitcsi iface flag was not detected."
          echo "Set CSI_IFACE_FLAG manually (for example --interface) or update collector command."
          exit 2
        fi
        echo "WARNING: could not auto-detect feitcsi iface flag; collector command left unchanged."
        ;;
      *)
        echo "ERROR: unsupported CSI_IFACE_FLAG='${CSI_IFACE_FLAG_RESOLVED}'. Use auto, --interface, --device, or none."
        exit 2
        ;;
    esac
  fi
fi

if [[ "${REQUIRE_WIFI_CONTROL}" == "true" ]]; then
  if [[ -z "${ROUTE_IFACE}" ]]; then
    echo "ERROR: REQUIRE_WIFI_CONTROL=true but route interface to Fleet host could not be determined."
    exit 2
  fi
  ROUTE_IS_WIFI="$(
    run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
iface=\"${ROUTE_IFACE}\"
if iw dev 2>/dev/null | awk \"\\\$1==\\\"Interface\\\"{print \\\$2}\" | grep -Fxq \"\${iface}\"; then
  echo true
else
  echo false
fi
'"
  )"
  ROUTE_IS_WIFI="${ROUTE_IS_WIFI//$'\r'/}"
  ROUTE_IS_WIFI="${ROUTE_IS_WIFI//$'\n'/}"
  if [[ "${ROUTE_IS_WIFI}" != "true" ]]; then
    echo "ERROR: REQUIRE_WIFI_CONTROL=true but Fleet route uses non-Wi-Fi iface: ${ROUTE_IFACE}"
    exit 2
  fi
fi

if [[ "${REQUIRE_REAL_CSI}" == "true" && -n "${ROUTE_IFACE}" && "${ROUTE_IFACE}" == wl* ]]; then
  if [[ "${ALLOW_WIFI_DISRUPTIVE_CSI}" == "true" ]]; then
    echo "WARNING: REQUIRE_REAL_CSI=true while Fleet route uses ${ROUTE_IFACE}."
    echo "CSI may disrupt SSH/control-plane on single-Wi-Fi setups."
  else
    echo "ERROR: REQUIRE_REAL_CSI=true but Fleet route uses ${ROUTE_IFACE}."
    echo "Use Ethernet for control-plane, or set ALLOW_WIFI_DISRUPTIVE_CSI=true if you accept SSH drops."
    exit 2
  fi
fi

if [[ "${REQUIRE_ETHERNET_CONTROL}" == "true" ]]; then
  if [[ -z "${ROUTE_IFACE}" ]]; then
    echo "ERROR: REQUIRE_ETHERNET_CONTROL=true but route interface to Fleet host could not be determined."
    exit 2
  fi
  case "${ROUTE_IFACE}" in
    eth*|en*|enx*)
      ;;
    *)
      echo "ERROR: REQUIRE_ETHERNET_CONTROL=true but Fleet route uses non-Ethernet iface: ${ROUTE_IFACE}"
      exit 2
      ;;
  esac
fi

if [[ "${RESTART_CONTROL_PLANE_BEFORE_REPORT}" == "true" && -n "${ROUTE_IFACE}" && "${ROUTE_IFACE}" == wl* ]]; then
  if [[ "${ALLOW_WIFI_CONTROL_PLANE_RESTART}" == "true" ]]; then
    echo "WARNING: forcing RESTART_CONTROL_PLANE_BEFORE_REPORT=true on Wi-Fi route iface=${ROUTE_IFACE}."
  elif [[ -z "${CONTROL_PLANE_RESTART_CMD}" ]]; then
    echo "Safety: disabling RESTART_CONTROL_PLANE_BEFORE_REPORT on Wi-Fi route iface=${ROUTE_IFACE}."
    echo "Set ALLOW_WIFI_CONTROL_PLANE_RESTART=true to force hard iface restart on Wi-Fi."
    RESTART_CONTROL_PLANE_BEFORE_REPORT="false"
  fi
fi

if [[ -z "${DISABLE_CSI_CAPTURE}" ]]; then
  if [[ "${ALLOW_WIFI_DISRUPTIVE_CSI}" != "true" ]]; then
    # Safe default: avoid disruptive CSI capture whenever current Fleet route uses Wi-Fi.
    # This is stricter than CONTROL_PLANE_IFACE matching and protects mixed/overridden setups.
    if [[ -n "${ROUTE_IFACE}" && "${ROUTE_IFACE}" == wl* ]]; then
      DISABLE_CSI_CAPTURE="true"
      echo "Safety: Fleet route currently uses ${ROUTE_IFACE}; setting DISABLE_CSI_CAPTURE=true."
      echo "Set ALLOW_WIFI_DISRUPTIVE_CSI=true to force CSI capture while routed over Wi-Fi."
    fi
  fi
fi

if [[ "${REQUIRE_REAL_CSI}" == "true" && "${DISABLE_CSI_CAPTURE}" == "true" ]]; then
  echo "ERROR: REQUIRE_REAL_CSI=true but DISABLE_CSI_CAPTURE=true after safety evaluation."
  exit 2
fi

if [[ "${REQUIRE_REAL_CSI}" == "true" && "${CSI_ALLOW_THROTTLED}" != "true" ]]; then
  THROTTLED_HEX="$(
    run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
if command -v vcgencmd >/dev/null 2>&1; then
  vcgencmd get_throttled | sed -n \"s/^throttled=0x\\([0-9a-fA-F]\\+\\)$/\\1/p\"
fi
'"
  )"
  THROTTLED_HEX="${THROTTLED_HEX//$'\r'/}"
  THROTTLED_HEX="${THROTTLED_HEX//$'\n'/}"
  if [[ -n "${THROTTLED_HEX}" ]]; then
    THROTTLED_HEX="${THROTTLED_HEX#0x}"
    if [[ "${THROTTLED_HEX}" =~ ^[0-9a-fA-F]+$ ]]; then
      THROTTLED_VAL=$((16#${THROTTLED_HEX}))
      if (( (THROTTLED_VAL & 0x1) != 0 || (THROTTLED_VAL & 0x4) != 0 || (THROTTLED_VAL & 0x10000) != 0 || (THROTTLED_VAL & 0x40000) != 0 )); then
        echo "ERROR: REQUIRE_REAL_CSI=true but Pi power health is throttled/undervoltage (throttled=0x${THROTTLED_HEX})."
        echo "Fix PSU/cable/hub power stability, then retry."
        echo "Override only if intentional: CSI_ALLOW_THROTTLED=true"
        exit 2
      fi
    fi
  fi
fi

if [[ "${SINGLE_RADIO_MODE}" == "true" && ( -z "${SINGLE_RADIO_WIFI_PROFILE}" || "${SINGLE_RADIO_WIFI_PROFILE}" == "auto" ) ]]; then
  DETECTED_WIFI_PROFILE="$(detect_active_wifi_profile "${CONTROL_PLANE_IFACE}" || true)"
  DETECTED_WIFI_PROFILE="${DETECTED_WIFI_PROFILE//$'\r'/}"
  DETECTED_WIFI_PROFILE="${DETECTED_WIFI_PROFILE//$'\n'/}"
  if [[ -z "${DETECTED_WIFI_PROFILE}" && -n "${ROUTE_IFACE}" && "${ROUTE_IFACE}" == wl* ]]; then
    DETECTED_WIFI_PROFILE="$(detect_active_wifi_profile "${ROUTE_IFACE}" || true)"
    DETECTED_WIFI_PROFILE="${DETECTED_WIFI_PROFILE//$'\r'/}"
    DETECTED_WIFI_PROFILE="${DETECTED_WIFI_PROFILE//$'\n'/}"
  fi
  if [[ -n "${DETECTED_WIFI_PROFILE}" ]]; then
    SINGLE_RADIO_WIFI_PROFILE="${DETECTED_WIFI_PROFILE}"
    echo "Auto-detected SINGLE_RADIO_WIFI_PROFILE=${SINGLE_RADIO_WIFI_PROFILE}"
  else
    echo "WARNING: SINGLE_RADIO_WIFI_PROFILE=auto but active Wi-Fi profile could not be detected."
  fi
fi

prepare_systemd_agent_for_manual_run

echo "Running one-cycle smoke test on ${PI_USER}@${PI_HOST}"
if [[ "${CSI_TRAFFIC_ENABLE}" == "true" ]]; then
  echo "CSI traffic trigger: iface=${CSI_TRAFFIC_IFACE} target=${CSI_TRAFFIC_TARGET} interval_ms=${CSI_TRAFFIC_INTERVAL_MS}"
fi
if [[ "${CSI_LIVE_MONITOR_ENABLE}" == "true" ]]; then
  echo "CSI live monitor enabled: path=${CSI_LIVE_MONITOR_PATH}"
fi
RUN_TOKEN="smoke-$(date +%s)-$$"
REMOTE_LOG="/tmp/${RUN_TOKEN}.log"
REMOTE_EXIT="/tmp/${RUN_TOKEN}.exit"
REMOTE_PID="/tmp/${RUN_TOKEN}.pid"

run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
rm -f \"${REMOTE_LOG}\" \"${REMOTE_EXIT}\" \"${REMOTE_PID}\"
cd \"${PI_DIR}\"
nohup bash -lc \"set +e
cd \\\"${PI_DIR}\\\"
FLEET_MANAGER_HOST=\\\"${FLEET_MANAGER_HOST}\\\" \\
FLEET_MANAGER_PORT=\\\"${FLEET_MANAGER_PORT}\\\" \\
AGENT_ID=\\\"${AGENT_ID}\\\" \\
CONTROL_PLANE_MODE=\\\"${CONTROL_PLANE_MODE}\\\" \\
CONTROL_PLANE_IFACE=\\\"${CONTROL_PLANE_IFACE}\\\" \\
WIFI_SCAN_IFACE=\\\"${WIFI_SCAN_IFACE}\\\" \\
WIFI_SAMPLE_MIN_INTERVAL_S=\\\"${WIFI_SAMPLE_MIN_INTERVAL_S}\\\" \\
WIFI_SAMPLE_MAX_DURATION_S=\\\"${WIFI_SAMPLE_MAX_DURATION_S}\\\" \\
WIFI_SAMPLE_MAX_POINTS=\\\"${WIFI_SAMPLE_MAX_POINTS}\\\" \\
ENABLE_HTTP_SAMPLE_INGEST=\\\"${ENABLE_HTTP_SAMPLE_INGEST}\\\" \\
ENABLE_ELAB_ARTIFACT_UPLOAD=\\\"${ENABLE_ELAB_ARTIFACT_UPLOAD}\\\" \\
ARTIFACT_UPLOAD_MAX_BYTES=\\\"${ARTIFACT_UPLOAD_MAX_BYTES}\\\" \\
ARTIFACT_UPLOAD_TARGET=\\\"${ARTIFACT_UPLOAD_TARGET}\\\" \\
ARTIFACT_UPLOAD_DURING_MEASURE=\\\"${ARTIFACT_UPLOAD_DURING_MEASURE}\\\" \\
ARTIFACT_EVICT_AFTER_UPLOAD=\\\"${ARTIFACT_EVICT_AFTER_UPLOAD}\\\" \\
ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S=\\\"${ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S}\\\" \\
ARTIFACT_UPLOAD_BACKOFF_S=\\\"${ARTIFACT_UPLOAD_BACKOFF_S}\\\" \\
RUN_ARTIFACT_SOFT_LIMIT_BYTES=\\\"${RUN_ARTIFACT_SOFT_LIMIT_BYTES}\\\" \\
UPLOAD_WINDOW_FROM=\\\"${UPLOAD_WINDOW_FROM}\\\" \\
UPLOAD_WINDOW_TO=\\\"${UPLOAD_WINDOW_TO}\\\" \\
UPLOAD_WINDOW_REQUIRED=\\\"${UPLOAD_WINDOW_REQUIRED}\\\" \\
UPLOAD_SLOT_COUNT=\\\"${UPLOAD_SLOT_COUNT}\\\" \\
UPLOAD_SLOT_JITTER_S=\\\"${UPLOAD_SLOT_JITTER_S}\\\" \\
PROM_REMOTE_WRITE_ON_CMD=\\\"${PROM_REMOTE_WRITE_ON_CMD}\\\" \\
PROM_REMOTE_WRITE_OFF_CMD=\\\"${PROM_REMOTE_WRITE_OFF_CMD}\\\" \\
PROM_REMOTE_WRITE_CMD_TIMEOUT_S=\\\"${PROM_REMOTE_WRITE_CMD_TIMEOUT_S}\\\" \\
REPORT_RPC_TIMEOUT_S=\\\"${REPORT_RPC_TIMEOUT_S}\\\" \\
CONTROL_RPC_TIMEOUT_S=\\\"${CONTROL_RPC_TIMEOUT_S}\\\" \\
SEPARATE_MEASURE_AND_REPORT=\\\"${SEPARATE_MEASURE_AND_REPORT}\\\" \\
RESTART_CONTROL_PLANE_BEFORE_REPORT=\\\"${RESTART_CONTROL_PLANE_BEFORE_REPORT}\\\" \\
CONTROL_PLANE_RESTART_IFACE=\\\"${CONTROL_PLANE_RESTART_IFACE}\\\" \\
CONTROL_PLANE_RESTART_DOWN_S=\\\"${CONTROL_PLANE_RESTART_DOWN_S}\\\" \\
CONTROL_PLANE_RESTART_WAIT_S=\\\"${CONTROL_PLANE_RESTART_WAIT_S}\\\" \\
CONTROL_PLANE_RESTART_CMD=\\\"${CONTROL_PLANE_RESTART_CMD}\\\" \\
ALLOW_WIFI_CONTROL_PLANE_RESTART=\\\"${ALLOW_WIFI_CONTROL_PLANE_RESTART}\\\" \\
SINGLE_RADIO_MODE=\\\"${SINGLE_RADIO_MODE}\\\" \\
SINGLE_RADIO_WIFI_PROFILE=\\\"${SINGLE_RADIO_WIFI_PROFILE}\\\" \\
SINGLE_RADIO_RECOVERY_RETRIES=\\\"${SINGLE_RADIO_RECOVERY_RETRIES}\\\" \\
SINGLE_RADIO_RECOVERY_ROUTE_WAIT_S=\\\"${SINGLE_RADIO_RECOVERY_ROUTE_WAIT_S}\\\" \\
SINGLE_RADIO_RECOVERY_CMD=\\\"${SINGLE_RADIO_RECOVERY_CMD}\\\" \\
SINGLE_RADIO_ROUTE_GATE=\\\"${SINGLE_RADIO_ROUTE_GATE}\\\" \\
SINGLE_RADIO_ROUTE_HEALTH_TIMEOUT_S=\\\"${SINGLE_RADIO_ROUTE_HEALTH_TIMEOUT_S}\\\" \\
SINGLE_RADIO_REBOOT_ON_RECOVERY_FAIL=\\\"${SINGLE_RADIO_REBOOT_ON_RECOVERY_FAIL}\\\" \\
DISABLE_CSI_CAPTURE=\\\"${DISABLE_CSI_CAPTURE}\\\" \\
CSI_COLLECTOR_CMD=\\\"${CSI_COLLECTOR_CMD}\\\" \\
CSI_MEASURE_IFACE=\\\"${CSI_MEASURE_IFACE}\\\" \\
CONTROL_PLANE_BAND_EXPECT=\\\"${CONTROL_PLANE_BAND_EXPECT}\\\" \\
CSI_MEASURE_BAND_EXPECT=\\\"${CSI_MEASURE_BAND_EXPECT}\\\" \\
CSI_REQUIRE_SEPARATE_IFACE=\\\"${CSI_REQUIRE_SEPARATE_IFACE}\\\" \\
CSI_OUTPUT_PATH=\\\"${CSI_OUTPUT_PATH}\\\" \\
CSI_OUTPUT_GLOB=\\\"${CSI_OUTPUT_GLOB}\\\" \\
CSI_FRAMES_REGEX=\\\"${CSI_FRAMES_REGEX}\\\" \\
CSI_OUTPUT_MAX_FILES=\\\"${CSI_OUTPUT_MAX_FILES}\\\" \\
CSI_PARSE_MAX_BYTES=\\\"${CSI_PARSE_MAX_BYTES}\\\" \\
CSI_REQUIRE_EVIDENCE=\\\"${CSI_REQUIRE_EVIDENCE}\\\" \\
CSI_MIN_FRAMES=\\\"${CSI_MIN_FRAMES}\\\" \\
CSI_MIN_OUTPUT_FILES=\\\"${CSI_MIN_OUTPUT_FILES}\\\" \\
CSI_SUDO_NONINTERACTIVE=\\\"${CSI_SUDO_NONINTERACTIVE}\\\" \\
CSI_TRAFFIC_ENABLE=\\\"${CSI_TRAFFIC_ENABLE}\\\" \\
CSI_TRAFFIC_TARGET=\\\"${CSI_TRAFFIC_TARGET}\\\" \\
CSI_TRAFFIC_IFACE=\\\"${CSI_TRAFFIC_IFACE}\\\" \\
CSI_TRAFFIC_INTERVAL_MS=\\\"${CSI_TRAFFIC_INTERVAL_MS}\\\" \\
CSI_HARD_TIMEOUT_S=\\\"${CSI_HARD_TIMEOUT_S}\\\" \\
CSI_CLEANUP_BEFORE_CAPTURE=\\\"${CSI_CLEANUP_BEFORE_CAPTURE}\\\" \\
CSI_CLEANUP_AFTER_CAPTURE=\\\"${CSI_CLEANUP_AFTER_CAPTURE}\\\" \\
CSI_KILL_PATTERNS=\\\"${CSI_KILL_PATTERNS}\\\" \\
MAX_SYNC_CYCLES=\\\"${MAX_SYNC_CYCLES}\\\" \\
EXECUTE_POLICY=\\\"${EXECUTE_POLICY}\\\" \\
\\\"${PI_DIR}/venv/bin/python\\\" -u \\\"${PI_DIR}/agent_v2_client.py\\\"
rc=\\\$?
echo \\\"\\\${rc}\\\" > \\\"${REMOTE_EXIT}\\\"
exit \\\"\\\${rc}\\\"
\" >\"${REMOTE_LOG}\" 2>&1 < /dev/null &
echo \"\$!\" > \"${REMOTE_PID}\"
echo \"STARTED pid=\$(cat \"${REMOTE_PID}\") log=${REMOTE_LOG} exit=${REMOTE_EXIT}\"
'"

audit_latest_sent_run() {
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
data_root=\"${PI_DIR}/data\"
sent_root=\"\${data_root}/sent\"
pending_root=\"\${data_root}/pending\"
latest_sent=\$(ls -1t \"\${sent_root}\" 2>/dev/null | head -n1 || true)
pending_count=0
if [[ -d \"\${pending_root}\" ]]; then
  pending_count=\$(find \"\${pending_root}\" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d \" \\t\\r\\n\")
fi
echo \"Pi audit: pending_runs=\${pending_count}\"
if [[ -z \"\${latest_sent}\" ]]; then
  echo \"Pi audit: no sent runs found under \${sent_root}\"
  exit 0
fi
run_dir=\"\${sent_root}/\${latest_sent}\"
art_dir=\"\${run_dir}/artifacts\"
echo \"Pi audit: latest_sent_run=\${latest_sent}\"
if [[ -d \"\${art_dir}\" ]]; then
  echo \"Pi audit: artifacts (bytes, name):\"
  shopt -s nullglob
  for f in \"\${art_dir}\"/*; do
    [[ -f \"\${f}\" ]] || continue
    bytes=\$(wc -c < \"\${f}\" | tr -d \" \\t\\r\\n\")
    printf \"%10s %s\\n\" \"\${bytes}\" \"\$(basename \"\${f}\")\"
  done | sort
else
  echo \"Pi audit: artifacts directory missing: \${art_dir}\"
fi
if [[ -f \"\${art_dir}/run-summary.json\" ]]; then
  echo \"Pi audit: run-summary key metrics:\"
  grep -E \"\\\"run_id\\\"|\\\"status\\\"|\\\"wifi_sampling_duration_s_requested\\\"|\\\"wifi_sampling_duration_s_applied\\\"|\\\"wifi_sampling_points_applied\\\"|\\\"wifi_scan_ok\\\"|\\\"ble_scan_ok\\\"|\\\"csi_capture_ok\\\"|\\\"csi_frames_total\\\"\" \"\${art_dir}/run-summary.json\" || true
fi
'"
}

recover_wifi_iface_after_run() {
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
iface=\"${WIFI_SCAN_IFACE}\"
iw_bin=\"/usr/sbin/iw\"
if [[ -z \"\${iface}\" ]]; then
  exit 0
fi
if ! ip link show \"\${iface}\" >/dev/null 2>&1; then
  phy=\$(\${iw_bin} phy 2>/dev/null | awk \"/^Wiphy /{print \\\$2; exit}\")
  if [[ -n \"\${phy}\" ]]; then
    sudo -n \${iw_bin} phy \"\${phy}\" interface add \"\${iface}\" type managed || true
  fi
fi
sudo -n ip link set \"\${iface}\" up || true
if command -v nmcli >/dev/null 2>&1; then
  profile=\$(nmcli -g GENERAL.CONNECTION device show \"\${iface}\" 2>/dev/null | head -n1 | tr -d \"\\r\")
  if [[ -z \"\${profile}\" || \"\${profile}\" == \"--\" || \"\${profile}\" == \"(unknown)\" || \"\${profile}\" == \"(null)\" ]]; then
    profile=\$(nmcli -t -f NAME,TYPE connection show 2>/dev/null | awk -F: \"\\\$2 ~ /wifi|802-11-wireless/ {print \\\$1; exit}\")
  fi
  if [[ -n \"\${profile}\" && \"\${profile}\" != \"--\" ]]; then
    sudo -n nmcli connection up \"\${profile}\" ifname \"\${iface}\" >/dev/null 2>&1 || true
  fi
fi
\${iw_bin} dev \"\${iface}\" link 2>/dev/null || true
'"
}

finalize_remote_run() {
  local rc="$1"
  echo "Remote agent run finished with exit code ${rc}."
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc 'echo \"----- agent log tail (${REMOTE_LOG}) -----\"; tail -n 120 \"${REMOTE_LOG}\" || true'" || true
  if [[ "${CSI_LIVE_MONITOR_ENABLE}" == "true" && -n "${CSI_LIVE_MONITOR_PATH}" ]]; then
    run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
csi_path=\"${CSI_LIVE_MONITOR_PATH}\"
if [[ -f \"\${csi_path}\" ]]; then
  bytes=\$(wc -c < \"\${csi_path}\" | tr -d \" \\t\\r\\n\")
  echo \"CSI final file: path=\${csi_path} bytes=\${bytes}\"
else
  echo \"CSI final file: missing path=\${csi_path}\"
fi
'" || true
  fi
if [[ "${rc}" != "0" ]]; then
    recover_wifi_iface_after_run || true
    exit "${rc}"
  fi
  recover_wifi_iface_after_run || true
  if [[ "${POST_RUN_AUDIT}" == "true" ]]; then
    audit_latest_sent_run || true
  fi
}

START_EPOCH="$(date +%s)"
LAST_SSH_OK_EPOCH="${START_EPOCH}"
LAST_CSI_LIVE_BYTES=""
while true; do
  STATUS_OUT=""
  FALLBACK_OUT=""
  if STATUS_OUT="$(
    run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
if [[ -f \"${REMOTE_EXIT}\" ]]; then
  rc=\$(cat \"${REMOTE_EXIT}\" | tr -d \"\\r\\n\")
  echo \"DONE:\${rc}\"
  exit 0
fi
pid=\$(cat \"${REMOTE_PID}\" 2>/dev/null || true)
if [[ -n \"\${pid}\" ]] && kill -0 \"\${pid}\" 2>/dev/null; then
  if [[ \"${CSI_LIVE_MONITOR_ENABLE}\" == \"true\" ]]; then
    csi_path=\"${CSI_LIVE_MONITOR_PATH}\"
    csi_bytes=\"-1\"
    if [[ -n \"\${csi_path}\" && -f \"\${csi_path}\" ]]; then
      csi_bytes=\$(wc -c < \"\${csi_path}\" | tr -d \" \\t\\r\\n\")
    fi
    echo \"RUNNING:\${pid}:csi_bytes=\${csi_bytes}\"
  else
    echo \"RUNNING:\${pid}\"
  fi
else
  if [[ -f "${REMOTE_LOG}" ]] && grep -q "stored and reported" "${REMOTE_LOG}" 2>/dev/null; then
    echo "DONE:0"
    exit 0
  fi
  echo \"WAITING\"
fi
'"
  )"; then
    LAST_SSH_OK_EPOCH="$(date +%s)"
    if [[ "${STATUS_OUT}" == DONE:* ]]; then
      RC="${STATUS_OUT#DONE:}"
      finalize_remote_run "${RC}"
      break
    fi
    if [[ "${CSI_LIVE_MONITOR_ENABLE}" == "true" && "${STATUS_OUT}" == RUNNING:*:csi_bytes=* ]]; then
      CSI_LIVE_BYTES="${STATUS_OUT##*:csi_bytes=}"
      if [[ "${CSI_LIVE_BYTES}" != "${LAST_CSI_LIVE_BYTES}" ]]; then
        if [[ "${CSI_LIVE_BYTES}" == "-1" ]]; then
          echo "CSI live: ${CSI_LIVE_MONITOR_PATH} not created yet."
        else
          echo "CSI live: ${CSI_LIVE_MONITOR_PATH} bytes=${CSI_LIVE_BYTES}"
        fi
        LAST_CSI_LIVE_BYTES="${CSI_LIVE_BYTES}"
      fi
    fi
  else
    echo "SSH dropped while waiting for remote agent completion; retrying..."
    FALLBACK_OUT=""
    for host in "${RECOVERY_HOSTS[@]:-}"; do
      FALLBACK_OUT="$(
      ssh "${FALLBACK_SSH_ARGS[@]}" "${PI_USER}@${host}" "bash -lc '
set -euo pipefail
if [[ -f \"${REMOTE_EXIT}\" ]]; then
  rc=\$(cat \"${REMOTE_EXIT}\" | tr -d \"\\r\\n\")
  echo \"DONE:\${rc}\"
elif [[ -f \"${REMOTE_LOG}\" ]] && grep -q \"stored and reported\" \"${REMOTE_LOG}\" 2>/dev/null; then
  echo \"DONE:0\"
else
  echo \"ONLINE\"
fi
'" 2>/dev/null || true
      )"
      if [[ -n "${FALLBACK_OUT}" ]]; then
        LAST_SSH_OK_EPOCH="$(date +%s)"
        if [[ "${host}" != "${PI_HOST}" ]]; then
          echo "Recovered SSH via fallback host ${host}."
          PI_HOST="${host}"
          init_recovery_hosts
        fi
        break
      fi
    done
    if [[ "${FALLBACK_OUT}" == DONE:* ]]; then
      RC="${FALLBACK_OUT#DONE:}"
      echo "Recovered completion status via fallback SSH probe."
      finalize_remote_run "${RC}"
      break
    fi
    if (( SSH_DROP_FAIL_FAST_S > 0 )); then
      NOW_EPOCH="$(date +%s)"
      SSH_DOWN_S="$((NOW_EPOCH - LAST_SSH_OK_EPOCH))"
      if (( SSH_DOWN_S >= SSH_DROP_FAIL_FAST_S )); then
        echo "ERROR: SSH/control-plane has been down for ${SSH_DOWN_S}s (fail-fast threshold=${SSH_DROP_FAIL_FAST_S}s)."
        if [[ "${RESTART_NOW_HINT_ON_SSH_DROP}" == "true" ]]; then
          echo "ACTION: RESTART NOW (Pi power-cycle), then rerun smoke."
        fi
        exit 125
      fi
    fi
  fi

  NOW_EPOCH="$(date +%s)"
  ELAPSED="$((NOW_EPOCH - START_EPOCH))"
  if (( ELAPSED >= AGENT_RUN_TIMEOUT_S )); then
    echo "ERROR: Remote agent run did not finish within ${AGENT_RUN_TIMEOUT_S}s."
    run_ssh "${PI_USER}@${PI_HOST}" "bash -lc 'tail -n 120 \"${REMOTE_LOG}\" || true'" || true
    exit 124
  fi
  sleep "${AGENT_STATUS_POLL_S}"
done
