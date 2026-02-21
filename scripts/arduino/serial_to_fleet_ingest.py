#!/usr/bin/env python3
"""Forward newline-delimited JSON metrics from serial (or stdin) to Fleet ingest."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Iterable
from urllib import request
from urllib.error import HTTPError, URLError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Forward Arduino/serial metrics to Fleet HTTP ingest.")
    parser.add_argument("--fleet-url", default="http://127.0.0.1:9108/ingest/v1/metrics")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--source", default="arduino-serial")
    parser.add_argument("--token", default="")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--serial-port", default="")
    parser.add_argument("--stdin", action="store_true", help="Read newline JSON from stdin instead of serial.")
    parser.add_argument("--retry-s", type=float, default=2.0)
    return parser.parse_args()


def build_payload(line_data: dict[str, Any], device_id: str, source: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "device_id": line_data.get("device_id") or device_id,
        "source": line_data.get("source") or source,
    }
    if isinstance(line_data.get("metrics"), list):
        payload["metrics"] = line_data["metrics"]
    elif isinstance(line_data.get("values"), dict):
        payload["values"] = line_data["values"]
    else:
        metrics = []
        for key, value in line_data.items():
            if key in {"device_id", "source", "ts_ms"}:
                continue
            if isinstance(value, (int, float)):
                metrics.append({"name": key, "value": float(value)})
        payload["metrics"] = metrics
    return payload


def post_payload(url: str, payload: dict[str, Any], token: str) -> None:
    raw = json.dumps(payload).encode("utf-8")
    headers = {"content-type": "application/json"}
    if token:
        headers["x-ingest-token"] = token
    req = request.Request(url, data=raw, headers=headers, method="POST")
    with request.urlopen(req, timeout=8) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        print(f"[fleet-ingest] status={resp.status} body={body}")


def iter_lines_from_serial(port: str, baud: int) -> Iterable[str]:
    try:
        import serial  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pyserial is required for --serial-port mode (pip install pyserial)") from exc

    with serial.Serial(port=port, baudrate=baud, timeout=1) as ser:
        while True:
            raw = ser.readline()
            if not raw:
                continue
            yield raw.decode("utf-8", errors="replace").strip()


def iter_lines_from_stdin() -> Iterable[str]:
    for raw in sys.stdin:
        yield raw.strip()


def main() -> int:
    args = parse_args()
    if not args.stdin and not args.serial_port:
        print("error: provide --stdin or --serial-port", file=sys.stderr)
        return 2

    line_iter = iter_lines_from_stdin() if args.stdin else iter_lines_from_serial(args.serial_port, args.baud)

    for line in line_iter:
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            print(f"[skip] not-json line: {line}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            print(f"[skip] JSON must be object: {line}", file=sys.stderr)
            continue
        payload = build_payload(data, args.device_id, args.source)
        try:
            post_payload(args.fleet_url, payload, args.token)
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"[retry] ingest failed: {exc}", file=sys.stderr)
            time.sleep(max(0.2, args.retry_s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
