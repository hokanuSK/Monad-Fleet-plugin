#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PI_HOST="${PI_HOST:-${1:-monad-rpi5.local}}"
PI_USER="${PI_USER:-admin}"
PI_WORKDIR="${PI_WORKDIR:-/home/${PI_USER}/src}"
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

echo "[1/5] Install build dependencies on ${PI_USER}@${PI_HOST}"
run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
sudo apt-get update
sudo apt-get install -y \
  git build-essential dkms bc flex bison libelf-dev libssl-dev pkg-config \
  libgtkmm-3.0-dev libnl-3-dev libnl-genl-3-dev libiw-dev libpcap-dev \
  linux-headers-\$(uname -r) || true
if ! dpkg -s linux-headers-\$(uname -r) >/dev/null 2>&1; then
  sudo apt-get install -y linux-headers-rpi-2712 || true
fi
'"

echo "[2/5] Clone or update FeitCSI repositories"
run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
mkdir -p \"${PI_WORKDIR}\"
cd \"${PI_WORKDIR}\"
if [[ ! -d FeitCSI-iwlwifi/.git ]]; then
  git clone https://github.com/KuskoSoft/FeitCSI-iwlwifi.git
fi
if [[ ! -d FeitCSI/.git ]]; then
  git clone https://github.com/KuskoSoft/FeitCSI.git
fi
cd FeitCSI-iwlwifi
git fetch --all --tags
git pull --ff-only
cd ../FeitCSI
git fetch --all --tags
git pull --ff-only
'"

echo "[3/5] Build and install FeitCSI iwlwifi driver"
run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
cd \"${PI_WORKDIR}/FeitCSI-iwlwifi\"
make defconfig-iwlwifi-public
# Repair accidental stale line from manual edits when present.
sed -i \"s/^iwlmld- += d3.o$/iwlmld-\\\\\$(CONFIG_PM_SLEEP) += d3.o/\" drivers/net/wireless/intel/iwlwifi/mld/Makefile || true

build_log=/tmp/feitcsi_iwlwifi_build.log
if ! make -j\$(nproc) >\"\${build_log}\" 2>&1; then
  # Kernel/API mismatch seen on some Raspberry Pi kernels with CPTCFG_IWLMLD enabled.
  if grep -q \"iwl_mld_no_wowlan_suspend\" \"\${build_log}\"; then
    echo \"Detected iwl_mld compatibility issue; retrying with CPTCFG_IWLMLD disabled\"
    sed -i \"s/^CPTCFG_IWLMLD=m/# CPTCFG_IWLMLD is not set/\" .config
    make olddefconfig >/dev/null 2>&1 || true
    make -j\$(nproc)
  else
    echo \"Driver build failed. Tail of \${build_log}:\" >&2
    tail -n 120 \"\${build_log}\" >&2 || true
    exit 1
  fi
fi
sudo make install
sudo depmod -a || true
'"

echo "[4/5] Build and install FeitCSI userspace tool"
run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
cd \"${PI_WORKDIR}/FeitCSI\"
make -j\$(nproc)
sudo make install
'"

echo "[5/5] Verify installation"
run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
which feitcsi || true
/usr/sbin/modinfo iwlwifi | head -n 8 || true
'"

echo "FeitCSI installation command sequence completed."
echo "If CSI still reports not configured, reboot Pi to load installed iwlwifi module:"
echo "  ssh ${PI_USER}@${PI_HOST} \"sudo reboot\""
echo "Warning: running active CSI capture on a Wi-Fi-only control link can drop SSH."
echo "Prefer Ethernet control-plane when collecting real CSI."
