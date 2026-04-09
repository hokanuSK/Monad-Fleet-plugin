#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.aws}"
WORK_DIR="${WORK_DIR:-/tmp/fleetmanager-elabimg-build}"
ELABIMG_REPO="${ELABIMG_REPO:-https://github.com/elabftw/elabimg.git}"
ELABFTW_OWNER="${ELABFTW_OWNER:-hokanuSK}"
ELABFTW_REPO="${ELABFTW_REPO:-elabftw}"
ELABFTW_VERSION="${ELABFTW_VERSION:-hypernext}"
ELAB_WEB_IMAGE="${ELAB_WEB_IMAGE:-elabftw/elabimg:custom}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git not found in PATH" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found in PATH" >&2
  exit 1
fi

rm -rf "$WORK_DIR"
git clone --depth 1 "$ELABIMG_REPO" "$WORK_DIR"

dockerfile="$WORK_DIR/Dockerfile"
sed -i.bak \
  "s|https://github.com/elabftw/elabftw/tarball/\$ELABFTW_VERSION|https://github.com/${ELABFTW_OWNER}/${ELABFTW_REPO}/tarball/\$ELABFTW_VERSION|g" \
  "$dockerfile"
sed -i.bak "s|mv elabftw-\\* src|mv *elabftw* src|g" "$dockerfile"
rm -f "$dockerfile.bak"

docker build \
  --build-arg ELABFTW_VERSION="$ELABFTW_VERSION" \
  -t "$ELAB_WEB_IMAGE" \
  "$WORK_DIR"

echo "Built image: $ELAB_WEB_IMAGE"
