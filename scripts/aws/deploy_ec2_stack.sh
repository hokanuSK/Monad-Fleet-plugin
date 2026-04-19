#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.aws}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/infrastructure/aws/docker-compose.aws.yml}"
BUILD_IMAGES="${BUILD_IMAGES:-true}"
BUILD_ELAB_IMAGE="${BUILD_ELAB_IMAGE:-false}"
RUN_BOOTSTRAP="${RUN_BOOTSTRAP:-true}"
RUN_VERIFY="${RUN_VERIFY:-true}"
ENABLE_REVERSE_PROXY="${ENABLE_REVERSE_PROXY:-false}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  echo "Copy .env.aws.example to .env.aws and fill it before deploy." >&2
  exit 1
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Missing compose file: $COMPOSE_FILE" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found in PATH" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose plugin is required" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

required_vars=(
  MYSQL_ROOT_PASSWORD
  MYSQL_PASSWORD
  ELAB_SECRET_KEY
  ELAB_SITE_URL
  ELAB_SERVER_NAME
  ELAB_API_KEY
  GRAFANA_ADMIN_PASSWORD
  ELAB_ADMIN_EMAIL
  ELAB_ADMIN_FIRSTNAME
  ELAB_ADMIN_LASTNAME
  ELAB_ADMIN_PASSWORD
  ELAB_TEAM_NAME
)

for var_name in "${required_vars[@]}"; do
  if [[ -z "${!var_name:-}" ]]; then
    echo "Missing required value in env file: $var_name" >&2
    exit 1
  fi
done

compose_cmd=(docker compose --project-directory "$ROOT_DIR" --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
if [[ "$ENABLE_REVERSE_PROXY" == "true" ]]; then
  compose_cmd+=(--profile proxy)
fi

if [[ "$BUILD_ELAB_IMAGE" == "true" ]]; then
  ENV_FILE="$ENV_FILE" "$ROOT_DIR/scripts/aws/build_elabimg_hypernext.sh"
fi

if [[ "$BUILD_IMAGES" == "true" ]]; then
  "${compose_cmd[@]}" build monad-fleet-service model-device
fi

"${compose_cmd[@]}" up -d

if [[ "$RUN_BOOTSTRAP" == "true" ]]; then
  ENV_FILE="$ENV_FILE" COMPOSE_FILE="$COMPOSE_FILE" "$ROOT_DIR/scripts/aws/elabftw_bootstrap_admin.sh"
fi

if [[ "$RUN_VERIFY" == "true" ]]; then
  ENV_FILE="$ENV_FILE" COMPOSE_FILE="$COMPOSE_FILE" "$ROOT_DIR/scripts/aws/verify_ec2_stack.sh"
fi

echo "Deploy complete."
echo "eLabFTW: ${ELAB_SITE_URL}"
echo "Grafana: http://$(hostname -I | awk '{print $1}'):${GRAFANA_PORT:-3000}"
if [[ "$ENABLE_REVERSE_PROXY" == "true" ]]; then
  echo "Reverse proxy enabled (Caddy):"
  echo "  eLab host: https://${ELAB_HOST}"
  echo "  Grafana host: https://${GRAFANA_HOST}"
  echo "  Mimir host: https://${MIMIR_HOST}"
  echo "  Fleet metrics host: https://${FLEET_METRICS_HOST}"
fi
