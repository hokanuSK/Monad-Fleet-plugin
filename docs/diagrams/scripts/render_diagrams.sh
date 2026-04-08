#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIAGRAM_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${DIAGRAM_DIR}"

MMDC_BIN="${DIAGRAM_DIR}/node_modules/.bin/mmdc"
if [[ ! -x "${MMDC_BIN}" ]]; then
  echo "error: Mermaid CLI is not installed in docs/diagrams." >&2
  echo "run: cd docs/diagrams && npm install" >&2
  exit 1
fi

declare -a sources
if [[ $# -eq 0 ]]; then
  while IFS= read -r src; do
    sources+=("${src}")
  done < <(ls *.mmd)
else
  for input in "$@"; do
    if [[ -f "${input}" ]]; then
      sources+=("${input}")
    elif [[ -f "${input}.mmd" ]]; then
      sources+=("${input}.mmd")
    else
      echo "error: missing diagram source '${input}'" >&2
      exit 1
    fi
  done
fi

declare -a svgs
for src in "${sources[@]}"; do
  out_svg="svg/${src%.mmd}.svg"
  echo "render ${src} -> ${out_svg}"
  "${MMDC_BIN}" -i "${src}" -o "${out_svg}" -c mermaid-config.json
  svgs+=("${out_svg}")
done

python3 scripts/normalize_phase_rect_spans.py "${svgs[@]}"

# Per-diagram top-header spacing overrides.
# This is intentionally per-file because Mermaid's top area geometry differs
# between diagrams (actor-man vs participants-only, bundle width/count, etc.).
for svg in "${svgs[@]}"; do
  gap=""
  case "${svg}" in
    svg/sequence-execution-runtime.svg)
      gap="25.0"
      ;;
    svg/sequence-policy-authoring-prepare.svg)
      gap="25.0"
      ;;
    svg/sequence-maintenance-runtime.svg)
      gap="25.0"
      ;;
    svg/sequence-lifecycle-v2-normalized.svg)
      gap="25.0"
      ;;
  esac
  if [[ -n "${gap}" ]]; then
    python3 scripts/align_reverse_sequence_labels.py --bundle-title-gap "${gap}" "${svg}"
  else
    python3 scripts/align_reverse_sequence_labels.py "${svg}"
  fi
done

# Flowchart-specific cluster span normalization.
for svg in "${svgs[@]}"; do
  case "${svg}" in
    svg/flowchart-windowed-upload-scheduler.svg)
      python3 scripts/normalize_flowchart_cluster_spans.py "${svg}"
      python3 scripts/center_flowchart_sections.py "${svg}"
      ;;
    svg/state-run-lifecycle.svg)
      python3 scripts/compact_state_self_loop.py "${svg}"
      ;;
  esac
done

echo "render complete: ${#svgs[@]} svg files"
