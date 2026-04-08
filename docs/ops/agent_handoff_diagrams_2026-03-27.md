# Diagram Handoff - 2026-03-27

This handoff captures the diagram refactor and alignment work done in `docs/diagrams` for `fleet.v2`.

## Scope Completed
- Renamed process diagram files from generic names to descriptive names.
- Split Process B into two focused runtime diagrams (`execution` and `maintenance`).
- Retired the combined runtime diagram; execution and maintenance remain split.
- Normalized top-note spacing in Process A (prepare diagram).
- Moved SVG-only legacy assets (no matching `.mmd`) into `docs/diagrams/legacy/`.
- Regenerated active SVG outputs.
- Added synchronization guidance for future threads in `docs/diagrams/README.md`.

## Active Diagram Set (Source + Render)
- `sequence-lifecycle-v2-normalized.mmd` + `svg/sequence-lifecycle-v2-normalized.svg` (source of truth)
- `sequence-policy-authoring-prepare.mmd` + `svg/sequence-policy-authoring-prepare.svg`
- `sequence-execution-runtime.mmd` + `svg/sequence-execution-runtime.svg`
- `sequence-maintenance-runtime.mmd` + `svg/sequence-maintenance-runtime.svg`
- `flowchart-windowed-upload-scheduler.mmd` + `svg/flowchart-windowed-upload-scheduler.svg`

## File Renames Applied
- `process-a.mmd` -> `sequence-policy-authoring-prepare.mmd`
- `process-b.mmd` -> `execution-maintenance-runtime.mmd`
- `svg/process-a.svg` -> `svg/sequence-policy-authoring-prepare.svg`
- `svg/process-b.svg` -> `svg/execution-maintenance-runtime.svg`

## New Split Diagrams
- `sequence-execution-runtime.mmd` / `svg/sequence-execution-runtime.svg`
- `sequence-maintenance-runtime.mmd` / `svg/sequence-maintenance-runtime.svg`

## Legacy Moves
Moved from `docs/diagrams/svg/` to `docs/diagrams/legacy/`:
- `agent-states.svg`
- `architecture.svg`
- `component-structure.svg`
- `data-flow.svg`
- `deployment.svg`
- `network-topology.svg`

Moved from active runtime set to `docs/diagrams/legacy/`:
- `execution-maintenance-runtime.mmd`
- `execution-maintenance-runtime.svg`
- `event-write-flow.mmd`
- `event-write-flow.svg`

## Important Content Constraints (From User Direction)
- Do not model this as patching experiment metadata from Fleet in a way that changes experiment-level metadata ownership.
- Prefer event/item metadata updates where status/progress reporting is needed.
- Keep maintenance behavior explicit:
  - window+slot gating,
  - wake-up via cron for re-check,
  - loop while maintenance window+slot remains open.
- Keep command naming and coloring consistent and distinct across runtime diagrams.

## Source-of-Truth Sync Rule
If one runtime diagram changes, propagate to all affected runtime views in this order:
1. `sequence-lifecycle-v2-normalized.mmd`
2. `sequence-policy-authoring-prepare.mmd`
3. `sequence-execution-runtime.mmd`
4. `sequence-maintenance-runtime.mmd`
5. `flowchart-windowed-upload-scheduler.mmd` (when upload/window semantics change)

## Render Command
Run from `docs/diagrams`:

```sh
npm install
npm run render
```

Notes:
- Mermaid CLI is pinned locally in `docs/diagrams/package.json` (`@mermaid-js/mermaid-cli@11.12.0`).
- Unified render pipeline is `docs/diagrams/scripts/render_diagrams.sh` (Mermaid render + both SVG post-processors).

