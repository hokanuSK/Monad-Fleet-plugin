#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

if command -v shfmt >/dev/null 2>&1; then
  echo "[format] shfmt"
  shell_files=()
  while IFS= read -r file; do
    shell_files+=("${file}")
  done < <(git ls-files '*.sh')
  if (( ${#shell_files[@]} > 0 )); then
    shfmt -w -i 2 -sr "${shell_files[@]}"
  fi
else
  echo "WARN: shfmt is not installed; skipping shell formatting."
fi

if command -v ruff >/dev/null 2>&1; then
  echo "[format] ruff format"
  ruff format apps scripts tools
else
  echo "WARN: ruff is not installed; skipping Python formatting."
fi

echo "[format] Done"
