#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.aws}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/docker-compose.aws.yml}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-60}"
SLEEP_SECONDS="${SLEEP_SECONDS:-5}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

required_vars=(
  MYSQL_ROOT_PASSWORD
  MYSQL_DATABASE
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

compose_cmd=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")

attempt=1
until "${compose_cmd[@]}" exec -T -e MYSQL_ROOT_PASSWORD="$MYSQL_ROOT_PASSWORD" mysql sh -lc \
  'mysqladmin ping -h 127.0.0.1 -uroot -p"$MYSQL_ROOT_PASSWORD" --silent' >/dev/null 2>&1; do
  if (( attempt > MAX_ATTEMPTS )); then
    echo "MySQL did not become ready in time." >&2
    exit 1
  fi
  sleep "$SLEEP_SECONDS"
  ((attempt++))
done

attempt=1
until "${compose_cmd[@]}" exec -T web sh -lc 'test -x /elabftw/bin/init' >/dev/null 2>&1; do
  if (( attempt > MAX_ATTEMPTS )); then
    echo "eLabFTW container is not ready in time." >&2
    exit 1
  fi
  sleep "$SLEEP_SECONDS"
  ((attempt++))
done

users_count_raw="$("${compose_cmd[@]}" exec -T \
  -e MYSQL_ROOT_PASSWORD="$MYSQL_ROOT_PASSWORD" \
  -e MYSQL_DATABASE="$MYSQL_DATABASE" \
  mysql sh -lc \
  'mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" -D"$MYSQL_DATABASE" -e "SELECT COUNT(*) FROM users;" 2>/dev/null || true' \
  | tr -d '\r' | tail -n 1)"

users_count="${users_count_raw:-0}"
if ! [[ "$users_count" =~ ^[0-9]+$ ]]; then
  users_count=0
fi

if (( users_count > 0 )); then
  echo "Bootstrap skipped: ${users_count} user(s) already exist."
  exit 0
fi

echo "Creating initial eLabFTW admin user (first install path)..."
"${compose_cmd[@]}" exec -T \
  -e ELAB_ADMIN_EMAIL="$ELAB_ADMIN_EMAIL" \
  -e ELAB_ADMIN_FIRSTNAME="$ELAB_ADMIN_FIRSTNAME" \
  -e ELAB_ADMIN_LASTNAME="$ELAB_ADMIN_LASTNAME" \
  -e ELAB_ADMIN_PASSWORD="$ELAB_ADMIN_PASSWORD" \
  -e ELAB_TEAM_NAME="$ELAB_TEAM_NAME" \
  web sh -lc \
  'bin/init db:install -n --reset \
    -e "$ELAB_ADMIN_EMAIL" \
    -f "$ELAB_ADMIN_FIRSTNAME" \
    -l "$ELAB_ADMIN_LASTNAME" \
    -p "$ELAB_ADMIN_PASSWORD" \
    -t "$ELAB_TEAM_NAME" >/dev/null'

echo "Initial admin bootstrap finished."
