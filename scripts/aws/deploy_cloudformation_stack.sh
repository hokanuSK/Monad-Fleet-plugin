#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLATE_FILE="${TEMPLATE_FILE:-$ROOT_DIR/infrastructure/aws/cloudformation/fleetmanager-ec2.yml}"

STACK_NAME="${STACK_NAME:-fleetmanager-ec2}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-north-1}}"
ENVIRONMENT_NAME="${ENVIRONMENT_NAME:-fleetmanager}"
VPC_ID="${VPC_ID:-}"
SUBNET_ID="${SUBNET_ID:-}"
KEY_PAIR_NAME="${KEY_PAIR_NAME:-}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.small}"
ROOT_VOLUME_SIZE_GIB="${ROOT_VOLUME_SIZE_GIB:-30}"
SSH_INGRESS_CIDR="${SSH_INGRESS_CIDR:-0.0.0.0/0}"
PUBLIC_WEB_INGRESS_CIDR="${PUBLIC_WEB_INGRESS_CIDR:-0.0.0.0/0}"
ENABLE_DIRECT_SERVICE_ACCESS="${ENABLE_DIRECT_SERVICE_ACCESS:-true}"
ATTACH_ELASTIC_IP="${ATTACH_ELASTIC_IP:-true}"
REPO_URL="${REPO_URL:-https://github.com/hokanuSK/Monad-Fleet-plugin.git}"
REPO_REF="${REPO_REF:-develop}"
ENV_SSM_PARAMETER_NAME="${ENV_SSM_PARAMETER_NAME:-}"
BUILD_ELAB_IMAGE="${BUILD_ELAB_IMAGE:-false}"
ENABLE_REVERSE_PROXY="${ENABLE_REVERSE_PROXY:-true}"
NO_EXECUTE_CHANGESET="${NO_EXECUTE_CHANGESET:-false}"

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI not found in PATH" >&2
  exit 1
fi

if [[ ! -f "$TEMPLATE_FILE" ]]; then
  echo "CloudFormation template not found: $TEMPLATE_FILE" >&2
  exit 1
fi

if [[ -z "$ENV_SSM_PARAMETER_NAME" ]]; then
  echo "Set ENV_SSM_PARAMETER_NAME to the SSM SecureString name with .env.aws content." >&2
  exit 1
fi

if [[ -z "$VPC_ID" ]]; then
  VPC_ID="$(aws ec2 describe-vpcs \
    --region "$REGION" \
    --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' \
    --output text)"
fi
if [[ -z "$VPC_ID" || "$VPC_ID" == "None" ]]; then
  echo "VPC_ID is required (default VPC was not found)." >&2
  exit 1
fi

if [[ -z "$SUBNET_ID" ]]; then
  SUBNET_ID="$(aws ec2 describe-subnets \
    --region "$REGION" \
    --filters Name=vpc-id,Values="$VPC_ID" Name=default-for-az,Values=true \
    --query 'Subnets | sort_by(@, &AvailabilityZone)[0].SubnetId' \
    --output text)"
fi
if [[ -z "$SUBNET_ID" || "$SUBNET_ID" == "None" ]]; then
  echo "SUBNET_ID is required (no default subnet found in VPC $VPC_ID)." >&2
  exit 1
fi

parameter_overrides=(
  "EnvironmentName=$ENVIRONMENT_NAME"
  "VpcId=$VPC_ID"
  "SubnetId=$SUBNET_ID"
  "KeyPairName=$KEY_PAIR_NAME"
  "InstanceType=$INSTANCE_TYPE"
  "RootVolumeSizeGiB=$ROOT_VOLUME_SIZE_GIB"
  "SshIngressCidr=$SSH_INGRESS_CIDR"
  "PublicWebIngressCidr=$PUBLIC_WEB_INGRESS_CIDR"
  "EnableDirectServiceAccess=$ENABLE_DIRECT_SERVICE_ACCESS"
  "AttachElasticIp=$ATTACH_ELASTIC_IP"
  "RepoUrl=$REPO_URL"
  "RepoRef=$REPO_REF"
  "EnvSsmParameterName=$ENV_SSM_PARAMETER_NAME"
  "BuildElabImage=$BUILD_ELAB_IMAGE"
  "EnableReverseProxy=$ENABLE_REVERSE_PROXY"
)

deploy_cmd=(
  aws cloudformation deploy
  --region "$REGION"
  --stack-name "$STACK_NAME"
  --template-file "$TEMPLATE_FILE"
  --capabilities CAPABILITY_NAMED_IAM
  --parameter-overrides "${parameter_overrides[@]}"
  --tags
  "project=FleetManager"
  "component=cloudformation"
  "env=$ENVIRONMENT_NAME"
)
if [[ "$NO_EXECUTE_CHANGESET" == "true" ]]; then
  deploy_cmd+=(--no-execute-changeset)
fi

echo "Deploying stack '$STACK_NAME' in region '$REGION'..."
"${deploy_cmd[@]}"

if [[ "$NO_EXECUTE_CHANGESET" == "true" ]]; then
  echo "Changeset created with --no-execute-changeset; stack was not updated."
  exit 0
fi

echo
echo "Stack outputs:"
aws cloudformation describe-stacks \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table
