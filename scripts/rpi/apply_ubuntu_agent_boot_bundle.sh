#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

usage() {
  cat <<'EOF'
Usage: scripts/rpi/apply_ubuntu_agent_boot_bundle.sh <mounted-system-boot-volume>

Adds a first-boot Monad Fleet agent installer to an already flashed Ubuntu
Server Raspberry Pi system-boot partition. The card should already have the
WireGuard bundle applied.

Environment:
  PI_USER              default: monad
  PI_DIR               default: /home/<PI_USER>/monad-fleet-agent
  FLEET_MANAGER_HOST   default: 10.200.0.1
  FLEET_MANAGER_PORT   default: 50060
  CONTROL_PLANE_IFACE  default: wg0
  WIFI_SCAN_IFACE      default: wlan0
  AGENT_ID_IFACE       default: wlan0
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

BOOT_MOUNT="${1:-}"
if [[ -z "${BOOT_MOUNT}" ]]; then
  usage >&2
  exit 2
fi
if [[ ! -d "${BOOT_MOUNT}" ]]; then
  echo "ERROR: boot mount is not a directory: ${BOOT_MOUNT}" >&2
  exit 2
fi
if [[ ! -f "${BOOT_MOUNT}/user-data" ]]; then
  echo "ERROR: Ubuntu cloud-init user-data not found: ${BOOT_MOUNT}/user-data" >&2
  exit 2
fi

AGENT_SRC="${ROOT_DIR}/src/device-sim/agent_v2_client.py"
AGENT_PKG_SRC="${ROOT_DIR}/src/device-sim/agent_v2"
PROTO_GEN_SRC="${ROOT_DIR}/shared/proto_gen"
if [[ ! -f "${AGENT_SRC}" || ! -d "${AGENT_PKG_SRC}" || ! -d "${PROTO_GEN_SRC}" ]]; then
  echo "ERROR: missing agent source or generated proto stubs" >&2
  exit 2
fi

PI_USER="${PI_USER:-monad}"
PI_DIR="${PI_DIR:-/home/${PI_USER}/monad-fleet-agent}"
FLEET_MANAGER_HOST="${FLEET_MANAGER_HOST:-10.200.0.1}"
FLEET_MANAGER_PORT="${FLEET_MANAGER_PORT:-50060}"
CONTROL_PLANE_IFACE="${CONTROL_PLANE_IFACE:-wg0}"
WIFI_SCAN_IFACE="${WIFI_SCAN_IFACE:-wlan0}"
AGENT_ID_IFACE="${AGENT_ID_IFACE:-wlan0}"
CAPABILITIES="${CAPABILITIES:-csi,ble,wifi}"
EXECUTE_POLICY="${EXECUTE_POLICY:-true}"
MAX_SYNC_CYCLES="${MAX_SYNC_CYCLES:-0}"
ARTIFACT_UPLOAD_TARGET="${ARTIFACT_UPLOAD_TARGET:-fleet_http}"
DEFAULT_METRICS_SINKS="${DEFAULT_METRICS_SINKS:-fleet_http}"
GRPC_DNS_RESOLVER="${GRPC_DNS_RESOLVER:-native}"
PROM_PI_ID="${PROM_PI_ID:-}"
PROM_SITE="${PROM_SITE:-monad-fleet}"
PROM_INSTANCE="${PROM_INSTANCE:-}"
PROM_REMOTE_WRITE_URL="${PROM_REMOTE_WRITE_URL:-http://10.200.0.1:9009/api/v1/push}"
PROM_EXPOSITION_PORT="${PROM_EXPOSITION_PORT:-9110}"

OUT_DIR="${OUT_DIR:-${ROOT_DIR}/artifacts/output/rpi-agent-boot/${TIMESTAMP}}"
STAGE_DIR="${OUT_DIR}/payload"
PAYLOAD="${OUT_DIR}/monad-agent-payload.tar.gz"
mkdir -p "${STAGE_DIR}" "${OUT_DIR}"

cp "${AGENT_SRC}" "${STAGE_DIR}/agent_v2_client.py"
rsync -a --exclude '__pycache__' "${AGENT_PKG_SRC}" "${STAGE_DIR}/"
cp "${PROTO_GEN_SRC}"/*_pb2*.py "${STAGE_DIR}/"
tar -C "${STAGE_DIR}" -czf "${PAYLOAD}" .

cp "${PAYLOAD}" "${BOOT_MOUNT}/monad-agent-payload.tar.gz"
chmod 600 "${BOOT_MOUNT}/monad-agent-payload.tar.gz" 2>/dev/null || true

USER_DATA="${BOOT_MOUNT}/user-data"
if grep -q "monad-agent-provision" "${USER_DATA}"; then
  echo "Monad agent cloud-init block is already present in ${USER_DATA}"
  echo "Payload refreshed: ${BOOT_MOUNT}/monad-agent-payload.tar.gz"
  exit 0
fi

backup="${USER_DATA}.bak.monad-agent.${TIMESTAMP}"
cp "${USER_DATA}" "${backup}"

cloud_config="$(mktemp)"
cat >"${cloud_config}" <<EOF
#cloud-config
write_files:
  - path: /usr/local/sbin/monad-agent-provision.sh
    owner: root:root
    permissions: '0700'
    content: |
      #!/usr/bin/env bash
      set -euo pipefail

      exec > >(tee -a /var/log/monad-agent-provision.log) 2>&1

      find_boot_payload() {
        for candidate in /boot/firmware/monad-agent-payload.tar.gz /boot/monad-agent-payload.tar.gz; do
          if [[ -f "\${candidate}" ]]; then
            printf '%s\n' "\${candidate}"
            return 0
          fi
        done
        return 1
      }

      wait_for_wg() {
        for _ in \$(seq 1 90); do
          if ip link show ${CONTROL_PLANE_IFACE} >/dev/null 2>&1; then
            return 0
          fi
          sleep 10
        done
        return 0
      }

      payload="\$(find_boot_payload)"
      wait_for_wg

      export DEBIAN_FRONTEND=noninteractive
      for attempt in \$(seq 1 30); do
        if apt-get -o Acquire::ForceIPv4=true update; then
          break
        fi
        echo "apt-get update failed, retry \${attempt}/30"
        sleep 20
      done
      apt-get -o Acquire::ForceIPv4=true install -y \\
        python3.12-venv python3-pip iw bluez wireless-tools tcpdump prometheus
      systemctl enable --now bluetooth || true

      id ${PI_USER} >/dev/null 2>&1 || useradd -m -s /bin/bash ${PI_USER}
      install -d -o ${PI_USER} -g ${PI_USER} "${PI_DIR}"
      tar -xzf "\${payload}" -C "${PI_DIR}"
      chown -R ${PI_USER}:${PI_USER} "${PI_DIR}"

      python3 -m venv "${PI_DIR}/venv"
      "${PI_DIR}/venv/bin/python" -m pip install --upgrade pip
      "${PI_DIR}/venv/bin/python" -m pip install --upgrade \\
        "grpcio>=1.74,<2" \\
        "protobuf>=6.31.1,<7" \\
        "prometheus-client>=0.20,<1"

      agent_id="\$(cat /sys/class/net/${AGENT_ID_IFACE}/address 2>/dev/null || hostname)"
      prom_pi_id="${PROM_PI_ID:-}"
      prom_instance="${PROM_INSTANCE:-}"
      if [[ -z "\${prom_pi_id}" ]]; then
        prom_pi_id="\$(hostname)"
      fi
      if [[ -z "\${prom_instance}" ]]; then
        prom_instance="\${prom_pi_id}"
      fi
      {
        printf '%s\n' 'FLEET_MANAGER_HOST=${FLEET_MANAGER_HOST}'
        printf '%s\n' 'FLEET_MANAGER_PORT=${FLEET_MANAGER_PORT}'
        printf '%s\n' 'GRPC_DNS_RESOLVER=${GRPC_DNS_RESOLVER}'
        printf '%s\n' "AGENT_ID=\${agent_id}"
        printf '%s\n' 'CONTROL_PLANE_MODE=RF_SHARING'
        printf '%s\n' 'CONTROL_PLANE_IFACE=${CONTROL_PLANE_IFACE}'
        printf '%s\n' 'WIFI_SCAN_IFACE=${WIFI_SCAN_IFACE}'
        printf '%s\n' 'MAX_SYNC_CYCLES=${MAX_SYNC_CYCLES}'
        printf '%s\n' 'EXECUTE_POLICY=${EXECUTE_POLICY}'
        printf '%s\n' 'CAPABILITIES=${CAPABILITIES}'
        printf '%s\n' 'DATA_ROOT=${PI_DIR}/data'
        printf '%s\n' 'SENT_RETENTION_DAYS=14'
        printf '%s\n' 'ARTIFACT_UPLOAD_TARGET=${ARTIFACT_UPLOAD_TARGET}'
        printf '%s\n' 'ARTIFACT_UPLOAD_DURING_MEASURE=true'
        printf '%s\n' 'ARTIFACT_EVICT_AFTER_UPLOAD=true'
        printf '%s\n' 'ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S=3'
        printf '%s\n' 'ARTIFACT_UPLOAD_BACKOFF_S=15'
        printf '%s\n' 'GRPC_ARTIFACT_CHUNK_BYTES=1024'
        printf '%s\n' 'ENABLE_COMMAND_STATUS_REPORTS=true'
        printf '%s\n' 'ENABLE_UPLOAD_STATUS_REPORTS=true'
        printf '%s\n' 'RUN_ARTIFACT_SOFT_LIMIT_BYTES=0'
        printf '%s\n' 'METRICS_PORT=9108'
        printf '%s\n' 'DEFAULT_METRICS_SINKS=${DEFAULT_METRICS_SINKS}'
        printf '%s\n' 'PROM_EXPOSITION_ENABLED=true'
        printf '%s\n' 'PROM_EXPOSITION_BIND_HOST=127.0.0.1'
        printf '%s\n' 'PROM_EXPOSITION_PORT=${PROM_EXPOSITION_PORT}'
      } >"${PI_DIR}/.env"
      chown ${PI_USER}:${PI_USER} "${PI_DIR}/.env"
      chmod 600 "${PI_DIR}/.env"

      install -d -m 0755 /etc/prometheus
      {
        printf '%s\n' 'global:'
        printf '%s\n' '  scrape_interval: 15s'
        printf '%s\n' '  evaluation_interval: 15s'
        printf '\n'
        printf '%s\n' 'scrape_configs:'
        printf '%s\n' '  - job_name: monad-fleet-agent'
        printf '%s\n' '    static_configs:'
        printf '%s\n' '      - targets: ["127.0.0.1:${PROM_EXPOSITION_PORT}"]'
        printf '%s\n' '        labels:'
        printf '%s\n' "          agent_id: \"\${agent_id}\""
        printf '%s\n' "          pi_id: \"\${prom_pi_id}\""
        printf '%s\n' '          site: "${PROM_SITE}"'
        printf '%s\n' "          instance: \"\${prom_instance}\""
        printf '\n'
        printf '%s\n' 'remote_write:'
        printf '%s\n' '  - url: "${PROM_REMOTE_WRITE_URL}"'
      } >/etc/prometheus/prometheus.yml

      {
        printf '%s\n' '[Unit]'
        printf '%s\n' 'Description=Monad Fleet Agent v2'
        printf '%s\n' 'After=network-online.target wg-quick@wg0.service'
        printf '%s\n' 'Wants=network-online.target wg-quick@wg0.service'
        printf '\n'
        printf '%s\n' '[Service]'
        printf '%s\n' 'Type=simple'
        printf '%s\n' 'User=${PI_USER}'
        printf '%s\n' 'WorkingDirectory=${PI_DIR}'
        printf '%s\n' 'EnvironmentFile=${PI_DIR}/.env'
        printf '%s\n' 'ExecStart=${PI_DIR}/venv/bin/python -u ${PI_DIR}/agent_v2_client.py'
        printf '%s\n' 'Restart=always'
        printf '%s\n' 'RestartSec=5'
        printf '%s\n' 'StandardOutput=journal'
        printf '%s\n' 'StandardError=journal'
        printf '\n'
        printf '%s\n' '[Install]'
        printf '%s\n' 'WantedBy=multi-user.target'
      } >/etc/systemd/system/monad-fleet-agent.service

      systemctl daemon-reload
      systemctl enable --now prometheus
      systemctl restart prometheus
      systemctl enable --now monad-fleet-agent.service

      rm -f "\${payload}"
      systemctl disable monad-agent-provision.service || true
      rm -f /etc/systemd/system/monad-agent-provision.service
      rm -f /usr/local/sbin/monad-agent-provision.sh
      systemctl daemon-reload

  - path: /etc/systemd/system/monad-agent-provision.service
    owner: root:root
    permissions: '0644'
    content: |
      [Unit]
      Description=Install Monad Fleet agent from boot payload
      Wants=network-online.target wg-quick@wg0.service
      After=network-online.target wg-quick@wg0.service
      ConditionPathExists=/boot/firmware/monad-agent-payload.tar.gz

      [Service]
      Type=oneshot
      ExecStart=/usr/local/sbin/monad-agent-provision.sh
      Restart=on-failure
      RestartSec=60

      [Install]
      WantedBy=multi-user.target

runcmd:
  - [ systemctl, daemon-reload ]
  - [ systemctl, enable, --now, monad-agent-provision.service ]
EOF

python3 - "${USER_DATA}" "${cloud_config}" <<'PY'
import re
import sys
from email import message_from_string
from pathlib import Path

user_data = Path(sys.argv[1])
cloud_config = Path(sys.argv[2]).read_text(encoding="utf-8")
text = user_data.read_text(encoding="utf-8")
match = re.search(r'boundary="?([^"\n;]+)"?', text.split("\n\n", 1)[0])

def split_agent_sections(cloud_cfg: str) -> tuple[str, str]:
    body = cloud_cfg
    if body.startswith("#cloud-config\n"):
        body = body[len("#cloud-config\n") :]
    wf_marker = "write_files:\n"
    rc_marker = "\nruncmd:\n"
    if wf_marker not in body or rc_marker not in body:
        raise SystemExit("agent cloud-config missing write_files or runcmd sections")
    wf_start = body.index(wf_marker) + len(wf_marker)
    rc_start = body.index(rc_marker)
    write_files_items = body[wf_start:rc_start]
    runcmd_items = body[rc_start + len(rc_marker) :]
    return write_files_items, runcmd_items

def merge_cloud_config(existing_cfg: str, addition_cfg: str) -> str:
    write_files_items, runcmd_items = split_agent_sections(addition_cfg)
    if "write_files:\n" not in existing_cfg or "\nruncmd:\n" not in existing_cfg:
        raise SystemExit("existing cloud-config missing write_files or runcmd sections")
    merged = existing_cfg.replace("\nruncmd:\n", "\n" + write_files_items + "runcmd:\n", 1)
    merged = merged.rstrip() + "\n" + runcmd_items.rstrip() + "\n"
    return merged

if match:
    boundary = match.group(1)
    msg = message_from_string(text)
    target_part = None
    cloud_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/cloud-config":
                cloud_parts.append(part)
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                decoded = payload.decode("utf-8", "replace")
                if "monad-wireguard-provision" in decoded:
                    target_part = part
                    break
    if target_part is None and cloud_parts:
        target_part = cloud_parts[-1]
    if target_part is None:
        closing = f"--{boundary}--"
        if closing not in text:
            raise SystemExit(f"closing MIME boundary not found: {closing}")
        insert = (
            f"--{boundary}\n"
            "Content-Type: text/cloud-config; charset=\"us-ascii\"\n\n"
            f"{cloud_config}\n"
        )
        text = text.replace(closing, insert + closing, 1)
    else:
        payload = target_part.get_payload(decode=True)
        if payload is None:
            raise SystemExit("target cloud-config part has empty payload")
        existing_cfg = payload.decode("utf-8", "replace")
        merged_cfg = merge_cloud_config(existing_cfg, cloud_config)
        target_part.set_payload(merged_cfg)
        text = msg.as_string()
else:
    boundary = "===============monad_agent_boot=="
    if text.startswith("#cloud-config"):
        original_type = "text/cloud-config"
    elif text.startswith("#!"):
        original_type = "text/x-shellscript"
    else:
        original_type = "text/plain"
    text = (
        "MIME-Version: 1.0\n"
        f"Content-Type: multipart/mixed; boundary=\"{boundary}\"\n\n"
        f"--{boundary}\n"
        f"Content-Type: {original_type}; charset=\"us-ascii\"\n\n"
        f"{text.rstrip()}\n\n"
        f"--{boundary}\n"
        "Content-Type: text/cloud-config; charset=\"us-ascii\"\n\n"
        f"{cloud_config.rstrip()}\n\n"
        f"--{boundary}--\n"
    )

user_data.write_text(text, encoding="utf-8")
PY

rm -f "${cloud_config}"
sync

cat <<EOF
Applied Monad agent boot bundle.

Boot mount: ${BOOT_MOUNT}
Payload:    ${BOOT_MOUNT}/monad-agent-payload.tar.gz
Backup:     ${backup}

First boot will install the agent for user ${PI_USER}, then start
monad-fleet-agent.service pointing at ${FLEET_MANAGER_HOST}:${FLEET_MANAGER_PORT}.
EOF
