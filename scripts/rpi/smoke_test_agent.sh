#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PI_HOST="${PI_HOST:-${1:-monad-rpi5.local}}"
PI_USER="${PI_USER:-admin}"
PI_DIR="${PI_DIR:-/home/${PI_USER}/monad-fleet-agent}"
SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE:-}"
USE_DEFAULT_SSH_KEY="${USE_DEFAULT_SSH_KEY:-false}"
DEFAULT_SSH_KEY="${ROOT_DIR}/scripts/rpi/keys/monad_rpi5_ed25519"
SSH_CONNECT_TIMEOUT_S="${SSH_CONNECT_TIMEOUT_S:-10}"
SSH_SERVER_ALIVE_INTERVAL_S="${SSH_SERVER_ALIVE_INTERVAL_S:-5}"
SSH_SERVER_ALIVE_COUNT_MAX="${SSH_SERVER_ALIVE_COUNT_MAX:-3}"
if [[ -z "${SSH_IDENTITY_FILE}" && "${USE_DEFAULT_SSH_KEY}" == "true" && -f "${DEFAULT_SSH_KEY}" ]]; then
  SSH_IDENTITY_FILE="${DEFAULT_SSH_KEY}"
fi

SSH_ARGS=(
  -o "ConnectTimeout=${SSH_CONNECT_TIMEOUT_S}"
  -o "ServerAliveInterval=${SSH_SERVER_ALIVE_INTERVAL_S}"
  -o "ServerAliveCountMax=${SSH_SERVER_ALIVE_COUNT_MAX}"
)
if [[ -n "${SSH_IDENTITY_FILE}" ]]; then
  SSH_ARGS+=(-i "${SSH_IDENTITY_FILE}" -o IdentitiesOnly=yes)
fi

run_ssh() {
  if [[ ${#SSH_ARGS[@]} -gt 0 ]]; then
    ssh "${SSH_ARGS[@]}" "$@"
  else
    ssh "$@"
  fi
}

FLEET_MANAGER_HOST="${FLEET_MANAGER_HOST:-192.168.0.70}"
FLEET_MANAGER_PORT="${FLEET_MANAGER_PORT:-50060}"
AGENT_ID="${AGENT_ID:-}"
CONTROL_PLANE_MODE="${CONTROL_PLANE_MODE:-RF_SHARING}"
CONTROL_PLANE_IFACE="${CONTROL_PLANE_IFACE:-wlan0}"
WIFI_SCAN_IFACE="${WIFI_SCAN_IFACE:-${CONTROL_PLANE_IFACE}}"
EXECUTE_POLICY="${EXECUTE_POLICY:-true}"
MAX_SYNC_CYCLES="${MAX_SYNC_CYCLES:-1}"
WIFI_SAMPLE_MIN_INTERVAL_S="${WIFI_SAMPLE_MIN_INTERVAL_S:-1}"
WIFI_SAMPLE_MAX_DURATION_S="${WIFI_SAMPLE_MAX_DURATION_S:-300}"
WIFI_SAMPLE_MAX_POINTS="${WIFI_SAMPLE_MAX_POINTS:-600}"
ENABLE_HTTP_SAMPLE_INGEST="${ENABLE_HTTP_SAMPLE_INGEST:-true}"
ENABLE_ELAB_ARTIFACT_UPLOAD="${ENABLE_ELAB_ARTIFACT_UPLOAD:-true}"
ARTIFACT_UPLOAD_MAX_BYTES="${ARTIFACT_UPLOAD_MAX_BYTES:-20971520}"
REPORT_RPC_TIMEOUT_S="${REPORT_RPC_TIMEOUT_S:-120}"
CONTROL_RPC_TIMEOUT_S="${CONTROL_RPC_TIMEOUT_S:-30}"
ALLOW_WIFI_DISRUPTIVE_CSI="${ALLOW_WIFI_DISRUPTIVE_CSI:-false}"
DISABLE_CSI_CAPTURE="${DISABLE_CSI_CAPTURE:-}"
REQUIRE_REAL_CSI="${REQUIRE_REAL_CSI:-false}"
CSI_COLLECTOR_CMD="${CSI_COLLECTOR_CMD:-}"
CSI_OUTPUT_PATH="${CSI_OUTPUT_PATH:-}"
CSI_OUTPUT_GLOB="${CSI_OUTPUT_GLOB:-}"
CSI_FRAMES_REGEX="${CSI_FRAMES_REGEX:-}"
CSI_OUTPUT_MAX_FILES="${CSI_OUTPUT_MAX_FILES:-}"
CSI_PARSE_MAX_BYTES="${CSI_PARSE_MAX_BYTES:-}"

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

echo "Running one-cycle smoke test on ${PI_USER}@${PI_HOST}"
run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
cd \"${PI_DIR}\"
FLEET_MANAGER_HOST=\"${FLEET_MANAGER_HOST}\" \\
FLEET_MANAGER_PORT=\"${FLEET_MANAGER_PORT}\" \\
AGENT_ID=\"${AGENT_ID}\" \\
CONTROL_PLANE_MODE=\"${CONTROL_PLANE_MODE}\" \\
CONTROL_PLANE_IFACE=\"${CONTROL_PLANE_IFACE}\" \\
WIFI_SCAN_IFACE=\"${WIFI_SCAN_IFACE}\" \\
WIFI_SAMPLE_MIN_INTERVAL_S=\"${WIFI_SAMPLE_MIN_INTERVAL_S}\" \\
WIFI_SAMPLE_MAX_DURATION_S=\"${WIFI_SAMPLE_MAX_DURATION_S}\" \\
WIFI_SAMPLE_MAX_POINTS=\"${WIFI_SAMPLE_MAX_POINTS}\" \\
ENABLE_HTTP_SAMPLE_INGEST=\"${ENABLE_HTTP_SAMPLE_INGEST}\" \\
ENABLE_ELAB_ARTIFACT_UPLOAD=\"${ENABLE_ELAB_ARTIFACT_UPLOAD}\" \\
ARTIFACT_UPLOAD_MAX_BYTES=\"${ARTIFACT_UPLOAD_MAX_BYTES}\" \\
REPORT_RPC_TIMEOUT_S=\"${REPORT_RPC_TIMEOUT_S}\" \\
CONTROL_RPC_TIMEOUT_S=\"${CONTROL_RPC_TIMEOUT_S}\" \\
DISABLE_CSI_CAPTURE=\"${DISABLE_CSI_CAPTURE}\" \\
CSI_COLLECTOR_CMD=\"${CSI_COLLECTOR_CMD}\" \\
CSI_OUTPUT_PATH=\"${CSI_OUTPUT_PATH}\" \\
CSI_OUTPUT_GLOB=\"${CSI_OUTPUT_GLOB}\" \\
CSI_FRAMES_REGEX=\"${CSI_FRAMES_REGEX}\" \\
CSI_OUTPUT_MAX_FILES=\"${CSI_OUTPUT_MAX_FILES}\" \\
CSI_PARSE_MAX_BYTES=\"${CSI_PARSE_MAX_BYTES}\" \\
MAX_SYNC_CYCLES=\"${MAX_SYNC_CYCLES}\" \\
EXECUTE_POLICY=\"${EXECUTE_POLICY}\" \\
\"${PI_DIR}/venv/bin/python\" -u \"${PI_DIR}/agent_v2_client.py\"
'"
