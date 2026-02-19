#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PI_HOST="${PI_HOST:-${1:-monad-rpi5.local}}"
PI_USER="${PI_USER:-admin}"
PI_DIR="${PI_DIR:-/home/${PI_USER}/monad-fleet-agent}"
DATA_ROOT="${DATA_ROOT:-${PI_DIR}/data}"
SMOKE_EXPERIMENT_ID="${SMOKE_EXPERIMENT_ID:-}"

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

REAL_DATA_ENFORCE="${REAL_DATA_ENFORCE:-true}"
REQUIRE_REAL_WIFI="${REQUIRE_REAL_WIFI:-}"
REQUIRE_REAL_BLE="${REQUIRE_REAL_BLE:-}"
REQUIRE_REAL_CSI="${REQUIRE_REAL_CSI:-}"
REQUIRE_CSI_FRAMES_MIN="${REQUIRE_CSI_FRAMES_MIN:-1}"

if [[ -z "${SMOKE_EXPERIMENT_ID}" ]]; then
  echo "SMOKE_EXPERIMENT_ID is required" >&2
  exit 1
fi

if [[ -z "${REQUIRE_REAL_WIFI}" ]]; then
  REQUIRE_REAL_WIFI="${REAL_DATA_ENFORCE}"
fi
if [[ -z "${REQUIRE_REAL_BLE}" ]]; then
  REQUIRE_REAL_BLE="${REAL_DATA_ENFORCE}"
fi
if [[ -z "${REQUIRE_REAL_CSI}" ]]; then
  REQUIRE_REAL_CSI="${REAL_DATA_ENFORCE}"
fi

echo "Inspecting latest Pi report for experiment elabftw:${SMOKE_EXPERIMENT_ID} on ${PI_USER}@${PI_HOST}"
run_ssh "${PI_USER}@${PI_HOST}" \
  "DATA_ROOT='${DATA_ROOT}' SMOKE_EXPERIMENT_ID='${SMOKE_EXPERIMENT_ID}' REAL_DATA_ENFORCE='${REAL_DATA_ENFORCE}' REQUIRE_REAL_WIFI='${REQUIRE_REAL_WIFI}' REQUIRE_REAL_BLE='${REQUIRE_REAL_BLE}' REQUIRE_REAL_CSI='${REQUIRE_REAL_CSI}' REQUIRE_CSI_FRAMES_MIN='${REQUIRE_CSI_FRAMES_MIN}' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail
python3 - <<'PY'
import json
import os
import sys
from pathlib import Path


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return default


def to_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        token = str(value).strip()
        if token == "":
            return default
        return int(float(token))
    except Exception:
        return default


def as_map(value):
    return value if isinstance(value, dict) else {}


def as_list(value):
    return value if isinstance(value, list) else []


experiment_id = f"elabftw:{(os.environ.get('SMOKE_EXPERIMENT_ID') or '').strip()}"
data_root = Path((os.environ.get("DATA_ROOT") or "./data").strip())

real_data_enforce = parse_bool(os.environ.get("REAL_DATA_ENFORCE"), True)
require_real_wifi = parse_bool(os.environ.get("REQUIRE_REAL_WIFI"), real_data_enforce)
require_real_ble = parse_bool(os.environ.get("REQUIRE_REAL_BLE"), real_data_enforce)
require_real_csi = parse_bool(os.environ.get("REQUIRE_REAL_CSI"), real_data_enforce)
require_csi_frames_min = max(0, to_int(os.environ.get("REQUIRE_CSI_FRAMES_MIN"), 1))

candidates = []
for bucket in ("sent", "pending"):
    base = data_root / bucket
    if not base.exists():
        continue
    for run_dir in base.iterdir():
        if not run_dir.is_dir():
            continue
        report_path = run_dir / "report.json"
        if not report_path.exists():
            continue
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(payload.get("experiment_id") or "").strip() != experiment_id:
            continue
        score = max(report_path.stat().st_mtime, run_dir.stat().st_mtime)
        candidates.append((score, bucket, run_dir, payload))

if not candidates:
    print(f"No report.json found for experiment_id={experiment_id} under {data_root}", file=sys.stderr)
    sys.exit(2)

candidates.sort(key=lambda row: row[0], reverse=True)
_, bucket, run_dir, report = candidates[0]
summary = as_map(report.get("summary_metrics"))
events = as_list(report.get("events"))
artifacts = as_list(report.get("artifacts"))
run_id = str(report.get("run_id") or "").strip()


def metric(key: str) -> str:
    raw = summary.get(key)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    for event in reversed(events):
        metrics = as_map(event.get("metrics"))
        if key in metrics and str(metrics.get(key)).strip() != "":
            return str(metrics.get(key)).strip()
    return ""


