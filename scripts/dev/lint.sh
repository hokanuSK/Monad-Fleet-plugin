#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

echo "[lint] Shell syntax"
shell_files=()
while IFS= read -r file; do
  shell_files+=("${file}")
done < <(git ls-files '*.sh')
for file in "${shell_files[@]}"; do
  bash -n "${file}"
done

echo "[lint] Python syntax"
PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m compileall -q apps scripts tools

echo "[lint] YAML syntax"
yaml_files=()
while IFS= read -r file; do
  yaml_files+=("${file}")
done < <(git ls-files '*.yml' '*.yaml')
if (( ${#yaml_files[@]} > 0 )); then
  if python3 -c 'import yaml' >/dev/null 2>&1; then
    python3 - "${yaml_files[@]}" <<'PY'
import sys
from pathlib import Path

import yaml

errors = 0
for raw in sys.argv[1:]:
    path = Path(raw)
    try:
        with path.open("r", encoding="utf-8") as handle:
            yaml.safe_load(handle)
    except Exception as exc:  # pragma: no cover
        errors += 1
        print(f"YAML parse error: {path}: {exc}", file=sys.stderr)

if errors:
    raise SystemExit(1)
PY
  else
    echo "WARN: PyYAML is not installed; skipping YAML parse checks."
  fi
fi

echo "[lint] Docker Compose sanity"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  docker compose -f docker-compose.yml config -q
else
  echo "WARN: docker compose is not available; skipping compose sanity check."
fi

echo "[lint] OK"
