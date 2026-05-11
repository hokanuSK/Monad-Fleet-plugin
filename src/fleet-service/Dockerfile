FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends iproute2 net-tools tcpdump && \
    rm -rf /var/lib/apt/lists/*

COPY src/fleet-service/requirements.txt ./requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

COPY shared/proto_gen/fleet_gateway_pb2.py \
     shared/proto_gen/fleet_gateway_pb2_grpc.py \
     shared/proto_gen/fleet_gateway_v2_pb2.py \
     shared/proto_gen/fleet_gateway_v2_pb2_grpc.py \
     shared/proto_gen/fleet_gateway_v3_pb2.py \
     shared/proto_gen/fleet_gateway_v3_pb2_grpc.py \
     ./
COPY src/fleet-service/gateway_server.py ./gateway_server.py
COPY src/fleet-service/monad_fleet_service ./monad_fleet_service

EXPOSE 50060
EXPOSE 9108

CMD ["python", "-u", "gateway_server.py"]
