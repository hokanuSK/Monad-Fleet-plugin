#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DEVICE_MODEL="${DEVICE_MODEL:-02:42:ac:14:00:04}"
DEVICE_PI="${DEVICE_PI:-2c:cf:67:80:f5:d9}"
MAX_SYNC_CYCLES="${MAX_SYNC_CYCLES:-1}"

RESET_STATE="${RESET_STATE:-true}"
RESET_METRICS="${RESET_METRICS:-true}"
RESET_GRAFANA="${RESET_GRAFANA:-false}"
CLEANUP="${CLEANUP:-false}"
DO_BUILD="${DO_BUILD:-false}"
SKIP_CORE_SERVICES_RESTART="${SKIP_CORE_SERVICES_RESTART:-false}"

RUN_PI="${RUN_PI:-false}"
RUN_PI_PASSIVE="${RUN_PI_PASSIVE:-false}"   # when true, do not SSH into Pi; rely on always-running Pi agent
PI_HOST="${PI_HOST:-monad-rpi5.local}"
PI_HOSTS_CSV="${PI_HOSTS_CSV:-}"
PI_RECOVERY_HOSTS_CSV="${PI_RECOVERY_HOSTS_CSV:-}"
AGENT_IDS_CSV="${AGENT_IDS_CSV:-}"
PI_HOSTS_FALLBACK="${PI_HOSTS_FALLBACK:-true}"  # when true, PI_HOSTS_CSV may be used as fallback candidates for one Pi
ALLOW_WIFI_DISRUPTIVE_CSI="${ALLOW_WIFI_DISRUPTIVE_CSI:-false}"

SMOKE_TITLE_PREFIX="${SMOKE_TITLE_PREFIX:-Fleet Smoke v3}"
SMOKE_TAGS_CSV="${SMOKE_TAGS_CSV:-fleet,smoke:v3,created-by:monad-fleet}"
SMOKE_PROFILE="${SMOKE_PROFILE:-full}"     # full|rssi|wireless_spec|rfpaper
RSSI_DURATION_S="${RSSI_DURATION_S:-30}"   # used when SMOKE_PROFILE=rssi
RSSI_INTERVAL_S="${RSSI_INTERVAL_S:-1}"    # default for non-Pi smoke
RSSI_INTERVAL_S_PI="${RSSI_INTERVAL_S_PI:-5}"  # default when RUN_PI=true
RSSI_ARTIFACT_STRIDE="${RSSI_ARTIFACT_STRIDE:-0}"  # 0=first+last sample artifacts only
FULL_DURATION_S="${FULL_DURATION_S:-0}"    # when >0 and profile=full, run WIFI_SCAN sampling for this duration
FULL_INTERVAL_S="${FULL_INTERVAL_S:-2}"    # sampling interval for full profile (non-Pi)
FULL_INTERVAL_S_PI="${FULL_INTERVAL_S_PI:-5}"  # sampling interval for full profile on Pi
FULL_ARTIFACT_STRIDE="${FULL_ARTIFACT_STRIDE:-0}"  # 0=first+last sample artifacts only
FULL_BLE_TIMEOUT_S="${FULL_BLE_TIMEOUT_S:-10}"
FULL_CSI_TIMEOUT_S="${FULL_CSI_TIMEOUT_S:-10}"
WIRELESS_SPEC_DURATION_S="${WIRELESS_SPEC_DURATION_S:-30}"
WIRELESS_SPEC_INTERVAL_S="${WIRELESS_SPEC_INTERVAL_S:-2}"
WIRELESS_SPEC_INTERVAL_S_PI="${WIRELESS_SPEC_INTERVAL_S_PI:-3}"
WIRELESS_SPEC_ARTIFACT_STRIDE="${WIRELESS_SPEC_ARTIFACT_STRIDE:-0}"
WIRELESS_SPEC_BLE_TIMEOUT_S="${WIRELESS_SPEC_BLE_TIMEOUT_S:-12}"
WIRELESS_SPEC_CSI_TIMEOUT_S="${WIRELESS_SPEC_CSI_TIMEOUT_S:-15}"
WIRELESS_SPEC_ENV_SNAPSHOT="${WIRELESS_SPEC_ENV_SNAPSHOT:-true}"
RFPAPER_BASELINE_S="${RFPAPER_BASELINE_S:-60}"
RFPAPER_SCRIPTED_S="${RFPAPER_SCRIPTED_S:-60}"
RFPAPER_FREEFORM_S="${RFPAPER_FREEFORM_S:-60}"
RFPAPER_INTERVAL_S="${RFPAPER_INTERVAL_S:-2}"
RFPAPER_INTERVAL_S_PI="${RFPAPER_INTERVAL_S_PI:-5}"
RFPAPER_ARTIFACT_STRIDE="${RFPAPER_ARTIFACT_STRIDE:-0}"
RFPAPER_BLE_TIMEOUT_S="${RFPAPER_BLE_TIMEOUT_S:-20}"
RFPAPER_CSI_TIMEOUT_S="${RFPAPER_CSI_TIMEOUT_S:-30}"
RFPAPER_ENV_SNAPSHOT="${RFPAPER_ENV_SNAPSHOT:-true}"
RFPAPER_ACTIVITY_LABEL="${RFPAPER_ACTIVITY_LABEL:-walking-scripted}"
RFPAPER_FREEFORM_LABEL="${RFPAPER_FREEFORM_LABEL:-free-form}"
RFPAPER_REQUIRE_ETHERNET_CONTROL="${RFPAPER_REQUIRE_ETHERNET_CONTROL:-false}"
RFPAPER_EXCITATION_RATE_HZ="${RFPAPER_EXCITATION_RATE_HZ:-100}"
RFPAPER_FIXED_FREQ_MHZ="${RFPAPER_FIXED_FREQ_MHZ:-5180}"
RFPAPER_FIXED_CHANNEL_WIDTH_MHZ="${RFPAPER_FIXED_CHANNEL_WIDTH_MHZ:-80}"
RFPAPER_FIXED_MCS="${RFPAPER_FIXED_MCS:-}"
RFPAPER_FIXED_TX_POWER_DBM="${RFPAPER_FIXED_TX_POWER_DBM:-}"
RFPAPER_CSI_PER_PHASE="${RFPAPER_CSI_PER_PHASE:-true}"
WIFI_SAMPLE_START_JITTER_S="${WIFI_SAMPLE_START_JITTER_S:-0}"
WIFI_SAMPLE_START_AT_EPOCH_S="${WIFI_SAMPLE_START_AT_EPOCH_S:-}"
WIFI_SAMPLE_MIN_INTERVAL_S="${WIFI_SAMPLE_MIN_INTERVAL_S:-1}"
WIFI_SAMPLE_MAX_DURATION_S="${WIFI_SAMPLE_MAX_DURATION_S:-7200}"
WIFI_SAMPLE_MAX_POINTS="${WIFI_SAMPLE_MAX_POINTS:-7200}"
REPORT_RPC_TIMEOUT_S="${REPORT_RPC_TIMEOUT_S:-120}"
CONTROL_RPC_TIMEOUT_S="${CONTROL_RPC_TIMEOUT_S:-30}"
AGENT_RUN_TIMEOUT_S="${AGENT_RUN_TIMEOUT_S:-900}"
AGENT_STATUS_POLL_S="${AGENT_STATUS_POLL_S:-5}"
SSH_WAIT_TIMEOUT_S="${SSH_WAIT_TIMEOUT_S:-180}"
SSH_WAIT_INTERVAL_S="${SSH_WAIT_INTERVAL_S:-5}"
SSH_DROP_FAIL_FAST_S="${SSH_DROP_FAIL_FAST_S:-0}"
RESTART_NOW_HINT_ON_SSH_DROP="${RESTART_NOW_HINT_ON_SSH_DROP:-true}"
MANAGE_SYSTEMD_AGENT="${MANAGE_SYSTEMD_AGENT:-true}"
SYSTEMD_AGENT_SERVICE="${SYSTEMD_AGENT_SERVICE:-monad-fleet-agent.service}"
ENABLE_ELAB_ARTIFACT_UPLOAD="${ENABLE_ELAB_ARTIFACT_UPLOAD:-true}"
ARTIFACT_UPLOAD_MAX_BYTES="${ARTIFACT_UPLOAD_MAX_BYTES:-20971520}"
ARTIFACT_UPLOAD_TARGET="${ARTIFACT_UPLOAD_TARGET:-elabftw}"
ARTIFACT_UPLOAD_DURING_MEASURE="${ARTIFACT_UPLOAD_DURING_MEASURE:-true}"
ARTIFACT_EVICT_AFTER_UPLOAD="${ARTIFACT_EVICT_AFTER_UPLOAD:-false}"
ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S="${ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S:-3}"
ARTIFACT_UPLOAD_BACKOFF_S="${ARTIFACT_UPLOAD_BACKOFF_S:-15}"
RUN_ARTIFACT_SOFT_LIMIT_BYTES="${RUN_ARTIFACT_SOFT_LIMIT_BYTES:-0}"
DATA_SINKS="${DATA_SINKS:-}"
METRICS_SINKS="${METRICS_SINKS:-}"
EVENT_SINKS="${EVENT_SINKS:-}"
ARTIFACT_SINKS="${ARTIFACT_SINKS:-}"
WEBHOOK_URL="${WEBHOOK_URL:-}"
WEBHOOK_TOKEN="${WEBHOOK_TOKEN:-}"
WEBHOOK_TIMEOUT_S="${WEBHOOK_TIMEOUT_S:-}"
WEBHOOK_ARTIFACT_INLINE_MAX_BYTES="${WEBHOOK_ARTIFACT_INLINE_MAX_BYTES:-}"
SQLITE_SINK_PATH="${SQLITE_SINK_PATH:-}"
SEPARATE_MEASURE_AND_REPORT="${SEPARATE_MEASURE_AND_REPORT:-false}"
RESTART_CONTROL_PLANE_BEFORE_REPORT="${RESTART_CONTROL_PLANE_BEFORE_REPORT:-false}"
CONTROL_PLANE_IFACE="${CONTROL_PLANE_IFACE:-wlan0}"
WIFI_SCAN_IFACE="${WIFI_SCAN_IFACE:-${CONTROL_PLANE_IFACE}}"
CONTROL_PLANE_BAND_EXPECT="${CONTROL_PLANE_BAND_EXPECT:-}"
CSI_MEASURE_IFACE="${CSI_MEASURE_IFACE:-}"
CSI_MEASURE_BAND_EXPECT="${CSI_MEASURE_BAND_EXPECT:-}"
CSI_REQUIRE_SEPARATE_IFACE="${CSI_REQUIRE_SEPARATE_IFACE:-false}"
CSI_IFACE_FLAG="${CSI_IFACE_FLAG:-auto}"
AUTO_SWITCH_CONTROL_PLANE_IFACE="${AUTO_SWITCH_CONTROL_PLANE_IFACE:-false}"
AUTO_SWITCH_CSI_MEASURE_IFACE="${AUTO_SWITCH_CSI_MEASURE_IFACE:-true}"
SINGLE_RADIO_MODE="${SINGLE_RADIO_MODE:-false}"
SINGLE_RADIO_WIFI_PROFILE="${SINGLE_RADIO_WIFI_PROFILE:-auto}"
SINGLE_RADIO_RECOVERY_RETRIES="${SINGLE_RADIO_RECOVERY_RETRIES:-3}"
SINGLE_RADIO_RECOVERY_ROUTE_WAIT_S="${SINGLE_RADIO_RECOVERY_ROUTE_WAIT_S:-45}"
SINGLE_RADIO_RECOVERY_CMD="${SINGLE_RADIO_RECOVERY_CMD:-}"
SINGLE_RADIO_ROUTE_GATE="${SINGLE_RADIO_ROUTE_GATE:-true}"
SINGLE_RADIO_ROUTE_HEALTH_TIMEOUT_S="${SINGLE_RADIO_ROUTE_HEALTH_TIMEOUT_S:-5}"
SINGLE_RADIO_REBOOT_ON_RECOVERY_FAIL="${SINGLE_RADIO_REBOOT_ON_RECOVERY_FAIL:-false}"
CONTROL_PLANE_RESTART_IFACE="${CONTROL_PLANE_RESTART_IFACE:-}"
CONTROL_PLANE_RESTART_DOWN_S="${CONTROL_PLANE_RESTART_DOWN_S:-2}"
CONTROL_PLANE_RESTART_WAIT_S="${CONTROL_PLANE_RESTART_WAIT_S:-45}"
CONTROL_PLANE_RESTART_CMD="${CONTROL_PLANE_RESTART_CMD:-}"
ALLOW_WIFI_CONTROL_PLANE_RESTART="${ALLOW_WIFI_CONTROL_PLANE_RESTART:-false}"
STRICT_CONTROL_PLANE_IFACE="${STRICT_CONTROL_PLANE_IFACE:-false}"
STRICT_WIFI_SCAN_IFACE="${STRICT_WIFI_SCAN_IFACE:-false}"
REQUIRE_WIFI_CONTROL="${REQUIRE_WIFI_CONTROL:-false}"
INJECT_HYPOTHETICAL_METRICS="${INJECT_HYPOTHETICAL_METRICS:-false}"
CSI_COLLECTOR_CMD="${CSI_COLLECTOR_CMD:-}"
CSI_OUTPUT_PATH="${CSI_OUTPUT_PATH:-}"
CSI_OUTPUT_GLOB="${CSI_OUTPUT_GLOB:-}"
CSI_FRAMES_REGEX="${CSI_FRAMES_REGEX:-}"
CSI_OUTPUT_MAX_FILES="${CSI_OUTPUT_MAX_FILES:-}"
CSI_PARSE_MAX_BYTES="${CSI_PARSE_MAX_BYTES:-}"
CSI_REQUIRE_EVIDENCE="${CSI_REQUIRE_EVIDENCE:-}"
CSI_MIN_FRAMES="${CSI_MIN_FRAMES:-}"
CSI_MIN_OUTPUT_FILES="${CSI_MIN_OUTPUT_FILES:-}"
CSI_SUDO_NONINTERACTIVE="${CSI_SUDO_NONINTERACTIVE:-true}"
CSI_TRAFFIC_ENABLE="${CSI_TRAFFIC_ENABLE:-}"
CSI_TRAFFIC_TARGET="${CSI_TRAFFIC_TARGET:-}"
CSI_TRAFFIC_IFACE="${CSI_TRAFFIC_IFACE:-}"
CSI_TRAFFIC_INTERVAL_MS="${CSI_TRAFFIC_INTERVAL_MS:-}"
CSI_ALLOW_THROTTLED="${CSI_ALLOW_THROTTLED:-false}"
CSI_HARD_TIMEOUT_S="${CSI_HARD_TIMEOUT_S:-0}"
CSI_CLEANUP_BEFORE_CAPTURE="${CSI_CLEANUP_BEFORE_CAPTURE:-true}"
CSI_CLEANUP_AFTER_CAPTURE="${CSI_CLEANUP_AFTER_CAPTURE:-true}"
CSI_KILL_PATTERNS="${CSI_KILL_PATTERNS:-feitcsi}"
CSI_DEFAULT_FEIT_FREQ_MHZ="${CSI_DEFAULT_FEIT_FREQ_MHZ:-5240}"
CSI_DEFAULT_FEIT_CHANNEL_WIDTH_MHZ="${CSI_DEFAULT_FEIT_CHANNEL_WIDTH_MHZ:-80}"
CSI_DEFAULT_FEIT_FORMAT="${CSI_DEFAULT_FEIT_FORMAT:-VHT}"
CSI_DEFAULT_OUTPUT_PATH="${CSI_DEFAULT_OUTPUT_PATH:-/tmp/csi.dat}"
CSI_COLLECTOR_VERBOSE="${CSI_COLLECTOR_VERBOSE:-false}"
REAL_DATA_ENFORCE="${REAL_DATA_ENFORCE:-}"
REQUIRE_REAL_WIFI="${REQUIRE_REAL_WIFI:-}"
REQUIRE_WIFI_CONNECTED="${REQUIRE_WIFI_CONNECTED:-false}"
REQUIRE_REAL_BLE="${REQUIRE_REAL_BLE:-}"
REQUIRE_REAL_CSI="${REQUIRE_REAL_CSI:-}"
REQUIRE_CSI_FRAMES_MIN="${REQUIRE_CSI_FRAMES_MIN:-1}"
RF_ROOM_ID="${RF_ROOM_ID:-}"
RF_SCENARIO_ID="${RF_SCENARIO_ID:-}"
RF_RUN_LABEL="${RF_RUN_LABEL:-}"
TARGET_DEVICE_IDS_CSV="${TARGET_DEVICE_IDS_CSV:-}"
PASSIVE_AGENT_WAIT_S="${PASSIVE_AGENT_WAIT_S:-120}"  # extra wait for passive Pi mode before verification
PASSIVE_VERIFY_MAX_WAIT_S="${PASSIVE_VERIFY_MAX_WAIT_S:-900}"  # additional async verify window for passive mode
PASSIVE_VERIFY_POLL_S="${PASSIVE_VERIFY_POLL_S:-15}"  # polling interval for passive async verification

