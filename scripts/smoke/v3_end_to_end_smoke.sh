#!/usr/bin/env bash
set -euo pipefail

DEVICE_MODEL="${DEVICE_MODEL:-02:42:ac:14:00:04}"
DEVICE_PI="${DEVICE_PI:-2c:cf:67:80:f5:d9}"
MAX_SYNC_CYCLES="${MAX_SYNC_CYCLES:-1}"

RESET_STATE="${RESET_STATE:-true}"
RESET_METRICS="${RESET_METRICS:-true}"
RESET_GRAFANA="${RESET_GRAFANA:-false}"
CLEANUP="${CLEANUP:-false}"
DO_BUILD="${DO_BUILD:-false}"

RUN_PI="${RUN_PI:-false}"
PI_HOST="${PI_HOST:-monad-rpi5.local}"
ALLOW_WIFI_DISRUPTIVE_CSI="${ALLOW_WIFI_DISRUPTIVE_CSI:-false}"

SMOKE_TITLE_PREFIX="${SMOKE_TITLE_PREFIX:-Fleet Smoke v3}"
SMOKE_TAGS_CSV="${SMOKE_TAGS_CSV:-fleet,smoke:v3,created-by:monad-fleet}"
SMOKE_PROFILE="${SMOKE_PROFILE:-full}"     # full|rssi|wireless_spec
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
REPORT_RPC_TIMEOUT_S="${REPORT_RPC_TIMEOUT_S:-120}"
CONTROL_RPC_TIMEOUT_S="${CONTROL_RPC_TIMEOUT_S:-30}"
ENABLE_ELAB_ARTIFACT_UPLOAD="${ENABLE_ELAB_ARTIFACT_UPLOAD:-true}"
ARTIFACT_UPLOAD_MAX_BYTES="${ARTIFACT_UPLOAD_MAX_BYTES:-20971520}"
INJECT_HYPOTHETICAL_METRICS="${INJECT_HYPOTHETICAL_METRICS:-false}"
CSI_COLLECTOR_CMD="${CSI_COLLECTOR_CMD:-}"
CSI_OUTPUT_PATH="${CSI_OUTPUT_PATH:-}"
CSI_OUTPUT_GLOB="${CSI_OUTPUT_GLOB:-}"
CSI_FRAMES_REGEX="${CSI_FRAMES_REGEX:-}"
CSI_OUTPUT_MAX_FILES="${CSI_OUTPUT_MAX_FILES:-}"
CSI_PARSE_MAX_BYTES="${CSI_PARSE_MAX_BYTES:-}"
REAL_DATA_ENFORCE="${REAL_DATA_ENFORCE:-}"
REQUIRE_REAL_WIFI="${REQUIRE_REAL_WIFI:-}"
REQUIRE_REAL_BLE="${REQUIRE_REAL_BLE:-}"
REQUIRE_REAL_CSI="${REQUIRE_REAL_CSI:-}"
REQUIRE_CSI_FRAMES_MIN="${REQUIRE_CSI_FRAMES_MIN:-1}"

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

if [[ "${REAL_DATA_ENFORCE}" == "true" && "${REQUIRE_REAL_CSI}" == "true" && -z "${CSI_COLLECTOR_CMD}" ]]; then
  echo "ERROR: REAL_DATA_ENFORCE=true and REQUIRE_REAL_CSI=true but CSI_COLLECTOR_CMD is empty."
  echo "Set CSI_COLLECTOR_CMD (and optional CSI_OUTPUT_PATH/CSI_OUTPUT_GLOB/CSI_FRAMES_REGEX) for real CSI runs."
  exit 1
fi

echo "[1/8] Restart core services"
if [[ "${DO_BUILD}" == "true" ]]; then
  echo "Building monad-fleet-service + model-device (DO_BUILD=true)"
  docker compose build monad-fleet-service model-device
else
  echo "Skipping build (DO_BUILD=false)"
fi
docker compose up -d --force-recreate --no-build monad-fleet-service model-device prometheus mimir grafana

