# FleetManager Agent Guide

This repo runs a local eLabFTW instance plus a Python gRPC "fleet manager" service and a simulated/real device agent.

## Repo Layout

- `docker-compose.yml`: brings up eLabFTW (web + mysql) + Monad Fleet service + device simulator + observability.
- `monad-fleet-service/`: Python gRPC server + HTTP `/metrics` + HTTP JSON ingest (`/ingest/v1/metrics`).
- `device-sim/`: simulator clients:
  - `sim_device_client.py`: legacy `fleet.v1` flow.
  - `agent_v2_client.py`: v2 PREPARE/REPORT agent with local spooling (`DATA_ROOT`).
- `scripts/smoke/`: end-to-end smoke tests for WiFi/BLE/CSI + Prometheus/Mimir.
- `scripts/rpi/`: deploy/run the v2 agent on a Raspberry Pi via SSH + systemd.
- `observability/`: Prometheus/Mimir/Grafana config/provisioning.
- `elabimg/`: Docker build context for the custom `elabftw/elabimg` image used by `docker-compose.yml`.

## Quick Start (Docker Compose)

1) Ensure the `web` image exists locally.

`docker-compose.yml` references `elabftw/elabimg:custom`. If you do not have this image, build it from `./elabimg`:

```bash
docker build \
  --build-arg ELABFTW_VERSION=<X.Y.Z-or-branch> \
  -t elabftw/elabimg:custom \
  ./elabimg
```

2) Bring up the stack:

```bash
docker compose up -d --build
docker compose ps
```

3) Ensure `monad-fleet-service` can talk to eLabFTW.

`docker-compose.yml` sets `ELAB_API_KEY` for `monad-fleet-service`. For anything that reads/writes policies in eLabFTW (including the smoke test), update that value to a real eLabFTW REST API key (do not commit real secrets).

If you need a REST API key quickly, you can generate one via the eLabFTW UI with a Playwright automation:

```bash
# Requires Node.js/npm (npx) and enough network access to fetch @playwright/cli (unless already cached).
HEADLESS=false TRACE=true ELAB_KEY_NAME="monad-fleet-service" scripts/playwright/elabftw_generate_rest_api_key.sh
```

The generated key is saved to:

- `output/playwright/elabftw_api_key/<timestamp>/elabftw_rest_api_key.txt`

Web endpoints:

- eLabFTW: https://localhost:8443
- Grafana: http://localhost:3000 (admin/admin)
- Prometheus: http://localhost:9090
- Mimir API: http://localhost:9009
- Monad Fleet gRPC: `localhost:50060`
- Monad Fleet metrics + ingest: `http://localhost:9108/metrics`, `http://localhost:9108/ingest/v1/metrics`

Logs:

```bash
docker compose logs -f monad-fleet-service
docker compose logs -f model-device
```

## Smoke Test (v3 WiFi/BLE/CSI + Metrics)

Run:

```bash
scripts/smoke/v3_end_to_end_smoke.sh
```

Useful env overrides:

```bash
DO_BUILD=true RESET_STATE=true RESET_METRICS=true CLEANUP=false scripts/smoke/v3_end_to_end_smoke.sh
```

What it does (high level):

- restarts Fleet + device + observability services (optionally builds images)
- optionally resets Fleet dedupe state + ingest journal (`/data/state.json`, `/data/ingest-metrics.ndjson`)
- optionally clears Prometheus/Mimir data directories (does not touch eLabFTW/MySQL bind mounts)
- creates a new eLabFTW experiment with a v3 policy (WiFi/BLE/CSI commands)
- runs one agent v2 cycle and publishes a report
- posts a sample low-level JSON payload to `/ingest/v1/metrics`
- verifies metrics in both Prometheus and Mimir query APIs

## Run Agent v2 Manually (In `model-device` Container)

One cycle (report only, no command execution):

```bash
docker compose exec -T model-device sh -lc \
  'MAX_SYNC_CYCLES=1 CONTROL_PLANE_MODE=RF_SHARING EXECUTE_POLICY=false python -u agent_v2_client.py'
```

Key env vars (agent v2):

- `FLEET_MANAGER_HOST`, `FLEET_MANAGER_PORT`
- `AGENT_ID` (defaults from `DEVICE_ID`/`DEVICE_MAC` when set)
- `CONTROL_PLANE_MODE` (`RF_SHARING` or `DUAL_NIC`)
- `CONTROL_PLANE_IFACE` (default `wlan0`)
- `CAPABILITIES` (default `csi,ble,wifi`)
- `MAX_SYNC_CYCLES` (0 = run forever)
- `DATA_ROOT` (default `./data` in the agent working directory)
- `SENT_RETENTION_DAYS` (default `14`)

