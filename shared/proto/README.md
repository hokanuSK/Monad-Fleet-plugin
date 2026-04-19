# Proto

Canonical protobuf source-of-truth for Fleet APIs.

Files:

- `fleet_gateway.proto` (`fleet.v1`)
- `fleet_gateway_v2.proto` (`fleet.v2`)
- `fleet_gateway_v3.proto` (`fleet.v3`, current)

App directories consume this through symlinked `proto/` directories:

- `src/fleet-service/proto -> ../../shared/proto`
- `src/device-sim/proto -> ../../shared/proto`
