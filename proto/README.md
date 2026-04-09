# Proto

Canonical protobuf source-of-truth for Fleet APIs.

Files:

- `fleet_gateway.proto` (`fleet.v1`)
- `fleet_gateway_v2.proto` (`fleet.v2`)

App directories consume this through symlinked `proto/` directories:

- `src/fleet-service/proto -> ../../proto`
- `src/device-sim/proto -> ../../proto`
