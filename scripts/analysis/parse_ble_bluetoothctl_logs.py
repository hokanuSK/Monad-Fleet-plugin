#!/usr/bin/env python3
"""Parse bluetoothctl scan logs into a ble_rx.csv-shaped dataset.

`prepare_measurement_dataset.py` does not yet understand the bluetoothctl
scan-log format, so it leaves `ble_rx.csv` empty. This script fills that gap
for environment-scan style experiments (BLE listen with no transmitter):

  - Strips ANSI colour codes
  - Pulls [NEW] Device <MAC> <name>  → first-seen events
  - Pulls [CHG] Device <MAC> RSSI: 0x<hex> (<dec>)  → RSSI samples
  - Pulls [CHG] Device <MAC> ManufacturerData.Key: 0x<hex> (<dec>)
    → BT-SIG company-id observations (vendor attribution)

Per-event timestamps are not present in bluetoothctl output, so the output
rows carry a synthetic monotonic `event_index` instead. That is enough for
device-count, RSSI-distribution and manufacturer-mix figures.

Output CSV columns: pi_id, event_index, event_type, device_mac, device_name,
rssi_dbm, manufacturer_key_int.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
NEW_RE = re.compile(r"\[NEW\]\s+Device\s+([0-9A-Fa-f:]{17})\s*(.*)$")
RSSI_RE = re.compile(r"\[CHG\]\s+Device\s+([0-9A-Fa-f:]{17})\s+RSSI:.*\((-?\d+)\)")
MFR_RE = re.compile(
    r"\[CHG\]\s+Device\s+([0-9A-Fa-f:]{17})\s+ManufacturerData\.Key:\s+0x([0-9a-fA-F]+)"
)


def derive_pi_id(path: Path, mac_to_pi: dict[str, str]) -> str:
    s = str(path).lower()
    m = re.search(r"monad[-_]?(\d+)", s)
    if m:
        return f"monad-{int(m.group(1)):02d}"
    mac_match = re.search(r"([0-9a-f]{2}(?:-[0-9a-f]{2}){5})", s)
    if mac_match:
        return mac_to_pi.get(mac_match.group(1), "unknown")
    return "unknown"


def load_mac_to_pi(metadata_path: Path) -> dict[str, str]:
    if not metadata_path.exists():
        return {}
    meta = json.loads(metadata_path.read_text())
    return {
        mac.lower().replace(":", "-"): pi
        for pi, mac in (meta.get("device_map") or {}).items()
        if isinstance(mac, str)
    }


def parse_log(path: Path, pi_id: str, sink) -> tuple[int, int, int]:
    """Stream the log into the CSV sink. Returns (n_new, n_rssi, n_mfr)."""
    n_new = n_rssi = n_mfr = 0
    event_index = 0
    device_names: dict[str, str] = {}
    with path.open("r", errors="replace") as fh:
        for raw in fh:
            line = ANSI.sub("", raw).rstrip()
            if not line:
                continue
            event_index += 1
            if m := NEW_RE.search(line):
                mac = m.group(1).upper()
                name = m.group(2).strip()
                device_names[mac] = name
                sink.writerow(
                    {
                        "pi_id": pi_id,
                        "event_index": event_index,
                        "event_type": "new",
                        "device_mac": mac,
                        "device_name": name,
                        "rssi_dbm": "",
                        "manufacturer_key_int": "",
                    }
                )
                n_new += 1
                continue
            if m := RSSI_RE.search(line):
                mac = m.group(1).upper()
                sink.writerow(
                    {
                        "pi_id": pi_id,
                        "event_index": event_index,
                        "event_type": "rssi",
                        "device_mac": mac,
                        "device_name": device_names.get(mac, ""),
                        "rssi_dbm": int(m.group(2)),
                        "manufacturer_key_int": "",
                    }
                )
                n_rssi += 1
                continue
            if m := MFR_RE.search(line):
                mac = m.group(1).upper()
                sink.writerow(
                    {
                        "pi_id": pi_id,
                        "event_index": event_index,
                        "event_type": "manufacturer_key",
                        "device_mac": mac,
                        "device_name": device_names.get(mac, ""),
                        "rssi_dbm": "",
                        "manufacturer_key_int": int(m.group(2), 16),
                    }
                )
                n_mfr += 1
    return n_new, n_rssi, n_mfr


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment",
        required=True,
        help="Experiment id under artifacts/analysis/<id>/ (e.g. exp183).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = Path("/Users/admin/FleetManager")
    base = repo / "artifacts" / "analysis" / args.experiment
    if not base.exists():
        sys.exit(f"No such dataset: {base}")

    mac_to_pi = load_mac_to_pi(base / "metadata.json")
    if not mac_to_pi:
        sys.exit("metadata.json missing device_map; cannot map MAC dirs to pi_id")

    raw_root = base / "raw"
    logs = sorted(raw_root.rglob("ble-discovery-raw-bluetoothctl-scan.log"))
    if not logs:
        sys.exit(f"No bluetoothctl scan logs found under {raw_root}")

    out = base / "ble_environment.csv"
    fields = [
        "pi_id",
        "event_index",
        "event_type",
        "device_mac",
        "device_name",
        "rssi_dbm",
        "manufacturer_key_int",
    ]
    totals = {"new": 0, "rssi": 0, "mfr": 0}
    with out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for log in logs:
            pi = derive_pi_id(log, mac_to_pi)
            n_new, n_rssi, n_mfr = parse_log(log, pi, writer)
            totals["new"] += n_new
            totals["rssi"] += n_rssi
            totals["mfr"] += n_mfr
            print(
                f"  {log.relative_to(repo)}: pi={pi}, "
                f"new={n_new}, rssi={n_rssi}, mfr={n_mfr}",
                flush=True,
            )
    print()
    print(f"Wrote {out}")
    print(f"Totals: {totals}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
