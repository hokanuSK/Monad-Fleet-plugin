#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

DEFAULT_FLEET_MANAGER_HOST="$(bash "${ROOT_DIR}/scripts/rpi/default_fleet_manager_host.sh")"
DEFAULT_FLEET_MANAGER_PORT="$(bash "${ROOT_DIR}/scripts/rpi/default_fleet_manager_port.sh")"

PI_HOST="${PI_HOST:-192.168.0.212}"
PI_USER="${PI_USER:-admin}"
AWS_HOST="${AWS_HOST:-16.171.70.171}"
AWS_USER="${AWS_USER:-ubuntu}"
AWS_KEY_PATH="${AWS_KEY_PATH:-/private/tmp/fleetmanager-key-20260419205532.pem}"
AWS_FLEET_CONTAINER="${AWS_FLEET_CONTAINER:-elabftw-aws-monad-fleet-service-1}"
FLEET_MANAGER_HOST="${FLEET_MANAGER_HOST:-${DEFAULT_FLEET_MANAGER_HOST}}"
FLEET_MANAGER_PORT="${FLEET_MANAGER_PORT:-${DEFAULT_FLEET_MANAGER_PORT}}"
GRPC_DNS_RESOLVER="${GRPC_DNS_RESOLVER:-native}"
CONTROL_PLANE_IFACE="${CONTROL_PLANE_IFACE:-wlan0}"
WIFI_SCAN_IFACE="${WIFI_SCAN_IFACE:-${CONTROL_PLANE_IFACE}}"
WIFI_DURATION_S="${WIFI_DURATION_S:-20}"
WIFI_INTERVAL_S="${WIFI_INTERVAL_S:-5}"
BLE_TIMEOUT_S="${BLE_TIMEOUT_S:-15}"
WINDOW_MINUTES="${WINDOW_MINUTES:-20}"
SYSTEMD_AGENT_SERVICE="${SYSTEMD_AGENT_SERVICE:-monad-fleet-agent.service}"
TEST_TAGS_CSV="${TEST_TAGS_CSV:-fleet,smoke:v3,no-csi,relay:mdns,created-by:codex}"
TEST_TITLE_PREFIX="${TEST_TITLE_PREFIX:-Fleet Smoke v3 AWS relay no-CSI}"
TEST_BODY="${TEST_BODY:-AWS relay smoke via MacBook-Pro.local with WiFi+BLE only.}"
OPS_OUTPUT_DIR="${OPS_OUTPUT_DIR:-${ROOT_DIR}/artifacts/output/ops}"
SMOKE_LOG_DIR="${SMOKE_LOG_DIR:-${ROOT_DIR}/artifacts/output/smoke_seq}"
TIMESTAMP_UTC="$(date -u +%Y%m%dT%H%M%SZ)"
PI_SMOKE_DATA_ROOT="${PI_SMOKE_DATA_ROOT:-/home/${PI_USER}/monad-fleet-agent/smoke-data/${TIMESTAMP_UTC}}"
RUN_LOG_PATH="${SMOKE_LOG_DIR}/${TIMESTAMP_UTC}-pi-aws-relay-no-csi-v3.log"
PI_SMOKE_CAPTURE_PATH="${SMOKE_LOG_DIR}/${TIMESTAMP_UTC}-pi-smoke-output.log"
PI_VERIFY_CAPTURE_PATH="${SMOKE_LOG_DIR}/${TIMESTAMP_UTC}-pi-verify-output.log"
OPS_CONTEXT_PATH="${OPS_OUTPUT_DIR}/aws_pi_relay_no_csi_v3_${TIMESTAMP_UTC}.json"
OPS_CONTEXT_LATEST="${OPS_OUTPUT_DIR}/latest_run_context.json"

SSH_PI_ARGS=(
  -o ConnectTimeout=10
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
)
SSH_AWS_ARGS=(
  -o ConnectTimeout=10
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  -i "${AWS_KEY_PATH}"
)

