#!/usr/bin/env python3
import logging
import os
import time

import grpc

import fleet_gateway_pb2
import fleet_gateway_pb2_grpc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger("sim-device")


def main():
    mac = os.environ.get("DEVICE_MAC", "02:42:ac:14:00:05")
    gateway_host = os.environ.get("GATEWAY_HOST", "fleet-gateway")
    gateway_port = int(os.environ.get("GATEWAY_PORT", "50060"))

    target = f"{gateway_host}:{gateway_port}"
    logger.info(
        "Simulated device starting with MAC %s, gateway %s",
        mac,
        target,
    )

    channel = grpc.insecure_channel(target)
    stub = fleet_gateway_pb2_grpc.FleetGatewayStub(channel)

    max_attempts = 30
    for attempt in range(1, max_attempts + 1):
        logger.info("Attempt %d to call DiscoverByMac...", attempt)
        try:
            response = stub.DiscoverByMac(
                fleet_gateway_pb2.DiscoverRequest(mac_address=mac),
                timeout=10,
            )
            logger.info(
                "DiscoverByMac response: success=%s message=%s mac_address=%s resource_id=%s",
                response.success,
                response.message,
                response.mac_address,
                response.resource_id,
            )
            break
        except grpc.RpcError as exc:  # noqa: BLE001
            logger.error(
                "Error calling DiscoverByMac (attempt %d/%d): %s",
                attempt,
                max_attempts,
                exc,
            )
            time.sleep(2)


if __name__ == "__main__":
    main()
