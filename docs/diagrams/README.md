# Diagrams Maintenance Guide

## Folder Layout
- `*.mmd`: source diagrams (authoritative)
- `svg/*.svg`: rendered diagrams for current `fleet.v2` flow
- `legacy/*.svg`: archived historical renders (do not edit unless intentionally backporting)

## Normalization Rules
- One active diagram = one basename pair: `<name>.mmd` and `svg/<name>.svg`.
- Keep filenames lowercase-kebab-case.
- Any `svg/*` file without matching `.mmd` should be either:
  - backfilled with a new `.mmd` source, or
  - moved to `legacy/` if no longer maintained.
- Every `.mmd` must include the header:
  - `%% NOTE: Keep this diagram updated with any protocol or flow changes!`
  - `%% Last updated: YYYY-MM-DD`

## How to Update Mermaid Diagrams and SVGs
1. Edit the `.mmd` source in this folder.
2. Keep the top note updated (date + short summary).
3. Render the matching SVG into `svg/`:

   ```sh
   npx -y -p @mermaid-js/mermaid-cli mmdc -i <input>.mmd -o svg/<input>.svg -c mermaid-config.json
   ```

4. Review the rendered result in `svg/`.
5. Commit both the updated `.mmd` and `svg/*.svg`.

## Quick Batch Render
Run from `diagrams/`:

```sh
for f in *.mmd; do npx -y -p @mermaid-js/mermaid-cli mmdc -i "$f" -o "svg/${f%.mmd}.svg" -c mermaid-config.json; done
```

## Requirements
- Node.js + npm
- `@mermaid-js/mermaid-cli` (fetched by `npx`)

## Content Guardrails
- Main diagrams must describe `fleet.v2` (`Hello`, `GetAssignment`, `GetPolicy`, `AckPrepared`, `PublishReport`; optional `PublishEvents`).
- Legacy `PublishEvent`-only flow belongs in `legacy/`, not in active `svg/`.
