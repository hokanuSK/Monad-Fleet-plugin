# Apps

Application code is organized by deployable/runtime unit:

- `fleet-service/`: Python gRPC fleet manager service (`monad-fleet-service` Compose service)
- `device-sim/`: simulator + v2 agent runtime client (`model-device` Compose service)
- `gateway-plugin/`: legacy gateway plugin prototype

Legacy root paths are kept as symlinks for compatibility:

- `monad-fleet-service -> apps/fleet-service`
- `device-sim -> apps/device-sim`
- `gateway-plugin -> apps/gateway-plugin`
