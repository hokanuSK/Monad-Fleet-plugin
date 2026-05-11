#!/usr/bin/env bash
set -euo pipefail

# Resets only Monad Fleet state and observability data.
# Does NOT touch MySQL/eLabFTW database volumes.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/infrastructure/docker-compose.yml}"

RESET_STATE="${RESET_STATE:-true}"
RESET_METRICS="${RESET_METRICS:-true}"
RESET_GRAFANA="${RESET_GRAFANA:-false}"

if [[ "${RESET_STATE}" == "true" ]]; then
  echo "[1/3] Reset Fleet service state"
  docker compose exec -T monad-fleet-service sh -lc "rm -f /data/state.json /data/ingest-metrics.ndjson || true"
  docker compose restart monad-fleet-service >/dev/null
else
  echo "[1/3] Skip Fleet state reset (RESET_STATE=${RESET_STATE})"
fi

if [[ "${RESET_METRICS}" == "true" ]]; then
  echo "[2/3] Reset Prometheus + Mimir data"
  if docker compose ps --services --status running | grep -qx "prometheus"; then
    docker compose exec -T prometheus sh -lc "rm -rf /prometheus/* || true"
    docker compose restart prometheus >/dev/null
  else
    echo "Prometheus is not running; skipping Prometheus reset."
  fi
  if docker compose ps --services --status running | grep -qx "mimir"; then
    docker compose exec -T mimir sh -lc "rm -rf /data/* || true"
    docker compose restart mimir >/dev/null
  else
    echo "Mimir is not running; skipping Mimir reset."
  fi
else
  echo "[2/3] Skip metrics reset (RESET_METRICS=${RESET_METRICS})"
fi

if [[ "${RESET_GRAFANA}" == "true" ]]; then
  echo "[3/3] Reset Grafana local data"
  docker compose exec -T grafana sh -lc "rm -rf /var/lib/grafana/* || true"
  docker compose restart grafana >/dev/null
else
  echo "[3/3] Skip Grafana reset (RESET_GRAFANA=${RESET_GRAFANA})"
fi

echo "Reset complete."
