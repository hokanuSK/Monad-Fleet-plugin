# Diagram Validity Handoff - 2026-04-08

This handoff is for future Codex windows working on `docs/diagrams`.

## User Direction (Must Follow)
- Focus on diagram validity only.
- Do not prioritize implementation vs diagram parity unless user asks.
- Do not upload or modify diagrams unless explicitly requested.
- Always create or update a handoff note before context/token limits.

## Current Scope Completed
- Cloned and ran `Sibyx/monad-knowledge` in `/tmp/monad-knowledge` using Python 3.13 venv.
- Used repo acquisition code paths (`monad_knowledge.acquire.*`) to fetch article candidates.
- Downloaded papers to `/tmp/monad-knowledge-downloads` for validation context.
- Validated all active Mermaid source diagrams by rendering to `/tmp` (no repo writes).

## Evidence Artifacts
- Download directory: `/tmp/monad-knowledge-downloads`
- Current count at handoff time: `18` PDFs
- Render validation output:
  - `/tmp/fleet-diagram-valid-npx`
  - all active `.mmd` files rendered successfully with no parse/syntax errors

## Active Diagram Render Status (Read-only Validation)
- `flowchart-docs-information-architecture.mmd` -> PASS
- `flowchart-metadata-write-boundaries.mmd` -> PASS
- `flowchart-windowed-upload-scheduler.mmd` -> PASS
- `sequence-execution-runtime.mmd` -> PASS
- `sequence-lifecycle-v2-normalized.mmd` -> PASS
- `sequence-maintenance-runtime.mmd` -> PASS
- `sequence-policy-authoring-prepare.mmd` -> PASS
- `state-run-lifecycle.mmd` -> PASS

## Diagram Set Integrity Checks
- Every active `*.mmd` has matching `svg/*.svg`: PASS
- No extra active `svg/*.svg` without source `*.mmd`: PASS
- Required header lines in all active `*.mmd`:
  - `%% NOTE: Keep this diagram updated with any protocol or flow changes!`
  - `%% Last updated: YYYY-MM-DD`
  - PASS for all active files

## Important Diagram-Only Consistency Notes
These are internal cross-diagram wording/logic consistency items (not implementation parity):

1. Report result branch wording differs across diagrams:
- `flowchart-windowed-upload-scheduler.mmd` includes `DUPLICATE or REJECTED -> sent`.
- `sequence-lifecycle-v2-normalized.mmd` shows `ACCEPTED`, `DUPLICATE`, and `RPC fail`.
- `sequence-maintenance-runtime.mmd` compresses as `report upload ACK/err`.

2. Upload state text in state diagram may be semantically unclear:
- `state-run-lifecycle.mmd` has `UPLOADING --> UPLOADING: queue drained (ACCEPTED / DUPLICATE)`.
- Phrase `queue drained` may imply completion while loop remains self-transition.

## Notes on SVG Freshness
- Hash comparison between repo `docs/diagrams/svg/*.svg` and ad-hoc `/tmp` renders differed for all active files.
- This does not imply invalid Mermaid; likely due to renderer/env/post-process differences.
- Treat as informational unless user asks to re-render/normalize.

## Next-Window Procedure (If Conversation Near Context Limit)
1. Append new findings to this handoff file.
2. Record:
  - what was validated,
  - what was downloaded (path + count),
  - unresolved diagram-only consistency items.
3. Do not edit diagram source/render files unless user explicitly requests changes.

