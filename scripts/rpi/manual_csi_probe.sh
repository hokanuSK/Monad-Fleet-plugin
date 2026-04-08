#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

PI_HOST="${PI_HOST:-${1:-192.168.0.211}}"
PI_USER="${PI_USER:-admin}"
SSH_CONNECT_TIMEOUT_S="${SSH_CONNECT_TIMEOUT_S:-8}"
SSH_WAIT_TIMEOUT_S="${SSH_WAIT_TIMEOUT_S:-300}"
SSH_WAIT_INTERVAL_S="${SSH_WAIT_INTERVAL_S:-3}"

PROBE_SECONDS="${PROBE_SECONDS:-35}"
PROBE_FREQ_MHZ="${PROBE_FREQ_MHZ:-5240}"
PROBE_CHANNEL_WIDTH_MHZ="${PROBE_CHANNEL_WIDTH_MHZ:-40}"
PROBE_FORMAT="${PROBE_FORMAT:-VHT}"
PROBE_VERBOSE="${PROBE_VERBOSE:-false}"
PROBE_WIFI_IFACE="${PROBE_WIFI_IFACE:-wlan0}"
PROBE_WIFI_PROFILE="${PROBE_WIFI_PROFILE:-}"
PROBE_RECOVER_WIFI="${PROBE_RECOVER_WIFI:-true}"

REMOTE_BASE_DIR="${REMOTE_BASE_DIR:-/home/${PI_USER}/monad-fleet-agent/data/manual-csi-probe}"
RUN_ID="${RUN_ID:-probe-$(date -u +%Y%m%dT%H%M%SZ)}"
REMOTE_DIR="${REMOTE_BASE_DIR}/${RUN_ID}"

SSH_ARGS=(
  -o "ConnectTimeout=${SSH_CONNECT_TIMEOUT_S}"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
)

run_ssh() {
  ssh "${SSH_ARGS[@]}" "${PI_USER}@${PI_HOST}" "$@"
}

echo "Manual CSI probe"
echo "  PI=${PI_USER}@${PI_HOST}"
echo "  RUN_ID=${RUN_ID}"
echo "  REMOTE_DIR=${REMOTE_DIR}"
echo "  FEIT=/usr/local/bin/feitcsi freq=${PROBE_FREQ_MHZ} width=${PROBE_CHANNEL_WIDTH_MHZ} format=${PROBE_FORMAT} seconds=${PROBE_SECONDS} verbose=${PROBE_VERBOSE}"
echo "  RECOVERY iface=${PROBE_WIFI_IFACE} profile=${PROBE_WIFI_PROFILE:-<auto>} enabled=${PROBE_RECOVER_WIFI}"

run_ssh "echo SSH_OK >/dev/null"

run_ssh "bash -s -- '${REMOTE_DIR}' '${PROBE_SECONDS}' '${PROBE_FREQ_MHZ}' '${PROBE_CHANNEL_WIDTH_MHZ}' '${PROBE_FORMAT}' '${PROBE_VERBOSE}' '${PROBE_WIFI_IFACE}' '${PROBE_WIFI_PROFILE}' '${PROBE_RECOVER_WIFI}'" <<'REMOTE'
set -euo pipefail
dir="$1"
seconds="$2"
freq="$3"
width="$4"
fmt="$5"
verbose="$6"
wifi_iface="$7"
wifi_profile="$8"
recover_wifi="$9"

mkdir -p "$dir"

cat > "$dir/run_probe.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

dir="$1"
seconds="$2"
freq="$3"
width="$4"
fmt="$5"
verbose="$6"
wifi_iface="$7"
wifi_profile="$8"
recover_wifi="$9"

echo "START=$(date -u +%FT%TZ)" > "$dir/start.txt"
sudo -n pkill -f feitcsi || true
sudo -n rm -f "$dir/csi_probe.dat" "$dir/feitcsi.log" "$dir/rc.txt" "$dir/end.txt" "$dir/frames.txt" "$dir/size_bytes.txt" "$dir/recover.log"

set +e
verbose_flag=()
if [[ "$verbose" == "true" ]]; then
  verbose_flag+=("-v")
fi
sudo -n timeout -s INT -k 2s "${seconds}s" /usr/local/bin/feitcsi \
  --frequency "$freq" \
  --channel-width "$width" \
  --format "$fmt" \
  --output-file "$dir/csi_probe.dat" \
  "${verbose_flag[@]}" > "$dir/feitcsi.log" 2>&1
rc=$?
set -e

echo "$rc" > "$dir/rc.txt"

