#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PI_HOST="${PI_HOST:-${1:-monad-rpi5.local}}"
PI_USER="${PI_USER:-admin}"
PI_DIR="${PI_DIR:-/home/${PI_USER}/monad-fleet-agent}"
SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE:-}"
SSH_PROXY_JUMP="${SSH_PROXY_JUMP:-}"
SSH_USER_KNOWN_HOSTS_FILE="${SSH_USER_KNOWN_HOSTS_FILE:-}"
SSH_STRICT_HOST_KEY_CHECKING="${SSH_STRICT_HOST_KEY_CHECKING:-accept-new}"
USE_DEFAULT_SSH_KEY="${USE_DEFAULT_SSH_KEY:-false}"
DEFAULT_SSH_KEY="${ROOT_DIR}/scripts/rpi/keys/monad_rpi5_ed25519"
if [[ -z "${SSH_IDENTITY_FILE}" && "${USE_DEFAULT_SSH_KEY}" == "true" && -f "${DEFAULT_SSH_KEY}" ]]; then
  SSH_IDENTITY_FILE="${DEFAULT_SSH_KEY}"
fi

SSH_ARGS=()
SCP_ARGS=()
if [[ -n "${SSH_STRICT_HOST_KEY_CHECKING}" ]]; then
  SSH_ARGS+=(-o "StrictHostKeyChecking=${SSH_STRICT_HOST_KEY_CHECKING}")
  SCP_ARGS+=(-o "StrictHostKeyChecking=${SSH_STRICT_HOST_KEY_CHECKING}")
fi
if [[ -n "${SSH_USER_KNOWN_HOSTS_FILE}" ]]; then
  SSH_ARGS+=(-o "UserKnownHostsFile=${SSH_USER_KNOWN_HOSTS_FILE}")
  SCP_ARGS+=(-o "UserKnownHostsFile=${SSH_USER_KNOWN_HOSTS_FILE}")
fi
if [[ -n "${SSH_IDENTITY_FILE}" ]]; then
  SSH_ARGS+=(-i "${SSH_IDENTITY_FILE}" -o IdentitiesOnly=yes)
  SCP_ARGS+=(-i "${SSH_IDENTITY_FILE}" -o IdentitiesOnly=yes)
fi
if [[ -n "${SSH_PROXY_JUMP}" ]]; then
  SSH_ARGS+=(-J "${SSH_PROXY_JUMP}")
  SCP_ARGS+=(-J "${SSH_PROXY_JUMP}")
fi

run_ssh() {
  if [[ ${#SSH_ARGS[@]} -gt 0 ]]; then
    ssh "${SSH_ARGS[@]}" "$@"
  else
    ssh "$@"
  fi
}

run_scp() {
  if [[ ${#SCP_ARGS[@]} -gt 0 ]]; then
    scp "${SCP_ARGS[@]}" "$@"
  else
    scp "$@"
  fi
}

AGENT_SRC="${ROOT_DIR}/src/device-sim/agent_v2_client.py"
AGENT_PKG_SRC="${ROOT_DIR}/src/device-sim/agent_v2"
PROTO_V2_SRC="${ROOT_DIR}/shared/proto/fleet_gateway_v2.proto"
PROTO_V3_SRC="${ROOT_DIR}/shared/proto/fleet_gateway_v3.proto"

if [[ ! -f "${AGENT_SRC}" ]]; then
  echo "Missing file: ${AGENT_SRC}" >&2
  exit 1
fi
if [[ ! -d "${AGENT_PKG_SRC}" ]]; then
  echo "Missing directory: ${AGENT_PKG_SRC}" >&2
  exit 1
fi
if [[ ! -f "${PROTO_V2_SRC}" ]]; then
  echo "Missing file: ${PROTO_V2_SRC}" >&2
  exit 1
fi
if [[ ! -f "${PROTO_V3_SRC}" ]]; then
  echo "Missing file: ${PROTO_V3_SRC}" >&2
  exit 1
fi

echo "[1/4] Preparing directory on ${PI_USER}@${PI_HOST}:${PI_DIR}"
run_ssh "${PI_USER}@${PI_HOST}" "mkdir -p '${PI_DIR}'"

echo "[2/4] Copying agent and proto"
run_scp "${AGENT_SRC}" "${PI_USER}@${PI_HOST}:${PI_DIR}/agent_v2_client.py"
run_scp -r "${AGENT_PKG_SRC}" "${PI_USER}@${PI_HOST}:${PI_DIR}/"
run_scp "${PROTO_V2_SRC}" "${PI_USER}@${PI_HOST}:${PI_DIR}/fleet_gateway_v2.proto"
run_scp "${PROTO_V3_SRC}" "${PI_USER}@${PI_HOST}:${PI_DIR}/fleet_gateway_v3.proto"

echo "[3/4] Creating venv and installing dependencies"
run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
python3 -m venv \"${PI_DIR}/venv\"
\"${PI_DIR}/venv/bin/python\" -m pip install --upgrade pip grpcio grpcio-tools protobuf
'"

echo "[4/4] Generating protobuf stubs"
run_ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
\"${PI_DIR}/venv/bin/python\" -m grpc_tools.protoc \\
  -I\"${PI_DIR}\" \\
  --python_out=\"${PI_DIR}\" \\
  --grpc_python_out=\"${PI_DIR}\" \\
  \"${PI_DIR}/fleet_gateway_v2.proto\" \\
  \"${PI_DIR}/fleet_gateway_v3.proto\"
ls -1 \\
  \"${PI_DIR}/fleet_gateway_v2_pb2.py\" \\
  \"${PI_DIR}/fleet_gateway_v2_pb2_grpc.py\" \\
  \"${PI_DIR}/fleet_gateway_v3_pb2.py\" \\
  \"${PI_DIR}/fleet_gateway_v3_pb2_grpc.py\"
'"

echo "Pi agent deployment completed."
