# Repository Layout Runbook

Last update: 2026-03-25

## Canonical Paths

- `apps/fleet-service`
- `apps/device-sim`
- `infra/observability`
- `infra/docker/elabimg`
- `proto`
- `artifacts/data`
- `artifacts/tmp`
- `artifacts/output`

## Compatibility Symlinks

Legacy root paths are currently kept as compatibility aliases:

- `monad-fleet-service -> apps/fleet-service`
- `device-sim -> apps/device-sim`
- `observability -> infra/observability`
- `data -> artifacts/data`
- `tmp -> artifacts/tmp`
- `output -> artifacts/output`

## Authoring Rules

- New scripts/docs must use canonical paths first.
- Keep compatibility links intact until all active tooling migrates.
- Avoid adding new references to legacy root paths in newly created files.

## Migration Plan

1. Phase 1 (active): canonical-first authoring with compatibility links still enabled.
2. Phase 2: remove legacy path usage from maintained scripts and docs.
3. Phase 3: remove compatibility symlinks after two stable release cycles with zero legacy path dependency.

## Verification

Use the local contract check:

```bash
make verify-layout
```

This validates required canonical directories and expected compatibility symlinks.
