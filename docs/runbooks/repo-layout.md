# Repository Layout Runbook

Last update: 2026-04-09

## Canonical Paths

- `src/fleet-service`
- `src/device-sim`
- `src/observability`
- `src/elabftw` (git submodule)
- `proto`
- `artifacts/data`

## Compatibility Symlinks

Legacy compatibility aliases:

- `data -> artifacts/data`

## Authoring Rules

- New scripts/docs must use canonical paths first.
- Do not reintroduce removed root aliases (`device-sim`, `monad-fleet-service`, `observability`, `tmp`, `output`).
- Avoid adding new references to removed legacy root paths in newly created files.

## Migration Plan

1. Phase 1: canonical-first authoring with compatibility links enabled.
2. Phase 2: remove legacy path usage from maintained scripts and docs.
3. Phase 3 (current): only `data -> artifacts/data` alias remains.

## Verification

Use the local contract check:

```bash
make verify-layout
```

This validates required canonical directories and the remaining compatibility symlink.