Spool layout (agent v2):

- pending: `DATA_ROOT/pending/<run_id>/`
- sent: `DATA_ROOT/sent/<run_id>/`
- per-run: `context.json`, `events.ndjson`, `report.json`, `artifacts/`

## Resetting State Quickly

Preferred (scripted reset; does not touch MySQL/eLabFTW volumes):

```bash
scripts/reset/dev_reset.sh
```

Env overrides:

```bash
RESET_STATE=true RESET_METRICS=true RESET_GRAFANA=false scripts/reset/dev_reset.sh
```

Reset Fleet service state only:

```bash
docker compose exec -T monad-fleet-service sh -lc "rm -f /data/state.json /data/ingest-metrics.ndjson || true"
docker compose restart monad-fleet-service
```

Reset Prometheus + Mimir data only:

```bash
docker compose exec -T prometheus sh -lc "rm -rf /prometheus/* || true"
docker compose exec -T mimir sh -lc "rm -rf /data/* || true"
docker compose restart prometheus mimir
```

## Raspberry Pi Agent (Deploy + systemd)

Deploy the v2 agent and proto to the Pi (creates venv, generates stubs):

```bash
PI_HOST=monad-rpi5.local scripts/rpi/deploy_agent.sh
```

Install systemd unit (and start it):

```bash
PI_HOST=monad-rpi5.local FLEET_MANAGER_HOST=<host-running-monad-fleet-service> scripts/rpi/install_systemd_service.sh
```

Run a one-cycle smoke test on the Pi:

```bash
PI_HOST=monad-rpi5.local FLEET_MANAGER_HOST=<host-running-monad-fleet-service> scripts/rpi/smoke_test_agent.sh
```

Check logs on Pi:

```bash
ssh admin@monad-rpi5.local
sudo systemctl status --no-pager monad-fleet-agent.service
sudo journalctl -u monad-fleet-agent.service -n 100 --no-pager
```

## Protobuf Workflow (Keep In Sync)

There are two copies of the protos (service + simulator). When editing proto definitions, update both:

- `monad-fleet-service/proto/`
- `device-sim/proto/`

Then rebuild containers so stubs are regenerated during Docker build:

```bash
docker compose build monad-fleet-service model-device
```

## Build gRPC Spec PDF (TeX)

Source:

- `docs/monad_fleet_grpc_interface_v2.tex`
- CI: `.github/workflows/docs_pdf.yml` builds `docs/monad_fleet_grpc_interface_v2.pdf` and uploads it as a workflow artifact (it also attempts to commit the PDF on `main`, if branch rules allow).

Build (preferred):

```bash
cd docs
latexmk -pdf -interaction=nonstopmode -halt-on-error monad_fleet_grpc_interface_v2.tex
```

Output:

- `docs/monad_fleet_grpc_interface_v2.pdf`

Clean:

```bash
cd docs
latexmk -c monad_fleet_grpc_interface_v2.tex
```

Fallback (if `latexmk` is unavailable):

```bash
cd docs
pdflatex -interaction=nonstopmode -halt-on-error monad_fleet_grpc_interface_v2.tex
pdflatex -interaction=nonstopmode -halt-on-error monad_fleet_grpc_interface_v2.tex
```

## Local (Non-Docker) Dev Loops (Optional)

Run the Fleet service locally:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r monad-fleet-service/requirements.txt
python -m grpc_tools.protoc -I monad-fleet-service/proto --python_out=monad-fleet-service --grpc_python_out=monad-fleet-service \
  monad-fleet-service/proto/fleet_gateway.proto monad-fleet-service/proto/fleet_gateway_v2.proto
ELAB_BASE_URL=https://localhost:8443/api/v2 ELAB_API_KEY=<key> python -u monad-fleet-service/gateway_server.py
```

Run the v2 agent locally:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r device-sim/requirements.txt
python -m grpc_tools.protoc -I device-sim/proto --python_out=device-sim --grpc_python_out=device-sim \
  device-sim/proto/fleet_gateway.proto device-sim/proto/fleet_gateway_v2.proto
FLEET_MANAGER_HOST=127.0.0.1 FLEET_MANAGER_PORT=50060 MAX_SYNC_CYCLES=1 EXECUTE_POLICY=false python -u device-sim/agent_v2_client.py
```
