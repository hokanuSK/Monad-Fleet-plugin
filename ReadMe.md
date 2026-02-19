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
- verifies metrics in Prometheus and Mimir query APIs,
- verifies artifact uploads in the created eLabFTW experiment,
- when `RUN_PI=true`, runs strict report validation with `scripts/rpi/verify_real_run.sh`.

Useful environment toggles:
```bash
DO_BUILD=true         # rebuild containers first
CLEANUP=true          # delete the created smoke experiment at the end
RESET_STATE=false     # keep /data/state.json
RESET_METRICS=false   # keep Prometheus/Mimir data
RESET_GRAFANA=true    # wipe Grafana local DB (dashboards/users)
RUN_PI=true           # run Raspberry Pi one-cycle instead of model-device
PI_HOST=monad-rpi5.local
INJECT_HYPOTHETICAL_METRICS=false  # default; keep real-only path
REAL_DATA_ENFORCE=true             # default when RUN_PI=true
REQUIRE_REAL_WIFI=true
REQUIRE_REAL_BLE=true
REQUIRE_REAL_CSI=true
REQUIRE_CSI_FRAMES_MIN=1
```

Strict real-data command (Pi):
```bash
RUN_PI=true PI_HOST=monad-rpi5.local \
SMOKE_PROFILE=wireless_spec \
REAL_DATA_ENFORCE=true REQUIRE_REAL_WIFI=true REQUIRE_REAL_BLE=true REQUIRE_REAL_CSI=true \
CSI_COLLECTOR_CMD='sudo feitcsi --frequency 5180 --channel-width 80 --format HESU --output-file /tmp/csi.dat -v' \
CSI_OUTPUT_PATH=/tmp/csi.dat \
scripts/smoke/v3_end_to_end_smoke.sh
```
If control-plane route is Wi-Fi and `REQUIRE_REAL_CSI=true`, the Pi smoke helper fails fast unless `ALLOW_WIFI_DISRUPTIVE_CSI=true`.
Using that override may interrupt SSH during capture.

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
SMOKE_EXPERIMENT_ID=<id> scripts/rpi/verify_real_run.sh 192.168.0.234
```
For real Wi-Fi/BLE metrics collection, ensure `EXECUTE_POLICY=true` on the Pi agent and that `iw` + `bluetoothctl` are available.
For strict CSI validation, a working CSI collector command is required.

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
export WIFI_SCAN_IFACE=wlan0
export MAX_SYNC_CYCLES=1
export EXECUTE_POLICY=true
export DATA_ROOT=./data
export SENT_RETENTION_DAYS=14
~/fleet-agent-venv/bin/python -u agent_v2_client.py
```
