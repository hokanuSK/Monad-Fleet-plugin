# Source Layout

Application/runtime code is organized by deployable unit:

- `fleet-service/`: Python gRPC fleet manager service (`monad-fleet-service` Compose service)
- `device-sim/`: simulator + v2 agent runtime client (`model-device` Compose service)
  - see `device-sim/README.md` for v2 lifecycle/runtime behavior
- `elabftw/`: upstream eLabFTW source submodule used for custom image builds

Related paths outside `src/`:

- `shared/proto/`: canonical protobuf contracts shared by service and simulator
- `infrastructure/observability/`: Prometheus, Mimir, and Grafana configuration
