# Repository Layout Runbook

Canonical paths:

- `apps/fleet-service`
- `apps/device-sim`
- `apps/gateway-plugin`
- `infra/observability`
- `infra/docker/elabimg`
- `proto`
- `artifacts/data`, `artifacts/tmp`, `artifacts/output`

Migration compatibility:

- Legacy root paths are symlinks to canonical paths.
- New scripts/docs should use canonical paths first.
- Existing scripts may continue using legacy paths until migration cleanup.
