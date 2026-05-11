FROM python:3.12-slim

WORKDIR /app

COPY src/device-sim/requirements.txt ./requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

COPY shared/proto_gen/fleet_gateway_pb2.py \
     shared/proto_gen/fleet_gateway_pb2_grpc.py \
     shared/proto_gen/fleet_gateway_v2_pb2.py \
     shared/proto_gen/fleet_gateway_v2_pb2_grpc.py \
     shared/proto_gen/fleet_gateway_v3_pb2.py \
     shared/proto_gen/fleet_gateway_v3_pb2_grpc.py \
     ./
COPY src/device-sim/sim_device_client.py ./sim_device_client.py
COPY src/device-sim/agent_v2_client.py ./agent_v2_client.py
COPY src/device-sim/agent_v2 ./agent_v2

CMD ["python", "-u", "sim_device_client.py"]