## Validation Checklist For Next Codex Thread
- Confirm every active `svg/*.svg` has matching `*.mmd`.
- Confirm old names (`process-a`, `process-b`) are not referenced.
- Confirm command names and loop conditions match across runtime diagrams.
- Confirm top-note spacing in `sequence-policy-authoring-prepare.svg` is visually consistent.
- Treat even tiny visual/content changes as global sync work: update every affected active diagram, then full re-render.
- Re-render all changed diagrams and commit both `.mmd` + `.svg`.

## Output Layout Lock (Do Not Drift)
- Keep sequence spacing config in `docs/diagrams/mermaid-config.json` as-is:
  - `diagramMarginX=30`, `diagramMarginY=20`
  - `actorMargin=48`, `messageMargin=22`
  - `boxMargin=16`, `boxTextMargin=8`, `noteMargin=14`
- Keep `Fleet Manager`, `eLabFTW`, and `Mimir` grouped under one top bundle named `Backend Services` in split runtime diagrams (`sequence-execution-runtime`, `sequence-maintenance-runtime`).
- Keep B2 (`sequence-maintenance-runtime.mmd`) as one top bundle named `Device Agent` with participants: `Agent v2`, `Upload Worker`, `Local Run Store`, `Local TSDB`.
- Keep lifecycle (`sequence-lifecycle-v2-normalized.mmd`) top `Device Agent` bundle with participants: `Agent v2`, `Command Runner`, `Upload Worker`, `Local TSDB`, `Local Run Store` for spacing parity with split runtime diagrams.
- Keep spacing uniform across all active diagrams using only `docs/diagrams/mermaid-config.json`; do not use per-diagram spacing overrides.
- Keep the derived-note line right under participant declarations.
- In B2 (`sequence-maintenance-runtime.mmd`), keep yellow section notes aligned to the same left start by using `Note over Agent,Mimir` for `Derived...`, `MAINTENANCE`, and final no-data-loss note.
- For loop labels, use `loop` plus `Note over <loop-start participant>: <label>` so loop text starts at loop origin, not centered across the loop span.
- Keep wrapped labels with `<br/>` (never literal `\n`) for long messages that would otherwise stretch columns.
- Keep Fleet->eLab PATCH labels concise and wrapped (avoid long raw metadata(...) strings), otherwise large top-column gaps reappear.
- Keep bundle title text from overlapping actor boxes (global `boxTextMargin=8` in config, not per-file overrides).
- Keep actor-top row aligned at `y=33` in all active sequence diagrams.
- Keep default bundle-title gap `16.5` for lifecycle + maintenance (`y=16.5`).
- Keep execution + policy with larger header gap override (`bundle-title-gap=28.0`, target `y=5`) via `scripts/render_diagrams.sh`.
- Keep top bundles tight and readable (do not let them become oversized):
  - `bundle-side-margin=22.0` (border hugs first/last participant header with small padding)
  - `bundle-gap-min=24.0` (maintain visible spacing between sibling bundles)
  - participants inside bundles stay aligned on the same top row
- Keep command color rects starting high enough to include `alt [command = ...]` labels (handled by `scripts/normalize_phase_rect_spans.py` defaults: `command-top-overlap=28`, `hello-top-overlap=54`).
- If alignment drifts, fix layout first, then content edits.

## Late Layout Lock Updates (2026-03-27)
- Keep `sequence-execution-runtime.mmd` with one top `Device Agent` bundle containing `Agent v2`, `Command Runner`, `Local TSDB`, `Local Run Store`.
- Keep lifecycle left nested fragments (LogMetrics / LogLogs / measure.type / async Measurements) readable without wording reductions; solve fit with SVG post-process spacing.
- Keep measure branch labels single-line in final SVG:
  - `[measure.type = WIFI]`
  - `[measure.type = BLE]`
  - `[measure.type = CSI]`
- Keep left-edge loop/alt condition labels shifted below/right of the tab corner so they do not overlay fragment top borders.
- Always run both post-processors after Mermaid render:
  1. `python3 scripts/normalize_phase_rect_spans.py ...`
  2. `python3 scripts/align_reverse_sequence_labels.py ...`
