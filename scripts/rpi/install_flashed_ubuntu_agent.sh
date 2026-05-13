#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

BUNDLE_DIR="${1:-}"
PI_HOST="${PI_HOST:-${2:-}}"

usage() {
  cat <<'EOF'
Usage: PI_HOST=<host-or-ip> scripts/rpi/install_flashed_ubuntu_agent.sh <bundle-dir>

Example:
  PI_HOST=10.200.0.12 \
    SSH_PROXY_JUMP=ladamik@34.198.184.128 \
    scripts/rpi/install_flashed_ubuntu_agent.sh \
    artifacts/output/rpi-agent-bootstrap/<timestamp>/monad-08

This script:
  1. stabilizes a fresh Ubuntu device (packages + optional WireGuard DNS cleanup)
  2. deploys the agent code
  3. installs the systemd unit using the bundle configuration
EOF
}

if [[ -z "${BUNDLE_DIR}" || -z "${PI_HOST}" ]]; then
  usage >&2
  exit 2
fi

ENV_FILE="${BUNDLE_DIR}/agent.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "ERROR: missing bundle env: ${ENV_FILE}" >&2
  exit 2
fi

# shellcheck disable=SC1090
source "${ENV_FILE}"

PI_USER="${PI_USER:-${MONAD_PI_USER:-monad}}"
PI_DIR="${PI_DIR:-${MONAD_PI_DIR:-/home/${PI_USER}/monad-fleet-agent}}"
SSH_PROXY_JUMP="${SSH_PROXY_JUMP:-}"
SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE:-}"
SSH_USER_KNOWN_HOSTS_FILE="${SSH_USER_KNOWN_HOSTS_FILE:-}"
SSH_STRICT_HOST_KEY_CHECKING="${SSH_STRICT_HOST_KEY_CHECKING:-accept-new}"

SSH_ARGS=()
if [[ -n "${SSH_STRICT_HOST_KEY_CHECKING}" ]]; then
  SSH_ARGS+=(-o "StrictHostKeyChecking=${SSH_STRICT_HOST_KEY_CHECKING}")
fi
if [[ -n "${SSH_USER_KNOWN_HOSTS_FILE}" ]]; then
  SSH_ARGS+=(-o "UserKnownHostsFile=${SSH_USER_KNOWN_HOSTS_FILE}")
fi
if [[ -n "${SSH_IDENTITY_FILE}" ]]; then
  SSH_ARGS+=(-i "${SSH_IDENTITY_FILE}" -o IdentitiesOnly=yes)
fi
if [[ -n "${SSH_PROXY_JUMP}" ]]; then
  SSH_ARGS+=(-J "${SSH_PROXY_JUMP}")
fi

run_ssh() {
  if [[ ${#SSH_ARGS[@]} -gt 0 ]]; then
    ssh "${SSH_ARGS[@]}" "$@"
  else
    ssh "$@"
  fi
}

echo "[1/4] Stabilizing fresh Ubuntu runtime on ${PI_USER}@${PI_HOST}"
run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
if [[ \"${MONAD_STRIP_WG_DNS:-false}\" == \"true\" ]] && sudo test -f /etc/wireguard/wg0.conf; then
  if sudo grep -q \"^DNS[[:space:]]*=\" /etc/wireguard/wg0.conf; then
    sudo sed -i.bak \"/^DNS[[:space:]]*=/d\" /etc/wireguard/wg0.conf
    sudo systemctl restart wg-quick@wg0 || true
  fi
fi
sudo DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true update
sudo DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true install -y \
  python3.12-venv iw bluez wireless-tools
sudo systemctl enable --now bluetooth || true
'"

echo "[2/4] Deploying agent code"
PI_HOST="${PI_HOST}" PI_USER="${PI_USER}" PI_DIR="${PI_DIR}" \
SSH_PROXY_JUMP="${SSH_PROXY_JUMP}" SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE}" \
SSH_USER_KNOWN_HOSTS_FILE="${SSH_USER_KNOWN_HOSTS_FILE}" \
SSH_STRICT_HOST_KEY_CHECKING="${SSH_STRICT_HOST_KEY_CHECKING}" \
"${ROOT_DIR}/scripts/rpi/deploy_agent.sh" "${PI_HOST}"

echo "[3/4] Installing systemd service"
PI_HOST="${PI_HOST}" PI_USER="${PI_USER}" PI_DIR="${PI_DIR}" \
SSH_PROXY_JUMP="${SSH_PROXY_JUMP}" SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE}" \
SSH_USER_KNOWN_HOSTS_FILE="${SSH_USER_KNOWN_HOSTS_FILE}" \
SSH_STRICT_HOST_KEY_CHECKING="${SSH_STRICT_HOST_KEY_CHECKING}" \
FLEET_MANAGER_HOST="${MONAD_FLEET_MANAGER_HOST}" \
FLEET_MANAGER_PORT="${MONAD_FLEET_MANAGER_PORT}" \
AGENT_ID="${MONAD_AGENT_ID}" \
CONTROL_PLANE_MODE="${MONAD_CONTROL_PLANE_MODE}" \
CONTROL_PLANE_IFACE="${MONAD_CONTROL_PLANE_IFACE}" \
WIFI_SCAN_IFACE="${MONAD_WIFI_SCAN_IFACE}" \
CAPABILITIES="${MONAD_CAPABILITIES}" \
EXECUTE_POLICY="${MONAD_EXECUTE_POLICY}" \
MAX_SYNC_CYCLES="${MONAD_MAX_SYNC_CYCLES}" \
SENT_RETENTION_DAYS="${MONAD_SENT_RETENTION_DAYS}" \
ARTIFACT_UPLOAD_TARGET="${MONAD_ARTIFACT_UPLOAD_TARGET}" \
ARTIFACT_UPLOAD_DURING_MEASURE="${MONAD_ARTIFACT_UPLOAD_DURING_MEASURE}" \
ARTIFACT_EVICT_AFTER_UPLOAD="${MONAD_ARTIFACT_EVICT_AFTER_UPLOAD}" \
RUN_ARTIFACT_SOFT_LIMIT_BYTES="${MONAD_RUN_ARTIFACT_SOFT_LIMIT_BYTES}" \
DEFAULT_METRICS_SINKS="${MONAD_DEFAULT_METRICS_SINKS}" \
GRPC_DNS_RESOLVER="${MONAD_GRPC_DNS_RESOLVER}" \
"${ROOT_DIR}/scripts/rpi/install_systemd_service.sh" "${PI_HOST}"

echo "[4/4] Verifying runtime"
run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
command -v iw
command -v bluetoothctl
${PI_DIR}/venv/bin/python - <<\"PY\"
import grpc
import google.protobuf
import prometheus_client
print(\"python-runtime-ok\")
PY
sudo systemctl is-active monad-fleet-agent.service
'"

cat <<EOF
Fresh Ubuntu agent install completed.
Host: ${PI_HOST}
Bundle: ${BUNDLE_DIR}
Fleet manager: ${MONAD_FLEET_MANAGER_HOST}:${MONAD_FLEET_MANAGER_PORT}
Control plane iface: ${MONAD_CONTROL_PLANE_IFACE}
Wi-Fi scan iface: ${MONAD_WIFI_SCAN_IFACE}
EOF