NOTION_SYNC="${NOTION_SYNC:-false}"
NOTION_SYNC_STRICT="${NOTION_SYNC_STRICT:-false}"
NOTION_API_TOKEN="${NOTION_API_TOKEN:-}"
OPS_OUTPUT_DIR="${OPS_OUTPUT_DIR:-${REPO_ROOT}/output/ops}"
OPS_RUN_CONTEXT_PATH="${OPS_RUN_CONTEXT_PATH:-${OPS_OUTPUT_DIR}/latest_run_context.json}"
OPS_INCIDENT_CONTEXT_PATH="${OPS_INCIDENT_CONTEXT_PATH:-${OPS_OUTPUT_DIR}/latest_incident_context.json}"
OPS_TASK_CONTEXT_PATH="${OPS_TASK_CONTEXT_PATH:-${OPS_OUTPUT_DIR}/latest_task_context.json}"
NOTION_RUNS_DB_ID="${NOTION_RUNS_DB_ID:-8dc731920bf34966bb711472100c7057}"
NOTION_INCIDENTS_DB_ID="${NOTION_INCIDENTS_DB_ID:-da72692b8106430bb1e3892866d41d20}"
NOTION_TASKS_DB_ID="${NOTION_TASKS_DB_ID:-4a5936bb0baa4614bb61bc8daed18cee}"
OPS_RUN_ID="${OPS_RUN_ID:-}"

SMOKE_STARTED_AT_UTC="$(date -u +%FT%TZ)"
SMOKE_LAST_STEP="bootstrap"
SMOKE_LAST_COMMAND=""
SMOKE_EXPERIMENT_ID=""

ensure_ops_output_dir() {
  mkdir -p "${OPS_OUTPUT_DIR}"
}

write_run_context() {
  local result="$1"
  ensure_ops_output_dir
  OPS_RUN_CONTEXT_PATH="${OPS_RUN_CONTEXT_PATH}" \
  RESULT="${result}" \
  SMOKE_STARTED_AT_UTC="${SMOKE_STARTED_AT_UTC}" \
  SMOKE_LAST_STEP="${SMOKE_LAST_STEP}" \
  SMOKE_LAST_COMMAND="${SMOKE_LAST_COMMAND}" \
  SMOKE_EXPERIMENT_ID="${SMOKE_EXPERIMENT_ID}" \
  OPS_RUN_ID="${OPS_RUN_ID}" \
  RUN_PI="${RUN_PI}" \
  RUN_PI_PASSIVE="${RUN_PI_PASSIVE}" \
  SMOKE_PROFILE="${SMOKE_PROFILE}" \
  DEVICE_MODEL="${DEVICE_MODEL}" \
  DEVICE_PI="${DEVICE_PI}" \
  TARGET_DEVICE_IDS_CSV="${TARGET_DEVICE_IDS_CSV}" \
  REAL_DATA_ENFORCE="${REAL_DATA_ENFORCE}" \
  CLEANUP="${CLEANUP}" \
  python3 - <<'PY'
import json
import os
from pathlib import Path
from datetime import datetime, timezone

path = Path(os.environ["OPS_RUN_CONTEXT_PATH"])
payload = {
    "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "started_at_utc": os.environ.get("SMOKE_STARTED_AT_UTC", ""),
    "result": os.environ.get("RESULT", "unknown"),
    "run_id": os.environ.get("OPS_RUN_ID", ""),
    "smoke_experiment_id": os.environ.get("SMOKE_EXPERIMENT_ID", ""),
    "smoke_profile": os.environ.get("SMOKE_PROFILE", ""),
    "run_pi": os.environ.get("RUN_PI", ""),
    "run_pi_passive": os.environ.get("RUN_PI_PASSIVE", ""),
    "device_model": os.environ.get("DEVICE_MODEL", ""),
    "device_pi": os.environ.get("DEVICE_PI", ""),
    "target_device_ids_csv": os.environ.get("TARGET_DEVICE_IDS_CSV", ""),
    "real_data_enforce": os.environ.get("REAL_DATA_ENFORCE", ""),
    "cleanup": os.environ.get("CLEANUP", ""),
    "last_step": os.environ.get("SMOKE_LAST_STEP", ""),
    "last_command": os.environ.get("SMOKE_LAST_COMMAND", ""),
}
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"[ops] wrote {path}")
PY
}

write_incident_context() {
  ensure_ops_output_dir
  OPS_INCIDENT_CONTEXT_PATH="${OPS_INCIDENT_CONTEXT_PATH}" \
  OPS_RUN_ID="${OPS_RUN_ID}" \
  SMOKE_EXPERIMENT_ID="${SMOKE_EXPERIMENT_ID}" \
  SMOKE_PROFILE="${SMOKE_PROFILE}" \
  SMOKE_LAST_STEP="${SMOKE_LAST_STEP}" \
  SMOKE_LAST_COMMAND="${SMOKE_LAST_COMMAND}" \
  python3 - <<'PY'
import json
import os
from pathlib import Path
from datetime import datetime, timezone

path = Path(os.environ["OPS_INCIDENT_CONTEXT_PATH"])
experiment_id = os.environ.get("SMOKE_EXPERIMENT_ID", "").strip()
payload = {
    "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "severity": "S3",
    "status": "open",
    "run_id": os.environ.get("OPS_RUN_ID", ""),
    "experiment_id": experiment_id,
    "symptom": f"Smoke failed at step={os.environ.get('SMOKE_LAST_STEP', '')}",
    "root_cause": "",
    "fix": "",
    "prevention": "",
    "links": "",
    "context": {
        "smoke_profile": os.environ.get("SMOKE_PROFILE", ""),
        "last_command": os.environ.get("SMOKE_LAST_COMMAND", ""),
    },
}
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"[ops] wrote {path}")
PY
}

write_task_context() {
  ensure_ops_output_dir
  OPS_TASK_CONTEXT_PATH="${OPS_TASK_CONTEXT_PATH}" \
  OPS_RUN_ID="${OPS_RUN_ID}" \
  SMOKE_EXPERIMENT_ID="${SMOKE_EXPERIMENT_ID}" \
  SMOKE_LAST_STEP="${SMOKE_LAST_STEP}" \
  python3 - <<'PY'
import json
import os
from pathlib import Path
from datetime import datetime, timezone

path = Path(os.environ["OPS_TASK_CONTEXT_PATH"])
experiment_id = os.environ.get("SMOKE_EXPERIMENT_ID", "").strip()
run_id = os.environ.get("OPS_RUN_ID", "").strip()
reference_parts = []
if run_id:
    reference_parts.append(f"run_id={run_id}")
if experiment_id:
    reference_parts.append(f"experiment_id={experiment_id}")
payload = {
    "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "title": "Follow-up: investigate smoke failure",
    "priority": "P1",
    "status": "todo",
    "owner": "",
    "due_date": "",
    "source": "Incident",
    "reference": ";".join(reference_parts),
    "notes": f"Failed at step={os.environ.get('SMOKE_LAST_STEP', '')}",
}
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"[ops] wrote {path}")
PY
}

run_notion_hook() {
  local hook_path="$1"
  shift
  if [[ "${NOTION_SYNC}" != "true" ]]; then
    return 0
  fi
  if [[ ! -x "${hook_path}" ]]; then
    echo "WARN: Notion sync enabled but hook missing/non-executable: ${hook_path}"
    return 1
  fi
  "${hook_path}" "$@"
}

sync_notion_run() {
  NOTION_API_TOKEN="${NOTION_API_TOKEN}" \
  NOTION_RUNS_DB_ID="${NOTION_RUNS_DB_ID}" \
  run_notion_hook "${REPO_ROOT}/scripts/ops/notion_log_run.sh" "${OPS_RUN_CONTEXT_PATH}"
}

sync_notion_incident_and_task() {
  local rc=0
  NOTION_API_TOKEN="${NOTION_API_TOKEN}" \
  NOTION_INCIDENTS_DB_ID="${NOTION_INCIDENTS_DB_ID}" \
  run_notion_hook "${REPO_ROOT}/scripts/ops/notion_log_incident.sh" "${OPS_INCIDENT_CONTEXT_PATH}" || rc=1
  NOTION_API_TOKEN="${NOTION_API_TOKEN}" \
  NOTION_TASKS_DB_ID="${NOTION_TASKS_DB_ID}" \
  run_notion_hook "${REPO_ROOT}/scripts/ops/notion_log_task.sh" "${OPS_TASK_CONTEXT_PATH}" || rc=1
  return "${rc}"
}

on_smoke_exit() {
  local rc="$1"
  set +e
  local result="pass"
  local hook_failed=0
  if [[ "${rc}" -ne 0 || "${SMOKE_LAST_STEP}" != "8_done" ]]; then
    result="fail"
    if [[ "${rc}" -eq 0 ]]; then
      rc=1
    fi
  fi

  write_run_context "${result}" || hook_failed=1
  sync_notion_run || hook_failed=1

  if [[ "${result}" == "fail" ]]; then
    write_incident_context || hook_failed=1
    write_task_context || hook_failed=1
    sync_notion_incident_and_task || hook_failed=1
  fi

  if [[ "${hook_failed}" -ne 0 && "${NOTION_SYNC_STRICT}" == "true" && "${rc}" -eq 0 ]]; then
    return 90
  fi
  return "${rc}"
}

on_smoke_exit_trap() {
  local rc="$1"
  trap - EXIT
  on_smoke_exit "${rc}"
  exit $?
}