if [[ "$recover_wifi" == "true" ]]; then
  iw_bin="/usr/sbin/iw"
  if [[ ! -x "$iw_bin" ]]; then
    iw_bin="$(command -v iw || true)"
  fi
  {
    echo "RECOVER_START=$(date -u +%FT%TZ)"
    if [[ -n "$wifi_iface" ]]; then
      if ! ip link show "$wifi_iface" >/dev/null 2>&1; then
        phy=""
        if [[ -n "$iw_bin" ]]; then
          phy="$($iw_bin phy 2>/dev/null | awk '/^Wiphy /{print $2; exit}')"
        fi
        if [[ -n "$phy" ]]; then
          sudo -n "$iw_bin" phy "$phy" interface add "$wifi_iface" type managed || true
        fi
      fi
      sudo -n ip link set "$wifi_iface" up || true
      if command -v nmcli >/dev/null 2>&1; then
        profile="$wifi_profile"
        if [[ -z "$profile" ]]; then
          profile="$(nmcli -g GENERAL.CONNECTION device show "$wifi_iface" 2>/dev/null | head -n1 | tr -d '\r')"
        fi
        if [[ -z "$profile" || "$profile" == "--" || "$profile" == "(unknown)" || "$profile" == "(null)" ]]; then
          profile="$(nmcli -t -f NAME,TYPE connection show 2>/dev/null | awk -F: '$2 ~ /wifi|802-11-wireless/ {print $1; exit}')"
        fi
        if [[ -n "$profile" && "$profile" != "--" ]]; then
          sudo -n nmcli connection up "$profile" ifname "$wifi_iface" >/dev/null 2>&1 || true
        fi
      fi
      ip -brief addr show "$wifi_iface" 2>/dev/null || true
      if [[ -n "$iw_bin" ]]; then
        "$iw_bin" dev "$wifi_iface" link 2>/dev/null || true
      fi
    fi
    echo "RECOVER_END=$(date -u +%FT%TZ)"
  } > "$dir/recover.log" 2>&1
fi

echo "END=$(date -u +%FT%TZ)" > "$dir/end.txt"

python3 - "$dir/csi_probe.dat" "$dir/frames.txt" "$dir/size_bytes.txt" <<'PY'
from pathlib import Path
import sys

dat_path = Path(sys.argv[1])
frames_path = Path(sys.argv[2])
size_path = Path(sys.argv[3])

if not dat_path.exists():
    size_path.write_text("0\n", encoding="utf-8")
    frames_path.write_text("0\n", encoding="utf-8")
    raise SystemExit(0)

size = max(0, int(dat_path.stat().st_size))
size_path.write_text(f"{size}\n", encoding="utf-8")

header_len = 272
frames = 0
consumed = 0
if size >= header_len + 4:
    with dat_path.open("rb") as handle:
        while consumed + 4 <= size:
            raw_size = handle.read(4)
            if len(raw_size) < 4:
                break
            consumed += 4
            csi_size = int.from_bytes(raw_size, byteorder="little", signed=False)
            record_size = header_len + csi_size
            if csi_size <= 0 or record_size <= header_len:
                break
            remaining = size - consumed
            skip = record_size - 4
            if skip > remaining:
                break
            handle.seek(skip, 1)
            consumed += skip
            frames += 1

frames_path.write_text(f"{frames}\n", encoding="utf-8")
PY
SH

chmod +x "$dir/run_probe.sh"
nohup "$dir/run_probe.sh" "$dir" "$seconds" "$freq" "$width" "$fmt" "$verbose" "$wifi_iface" "$wifi_profile" "$recover_wifi" > "$dir/nohup.log" 2>&1 < /dev/null &
echo "$!" > "$dir/pid.txt"
echo "STARTED pid=$(cat "$dir/pid.txt")"
REMOTE

echo "Probe launched. Waiting for CSI window + SSH recovery..."
sleep "$((PROBE_SECONDS + 3))"

started_epoch="$(date +%s)"
while true; do
  if run_ssh "echo SSH_RECOVERED >/dev/null" >/dev/null 2>&1; then
    break
  fi
  now_epoch="$(date +%s)"
  elapsed="$((now_epoch - started_epoch))"
  if (( elapsed >= SSH_WAIT_TIMEOUT_S )); then
    echo "ERROR: SSH did not recover within ${SSH_WAIT_TIMEOUT_S}s after probe launch."
    exit 1
  fi
  sleep "${SSH_WAIT_INTERVAL_S}"
done

echo "Probe summary from ${REMOTE_DIR}:"
run_ssh "bash -lc '
set -euo pipefail
dir=\"${REMOTE_DIR}\"
echo FILES:
ls -l \"${REMOTE_DIR}\" || true
echo START:
cat \"${REMOTE_DIR}/start.txt\" 2>/dev/null || true
echo END:
cat \"${REMOTE_DIR}/end.txt\" 2>/dev/null || true
echo RC:
cat \"${REMOTE_DIR}/rc.txt\" 2>/dev/null || true
echo SIZE_BYTES:
cat \"${REMOTE_DIR}/size_bytes.txt\" 2>/dev/null || true
echo FRAMES:
cat \"${REMOTE_DIR}/frames.txt\" 2>/dev/null || true
echo RECOVER_LOG_TAIL:
tail -n 80 \"${REMOTE_DIR}/recover.log\" 2>/dev/null || true
echo FEITCSI_LOG_TAIL:
tail -n 60 \"${REMOTE_DIR}/feitcsi.log\" 2>/dev/null || true
'"

echo "Done."
