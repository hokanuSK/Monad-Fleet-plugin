#!/usr/bin/env bash
set -euo pipefail

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:55061 -sTCP:LISTEN >/dev/null 2>&1; then
  printf '%s\n' "55061"
  exit 0
fi

if command -v nc >/dev/null 2>&1 && nc -z 127.0.0.1 55061 >/dev/null 2>&1; then
  printf '%s\n' "55061"
  exit 0
fi

printf '%s\n' "50060"