trap 'SMOKE_LAST_COMMAND="${BASH_COMMAND}"' ERR
trap 'on_smoke_exit_trap $?' EXIT

csv_to_lines() {
  local raw="$1"
  local part=""
  local old_ifs="$IFS"
  IFS=','
  for part in $raw; do
    # Trim leading/trailing whitespace without invoking external tools.
    part="${part#"${part%%[![:space:]]*}"}"
    part="${part%"${part##*[![:space:]]}"}"
    if [[ -n "${part}" ]]; then
      printf '%s\n' "${part}"
    fi
  done
  IFS="$old_ifs"
}

PI_HOSTS_LIST=()
AGENT_IDS_LIST=()
PI_HOSTS_FALLBACK_ACTIVE="false"
PI_HOSTS_FALLBACK_CSV=""
if [[ "${RUN_PI}" == "true" ]]; then
  while IFS= read -r row; do
    PI_HOSTS_LIST+=("${row}")
  done < <(csv_to_lines "${PI_HOSTS_CSV}")
  if [[ ${#PI_HOSTS_LIST[@]} -eq 0 ]]; then
    PI_HOSTS_LIST=("${PI_HOST}")
  fi

  while IFS= read -r row; do
    AGENT_IDS_LIST+=("${row}")
  done < <(csv_to_lines "${AGENT_IDS_CSV}")
  if [[ ${#PI_HOSTS_LIST[@]} -gt 1 && ${#AGENT_IDS_LIST[@]} -eq 0 && -z "${TARGET_DEVICE_IDS_CSV}" ]]; then
    if [[ "${PI_HOSTS_FALLBACK}" == "true" ]]; then
      PI_HOSTS_FALLBACK_ACTIVE="true"
      PI_HOSTS_FALLBACK_CSV="$(IFS=,; printf "%s" "${PI_HOSTS_LIST[*]}")"
      PI_HOSTS_LIST=("${PI_HOSTS_LIST[0]}")
      echo "RUN_PI fallback mode: using PI_HOSTS_CSV as candidate list for one device."
      echo "Candidates: ${PI_HOSTS_FALLBACK_CSV}"
    else
      echo "ERROR: RUN_PI=true with multiple PI hosts requires AGENT_IDS_CSV (or TARGET_DEVICE_IDS_CSV)."
      echo "Example: PI_HOSTS_CSV='pi1.local,pi2.local' AGENT_IDS_CSV='aa:bb:...,cc:dd:...'"
      echo "Or set PI_HOSTS_FALLBACK=true to treat PI_HOSTS_CSV as fallback candidates for one Pi."
      exit 1
    fi
  fi
  if [[ ${#AGENT_IDS_LIST[@]} -gt 0 && ${#AGENT_IDS_LIST[@]} -ne ${#PI_HOSTS_LIST[@]} ]]; then
    echo "ERROR: AGENT_IDS_CSV count must match PI_HOSTS_CSV count."
    echo "hosts=${#PI_HOSTS_LIST[@]} agent_ids=${#AGENT_IDS_LIST[@]}"
    exit 1
  fi

  if [[ -z "${TARGET_DEVICE_IDS_CSV}" ]]; then
    if [[ ${#AGENT_IDS_LIST[@]} -gt 0 ]]; then
      TARGET_DEVICE_IDS_CSV="$(IFS=,; printf "%s" "${AGENT_IDS_LIST[*]}")"
    else
      TARGET_DEVICE_IDS_CSV="${DEVICE_PI}"
    fi
  fi
  if [[ -z "${PI_RECOVERY_HOSTS_CSV}" ]]; then
    PI_RECOVERY_HOSTS_CSV="${PI_HOST}"
  fi
fi

if [[ -z "${REAL_DATA_ENFORCE}" ]]; then
  if [[ "${RUN_PI}" == "true" ]]; then
    REAL_DATA_ENFORCE="true"
  else
    REAL_DATA_ENFORCE="false"
  fi
fi

if [[ "${REAL_DATA_ENFORCE}" == "true" ]]; then
  INJECT_HYPOTHETICAL_METRICS="false"
  REQUIRE_REAL_WIFI="${REQUIRE_REAL_WIFI:-true}"
  REQUIRE_REAL_BLE="${REQUIRE_REAL_BLE:-true}"
  REQUIRE_REAL_CSI="${REQUIRE_REAL_CSI:-true}"
fi

if [[ "${RUN_PI}" == "true" && -z "${CSI_COLLECTOR_CMD}" ]]; then
  CSI_VERBOSE_FLAG=""
  if [[ "${CSI_COLLECTOR_VERBOSE}" == "true" ]]; then
    CSI_VERBOSE_FLAG=" -v"
  fi
  CSI_COLLECTOR_CMD="sudo feitcsi --frequency ${CSI_DEFAULT_FEIT_FREQ_MHZ} --channel-width ${CSI_DEFAULT_FEIT_CHANNEL_WIDTH_MHZ} --format ${CSI_DEFAULT_FEIT_FORMAT} --output-file ${CSI_DEFAULT_OUTPUT_PATH}${CSI_VERBOSE_FLAG}"
  echo "CSI default: using collector command '${CSI_COLLECTOR_CMD}'."
fi
if [[ -z "${CSI_OUTPUT_PATH}" && -z "${CSI_OUTPUT_GLOB}" ]]; then
  CSI_OUTPUT_PATH="${CSI_DEFAULT_OUTPUT_PATH}"
fi

if [[ "${REAL_DATA_ENFORCE}" == "true" && "${REQUIRE_REAL_CSI}" == "true" && -z "${CSI_COLLECTOR_CMD}" ]]; then
  echo "ERROR: REAL_DATA_ENFORCE=true and REQUIRE_REAL_CSI=true but CSI_COLLECTOR_CMD is empty."
  echo "Set CSI_COLLECTOR_CMD (and optional CSI_OUTPUT_PATH/CSI_OUTPUT_GLOB/CSI_FRAMES_REGEX) for real CSI runs."
  exit 1
fi
if [[ -z "${CSI_REQUIRE_EVIDENCE}" && "${REAL_DATA_ENFORCE}" == "true" && "${REQUIRE_REAL_CSI}" == "true" ]]; then
  CSI_REQUIRE_EVIDENCE="true"
fi
if [[ -z "${CSI_MIN_FRAMES}" && "${REAL_DATA_ENFORCE}" == "true" && "${REQUIRE_REAL_CSI}" == "true" ]]; then
  CSI_MIN_FRAMES="${REQUIRE_CSI_FRAMES_MIN}"
fi
if [[ -z "${CSI_MIN_OUTPUT_FILES}" && "${REAL_DATA_ENFORCE}" == "true" && "${REQUIRE_REAL_CSI}" == "true" ]]; then
  CSI_MIN_OUTPUT_FILES="1"
fi
if [[ -z "${CSI_MIN_OUTPUT_FILES}" && "${SMOKE_PROFILE}" == "rfpaper" ]]; then
  CSI_MIN_OUTPUT_FILES="1"
fi

expected_wifi_duration_s=0
expected_wifi_interval_s=1
expected_run_min_timeout_s=900
if [[ "${SMOKE_PROFILE}" == "rssi" ]]; then
  expected_wifi_duration_s="${RSSI_DURATION_S}"
  expected_wifi_interval_s="${RSSI_INTERVAL_S}"
  expected_run_min_timeout_s="$((RSSI_DURATION_S + 300))"
elif [[ "${SMOKE_PROFILE}" == "full" && "${FULL_DURATION_S}" -gt 0 ]]; then
  expected_wifi_duration_s="${FULL_DURATION_S}"
  expected_wifi_interval_s="${FULL_INTERVAL_S}"
  expected_run_min_timeout_s="$((FULL_DURATION_S + FULL_BLE_TIMEOUT_S + FULL_CSI_TIMEOUT_S + 300))"
elif [[ "${SMOKE_PROFILE}" == "wireless_spec" ]]; then
  expected_wifi_duration_s="${WIRELESS_SPEC_DURATION_S}"
  expected_wifi_interval_s="${WIRELESS_SPEC_INTERVAL_S}"
  expected_run_min_timeout_s="$((WIRELESS_SPEC_DURATION_S + WIRELESS_SPEC_BLE_TIMEOUT_S + WIRELESS_SPEC_CSI_TIMEOUT_S + 300))"
elif [[ "${SMOKE_PROFILE}" == "rfpaper" ]]; then
  expected_wifi_duration_s="${RFPAPER_BASELINE_S}"
  if (( RFPAPER_SCRIPTED_S > expected_wifi_duration_s )); then
    expected_wifi_duration_s="${RFPAPER_SCRIPTED_S}"
  fi
  if (( RFPAPER_FREEFORM_S > expected_wifi_duration_s )); then
    expected_wifi_duration_s="${RFPAPER_FREEFORM_S}"
  fi
  expected_wifi_interval_s="${RFPAPER_INTERVAL_S}"
  csi_window_s="${RFPAPER_CSI_TIMEOUT_S}"
  if [[ "${RFPAPER_CSI_PER_PHASE}" == "true" ]]; then
    csi_window_s="$((RFPAPER_CSI_TIMEOUT_S * 3))"
  fi
  expected_run_min_timeout_s="$((RFPAPER_BASELINE_S + RFPAPER_SCRIPTED_S + RFPAPER_FREEFORM_S + (RFPAPER_BLE_TIMEOUT_S * 3) + csi_window_s + 600))"
fi

if [[ "${RUN_PI}" == "true" ]]; then
  if [[ "${SMOKE_PROFILE}" == "rssi" ]]; then
    expected_wifi_interval_s="${RSSI_INTERVAL_S_PI}"
  elif [[ "${SMOKE_PROFILE}" == "full" && "${FULL_DURATION_S}" -gt 0 ]]; then
    expected_wifi_interval_s="${FULL_INTERVAL_S_PI}"
  elif [[ "${SMOKE_PROFILE}" == "wireless_spec" ]]; then
    expected_wifi_interval_s="${WIRELESS_SPEC_INTERVAL_S_PI}"
  elif [[ "${SMOKE_PROFILE}" == "rfpaper" ]]; then
    expected_wifi_interval_s="${RFPAPER_INTERVAL_S_PI}"
  fi
fi

if (( expected_wifi_duration_s > 0 )); then
  if (( expected_wifi_duration_s > WIFI_SAMPLE_MAX_DURATION_S )); then
    WIFI_SAMPLE_MAX_DURATION_S="${expected_wifi_duration_s}"
    echo "Adjusted WIFI_SAMPLE_MAX_DURATION_S to ${WIFI_SAMPLE_MAX_DURATION_S}s for profile=${SMOKE_PROFILE}."
  fi
  if (( expected_wifi_interval_s < 1 )); then
    expected_wifi_interval_s=1
  fi
  expected_wifi_points="$(((expected_wifi_duration_s + expected_wifi_interval_s - 1) / expected_wifi_interval_s))"
  if (( expected_wifi_points > WIFI_SAMPLE_MAX_POINTS )); then
    WIFI_SAMPLE_MAX_POINTS="$((expected_wifi_points + 10))"
    echo "Adjusted WIFI_SAMPLE_MAX_POINTS to ${WIFI_SAMPLE_MAX_POINTS} for profile=${SMOKE_PROFILE}."
  fi
fi

if (( AGENT_RUN_TIMEOUT_S < expected_run_min_timeout_s )); then
  AGENT_RUN_TIMEOUT_S="${expected_run_min_timeout_s}"
  echo "Adjusted AGENT_RUN_TIMEOUT_S to ${AGENT_RUN_TIMEOUT_S}s for profile=${SMOKE_PROFILE}."
fi

SMOKE_LAST_STEP="1_core_services_restart"
if [[ "${SKIP_CORE_SERVICES_RESTART}" == "true" ]]; then
  echo "[1/8] Skip core service restart (SKIP_CORE_SERVICES_RESTART=true)"
else
  echo "[1/8] Restart core services"
  if [[ "${DO_BUILD}" == "true" ]]; then
    echo "Building monad-fleet-service + model-device (DO_BUILD=true)"
    docker compose build monad-fleet-service model-device
  else
    echo "Skipping build (DO_BUILD=false)"
  fi
  docker compose up -d --force-recreate --no-build monad-fleet-service model-device prometheus mimir grafana
fi

SMOKE_LAST_STEP="2_reset_fleet_state"
if [[ "${RESET_STATE}" == "true" ]]; then
  echo "[2/8] Reset Fleet service state (dedupe + ingest journal) only"
  docker compose exec -T monad-fleet-service sh -lc "rm -f /data/state.json /data/ingest-metrics.ndjson || true"
  docker compose restart monad-fleet-service >/dev/null
else
  echo "[2/8] Skip Fleet state reset (RESET_STATE=${RESET_STATE})"
fi

SMOKE_LAST_STEP="3_reset_metrics"
if [[ "${RESET_METRICS}" == "true" ]]; then
  echo "[3/8] Reset Prometheus+Mimir data only (does not touch eLabFTW/MySQL)"
  docker compose exec -T prometheus sh -lc "rm -rf /prometheus/* || true"
  docker compose exec -T mimir sh -lc "rm -rf /data/* || true"
  docker compose restart prometheus mimir >/dev/null
else
  echo "[3/8] Skip metrics reset (RESET_METRICS=${RESET_METRICS})"
fi

SMOKE_LAST_STEP="3b_reset_grafana"
if [[ "${RESET_GRAFANA}" == "true" ]]; then
  echo "[3b/8] Reset Grafana local data (dashboards/users are wiped; metrics live in Mimir)"
  docker compose exec -T grafana sh -lc "rm -rf /var/lib/grafana/* || true"
  docker compose restart grafana >/dev/null
else
  echo "[3b/8] Skip Grafana reset (RESET_GRAFANA=${RESET_GRAFANA})"
fi

SMOKE_LAST_STEP="3c_pi_preflight"
if [[ "${RUN_PI}" == "true" && "${RUN_PI_PASSIVE}" != "true" ]]; then
  echo "[3c/8] Preflight Pi SSH reachability"
  ENFORCE_CONTROL_PLANE_ROUTE="false"
  PI_HOSTS_CSV_FOR_AGENT=""
  if [[ "${PI_HOSTS_FALLBACK_ACTIVE}" == "true" ]]; then
    PI_HOSTS_CSV_FOR_AGENT="${PI_HOSTS_FALLBACK_CSV}"
  fi
  if [[ "${SMOKE_PROFILE}" == "rfpaper" && "${RFPAPER_REQUIRE_ETHERNET_CONTROL}" == "true" ]]; then
    ENFORCE_CONTROL_PLANE_ROUTE="true"
  fi
  for host in "${PI_HOSTS_LIST[@]}"; do
    echo "  -> precheck host=${host}"
    PI_HOSTS_CSV="${PI_HOSTS_CSV_FOR_AGENT}" PI_RECOVERY_HOSTS_CSV="${PI_RECOVERY_HOSTS_CSV}" PRECHECK_ONLY="true" SSH_WAIT_TIMEOUT_S="${SSH_WAIT_TIMEOUT_S}" SSH_WAIT_INTERVAL_S="${SSH_WAIT_INTERVAL_S}" SSH_DROP_FAIL_FAST_S="${SSH_DROP_FAIL_FAST_S}" RESTART_NOW_HINT_ON_SSH_DROP="${RESTART_NOW_HINT_ON_SSH_DROP}" MANAGE_SYSTEMD_AGENT="${MANAGE_SYSTEMD_AGENT}" SYSTEMD_AGENT_SERVICE="${SYSTEMD_AGENT_SERVICE}" ENFORCE_CONTROL_PLANE_ROUTE="${ENFORCE_CONTROL_PLANE_ROUTE}" CONTROL_PLANE_IFACE="${CONTROL_PLANE_IFACE}" WIFI_SCAN_IFACE="${WIFI_SCAN_IFACE}" CONTROL_PLANE_BAND_EXPECT="${CONTROL_PLANE_BAND_EXPECT}" CSI_MEASURE_IFACE="${CSI_MEASURE_IFACE}" CSI_MEASURE_BAND_EXPECT="${CSI_MEASURE_BAND_EXPECT}" CSI_REQUIRE_SEPARATE_IFACE="${CSI_REQUIRE_SEPARATE_IFACE}" CSI_IFACE_FLAG="${CSI_IFACE_FLAG}" AUTO_SWITCH_CONTROL_PLANE_IFACE="${AUTO_SWITCH_CONTROL_PLANE_IFACE}" AUTO_SWITCH_CSI_MEASURE_IFACE="${AUTO_SWITCH_CSI_MEASURE_IFACE}" SINGLE_RADIO_MODE="${SINGLE_RADIO_MODE}" SINGLE_RADIO_WIFI_PROFILE="${SINGLE_RADIO_WIFI_PROFILE}" SINGLE_RADIO_RECOVERY_RETRIES="${SINGLE_RADIO_RECOVERY_RETRIES}" SINGLE_RADIO_RECOVERY_ROUTE_WAIT_S="${SINGLE_RADIO_RECOVERY_ROUTE_WAIT_S}" SINGLE_RADIO_RECOVERY_CMD="${SINGLE_RADIO_RECOVERY_CMD}" SINGLE_RADIO_ROUTE_GATE="${SINGLE_RADIO_ROUTE_GATE}" SINGLE_RADIO_ROUTE_HEALTH_TIMEOUT_S="${SINGLE_RADIO_ROUTE_HEALTH_TIMEOUT_S}" SINGLE_RADIO_REBOOT_ON_RECOVERY_FAIL="${SINGLE_RADIO_REBOOT_ON_RECOVERY_FAIL}" STRICT_CONTROL_PLANE_IFACE="${STRICT_CONTROL_PLANE_IFACE}" STRICT_WIFI_SCAN_IFACE="${STRICT_WIFI_SCAN_IFACE}" REQUIRE_WIFI_CONTROL="${REQUIRE_WIFI_CONTROL}" scripts/rpi/smoke_test_agent.sh "${host}"
  done
elif [[ "${RUN_PI}" == "true" && "${RUN_PI_PASSIVE}" == "true" ]]; then
  echo "[3c/8] Skip Pi SSH preflight (RUN_PI_PASSIVE=true; expecting always-running Pi agent)"
fi

SMOKE_LAST_STEP="4_create_experiment"
echo "[4/8] Create a new eLabFTW smoke experiment policy (profile=${SMOKE_PROFILE}; no existing experiments modified)"
SMOKE_EXPERIMENT_ID="$(docker compose exec -T monad-fleet-service sh -lc "DEVICE_MODEL='${DEVICE_MODEL}' DEVICE_PI='${DEVICE_PI}' RUN_PI='${RUN_PI}' TARGET_DEVICE_IDS_CSV='${TARGET_DEVICE_IDS_CSV}' SMOKE_TITLE_PREFIX='${SMOKE_TITLE_PREFIX}' SMOKE_TAGS_CSV='${SMOKE_TAGS_CSV}' SMOKE_PROFILE='${SMOKE_PROFILE}' RSSI_DURATION_S='${RSSI_DURATION_S}' RSSI_INTERVAL_S='${RSSI_INTERVAL_S}' RSSI_INTERVAL_S_PI='${RSSI_INTERVAL_S_PI}' RSSI_ARTIFACT_STRIDE='${RSSI_ARTIFACT_STRIDE}' FULL_DURATION_S='${FULL_DURATION_S}' FULL_INTERVAL_S='${FULL_INTERVAL_S}' FULL_INTERVAL_S_PI='${FULL_INTERVAL_S_PI}' FULL_ARTIFACT_STRIDE='${FULL_ARTIFACT_STRIDE}' FULL_BLE_TIMEOUT_S='${FULL_BLE_TIMEOUT_S}' FULL_CSI_TIMEOUT_S='${FULL_CSI_TIMEOUT_S}' WIRELESS_SPEC_DURATION_S='${WIRELESS_SPEC_DURATION_S}' WIRELESS_SPEC_INTERVAL_S='${WIRELESS_SPEC_INTERVAL_S}' WIRELESS_SPEC_INTERVAL_S_PI='${WIRELESS_SPEC_INTERVAL_S_PI}' WIRELESS_SPEC_ARTIFACT_STRIDE='${WIRELESS_SPEC_ARTIFACT_STRIDE}' WIRELESS_SPEC_BLE_TIMEOUT_S='${WIRELESS_SPEC_BLE_TIMEOUT_S}' WIRELESS_SPEC_CSI_TIMEOUT_S='${WIRELESS_SPEC_CSI_TIMEOUT_S}' WIRELESS_SPEC_ENV_SNAPSHOT='${WIRELESS_SPEC_ENV_SNAPSHOT}' RFPAPER_BASELINE_S='${RFPAPER_BASELINE_S}' RFPAPER_SCRIPTED_S='${RFPAPER_SCRIPTED_S}' RFPAPER_FREEFORM_S='${RFPAPER_FREEFORM_S}' RFPAPER_INTERVAL_S='${RFPAPER_INTERVAL_S}' RFPAPER_INTERVAL_S_PI='${RFPAPER_INTERVAL_S_PI}' RFPAPER_ARTIFACT_STRIDE='${RFPAPER_ARTIFACT_STRIDE}' RFPAPER_BLE_TIMEOUT_S='${RFPAPER_BLE_TIMEOUT_S}' RFPAPER_CSI_TIMEOUT_S='${RFPAPER_CSI_TIMEOUT_S}' RFPAPER_ENV_SNAPSHOT='${RFPAPER_ENV_SNAPSHOT}' RFPAPER_ACTIVITY_LABEL='${RFPAPER_ACTIVITY_LABEL}' RFPAPER_FREEFORM_LABEL='${RFPAPER_FREEFORM_LABEL}' RFPAPER_REQUIRE_ETHERNET_CONTROL='${RFPAPER_REQUIRE_ETHERNET_CONTROL}' RFPAPER_EXCITATION_RATE_HZ='${RFPAPER_EXCITATION_RATE_HZ}' RFPAPER_FIXED_FREQ_MHZ='${RFPAPER_FIXED_FREQ_MHZ}' RFPAPER_FIXED_CHANNEL_WIDTH_MHZ='${RFPAPER_FIXED_CHANNEL_WIDTH_MHZ}' RFPAPER_FIXED_MCS='${RFPAPER_FIXED_MCS}' RFPAPER_FIXED_TX_POWER_DBM='${RFPAPER_FIXED_TX_POWER_DBM}' RFPAPER_CSI_PER_PHASE='${RFPAPER_CSI_PER_PHASE}' WIFI_SAMPLE_START_JITTER_S='${WIFI_SAMPLE_START_JITTER_S}' WIFI_SAMPLE_START_AT_EPOCH_S='${WIFI_SAMPLE_START_AT_EPOCH_S}' RF_ROOM_ID='${RF_ROOM_ID}' RF_SCENARIO_ID='${RF_SCENARIO_ID}' RF_RUN_LABEL='${RF_RUN_LABEL}' ARTIFACT_UPLOAD_TARGET='${ARTIFACT_UPLOAD_TARGET}' ARTIFACT_UPLOAD_DURING_MEASURE='${ARTIFACT_UPLOAD_DURING_MEASURE}' ARTIFACT_EVICT_AFTER_UPLOAD='${ARTIFACT_EVICT_AFTER_UPLOAD}' RUN_ARTIFACT_SOFT_LIMIT_BYTES='${RUN_ARTIFACT_SOFT_LIMIT_BYTES}' DATA_SINKS='${DATA_SINKS}' METRICS_SINKS='${METRICS_SINKS}' EVENT_SINKS='${EVENT_SINKS}' ARTIFACT_SINKS='${ARTIFACT_SINKS}' WEBHOOK_URL='${WEBHOOK_URL}' WEBHOOK_TOKEN='${WEBHOOK_TOKEN}' WEBHOOK_TIMEOUT_S='${WEBHOOK_TIMEOUT_S}' WEBHOOK_ARTIFACT_INLINE_MAX_BYTES='${WEBHOOK_ARTIFACT_INLINE_MAX_BYTES}' SQLITE_SINK_PATH='${SQLITE_SINK_PATH}' CSI_COLLECTOR_CMD='${CSI_COLLECTOR_CMD}' CSI_OUTPUT_PATH='${CSI_OUTPUT_PATH}' CSI_OUTPUT_GLOB='${CSI_OUTPUT_GLOB}' CSI_FRAMES_REGEX='${CSI_FRAMES_REGEX}' CSI_OUTPUT_MAX_FILES='${CSI_OUTPUT_MAX_FILES}' CSI_PARSE_MAX_BYTES='${CSI_PARSE_MAX_BYTES}' CSI_REQUIRE_EVIDENCE='${CSI_REQUIRE_EVIDENCE}' CSI_MIN_FRAMES='${CSI_MIN_FRAMES}' CSI_MIN_OUTPUT_FILES='${CSI_MIN_OUTPUT_FILES}' CSI_SUDO_NONINTERACTIVE='${CSI_SUDO_NONINTERACTIVE}' CSI_TRAFFIC_ENABLE='${CSI_TRAFFIC_ENABLE}' CSI_TRAFFIC_TARGET='${CSI_TRAFFIC_TARGET}' CSI_TRAFFIC_IFACE='${CSI_TRAFFIC_IFACE}' CSI_TRAFFIC_INTERVAL_MS='${CSI_TRAFFIC_INTERVAL_MS}' CSI_HARD_TIMEOUT_S='${CSI_HARD_TIMEOUT_S}' CSI_CLEANUP_BEFORE_CAPTURE='${CSI_CLEANUP_BEFORE_CAPTURE}' CSI_CLEANUP_AFTER_CAPTURE='${CSI_CLEANUP_AFTER_CAPTURE}' CSI_KILL_PATTERNS='${CSI_KILL_PATTERNS}' python - <<'PY'
import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests
import urllib3

urllib3.disable_warnings()
base = os.environ.get('ELAB_BASE_URL', 'https://web/api/v2').rstrip('/')
key = os.environ.get('ELAB_API_KEY', '')
device_model = (os.environ.get('DEVICE_MODEL') or '02:42:ac:14:00:04').lower().strip()
device_pi = (os.environ.get('DEVICE_PI') or '').lower().strip()
run_pi = (os.environ.get('RUN_PI') or '').lower().strip() in {'1','true','yes'}
target_device_ids_csv = (os.environ.get('TARGET_DEVICE_IDS_CSV') or '').strip()
title_prefix = (os.environ.get('SMOKE_TITLE_PREFIX') or 'Fleet Smoke v3').strip() or 'Fleet Smoke v3'
tags_csv = os.environ.get('SMOKE_TAGS_CSV') or 'fleet,smoke:v3,created-by:monad-fleet'
smoke_profile = (os.environ.get('SMOKE_PROFILE') or 'full').strip().lower()
rssi_duration_s = max(1, int((os.environ.get('RSSI_DURATION_S') or '30').strip()))
rssi_interval_s = max(1, int((os.environ.get('RSSI_INTERVAL_S') or '1').strip()))
rssi_interval_s_pi = max(1, int((os.environ.get('RSSI_INTERVAL_S_PI') or '5').strip()))
rssi_artifact_stride = max(0, int((os.environ.get('RSSI_ARTIFACT_STRIDE') or '0').strip()))
full_duration_s = max(0, int((os.environ.get('FULL_DURATION_S') or '0').strip()))
full_interval_s = max(1, int((os.environ.get('FULL_INTERVAL_S') or '2').strip()))
full_interval_s_pi = max(1, int((os.environ.get('FULL_INTERVAL_S_PI') or '5').strip()))
full_artifact_stride = max(0, int((os.environ.get('FULL_ARTIFACT_STRIDE') or '0').strip()))
full_ble_timeout_s = max(1, int((os.environ.get('FULL_BLE_TIMEOUT_S') or '10').strip()))
full_csi_timeout_s = max(1, int((os.environ.get('FULL_CSI_TIMEOUT_S') or '10').strip()))
wireless_spec_duration_s = max(1, int((os.environ.get('WIRELESS_SPEC_DURATION_S') or '30').strip()))
wireless_spec_interval_s = max(1, int((os.environ.get('WIRELESS_SPEC_INTERVAL_S') or '2').strip()))
wireless_spec_interval_s_pi = max(1, int((os.environ.get('WIRELESS_SPEC_INTERVAL_S_PI') or '3').strip()))
wireless_spec_artifact_stride = max(0, int((os.environ.get('WIRELESS_SPEC_ARTIFACT_STRIDE') or '0').strip()))
wireless_spec_ble_timeout_s = max(1, int((os.environ.get('WIRELESS_SPEC_BLE_TIMEOUT_S') or '12').strip()))
wireless_spec_csi_timeout_s = max(1, int((os.environ.get('WIRELESS_SPEC_CSI_TIMEOUT_S') or '15').strip()))
wireless_spec_env_snapshot = (os.environ.get('WIRELESS_SPEC_ENV_SNAPSHOT') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'}
rfpaper_baseline_s = max(10, int((os.environ.get('RFPAPER_BASELINE_S') or '60').strip()))
rfpaper_scripted_s = max(10, int((os.environ.get('RFPAPER_SCRIPTED_S') or '60').strip()))
rfpaper_freeform_s = max(10, int((os.environ.get('RFPAPER_FREEFORM_S') or '60').strip()))
rfpaper_interval_s = max(1, int((os.environ.get('RFPAPER_INTERVAL_S') or '2').strip()))
rfpaper_interval_s_pi = max(1, int((os.environ.get('RFPAPER_INTERVAL_S_PI') or '5').strip()))
rfpaper_artifact_stride = max(0, int((os.environ.get('RFPAPER_ARTIFACT_STRIDE') or '0').strip()))
rfpaper_ble_timeout_s = max(1, int((os.environ.get('RFPAPER_BLE_TIMEOUT_S') or '20').strip()))
rfpaper_csi_timeout_s = max(1, int((os.environ.get('RFPAPER_CSI_TIMEOUT_S') or '30').strip()))
rfpaper_env_snapshot = (os.environ.get('RFPAPER_ENV_SNAPSHOT') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'}
rfpaper_activity_label = (os.environ.get('RFPAPER_ACTIVITY_LABEL') or 'walking-scripted').strip()
rfpaper_freeform_label = (os.environ.get('RFPAPER_FREEFORM_LABEL') or 'free-form').strip()
rfpaper_excitation_rate_hz = (os.environ.get('RFPAPER_EXCITATION_RATE_HZ') or '100').strip()
rfpaper_fixed_freq_mhz = (os.environ.get('RFPAPER_FIXED_FREQ_MHZ') or '5180').strip()
rfpaper_fixed_channel_width_mhz = (os.environ.get('RFPAPER_FIXED_CHANNEL_WIDTH_MHZ') or '80').strip()
rfpaper_fixed_mcs = (os.environ.get('RFPAPER_FIXED_MCS') or '').strip()
rfpaper_fixed_tx_power_dbm = (os.environ.get('RFPAPER_FIXED_TX_POWER_DBM') or '').strip()
rfpaper_csi_per_phase = (os.environ.get('RFPAPER_CSI_PER_PHASE') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'}
wifi_sample_start_jitter_s = max(0, int((os.environ.get('WIFI_SAMPLE_START_JITTER_S') or '0').strip()))
wifi_sample_start_at_epoch_s = (os.environ.get('WIFI_SAMPLE_START_AT_EPOCH_S') or '').strip()
rf_room_id = (os.environ.get('RF_ROOM_ID') or '').strip()
rf_scenario_id = (os.environ.get('RF_SCENARIO_ID') or '').strip()
rf_run_label = (os.environ.get('RF_RUN_LABEL') or '').strip()
csi_collector_cmd = (os.environ.get('CSI_COLLECTOR_CMD') or '').strip()
csi_output_path = (os.environ.get('CSI_OUTPUT_PATH') or '').strip()
csi_output_glob = (os.environ.get('CSI_OUTPUT_GLOB') or '').strip()
csi_frames_regex = (os.environ.get('CSI_FRAMES_REGEX') or '').strip()
csi_output_max_files = (os.environ.get('CSI_OUTPUT_MAX_FILES') or '').strip()
csi_parse_max_bytes = (os.environ.get('CSI_PARSE_MAX_BYTES') or '').strip()
csi_require_evidence = (os.environ.get('CSI_REQUIRE_EVIDENCE') or '').strip()
csi_min_frames = (os.environ.get('CSI_MIN_FRAMES') or '').strip()
csi_min_output_files = (os.environ.get('CSI_MIN_OUTPUT_FILES') or '').strip()
csi_sudo_noninteractive = (os.environ.get('CSI_SUDO_NONINTERACTIVE') or '').strip()
csi_traffic_enable = (os.environ.get('CSI_TRAFFIC_ENABLE') or '').strip()
csi_traffic_target = (os.environ.get('CSI_TRAFFIC_TARGET') or '').strip()
csi_traffic_iface = (os.environ.get('CSI_TRAFFIC_IFACE') or '').strip()
csi_traffic_interval_ms = (os.environ.get('CSI_TRAFFIC_INTERVAL_MS') or '').strip()
csi_hard_timeout_s = (os.environ.get('CSI_HARD_TIMEOUT_S') or '').strip()
csi_cleanup_before_capture = (os.environ.get('CSI_CLEANUP_BEFORE_CAPTURE') or '').strip()
csi_cleanup_after_capture = (os.environ.get('CSI_CLEANUP_AFTER_CAPTURE') or '').strip()
csi_kill_patterns = (os.environ.get('CSI_KILL_PATTERNS') or '').strip()
artifact_upload_target = (os.environ.get('ARTIFACT_UPLOAD_TARGET') or '').strip()
artifact_upload_during_measure = (os.environ.get('ARTIFACT_UPLOAD_DURING_MEASURE') or '').strip()
artifact_evict_after_upload = (os.environ.get('ARTIFACT_EVICT_AFTER_UPLOAD') or '').strip()
run_artifact_soft_limit_bytes = (os.environ.get('RUN_ARTIFACT_SOFT_LIMIT_BYTES') or '').strip()
data_sinks = (os.environ.get('DATA_SINKS') or '').strip()
metrics_sinks = (os.environ.get('METRICS_SINKS') or '').strip()
event_sinks = (os.environ.get('EVENT_SINKS') or '').strip()
artifact_sinks = (os.environ.get('ARTIFACT_SINKS') or '').strip()
webhook_url = (os.environ.get('WEBHOOK_URL') or '').strip()
webhook_token = (os.environ.get('WEBHOOK_TOKEN') or '').strip()
webhook_timeout_s = (os.environ.get('WEBHOOK_TIMEOUT_S') or '').strip()
webhook_artifact_inline_max_bytes = (os.environ.get('WEBHOOK_ARTIFACT_INLINE_MAX_BYTES') or '').strip()
sqlite_sink_path = (os.environ.get('SQLITE_SINK_PATH') or '').strip()
now = datetime.now(timezone.utc)

explicit_device_ids = [row.strip().lower() for row in target_device_ids_csv.split(',') if row.strip()]
if run_pi:
    # Always use Pi-safe sampling intervals when targeting real Pi runs.
    rssi_interval_s = rssi_interval_s_pi
    full_interval_s = full_interval_s_pi
    wireless_spec_interval_s = wireless_spec_interval_s_pi
    rfpaper_interval_s = rfpaper_interval_s_pi
if explicit_device_ids:
    # Keep order stable while removing duplicates.
    seen_ids = set()
    device_ids = []
    for row in explicit_device_ids:
        if row in seen_ids:
            continue
        seen_ids.add(row)
        device_ids.append(row)
elif run_pi and device_pi:
    device_ids = [device_pi]
else:
    device_ids = [device_model]

def with_artifact_upload_env(env_map: dict[str, str]) -> dict[str, str]:
    merged = dict(env_map)
    if artifact_upload_target:
        merged['ARTIFACT_UPLOAD_TARGET'] = artifact_upload_target
    if artifact_upload_during_measure:
        merged['ARTIFACT_UPLOAD_DURING_MEASURE'] = artifact_upload_during_measure
    if artifact_evict_after_upload:
        merged['ARTIFACT_EVICT_AFTER_UPLOAD'] = artifact_evict_after_upload
    if run_artifact_soft_limit_bytes:
        merged['RUN_ARTIFACT_SOFT_LIMIT_BYTES'] = run_artifact_soft_limit_bytes
    return merged

def with_sink_env(env_map: dict[str, str]) -> dict[str, str]:
    merged = dict(env_map)
    if data_sinks:
        merged['DATA_SINKS'] = data_sinks
    if metrics_sinks:
        merged['METRICS_SINKS'] = metrics_sinks
    if event_sinks:
        merged['EVENT_SINKS'] = event_sinks
    if artifact_sinks:
        merged['ARTIFACT_SINKS'] = artifact_sinks
    if webhook_url:
        merged['WEBHOOK_URL'] = webhook_url
    if webhook_token:
        merged['WEBHOOK_TOKEN'] = webhook_token
    if webhook_timeout_s:
        merged['WEBHOOK_TIMEOUT_S'] = webhook_timeout_s
    if webhook_artifact_inline_max_bytes:
        merged['WEBHOOK_ARTIFACT_INLINE_MAX_BYTES'] = webhook_artifact_inline_max_bytes
    if sqlite_sink_path:
        merged['SQLITE_SINK_PATH'] = sqlite_sink_path
    return merged

def with_runtime_env(env_map: dict[str, str]) -> dict[str, str]:
    return with_sink_env(with_artifact_upload_env(env_map))

def with_command_runtime_env(command: dict) -> dict:
    merged_command = dict(command)
    current_env = merged_command.get('env') if isinstance(merged_command.get('env'), dict) else {}
    merged_env = with_runtime_env(current_env)
    if merged_env:
        merged_command['env'] = merged_env
    elif 'env' in merged_command and not merged_command['env']:
        merged_command.pop('env', None)
    return merged_command

csi_env = {}
if csi_collector_cmd:
    csi_env['CSI_COLLECTOR_CMD'] = csi_collector_cmd
if csi_output_path:
    csi_env['CSI_OUTPUT_PATH'] = csi_output_path
if csi_output_glob:
    csi_env['CSI_OUTPUT_GLOB'] = csi_output_glob
if csi_frames_regex:
    csi_env['CSI_FRAMES_REGEX'] = csi_frames_regex
if csi_output_max_files:
    csi_env['CSI_OUTPUT_MAX_FILES'] = csi_output_max_files
if csi_parse_max_bytes:
    csi_env['CSI_PARSE_MAX_BYTES'] = csi_parse_max_bytes
if csi_require_evidence:
    csi_env['CSI_REQUIRE_EVIDENCE'] = csi_require_evidence
if csi_min_frames:
    csi_env['CSI_MIN_FRAMES'] = csi_min_frames
if csi_min_output_files:
    csi_env['CSI_MIN_OUTPUT_FILES'] = csi_min_output_files
if csi_sudo_noninteractive:
    csi_env['CSI_SUDO_NONINTERACTIVE'] = csi_sudo_noninteractive
if csi_traffic_enable:
    csi_env['CSI_TRAFFIC_ENABLE'] = csi_traffic_enable
if csi_traffic_target:
    csi_env['CSI_TRAFFIC_TARGET'] = csi_traffic_target
if csi_traffic_iface:
    csi_env['CSI_TRAFFIC_IFACE'] = csi_traffic_iface
if csi_traffic_interval_ms:
    csi_env['CSI_TRAFFIC_INTERVAL_MS'] = csi_traffic_interval_ms
if csi_hard_timeout_s:
    csi_env['CSI_HARD_TIMEOUT_S'] = csi_hard_timeout_s
if csi_cleanup_before_capture:
    csi_env['CSI_CLEANUP_BEFORE_CAPTURE'] = csi_cleanup_before_capture
if csi_cleanup_after_capture:
    csi_env['CSI_CLEANUP_AFTER_CAPTURE'] = csi_cleanup_after_capture
if csi_kill_patterns:
    csi_env['CSI_KILL_PATTERNS'] = csi_kill_patterns
csi_env = with_runtime_env(csi_env)

def wifi_sampling_env(duration_s: int, interval_s: int, artifact_stride: int) -> dict[str, str]:
    env_map = {
        'WIFI_SAMPLE_DURATION_S': str(duration_s),
        'WIFI_SAMPLE_INTERVAL_S': str(interval_s),
        'WIFI_SAMPLE_EMIT_HTTP': '1',
        'WIFI_SAMPLE_ARTIFACT_STRIDE': str(max(0, artifact_stride)),
    }
    if wifi_sample_start_jitter_s > 0:
        env_map['WIFI_SAMPLE_START_JITTER_S'] = str(wifi_sample_start_jitter_s)
    if wifi_sample_start_at_epoch_s:
        env_map['WIFI_SAMPLE_START_AT_EPOCH_S'] = wifi_sample_start_at_epoch_s
    return with_runtime_env(env_map)

def with_rf_env(env_map: dict[str, str], phase: str, activity_label: str = '') -> dict[str, str]:
    merged = dict(env_map)
    merged['RF_PROFILE'] = 'rfpaper'
    merged['RF_PHASE'] = phase
    if activity_label:
        merged['RF_ACTIVITY_LABEL'] = activity_label
    if rf_room_id:
        merged['RF_ROOM_ID'] = rf_room_id
    if rf_scenario_id:
        merged['RF_SCENARIO_ID'] = rf_scenario_id
    if rf_run_label:
        merged['RF_RUN_LABEL'] = rf_run_label
    if rfpaper_excitation_rate_hz:
        merged['RF_EXCITATION_RATE_HZ'] = rfpaper_excitation_rate_hz
    if rfpaper_fixed_freq_mhz:
        merged['RF_FIXED_FREQ_MHZ'] = rfpaper_fixed_freq_mhz
    if rfpaper_fixed_channel_width_mhz:
        merged['RF_FIXED_CHANNEL_WIDTH_MHZ'] = rfpaper_fixed_channel_width_mhz
    if rfpaper_fixed_mcs:
        merged['RF_FIXED_MCS'] = rfpaper_fixed_mcs
    if rfpaper_fixed_tx_power_dbm:
        merged['RF_FIXED_TX_POWER_DBM'] = rfpaper_fixed_tx_power_dbm
    return merged

commands = [
    {'id': 'wifi-scan', 'type': 'WIFI_SCAN', 'timeout_s': 15, 'retries': 0},
    {'id': 'ble-scan', 'type': 'BLE_SCAN', 'timeout_s': 15, 'retries': 0},
    {'id': 'csi-capture', 'type': 'CAPTURE_CSI', 'timeout_s': 15, 'retries': 0, **({'env': csi_env} if csi_env else {})},
]
group_name = 'WiFi BLE CSI v3'
group_id = 'v3-wifi-ble-csi'
window_minutes = 30

if smoke_profile == 'full' and full_duration_s > 0:
    timeout_s = max(10, full_duration_s + 10)
    commands = [
        {
            'id': 'wifi-scan-series',
            'type': 'WIFI_SCAN',
            'timeout_s': timeout_s,
            'retries': 0,
            'env': wifi_sampling_env(full_duration_s, full_interval_s, full_artifact_stride),
        },
        {'id': 'ble-scan', 'type': 'BLE_SCAN', 'timeout_s': full_ble_timeout_s, 'retries': 0},
        {'id': 'csi-capture', 'type': 'CAPTURE_CSI', 'timeout_s': full_csi_timeout_s, 'retries': 0, **({'env': csi_env} if csi_env else {})},
    ]
    group_name = f'WiFi BLE CSI Full {full_duration_s}s/{full_interval_s}s'
    group_id = 'v3-wifi-ble-csi-full-sampled'
    window_minutes = max(5, int((full_duration_s + full_ble_timeout_s + full_csi_timeout_s + 180) / 60))

if smoke_profile == 'rssi':
    timeout_s = max(10, rssi_duration_s + 5)
    commands = [
        {
            'id': 'wifi-rssi-series',
            'type': 'WIFI_SCAN',
            'timeout_s': timeout_s,
            'retries': 0,
            'env': wifi_sampling_env(rssi_duration_s, rssi_interval_s, rssi_artifact_stride),
        }
    ]
    group_name = f'WiFi RSSI Sampling {rssi_duration_s}s/{rssi_interval_s}s'
    group_id = 'v3-wifi-rssi-sampling'
    window_minutes = max(5, int((rssi_duration_s + 120) / 60))

if smoke_profile == 'wireless_spec':
    timeout_s = max(10, wireless_spec_duration_s + 10)
    commands = [
        {
            'id': 'wifi-sensing-series',
            'type': 'WIFI_SCAN',
            'timeout_s': timeout_s,
            'retries': 0,
            'env': wifi_sampling_env(wireless_spec_duration_s, wireless_spec_interval_s, wireless_spec_artifact_stride),
        },
        {'id': 'ble-scan', 'type': 'BLE_SCAN', 'timeout_s': wireless_spec_ble_timeout_s, 'retries': 0},
        {
            'id': 'csi-capture',
            'type': 'CAPTURE_CSI',
            'timeout_s': wireless_spec_csi_timeout_s,
            'retries': 0,
            **({'env': csi_env} if csi_env else {}),
        },
    ]
    if wireless_spec_env_snapshot:
        commands.append(
            {
                'id': 'rf-env-snapshot',
                'type': 'SHELL',
                'timeout_s': 20,
                'argv': [
                    'sh',
                    '-lc',
                    'date -Iseconds; uname -a; iw dev || true; iw dev wlan0 info || true; '
                    'iw dev wlan0 link || true; cat /proc/net/wireless || true; '
                    'bluetoothctl show || true; hciconfig -a || true',
                ],
                'retries': 0,
            }
        )
    group_name = f'Wireless Spec WiFi+BLE+CSI {wireless_spec_duration_s}s/{wireless_spec_interval_s}s'
    group_id = 'v3-wireless-spec'
    window_minutes = max(5, int((wireless_spec_duration_s + wireless_spec_ble_timeout_s + wireless_spec_csi_timeout_s + 180) / 60))

if smoke_profile == 'rfpaper':
    baseline_timeout_s = max(10, rfpaper_baseline_s + 10)
    scripted_timeout_s = max(10, rfpaper_scripted_s + 10)
    freeform_timeout_s = max(10, rfpaper_freeform_s + 10)
    baseline_csi_timeout_s = max(rfpaper_csi_timeout_s, rfpaper_baseline_s)
    scripted_csi_timeout_s = max(rfpaper_csi_timeout_s, rfpaper_scripted_s)
    freeform_csi_timeout_s = max(rfpaper_csi_timeout_s, rfpaper_freeform_s)
    rfpaper_csi_env = dict(csi_env)
    if not rfpaper_csi_env.get('CSI_REQUIRE_EVIDENCE'):
        rfpaper_csi_env['CSI_REQUIRE_EVIDENCE'] = '1'
    if not rfpaper_csi_env.get('CSI_MIN_OUTPUT_FILES') and not rfpaper_csi_env.get('CSI_MIN_FRAMES'):
        rfpaper_csi_env['CSI_MIN_OUTPUT_FILES'] = '1'
    commands = [
        {
            'id': 'wifi-empty-baseline',
            'type': 'WIFI_SCAN',
            'timeout_s': baseline_timeout_s,
            'retries': 0,
            'env': with_rf_env(
                wifi_sampling_env(rfpaper_baseline_s, rfpaper_interval_s, rfpaper_artifact_stride),
                'baseline',
                'empty-room',
            ),
        },
        {'id': 'ble-scan-baseline', 'type': 'BLE_SCAN', 'timeout_s': rfpaper_ble_timeout_s, 'retries': 0},
        {
            'id': 'csi-capture-baseline',
            'type': 'CAPTURE_CSI',
            'timeout_s': baseline_csi_timeout_s,
            'retries': 0,
            'env': with_rf_env(rfpaper_csi_env, 'baseline', 'empty-room'),
        },
        {
            'id': 'wifi-scripted-activity',
            'type': 'WIFI_SCAN',
            'timeout_s': scripted_timeout_s,
            'retries': 0,
            'env': with_rf_env(
                wifi_sampling_env(rfpaper_scripted_s, rfpaper_interval_s, rfpaper_artifact_stride),
                'scripted',
                rfpaper_activity_label,
            ),
        },
        {'id': 'ble-scan-scripted', 'type': 'BLE_SCAN', 'timeout_s': rfpaper_ble_timeout_s, 'retries': 0},
        {
            'id': 'csi-capture-scripted',
            'type': 'CAPTURE_CSI',
            'timeout_s': scripted_csi_timeout_s,
            'retries': 0,
            'env': with_rf_env(rfpaper_csi_env, 'scripted', rfpaper_activity_label),
        },
        {
            'id': 'wifi-freeform',
            'type': 'WIFI_SCAN',
            'timeout_s': freeform_timeout_s,
            'retries': 0,
            'env': with_rf_env(
                wifi_sampling_env(rfpaper_freeform_s, rfpaper_interval_s, rfpaper_artifact_stride),
                'freeform',
                rfpaper_freeform_label,
            ),
        },
        {'id': 'ble-scan-freeform', 'type': 'BLE_SCAN', 'timeout_s': rfpaper_ble_timeout_s, 'retries': 0},
        {
            'id': 'csi-capture-freeform',
            'type': 'CAPTURE_CSI',
            'timeout_s': freeform_csi_timeout_s,
            'retries': 0,
            'env': with_rf_env(rfpaper_csi_env, 'freeform', rfpaper_freeform_label),
        },
    ]
    if not rfpaper_csi_per_phase:
        commands = [
            cmd for cmd in commands
            if cmd.get('id') not in {'csi-capture-baseline', 'csi-capture-freeform'}
        ]
    if rfpaper_env_snapshot:
        commands.append(
            {
                'id': 'rf-env-snapshot',
                'type': 'SHELL',
                'timeout_s': 30,
                'argv': [
                    'sh',
                    '-lc',
                    'date -Iseconds; uname -a; '
                    'ip route || true; iw dev || true; iw dev wlan0 info || true; iw dev wlan0 link || true; '
                    'cat /proc/net/wireless || true; '
                    'cat /sys/class/thermal/thermal_zone0/temp || true; '
                    'vcgencmd measure_temp 2>/dev/null || true; '
                    'bluetoothctl show || true; hciconfig -a || true',
                ],
                'retries': 0,
            }
        )
    group_name = (
        f'RFPaper Deterministic Run '
        f'baseline={rfpaper_baseline_s}s scripted={rfpaper_scripted_s}s freeform={rfpaper_freeform_s}s '
        f'interval={rfpaper_interval_s}s'
    )
    group_id = 'v3-rfpaper-deterministic'
    total_phase_s = rfpaper_baseline_s + rfpaper_scripted_s + rfpaper_freeform_s
    csi_window_s = scripted_csi_timeout_s if not rfpaper_csi_per_phase else (baseline_csi_timeout_s + scripted_csi_timeout_s + freeform_csi_timeout_s)
    window_minutes = max(8, int((total_phase_s + (3 * rfpaper_ble_timeout_s) + csi_window_s + 240) / 60))

commands = [with_command_runtime_env(command) for command in commands]

policy = {
    'target_selector': {'device_ids': device_ids},
    'range': {
        'from': (now - timedelta(minutes=2)).isoformat().replace('+00:00', 'Z'),
        'to': (now + timedelta(minutes=window_minutes)).isoformat().replace('+00:00', 'Z'),
    },
    'command_groups': [
        {
            'id': group_id,
            'name': group_name,
            'failure_mode': 'FAIL_FAST',
            'commands': commands,
        }
    ],
}

metadata = {'fleet': {'policy': policy}}
rf_context = {}
if rf_room_id:
    rf_context['room_id'] = rf_room_id
if rf_scenario_id:
    rf_context['scenario_id'] = rf_scenario_id
if rf_run_label:
    rf_context['run_label'] = rf_run_label
if rf_context:
    rf_context['created_at'] = now.isoformat().replace('+00:00', 'Z')
    rf_context['target_device_ids'] = device_ids
    if smoke_profile == 'rfpaper':
      rf_context['rfpaper'] = {
          'baseline_s': rfpaper_baseline_s,
          'scripted_s': rfpaper_scripted_s,
          'freeform_s': rfpaper_freeform_s,
          'sample_interval_s': rfpaper_interval_s,
          'ble_timeout_s': rfpaper_ble_timeout_s,
          'csi_timeout_s': rfpaper_csi_timeout_s,
          'excitation_rate_hz': rfpaper_excitation_rate_hz,
          'fixed_freq_mhz': rfpaper_fixed_freq_mhz,
          'fixed_channel_width_mhz': rfpaper_fixed_channel_width_mhz,
          'fixed_mcs': rfpaper_fixed_mcs,
          'fixed_tx_power_dbm': rfpaper_fixed_tx_power_dbm,
          'csi_per_phase': rfpaper_csi_per_phase,
          'require_ethernet_control': (os.environ.get('RFPAPER_REQUIRE_ETHERNET_CONTROL') or 'false').strip().lower() in {'1', 'true', 'yes', 'on'},
          'activity_label': rfpaper_activity_label,
          'freeform_label': rfpaper_freeform_label,
      }
    metadata['fleet']['rf_context'] = rf_context

tags = [t.strip() for t in tags_csv.split(',') if t.strip()]
if 'fleet' not in tags:
    tags.insert(0, 'fleet')

payload = {
    'title': '%s %s' % (title_prefix, now.strftime('%Y-%m-%d %H:%M:%SZ')),
    'tags': tags,
    'body': 'Smoke experiment created by scripts/smoke/v3_end_to_end_smoke.sh. Safe to delete.',
    # eLabFTW POST /experiments may store string metadata double-encoded on some builds.
    # Create first, then PATCH metadata to the canonical JSON string form used by the policy resolver.
    'metadata': '{}',
}
resp = requests.post(
    '%s/experiments' % base,
    headers={'Authorization': key},
    json=payload,
    verify=False,
    timeout=20,
)
resp.raise_for_status()
location = resp.headers.get('location', '')
match = re.search(r'/experiments/(\\d+)$', location)
if not match:
    raise RuntimeError('Could not parse experiment id from Location header: %s' % location)

exp_id = int(match.group(1))
patch = requests.patch(
    '%s/experiments/%d' % (base, exp_id),
    headers={'Authorization': key},
    json={'metadata': json.dumps(metadata, sort_keys=True, separators=(',', ':'))},
    verify=False,
    timeout=20,
)
patch.raise_for_status()
print(exp_id)
PY"
)"
echo "Created smoke experiment id=${SMOKE_EXPERIMENT_ID}"

SMOKE_LAST_STEP="5_run_agent_cycle"
if [[ "${RUN_PI}" == "true" && "${RUN_PI_PASSIVE}" != "true" ]]; then
  echo "[5/8] Run Raspberry Pi one-cycle report(s) (real Wi-Fi/BLE collection if tools are available)"
  REQUIRE_ETHERNET_CONTROL="false"
  ENFORCE_CONTROL_PLANE_ROUTE="false"
  if [[ "${SMOKE_PROFILE}" == "rfpaper" && "${RFPAPER_REQUIRE_ETHERNET_CONTROL}" == "true" ]]; then
    REQUIRE_ETHERNET_CONTROL="true"
    ENFORCE_CONTROL_PLANE_ROUTE="true"
  fi
  PI_HOSTS_CSV_FOR_AGENT=""
  if [[ "${PI_HOSTS_FALLBACK_ACTIVE}" == "true" ]]; then
    PI_HOSTS_CSV_FOR_AGENT="${PI_HOSTS_FALLBACK_CSV}"
  fi
  for idx in "${!PI_HOSTS_LIST[@]}"; do
    host="${PI_HOSTS_LIST[$idx]}"
    agent_override=""
    if [[ ${#AGENT_IDS_LIST[@]} -gt 0 ]]; then
      agent_override="${AGENT_IDS_LIST[$idx]}"
    fi
    echo "  -> host=${host} agent_id=${agent_override:-auto-detect}"
    PI_HOSTS_CSV="${PI_HOSTS_CSV_FOR_AGENT}" PI_RECOVERY_HOSTS_CSV="${PI_RECOVERY_HOSTS_CSV}" EXECUTE_POLICY="true" MAX_SYNC_CYCLES="${MAX_SYNC_CYCLES}" AGENT_ID="${agent_override}" CONTROL_PLANE_IFACE="${CONTROL_PLANE_IFACE}" WIFI_SCAN_IFACE="${WIFI_SCAN_IFACE}" CONTROL_PLANE_BAND_EXPECT="${CONTROL_PLANE_BAND_EXPECT}" CSI_MEASURE_IFACE="${CSI_MEASURE_IFACE}" CSI_MEASURE_BAND_EXPECT="${CSI_MEASURE_BAND_EXPECT}" CSI_REQUIRE_SEPARATE_IFACE="${CSI_REQUIRE_SEPARATE_IFACE}" CSI_IFACE_FLAG="${CSI_IFACE_FLAG}" AUTO_SWITCH_CONTROL_PLANE_IFACE="${AUTO_SWITCH_CONTROL_PLANE_IFACE}" AUTO_SWITCH_CSI_MEASURE_IFACE="${AUTO_SWITCH_CSI_MEASURE_IFACE}" SINGLE_RADIO_MODE="${SINGLE_RADIO_MODE}" SINGLE_RADIO_WIFI_PROFILE="${SINGLE_RADIO_WIFI_PROFILE}" SINGLE_RADIO_RECOVERY_RETRIES="${SINGLE_RADIO_RECOVERY_RETRIES}" SINGLE_RADIO_RECOVERY_ROUTE_WAIT_S="${SINGLE_RADIO_RECOVERY_ROUTE_WAIT_S}" SINGLE_RADIO_RECOVERY_CMD="${SINGLE_RADIO_RECOVERY_CMD}" SINGLE_RADIO_ROUTE_GATE="${SINGLE_RADIO_ROUTE_GATE}" SINGLE_RADIO_ROUTE_HEALTH_TIMEOUT_S="${SINGLE_RADIO_ROUTE_HEALTH_TIMEOUT_S}" SINGLE_RADIO_REBOOT_ON_RECOVERY_FAIL="${SINGLE_RADIO_REBOOT_ON_RECOVERY_FAIL}" WIFI_SAMPLE_MIN_INTERVAL_S="${WIFI_SAMPLE_MIN_INTERVAL_S}" WIFI_SAMPLE_MAX_DURATION_S="${WIFI_SAMPLE_MAX_DURATION_S}" WIFI_SAMPLE_MAX_POINTS="${WIFI_SAMPLE_MAX_POINTS}" ENABLE_ELAB_ARTIFACT_UPLOAD="${ENABLE_ELAB_ARTIFACT_UPLOAD}" ARTIFACT_UPLOAD_MAX_BYTES="${ARTIFACT_UPLOAD_MAX_BYTES}" ARTIFACT_UPLOAD_TARGET="${ARTIFACT_UPLOAD_TARGET}" ARTIFACT_UPLOAD_DURING_MEASURE="${ARTIFACT_UPLOAD_DURING_MEASURE}" ARTIFACT_EVICT_AFTER_UPLOAD="${ARTIFACT_EVICT_AFTER_UPLOAD}" ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S="${ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S}" ARTIFACT_UPLOAD_BACKOFF_S="${ARTIFACT_UPLOAD_BACKOFF_S}" RUN_ARTIFACT_SOFT_LIMIT_BYTES="${RUN_ARTIFACT_SOFT_LIMIT_BYTES}" DATA_SINKS="${DATA_SINKS}" METRICS_SINKS="${METRICS_SINKS}" EVENT_SINKS="${EVENT_SINKS}" ARTIFACT_SINKS="${ARTIFACT_SINKS}" WEBHOOK_URL="${WEBHOOK_URL}" WEBHOOK_TOKEN="${WEBHOOK_TOKEN}" WEBHOOK_TIMEOUT_S="${WEBHOOK_TIMEOUT_S}" WEBHOOK_ARTIFACT_INLINE_MAX_BYTES="${WEBHOOK_ARTIFACT_INLINE_MAX_BYTES}" SQLITE_SINK_PATH="${SQLITE_SINK_PATH}" REPORT_RPC_TIMEOUT_S="${REPORT_RPC_TIMEOUT_S}" CONTROL_RPC_TIMEOUT_S="${CONTROL_RPC_TIMEOUT_S}" AGENT_RUN_TIMEOUT_S="${AGENT_RUN_TIMEOUT_S}" AGENT_STATUS_POLL_S="${AGENT_STATUS_POLL_S}" SSH_DROP_FAIL_FAST_S="${SSH_DROP_FAIL_FAST_S}" RESTART_NOW_HINT_ON_SSH_DROP="${RESTART_NOW_HINT_ON_SSH_DROP}" MANAGE_SYSTEMD_AGENT="${MANAGE_SYSTEMD_AGENT}" SYSTEMD_AGENT_SERVICE="${SYSTEMD_AGENT_SERVICE}" SEPARATE_MEASURE_AND_REPORT="${SEPARATE_MEASURE_AND_REPORT}" RESTART_CONTROL_PLANE_BEFORE_REPORT="${RESTART_CONTROL_PLANE_BEFORE_REPORT}" CONTROL_PLANE_RESTART_IFACE="${CONTROL_PLANE_RESTART_IFACE}" CONTROL_PLANE_RESTART_DOWN_S="${CONTROL_PLANE_RESTART_DOWN_S}" CONTROL_PLANE_RESTART_WAIT_S="${CONTROL_PLANE_RESTART_WAIT_S}" CONTROL_PLANE_RESTART_CMD="${CONTROL_PLANE_RESTART_CMD}" ALLOW_WIFI_CONTROL_PLANE_RESTART="${ALLOW_WIFI_CONTROL_PLANE_RESTART}" ALLOW_WIFI_DISRUPTIVE_CSI="${ALLOW_WIFI_DISRUPTIVE_CSI}" REQUIRE_REAL_CSI="${REQUIRE_REAL_CSI}" REQUIRE_ETHERNET_CONTROL="${REQUIRE_ETHERNET_CONTROL}" REQUIRE_WIFI_CONTROL="${REQUIRE_WIFI_CONTROL}" STRICT_CONTROL_PLANE_IFACE="${STRICT_CONTROL_PLANE_IFACE}" STRICT_WIFI_SCAN_IFACE="${STRICT_WIFI_SCAN_IFACE}" ENFORCE_CONTROL_PLANE_ROUTE="${ENFORCE_CONTROL_PLANE_ROUTE}" CSI_COLLECTOR_CMD="${CSI_COLLECTOR_CMD}" CSI_OUTPUT_PATH="${CSI_OUTPUT_PATH}" CSI_OUTPUT_GLOB="${CSI_OUTPUT_GLOB}" CSI_FRAMES_REGEX="${CSI_FRAMES_REGEX}" CSI_OUTPUT_MAX_FILES="${CSI_OUTPUT_MAX_FILES}" CSI_PARSE_MAX_BYTES="${CSI_PARSE_MAX_BYTES}" CSI_REQUIRE_EVIDENCE="${CSI_REQUIRE_EVIDENCE}" CSI_MIN_FRAMES="${CSI_MIN_FRAMES}" CSI_MIN_OUTPUT_FILES="${CSI_MIN_OUTPUT_FILES}" CSI_SUDO_NONINTERACTIVE="${CSI_SUDO_NONINTERACTIVE}" CSI_TRAFFIC_ENABLE="${CSI_TRAFFIC_ENABLE}" CSI_TRAFFIC_TARGET="${CSI_TRAFFIC_TARGET}" CSI_TRAFFIC_IFACE="${CSI_TRAFFIC_IFACE}" CSI_TRAFFIC_INTERVAL_MS="${CSI_TRAFFIC_INTERVAL_MS}" CSI_ALLOW_THROTTLED="${CSI_ALLOW_THROTTLED}" CSI_HARD_TIMEOUT_S="${CSI_HARD_TIMEOUT_S}" CSI_CLEANUP_BEFORE_CAPTURE="${CSI_CLEANUP_BEFORE_CAPTURE}" CSI_CLEANUP_AFTER_CAPTURE="${CSI_CLEANUP_AFTER_CAPTURE}" CSI_KILL_PATTERNS="${CSI_KILL_PATTERNS}" scripts/rpi/smoke_test_agent.sh "${host}"
  done
elif [[ "${RUN_PI}" == "true" && "${RUN_PI_PASSIVE}" == "true" ]]; then
  echo "[5/8] Passive Pi mode: skip remote agent execution (RUN_PI_PASSIVE=true)"
  echo "      Waiting for always-running Pi agent(s) to pick up and execute the created policy."
else
  echo "[5/8] Run local model-device one-cycle report"
  docker compose exec -T model-device sh -lc "MAX_SYNC_CYCLES='${MAX_SYNC_CYCLES}' EXECUTE_POLICY='false' AGENT_ID='${DEVICE_MODEL}' CONTROL_PLANE_MODE='RF_SHARING' WIFI_SAMPLE_MIN_INTERVAL_S='${WIFI_SAMPLE_MIN_INTERVAL_S}' WIFI_SAMPLE_MAX_DURATION_S='${WIFI_SAMPLE_MAX_DURATION_S}' WIFI_SAMPLE_MAX_POINTS='${WIFI_SAMPLE_MAX_POINTS}' ENABLE_ELAB_ARTIFACT_UPLOAD='${ENABLE_ELAB_ARTIFACT_UPLOAD}' ARTIFACT_UPLOAD_MAX_BYTES='${ARTIFACT_UPLOAD_MAX_BYTES}' ARTIFACT_UPLOAD_TARGET='${ARTIFACT_UPLOAD_TARGET}' ARTIFACT_UPLOAD_DURING_MEASURE='${ARTIFACT_UPLOAD_DURING_MEASURE}' ARTIFACT_EVICT_AFTER_UPLOAD='${ARTIFACT_EVICT_AFTER_UPLOAD}' ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S='${ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S}' ARTIFACT_UPLOAD_BACKOFF_S='${ARTIFACT_UPLOAD_BACKOFF_S}' RUN_ARTIFACT_SOFT_LIMIT_BYTES='${RUN_ARTIFACT_SOFT_LIMIT_BYTES}' DATA_SINKS='${DATA_SINKS}' METRICS_SINKS='${METRICS_SINKS}' EVENT_SINKS='${EVENT_SINKS}' ARTIFACT_SINKS='${ARTIFACT_SINKS}' WEBHOOK_URL='${WEBHOOK_URL}' WEBHOOK_TOKEN='${WEBHOOK_TOKEN}' WEBHOOK_TIMEOUT_S='${WEBHOOK_TIMEOUT_S}' WEBHOOK_ARTIFACT_INLINE_MAX_BYTES='${WEBHOOK_ARTIFACT_INLINE_MAX_BYTES}' SQLITE_SINK_PATH='${SQLITE_SINK_PATH}' REPORT_RPC_TIMEOUT_S='${REPORT_RPC_TIMEOUT_S}' CONTROL_RPC_TIMEOUT_S='${CONTROL_RPC_TIMEOUT_S}' SEPARATE_MEASURE_AND_REPORT='${SEPARATE_MEASURE_AND_REPORT}' RESTART_CONTROL_PLANE_BEFORE_REPORT='${RESTART_CONTROL_PLANE_BEFORE_REPORT}' CONTROL_PLANE_RESTART_IFACE='${CONTROL_PLANE_RESTART_IFACE}' CONTROL_PLANE_RESTART_DOWN_S='${CONTROL_PLANE_RESTART_DOWN_S}' CONTROL_PLANE_RESTART_WAIT_S='${CONTROL_PLANE_RESTART_WAIT_S}' CONTROL_PLANE_RESTART_CMD='${CONTROL_PLANE_RESTART_CMD}' ALLOW_WIFI_CONTROL_PLANE_RESTART='${ALLOW_WIFI_CONTROL_PLANE_RESTART}' python -u agent_v2_client.py"
fi

SMOKE_LAST_STEP="6_optional_ingest"
if [[ "${INJECT_HYPOTHETICAL_METRICS}" == "true" ]]; then
echo "[6/8] Send hypothetical low-level board payload to HTTP ingest"
docker compose exec -T monad-fleet-service sh -lc "python - <<'PY'
import requests

payload = {
    'source': 'embedded-hypothetical',
    'device_id': 'lowlevel-board-01',
    'metrics': [
        {'name': 'wifi_rssi_dbm', 'value': -58.2},
        {'name': 'ble_adv_count', 'value': 81},
        {'name': 'csi_frames_count', 'value': 420},
    ],
}
resp = requests.post('http://127.0.0.1:9108/ingest/v1/metrics', json=payload, timeout=10)
resp.raise_for_status()
print(resp.text)
PY"
else
echo "[6/8] Skip hypothetical low-level board payload (INJECT_HYPOTHETICAL_METRICS=${INJECT_HYPOTHETICAL_METRICS})"
fi

SMOKE_LAST_STEP="7_wait_scrape"
WAIT_S=20
if [[ "${RUN_PI}" == "true" && "${RUN_PI_PASSIVE}" == "true" ]]; then
  WAIT_S="${PASSIVE_AGENT_WAIT_S}"
fi
echo "[7/8] Wait for Prometheus scrape + remote_write (sleep=${WAIT_S}s)"
sleep "${WAIT_S}"

SMOKE_LAST_STEP="8_verify"
echo "[8/8] Verify metrics in Prometheus and Mimir"
docker compose exec -T monad-fleet-service sh -lc "DEVICE_ID='${DEVICE_PI}' TARGET_DEVICE_IDS_CSV='${TARGET_DEVICE_IDS_CSV}' RUN_PI='${RUN_PI}' DEVICE_MODEL='${DEVICE_MODEL}' INJECT_HYPOTHETICAL_METRICS='${INJECT_HYPOTHETICAL_METRICS}' python - <<'PY'
import requests
import os

run_pi = (os.environ.get('RUN_PI') or '').lower().strip() in {'1','true','yes'}
target_csv = (os.environ.get('TARGET_DEVICE_IDS_CSV') or '').strip()
inject_hypothetical = (os.environ.get('INJECT_HYPOTHETICAL_METRICS') or '').lower().strip() in {'1','true','yes'}

if target_csv:
    targets = [row.strip().lower() for row in target_csv.split(',') if row.strip()]
else:
    target = (os.environ.get('DEVICE_ID') or '').strip().lower()
    if not run_pi:
        target = (os.environ.get('DEVICE_MODEL') or '').strip().lower()
    targets = [target] if target else []

def do_query(url: str, q: str):
    resp = requests.get(url, params={'query': q}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get('data', {}).get('result', [])

metric_keys = [
    'wifi_ap_total',
    'wifi_avg_rssi_dbm',
    'wifi_link_quality',
    'wifi_signal_dbm',
    'wifi_tx_bitrate_mbps',
    'wifi_rx_bitrate_mbps',
    'wifi_channel',
    'wifi_freq_mhz',
    'wifi_if_rx_bytes',
    'wifi_if_tx_bytes',
    'wifi_sampling_points_applied',
    'ble_adv_total',
    'ble_avg_rssi_dbm',
    'ble_scan_ok',
    'csi_frames_total',
    'csi_collector_configured',
    'csi_capture_ok',
    'csi_capture_disabled',
    'device_cpu_temp_c',
    'device_load1',
]

for target in targets:
    print('target_device', target)
    for metric in metric_keys:
        q = f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"{metric}\"}}'
        pres = do_query('http://prometheus:9090/api/v1/query', q)
        print('prometheus', q, '=>', len(pres), pres[:1])
        mres = do_query('http://mimir:9009/prometheus/api/v1/query', q)
        print('mimir     ', q, '=>', len(mres), mres[:1])

if inject_hypothetical:
    q = 'monad_fleet_metric_value{device_id=\"lowlevel-board-01\"}'
    pres = do_query('http://prometheus:9090/api/v1/query', q)
    print('prometheus', q, '=>', len(pres), pres[:1])
    mres = do_query('http://mimir:9009/prometheus/api/v1/query', q)
    print('mimir     ', q, '=>', len(mres), mres[:1])
PY
"

echo "[8b/8] Verify artifact uploads in eLabFTW experiment"
run_elab_upload_check() {
docker compose exec -T monad-fleet-service sh -lc "EXPERIMENT_ID='${SMOKE_EXPERIMENT_ID}' REAL_DATA_ENFORCE='${REAL_DATA_ENFORCE}' REQUIRE_REAL_WIFI='${REQUIRE_REAL_WIFI}' REQUIRE_REAL_BLE='${REQUIRE_REAL_BLE}' REQUIRE_REAL_CSI='${REQUIRE_REAL_CSI}' python - <<'PY'
import os
import requests
import urllib3

urllib3.disable_warnings()
base = os.environ.get('ELAB_BASE_URL', 'https://web/api/v2').rstrip('/')
key = os.environ.get('ELAB_API_KEY', '')
exp_id = int(os.environ.get('EXPERIMENT_ID', '0') or 0)
real_data_enforce = (os.environ.get('REAL_DATA_ENFORCE') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
require_real_wifi = (os.environ.get('REQUIRE_REAL_WIFI') or '').strip().lower() in {'1', 'true', 'yes', 'on'} if os.environ.get('REQUIRE_REAL_WIFI') else real_data_enforce
require_real_ble = (os.environ.get('REQUIRE_REAL_BLE') or '').strip().lower() in {'1', 'true', 'yes', 'on'} if os.environ.get('REQUIRE_REAL_BLE') else real_data_enforce
require_real_csi = (os.environ.get('REQUIRE_REAL_CSI') or '').strip().lower() in {'1', 'true', 'yes', 'on'} if os.environ.get('REQUIRE_REAL_CSI') else real_data_enforce

resp = requests.get(
    f'{base}/experiments/{exp_id}/uploads',
    headers={'Authorization': key},
    verify=False,
    timeout=20,
)
resp.raise_for_status()
rows = resp.json() if isinstance(resp.json(), list) else []
print('elab_uploads_total', len(rows))
for row in rows[:12]:
    print('upload', row.get('id'), row.get('real_name'), row.get('comment', '')[:80])

name_tokens = [(str(row.get('real_name') or '').lower(), str(row.get('comment') or '').lower()) for row in rows]
has_wifi = any(('wifi' in name) or ('artifact=wifi' in comment) for name, comment in name_tokens)
has_ble = any(('ble' in name) or ('artifact=ble' in comment) for name, comment in name_tokens)
has_csi = any(('csi' in name) or ('artifact=csi' in comment) for name, comment in name_tokens)
has_summary = any(('run-summary' in name) or ('artifact=run-summary' in comment) for name, comment in name_tokens)
print('upload_classes', {'wifi': has_wifi, 'ble': has_ble, 'csi': has_csi, 'run_summary': has_summary})

failures = []
if require_real_wifi and not has_wifi:
    failures.append('wifi upload artifact missing')
if require_real_ble and not has_ble:
    failures.append('ble upload artifact missing')
if require_real_csi and not has_csi:
    failures.append('csi upload artifact missing')
if not has_summary:
    failures.append('run-summary upload artifact missing')

if failures:
    raise SystemExit('elab_upload_check_failed: ' + '; '.join(failures))
PY
"
}

if [[ "${RUN_PI}" == "true" && "${RUN_PI_PASSIVE}" == "true" ]]; then
  echo "      Passive async verify enabled (max_wait=${PASSIVE_VERIFY_MAX_WAIT_S}s, poll=${PASSIVE_VERIFY_POLL_S}s)"
  deadline_epoch=$(( $(date +%s) + PASSIVE_VERIFY_MAX_WAIT_S ))
  attempt=0
  while true; do
    attempt=$((attempt + 1))
    echo "  -> elab upload check attempt=${attempt}"
    if run_elab_upload_check; then
      break
    fi
    now_epoch=$(date +%s)
    if (( now_epoch >= deadline_epoch )); then
      echo "ERROR: passive async eLab upload verification timed out after ${PASSIVE_VERIFY_MAX_WAIT_S}s"
      exit 1
    fi
    sleep "${PASSIVE_VERIFY_POLL_S}"
  done
else
  run_elab_upload_check
fi

if [[ "${RUN_PI}" == "true" ]]; then
  echo "[8c/8] Verify latest Pi run report(s) for this experiment (real-data gates)"
  run_real_verify_for_host() {
    local host="$1"
    REAL_DATA_ENFORCE="${REAL_DATA_ENFORCE}" \
    REQUIRE_REAL_WIFI="${REQUIRE_REAL_WIFI}" \
    REQUIRE_WIFI_CONNECTED="${REQUIRE_WIFI_CONNECTED}" \
    REQUIRE_REAL_BLE="${REQUIRE_REAL_BLE}" \
    REQUIRE_REAL_CSI="${REQUIRE_REAL_CSI}" \
    REQUIRE_CSI_FRAMES_MIN="${REQUIRE_CSI_FRAMES_MIN}" \
    SMOKE_EXPERIMENT_ID="${SMOKE_EXPERIMENT_ID}" \
    scripts/rpi/verify_real_run.sh "${host}"
  }
  for host in "${PI_HOSTS_LIST[@]}"; do
    if [[ "${RUN_PI_PASSIVE}" == "true" ]]; then
      deadline_epoch=$(( $(date +%s) + PASSIVE_VERIFY_MAX_WAIT_S ))
      attempt=0
      while true; do
        attempt=$((attempt + 1))
        echo "  -> verify host=${host} attempt=${attempt}"
        if run_real_verify_for_host "${host}"; then
          break
        fi
        now_epoch=$(date +%s)
        if (( now_epoch >= deadline_epoch )); then
          echo "ERROR: passive async real-data verification timed out host=${host} after ${PASSIVE_VERIFY_MAX_WAIT_S}s"
          exit 1
        fi
        sleep "${PASSIVE_VERIFY_POLL_S}"
      done
    else
      echo "  -> verify host=${host}"
      run_real_verify_for_host "${host}"
    fi
  done
fi

SMOKE_LAST_STEP="8_done"
echo "Smoke test completed."
echo "eLabFTW smoke experiment id: ${SMOKE_EXPERIMENT_ID}"
echo "Grafana: http://localhost:3000 (admin/admin)"
echo "Prometheus: http://localhost:9090"
echo "Mimir API: http://localhost:9009"

if [[ "${CLEANUP}" == "true" ]]; then
  echo "Cleanup enabled; deleting smoke experiment id=${SMOKE_EXPERIMENT_ID}"
  docker compose exec -T monad-fleet-service sh -lc "EXPERIMENT_ID='${SMOKE_EXPERIMENT_ID}' python - <<'PY'
import os
import requests
import urllib3

urllib3.disable_warnings()
base = os.environ.get('ELAB_BASE_URL', 'https://web/api/v2').rstrip('/')
key = os.environ.get('ELAB_API_KEY', '')
exp_id = int(os.environ.get('EXPERIMENT_ID', '0') or 0)
resp = requests.delete('%s/experiments/%d' % (base, exp_id), headers={'Authorization': key}, verify=False, timeout=20)
resp.raise_for_status()
print('deleted', exp_id)
PY"
fi
