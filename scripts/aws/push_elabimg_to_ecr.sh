#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.aws}"
REGION="${AWS_REGION:-eu-north-1}"
REPO_NAME="fleetmanager-elabimg"
LOCAL_IMAGE="elabftw/elabimg:custom"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_URI="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO_NAME:custom"

echo "Creating ECR repository if it doesn't exist..."
aws ecr describe-repositories --repository-names "$REPO_NAME" --region "$REGION" 2>/dev/null || \
  aws ecr create-repository --repository-name "$REPO_NAME" --region "$REGION"

echo "Authenticating Docker to ECR..."
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"

echo "Tagging image..."
docker tag "$LOCAL_IMAGE" "$ECR_URI"

echo "Pushing image to ECR..."
docker push "$ECR_URI"

echo "Image pushed: $ECR_URI"
echo "Set ELAB_WEB_IMAGE=$ECR_URI in .env.aws for deployment"