def has_command_event(prefix: str) -> bool:
    prefix = prefix.lower().strip()
    for event in events:
        command_id = str(event.get("command_id") or "").lower().strip()
        event_type = str(event.get("type") or "").upper().strip()
        if not command_id.startswith(prefix):
            continue
        if event_type in {"COMMAND_FINISHED", "ERROR", "ARTIFACT_UPLOADED"}:
            return True
    return False


artifact_names = [str(as_map(a).get("name") or "").lower() for a in artifacts]
artifact_uris = [str(as_map(a).get("uri") or "").lower() for a in artifacts]

has_wifi_artifact = any("wifi" in name for name in artifact_names) or any("wifi" in uri for uri in artifact_uris)
has_ble_artifact = any("ble" in name for name in artifact_names) or any("ble" in uri for uri in artifact_uris)
has_csi_artifact = any("csi" in name for name in artifact_names) or any("csi" in uri for uri in artifact_uris)
has_summary_artifact = any("run-summary" in name for name in artifact_names)

wifi_scan_ok = to_int(metric("wifi_scan_ok"), 0)
ble_scan_ok = to_int(metric("ble_scan_ok"), 0)
csi_collector_configured = to_int(metric("csi_collector_configured"), 0)
csi_capture_ok = to_int(metric("csi_capture_ok"), 0)
csi_capture_disabled = to_int(metric("csi_capture_disabled"), 0)
csi_output_files_count = to_int(metric("csi_output_files_count"), 0)
csi_frames_total = to_int(metric("csi_frames_total"), 0)
if csi_frames_total <= 0:
    csi_frames_total = to_int(metric("csi_frames_count"), 0)

result = {
    "experiment_id": experiment_id,
    "bucket": bucket,
    "run_dir": str(run_dir),
    "run_id": run_id,
    "status": str(report.get("status") or ""),
    "summary_metrics": {
        "wifi_scan_ok": wifi_scan_ok,
        "ble_scan_ok": ble_scan_ok,
        "csi_collector_configured": csi_collector_configured,
        "csi_capture_ok": csi_capture_ok,
        "csi_capture_disabled": csi_capture_disabled,
        "csi_output_files_count": csi_output_files_count,
        "csi_frames_total": csi_frames_total,
        "wifi_avg_rssi_dbm": metric("wifi_avg_rssi_dbm"),
        "wifi_link_quality": metric("wifi_link_quality"),
        "ble_avg_rssi_dbm": metric("ble_avg_rssi_dbm"),
    },
    "artifacts": {
        "total": len(artifacts),
        "has_wifi_artifact": has_wifi_artifact,
        "has_ble_artifact": has_ble_artifact,
        "has_csi_artifact": has_csi_artifact,
        "has_run_summary": has_summary_artifact,
    },
    "commands": {
        "has_wifi_command_event": has_command_event("wifi"),
        "has_ble_command_event": has_command_event("ble"),
        "has_csi_command_event": has_command_event("csi"),
    },
}

print(f"RUN_ID={run_id}")
print(json.dumps(result, indent=2, sort_keys=True))

failures = []
if require_real_wifi:
    if not result["commands"]["has_wifi_command_event"]:
        failures.append("wifi command event not found")
    if wifi_scan_ok < 1:
        failures.append("wifi_scan_ok != 1")
    if not has_wifi_artifact:
        failures.append("wifi artifact missing")

if require_real_ble:
    if not result["commands"]["has_ble_command_event"]:
        failures.append("ble command event not found")
    if ble_scan_ok < 1:
        failures.append("ble_scan_ok != 1")
    if not has_ble_artifact:
        failures.append("ble artifact missing")

if require_real_csi:
    if not result["commands"]["has_csi_command_event"]:
        failures.append("csi command event not found")
    if csi_capture_disabled > 0:
        failures.append("csi capture disabled by safety/config")
    if csi_collector_configured < 1:
        failures.append("csi collector not configured")
    if csi_capture_ok < 1:
        failures.append("csi capture failed")
    if csi_frames_total < require_csi_frames_min and csi_output_files_count < 1:
        failures.append(
            f"csi evidence insufficient (frames={csi_frames_total}, output_files={csi_output_files_count}, require_frames>={require_csi_frames_min})"
        )
    if not has_csi_artifact:
        failures.append("csi artifact missing")

if not has_summary_artifact:
    failures.append("run-summary artifact missing")

if failures:
    print("REAL_DATA_CHECK=FAIL", file=sys.stderr)
    for row in failures:
        print(f" - {row}", file=sys.stderr)
    sys.exit(3)

print("REAL_DATA_CHECK=PASS")
PY
REMOTE_SCRIPT
