# Monad Fleet as eLabFTW plugin service
This repository has 3 parts and can be run with `docker-compose`:
1. eLabFTW platform (`web` + `mysql`)
2. Monad Fleet service (`monad-fleet-service` folder)
3. Device simulator (`device-sim` folder)

## gRPC APIs
- `fleet.v1.FleetManager` (legacy flow): `Hello`, `GetPolicy`, `PublishEvent`
- `fleet.v2.FleetManager` (standardized draft): `Hello`, `GetAssignment`, `GetPolicy`, `AckPrepared`, `PublishReport`, optional `PublishEvents`

## Run the stack
```bash
docker compose up -d --build
```

## Run v2 agent client (local container)
```bash
docker compose exec -T model-device sh -lc \
  'MAX_SYNC_CYCLES=1 CONTROL_PLANE_MODE=RF_SHARING EXECUTE_POLICY=false python -u agent_v2_client.py'
```

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
~/fleet-agent-venv/bin/python -u agent_v2_client.py
```
