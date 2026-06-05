#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEFAULT_DEVICE_CSV="${ROOT_DIR}/scripts/rpi/wireguard_flash_devices_america.csv"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

DEVICE_CSV="${DEVICE_CSV:-${DEFAULT_DEVICE_CSV}}"
OUT_DIR="${OUT_DIR:-${ROOT_DIR}/artifacts/output/rpi-wireguard-flash/${TIMESTAMP}}"
KEY_DIR="${KEY_DIR:-${ROOT_DIR}/artifacts/private/rpi-wireguard-keys}"

SERVER_PUBLIC_KEY="${SERVER_PUBLIC_KEY:-OYe0AG2KfrwToF0RMAOYTY3rkkUsOx/75J6D/FEQWQA=}"
SERVER_ENDPOINT="${SERVER_ENDPOINT:-34.198.184.128:51820}"
WG_ALLOWED_IPS="${WG_ALLOWED_IPS:-10.200.0.0/24}"
WG_MTU="${WG_MTU:-1280}"
WG_DNS="${WG_DNS:-}"
WG_PERSISTENT_KEEPALIVE="${WG_PERSISTENT_KEEPALIVE:-25}"
INSTALL_WIREGUARD="${INSTALL_WIREGUARD:-true}"
FIRSTBOOT_RUN_PATH="${FIRSTBOOT_RUN_PATH:-/boot/firmware/firstrun.sh}"
WG_KEYGEN_SSH="${WG_KEYGEN_SSH:-}"

usage() {
  cat <<EOF
Usage: scripts/rpi/generate_wireguard_flash_bundle.sh

Generates per-device Raspberry Pi boot-partition bundles that install and
enable WireGuard on first boot.

Environment:
  DEVICE_CSV              CSV with columns: hostname,vpn_ip for devices that
                          need fresh WireGuard keys and boot bundles
                          default: ${DEFAULT_DEVICE_CSV}
  OUT_DIR                 output directory
                          default: artifacts/output/rpi-wireguard-flash/<timestamp>
  KEY_DIR                 private key store, gitignored under artifacts/
                          default: artifacts/private/rpi-wireguard-keys
  SERVER_PUBLIC_KEY       WireGuard server public key
                          default: America EC2 wg0 public key
  SERVER_ENDPOINT         WireGuard endpoint host:port
                          default: 34.198.184.128:51820
  WG_ALLOWED_IPS          client AllowedIPs; split tunnel by default
                          default: 10.200.0.0/24
  WG_MTU                  client MTU
                          default: 1280
  WG_KEYGEN_SSH           optional SSH target with wg installed when local wg is absent
                          example: ladamik@34.198.184.128
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ ! -f "${DEVICE_CSV}" ]]; then
  echo "ERROR: device CSV not found: ${DEVICE_CSV}" >&2
  exit 2
fi

