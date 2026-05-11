#!/usr/bin/env python3
"""Phase 4 concurrent-dispatch smoke test on monad-02.

Runs WIFI_SCAN (passive_monitor, RUN_ASYNC) and BLE_SCAN (advertise, RUN_ASYNC)
in the same command group so both capture concurrently for the full 3-min window.

Verification checklist after the run:
  - Agent log shows "passive_monitor started async" and "ble advertise started async"
    within a second of each other.
  - Grafana: monad_pi_wifi5g_capture_active=1 and monad_pi_ble_advertise_active=1
    overlap for the full 3-min window.
  - eLab bundle: wifi-5g-monitor.pcap (non-empty) + ble-tx-log.json (~90 entries
    at 2s interval) both present.

Run with: ELAB_API_KEY=<key> python3 create_monad02_phase4_concurrent_smoke.py
"""
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEVICE_ID = "24:eb:16:e3:6a:07"   # monad-02 wlan0
DEVICE_LABEL = "monad-02/10.200.0.11"

MEASURE_WINDOW_S = 180
WIFI_CHANNELS = "36,40,44,48,52,56,60,64,100,104,108,112,116,120,124,128,132,136,140,149,153,157,161,165"


def request(api_key, method, url, payload=None):
    data = None
    headers = {"Authorization": api_key}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"{method} {url} failed {exc.code}: {body}") from exc


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def build_policy(stamp):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    measure_from = now - timedelta(seconds=5)
    measure_to = measure_from + timedelta(seconds=MEASURE_WINDOW_S)
    upload_from = measure_to
    upload_to = upload_from + timedelta(minutes=5)
    range_to = upload_to + timedelta(minutes=1)
    group_id = f"monad02-phase4-concurrent-{stamp.lower()}"

    cmd_timeout_s = MEASURE_WINDOW_S - 5

    common_env = {
        "DISABLE_CSI_CAPTURE": "true",
        "REQUIRE_REAL_CSI": "false",
        "ARTIFACT_UPLOAD_DURING_MEASURE": "false",
        "ARTIFACT_UPLOAD_TARGET": "fleet_grpc",
        "GRPC_ARTIFACT_CHUNK_BYTES": "65536",
        "ENABLE_COMMAND_STATUS_REPORTS": "false",
        "ENABLE_UPLOAD_STATUS_REPORTS": "false",
    }
    upload_env = {
        "UPLOAD_WINDOW_FROM": iso(upload_from),
        "UPLOAD_WINDOW_TO": iso(upload_to),
        "UPLOAD_WINDOW_REQUIRED": "true",
        "UPLOAD_SLOT_COUNT": "1",
        "UPLOAD_SLOT_JITTER_S": "0",
    }

    commands = [
        {
            "id": "sync-phase4-window",
            "type": "SYNC",
            "timeout_s": 5,
            "retries": 0,
            "target_selector": {"device_ids": [DEVICE_ID]},
            "validity_policy_window": {"from": iso(measure_from), "to": iso(range_to)},
            "measure_window": {"from": iso(measure_from), "to": iso(measure_to)},
            "env": {**common_env, **upload_env, "SYNC_TIMEZONE": "UTC"},
        },
        # WIFI_SCAN passive_monitor — async so dispatcher continues immediately
        {
            "id": "wifi-scan-passive-monitor-async",
            "type": "WIFI_SCAN",
            "timeout_s": cmd_timeout_s,
            "retries": 0,
            "env": {
                **common_env,
                **upload_env,
                "WIFI_SCAN_MODE": "passive_monitor",
                "WIFI_SCAN_IFACE": "wlan0",
                "WIFI_SCAN_CHANNELS": WIFI_CHANNELS,
                "WIFI_SCAN_CHANNEL_DWELL_S": "0.5",
                "WIFI_SCAN_CHANNEL_WIDTH_MHZ": "20",
                "WIFI_SCAN_RUN_ASYNC": "true",
            },
        },
        # BLE_SCAN advertise — async, runs concurrently with WIFI capture above
        {
            "id": "ble-scan-advertise-async",
            "type": "BLE_SCAN",
            "timeout_s": cmd_timeout_s,
            "retries": 0,
            "env": {
                **common_env,
                **upload_env,
                "BLE_SCAN_MODE": "advertise",
                "BLE_ADV_PAYLOAD_PREFIX": "monad-02",
                "BLE_ADV_UPDATE_INTERVAL_S": "2.0",
                "BLE_SCAN_RUN_ASYNC": "true",
            },
        },
    ]

    return {
        "id": f"monad02-phase4-concurrent-{stamp}",
        "name": "monad-02 Phase 4 concurrent WIFI+BLE smoke",
        "experiment_id": f"monad02-phase4-concurrent-{stamp}",
        "target_selector": {"device_ids": [DEVICE_ID]},
        "range": {"from": iso(measure_from), "to": iso(range_to)},
        "control_plane_mode": "RF_SHARING",
        "reporting": {
            "measure_window": {"from": iso(measure_from), "to": iso(measure_to)},
            "upload_window": {"from": iso(upload_from), "to": iso(upload_to)},
            "require_upload_window": True,
            "slotting": {"slot_count": 1, "jitter_s": 0},
        },
        "notes": {
            "profile": "monad02-phase4-concurrent",
            "purpose": "Phase 4 concurrent dispatch: WIFI passive_monitor + BLE advertise in same group",
            "artifact_upload_target": "fleet_grpc",
        },
        "command_groups": [
            {
                "id": group_id,
                "name": "Phase 4 concurrent dispatch",
                "failure_mode": "CONTINUE_ON_ERROR",
                "commands": commands,
            }
        ],
    }


