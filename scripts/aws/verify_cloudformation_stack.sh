#!/usr/bin/env bash
set -euo pipefail

STACK_NAME="${STACK_NAME:-fleetmanager-ec2}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-north-1}}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-60}"
SLEEP_SECONDS="${SLEEP_SECONDS:-10}"
ELAB_WEB_PORT="${ELAB_WEB_PORT:-9443}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
FLEET_METRICS_PORT="${FLEET_METRICS_PORT:-9108}"

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI not found in PATH" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "curl not found in PATH" >&2
  exit 1
fi

stack_status="$(aws cloudformation describe-stacks \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].StackStatus' \
  --output text)"

case "$stack_status" in
  CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE)
    ;;
  *)
    echo "Stack '$STACK_NAME' is not in a ready state: $stack_status" >&2
    exit 1
    ;;
esac

public_ip="$(aws cloudformation describe-stacks \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='PublicIp'].OutputValue | [0]" \
  --output text)"
public_dns="$(aws cloudformation describe-stacks \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='PublicDnsName'].OutputValue | [0]" \
  --output text)"

host="$public_ip"
if [[ -z "$host" || "$host" == "None" ]]; then
  host="$public_dns"
fi
if [[ -z "$host" || "$host" == "None" ]]; then
  echo "Cannot determine host from stack outputs (PublicIp/PublicDnsName)." >&2
  exit 1
fi

echo "Using host: $host"

wait_for_http() {
  local url="$1"
  local insecure="$2"
  local attempt=1
  local code=""
  local curl_flags=(-sS -L -o /dev/null -w '%{http_code}')
  if [[ "$insecure" == "true" ]]; then
    curl_flags+=(-k)
  fi

  until (( attempt > MAX_ATTEMPTS )); do
    code="$(curl "${curl_flags[@]}" "$url" || true)"
    if [[ "$code" == "200" ]]; then
      echo "OK  $url -> $code"
      return 0
    fi
    echo "WAIT $url -> ${code:-curl-error} (attempt $attempt/$MAX_ATTEMPTS)"
    sleep "$SLEEP_SECONDS"
    ((attempt++))
  done

  echo "HTTP check failed: $url (last code: ${code:-curl-error})" >&2
  return 1
}

wait_for_http "https://$host:${ELAB_WEB_PORT}/login.php" "true"
wait_for_http "http://$host:${GRAFANA_PORT}/api/health" "false"
wait_for_http "http://$host:${FLEET_METRICS_PORT}/metrics" "false"

echo
echo "CloudFormation stack verification successful."