if [[ ${#SERVER_PUBLIC_KEY} -ne 44 ]]; then
  echo "ERROR: SERVER_PUBLIC_KEY must be a 44-character WireGuard public key" >&2
  exit 2
fi

have_local_wg() {
  command -v wg >/dev/null 2>&1
}

have_python3() {
  command -v python3 >/dev/null 2>&1
}

python_wg_genkey() {
  python3 - <<'PY'
import base64
import os

key = bytearray(os.urandom(32))
key[0] &= 248
key[31] &= 127
key[31] |= 64
print(base64.b64encode(bytes(key)).decode("ascii"))
PY
}

python_wg_pubkey() {
  local private_key="$1"
  PRIVATE_KEY="${private_key}" python3 - <<'PY'
import base64
import os

P = 2 ** 255 - 19
A24 = 121665


def x25519(scalar: bytes, u: bytes) -> bytes:
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    x1 = int.from_bytes(u, "little")
    x2, z2 = 1, 0
    x3, z3 = x1, 1
    swap = 0

    for t in range(254, -1, -1):
        kt = (k[t // 8] >> (t & 7)) & 1
        swap ^= kt
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = kt

        a = (x2 + z2) % P
        aa = (a * a) % P
        b = (x2 - z2) % P
        bb = (b * b) % P
        e = (aa - bb) % P
        c = (x3 + z3) % P
        d = (x3 - z3) % P
        da = (d * a) % P
        cb = (c * b) % P
        x3 = ((da + cb) ** 2) % P
        z3 = (x1 * ((da - cb) ** 2)) % P
        x2 = (aa * bb) % P
        z2 = (e * (aa + A24 * e)) % P

    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2

    return (x2 * pow(z2, P - 2, P) % P).to_bytes(32, "little")


private_key = os.environ["PRIVATE_KEY"].strip()
private = base64.b64decode(private_key)
if len(private) != 32:
    raise SystemExit("private key must decode to 32 bytes")
public = x25519(private, bytes([9]) + bytes(31))
print(base64.b64encode(public).decode("ascii"))
PY
}

run_wg() {
  if have_local_wg; then
    wg "$@"
    return
  fi
  if [[ "${1:-}" == "genkey" ]] && have_python3; then
    python_wg_genkey
    return
  fi
  if [[ -n "${WG_KEYGEN_SSH}" ]]; then
    ssh "${WG_KEYGEN_SSH}" wg "$@"
    return
  fi
  echo "ERROR: wg is not installed locally and python3 fallback is unavailable. Install wireguard-tools or set WG_KEYGEN_SSH=ladamik@34.198.184.128" >&2
  exit 2
}

wg_public_from_private() {
  local private_key="$1"
  if have_local_wg; then
    printf '%s\n' "${private_key}" | wg pubkey
    return
  fi
  if have_python3; then
    python_wg_pubkey "${private_key}"
    return
  fi
  if [[ -n "${WG_KEYGEN_SSH}" ]]; then
    printf '%s\n' "${private_key}" | ssh "${WG_KEYGEN_SSH}" wg pubkey
    return
  fi
  echo "ERROR: cannot derive WireGuard public key" >&2
  exit 2
}

emit_firstrun() {
  local path="$1"
  cat >"${path}" <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

exec > >(tee -a /var/log/monad-wireguard-firstrun.log) 2>&1

find_boot_file() {
  local name="$1"
  for candidate in "/boot/firmware/${name}" "/boot/${name}"; do
    if [[ -f "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

remove_kernel_args() {
  local cmdline
  cmdline="$(find_boot_file cmdline.txt || true)"
  if [[ -z "${cmdline}" ]]; then
    return 0
  fi
  cp "${cmdline}" "${cmdline}.bak.monad-wireguard.$(date -u +%Y%m%dT%H%M%SZ)"
  tr ' ' '\n' <"${cmdline}" \
    | grep -v '^systemd\.run=' \
    | grep -v '^systemd\.run_success_action=' \
    | grep -v '^systemd\.unit=kernel-command-line\.target$' \
    | paste -sd ' ' - >"${cmdline}.new"
  mv "${cmdline}.new" "${cmdline}"
}

ENV_FILE="$(find_boot_file monad-wireguard.env)"
# shellcheck disable=SC1090
source "${ENV_FILE}"

hostnamectl set-hostname "${MONAD_HOSTNAME}"
if grep -q '^127\.0\.1\.1' /etc/hosts; then
  sed -i "s/^127\\.0\\.1\\.1.*/127.0.1.1 ${MONAD_HOSTNAME}/" /etc/hosts
else
  printf '127.0.1.1 %s\n' "${MONAD_HOSTNAME}" >>/etc/hosts
fi

install -d -m 700 /root
install -m 600 "${ENV_FILE}" /root/monad-wireguard.env

cat >/usr/local/sbin/monad-wireguard-provision.sh <<'PROVISION'
#!/usr/bin/env bash
set -euo pipefail

exec > >(tee -a /var/log/monad-wireguard-provision.log) 2>&1

# shellcheck disable=SC1091
source /root/monad-wireguard.env

if [[ "${MONAD_INSTALL_WIREGUARD}" == "true" ]] && ! command -v wg-quick >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
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

rm -f /root/monad-wireguard.env
for candidate in /boot/firmware/monad-wireguard.env /boot/monad-wireguard.env; do
  rm -f "${candidate}"
done
systemctl disable monad-wireguard-provision.service || true
rm -f /etc/systemd/system/monad-wireguard-provision.service
rm -f /usr/local/sbin/monad-wireguard-provision.sh
systemctl daemon-reload
PROVISION
chmod 700 /usr/local/sbin/monad-wireguard-provision.sh

cat >/etc/systemd/system/monad-wireguard-provision.service <<'UNIT'
[Unit]
Description=Provision Monad WireGuard tunnel
Wants=network-online.target
After=network-online.target
ConditionPathExists=/root/monad-wireguard.env

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/monad-wireguard-provision.sh

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable monad-wireguard-provision.service

remove_kernel_args
for candidate in /boot/firmware/firstrun.sh /boot/firstrun.sh; do
  rm -f "${candidate}"
done
SCRIPT
  chmod 755 "${path}"
}

mkdir -p "${OUT_DIR}" "${KEY_DIR}"
chmod 700 "${KEY_DIR}"

server_peers="${OUT_DIR}/server_peers.conf"
server_apply="${OUT_DIR}/server_apply_peers.sh"
manifest="${OUT_DIR}/manifest.csv"

cat >"${server_peers}" <<EOF
# Append these peers to /etc/wireguard/wg0.conf on 34.198.184.128.
# Generated ${TIMESTAMP}. Contains public keys only.
EOF

cat >"${server_apply}" <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

WG_CONF="${WG_CONF:-/etc/wireguard/wg0.conf}"
WG_IFACE="${WG_IFACE:-wg0}"

add_peer() {
  local label="$1"
  local public_key="$2"
  local allowed_ips="$3"

  if ! sudo grep -qF "${public_key}" "${WG_CONF}"; then
    {
      printf '\n[Peer]\n'
      printf '# %s\n' "${label}"
      printf 'PublicKey = %s\n' "${public_key}"
      printf 'AllowedIPs = %s\n' "${allowed_ips}"
    } | sudo tee -a "${WG_CONF}" >/dev/null
  fi

  sudo wg set "${WG_IFACE}" peer "${public_key}" allowed-ips "${allowed_ips}"
}

sudo cp "${WG_CONF}" "${WG_CONF}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
SCRIPT

printf 'hostname,vpn_ip,public_key\n' >"${manifest}"

tail -n +2 "${DEVICE_CSV}" | while IFS=, read -r hostname vpn_ip rest; do
  hostname="${hostname//$'\r'/}"
  vpn_ip="${vpn_ip//$'\r'/}"
  if [[ -z "${hostname}" || -z "${vpn_ip}" ]]; then
    continue
  fi
  if [[ -n "${rest:-}" ]]; then
    echo "ERROR: too many CSV fields for ${hostname}" >&2
    exit 2
  fi

  device_key_dir="${KEY_DIR}/${hostname}"
  mkdir -p "${device_key_dir}"
  chmod 700 "${device_key_dir}"

  private_key_file="${device_key_dir}/privatekey"
  public_key_file="${device_key_dir}/publickey"

  if [[ ! -s "${private_key_file}" ]]; then
    run_wg genkey >"${private_key_file}"
    chmod 600 "${private_key_file}"
  fi

  private_key="$(<"${private_key_file}")"
  public_key="$(wg_public_from_private "${private_key}")"
  printf '%s\n' "${public_key}" >"${public_key_file}"
  chmod 644 "${public_key_file}"

  device_dir="${OUT_DIR}/${hostname}"
  boot_dir="${device_dir}/boot"
  mkdir -p "${boot_dir}"

  cat >"${boot_dir}/monad-wireguard.env" <<EOF
MONAD_HOSTNAME='${hostname}'
MONAD_VPN_IP='${vpn_ip}'
MONAD_WG_PRIVATE_KEY='${private_key}'
MONAD_WG_SERVER_PUBLIC_KEY='${SERVER_PUBLIC_KEY}'
MONAD_WG_SERVER_ENDPOINT='${SERVER_ENDPOINT}'
MONAD_WG_ALLOWED_IPS='${WG_ALLOWED_IPS}'
MONAD_WG_MTU='${WG_MTU}'
MONAD_WG_DNS='${WG_DNS}'
MONAD_WG_PERSISTENT_KEEPALIVE='${WG_PERSISTENT_KEEPALIVE}'
MONAD_INSTALL_WIREGUARD='${INSTALL_WIREGUARD}'
EOF
  chmod 600 "${boot_dir}/monad-wireguard.env"

  emit_firstrun "${boot_dir}/firstrun.sh"

  cat >"${device_dir}/wg0.conf" <<EOF
[Interface]
Address = ${vpn_ip}/32
PrivateKey = ${private_key}
MTU = ${WG_MTU}

[Peer]
PublicKey = ${SERVER_PUBLIC_KEY}
AllowedIPs = ${WG_ALLOWED_IPS}
Endpoint = ${SERVER_ENDPOINT}
PersistentKeepalive = ${WG_PERSISTENT_KEEPALIVE}
EOF
  chmod 600 "${device_dir}/wg0.conf"

  printf '%s\n' "${public_key}" >"${device_dir}/publickey"
  chmod 644 "${device_dir}/publickey"

  cat >"${device_dir}/README.txt" <<EOF
Device: ${hostname}
VPN IP: ${vpn_ip}/32
Public key: ${public_key}

After flashing Raspberry Pi OS, mount the boot partition and run:

  scripts/rpi/apply_wireguard_boot_bundle.sh ${device_dir} /Volumes/<boot-volume>

Then eject the card and boot the Pi. The first boot installs/enables wg0,
sets hostname=${hostname}, and removes the boot-time private-key payload.
EOF

  cat >>"${server_peers}" <<EOF

[Peer]
# ${hostname}
PublicKey = ${public_key}
AllowedIPs = ${vpn_ip}/32
EOF

  printf 'add_peer %q %q %q\n' "${hostname}" "${public_key}" "${vpn_ip}/32" >>"${server_apply}"
  printf '%s,%s,%s\n' "${hostname}" "${vpn_ip}" "${public_key}" >>"${manifest}"
done

cat >>"${server_apply}" <<'SCRIPT'

sudo chmod 600 "${WG_CONF}"
sudo wg show "${WG_IFACE}"
SCRIPT
chmod 755 "${server_apply}"

cat >"${OUT_DIR}/README.md" <<EOF
# Monad WireGuard Flash Bundle

Generated: ${TIMESTAMP}

This directory contains first-boot bundles for Raspberry Pis plus an EC2
server-side peer installer.

## 1. Add peers on EC2

\`\`\`bash
scp "${server_apply}" ladamik@34.198.184.128:/tmp/monad-add-wg-peers.sh
ssh -t ladamik@34.198.184.128 'bash /tmp/monad-add-wg-peers.sh'
\`\`\`

The script appends peer blocks if missing, hot-adds them with \`wg set\`, and
does not bounce \`wg0\`.

## 2. Flash and apply each boot bundle

Flash Ubuntu Server 24.04 LTS with Raspberry Pi Imager. Configure SSH and
LAN/Wi-Fi there as usual. After flashing, mount the `system-boot` partition and
run:

\`\`\`bash
scripts/rpi/apply_ubuntu_wireguard_boot_bundle.sh "${OUT_DIR}/monad-03" /Volumes/system-boot
\`\`\`

Repeat with the matching device directory for each SD card.

## 3. Verify

\`\`\`bash
ssh -J ladamik@34.198.184.128 <pi-user>@10.200.0.13
sudo wg show wg0
\`\`\`

Private keys are stored only under the gitignored \`KEY_DIR\` and per-device
bundle directories. Treat \`${OUT_DIR}\` as secret material until every SD card
has first-booted and removed its boot payload.
EOF

cat <<EOF
Generated WireGuard flash bundle:
  ${OUT_DIR}

Server peer installer:
  ${server_apply}

Manifest:
  ${manifest}

Private key store:
  ${KEY_DIR}

Next:
  1. Run the server peer installer on 34.198.184.128.
  2. Flash each SD card.
  3. Run scripts/rpi/apply_wireguard_boot_bundle.sh <device-dir> <mounted-boot-volume>.
EOF