mkdir -p "${OPS_OUTPUT_DIR}" "${SMOKE_LOG_DIR}"

PI_SERVICE_WAS_ACTIVE="false"

run_pi_ssh() {
  ssh "${SSH_PI_ARGS[@]}" "${PI_USER}@${PI_HOST}" "$@"
}

run_aws_ssh() {
  ssh "${SSH_AWS_ARGS[@]}" "${AWS_USER}@${AWS_HOST}" "$@"
}

require_local_listener() {
  local port="$1"
  if ! lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: expected local relay listener on TCP ${port}" >&2
    exit 2
  fi
}

uses_local_relay_target() {
  case "${FLEET_MANAGER_HOST}" in
    localhost|127.0.0.1|MacBook-Pro.local)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

ensure_local_listener() {
  local local_port="$1"
  local remote_port="$2"
  if lsof -nP -iTCP:"${local_port}" -sTCP:LISTEN >/dev/null 2>&1; then
    return 0
  fi
  echo "Opening local relay listener TCP ${local_port} -> ${AWS_HOST}:${remote_port}"
  ssh \
    "${SSH_AWS_ARGS[@]}" \
    -f -N -g \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -L "0.0.0.0:${local_port}:127.0.0.1:${remote_port}" \
    "${AWS_USER}@${AWS_HOST}"
  sleep 1
  require_local_listener "${local_port}"
}

require_remote_access() {
  run_pi_ssh "echo PI_SSH_OK >/dev/null"
  run_aws_ssh "sudo docker ps --format '{{.Names}}' | grep -Fx '${AWS_FLEET_CONTAINER}' >/dev/null"
}

prepare_pi_service_for_manual_smoke() {
  local state=""
  state="$(run_pi_ssh "bash -lc 'systemctl is-active \"${SYSTEMD_AGENT_SERVICE}\" 2>/dev/null || true'" || true)"
  state="${state//$'\r'/}"
  state="${state//$'\n'/}"
  case "${state}" in
    active|activating|reloading)
      PI_SERVICE_WAS_ACTIVE="true"
      echo "Stopping ${SYSTEMD_AGENT_SERVICE} on ${PI_HOST} before creating the experiment..."
      run_pi_ssh "bash -lc 'sudo -n systemctl stop \"${SYSTEMD_AGENT_SERVICE}\" || true; sudo -n systemctl reset-failed \"${SYSTEMD_AGENT_SERVICE}\" || true'"
      ;;
    *)
      PI_SERVICE_WAS_ACTIVE="false"
      ;;
  esac
}

restore_pi_service_after_manual_smoke() {
  if [[ "${PI_SERVICE_WAS_ACTIVE}" != "true" ]]; then
    return 0
  fi
  echo "Restoring ${SYSTEMD_AGENT_SERVICE} on ${PI_HOST}..."
  run_pi_ssh "bash -lc 'sudo -n systemctl start \"${SYSTEMD_AGENT_SERVICE}\" || true'" || true
  PI_SERVICE_WAS_ACTIVE="false"
}

resolve_agent_id() {
  run_pi_ssh "cat /sys/class/net/${CONTROL_PLANE_IFACE}/address" | tr -d '\r\n' | tr '[:upper:]' '[:lower:]'
}

aws_fleet_python() {
  local remote_cmd="$1"
  shift
  run_aws_ssh "${remote_cmd}" "$@"
}

create_experiment() {
  local agent_id="$1"
  local range_from range_to title
  range_from="$(date -u -v-1M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || python3 - <<'PY'
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ'))
PY
)"
  range_to="$(date -u -v+"${WINDOW_MINUTES}"M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || python3 - "${WINDOW_MINUTES}" <<'PY'
