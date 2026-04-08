# Diagrams Maintenance Guide

## Folder Layout
- `*.mmd`: source diagrams (authoritative)
- `svg/*.svg`: rendered diagrams for current `fleet.v2` flow
- `legacy/*`: archived historical sources/renders (do not edit unless intentionally backporting)

## Normalization Rules
- One active diagram = one basename pair: `<name>.mmd` and `svg/<name>.svg`.
- Keep filenames lowercase-kebab-case.
- Any `svg/*` file without matching `.mmd` should be either:
  - backfilled with a new `.mmd` source, or
  - moved to `legacy/` if no longer maintained.
- Prefer **single canonical flow** for key protocols;
  avoid multiple strongly-overlapping active diagrams in the same domain.
  - Current canonical sequence: `sequence-lifecycle-v2-normalized.mmd`
  - Current state overview: `state-run-lifecycle.mmd` (derived from lifecycle sequence)
  - Runtime precedence rule: if `sequence-policy-authoring-prepare`, `sequence-execution-runtime`, `sequence-maintenance-runtime`, or `flowchart-windowed-upload-scheduler` differ from lifecycle, update them to match lifecycle.
- Diagram variants may be maintained in source for historical context or simplified views (e.g., onboarding splits) but should be clearly marked as such.
- Every `.mmd` must include the header:
  - `%% NOTE: Keep this diagram updated with any protocol or flow changes!`
  - `%% Last updated: YYYY-MM-DD`

## New Thread Handoff Notes
- If a user changes one runtime diagram, assume the same protocol change must be propagated to all runtime views.
- Treat every small visual or wording tweak as a cross-diagram sync item: propagate to all affected active diagrams in the same pass.
- Update in this order:
  1. `sequence-lifecycle-v2-normalized.mmd` (source of truth)
  2. `sequence-policy-authoring-prepare.mmd` (authoring + prepare)
  3. `sequence-execution-runtime.mmd` (execution only)
  4. `sequence-maintenance-runtime.mmd` (maintenance only)
  5. `flowchart-windowed-upload-scheduler.mmd` (if maintenance/upload behavior changed)
  6. `state-run-lifecycle.mmd` (if lifecycle states/transitions changed)
- Keep command names, status labels, loop conditions, and maintenance-window semantics identical across all affected diagrams.
- Re-render all changed files and commit both `.mmd` + `svg/*.svg` pairs.
- Before finishing, run a quick grep to catch stale wording (e.g., old command names, removed loops, old metadata targets).

## Output Layout Lock
- Keep `docs/diagrams/mermaid-config.json` sequence spacing values unchanged unless user explicitly asks for visual redesign:
  - `diagramMarginX=30`
  - `diagramMarginY=20`
  - `actorMargin=48`
  - `messageMargin=22`
  - `boxMargin=16`
  - `boxTextMargin=8`
  - `noteMargin=14`
- In split runtime diagrams (`sequence-execution-runtime`, `sequence-maintenance-runtime`), keep `Fleet Manager`, `eLabFTW`, and `Mimir` under one top bundle named `Backend Services`.
- In `sequence-execution-runtime.mmd`, keep one top `Device Agent` bundle containing `Agent v2`, `Command Runner`, `Local TSDB`, and `Local Run Store`.
- In `sequence-maintenance-runtime.mmd`, keep a single top bundle named `Device Agent` containing `Agent v2`, `Upload Worker`, `Local Run Store`, and `Local TSDB`.
- In `sequence-lifecycle-v2-normalized.mmd`, keep one top `Device Agent` bundle containing `Agent v2`, `Command Runner`, `Upload Worker`, `Local TSDB`, and `Local Run Store` so lifecycle box spacing tracks split runtime diagrams.
- Apply one shared spacing rule across all active diagrams via `docs/diagrams/mermaid-config.json` only; do not add per-file `%%{init ...}%%` spacing overrides.
- Keep the "Derived diagram. Source of truth: sequence-lifecycle-v2-normalized.mmd" note directly below participant declarations (not above them).
- In all active sequence diagrams, keep phase `rect` blocks full-span from first actor column to last actor column.
  Keep each phase headline note inside that `rect` and span it first..last actors.
