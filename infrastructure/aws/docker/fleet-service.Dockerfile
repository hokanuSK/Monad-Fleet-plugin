FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends iproute2 net-tools tcpdump && \
    rm -rf /var/lib/apt/lists/*

COPY src/fleet-service/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY shared/proto ./proto
COPY src/fleet-service/gateway_server.py ./gateway_server.py
COPY src/fleet-service/monad_fleet_service ./monad_fleet_service

RUN python -m grpc_tools.protoc \
    -I./proto \
    --python_out=. \
    --grpc_python_out=. \
    proto/fleet_gateway.proto \
    proto/fleet_gateway_v2.proto

EXPOSE 50060
EXPOSE 9108

CMD ["python", "-u", "gateway_server.py"]
