#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PI_HOST="${PI_HOST:-${1:-monad-rpi5.local}}"
PI_USER="${PI_USER:-admin}"
SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE:-}"
USE_DEFAULT_SSH_KEY="${USE_DEFAULT_SSH_KEY:-false}"
DEFAULT_SSH_KEY="${ROOT_DIR}/scripts/rpi/keys/monad_rpi5_ed25519"
SSH_CONNECT_TIMEOUT_S="${SSH_CONNECT_TIMEOUT_S:-10}"
SSH_SERVER_ALIVE_INTERVAL_S="${SSH_SERVER_ALIVE_INTERVAL_S:-5}"
SSH_SERVER_ALIVE_COUNT_MAX="${SSH_SERVER_ALIVE_COUNT_MAX:-3}"

if [[ -z "${SSH_IDENTITY_FILE}" && "${USE_DEFAULT_SSH_KEY}" == "true" && -f "${DEFAULT_SSH_KEY}" ]]; then
  SSH_IDENTITY_FILE="${DEFAULT_SSH_KEY}"
fi

SSH_ARGS=(
  -o "ConnectTimeout=${SSH_CONNECT_TIMEOUT_S}"
  -o "ServerAliveInterval=${SSH_SERVER_ALIVE_INTERVAL_S}"
  -o "ServerAliveCountMax=${SSH_SERVER_ALIVE_COUNT_MAX}"
)
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

echo "Collecting FeitCSI compatibility snapshot on ${PI_USER}@${PI_HOST}"
run_ssh "${PI_USER}@${PI_HOST}" "bash -s" <<'REMOTE'
set -euo pipefail

kernel_release="$(uname -r)"
kernel_full="$(uname -a)"
os_release="$(. /etc/os-release && printf '%s %s' "${ID:-unknown}" "${VERSION_ID:-unknown}")"
iwlwifi_path="$(modinfo -n iwlwifi 2>/dev/null || true)"
iwlwifi_vermagic="$(modinfo -F vermagic iwlwifi 2>/dev/null || true)"
iwlwifi_srcversion="$(modinfo -F srcversion iwlwifi 2>/dev/null || true)"
iwlwifi_version="$(modinfo -F version iwlwifi 2>/dev/null || true)"
if [[ -z "${iwlwifi_path}" ]]; then
  iwlwifi_path="$(find "/lib/modules/${kernel_release}" -type f -name 'iwlwifi*.ko*' 2>/dev/null | head -n1 || true)"
fi

feitcsi_path="$(command -v feitcsi 2>/dev/null || true)"
if [[ -z "${feitcsi_path}" && -x /usr/local/bin/feitcsi ]]; then
  feitcsi_path="/usr/local/bin/feitcsi"
fi
has_feitcsi=0
if [[ -n "${feitcsi_path}" ]]; then
  has_feitcsi=1
fi

wiphy_count="$(/usr/sbin/iw phy 2>/dev/null | grep -c '^Wiphy ' || true)"
iface_snapshot="$(ip -brief a | tr -s ' ')"

oom_kill_count="$(dmesg | grep -Ec 'Out of memory: Killed process .*feitcsi' || true)"
oom_invoke_count="$(dmesg | grep -Ec 'feitcsi invoked oom-killer' || true)"
monitor_dup_count="$(dmesg | grep -Ec 'debugfs: Directory .*netdev:phy0-monitor.*already present' || true)"
symbol_mismatch_count="$(dmesg | grep -Ec 'brcmfmac: .*Unknown symbol .*cfg80211' || true)"

status="PASS"
reasons=()

if [[ "${has_feitcsi}" != "1" ]]; then
  status="FAIL"
  reasons+=("feitcsi_missing")
fi
if (( oom_kill_count > 0 || oom_invoke_count > 0 )); then
  status="FAIL"
  reasons+=("feitcsi_oom_kill")
fi
if (( monitor_dup_count > 0 )); then
  if [[ "${status}" == "PASS" ]]; then
    status="WARN"
  fi
  reasons+=("monitor_debugfs_duplicates")
fi
if (( symbol_mismatch_count > 0 )); then
  if [[ "${status}" == "PASS" ]]; then
    status="WARN"
  fi
  reasons+=("kernel_module_symbol_mismatch")
fi
if (( wiphy_count < 1 )); then
  status="FAIL"
  reasons+=("no_wireless_phy")
fi

if [[ ${#reasons[@]} -eq 0 ]]; then
  reasons=("none")
fi

echo "COMPAT_STATUS=${status}"
echo "COMPAT_REASONS=$(IFS=,; printf '%s' "${reasons[*]}")"
echo "KERNEL_RELEASE=${kernel_release}"
echo "KERNEL_FULL=${kernel_full}"
echo "OS_RELEASE=${os_release}"
echo "IWLWIFI_PATH=${iwlwifi_path}"
echo "IWLWIFI_VERMAGIC=${iwlwifi_vermagic}"
echo "IWLWIFI_SRCVERSION=${iwlwifi_srcversion}"
echo "IWLWIFI_VERSION=${iwlwifi_version}"
echo "FEITCSI_PATH=${feitcsi_path}"
echo "WIPHY_COUNT=${wiphy_count}"
echo "OOM_KILL_COUNT=${oom_kill_count}"
echo "OOM_INVOKE_COUNT=${oom_invoke_count}"
echo "MONITOR_DUP_COUNT=${monitor_dup_count}"
echo "SYMBOL_MISMATCH_COUNT=${symbol_mismatch_count}"
echo "IFACE_SNAPSHOT_BEGIN"
echo "${iface_snapshot}"
echo "IFACE_SNAPSHOT_END"

echo "DMESG_FEITCSI_RECENT_BEGIN"
dmesg | grep -Ei 'feitcsi|Out of memory: Killed process .*feitcsi|oom-killer|phy0-monitor|Unknown symbol .*cfg80211' | tail -n 60 || true
echo "DMESG_FEITCSI_RECENT_END"
REMOTE