if [[ "${RESET_STATE}" == "true" ]]; then
  echo "[2/8] Reset Fleet service state (dedupe + ingest journal) only"
  docker compose exec -T monad-fleet-service sh -lc "rm -f /data/state.json /data/ingest-metrics.ndjson || true"
  docker compose restart monad-fleet-service >/dev/null
else
  echo "[2/8] Skip Fleet state reset (RESET_STATE=${RESET_STATE})"
fi

if [[ "${RESET_METRICS}" == "true" ]]; then
  echo "[3/8] Reset Prometheus+Mimir data only (does not touch eLabFTW/MySQL)"
  docker compose exec -T prometheus sh -lc "rm -rf /prometheus/* || true"
  docker compose exec -T mimir sh -lc "rm -rf /data/* || true"
  docker compose restart prometheus mimir >/dev/null
else
  echo "[3/8] Skip metrics reset (RESET_METRICS=${RESET_METRICS})"
fi

if [[ "${RESET_GRAFANA}" == "true" ]]; then
  echo "[3b/8] Reset Grafana local data (dashboards/users are wiped; metrics live in Mimir)"
  docker compose exec -T grafana sh -lc "rm -rf /var/lib/grafana/* || true"
  docker compose restart grafana >/dev/null
else
  echo "[3b/8] Skip Grafana reset (RESET_GRAFANA=${RESET_GRAFANA})"
fi

echo "[4/8] Create a new eLabFTW smoke experiment policy (profile=${SMOKE_PROFILE}; no existing experiments modified)"
SMOKE_EXPERIMENT_ID="$(docker compose exec -T monad-fleet-service sh -lc "DEVICE_MODEL='${DEVICE_MODEL}' DEVICE_PI='${DEVICE_PI}' RUN_PI='${RUN_PI}' SMOKE_TITLE_PREFIX='${SMOKE_TITLE_PREFIX}' SMOKE_TAGS_CSV='${SMOKE_TAGS_CSV}' SMOKE_PROFILE='${SMOKE_PROFILE}' RSSI_DURATION_S='${RSSI_DURATION_S}' RSSI_INTERVAL_S='${RSSI_INTERVAL_S}' RSSI_INTERVAL_S_PI='${RSSI_INTERVAL_S_PI}' RSSI_ARTIFACT_STRIDE='${RSSI_ARTIFACT_STRIDE}' FULL_DURATION_S='${FULL_DURATION_S}' FULL_INTERVAL_S='${FULL_INTERVAL_S}' FULL_INTERVAL_S_PI='${FULL_INTERVAL_S_PI}' FULL_ARTIFACT_STRIDE='${FULL_ARTIFACT_STRIDE}' FULL_BLE_TIMEOUT_S='${FULL_BLE_TIMEOUT_S}' FULL_CSI_TIMEOUT_S='${FULL_CSI_TIMEOUT_S}' WIRELESS_SPEC_DURATION_S='${WIRELESS_SPEC_DURATION_S}' WIRELESS_SPEC_INTERVAL_S='${WIRELESS_SPEC_INTERVAL_S}' WIRELESS_SPEC_INTERVAL_S_PI='${WIRELESS_SPEC_INTERVAL_S_PI}' WIRELESS_SPEC_ARTIFACT_STRIDE='${WIRELESS_SPEC_ARTIFACT_STRIDE}' WIRELESS_SPEC_BLE_TIMEOUT_S='${WIRELESS_SPEC_BLE_TIMEOUT_S}' WIRELESS_SPEC_CSI_TIMEOUT_S='${WIRELESS_SPEC_CSI_TIMEOUT_S}' WIRELESS_SPEC_ENV_SNAPSHOT='${WIRELESS_SPEC_ENV_SNAPSHOT}' CSI_COLLECTOR_CMD='${CSI_COLLECTOR_CMD}' CSI_OUTPUT_PATH='${CSI_OUTPUT_PATH}' CSI_OUTPUT_GLOB='${CSI_OUTPUT_GLOB}' CSI_FRAMES_REGEX='${CSI_FRAMES_REGEX}' CSI_OUTPUT_MAX_FILES='${CSI_OUTPUT_MAX_FILES}' CSI_PARSE_MAX_BYTES='${CSI_PARSE_MAX_BYTES}' python - <<'PY'
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
csi_collector_cmd = (os.environ.get('CSI_COLLECTOR_CMD') or '').strip()
csi_output_path = (os.environ.get('CSI_OUTPUT_PATH') or '').strip()
csi_output_glob = (os.environ.get('CSI_OUTPUT_GLOB') or '').strip()
csi_frames_regex = (os.environ.get('CSI_FRAMES_REGEX') or '').strip()
csi_output_max_files = (os.environ.get('CSI_OUTPUT_MAX_FILES') or '').strip()
csi_parse_max_bytes = (os.environ.get('CSI_PARSE_MAX_BYTES') or '').strip()
now = datetime.now(timezone.utc)

