FROM python:3.12-slim

WORKDIR /app

COPY apps/device-sim/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY proto ./proto
COPY apps/device-sim/sim_device_client.py ./sim_device_client.py
COPY apps/device-sim/agent_v2_client.py ./agent_v2_client.py
COPY apps/device-sim/agent_v2 ./agent_v2

RUN python -m grpc_tools.protoc \
    -I./proto \
    --python_out=. \
    --grpc_python_out=. \
    proto/fleet_gateway.proto \
    proto/fleet_gateway_v2.proto

CMD ["python", "-u", "sim_device_client.py"]
