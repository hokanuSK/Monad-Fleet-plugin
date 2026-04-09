# Quick Mermaid Picture Regeneration

Source of truth is `docs/diagrams/*.mmd`.
Rendered pictures are `docs/diagrams/svg/*.svg`.

## Fastest reliable command (Docker)

```bash
cd /Users/admin/FleetManager/docs/diagrams
for f in *.mmd; do
  echo "render ${f}"
  docker run --rm -v /Users/admin/FleetManager/docs/diagrams:/data minlag/mermaid-cli \
    -i "/data/${f}" -o "/data/svg/${f%.mmd}.svg" -c /data/mermaid-config.json
done
```

## Why we keep redoing this

- Every diagram content change is made in `.mmd`.
- SVGs are generated files, so they must be re-rendered after `.mmd` changes.
- If SVGs are not regenerated, docs and repo visuals become out of sync with the actual flow.
