#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

required_dirs=(src infrastructure shared artifacts scripts docs)
for dir in "${required_dirs[@]}"; do
  if [[ ! -d "${dir}" ]]; then
    echo "ERROR: missing required directory '${dir}'." >&2
    exit 1
  fi
done

expected_links=(
  "data:artifacts/data"
)

failures=0
for entry in "${expected_links[@]}"; do
  link_name="${entry%%:*}"
  expected_target="${entry#*:}"
  if [[ ! -L "${link_name}" ]]; then
    echo "ERROR: expected symlink '${link_name}' -> '${expected_target}' is missing." >&2
    failures=$((failures + 1))
    continue
  fi
  actual_target="$(readlink "${link_name}")"
  if [[ "${actual_target}" != "${expected_target}" ]]; then
    echo "ERROR: symlink '${link_name}' points to '${actual_target}' (expected '${expected_target}')." >&2
    failures=$((failures + 1))
  fi
done

if (( failures > 0 )); then
  exit 1
fi

echo "[layout] OK"