def main():
    api_key = os.environ.get("ELAB_API_KEY")
    if not api_key:
        raise SystemExit("ELAB_API_KEY env var required")

    api_base = os.environ.get(
        "ELAB_API_BASE",
        "https://elab-monad-fleet.34.198.184.128.sslip.io:9443/api/v2",
    ).rstrip("/")
    public_base = os.environ.get(
        "ELAB_PUBLIC_BASE",
        "https://elab-monad-fleet.34.198.184.128.sslip.io:9443",
    ).rstrip("/")

    request(api_key, "GET", api_base + "/info")

    stamp = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y%m%dT%H%M%SZ")
    policy = build_policy(stamp)
    title = f"monad-02 Phase 4 concurrent WIFI+BLE smoke {stamp}"
    body = (
        "Phase 4 concurrent dispatch smoke: WIFI_SCAN passive_monitor (WIFI_SCAN_RUN_ASYNC=true) "
        f"and BLE_SCAN advertise (BLE_SCAN_RUN_ASYNC=true) run simultaneously in the same command "
        f"group for a {MEASURE_WINDOW_S}s measure window. "
        "Expected: wifi5g_capture_active=1 and ble_advertise_active=1 overlap in Grafana; "
        "eLab bundle contains both wifi-5g-monitor.pcap and ble-tx-log.json."
    )

    _, headers, _ = request(
        api_key, "POST", api_base + "/experiments",
        {"title": title, "body": body, "metadata": "{}"},
    )
    loc = headers.get("Location") or headers.get("location") or ""
    m = re.search(r"/experiments/(\d+)$", loc)
    if not m:
        raise RuntimeError(f"could not parse experiment id from Location={loc!r}")
    experiment_id = int(m.group(1))

    request(
        api_key, "PATCH", f"{api_base}/experiments/{experiment_id}",
        {"metadata": json.dumps({"fleet": {"policy": policy}}, sort_keys=True, separators=(",", ":"))},
    )
    tags = ["fleet", "phase4", "wifi-passive-monitor", "ble-advertise", "concurrent", "monad-02", "smoke:v1", "no-csi"]
    for tag in tags:
        request(api_key, "POST", f"{api_base}/experiments/{experiment_id}/tags", {"tag": tag})

    out = {
        "experiment_id": experiment_id,
        "sharelink": f"{public_base}/experiments.php?mode=view&id={experiment_id}",
        "title": title,
        "device_id": DEVICE_ID,
        "device_label": DEVICE_LABEL,
        "tags": tags,
        "measure_window_s": MEASURE_WINDOW_S,
        "created_at_utc": iso(datetime.now(timezone.utc).replace(microsecond=0)),
    }
    out_dir = Path(__file__).resolve().parent
    out_path = out_dir / f"monad02_phase4_concurrent_{experiment_id}_{stamp}.json"
    out_path.write_text(json.dumps({**out, "policy": policy}, indent=2, sort_keys=True) + "\n")
    out["context_path"] = str(out_path)
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
