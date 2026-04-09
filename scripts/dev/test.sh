#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

scripts/dev/verify_layout.sh

ran_tests=0
for root in apps scripts tools; do
  if [[ -n "$(find "${root}" -type f -name 'test_*.py' -print -quit)" ]]; then
    echo "[test] python unittest discover in ${root}/"
    python3 -m unittest discover -s "${root}" -p 'test_*.py'
    ran_tests=1
  fi
done

if (( ran_tests == 0 )); then
  echo "[test] No python unit tests discovered under apps/, scripts/, tools/."
fi

echo "[test] OK"
