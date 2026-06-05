#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/rpi/apply_ubuntu_wireguard_boot_bundle.sh <device-bundle-dir> <mounted-system-boot-volume>

Example:
  scripts/rpi/apply_ubuntu_wireguard_boot_bundle.sh \
    artifacts/output/rpi-wireguard-flash/20260512T120000Z/monad-03 \
    /Volumes/system-boot

Use this after Raspberry Pi Imager writes Ubuntu Server 24.04 LTS. It preserves
the existing cloud-init user-data by converting it to a multipart document and
adds a Monad WireGuard provisioning part.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

DEVICE_DIR="${1:-}"
BOOT_MOUNT="${2:-}"

if [[ -z "${DEVICE_DIR}" || -z "${BOOT_MOUNT}" ]]; then
  usage >&2
  exit 2
fi

ENV_SRC="${DEVICE_DIR}/boot/monad-wireguard.env"
if [[ ! -f "${ENV_SRC}" ]]; then
  echo "ERROR: device bundle is missing ${ENV_SRC}" >&2
  exit 2
fi

if [[ ! -d "${BOOT_MOUNT}" ]]; then
  echo "ERROR: system-boot mount is not a directory: ${BOOT_MOUNT}" >&2
  exit 2
fi

USER_DATA="${BOOT_MOUNT}/user-data"
if [[ ! -f "${USER_DATA}" ]]; then
  echo "ERROR: Ubuntu cloud-init user-data not found: ${USER_DATA}" >&2
  exit 2
fi

# shellcheck disable=SC1090
source "${ENV_SRC}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="${USER_DATA}.bak.monad-wireguard.${timestamp}"
boundary="===============monad_wireguard_${timestamp}=="
original_part_type="text/plain"
if head -n 1 "${USER_DATA}" | grep -q '^#cloud-config'; then
  original_part_type="text/cloud-config"
elif head -n 1 "${USER_DATA}" | grep -q '^#!'; then
  original_part_type="text/x-shellscript"
fi

cp "${USER_DATA}" "${backup}"
cp "${ENV_SRC}" "${BOOT_MOUNT}/monad-wireguard.env"
chmod 600 "${BOOT_MOUNT}/monad-wireguard.env" 2>/dev/null || true

monad_cloud_config="$(mktemp)"
cat >"${monad_cloud_config}" <<'CLOUD'
#cloud-config
write_files:
  - path: /usr/local/sbin/monad-wireguard-provision.sh
    owner: root:root
    permissions: '0700'
    content: |
      #!/usr/bin/env bash
      set -euo pipefail

      exec > >(tee -a /var/log/monad-wireguard-provision.log) 2>&1

      find_boot_env() {
        for candidate in /boot/firmware/monad-wireguard.env /boot/monad-wireguard.env; do
          if [[ -f "${candidate}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
          fi
        done
        return 1
      }

      env_file="$(find_boot_env)"
      # shellcheck disable=SC1090
      source "${env_file}"

      hostnamectl set-hostname "${MONAD_HOSTNAME}"
      if grep -q '^127\.0\.1\.1' /etc/hosts; then
        sed -i "s/^127\\.0\\.1\\.1.*/127.0.1.1 ${MONAD_HOSTNAME}/" /etc/hosts
      else
        printf '127.0.1.1 %s\n' "${MONAD_HOSTNAME}" >>/etc/hosts
      fi

      if [[ "${MONAD_INSTALL_WIREGUARD}" == "true" ]] && ! command -v wg-quick >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        for attempt in $(seq 1 30); do
          if apt-get update; then
            break
          fi
          echo "apt-get update failed, retry ${attempt}/30"
          sleep 20
        done
        apt-get install -y wireguard wireguard-tools
      fi

      install -d -m 700 /etc/wireguard
      umask 077
      {
        printf '[Interface]\n'
        printf 'Address = %s/32\n' "${MONAD_VPN_IP}"
        printf 'PrivateKey = %s\n' "${MONAD_WG_PRIVATE_KEY}"
        if [[ -n "${MONAD_WG_MTU}" ]]; then
          printf 'MTU = %s\n' "${MONAD_WG_MTU}"
        fi
        if [[ -n "${MONAD_WG_DNS}" ]]; then
          printf 'DNS = %s\n' "${MONAD_WG_DNS}"
        fi
        printf '\n[Peer]\n'
        printf 'PublicKey = %s\n' "${MONAD_WG_SERVER_PUBLIC_KEY}"
        printf 'AllowedIPs = %s\n' "${MONAD_WG_ALLOWED_IPS}"
        printf 'Endpoint = %s\n' "${MONAD_WG_SERVER_ENDPOINT}"
        printf 'PersistentKeepalive = %s\n' "${MONAD_WG_PERSISTENT_KEEPALIVE}"
      } >/etc/wireguard/wg0.conf
      chmod 600 /etc/wireguard/wg0.conf

      systemctl enable --now wg-quick@wg0
      wg show wg0 || true

      rm -f "${env_file}"
      systemctl disable monad-wireguard-provision.service || true
      rm -f /etc/systemd/system/monad-wireguard-provision.service
      rm -f /usr/local/sbin/monad-wireguard-provision.sh
      systemctl daemon-reload

  - path: /etc/systemd/system/monad-wireguard-provision.service
    owner: root:root
    permissions: '0644'
    content: |
      [Unit]
      Description=Provision Monad WireGuard tunnel
      Wants=network-online.target
      After=network-online.target
      ConditionPathExists=/boot/firmware/monad-wireguard.env

      [Service]
      Type=oneshot
      ExecStart=/usr/local/sbin/monad-wireguard-provision.sh
      Restart=on-failure
      RestartSec=60

      [Install]
      WantedBy=multi-user.target

runcmd:
  - [ systemctl, daemon-reload ]
  - [ systemctl, enable, --now, monad-wireguard-provision.service ]
CLOUD

{
  printf 'MIME-Version: 1.0\n'
  printf 'Content-Type: multipart/mixed; boundary="%s"\n\n' "${boundary}"
  printf -- '--%s\n' "${boundary}"
  printf 'Content-Type: %s; charset="us-ascii"\n\n' "${original_part_type}"
  cat "${backup}"
  printf '\n--%s\n' "${boundary}"
  printf 'Content-Type: text/cloud-config; charset="us-ascii"\n\n'
  cat "${monad_cloud_config}"
  printf '\n--%s--\n' "${boundary}"
} >"${USER_DATA}"

rm -f "${monad_cloud_config}"
sync

cat <<EOF
Applied Ubuntu WireGuard cloud-init bundle.

Device:       ${MONAD_HOSTNAME}
VPN IP:       ${MONAD_VPN_IP}
Boot mount:   ${BOOT_MOUNT}
Backup:       ${backup}

Eject the SD card and boot the Pi. Cloud-init will install WireGuard once
networking is online, enable wg0, and remove monad-wireguard.env from the boot
partition after success.
EOF
