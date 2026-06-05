#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEFAULT_FLEET_MANAGER_HOST="$(bash "${ROOT_DIR}/scripts/rpi/default_fleet_manager_host.sh")"
DEFAULT_FLEET_MANAGER_PORT="$(bash "${ROOT_DIR}/scripts/rpi/default_fleet_manager_port.sh")"

PI_HOST="${PI_HOST:-${1:-monad-rpi5.local}}"
FLEET_MANAGER_HOST="${FLEET_MANAGER_HOST:-${2:-${DEFAULT_FLEET_MANAGER_HOST}}}"
FLEET_MANAGER_PORT="${FLEET_MANAGER_PORT:-${DEFAULT_FLEET_MANAGER_PORT}}"
PI_USER="${PI_USER:-admin}"
PI_DIR="${PI_DIR:-/home/${PI_USER}/monad-fleet-agent}"
USE_DEFAULT_SSH_KEY="${USE_DEFAULT_SSH_KEY:-true}"
SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE:-}"
SSH_CONNECT_TIMEOUT_S="${SSH_CONNECT_TIMEOUT_S:-5}"
SSH_BOOTSTRAP_RETRIES="${SSH_BOOTSTRAP_RETRIES:-20}"
SSH_BOOTSTRAP_RETRY_DELAY_S="${SSH_BOOTSTRAP_RETRY_DELAY_S:-3}"

# Room-ready defaults for a continuously running real device.
CONTROL_PLANE_MODE="${CONTROL_PLANE_MODE:-RF_SHARING}"
CONTROL_PLANE_IFACE="${CONTROL_PLANE_IFACE:-wlan0}"
WIFI_SCAN_IFACE="${WIFI_SCAN_IFACE:-${CONTROL_PLANE_IFACE}}"
CAPABILITIES="${CAPABILITIES:-csi,ble,wifi}"
EXECUTE_POLICY="${EXECUTE_POLICY:-true}"
MAX_SYNC_CYCLES="${MAX_SYNC_CYCLES:-0}"
SENT_RETENTION_DAYS="${SENT_RETENTION_DAYS:-14}"
ARTIFACT_UPLOAD_TARGET="${ARTIFACT_UPLOAD_TARGET:-elabftw}"
ARTIFACT_UPLOAD_DURING_MEASURE="${ARTIFACT_UPLOAD_DURING_MEASURE:-true}"
ARTIFACT_EVICT_AFTER_UPLOAD="${ARTIFACT_EVICT_AFTER_UPLOAD:-true}"
ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S="${ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S:-3}"
ARTIFACT_UPLOAD_BACKOFF_S="${ARTIFACT_UPLOAD_BACKOFF_S:-15}"
RUN_ARTIFACT_SOFT_LIMIT_BYTES="${RUN_ARTIFACT_SOFT_LIMIT_BYTES:-0}"
DEFAULT_METRICS_SINKS="${DEFAULT_METRICS_SINKS:-fleet_http}"
AGENT_ID="${AGENT_ID:-}"
GRPC_DNS_RESOLVER="${GRPC_DNS_RESOLVER:-native}"

SSH_ARGS=()
if [[ -n "${SSH_IDENTITY_FILE}" ]]; then
  SSH_ARGS+=(-i "${SSH_IDENTITY_FILE}" -o IdentitiesOnly=yes)
fi

