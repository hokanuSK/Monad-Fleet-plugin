#!/usr/bin/env python3
"""Prepare normalized 5 GHz/BLE/power datasets for the analysis notebook.

Inputs are local evidence bundles produced by the Fleet agent, plus optional
standalone pcaps. Outputs follow the notebook contract under
``artifacts/analysis/<run_id>/``.

The script deliberately works without pandas so it can run in the base project
environment. If ``tshark`` is available, pcaps are converted to
``wifi_5g_frames.csv``. Otherwise pcaps are copied into ``raw/`` and the CSV is
created with headers only.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


WIFI_FRAME_FIELDS = [
    "timestamp",
    "pi_id",
    "channel",
    "freq_mhz",
    "bandwidth_mhz",
    "src_mac",
    "dst_mac",
    "bssid",
    "src_oui",
    "frame_type",
    "frame_subtype",
    "frame_len",
    "rssi_dbm",
    "ssid",
    "source_pcap",
]

BLE_TX_FIELDS = ["adv_id", "tx_timestamp", "tx_pi_id", "seq", "monotonic_us", "source"]
BLE_RX_FIELDS = [
    "adv_id",
    "rx_timestamp",
    "rx_pi_id",
    "device_mac",
    "device_name",
    "rssi_dbm",
    "event_type",
    "event_index",
    "source",
]
POWER_FIELDS = [
    "timestamp",
    "pi_id",
    "core_voltage_v",
    "pmic_3v3_sys_current_a",
    "pmic_3v3_sys_voltage_v",
    "under_voltage",
    "cpu_temp_c",
]
WIFI_CAPTURE_SUMMARY_FIELDS = [
    "timestamp",
    "run_id",
    "pi_id",
    "command_id",
    "exit_code",
    "duration_ms",
    "pcap_bytes",
    "dwell_changes",
    "channels",
    "message",
    "source",
]

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
DEVICE_RE = re.compile(
    r"\[(?P<event>NEW|CHG|DEL)\]\s+Device\s+"
    r"(?P<mac>(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})"
    r"(?:\s+(?P<name>.*))?$"
)
RSSI_RE = re.compile(r"Device\s+(?P<mac>(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s+RSSI:.*\((-?\d+)\)")
MONAD_ADV_RE = re.compile(r"\b(monad-[0-9a-zA-Z_-]+-[0-9a-fA-F]{8})\b")
WIFI_CAPTURE_MSG_RE = re.compile(r"passive_monitor:\s+(\d+)B pcap,\s+(\d+) dwells over\s+(\d+) channels")
TCPDUMP_LOCAL_TZ = ZoneInfo("Europe/Bratislava")


@dataclass
class Bundle:
    path: Path
    manifest: dict
    files: dict[str, str]
    pi_id: str


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def normalize_pi_from_name(path: Path) -> str:
    for candidate in [path.name, *path.parts[-4:]]:
        stem = candidate.lower()
        match = re.search(r"monad[-_]?(\d+)", stem)
        if match:
            return f"monad-{int(match.group(1)):02d}"
    return ""


def parse_status_log(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    lines = text.splitlines()
    message_lines: list[str] = []
    in_message = False
    for line in lines:
        if in_message:
            message_lines.append(line)
            continue
        if line == "message:":
            in_message = True
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    out["message"] = "\n".join(message_lines).strip()
    return out


def infer_pi_id(bundle_name: Path, files: dict[str, str]) -> str:
    by_name = normalize_pi_from_name(bundle_name)
    if by_name:
        return by_name
    for name, raw in files.items():
        match = re.search(r"\bmonad-(\d{2})\b", f"{name}\n{raw}", re.I)
        if match:
            return f"monad-{int(match.group(1)):02d}"
    for name, raw in files.items():
        if not name.endswith("-raw-output.txt"):
            continue
        raw = strip_ansi(raw)
        lines = [line.strip() for line in raw.splitlines()]
        for idx, line in enumerate(lines):
            if line == "=== hostname ===" and idx + 1 < len(lines):
                return lines[idx + 1]
            if line.startswith("monad-"):
                return line.split()[0]
    return bundle_name.stem


def load_bundle(path: Path) -> Bundle:
    files: dict[str, str] = {}
    manifest: dict = {}
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            payload = fh.read()
            text = payload.decode("utf-8", errors="replace")
            files[member.name] = text
            if member.name == "manifest.json":
                try:
                    manifest = json.loads(text)
                except json.JSONDecodeError:
                    manifest = {}
    return Bundle(path=path, manifest=manifest, files=files, pi_id=infer_pi_id(path, files))


def iter_status_rows(bundle: Bundle) -> Iterable[dict[str, str]]:
    run_id = str(bundle.manifest.get("run_id") or "")
    for name, text in bundle.files.items():
        if not name.endswith("-execution-status.log"):
            continue
        parsed = parse_status_log(text)
        yield {
            "timestamp": parsed.get("timestamp", ""),
            "run_id": run_id,
            "pi_id": bundle.pi_id,
            "command_id": parsed.get("command_id", ""),
            "cmdline": parsed.get("cmdline", ""),
            "exit_code": parsed.get("exit_code", ""),
            "duration_ms": parsed.get("duration_ms", ""),
            "message": parsed.get("message", ""),
            "source": bundle.path.name,
        }


def parse_ble_rx(bundle: Bundle) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    device_names: dict[str, str] = {}
    device_rssi: dict[str, str] = {}
    event_index = 0
    for name, text in bundle.files.items():
        if "ble-discovery" not in name:
            continue
        for raw_line in strip_ansi(text).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            rssi_match = RSSI_RE.search(line)
            if rssi_match:
                mac = rssi_match.group("mac").upper()
                rssi = rssi_match.group(2)
                device_rssi[mac] = rssi
                rows.append(
                    {
                        "adv_id": "",
                        "rx_timestamp": "",
                        "rx_pi_id": bundle.pi_id,
                        "device_mac": mac,
                        "device_name": device_names.get(mac, ""),
                        "rssi_dbm": rssi,
                        "event_type": "RSSI",
                        "event_index": str(event_index),
                        "source": f"{bundle.path.name}:{name}",
                    }
                )
                event_index += 1
                continue
            dev_match = DEVICE_RE.search(line)
            if not dev_match:
                continue
            event_type = dev_match.group("event")
            mac = dev_match.group("mac").upper()
            dev_name = (dev_match.group("name") or "").strip()
            if dev_name:
                device_names[mac] = dev_name
            adv_match = MONAD_ADV_RE.search(dev_name)
            rows.append(
                {
                    "adv_id": adv_match.group(1) if adv_match else "",
                    "rx_timestamp": "",
                    "rx_pi_id": bundle.pi_id,
                    "device_mac": mac,
                    "device_name": dev_name,
                    "rssi_dbm": device_rssi.get(mac, ""),
                    "event_type": event_type,
                    "event_index": str(event_index),
                    "source": f"{bundle.path.name}:{name}",
                }
            )
            event_index += 1
    return rows


def parse_ble_tx(bundle: Bundle) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for name, text in bundle.files.items():
        if "ble-tx-log" not in name:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            send_ts_us = row.get("send_ts_us", "")
            tx_timestamp = ""
            if isinstance(send_ts_us, int):
                tx_timestamp = datetime.fromtimestamp(send_ts_us / 1_000_000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            rows.append(
                {
                    "adv_id": str(row.get("adv_id") or ""),
                    "tx_timestamp": tx_timestamp,
                    "tx_pi_id": bundle.pi_id,
                    "seq": str(row.get("seq") or ""),
                    "monotonic_us": str(row.get("monotonic_us") or ""),
                    "source": f"{bundle.path.name}:{name}",
                }
            )
    return rows


def parse_schedule(bundle: Bundle) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    run_id = str(bundle.manifest.get("run_id") or "")
    for name, text in bundle.files.items():
        if "wifi-5g-channel-schedule" not in name:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            start_us = row.get("dwell_start_us")
            end_us = row.get("dwell_end_us")
            duration_s = ""
            if isinstance(start_us, int) and isinstance(end_us, int):
                duration_s = f"{max(0, end_us - start_us) / 1_000_000:.6f}"
            row["run_id"] = run_id
            row["pi_id"] = bundle.pi_id
            row["dwell_duration_s"] = duration_s
            row["source"] = f"{bundle.path.name}:{name}"
            rows.append(row)
    return rows


def pcap_pi_id_from_bundles(pcap: Path, bundles: list[Bundle]) -> str:
    match = re.search(r"run-([0-9a-fA-F]+)", pcap.name)
    if match:
        short_run = match.group(1).lower()
        for bundle in bundles:
            run_id = str(bundle.manifest.get("run_id") or "").lower()
            if run_id.startswith(short_run):
                return bundle.pi_id
    return normalize_pi_from_name(pcap) or "unknown"


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def unique_raw_copy_path(raw_dir: Path, src: Path) -> Path:
    candidate = raw_dir / src.name
    if not candidate.exists():
        return candidate
    tagged = raw_dir / f"{normalize_pi_from_name(src) or src.parent.name}__{src.name}"
    if not tagged.exists():
        return tagged
    return raw_dir / f"{src.stem}__{src.parent.name}{src.suffix}"


def parse_wifi_capture_summary(status_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for row in status_rows:
        if row.get("cmdline") != "WIFI_SCAN":
            continue
        message = row.get("message", "")
        match = WIFI_CAPTURE_MSG_RE.search(message)
        rows.append(
            {
                "timestamp": row.get("timestamp", ""),
                "run_id": row.get("run_id", ""),
                "pi_id": row.get("pi_id", ""),
                "command_id": row.get("command_id", ""),
                "exit_code": row.get("exit_code", ""),
                "duration_ms": row.get("duration_ms", ""),
                "pcap_bytes": match.group(1) if match else "",
                "dwell_changes": match.group(2) if match else "",
                "channels": match.group(3) if match else "",
                "message": message,
                "source": row.get("source", ""),
            }
        )
    return rows


def build_summary(
    *,
    status_rows: list[dict[str, str]],
    ble_rx_rows: list[dict[str, str]],
    ble_tx_rows: list[dict[str, str]],
    schedule_rows: list[dict[str, object]],
    pcap_count: int,
) -> dict[str, object]:
    command_status = Counter((row.get("command_id", ""), row.get("exit_code", "")) for row in status_rows)
    schedule_channels = Counter(str(row.get("channel", "")) for row in schedule_rows)
    schedule_set_ok = Counter(str(row.get("set_ok", "")) for row in schedule_rows)
    ble_by_pi = Counter(row.get("rx_pi_id", "") for row in ble_rx_rows)
    wifi_captures = parse_wifi_capture_summary(status_rows)
    return {
        "command_status_by_command_exit": {
            f"{command_id}|exit_{exit_code}": count
            for (command_id, exit_code), count in sorted(command_status.items())
        },
        "wifi_capture_summary": wifi_captures,
        "wifi_schedule": {
            "rows": len(schedule_rows),
            "channels": dict(sorted(schedule_channels.items())),
            "set_ok": dict(sorted(schedule_set_ok.items())),
        },
        "ble": {
            "rx_rows": len(ble_rx_rows),
            "tx_rows": len(ble_tx_rows),
            "rx_rows_by_pi": dict(sorted(ble_by_pi.items())),
            "rx_rows_with_rssi": sum(1 for row in ble_rx_rows if row.get("rssi_dbm")),
            "rx_rows_with_adv_id": sum(1 for row in ble_rx_rows if row.get("adv_id")),
        },
        "pcaps": {
            "inputs": pcap_count,
            "tshark_available": bool(shutil.which("tshark")),
        },
    }


def run_tshark(pcap: Path, pi_id: str, out_csv: Path) -> int:
    tshark = shutil.which("tshark")
    if not tshark:
        return 0
    fields = [
        "-e", "frame.time_epoch",
        "-e", "wlan_radio.channel",
        "-e", "wlan_radio.frequency",
        "-e", "wlan_radio.11n.bandwidth",
        "-e", "wlan.sa",
        "-e", "wlan.da",
        "-e", "wlan.bssid",
        "-e", "wlan.fc.type",
        "-e", "wlan.fc.type_subtype",
        "-e", "frame.len",
        "-e", "wlan_radio.signal_dbm",
        "-e", "wlan.ssid",
    ]
    rows = 0
    cmd = [tshark, "-r", str(pcap), "-T", "fields", "-E", "separator=,", "-E", "quote=d", *fields]
    with out_csv.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        for line in proc.stdout:
            if not line.strip():
                continue
            parsed = next(csv.reader([line]))
            parsed = parsed + [""] * (12 - len(parsed))
            src_mac = parsed[4]
            src_oui = src_mac[:8].upper() if src_mac else ""
            writer.writerow(
                [
                    parsed[0],
                    pi_id,
                    parsed[1],
                    parsed[2],
                    parsed[3],
                    parsed[4],
                    parsed[5],
                    parsed[6],
                    src_oui,
                    parsed[7],
                    parsed[8],
                    parsed[9],
                    parsed[10],
                    parsed[11],
                    str(pcap),
                ]
            )
            rows += 1
        _, stderr = proc.communicate()
    if proc.returncode != 0:
        return 0
    return rows


def tcpdump_line_to_row(line: str, pi_id: str, pcap: Path) -> list[str] | None:
    # Example prefix: 2026-05-14 01:03:56.984568 ...
    ts_match = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}\.\d+)\s+(.*)$", line)
    if not ts_match:
        return None
    try:
        local_dt = datetime.strptime(f"{ts_match.group(1)} {ts_match.group(2)}", "%Y-%m-%d %H:%M:%S.%f")
        timestamp = local_dt.replace(tzinfo=TCPDUMP_LOCAL_TZ).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        timestamp = f"{ts_match.group(1)}T{ts_match.group(2)}"
    body = ts_match.group(3)

    freq_match = re.search(r"\b(\d{4})\s+MHz\b", body)
    freq_mhz = freq_match.group(1) if freq_match else ""
    channel = ""
    if freq_match:
        freq = int(freq_match.group(1))
        if 5000 <= freq <= 5900:
            channel = str(int((freq - 5000) / 5))
        elif 2407 <= freq <= 2484:
            channel = "14" if freq == 2484 else str(int((freq - 2407) / 5))

    bw_match = re.search(r"\b(20|40|80|160)\s+MHz\b", body)
    rssi_match = re.search(r"(-?\d+)dBm\s+signal", body)
    sa_match = re.search(r"\bSA:((?:[0-9a-f]{2}:){5}[0-9a-f]{2})\b", body, re.I)
    da_match = re.search(r"\bDA:((?:[0-9a-f]{2}:){5}[0-9a-f]{2})\b", body, re.I)
    bssid_match = re.search(r"\bBSSID:((?:[0-9a-f]{2}:){5}[0-9a-f]{2})\b", body, re.I)
    ssid_match = re.search(r"\b(?:Beacon|Probe Response)\s+\\(([^)]*)\\)", body)

    frame_type = ""
    frame_subtype = ""
    for token in ("Beacon", "Probe Request", "Probe Response", "Request-To-Send", "Clear-To-Send", "Acknowledgment", "Data"):
        if token in body:
            frame_subtype = token
            if token in {"Beacon", "Probe Request", "Probe Response"}:
                frame_type = "mgmt"
            elif token in {"Request-To-Send", "Clear-To-Send", "Acknowledgment"}:
                frame_type = "ctrl"
            else:
                frame_type = "data"
            break

    src_mac = sa_match.group(1).lower() if sa_match else ""
    return [
        timestamp,
        pi_id,
        channel,
        freq_mhz,
        bw_match.group(1) if bw_match else "",
        src_mac,
        da_match.group(1).lower() if da_match else "",
        bssid_match.group(1).lower() if bssid_match else "",
        src_mac[:8].upper() if src_mac else "",
        frame_type,
        frame_subtype,
        "",
        rssi_match.group(1) if rssi_match else "",
        ssid_match.group(1) if ssid_match else "",
        str(pcap),
    ]


def run_tcpdump(pcap: Path, pi_id: str, out_csv: Path) -> int:
    tcpdump = shutil.which("tcpdump")
    if not tcpdump:
        return 0
    cmd = [tcpdump, "-r", str(pcap), "-tttt", "-e", "-n"]
    rows = 0
    with out_csv.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        for line in proc.stdout:
            row = tcpdump_line_to_row(line, pi_id, pcap)
            if row is None:
                continue
            writer.writerow(row)
            rows += 1
        _, stderr = proc.communicate()
    if proc.returncode != 0:
        return 0
    return rows


def convert_pcap(pcap: Path, pi_id: str, out_csv: Path) -> int:
    rows = run_tshark(pcap, pi_id, out_csv)
    if rows:
        return rows
    return run_tcpdump(pcap, pi_id, out_csv)


def prepare_dataset(args: argparse.Namespace) -> dict[str, object]:
    out_dir = args.output.resolve()
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    bundles = [load_bundle(path.resolve()) for path in args.bundle]
    status_rows = [row for bundle in bundles for row in iter_status_rows(bundle)]
    ble_rx_rows = [row for bundle in bundles for row in parse_ble_rx(bundle)]
    ble_tx_rows = [row for bundle in bundles for row in parse_ble_tx(bundle)]
    schedule_rows = [row for bundle in bundles for row in parse_schedule(bundle)]

    copied_bundles = []
    for bundle in bundles:
        dst = raw_dir / bundle.path.name
        if bundle.path.resolve() != dst.resolve():
            shutil.copy2(bundle.path, dst)
        copied_bundles.append(str(dst))

    write_csv(
        out_dir / "command_status.csv",
        status_rows,
        ["timestamp", "run_id", "pi_id", "command_id", "cmdline", "exit_code", "duration_ms", "message", "source"],
    )
    wifi_capture_summary = parse_wifi_capture_summary(status_rows)
    write_csv(out_dir / "wifi_capture_summary.csv", wifi_capture_summary, WIFI_CAPTURE_SUMMARY_FIELDS)
    write_csv(out_dir / "ble_rx.csv", ble_rx_rows, BLE_RX_FIELDS)
    write_csv(out_dir / "ble_tx.csv", ble_tx_rows, BLE_TX_FIELDS)
    write_jsonl(out_dir / "wifi_5g_channel_schedule.jsonl", schedule_rows)
    write_csv(out_dir / "power_timeseries.csv", [], POWER_FIELDS)

    wifi_csv = out_dir / "wifi_5g_frames.csv"
    write_csv(wifi_csv, [], WIFI_FRAME_FIELDS)
    pcap_rows_written = 0
    pcap_inputs = [path.resolve() for path in args.pcap]
    for pcap in pcap_inputs:
        dst = unique_raw_copy_path(raw_dir, pcap)
        if pcap.resolve() != dst.resolve():
            shutil.copy2(pcap, dst)
        pi_id = pcap_pi_id_from_bundles(pcap, bundles)
        pcap_rows_written += convert_pcap(dst, pi_id, wifi_csv)

    events_path = out_dir / "events.ndjson"
    event_rows = []
    for idx, row in enumerate(status_rows):
        event_rows.append(
            {
                "event_id": f"{row.get('run_id') or 'unknown'}:{row.get('command_id') or idx}:status",
                "timestamp": row.get("timestamp", ""),
                "run_id": row.get("run_id", ""),
                "agent_id": row.get("pi_id", ""),
                "command_id": row.get("command_id", ""),
                "exit_code": row.get("exit_code", ""),
                "duration_ms": row.get("duration_ms", ""),
                "message": row.get("message", ""),
                "source": row.get("source", ""),
            }
        )
    write_jsonl(events_path, event_rows)

    metadata = {
        "schema": "monad.analysis_dataset.v1",
        "dataset_id": args.dataset_id,
        "experiment_id": args.experiment_id,
        "source_bundles": copied_bundles,
        "source_pcaps": [str(path) for path in pcap_inputs],
        "bundle_run_ids": [bundle.manifest.get("run_id") for bundle in bundles if bundle.manifest.get("run_id")],
        "pi_ids": sorted({bundle.pi_id for bundle in bundles}),
        "counts": {
            "bundles": len(bundles),
            "pcaps": len(pcap_inputs),
            "command_status_rows": len(status_rows),
            "ble_rx_rows": len(ble_rx_rows),
            "ble_tx_rows": len(ble_tx_rows),
            "wifi_schedule_rows": len(schedule_rows),
            "wifi_frame_rows_from_pcap": pcap_rows_written,
        },
        "notes": [
            "wifi_5g_frames.csv uses tshark when available, otherwise a tcpdump fallback with fewer fields.",
            "BLE discovery logs from bluetoothctl do not include per-event timestamps; rx_timestamp is blank for those rows.",
            "power_timeseries.csv is header-only until Mimir/Prometheus export is added.",
        ],
    }
    with (out_dir / "metadata.json").open("w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, sort_keys=True)
    summary = build_summary(
        status_rows=status_rows,
        ble_rx_rows=ble_rx_rows,
        ble_tx_rows=ble_tx_rows,
        schedule_rows=schedule_rows,
        pcap_count=len(pcap_inputs),
    )
    with (out_dir / "dataset_summary.json").open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
    return metadata


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", action="append", type=Path, default=[], help="Evidence tar.gz bundle to normalize.")
    parser.add_argument("--pcap", action="append", type=Path, default=[], help="Optional standalone pcap to convert/copy.")
    parser.add_argument("--output", type=Path, required=True, help="Output dataset directory.")
    parser.add_argument("--dataset-id", default="latest", help="Stable dataset/run id for metadata.")
    parser.add_argument("--experiment-id", default="", help="Experiment id for metadata, e.g. exp62.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.bundle and not args.pcap:
        parser.error("provide at least one --bundle or --pcap")
    metadata = prepare_dataset(args)
    print(json.dumps({"output": str(args.output), "counts": metadata["counts"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