from datetime import datetime, timedelta, timezone
import sys
minutes = int(sys.argv[1])
print((datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%SZ'))
PY
)"
  title="${TEST_TITLE_PREFIX} ${TIMESTAMP_UTC}"

  local remote_cmd
  printf -v remote_cmd \
    "sudo docker exec -e AGENT_ID=%q -e TEST_TITLE=%q -e TEST_BODY=%q -e TAGS_CSV=%q -e RANGE_FROM=%q -e RANGE_TO=%q -e WIFI_DURATION_S=%q -e WIFI_INTERVAL_S=%q -e BLE_TIMEOUT_S=%q -i %q python -" \
    "${agent_id}" "${title}" "${TEST_BODY}" "${TEST_TAGS_CSV}" "${range_from}" "${range_to}" "${WIFI_DURATION_S}" "${WIFI_INTERVAL_S}" "${BLE_TIMEOUT_S}" "${AWS_FLEET_CONTAINER}"

  aws_fleet_python "${remote_cmd}" <<'PY'
import json
import os
import re
import requests
import urllib3

urllib3.disable_warnings()

base = os.environ["ELAB_BASE_URL"].rstrip("/")
key = os.environ["ELAB_API_KEY"]
headers = {"Authorization": key}

agent_id = os.environ["AGENT_ID"].strip().lower()
title = os.environ["TEST_TITLE"].strip()
body = os.environ["TEST_BODY"].strip()
tags = [row.strip() for row in os.environ["TAGS_CSV"].split(",") if row.strip()]
range_from = os.environ["RANGE_FROM"].strip()
range_to = os.environ["RANGE_TO"].strip()
wifi_duration_s = int(os.environ["WIFI_DURATION_S"].strip())
wifi_interval_s = int(os.environ["WIFI_INTERVAL_S"].strip())
ble_timeout_s = int(os.environ["BLE_TIMEOUT_S"].strip())

policy = {
    "range": {
        "from": range_from,
        "to": range_to,
    },
    "target_selector": {
        "device_ids": [agent_id],
    },
    "command_groups": [
        {
            "id": "v3-no-csi-aws-relay",
            "name": "v3 no-csi aws relay",
            "failure_mode": "FAIL_FAST",
            "commands": [
                {
                    "id": "wifi-scan-series",
                    "type": "WIFI_SCAN",
                    "timeout_s": max(10, wifi_duration_s + 5),
                    "retries": 0,
                    "env": {
                        "DISABLE_CSI_CAPTURE": "true",
                        "WIFI_SAMPLE_EMIT_HTTP": "1",
                        "WIFI_SAMPLE_DURATION_S": str(wifi_duration_s),
                        "WIFI_SAMPLE_INTERVAL_S": str(wifi_interval_s),
                        "WIFI_SAMPLE_ARTIFACT_STRIDE": "0",
                    },
                },
                {
                    "id": "ble-scan",
                    "type": "BLE_SCAN",
                    "timeout_s": ble_timeout_s,
                    "retries": 0,
                    "env": {
                        "DISABLE_CSI_CAPTURE": "true",
                    },
                },
            ],
        }
    ],
}

create_resp = requests.post(
    base + "/experiments",
    headers=headers,
    json={
        "title": title,
        "body": body,
        "metadata": "{}",
    },
    verify=False,
    timeout=20,
)
create_resp.raise_for_status()
location = create_resp.headers.get("location", "")
match = re.search(r"/experiments/(\d+)$", location)
if not match:
    raise SystemExit(f"could not parse experiment id from location={location!r}")
experiment_id = int(match.group(1))

patch_resp = requests.patch(
    base + f"/experiments/{experiment_id}",
    headers=headers,
    json={"metadata": json.dumps({"fleet": {"policy": policy}}, sort_keys=True)},
    verify=False,
    timeout=20,
)
patch_resp.raise_for_status()

for tag in tags:
    tag_resp = requests.post(
        base + f"/experiments/{experiment_id}/tags",
        headers=headers,
        json={"tag": tag},
        verify=False,
        timeout=20,
    )
    tag_resp.raise_for_status()

exp_resp = requests.get(
    base + f"/experiments/{experiment_id}",
    headers=headers,
    verify=False,
    timeout=20,
)
exp_resp.raise_for_status()
experiment = exp_resp.json()

print(
    json.dumps(
        {
            "experiment_id": experiment_id,
            "sharelink": experiment.get("sharelink", ""),
            "title": experiment.get("title", ""),
            "range_from": range_from,
            "range_to": range_to,
            "agent_id": agent_id,
            "tags": tags,
        },
        sort_keys=True,
    )
)
PY
}

