# Source Layout

Application code is organized by deployable/runtime unit:

- `fleet-service/`: Python gRPC fleet manager service (`monad-fleet-service` Compose service)
- `device-sim/`: simulator + v2 agent runtime client (`model-device` Compose service)

Legacy root paths are kept as symlinks for compatibility:

- `monad-fleet-service -> src/fleet-service`
- `device-sim -> src/device-sim`