if run_pi and device_pi:
    device_ids = [device_pi]
    # On real Pi, keep safer default interval unless explicitly overridden higher/lower by env.
    rssi_interval_s = rssi_interval_s_pi
    full_interval_s = full_interval_s_pi
    wireless_spec_interval_s = wireless_spec_interval_s_pi
else:
    device_ids = [device_model]

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
            'env': {
                'WIFI_SAMPLE_DURATION_S': str(full_duration_s),
                'WIFI_SAMPLE_INTERVAL_S': str(full_interval_s),
                'WIFI_SAMPLE_EMIT_HTTP': '1',
                'WIFI_SAMPLE_ARTIFACT_STRIDE': str(full_artifact_stride),
            },
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
            'env': {
                'WIFI_SAMPLE_DURATION_S': str(rssi_duration_s),
                'WIFI_SAMPLE_INTERVAL_S': str(rssi_interval_s),
                'WIFI_SAMPLE_EMIT_HTTP': '1',
                'WIFI_SAMPLE_ARTIFACT_STRIDE': str(rssi_artifact_stride),
            },
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
            'env': {
                'WIFI_SAMPLE_DURATION_S': str(wireless_spec_duration_s),
                'WIFI_SAMPLE_INTERVAL_S': str(wireless_spec_interval_s),
                'WIFI_SAMPLE_EMIT_HTTP': '1',
                'WIFI_SAMPLE_ARTIFACT_STRIDE': str(wireless_spec_artifact_stride),
            },
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

if [[ "${RUN_PI}" == "true" ]]; then
  echo "[5/8] Run Raspberry Pi one-cycle report (real Wi-Fi/BLE collection if tools are available)"
  EXECUTE_POLICY="true" MAX_SYNC_CYCLES="${MAX_SYNC_CYCLES}" ENABLE_ELAB_ARTIFACT_UPLOAD="${ENABLE_ELAB_ARTIFACT_UPLOAD}" ARTIFACT_UPLOAD_MAX_BYTES="${ARTIFACT_UPLOAD_MAX_BYTES}" REPORT_RPC_TIMEOUT_S="${REPORT_RPC_TIMEOUT_S}" CONTROL_RPC_TIMEOUT_S="${CONTROL_RPC_TIMEOUT_S}" ALLOW_WIFI_DISRUPTIVE_CSI="${ALLOW_WIFI_DISRUPTIVE_CSI}" REQUIRE_REAL_CSI="${REQUIRE_REAL_CSI}" CSI_COLLECTOR_CMD="${CSI_COLLECTOR_CMD}" CSI_OUTPUT_PATH="${CSI_OUTPUT_PATH}" CSI_OUTPUT_GLOB="${CSI_OUTPUT_GLOB}" CSI_FRAMES_REGEX="${CSI_FRAMES_REGEX}" CSI_OUTPUT_MAX_FILES="${CSI_OUTPUT_MAX_FILES}" CSI_PARSE_MAX_BYTES="${CSI_PARSE_MAX_BYTES}" scripts/rpi/smoke_test_agent.sh "${PI_HOST}"