verify_aws_uploads() {
  local experiment_id="$1"
  local remote_cmd
  printf -v remote_cmd \
    "sudo docker exec -e EXPERIMENT_ID=%q -i %q python -" \
    "${experiment_id}" "${AWS_FLEET_CONTAINER}"

  aws_fleet_python "${remote_cmd}" <<'PY'
import json
import os
import re
import requests
import urllib3

urllib3.disable_warnings()

base = os.environ["ELAB_BASE_URL"].rstrip("/")
key = os.environ["ELAB_API_KEY"]
headers = {"Authorization": key}
experiment_id = int(os.environ["EXPERIMENT_ID"])

resp = requests.get(
    base + f"/experiments/{experiment_id}/uploads",
    headers=headers,
    verify=False,
    timeout=20,
)
resp.raise_for_status()
rows = resp.json() if isinstance(resp.json(), list) else []

name_tokens = [(str(row.get("real_name") or "").lower(), str(row.get("comment") or "").lower()) for row in rows]
has_wifi = any(("wifi" in name) or ("artifact=wifi" in comment) for name, comment in name_tokens)
has_ble = any(("ble" in name) or ("artifact=ble" in comment) for name, comment in name_tokens)
has_summary = any(("run-summary" in name) or ("artifact=run-summary" in comment) for name, comment in name_tokens)
has_csi = any(("csi" in name) or ("artifact=csi" in comment) for name, comment in name_tokens)

run_ids = []
for _, comment in name_tokens:
    match = re.search(r"run=([0-9a-f-]{36})", comment)
    if match:
        run_ids.append(match.group(1))

result = {
    "experiment_id": experiment_id,
    "uploads_total": len(rows),
    "has_wifi": has_wifi,
    "has_ble": has_ble,
    "has_run_summary": has_summary,
    "has_csi": has_csi,
    "run_ids": sorted(set(run_ids)),
    "sample_uploads": [str(row.get("real_name") or "") for row in rows[:12]],
}
print(json.dumps(result, sort_keys=True))

failures = []
if not has_wifi:
    failures.append("wifi upload artifact missing")
if not has_ble:
    failures.append("ble upload artifact missing")
if not has_summary:
    failures.append("run-summary upload artifact missing")
if has_csi:
    failures.append("unexpected csi upload present in no-csi smoke")

if failures:
    raise SystemExit("aws_upload_check_failed: " + "; ".join(failures))
PY
}

clear_experiment_policy() {
  local experiment_id="$1"
  local remote_cmd
  printf -v remote_cmd \
    "sudo docker exec -e EXPERIMENT_ID=%q -i %q python -" \
    "${experiment_id}" "${AWS_FLEET_CONTAINER}"

  aws_fleet_python "${remote_cmd}" <<'PY'
import json
import os
import requests
import urllib3

urllib3.disable_warnings()

base = os.environ["ELAB_BASE_URL"].rstrip("/")
key = os.environ["ELAB_API_KEY"]
headers = {"Authorization": key}
experiment_id = int(os.environ["EXPERIMENT_ID"])

resp = requests.patch(
    base + f"/experiments/{experiment_id}",
    headers=headers,
    json={"metadata": json.dumps({"fleet": {}}, sort_keys=True)},
    verify=False,
    timeout=20,
)
resp.raise_for_status()
print(json.dumps({"experiment_id": experiment_id, "policy_cleared": True}, sort_keys=True))
PY
}

