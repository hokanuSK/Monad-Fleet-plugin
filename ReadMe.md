# Monad Fleet as eLabFTW plugin service
This repository has 3 parts and can be run with `docker-compose`:
1. eLabFTW platform (`web` + `mysql`)
2. Monad Fleet service (`monad-fleet-service` folder)
3. Device simulator (`device-sim` folder)
4. Observability stack (`prometheus` + `mimir` + `grafana`)

## gRPC APIs
- `fleet.v1.FleetManager` (legacy flow): `Hello`, `GetPolicy`, `PublishEvent`
- `fleet.v2.FleetManager` (standardized draft): `Hello`, `GetAssignment`, `GetPolicy`, `AckPrepared`, `PublishReport`, optional `PublishEvents`

## Run the stack
```bash
docker compose up -d --build
```

Web endpoints:
- eLabFTW: `https://localhost:8443`
- Grafana: `http://localhost:3000` (`admin` / `admin`)
- Prometheus: `http://localhost:9090`
- Mimir API: `http://localhost:9009`

## Run v2 agent client (local container)
```bash
docker compose exec -T model-device sh -lc \
  'MAX_SYNC_CYCLES=1 CONTROL_PLANE_MODE=RF_SHARING EXECUTE_POLICY=false python -u agent_v2_client.py'
```

## V3 smoke test (WiFi/BLE/CSI + Mimir/Grafana)
```bash
scripts/smoke/v3_end_to_end_smoke.sh
```
This smoke test:
- resets Fleet service state + Prometheus/Mimir data (does not touch MySQL/eLabFTW user content),
- creates a new eLabFTW smoke experiment tagged `fleet` and stores policy JSON in experiment metadata,
- runs one device cycle and publishes a report,
- sends hypothetical low-level board JSON to `POST /ingest/v1/metrics`,
- verifies metrics in both Prometheus and Mimir query APIs.

Useful environment toggles:
```bash
DO_BUILD=true         # rebuild containers first
CLEANUP=true          # delete the created smoke experiment at the end
RESET_STATE=false     # keep /data/state.json
RESET_METRICS=false   # keep Prometheus/Mimir data
```

Reset only Fleet + metrics state (without touching MySQL/eLabFTW DB):
```bash
scripts/reset/dev_reset.sh
```

Example low-level sender payload:
```json
{
  "source": "embedded-hypothetical",
  "device_id": "lowlevel-board-01",
  "metrics": [
    {"name": "wifi_rssi_dbm", "value": -58.2},
    {"name": "ble_adv_count", "value": 81},
    {"name": "csi_frames_count", "value": 420}
  ]
}
```

## Agent data persistence (new)
The v2 agent now stores run data locally first and uploads reports after run completion:
- local spool root: `DATA_ROOT` (default `./data` in agent working directory)
- pending runs: `DATA_ROOT/pending/<run_id>/`
- sent runs: `DATA_ROOT/sent/<run_id>/`
- per-run files: `context.json`, `events.ndjson`, `report.json`, `artifacts/*.log`, `artifacts/run-summary.json`

Upload behavior:
- after each run, the report is persisted locally
- agent replays pending reports to `PublishReport`
- on `ACCEPTED` or `DUPLICATE`, run directory moves from `pending/` to `sent/`
- old sent runs are pruned by `SENT_RETENTION_DAYS` (default `14`)

## Run v2 agent on Raspberry Pi (Wi-Fi only mode)
Automated from this repo host:
```bash
PI_HOST=192.168.0.234 scripts/rpi/deploy_agent.sh
PI_HOST=192.168.0.234 FLEET_MANAGER_HOST=192.168.0.70 scripts/rpi/install_systemd_service.sh
PI_HOST=192.168.0.234 FLEET_MANAGER_HOST=192.168.0.70 scripts/rpi/smoke_test_agent.sh
```

Check service logs on Pi:
```bash
ssh admin@monad-rpi5.local
sudo systemctl status --no-pager monad-fleet-agent.service
sudo journalctl -u monad-fleet-agent.service -n 100 --no-pager
```

Manual fallback (if you do not use scripts):
```bash
python3 -m venv ~/fleet-agent-venv
~/fleet-agent-venv/bin/python -m pip install --upgrade pip grpcio grpcio-tools protobuf
~/fleet-agent-venv/bin/python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. fleet_gateway_v2.proto
```
Run:
```bash
export FLEET_MANAGER_HOST=<host-running-monad-fleet-service>
export FLEET_MANAGER_PORT=50060
export AGENT_ID=<pi-agent-id>
export CONTROL_PLANE_MODE=RF_SHARING
export CONTROL_PLANE_IFACE=wlan0
export MAX_SYNC_CYCLES=1
export DATA_ROOT=./data
export SENT_RETENTION_DAYS=14
~/fleet-agent-venv/bin/python -u agent_v2_client.py
```