else
  echo "[5/8] Run local model-device one-cycle report"
  docker compose exec -T model-device sh -lc "MAX_SYNC_CYCLES='${MAX_SYNC_CYCLES}' EXECUTE_POLICY='false' AGENT_ID='${DEVICE_MODEL}' CONTROL_PLANE_MODE='RF_SHARING' ENABLE_ELAB_ARTIFACT_UPLOAD='${ENABLE_ELAB_ARTIFACT_UPLOAD}' ARTIFACT_UPLOAD_MAX_BYTES='${ARTIFACT_UPLOAD_MAX_BYTES}' REPORT_RPC_TIMEOUT_S='${REPORT_RPC_TIMEOUT_S}' CONTROL_RPC_TIMEOUT_S='${CONTROL_RPC_TIMEOUT_S}' python -u agent_v2_client.py"
fi

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

echo "[7/8] Wait for Prometheus scrape + remote_write"
sleep 20

echo "[8/8] Verify metrics in Prometheus and Mimir"
docker compose exec -T monad-fleet-service sh -lc "DEVICE_ID='${DEVICE_PI}' RUN_PI='${RUN_PI}' DEVICE_MODEL='${DEVICE_MODEL}' INJECT_HYPOTHETICAL_METRICS='${INJECT_HYPOTHETICAL_METRICS}' python - <<'PY'
import requests
import os

run_pi = (os.environ.get('RUN_PI') or '').lower().strip() in {'1','true','yes'}
target = (os.environ.get('DEVICE_ID') or '').strip().lower()
if not run_pi:
    target = (os.environ.get('DEVICE_MODEL') or '').strip().lower()
inject_hypothetical = (os.environ.get('INJECT_HYPOTHETICAL_METRICS') or '').lower().strip() in {'1','true','yes'}

queries = [
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"wifi_ap_total\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"wifi_avg_rssi_dbm\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"wifi_link_quality\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"wifi_signal_dbm\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"wifi_tx_bitrate_mbps\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"wifi_rx_bitrate_mbps\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"wifi_channel\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"wifi_freq_mhz\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"wifi_if_rx_bytes\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"wifi_if_tx_bytes\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"wifi_sampling_points_applied\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"ble_adv_total\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"ble_avg_rssi_dbm\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"ble_scan_ok\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"csi_frames_total\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"csi_collector_configured\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"csi_capture_ok\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"csi_capture_disabled\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"device_cpu_temp_c\"}}',
    f'monad_fleet_metric_value{{device_id=\"{target}\",metric=\"device_load1\"}}',
]
if inject_hypothetical:
    queries.append('monad_fleet_metric_value{device_id=\"lowlevel-board-01\"}')

def do_query(url: str, q: str):
    resp = requests.get(url, params={'query': q}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get('data', {}).get('result', [])

for q in queries:
    pres = do_query('http://prometheus:9090/api/v1/query', q)
    print('prometheus', q, '=>', len(pres), pres[:1])
    mres = do_query('http://mimir:9009/prometheus/api/v1/query', q)
    print('mimir     ', q, '=>', len(mres), mres[:1])
PY
"

echo "[8b/8] Verify artifact uploads in eLabFTW experiment"
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

if [[ "${RUN_PI}" == "true" ]]; then
  echo "[8c/8] Verify latest Pi run report for this experiment (real-data gates)"
  REAL_DATA_ENFORCE="${REAL_DATA_ENFORCE}" \
  REQUIRE_REAL_WIFI="${REQUIRE_REAL_WIFI}" \
  REQUIRE_REAL_BLE="${REQUIRE_REAL_BLE}" \
  REQUIRE_REAL_CSI="${REQUIRE_REAL_CSI}" \
  REQUIRE_CSI_FRAMES_MIN="${REQUIRE_CSI_FRAMES_MIN}" \
  SMOKE_EXPERIMENT_ID="${SMOKE_EXPERIMENT_ID}" \
  scripts/rpi/verify_real_run.sh "${PI_HOST}"
fi

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
