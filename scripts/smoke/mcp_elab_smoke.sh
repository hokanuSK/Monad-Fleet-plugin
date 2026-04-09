#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"
export COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/infrastructure/docker-compose.yml}"

MODE="${MODE:-local}" # local|compose

if [[ "${MODE}" == "local" ]]; then
  CMD=(python3 scripts/elab-mcp/server.py)
elif [[ "${MODE}" == "compose" ]]; then
  CMD=(docker compose run --rm -T elab-mcp)
else
  echo "Unsupported MODE='${MODE}'. Expected local|compose."
  exit 1
fi

echo "Running eLab MCP smoke test (mode=${MODE})"
python3 scripts/elab-mcp/smoke_client.py -- "${CMD[@]}"
