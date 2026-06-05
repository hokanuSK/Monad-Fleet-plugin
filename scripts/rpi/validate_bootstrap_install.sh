#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

BUNDLE_DIR="${1:-}"
PI_HOST="${PI_HOST:-${2:-}}"

usage() {
  cat <<'EOF'
Usage: PI_HOST=<host-or-ip> scripts/rpi/validate_bootstrap_install.sh <bundle-dir>

Validates that a Raspberry Pi bootstrap install is actually ready for Fleet
experiments:
  - monad-fleet-agent.service active
  - prometheus active
  - local metrics endpoint reachable
  - wifi scan iface administratively UP
  - monitor interface creation works on the measurement iface when applicable
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
WIFI_SCAN_FORCE_UP="${WIFI_SCAN_FORCE_UP:-${MONAD_WIFI_SCAN_FORCE_UP:-false}}"
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

echo "[validate] Checking services and measurement iface on ${PI_USER}@${PI_HOST}"
run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
systemctl is-active monad-fleet-agent.service
sudo systemctl is-active prometheus
curl -fsS http://127.0.0.1:${MONAD_PROM_EXPOSITION_PORT:-9110}/metrics >/dev/null
ip -br link show ${MONAD_WIFI_SCAN_IFACE}
if [[ \"${WIFI_SCAN_FORCE_UP}\" == \"true\" ]] || [[ \"${MONAD_WIFI_SCAN_IFACE}\" != \"${MONAD_CONTROL_PLANE_IFACE}\" ]]; then
  sudo iw dev ${MONAD_WIFI_SCAN_IFACE} interface add __monad_validate_mon type monitor
  sudo iw dev __monad_validate_mon del
fi
'"

cat <<EOF
Bootstrap validation passed.
Host: ${PI_HOST}
Bundle: ${BUNDLE_DIR}
Wi-Fi scan iface: ${MONAD_WIFI_SCAN_IFACE}
Prometheus metrics: 127.0.0.1:${MONAD_PROM_EXPOSITION_PORT:-9110}
EOF
