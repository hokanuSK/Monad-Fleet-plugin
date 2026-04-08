#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KEY_DIR="${KEY_DIR:-${ROOT_DIR}/scripts/rpi/keys}"
KEY_NAME="${KEY_NAME:-monad_rpi5_ed25519}"
KEY_PATH="${KEY_PATH:-${KEY_DIR}/${KEY_NAME}}"

PI_USER="${PI_USER:-admin}"
PI_HOST="${PI_HOST:-monad-rpi5.local}"
PI_HOST_FALLBACK="${PI_HOST_FALLBACK:-192.168.0.234}"
FORCE_NEW="${FORCE_NEW:-false}"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=6 -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new)

mkdir -p "${KEY_DIR}"

if [[ -f "${KEY_PATH}" && "${FORCE_NEW}" == "true" ]]; then
  rm -f "${KEY_PATH}" "${KEY_PATH}.pub"
fi

if [[ ! -f "${KEY_PATH}" ]]; then
  ssh-keygen -t ed25519 -a 100 -N "" -C "fleet-rpi5-$(date +%Y%m%d-%H%M%S)" -f "${KEY_PATH}" >/dev/null
fi

echo "KEY_PATH=${KEY_PATH}"
echo "PUBLIC_KEY:"
cat "${KEY_PATH}.pub"
echo

echo "Testing SSH with new key..."
if ssh -i "${KEY_PATH}" "${SSH_OPTS[@]}" "${PI_USER}@${PI_HOST}" "echo ONLINE && hostname && date"; then
  echo "Connected via ${PI_HOST}"
  exit 0
fi

if ssh -i "${KEY_PATH}" "${SSH_OPTS[@]}" "${PI_USER}@${PI_HOST_FALLBACK}" "echo ONLINE && hostname && date"; then
  echo "Connected via ${PI_HOST_FALLBACK}"
  exit 0
fi

echo "Pi not reachable with SSH key yet."
echo "When the Pi is reachable and password auth is available, install key with:"
echo "  cat '${KEY_PATH}.pub' >> ~/.ssh/authorized_keys"
echo "or:"
echo "  ssh-copy-id -i '${KEY_PATH}.pub' ${PI_USER}@<pi-host>"
