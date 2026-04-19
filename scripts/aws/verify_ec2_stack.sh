#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.aws}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/infrastructure/aws/docker-compose.aws.yml}"
ELAB_CURL_INSECURE="${ELAB_CURL_INSECURE:-true}"
ENABLE_REVERSE_PROXY="${ENABLE_REVERSE_PROXY:-false}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

compose_cmd=(docker compose --project-directory "$ROOT_DIR" --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
if [[ "$ENABLE_REVERSE_PROXY" == "true" ]]; then
  compose_cmd+=(--profile proxy)
fi

required_services=(
  mysql
  web
  monad-fleet-service
  model-device
  mimir
  grafana
)

running_services="$("${compose_cmd[@]}" ps --services --status running | tr -d '\r')"
for service_name in "${required_services[@]}"; do
  if ! grep -qx "$service_name" <<<"$running_services"; then
    echo "Service not running: $service_name" >&2
    exit 1
  fi
done

curl_flags=(-sS -o /dev/null -w '%{http_code}')
if [[ "$ELAB_CURL_INSECURE" == "true" ]]; then
  curl_flags+=(-k)
fi

elab_base="${ELAB_SITE_URL%/}"
checks=(
  "$elab_base/login.php"
  "$elab_base/assets/vendor.bundle.js"
  "$elab_base/assets/main.bundle.js"
  "$elab_base/assets/vendor.min.css"
  "$elab_base/assets/elabftw.min.css"
)

for url in "${checks[@]}"; do
  code="$(curl "${curl_flags[@]}" "$url")"
  if [[ "$code" != "200" ]]; then
    echo "HTTP check failed: $url -> $code" >&2
    exit 1
  fi
done

metrics_code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${FLEET_METRICS_PORT:-9108}/metrics")"
if [[ "$metrics_code" != "200" ]]; then
  echo "Fleet metrics check failed on port ${FLEET_METRICS_PORT:-9108}: $metrics_code" >&2
  exit 1
fi

grafana_code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${GRAFANA_PORT:-3000}/api/health")"
if [[ "$grafana_code" != "200" ]]; then
  echo "Grafana health check failed on port ${GRAFANA_PORT:-3000}: $grafana_code" >&2
  exit 1
fi

if [[ "$ENABLE_REVERSE_PROXY" == "true" ]]; then
  proxy_elab_code="$(
    curl -k -sS -o /dev/null -w '%{http_code}' \
      -H "Host: ${ELAB_HOST}" \
      "https://127.0.0.1:${PROXY_HTTPS_PORT:-443}/login.php"
  )"
  if [[ "$proxy_elab_code" != "200" ]]; then
    echo "Reverse proxy eLab check failed for host ${ELAB_HOST}: $proxy_elab_code" >&2
    exit 1
  fi

  proxy_grafana_code="$(
    curl -k -sS -o /dev/null -w '%{http_code}' \
      -H "Host: ${GRAFANA_HOST}" \
      "https://127.0.0.1:${PROXY_HTTPS_PORT:-443}/api/health"
  )"
  if [[ "$proxy_grafana_code" != "200" ]]; then
    echo "Reverse proxy Grafana check failed for host ${GRAFANA_HOST}: $proxy_grafana_code" >&2
    exit 1
  fi
fi

echo "Verification successful."
