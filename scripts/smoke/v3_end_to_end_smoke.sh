#!/usr/bin/env bash
set -euo pipefail

DEVICE_MODEL="${DEVICE_MODEL:-02:42:ac:14:00:04}"
DEVICE_PI="${DEVICE_PI:-2c:cf:67:80:f5:d9}"
MAX_SYNC_CYCLES="${MAX_SYNC_CYCLES:-1}"

RESET_STATE="${RESET_STATE:-true}"
RESET_METRICS="${RESET_METRICS:-true}"
CLEANUP="${CLEANUP:-false}"
DO_BUILD="${DO_BUILD:-false}"

SMOKE_TITLE_PREFIX="${SMOKE_TITLE_PREFIX:-Fleet Smoke v3}"
SMOKE_TAGS_CSV="${SMOKE_TAGS_CSV:-fleet,smoke:v3,created-by:monad-fleet}"

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

echo "[4/8] Create a new eLabFTW smoke experiment with v3 WiFi/BLE/CSI policy (no existing experiments modified)"
SMOKE_EXPERIMENT_ID="$(docker compose exec -T monad-fleet-service sh -lc "DEVICE_MODEL='${DEVICE_MODEL}' DEVICE_PI='${DEVICE_PI}' SMOKE_TITLE_PREFIX='${SMOKE_TITLE_PREFIX}' SMOKE_TAGS_CSV='${SMOKE_TAGS_CSV}' python - <<'PY'
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
title_prefix = (os.environ.get('SMOKE_TITLE_PREFIX') or 'Fleet Smoke v3').strip() or 'Fleet Smoke v3'
tags_csv = os.environ.get('SMOKE_TAGS_CSV') or 'fleet,smoke:v3,created-by:monad-fleet'
now = datetime.now(timezone.utc)

device_ids = [d for d in [device_model, device_pi] if d]

policy = {
    'target_selector': {'device_ids': device_ids},
    'range': {
        'from': (now - timedelta(minutes=2)).isoformat().replace('+00:00', 'Z'),
        'to': (now + timedelta(minutes=30)).isoformat().replace('+00:00', 'Z'),
    },
    'command_groups': [
        {
            'id': 'v3-wifi-ble-csi',
            'name': 'WiFi BLE CSI v3',
            'failure_mode': 'FAIL_FAST',
            'commands': [
                {'id': 'wifi-scan', 'type': 'WIFI_SCAN', 'cmd': 'echo wifi-scan-v3', 'timeout_s': 10, 'retries': 0},
                {'id': 'ble-scan', 'type': 'BLE_SCAN', 'cmd': 'echo ble-scan-v3', 'timeout_s': 10, 'retries': 0},
                {'id': 'csi-capture', 'type': 'CAPTURE_CSI', 'cmd': 'echo csi-capture-v3', 'timeout_s': 10, 'retries': 0},
            ],
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

echo "[5/8] Run local model-device one-cycle report"
docker compose exec -T model-device sh -lc "MAX_SYNC_CYCLES='${MAX_SYNC_CYCLES}' EXECUTE_POLICY='false' AGENT_ID='${DEVICE_MODEL}' CONTROL_PLANE_MODE='RF_SHARING' python -u agent_v2_client.py"

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

echo "[7/8] Wait for Prometheus scrape + remote_write"
sleep 20

echo "[8/8] Verify metrics in Prometheus and Mimir"
docker compose exec -T monad-fleet-service python - <<'PY'
import requests

queries = [
    'monad_fleet_metric_value{metric=\"wifi_ap_total\"}',
    'monad_fleet_metric_value{metric=\"ble_adv_total\"}',
    'monad_fleet_metric_value{metric=\"csi_frames_total\"}',
    'monad_fleet_metric_value{device_id=\"lowlevel-board-01\"}',
]

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
