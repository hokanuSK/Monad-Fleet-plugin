#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PI_HOST="${PI_HOST:-${1:-monad-rpi5.local}}"
PI_HOSTS_CSV="${PI_HOSTS_CSV:-}"
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
CONTROL_PLANE_IPV4="${CONTROL_PLANE_IPV4:-}"
CONTROL_PLANE_IPV4_PREFIX="${CONTROL_PLANE_IPV4_PREFIX:-16}"
AUTO_LINKLOCAL_ETHERNET_IPV4="${AUTO_LINKLOCAL_ETHERNET_IPV4:-true}"
EXECUTE_POLICY="${EXECUTE_POLICY:-true}"
MAX_SYNC_CYCLES="${MAX_SYNC_CYCLES:-1}"
WIFI_SAMPLE_MIN_INTERVAL_S="${WIFI_SAMPLE_MIN_INTERVAL_S:-1}"
WIFI_SAMPLE_MAX_DURATION_S="${WIFI_SAMPLE_MAX_DURATION_S:-300}"
WIFI_SAMPLE_MAX_POINTS="${WIFI_SAMPLE_MAX_POINTS:-600}"
ENABLE_HTTP_SAMPLE_INGEST="${ENABLE_HTTP_SAMPLE_INGEST:-true}"
ENABLE_ELAB_ARTIFACT_UPLOAD="${ENABLE_ELAB_ARTIFACT_UPLOAD:-true}"
ARTIFACT_UPLOAD_MAX_BYTES="${ARTIFACT_UPLOAD_MAX_BYTES:-20971520}"
ARTIFACT_UPLOAD_TARGET="${ARTIFACT_UPLOAD_TARGET:-elabftw}"
ARTIFACT_UPLOAD_DURING_MEASURE="${ARTIFACT_UPLOAD_DURING_MEASURE:-false}"
ARTIFACT_EVICT_AFTER_UPLOAD="${ARTIFACT_EVICT_AFTER_UPLOAD:-false}"
ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S="${ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S:-3}"
ARTIFACT_UPLOAD_BACKOFF_S="${ARTIFACT_UPLOAD_BACKOFF_S:-15}"
RUN_ARTIFACT_SOFT_LIMIT_BYTES="${RUN_ARTIFACT_SOFT_LIMIT_BYTES:-0}"
REPORT_RPC_TIMEOUT_S="${REPORT_RPC_TIMEOUT_S:-120}"
CONTROL_RPC_TIMEOUT_S="${CONTROL_RPC_TIMEOUT_S:-30}"
AGENT_RUN_TIMEOUT_S="${AGENT_RUN_TIMEOUT_S:-900}"
AGENT_STATUS_POLL_S="${AGENT_STATUS_POLL_S:-5}"
ALLOW_WIFI_DISRUPTIVE_CSI="${ALLOW_WIFI_DISRUPTIVE_CSI:-false}"
DISABLE_CSI_CAPTURE="${DISABLE_CSI_CAPTURE:-}"
REQUIRE_REAL_CSI="${REQUIRE_REAL_CSI:-false}"
REQUIRE_ETHERNET_CONTROL="${REQUIRE_ETHERNET_CONTROL:-false}"
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
CSI_ALLOW_THROTTLED="${CSI_ALLOW_THROTTLED:-false}"
CSI_DEFAULT_FEIT_FREQ_MHZ="${CSI_DEFAULT_FEIT_FREQ_MHZ:-5240}"
CSI_DEFAULT_FEIT_CHANNEL_WIDTH_MHZ="${CSI_DEFAULT_FEIT_CHANNEL_WIDTH_MHZ:-80}"
CSI_DEFAULT_FEIT_FORMAT="${CSI_DEFAULT_FEIT_FORMAT:-VHT}"
CSI_DEFAULT_OUTPUT_PATH="${CSI_DEFAULT_OUTPUT_PATH:-/tmp/csi.dat}"

