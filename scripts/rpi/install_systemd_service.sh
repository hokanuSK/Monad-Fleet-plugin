#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PI_HOST="${PI_HOST:-${1:-monad-rpi5.local}}"
PI_USER="${PI_USER:-admin}"
PI_DIR="${PI_DIR:-/home/${PI_USER}/monad-fleet-agent}"
SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE:-}"
USE_DEFAULT_SSH_KEY="${USE_DEFAULT_SSH_KEY:-false}"
DEFAULT_SSH_KEY="${ROOT_DIR}/scripts/rpi/keys/monad_rpi5_ed25519"
if [[ -z "${SSH_IDENTITY_FILE}" && "${USE_DEFAULT_SSH_KEY}" == "true" && -f "${DEFAULT_SSH_KEY}" ]]; then
  SSH_IDENTITY_FILE="${DEFAULT_SSH_KEY}"
fi

SSH_ARGS=()
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
CONTROL_PLANE_MODE="${CONTROL_PLANE_MODE:-RF_SHARING}"
CONTROL_PLANE_IFACE="${CONTROL_PLANE_IFACE:-wlan0}"
WIFI_SCAN_IFACE="${WIFI_SCAN_IFACE:-${CONTROL_PLANE_IFACE}}"
MAX_SYNC_CYCLES="${MAX_SYNC_CYCLES:-0}"
EXECUTE_POLICY="${EXECUTE_POLICY:-false}"
CAPABILITIES="${CAPABILITIES:-csi,ble,wifi}"
DATA_ROOT="${DATA_ROOT:-${PI_DIR}/data}"
SENT_RETENTION_DAYS="${SENT_RETENTION_DAYS:-14}"
AGENT_ID="${AGENT_ID:-}"

if [[ -z "${AGENT_ID}" ]]; then
  AGENT_ID="$(run_ssh "${PI_USER}@${PI_HOST}" "cat /sys/class/net/${CONTROL_PLANE_IFACE}/address")"
fi

echo "Installing systemd unit on ${PI_USER}@${PI_HOST}"
echo "Using AGENT_ID=${AGENT_ID}"

run_ssh "${PI_USER}@${PI_HOST}" "bash -s" <<EOF
set -euo pipefail

PI_DIR="${PI_DIR}"
mkdir -p "\${PI_DIR}"

cat > "\${PI_DIR}/.env" <<ENV
FLEET_MANAGER_HOST=${FLEET_MANAGER_HOST}
FLEET_MANAGER_PORT=${FLEET_MANAGER_PORT}
AGENT_ID=${AGENT_ID}
CONTROL_PLANE_MODE=${CONTROL_PLANE_MODE}
CONTROL_PLANE_IFACE=${CONTROL_PLANE_IFACE}
WIFI_SCAN_IFACE=${WIFI_SCAN_IFACE}
MAX_SYNC_CYCLES=${MAX_SYNC_CYCLES}
EXECUTE_POLICY=${EXECUTE_POLICY}
CAPABILITIES=${CAPABILITIES}
DATA_ROOT=${DATA_ROOT}
SENT_RETENTION_DAYS=${SENT_RETENTION_DAYS}
ENV

sudo tee /etc/systemd/system/monad-fleet-agent.service >/dev/null <<UNIT
[Unit]
Description=Monad Fleet Agent v2
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${PI_USER}
WorkingDirectory=${PI_DIR}
EnvironmentFile=${PI_DIR}/.env
ExecStart=${PI_DIR}/venv/bin/python -u ${PI_DIR}/agent_v2_client.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now monad-fleet-agent.service
sudo systemctl restart monad-fleet-agent.service
sudo systemctl is-enabled monad-fleet-agent.service
sudo systemctl is-active monad-fleet-agent.service
EOF

echo "systemd service installed and restarted."