write_ops_context() {
  local experiment_id="$1"
  local pi_run_id="$2"
  local aws_run_id="$3"
  local result="$4"
  local last_step="$5"
  local device_id="$6"

  CONTEXT_EXPERIMENT_ID="${experiment_id}" \
  CONTEXT_PI_RUN_ID="${pi_run_id}" \
  CONTEXT_AWS_RUN_ID="${aws_run_id}" \
  CONTEXT_RESULT="${result}" \
  CONTEXT_LAST_STEP="${last_step}" \
  CONTEXT_DEVICE_ID="${device_id}" \
  CONTEXT_PI_HOST="${PI_HOST}" \
  CONTEXT_STARTED_AT="${STARTED_AT_UTC}" \
  CONTEXT_GENERATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  CONTEXT_PATH="${OPS_CONTEXT_PATH}" \
  CONTEXT_LATEST_PATH="${OPS_CONTEXT_LATEST}" \
  python3 - <<'PY'
import json
import os

payload = {
    "cleanup": "false",
    "device_model": os.environ["CONTEXT_DEVICE_ID"],
    "device_pi": os.environ["CONTEXT_PI_HOST"],
    "generated_at_utc": os.environ["CONTEXT_GENERATED_AT"],
    "last_command": "scripts/smoke/pi_aws_relay_no_csi_v3.sh",
    "last_step": os.environ["CONTEXT_LAST_STEP"],
    "real_data_enforce": "true",
    "result": os.environ["CONTEXT_RESULT"],
    "run_id": os.environ["CONTEXT_PI_RUN_ID"] or os.environ["CONTEXT_AWS_RUN_ID"],
    "run_pi": "true",
    "run_pi_passive": "false",
    "smoke_experiment_id": os.environ["CONTEXT_EXPERIMENT_ID"],
    "smoke_profile": "aws-relay-no-csi",
    "started_at_utc": os.environ["CONTEXT_STARTED_AT"],
    "target_device_ids_csv": os.environ["CONTEXT_DEVICE_ID"],
    "aws_run_id": os.environ["CONTEXT_AWS_RUN_ID"],
    "pi_run_id": os.environ["CONTEXT_PI_RUN_ID"],
}
for path_key in ("CONTEXT_PATH", "CONTEXT_LATEST_PATH"):
    with open(os.environ[path_key], "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
PY
}

STARTED_AT_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "Smoke: AWS relay no-CSI v3"
echo "Pi host: ${PI_USER}@${PI_HOST}"
echo "AWS host: ${AWS_USER}@${AWS_HOST}"
echo "Relay target: ${FLEET_MANAGER_HOST}:${FLEET_MANAGER_PORT}"
echo "Run log: ${RUN_LOG_PATH}"

if uses_local_relay_target; then
  ensure_local_listener "${FLEET_MANAGER_PORT}" 50060
  ensure_local_listener 9108 9108
fi
require_remote_access
trap restore_pi_service_after_manual_smoke EXIT
prepare_pi_service_for_manual_smoke

AGENT_ID="${AGENT_ID:-$(resolve_agent_id)}"
if [[ -z "${AGENT_ID}" ]]; then
  echo "ERROR: failed to resolve AGENT_ID from ${PI_HOST}" >&2
  exit 2
fi
echo "Agent ID: ${AGENT_ID}"

EXPERIMENT_JSON="$(create_experiment "${AGENT_ID}")"
echo "${EXPERIMENT_JSON}" | tee -a "${RUN_LOG_PATH}"
SMOKE_EXPERIMENT_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["experiment_id"])' <<<"${EXPERIMENT_JSON}")"
SMOKE_SHARELINK="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("sharelink",""))' <<<"${EXPERIMENT_JSON}")"

echo "Created experiment: ${SMOKE_EXPERIMENT_ID}"
echo "Sharelink: ${SMOKE_SHARELINK}"

