import json
import logging
import os
from concurrent import futures

import grpc
import requests

import fleet_gateway_pb2
import fleet_gateway_pb2_grpc


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("fleet-gateway")


class ElabFTWClient:
    """
    Jednoduchý klient na volanie eLabFTW REST API.
    """

    def __init__(self, base_url: str, api_key: str | None, verify_tls: bool = False):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._verify_tls = verify_tls

        log.info(
            "Configuring eLabFTW client with base_url=%s verify_tls=%s",
            self._base_url,
            self._verify_tls,
        )
        if not self._api_key:
            log.warning(
                "ELAB_API_KEY is not set; eLabFTW API calls will fail with 401 Unauthorized."
            )

    def _request(self, method: str, path: str, **kwargs):
        """
        Zabali requests.request tak, aby vždy použil base_url a Authorization header.
        path očakávame vo forme '/items', nie '/api/v2/items'.
        """
        url = f"{self._base_url}{path}"

        headers = kwargs.pop("headers", {}) or {}
        if self._api_key:
            # eLabFTW očakáva celý API key reťazec v Authorization headeri
            headers["Authorization"] = self._api_key

        log.debug("eLabFTW API %s %s", method, url)

        resp = requests.request(
            method,
            url,
            headers=headers,
            timeout=10,
            verify=self._verify_tls,
            **kwargs,
        )
        resp.raise_for_status()
        return resp

    def create_device_resource(self, mac: str) -> dict:
        """
        Vytvorí nový Resource item v eLabFTW pre daný MAC.
        """
        title = f"Device {mac}"
        body = json.dumps({"mac": mac})

        payload = {
            "type": "resources",
            "title": title,
            "body": body,
        }

        log.info("Creating eLabFTW resource for MAC %s", mac)
        resp = self._request("POST", "/items", json=payload)
        data = resp.json()
        log.info("Created eLabFTW resource id=%s for MAC %s", data.get("id"), mac)
        return data

    def _find_device_by_mac(self, mac: str) -> dict | None:
        """
        Nájde resource podľa MAC tak, že:
        1) cez 'search' zúži výsledky,
        2) ale reálne uzná len taký item, ktorý má v body JSON presne rovnaký 'mac'.
        """
        params = {
            "type": "resources",
            "search": mac,
            "limit": 25,  # vezmeme viac výsledkov a potom filtrujeme podľa body.mac
        }
        log.info("Searching eLabFTW resource for MAC %s", mac)
        resp = self._request("GET", "/items", params=params)
        data = resp.json()

        if not data:
            log.info("No candidates returned from eLabFTW for MAC %s", mac)
            return None

        for item in data:
            body_raw = item.get("body") or ""
            try:
                meta = json.loads(body_raw)
            except Exception:
                continue

            if (
                isinstance(meta, dict)
                and str(meta.get("mac", "")).strip().lower() == mac
            ):
                log.info(
                    "Found existing eLabFTW resource id=%s with exact MAC %s",
                    item.get("id"),
                    mac,
                )
                return item

        log.info(
            "No eLabFTW resource with exact MAC %s found among %d candidates",
            mac,
            len(data),
        )
        return None

    def get_or_create_device(self, mac: str) -> dict:
        """
        Vráti existujúci Resource pre MAC, alebo ho vytvorí.
        """
        existing = self._find_device_by_mac(mac)
        if existing is not None:
            return existing
        return self.create_device_resource(mac)


class FleetGatewayServicer(fleet_gateway_pb2_grpc.FleetGatewayServicer):
    def __init__(self, elab_client: ElabFTWClient):
        self._elab_client = elab_client

    def DiscoverByMac(self, request, context):
        mac = (request.mac_address or "").strip().lower()
        log.info("DiscoverByMac called with mac_address=%s", mac)

        if not mac:
            msg = "MAC address is empty"
            log.error(msg)
            return fleet_gateway_pb2.DiscoverResponse(
                success=False,
                message=msg,
                mac_address=mac,
                resource_id="",
            )

        try:
            item = self._elab_client.get_or_create_device(mac)
            resource_id = str(item.get("id", ""))

            log.info(
                "Returning success for MAC %s, resource_id=%s",
                mac,
                resource_id,
            )
            return fleet_gateway_pb2.DiscoverResponse(
                success=True,
                message="OK",
                mac_address=mac,
                resource_id=resource_id,
            )

        except Exception as e:
            msg = f"Failed to create/find resource in eLabFTW: {e}"
            log.exception("Failed to create/find resource in eLabFTW")
            context.set_details(msg)
            context.set_code(grpc.StatusCode.INTERNAL)
            return fleet_gateway_pb2.DiscoverResponse(
                success=False,
                message=msg,
                mac_address=mac,
                resource_id="",
            )


def serve():
    base_url = os.environ.get("ELAB_BASE_URL", "https://web/api/v2")
    api_key = os.environ.get("ELAB_API_KEY")
    verify_env = os.environ.get("ELAB_VERIFY_TLS", "false").strip().lower()
    verify_tls = verify_env in ("1", "true", "yes")

    elab_client = ElabFTWClient(base_url=base_url, api_key=api_key, verify_tls=verify_tls)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    fleet_gateway_pb2_grpc.add_FleetGatewayServicer_to_server(
        FleetGatewayServicer(elab_client),
        server,
    )

    port = int(os.environ.get("GATEWAY_PORT", "50060"))
    listen_addr = f"[::]:{port}"
    server.add_insecure_port(listen_addr)

    log.info("Starting FleetGateway gRPC server on %s", listen_addr)
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
