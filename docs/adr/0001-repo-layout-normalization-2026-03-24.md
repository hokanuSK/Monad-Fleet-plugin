# ADR 0001: Repository Layout Normalization

- Date: 2026-03-24
- Status: Accepted

## Context

The repository previously mixed deployable applications, infrastructure assets, and runtime artifacts at the top level. This caused path ambiguity and made ownership boundaries harder to maintain.

## Decision

Adopt a normalized top-level structure:

- `apps/` for deployable/runtime applications
- `infra/` for infrastructure definitions and image contexts
- `proto/` as canonical protobuf source-of-truth
- `artifacts/` for runtime state and generated outputs
- `libs/` for shared libraries

Keep backward compatibility by preserving legacy root paths as symlinks.

## Consequences

Positive:

- Clear ownership and domain boundaries
- Simpler future modularization of shared code
- Proto synchronization source is explicit

Tradeoffs:

- Some tooling may still rely on legacy root paths
- Symlink support is required in development environments

Mitigation:

- Maintain compatibility symlinks during migration window
- Prefer canonical paths in new code and docs