resolve_ssh_target() {
  local host="$1"
  local attempt
  local conn
  local server_ip

  run_bootstrap_ssh() {
    if [[ ${#SSH_ARGS[@]} -gt 0 ]]; then
      ssh "${SSH_ARGS[@]}" \
        -o ConnectTimeout="${SSH_CONNECT_TIMEOUT_S}" \
        -o BatchMode=yes \
        -o StrictHostKeyChecking=accept-new \
        "${PI_USER}@${host}" 'echo "${SSH_CONNECTION:-}"'
    else
      ssh \
        -o ConnectTimeout="${SSH_CONNECT_TIMEOUT_S}" \
        -o BatchMode=yes \
        -o StrictHostKeyChecking=accept-new \
        "${PI_USER}@${host}" 'echo "${SSH_CONNECTION:-}"'
    fi
  }

  for attempt in $(seq 1 "${SSH_BOOTSTRAP_RETRIES}"); do
    if conn="$(run_bootstrap_ssh 2>/dev/null)"; then
      server_ip="$(awk '{print $3}' <<<"${conn}")"
      if [[ -n "${server_ip}" ]]; then
        echo "${server_ip}"
        return 0
      fi
      echo "${host}"
      return 0
    fi
    echo "Waiting for SSH (${attempt}/${SSH_BOOTSTRAP_RETRIES}) host=${host}" >&2
    sleep "${SSH_BOOTSTRAP_RETRY_DELAY_S}"
  done
  return 1
}

if ! PI_TARGET_HOST="$(resolve_ssh_target "${PI_HOST}")"; then
  echo "ERROR: unable to establish SSH to ${PI_HOST}" >&2
  exit 2
fi

echo "Using SSH target: ${PI_TARGET_HOST} (from ${PI_HOST})"

echo "[1/3] Deploy agent files to ${PI_USER}@${PI_TARGET_HOST}"
PI_HOST="${PI_TARGET_HOST}" PI_USER="${PI_USER}" PI_DIR="${PI_DIR}" \
USE_DEFAULT_SSH_KEY="${USE_DEFAULT_SSH_KEY}" SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE}" \
"${ROOT_DIR}/scripts/rpi/deploy_agent.sh" "${PI_TARGET_HOST}"

echo "[2/3] Install systemd service with room-ready defaults"
PI_HOST="${PI_TARGET_HOST}" PI_USER="${PI_USER}" PI_DIR="${PI_DIR}" \
USE_DEFAULT_SSH_KEY="${USE_DEFAULT_SSH_KEY}" SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE}" \
FLEET_MANAGER_HOST="${FLEET_MANAGER_HOST}" CONTROL_PLANE_MODE="${CONTROL_PLANE_MODE}" \
FLEET_MANAGER_PORT="${FLEET_MANAGER_PORT}" CONTROL_PLANE_IFACE="${CONTROL_PLANE_IFACE}" \
WIFI_SCAN_IFACE="${WIFI_SCAN_IFACE}" \
CAPABILITIES="${CAPABILITIES}" EXECUTE_POLICY="${EXECUTE_POLICY}" \
MAX_SYNC_CYCLES="${MAX_SYNC_CYCLES}" SENT_RETENTION_DAYS="${SENT_RETENTION_DAYS}" \
GRPC_DNS_RESOLVER="${GRPC_DNS_RESOLVER}" \
ARTIFACT_UPLOAD_TARGET="${ARTIFACT_UPLOAD_TARGET}" \
ARTIFACT_UPLOAD_DURING_MEASURE="${ARTIFACT_UPLOAD_DURING_MEASURE}" \
ARTIFACT_EVICT_AFTER_UPLOAD="${ARTIFACT_EVICT_AFTER_UPLOAD}" \
ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S="${ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S}" \
ARTIFACT_UPLOAD_BACKOFF_S="${ARTIFACT_UPLOAD_BACKOFF_S}" \
RUN_ARTIFACT_SOFT_LIMIT_BYTES="${RUN_ARTIFACT_SOFT_LIMIT_BYTES}" \
DEFAULT_METRICS_SINKS="${DEFAULT_METRICS_SINKS}" \
AGENT_ID="${AGENT_ID}" "${ROOT_DIR}/scripts/rpi/install_systemd_service.sh" "${PI_TARGET_HOST}"

echo "[3/3] Verify service status"
ssh "${SSH_ARGS[@]}" "${PI_USER}@${PI_TARGET_HOST}" "sudo systemctl status --no-pager monad-fleet-agent.service | sed -n '1,25p'"

cat <<EOF

Room-ready Pi agent is active.
Host: ${PI_TARGET_HOST}
Fleet manager: ${FLEET_MANAGER_HOST}
Fleet manager port: ${FLEET_MANAGER_PORT}
gRPC DNS resolver: ${GRPC_DNS_RESOLVER}
Mode: ${CONTROL_PLANE_MODE}
Execute policy: ${EXECUTE_POLICY}
Artifact upload during measure: ${ARTIFACT_UPLOAD_DURING_MEASURE}
EOF
