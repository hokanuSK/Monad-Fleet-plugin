#!/usr/bin/env bash
set -euo pipefail

PI_HOST="${PI_HOST:-${1:-monad-rpi5.local}}"
PI_USER="${PI_USER:-admin}"
PI_DIR="${PI_DIR:-/home/${PI_USER}/monad-fleet-agent}"
SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE:-}"

SSH_ARGS=()
if [[ -n "${SSH_IDENTITY_FILE}" ]]; then
  SSH_ARGS+=(-i "${SSH_IDENTITY_FILE}" -o IdentitiesOnly=yes)
fi

FLEET_MANAGER_HOST="${FLEET_MANAGER_HOST:-192.168.0.70}"
FLEET_MANAGER_PORT="${FLEET_MANAGER_PORT:-50060}"
AGENT_ID="${AGENT_ID:-}"
CONTROL_PLANE_MODE="${CONTROL_PLANE_MODE:-RF_SHARING}"
CONTROL_PLANE_IFACE="${CONTROL_PLANE_IFACE:-wlan0}"
EXECUTE_POLICY="${EXECUTE_POLICY:-false}"
MAX_SYNC_CYCLES="${MAX_SYNC_CYCLES:-1}"

if [[ -z "${AGENT_ID}" ]]; then
  AGENT_ID="$(ssh "${SSH_ARGS[@]}" "${PI_USER}@${PI_HOST}" "cat /sys/class/net/${CONTROL_PLANE_IFACE}/address")"
fi

echo "Running one-cycle smoke test on ${PI_USER}@${PI_HOST}"
ssh "${SSH_ARGS[@]}" "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
cd \"${PI_DIR}\"
FLEET_MANAGER_HOST=\"${FLEET_MANAGER_HOST}\" \\
FLEET_MANAGER_PORT=\"${FLEET_MANAGER_PORT}\" \\
AGENT_ID=\"${AGENT_ID}\" \\
CONTROL_PLANE_MODE=\"${CONTROL_PLANE_MODE}\" \\
CONTROL_PLANE_IFACE=\"${CONTROL_PLANE_IFACE}\" \\
MAX_SYNC_CYCLES=\"${MAX_SYNC_CYCLES}\" \\
EXECUTE_POLICY=\"${EXECUTE_POLICY}\" \\
\"${PI_DIR}/venv/bin/python\" -u \"${PI_DIR}/agent_v2_client.py\"
'"