set -o pipefail
PI_HOST="${PI_HOST}" \
PI_USER="${PI_USER}" \
FLEET_MANAGER_HOST="${FLEET_MANAGER_HOST}" \
FLEET_MANAGER_PORT="${FLEET_MANAGER_PORT}" \
GRPC_DNS_RESOLVER="${GRPC_DNS_RESOLVER}" \
DATA_ROOT="${PI_SMOKE_DATA_ROOT}" \
AGENT_ID="${AGENT_ID}" \
CONTROL_PLANE_IFACE="${CONTROL_PLANE_IFACE}" \
WIFI_SCAN_IFACE="${WIFI_SCAN_IFACE}" \
MANAGE_SYSTEMD_AGENT=false \
MAX_SYNC_CYCLES=1 \
EXECUTE_POLICY=true \
DISABLE_CSI_CAPTURE=true \
REQUIRE_REAL_CSI=false \
ENABLE_ELAB_ARTIFACT_UPLOAD=true \
ARTIFACT_UPLOAD_TARGET=elabftw \
ARTIFACT_UPLOAD_DURING_MEASURE=true \
POST_RUN_AUDIT=true \
"${ROOT_DIR}/scripts/rpi/smoke_test_agent.sh" "${PI_HOST}" 2>&1 | tee -a "${RUN_LOG_PATH}" "${PI_SMOKE_CAPTURE_PATH}" >/dev/null
set +o pipefail

echo "Pi smoke run completed."

set -o pipefail
PI_HOST="${PI_HOST}" \
PI_USER="${PI_USER}" \
SMOKE_EXPERIMENT_ID="${SMOKE_EXPERIMENT_ID}" \
DATA_ROOT="${PI_SMOKE_DATA_ROOT}" \
REAL_DATA_ENFORCE=true \
REQUIRE_REAL_WIFI=true \
REQUIRE_REAL_BLE=true \
REQUIRE_REAL_CSI=false \
"${ROOT_DIR}/scripts/rpi/verify_real_run.sh" "${PI_HOST}" 2>&1 | tee -a "${RUN_LOG_PATH}" "${PI_VERIFY_CAPTURE_PATH}" >/dev/null
set +o pipefail

PI_RUN_ID="$(sed -n 's/^RUN_ID=//p' "${PI_VERIFY_CAPTURE_PATH}" | tail -n1)"

AWS_VERIFY_JSON="$(verify_aws_uploads "${SMOKE_EXPERIMENT_ID}")"
echo "${AWS_VERIFY_JSON}" | tee -a "${RUN_LOG_PATH}"
AWS_RUN_ID="$(python3 -c 'import json,sys; rows=json.load(sys.stdin).get("run_ids", []); print(rows[-1] if rows else "")' <<<"${AWS_VERIFY_JSON}")"

RESULT="pass"
LAST_STEP="verify_aws_uploads"
if [[ -n "${PI_RUN_ID}" && -n "${AWS_RUN_ID}" && "${PI_RUN_ID}" != "${AWS_RUN_ID}" ]]; then
  echo "ERROR: Pi run_id (${PI_RUN_ID}) does not match AWS upload run_id (${AWS_RUN_ID})" | tee -a "${RUN_LOG_PATH}" >&2
  RESULT="fail"
  LAST_STEP="run_id_mismatch"
fi

if [[ "${RESULT}" == "pass" ]]; then
  CLEAR_POLICY_JSON="$(clear_experiment_policy "${SMOKE_EXPERIMENT_ID}")"
  echo "${CLEAR_POLICY_JSON}" | tee -a "${RUN_LOG_PATH}"
  LAST_STEP="clear_experiment_policy"
fi

write_ops_context "${SMOKE_EXPERIMENT_ID}" "${PI_RUN_ID}" "${AWS_RUN_ID}" "${RESULT}" "${LAST_STEP}" "${AGENT_ID}"

if [[ "${RESULT}" != "pass" ]]; then
  exit 3
fi

echo "Smoke PASS: experiment=${SMOKE_EXPERIMENT_ID} run_id=${PI_RUN_ID:-${AWS_RUN_ID}}"
