#!/usr/bin/env bash
# Regenerate gRPC/protobuf Python stubs from shared/proto/ into shared/proto_gen/.
#
# Run this whenever any *.proto file changes, then commit the output.
# Requires: pip install grpcio-tools  (or run inside a venv that has it)
#
# Usage: scripts/generate_proto.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROTO_DIR="$REPO_ROOT/shared/proto"
OUT_DIR="$REPO_ROOT/shared/proto_gen"

mkdir -p "$OUT_DIR"

python3 -m grpc_tools.protoc \
  -I"$PROTO_DIR" \
  --python_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$PROTO_DIR/fleet_gateway.proto" \
  "$PROTO_DIR/fleet_gateway_v2.proto" \
  "$PROTO_DIR/fleet_gateway_v3.proto"

echo "Generated stubs in $OUT_DIR:"
ls -lh "$OUT_DIR"/*.py
