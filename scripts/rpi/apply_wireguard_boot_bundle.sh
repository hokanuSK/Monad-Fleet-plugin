#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/rpi/apply_wireguard_boot_bundle.sh <device-bundle-dir> <mounted-boot-volume>

Example:
  scripts/rpi/apply_wireguard_boot_bundle.sh \
    artifacts/output/rpi-wireguard-flash/20260512T120000Z/monad-04 \
    /Volumes/bootfs

The mounted boot volume is the FAT partition produced by Raspberry Pi Imager.
This script copies firstrun.sh + monad-wireguard.env and patches cmdline.txt so
the Pi installs/enables wg0 on first boot.

Environment:
  FIRSTBOOT_RUN_PATH  path visible inside the Pi OS root filesystem
                      default: /boot/firmware/firstrun.sh
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

DEVICE_DIR="${1:-}"
BOOT_MOUNT="${2:-}"
FIRSTBOOT_RUN_PATH="${FIRSTBOOT_RUN_PATH:-/boot/firmware/firstrun.sh}"

if [[ -z "${DEVICE_DIR}" || -z "${BOOT_MOUNT}" ]]; then
  usage >&2
  exit 2
fi

if [[ ! -d "${DEVICE_DIR}/boot" ]]; then
  echo "ERROR: device bundle missing boot directory: ${DEVICE_DIR}/boot" >&2
  exit 2
fi

if [[ ! -d "${BOOT_MOUNT}" ]]; then
  echo "ERROR: boot mount is not a directory: ${BOOT_MOUNT}" >&2
  exit 2
fi

if [[ ! -f "${BOOT_MOUNT}/cmdline.txt" ]]; then
  echo "ERROR: cmdline.txt not found in boot mount: ${BOOT_MOUNT}" >&2
  exit 2
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
cp "${BOOT_MOUNT}/cmdline.txt" "${BOOT_MOUNT}/cmdline.txt.bak.monad-wireguard.${timestamp}"

cp "${DEVICE_DIR}/boot/firstrun.sh" "${BOOT_MOUNT}/firstrun.sh"
cp "${DEVICE_DIR}/boot/monad-wireguard.env" "${BOOT_MOUNT}/monad-wireguard.env"
chmod 755 "${BOOT_MOUNT}/firstrun.sh" 2>/dev/null || true
chmod 600 "${BOOT_MOUNT}/monad-wireguard.env" 2>/dev/null || true

clean_cmdline="$(
  tr ' ' '\n' <"${BOOT_MOUNT}/cmdline.txt" \
    | grep -v '^systemd\.run=' \
    | grep -v '^systemd\.run_success_action=' \
    | grep -v '^systemd\.unit=kernel-command-line\.target$' \
    | paste -sd ' ' -
)"

printf '%s systemd.run=%s systemd.run_success_action=reboot systemd.unit=kernel-command-line.target\n' \
  "${clean_cmdline}" "${FIRSTBOOT_RUN_PATH}" >"${BOOT_MOUNT}/cmdline.txt"

sync

cat <<EOF
Applied WireGuard first-boot bundle.

Device bundle: ${DEVICE_DIR}
Boot mount:    ${BOOT_MOUNT}
Run path:      ${FIRSTBOOT_RUN_PATH}
Backup:        ${BOOT_MOUNT}/cmdline.txt.bak.monad-wireguard.${timestamp}

Eject the SD card, boot the Pi, and wait for the automatic first-boot reboot.
EOF
