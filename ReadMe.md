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
Copy `device-sim/agent_v2_client.py` and `device-sim/proto/fleet_gateway_v2.proto` to the Pi, then generate stubs:
```bash
python3 -m pip install grpcio grpcio-tools protobuf
python3 -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. fleet_gateway_v2.proto
```
Run:
```bash
export FLEET_MANAGER_HOST=<host-running-monad-fleet-service>
export FLEET_MANAGER_PORT=50060
export AGENT_ID=<pi-agent-id>
export CONTROL_PLANE_MODE=RF_SHARING
export CONTROL_PLANE_IFACE=wlan0
python3 -u agent_v2_client.py
```