- Keep command-color `rect` blocks inset inside the phase span (not flush with phase edges).
- Keep command-color `rect` blocks extended upward so `alt [command = ...]` labels sit inside the same color area (normalized by post-process).
- Keep `loop/alt` fragment left edges aligned to the same inset as command-color blocks.
- Keep colored rect backgrounds rendered above top bundle borders, and keep labels/text above both.
- Keep bundle title text clear of actor boxes via global `boxTextMargin=8` (all diagrams).
- Tune bundle-title gap per diagram in `scripts/render_diagrams.sh` (`bundle_title_gap_by_svg`) to normalize visual spacing:
  - `sequence-execution-runtime.svg`: `25.0`
  - `sequence-policy-authoring-prepare.svg`: `25.0`
  - `sequence-maintenance-runtime.svg`: `25.0`
  - `sequence-lifecycle-v2-normalized.svg`: `25.0`
- Reason: Mermaid computes slightly different top geometry per diagram, so identical numeric gap does not always look identical.
- Keep top bundle width tight to participant headers (not oversized): `--bundle-side-margin=22.0`.
- Keep visible horizontal separation between sibling top bundles: `--bundle-gap-min=24.0` (current renders produce 36px in runtime diagrams).
- Keep participant header boxes vertically aligned on one row (`y=33`) across all active sequence diagrams.
- Keep top stick-figure actor labels (e.g., `Researcher`) aligned to the same text row as participant box labels (handled by `align_reverse_sequence_labels.py`).
- For lifecycle nested left-side fragments (LogMetrics/LogLogs/measure), do not shorten protocol wording just to fit; keep wording in `.mmd` and fix fit via SVG post-process spacing.
- Keep `measure.type` branch conditions rendered as one-line labels: `[measure.type = WIFI]`, `[measure.type = BLE]`, `[measure.type = CSI]`.
- Keep left-edge loop/alt condition text under the tab corner (shifted down/right enough so it never overlays the top border).
- In `sequence-maintenance-runtime.mmd`, keep all primary yellow notes (`Derived...`, `MAINTENANCE`, final no-data-loss note) spanning `Agent..Mimir` so they share the same left start edge.
- In `sequence-maintenance-runtime.mmd`, keep `MAINTENANCE` + initial `Check` + `Set cron` inside the orange `rect`.
- For loop labels, put the condition directly in the loop header: `loop <label>` (avoid separate loop-condition notes).
- Keep long actor-to-actor labels wrapped with `<br/>` (not literal `\n`) when they widen columns and break alignment.
- In `sequence-maintenance-runtime.mmd`, keep `Fleet -> eLabFTW` PATCH labels concise + wrapped; long metadata labels will recreate large top-column gaps.
- In `sequence-lifecycle-v2-normalized.mmd`, keep `Fleet -> eLabFTW` PATCH labels concise (execution-style wording) to prevent backend-column over-expansion.
- After edits, visually validate top area first: bundle header spacing, actor columns, derived-note padding.

## How to Update Mermaid Diagrams and SVGs
1. Edit the `.mmd` source in this folder.
2. Keep the top note updated (date + short summary).
3. Install local pinned Mermaid CLI once (or after `package.json` changes):

   ```sh
   npm install
   ```

4. Render + post-process with one command:

   ```sh
   npm run render -- <input>.mmd
   ```

   You can omit args to render all active diagrams:

   ```sh
   npm run render
   ```

   Script used: `scripts/render_diagrams.sh` (runs `mmdc` + both Python post-processors).
   The script is macOS-bash compatible (no `mapfile` dependency).
  It enforces layout lock:
  - reverse-arrow label alignment,
  - fragment frame normalization,
  - normalized bundle-title to actor-box vertical gap (`16.5` default),
  - active runtime/process diagrams use per-file bundle-title override (`25.0`),
  - top-bundle side margin (`22.0`) + minimum inter-bundle gap (`24.0`),
  - centered fragment condition names (without moving corner `alt/loop` tabs),
  - single-line `measure.type` branch labels,
  - one post-process pass per SVG (avoids second-pass drift).

4a. Keep bundle border layering lock:
   - colored rects over bundle borders,
   - labels/text over colored rects and borders.

6. Review the rendered result in `svg/`.
7. Commit both the updated `.mmd` and `svg/*.svg`.

## Quick Batch Render
Run from `diagrams/`:

```sh
npm run render
```

## Requirements
- Node.js + npm
- Local pinned dependency install:

  ```sh
  cd docs/diagrams
  npm install
  ```

## Content Guardrails
- Main diagrams must describe `fleet.v2` runtime as defined by `sequence-lifecycle-v2-normalized.mmd` (`GetPolicy`, execution command loop, `ReportCommandStatus`, `PublishReport`, maintenance window upload).
- Legacy `PublishEvent`-only flow belongs in `legacy/`, not in active `svg/`.