if [[ -z "${CSI_COLLECTOR_CMD}" ]]; then
  CSI_COLLECTOR_CMD="sudo feitcsi --frequency ${CSI_DEFAULT_FEIT_FREQ_MHZ} --channel-width ${CSI_DEFAULT_FEIT_CHANNEL_WIDTH_MHZ} --format ${CSI_DEFAULT_FEIT_FORMAT} --output-file ${CSI_DEFAULT_OUTPUT_PATH} -v"
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
fi

wait_for_ssh
if [[ "${PRECHECK_ONLY}" == "true" ]]; then
  echo "SSH precheck OK for ${PI_USER}@${PI_HOST}"
  exit 0
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
  AGENT_ID="$(run_ssh "${PI_USER}@${PI_HOST}" "cat /sys/class/net/${CONTROL_PLANE_IFACE}/address")"
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

echo "Running one-cycle smoke test on ${PI_USER}@${PI_HOST}"
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
REPORT_RPC_TIMEOUT_S=\\\"${REPORT_RPC_TIMEOUT_S}\\\" \\
CONTROL_RPC_TIMEOUT_S=\\\"${CONTROL_RPC_TIMEOUT_S}\\\" \\
DISABLE_CSI_CAPTURE=\\\"${DISABLE_CSI_CAPTURE}\\\" \\
CSI_COLLECTOR_CMD=\\\"${CSI_COLLECTOR_CMD}\\\" \\
CSI_OUTPUT_PATH=\\\"${CSI_OUTPUT_PATH}\\\" \\
CSI_OUTPUT_GLOB=\\\"${CSI_OUTPUT_GLOB}\\\" \\
CSI_FRAMES_REGEX=\\\"${CSI_FRAMES_REGEX}\\\" \\
CSI_OUTPUT_MAX_FILES=\\\"${CSI_OUTPUT_MAX_FILES}\\\" \\
CSI_PARSE_MAX_BYTES=\\\"${CSI_PARSE_MAX_BYTES}\\\" \\
CSI_REQUIRE_EVIDENCE=\\\"${CSI_REQUIRE_EVIDENCE}\\\" \\
CSI_MIN_FRAMES=\\\"${CSI_MIN_FRAMES}\\\" \\
CSI_MIN_OUTPUT_FILES=\\\"${CSI_MIN_OUTPUT_FILES}\\\" \\
CSI_SUDO_NONINTERACTIVE=\\\"${CSI_SUDO_NONINTERACTIVE}\\\" \\
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

finalize_remote_run() {
  local rc="$1"
  echo "Remote agent run finished with exit code ${rc}."
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc 'echo \"----- agent log tail (${REMOTE_LOG}) -----\"; tail -n 120 \"${REMOTE_LOG}\" || true'" || true
  if [[ "${rc}" != "0" ]]; then
    exit "${rc}"
  fi
}

START_EPOCH="$(date +%s)"
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
  echo \"RUNNING:\${pid}\"
else
  echo \"WAITING\"
fi
'"
  )"; then
    if [[ "${STATUS_OUT}" == DONE:* ]]; then
      RC="${STATUS_OUT#DONE:}"
      finalize_remote_run "${RC}"
      break
    fi
  else
    echo "SSH dropped while waiting for remote agent completion; retrying..."
    FALLBACK_OUT="$(
      ssh "${FALLBACK_SSH_ARGS[@]}" "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
if [[ -f \"${REMOTE_EXIT}\" ]]; then
  rc=\$(cat \"${REMOTE_EXIT}\" | tr -d \"\\r\\n\")
  echo \"DONE:\${rc}\"
fi
'" 2>/dev/null || true
    )"
    if [[ "${FALLBACK_OUT}" == DONE:* ]]; then
      RC="${FALLBACK_OUT#DONE:}"
      echo "Recovered completion status via fallback SSH probe."
      finalize_remote_run "${RC}"
      break
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
