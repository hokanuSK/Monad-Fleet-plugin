#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PI_HOST="${PI_HOST:-${1:-monad-rpi5.local}}"
PI_USER="${PI_USER:-admin}"
PI_DIR="${PI_DIR:-/home/${PI_USER}/monad-fleet-agent}"

AGENT_SRC="${ROOT_DIR}/device-sim/agent_v2_client.py"
PROTO_SRC="${ROOT_DIR}/device-sim/proto/fleet_gateway_v2.proto"

if [[ ! -f "${AGENT_SRC}" ]]; then
  echo "Missing file: ${AGENT_SRC}" >&2
  exit 1
fi
if [[ ! -f "${PROTO_SRC}" ]]; then
  echo "Missing file: ${PROTO_SRC}" >&2
  exit 1
fi

echo "[1/4] Preparing directory on ${PI_USER}@${PI_HOST}:${PI_DIR}"
ssh "${PI_USER}@${PI_HOST}" "mkdir -p '${PI_DIR}'"

echo "[2/4] Copying agent and proto"
scp "${AGENT_SRC}" "${PI_USER}@${PI_HOST}:${PI_DIR}/agent_v2_client.py"
scp "${PROTO_SRC}" "${PI_USER}@${PI_HOST}:${PI_DIR}/fleet_gateway_v2.proto"

echo "[3/4] Creating venv and installing dependencies"
ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
python3 -m venv \"${PI_DIR}/venv\"
\"${PI_DIR}/venv/bin/python\" -m pip install --upgrade pip grpcio grpcio-tools protobuf
'"

echo "[4/4] Generating protobuf stubs"
ssh "${PI_USER}@${PI_HOST}" "bash -lc '
set -euo pipefail
\"${PI_DIR}/venv/bin/python\" -m grpc_tools.protoc \\
  -I\"${PI_DIR}\" \\
  --python_out=\"${PI_DIR}\" \\
  --grpc_python_out=\"${PI_DIR}\" \\
  \"${PI_DIR}/fleet_gateway_v2.proto\"
ls -1 \"${PI_DIR}/fleet_gateway_v2_pb2.py\" \"${PI_DIR}/fleet_gateway_v2_pb2_grpc.py\"
'"

echo "Pi agent deployment completed."
