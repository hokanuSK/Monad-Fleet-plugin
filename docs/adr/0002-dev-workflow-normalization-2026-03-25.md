# ADR 0002: Development Workflow Normalization

- Date: 2026-03-25
- Status: Accepted

## Context

The repository had no single local entrypoint for common workflows (`up`, `smoke`, `reset`, checks).
Quality checks were also inconsistent across local usage and CI.

## Decision

Adopt a normalized developer contract:

- Root `Makefile` as canonical entrypoint.
- Standard commands: `make up`, `make smoke`, `make reset`, `make lint`, `make test`, `make check`.
- Shared check scripts under `scripts/dev/`.
- Optional local pre-commit hooks through `.pre-commit-config.yaml`.
- CI workflow (`.github/workflows/repo_checks.yml`) executes the same `make` checks.
- Add `.env.example` as the baseline environment template.

## Consequences

Positive:

- Single, documented operator path for local development.
- Local and CI checks stay aligned by using the same scripts.
- Reduced drift in repository structure and script expectations.

Tradeoffs:

- Additional maintenance burden for dev scripts and Make targets.
- Some checks are capability-sensitive (for example docker compose availability) and may be skipped when dependencies are missing.

Mitigation:

- Keep checks deterministic and dependency-aware.
- Document expected tooling in `README.md` and keep `requirements-dev.txt` minimal.
