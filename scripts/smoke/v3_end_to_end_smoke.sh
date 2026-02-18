#!/usr/bin/env bash
set -euo pipefail

DEVICE_MODEL="${DEVICE_MODEL:-02:42:ac:14:00:04}"
DEVICE_PI="${DEVICE_PI:-2c:cf:67:80:f5:d9}"
EXPERIMENT_ID="${EXPERIMENT_ID:-17}"
MAX_SYNC_CYCLES="${MAX_SYNC_CYCLES:-1}"

echo "[1/6] Build and restart core services"
docker compose build monad-fleet-service model-device
docker compose up -d --force-recreate monad-fleet-service model-device prometheus mimir grafana

echo "[2/6] Patch fleet experiment ${EXPERIMENT_ID} with v3 WiFi/BLE/CSI policy"
docker compose exec -T monad-fleet-service sh -lc "EXPERIMENT_ID='${EXPERIMENT_ID}' DEVICE_MODEL='${DEVICE_MODEL}' DEVICE_PI='${DEVICE_PI}' python - <<'PY'
import json
import os
from datetime import datetime, timedelta, timezone

import requests
import urllib3

urllib3.disable_warnings()
base = os.environ.get('ELAB_BASE_URL', 'https://web/api/v2').rstrip('/')
key = os.environ.get('ELAB_API_KEY', '')
exp_id = int(os.environ.get('EXPERIMENT_ID', '17'))
device_model = os.environ.get('DEVICE_MODEL', '02:42:ac:14:00:04').lower()
device_pi = os.environ.get('DEVICE_PI', '2c:cf:67:80:f5:d9').lower()
now = datetime.now(timezone.utc)

policy = {
    'target_selector': {'device_ids': [device_model, device_pi]},
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
resp = requests.patch(
    f'{base}/experiments/{exp_id}',
    headers={'Authorization': key},
    json={'metadata': json.dumps(metadata)},
    verify=False,
    timeout=20,
)
resp.raise_for_status()
print(f'patched experiment={exp_id}')
PY"

echo "[3/6] Run local model-device one-cycle report"
docker compose exec -T model-device sh -lc "MAX_SYNC_CYCLES='${MAX_SYNC_CYCLES}' EXECUTE_POLICY='false' AGENT_ID='${DEVICE_MODEL}' CONTROL_PLANE_MODE='RF_SHARING' python -u agent_v2_client.py"

echo "[4/6] Send hypothetical low-level board payload to HTTP ingest"
docker compose exec -T monad-fleet-service sh -lc "python - <<'PY'
import json
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

echo "[5/6] Wait for Prometheus scrape + remote_write"
sleep 20

echo "[6/6] Verify metrics in Prometheus and Mimir"
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
