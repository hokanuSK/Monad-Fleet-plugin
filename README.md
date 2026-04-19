# FleetManager

FleetManager runs a local eLabFTW stack plus a Python gRPC fleet service, a device simulator/agent, and observability services.

## Canonical Development Entry Points

1. Copy config template:
   ```bash
   cp .env.example .env
   ```
2. Start the stack:
   ```bash
   make up
   ```
3. Run smoke:
   ```bash
   make smoke
   ```
4. Reset non-DB runtime state:
   ```bash
   make reset
   ```
5. Run repo checks:
   ```bash
   make check
   ```

`Makefile` is the canonical local entrypoint for day-to-day operations.

## Repository Layout (Canonical)

- `src/`: deployable/runtime source code (`fleet-service`, `device-sim`, `observability`, `elabftw` submodule)
- `shared/`: shared assets/libraries across apps (protobuf contracts under `shared/proto/`)
- `infrastructure/`: infrastructure and deployment assets
- `artifacts/`: runtime data, temp files, outputs
- `scripts/`: operational scripts grouped by domain (`smoke/`, `rpi/`, `reset/`, `ops/`, `dev/`)

## Legacy Compatibility Paths

The following root aliases are kept for compatibility during migration:

- `data -> artifacts/data`

Use canonical paths in new code and docs. Migration details are in `docs/runbooks/repo-layout.md`.

## Quality Workflow

Unified commands:

- `make lint`: shell + python + yaml + docker compose sanity checks
- `make format`: optional local formatters (`shfmt`, `ruff`) when installed
- `make test`: layout contract + discovered Python unit tests
- `make check`: lint + test

Optional pre-commit setup:

```bash
pip install -r requirements-dev.txt
pre-commit install
```

CI runs the same contract via `.github/workflows/repo_checks.yml`.

## Core Endpoints

- eLabFTW: `https://localhost:8443`
- Grafana: `http://localhost:3000`
- Mimir: `http://localhost:9009`
- Fleet gRPC: `localhost:50060`
- Fleet metrics/ingest: `http://localhost:9108/metrics`, `http://localhost:9108/ingest/v1/metrics`
- Prometheus: device-local only (not part of the localhost stack)

## AWS CloudFormation Bootstrap

For one-host AWS deploys using infrastructure-as-code:

1. Prepare `.env.aws` from `.env.aws.example`.
2. Store `.env.aws` in SSM Parameter Store (SecureString).
3. Deploy stack:
   ```bash
   AWS_REGION=eu-north-1 \
   STACK_NAME=fleetmanager-ec2 \
   ENV_SSM_PARAMETER_NAME=/fleetmanager/prod/env \
   scripts/aws/deploy_cloudformation_stack.sh
   ```
4. Verify stack + services:
   ```bash
   AWS_REGION=eu-north-1 \
   STACK_NAME=fleetmanager-ec2 \
   scripts/aws/verify_cloudformation_stack.sh
   ```

CloudFormation template path:
- `infrastructure/aws/cloudformation/fleetmanager-ec2.yml`

## Useful Docs

- Operational handoff: `docs/ops/agent_handoff_notion_ops_2026-03-19.md`
- Layout runbook: `docs/runbooks/repo-layout.md`
- Docs index: `docs/DOCS_MAP.md`
- gRPC spec source: `docs/monad_fleet_grpc_interface_v2.tex`
