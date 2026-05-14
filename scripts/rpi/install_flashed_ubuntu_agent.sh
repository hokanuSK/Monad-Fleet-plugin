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
  3. configures local Prometheus scrape + remote_write to Mimir
  4. installs the systemd unit using the bundle configuration
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
WIFI_SCAN_FORCE_UP="${WIFI_SCAN_FORCE_UP:-${MONAD_WIFI_SCAN_FORCE_UP:-false}}"
PROM_PI_ID="${PROM_PI_ID:-${MONAD_PROM_PI_ID:-${MONAD_HOSTNAME:-unknown-pi}}}"
PROM_SITE="${PROM_SITE:-${MONAD_PROM_SITE:-monad-fleet}}"
PROM_INSTANCE="${PROM_INSTANCE:-${MONAD_PROM_INSTANCE:-${MONAD_HOSTNAME:-unknown-pi}}}"
PROM_METRICS_PORT="${PROM_METRICS_PORT:-${MONAD_PROM_METRICS_PORT:-9110}}"
PROM_EXPOSITION_PORT="${PROM_EXPOSITION_PORT:-${MONAD_PROM_EXPOSITION_PORT:-${PROM_METRICS_PORT}}}"
PROM_REMOTE_WRITE_URL="${PROM_REMOTE_WRITE_URL:-${MONAD_PROM_REMOTE_WRITE_URL:-http://10.200.0.1:9009/api/v1/push}}"
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
  python3.12-venv iw bluez wireless-tools tcpdump prometheus
sudo systemctl enable --now bluetooth || true

# On Ubuntu the agent runs as a non-root user; iw/ip need CAP_NET_ADMIN for
# monitor-vif management, tcpdump needs CAP_NET_RAW for raw socket capture.
echo \"${PI_USER} ALL=(ALL) NOPASSWD: /usr/sbin/iw, /sbin/iw, /usr/sbin/ip, /sbin/ip, /usr/bin/tcpdump\" \
  | sudo tee /etc/sudoers.d/monad-iw > /dev/null
sudo chmod 440 /etc/sudoers.d/monad-iw
sudo visudo -c -f /etc/sudoers.d/monad-iw
'"

echo "[2/4] Deploying agent code"
PI_HOST="${PI_HOST}" PI_USER="${PI_USER}" PI_DIR="${PI_DIR}" \
SSH_PROXY_JUMP="${SSH_PROXY_JUMP}" SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE}" \
SSH_USER_KNOWN_HOSTS_FILE="${SSH_USER_KNOWN_HOSTS_FILE}" \
SSH_STRICT_HOST_KEY_CHECKING="${SSH_STRICT_HOST_KEY_CHECKING}" \
"${ROOT_DIR}/scripts/rpi/deploy_agent.sh" "${PI_HOST}"

echo "[3/4] Configuring local Prometheus"
run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
sudo install -d -m 0755 /etc/prometheus
sudo tee /etc/prometheus/prometheus.yml >/dev/null <<\"PROM\"
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: monad-fleet-agent
    static_configs:
      - targets: [\"127.0.0.1:${PROM_METRICS_PORT}\"]
        labels:
          agent_id: \"${MONAD_AGENT_ID}\"
          pi_id: \"${PROM_PI_ID}\"
          site: \"${PROM_SITE}\"
          instance: \"${PROM_INSTANCE}\"

remote_write:
  - url: \"${PROM_REMOTE_WRITE_URL}\"
PROM
sudo systemctl enable --now prometheus
sudo systemctl restart prometheus
'"

if [[ "${WIFI_SCAN_FORCE_UP}" == "true" ]]; then
  echo "[3b/4] Installing measurement-interface bring-up service"
  run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
sudo tee /etc/systemd/system/monad-measurement-iface-up.service >/dev/null <<EOF
[Unit]
Description=Bring up Monad measurement interface
Before=monad-fleet-agent.service
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/sbin/ip link set ${MONAD_WIFI_SCAN_IFACE} up
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now monad-measurement-iface-up.service
'"
fi

echo "[4/4] Installing systemd service"
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
METRICS_PORT="${PROM_EXPOSITION_PORT}" \
"${ROOT_DIR}/scripts/rpi/install_systemd_service.sh" "${PI_HOST}"

echo "[5/5] Verifying runtime"
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
sudo systemctl is-active prometheus
sudo systemctl is-active monad-fleet-agent.service
sudo grep -q \"${PROM_REMOTE_WRITE_URL}\" /etc/prometheus/prometheus.yml
'"

echo "[5b/5] Validating experiment readiness"
PI_HOST="${PI_HOST}" PI_USER="${PI_USER}" \
SSH_PROXY_JUMP="${SSH_PROXY_JUMP}" SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE}" \
SSH_USER_KNOWN_HOSTS_FILE="${SSH_USER_KNOWN_HOSTS_FILE}" \
SSH_STRICT_HOST_KEY_CHECKING="${SSH_STRICT_HOST_KEY_CHECKING}" \
"${ROOT_DIR}/scripts/rpi/validate_bootstrap_install.sh" "${BUNDLE_DIR}"

cat <<EOF
Fresh Ubuntu agent install completed.
Host: ${PI_HOST}
Bundle: ${BUNDLE_DIR}
Fleet manager: ${MONAD_FLEET_MANAGER_HOST}:${MONAD_FLEET_MANAGER_PORT}
Control plane iface: ${MONAD_CONTROL_PLANE_IFACE}
Wi-Fi scan iface: ${MONAD_WIFI_SCAN_IFACE}
Prometheus scrape: 127.0.0.1:${PROM_METRICS_PORT}
Prometheus remote_write: ${PROM_REMOTE_WRITE_URL}
EOF
