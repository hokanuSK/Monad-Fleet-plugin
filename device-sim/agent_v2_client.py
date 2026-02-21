#!/usr/bin/env python3
import base64
import glob
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import shlex
import shutil
import signal
import socket
import subprocess
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

import fleet_gateway_v2_pb2
import fleet_gateway_v2_pb2_grpc


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agent-v2")

# Opportunistic in-run artifact upload backoff state.
_ARTIFACT_UPLOAD_NEXT_TRY_AT = 0.0

# "Placeholder" cmdlines used in early smoke policies. For typed commands, we treat these as hints,
# not mandatory implementations.
_TRIVIAL_CMD_RE = re.compile(r"^\s*(?:echo\b|true\s*$|:\s*$)", re.IGNORECASE)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_ts() -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(now_utc())
    return ts


def ts_to_datetime(ts: Timestamp) -> datetime:
    return datetime.fromtimestamp(ts.seconds + ts.nanos / 1_000_000_000, tz=timezone.utc)


def ts_to_iso(ts: Timestamp) -> str:
    return ts_to_datetime(ts).isoformat().replace("+00:00", "Z")


def iso_to_ts(value: str) -> Timestamp:
    ts = Timestamp()
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    ts.FromDatetime(dt)
    return ts


def in_window(start: Timestamp, end: Timestamp) -> bool:
    if (start.seconds == 0 and start.nanos == 0) or (end.seconds == 0 and end.nanos == 0):
        return True
    now = now_utc()
    return ts_to_datetime(start) <= now <= ts_to_datetime(end)


def normalize(text: Any) -> str:
    if text is None:
        return ""
    return str(text).strip()


def sanitize_name(text: Any, default: str) -> str:
    value = normalize(text)
    if not value:
        value = default
    safe = re.sub(r"[^a-zA-Z0-9._-]", "-", value)
    return safe.strip("-") or default


def mode_from_env(raw: str) -> int:
    text = normalize(raw).upper()
    if text == "DUAL_NIC":
        return fleet_gateway_v2_pb2.DUAL_NIC
    return fleet_gateway_v2_pb2.RF_SHARING


def parse_int(value: Any, default: int) -> int:
    text = normalize(value)
    if not text:
        return default
    try:
        return int(text)
    except Exception:
        return default


def parse_bool(value: Any, default: bool = False) -> bool:
    text = normalize(value).lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def parse_timeout_seconds(env_name: str, default: int, *, minimum: int = 1) -> int:
    return max(minimum, parse_int(os.environ.get(env_name), default))


def parse_experiment_numeric_id(value: Any) -> int | None:
    text = normalize(value)
    if not text:
        return None
    if text.isdigit():
        return int(text)
    match = re.search(r"(\d+)$", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def route_interface_for_target(target_host: str) -> str:
    if not target_host:
        return ""
    try:
        out = subprocess.check_output(["ip", "route", "get", target_host], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""
    match = re.search(r"\bdev\s+(\S+)", out)
    return match.group(1) if match else ""


def detect_interfaces() -> list[fleet_gateway_v2_pb2.NetworkInterface]:
    result: list[fleet_gateway_v2_pb2.NetworkInterface] = []
    try:
        out = subprocess.check_output(["ip", "-o", "link", "show"], text=True)
    except Exception:
        return result

    for line in out.splitlines():
        match = re.match(r"^\d+:\s+([^:]+):", line)
        if not match:
            continue
        name = match.group(1)
        if name == "lo":
            continue
        mac_match = re.search(r"link/\w+\s+([0-9a-f:]{17})", line.lower())
        mac = mac_match.group(1) if mac_match else ""
        kind = "wifi" if name.startswith("wl") else "ethernet" if name.startswith(("eth", "en")) else "unknown"
        result.append(
            fleet_gateway_v2_pb2.NetworkInterface(
                name=name,
                mac=mac,
                kind=kind,
                driver="",
                firmware="",
            )
        )
    return result


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _combined_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    out = _as_text(stdout).strip()
    err = _as_text(stderr).strip()
    if out and err:
        return out + "\n" + err
    return out or err


def _short_message(text: str, limit: int = 1000) -> str:
    msg = (text or "").strip()
    if len(msg) <= limit:
        return msg
    return msg[:limit] + "…"


def _is_trivial_cmdline(cmdline: str) -> bool:
    return bool(_TRIVIAL_CMD_RE.match(cmdline or ""))


def _sudo_is_noninteractive(args: list[str]) -> bool:
    for arg in args[1:]:
        if arg == "--":
            break
        if arg == "--non-interactive":
            return True
        if arg.startswith("-") and "n" in arg[1:]:
            return True
    return False


def _with_noninteractive_sudo_argv(args: list[str]) -> list[str]:
    if not args:
        return args
    first = Path(args[0]).name
    if first != "sudo":
        return args
    if _sudo_is_noninteractive(args):
        return args
    return [args[0], "-n", *args[1:]]


def _with_noninteractive_sudo_cmdline(cmdline: str) -> str:
    text = normalize(cmdline)
    if not text:
        return text
    try:
        parsed = shlex.split(text)
    except Exception:
        if re.match(r"^\s*sudo\b", text) and not re.search(r"\bsudo\s+-[A-Za-z]*n", text):
            return re.sub(r"^\s*sudo\b", "sudo -n", text, count=1)
        return text
    updated = _with_noninteractive_sudo_argv(parsed)
    if updated == parsed:
        return text
    return " ".join(shlex.quote(part) for part in updated)


def _terminate_process_tree(proc: subprocess.Popen[str], grace_s: float = 2.0) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    until = time.time() + max(0.1, float(grace_s))
    while proc.poll() is None and time.time() < until:
        time.sleep(0.05)
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _extract_output_paths_from_command(cmdline: str, argv: list[str]) -> list[str]:
    args = [normalize(a) for a in (argv or []) if normalize(a)]
    if not args:
        try:
            args = [normalize(a) for a in shlex.split(normalize(cmdline)) if normalize(a)]
        except Exception:
            args = []
    if not args:
        return []
    result: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token in {"--output-file", "-o", "--output"}:
            if i + 1 < len(args):
                result.append(args[i + 1])
                i += 2
                continue
        if token.startswith("--output-file="):
            result.append(token.split("=", 1)[1])
        elif token.startswith("--output="):
            result.append(token.split("=", 1)[1])
        i += 1
    deduped: list[str] = []
    seen: set[str] = set()
    for path in result:
        key = normalize(path)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def run_command_capture(
    cmdline: str,
    argv: list[str],
    env: dict[str, str],
    timeout_ms: int,
    execute_policy: bool,
    *,
    force_noninteractive_sudo: bool = False,
) -> tuple[int, int, str, str]:
    if not execute_policy:
        time.sleep(min(1.0, max(0.1, timeout_ms / 1000.0)))
        simulated = f"simulated: {cmdline or ' '.join(argv) or '<no-op>'}"
        return 0, min(timeout_ms, 1000), simulated, simulated

    started = time.time()
    try:
        timeout_s = max(1, int(timeout_ms / 1000))
        child_env = dict(os.environ)
        child_env.update({normalize(k): normalize(v) for k, v in (env or {}).items() if normalize(k)})

        safe_argv = [normalize(a) for a in argv if normalize(a)]
        safe_cmdline = normalize(cmdline)
        if force_noninteractive_sudo:
            if safe_argv:
                safe_argv = _with_noninteractive_sudo_argv(safe_argv)
            else:
                safe_cmdline = _with_noninteractive_sudo_cmdline(safe_cmdline)
        try:
            if safe_argv:
                proc = subprocess.Popen(  # noqa: S603
                    safe_argv,
                    shell=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=child_env,
                    start_new_session=True,
                )
            else:
                proc = subprocess.Popen(  # noqa: S602
                    safe_cmdline,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=child_env,
                    start_new_session=True,
                )
        except FileNotFoundError:
            duration_ms = int((time.time() - started) * 1000)
            missing = safe_argv[0] if safe_argv else safe_cmdline.split(" ", 1)[0]
            return 127, duration_ms, f"missing command: {missing}", f"missing command: {missing}"

        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
            duration_ms = int((time.time() - started) * 1000)
            full = _combined_output(stdout, stderr)
            return int(proc.returncode), duration_ms, _short_message(full), full
        except subprocess.TimeoutExpired:
            _terminate_process_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=1)
            except Exception:
                stdout, stderr = "", ""
            duration_ms = int((time.time() - started) * 1000)
            full = _combined_output(stdout, stderr) or "command timed out"
            return 124, duration_ms, "command timed out", full
    except Exception as exc:
        duration_ms = int((time.time() - started) * 1000)
        message = f"command failed: {exc}"
        return 1, duration_ms, _short_message(message), message


def _run_capture_args(args: list[str], timeout_ms: int) -> tuple[int, int, str]:
    started = time.time()
    try:
        proc = subprocess.run(  # noqa: S603
            [normalize(a) for a in args if normalize(a)],
            shell=False,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_ms / 1000)),
            check=False,
        )
        duration_ms = int((time.time() - started) * 1000)
        return int(proc.returncode), duration_ms, _combined_output(proc.stdout, proc.stderr)
    except FileNotFoundError:
        duration_ms = int((time.time() - started) * 1000)
        return 127, duration_ms, f"missing command: {args[0] if args else '<unknown>'}"
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.time() - started) * 1000)
        out = _combined_output(getattr(exc, "stdout", None), getattr(exc, "stderr", None))
        return 124, duration_ms, out or "command timed out"


def resolve_executable(name: str, extra_candidates: list[str] | None = None) -> str | None:
    path = shutil.which(name)
    if path:
        return path
    for candidate in extra_candidates or []:
        try:
            p = Path(candidate)
            if p.exists() and os.access(str(p), os.X_OK):
                return str(p)
        except Exception:
            continue
    return None


def _parse_iw_scan(text: str) -> tuple[int, float | None]:
    bss_count = len(re.findall(r"^BSS\s+[0-9a-f:]{17}", text, flags=re.IGNORECASE | re.MULTILINE))
    rssis: list[float] = []
    for m in re.finditer(r"\bsignal:\s*(-?\d+(?:\.\d+)?)\s*dBm\b", text, flags=re.IGNORECASE):
        try:
            rssis.append(float(m.group(1)))
        except Exception:
            continue
    avg_rssi = (sum(rssis) / len(rssis)) if rssis else None
    return bss_count, avg_rssi


def _parse_iw_link(text: str) -> tuple[int, float | None]:
    connected = 1 if re.search(r"\bConnected to\b", text) else 0
    m = re.search(r"\bsignal:\s*(-?\d+(?:\.\d+)?)\s*dBm\b", text, flags=re.IGNORECASE)
    avg_rssi = None
    if m:
        try:
            avg_rssi = float(m.group(1))
        except Exception:
            avg_rssi = None
    return connected, avg_rssi


def _parse_proc_net_wireless(text: str, iface: str) -> tuple[float | None, float | None]:
    # /proc/net/wireless columns:
    # iface: status link level noise ...
    # Example:
    # wlan0: 0000   47.  -63.  -256        0      0      0      0      0        0
    iface = normalize(iface)
    if not iface:
        return None, None
    for line in (text or "").splitlines():
        if not line.strip().startswith(iface + ":"):
            continue
        # Keep it permissive; values may be floats or ints.
        m = re.search(rf"^{re.escape(iface)}:\s+\S+\s+([0-9.]+)\s+(-?[0-9.]+)\s+(-?[0-9.]+)", line.strip())
        if not m:
            return None, None
        try:
            link = float(m.group(1))
        except Exception:
            link = None
        try:
            level = float(m.group(2))
        except Exception:
            level = None
        return link, level
    return None, None


def _safe_float(value: Any) -> float | None:
    text = normalize(value)
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _safe_int(value: Any) -> int | None:
    text = normalize(value)
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None


def _read_text_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _read_int_file(path: str) -> int | None:
    try:
        return int(Path(path).read_text(encoding="utf-8", errors="replace").strip())
    except Exception:
        return None


def _parse_iw_link_extended(text: str) -> dict[str, str]:
    metrics: dict[str, str] = {}
    payload = text or ""

    bssid = re.search(r"\bConnected to\s+([0-9a-f:]{17})\b", payload, flags=re.IGNORECASE)
    if bssid:
        metrics["wifi_bssid"] = bssid.group(1).lower()
        metrics["wifi_connected"] = "1"
    elif re.search(r"\bNot connected\b", payload, flags=re.IGNORECASE):
        metrics["wifi_connected"] = "0"

    ssid = re.search(r"^\s*SSID:\s*(.+?)\s*$", payload, flags=re.IGNORECASE | re.MULTILINE)
    if ssid:
        metrics["wifi_ssid"] = ssid.group(1).strip()

    signal = re.search(r"\bsignal:\s*(-?\d+(?:\.\d+)?)\s*dBm\b", payload, flags=re.IGNORECASE)
    if signal:
        metrics["wifi_signal_dbm"] = f"{float(signal.group(1)):.2f}"
        metrics.setdefault("wifi_avg_rssi_dbm", metrics["wifi_signal_dbm"])

    tx_bitrate = re.search(r"\btx bitrate:\s*([0-9]+(?:\.[0-9]+)?)\s*MBit/s\b", payload, flags=re.IGNORECASE)
    if tx_bitrate:
        metrics["wifi_tx_bitrate_mbps"] = f"{float(tx_bitrate.group(1)):.2f}"

    rx_bitrate = re.search(r"\brx bitrate:\s*([0-9]+(?:\.[0-9]+)?)\s*MBit/s\b", payload, flags=re.IGNORECASE)
    if rx_bitrate:
        metrics["wifi_rx_bitrate_mbps"] = f"{float(rx_bitrate.group(1)):.2f}"

    freq = re.search(r"\bfreq:\s*(\d+)\b", payload, flags=re.IGNORECASE)
    if freq:
        metrics["wifi_freq_mhz"] = str(int(freq.group(1)))

    for source_key, metric_key in (
        ("tx retries", "wifi_tx_retries"),
        ("tx failed", "wifi_tx_failed"),
        ("beacon loss", "wifi_beacon_loss"),
    ):
        mm = re.search(rf"\b{re.escape(source_key)}:\s*(\d+)\b", payload, flags=re.IGNORECASE)
        if mm:
            metrics[metric_key] = str(int(mm.group(1)))

    rx = re.search(r"^\s*RX:\s*(\d+)\s+bytes\s+\((\d+)\s+packets\)", payload, flags=re.IGNORECASE | re.MULTILINE)
    if rx:
        metrics["wifi_link_rx_bytes"] = str(int(rx.group(1)))
        metrics["wifi_link_rx_packets"] = str(int(rx.group(2)))

    tx = re.search(r"^\s*TX:\s*(\d+)\s+bytes\s+\((\d+)\s+packets\)", payload, flags=re.IGNORECASE | re.MULTILINE)
    if tx:
        metrics["wifi_link_tx_bytes"] = str(int(tx.group(1)))
        metrics["wifi_link_tx_packets"] = str(int(tx.group(2)))

    return metrics


def _parse_iw_info_extended(text: str) -> dict[str, str]:
    metrics: dict[str, str] = {}
    payload = text or ""

    # Example:
    # channel 1 (2412 MHz), width: 20 MHz, center1: 2412 MHz
    channel = re.search(
        r"\bchannel\s+(\d+)\s+\((\d+)\s+MHz\)\s*,\s*width:\s*([0-9]+)\s*MHz(?:,\s*center1:\s*(\d+)\s*MHz)?(?:,\s*center2:\s*(\d+)\s*MHz)?",
        payload,
        flags=re.IGNORECASE,
    )
    if channel:
        metrics["wifi_channel"] = str(int(channel.group(1)))
        metrics["wifi_freq_mhz"] = str(int(channel.group(2)))
        metrics["wifi_channel_width_mhz"] = str(int(channel.group(3)))
        if channel.group(4):
            metrics["wifi_center1_mhz"] = str(int(channel.group(4)))
        if channel.group(5):
            metrics["wifi_center2_mhz"] = str(int(channel.group(5)))

    txpower = re.search(r"\btxpower\s+([0-9]+(?:\.[0-9]+)?)\s*dBm\b", payload, flags=re.IGNORECASE)
    if txpower:
        metrics["wifi_tx_power_dbm"] = f"{float(txpower.group(1)):.2f}"

    mode = re.search(r"^\s*type\s+(\S+)\s*$", payload, flags=re.IGNORECASE | re.MULTILINE)
    if mode:
        metrics["wifi_iface_mode"] = mode.group(1).strip().lower()

    return metrics


def _collect_iface_counters(iface: str) -> dict[str, str]:
    metrics: dict[str, str] = {}
    root = Path(f"/sys/class/net/{normalize(iface)}/statistics")
    if not root.exists():
        return metrics
    for src, dst in (
        ("rx_bytes", "wifi_if_rx_bytes"),
        ("tx_bytes", "wifi_if_tx_bytes"),
        ("rx_packets", "wifi_if_rx_packets"),
        ("tx_packets", "wifi_if_tx_packets"),
        ("rx_errors", "wifi_if_rx_errors"),
        ("tx_errors", "wifi_if_tx_errors"),
        ("rx_dropped", "wifi_if_rx_dropped"),
        ("tx_dropped", "wifi_if_tx_dropped"),
    ):
        value = _read_int_file(str(root / src))
        if value is not None:
            metrics[dst] = str(max(0, int(value)))
    return metrics


def _collect_device_status_metrics() -> dict[str, str]:
    metrics: dict[str, str] = {}
    if not parse_bool(os.environ.get("ENABLE_DEVICE_STATUS_METRICS"), True):
        return metrics

    try:
        load1, load5, load15 = os.getloadavg()
        metrics["device_load1"] = f"{load1:.2f}"
        metrics["device_load5"] = f"{load5:.2f}"
        metrics["device_load15"] = f"{load15:.2f}"
    except Exception:
        pass

    uptime_payload = _read_text_file("/proc/uptime")
    if uptime_payload:
        parts = uptime_payload.split()
        if parts:
            uptime_s = _safe_float(parts[0])
            if uptime_s is not None:
                metrics["device_uptime_s"] = str(max(0, int(uptime_s)))

    meminfo = _read_text_file("/proc/meminfo")
    if meminfo:
        mem_values: dict[str, int] = {}
        for line in meminfo.splitlines():
            mm = re.match(r"^(\w+):\s+(\d+)\s+kB$", line.strip())
            if not mm:
                continue
            mem_values[mm.group(1)] = int(mm.group(2))
        if "MemTotal" in mem_values:
            metrics["device_mem_total_bytes"] = str(mem_values["MemTotal"] * 1024)
        if "MemAvailable" in mem_values:
            metrics["device_mem_available_bytes"] = str(mem_values["MemAvailable"] * 1024)
        if "MemFree" in mem_values:
            metrics["device_mem_free_bytes"] = str(mem_values["MemFree"] * 1024)

    try:
        fs = os.statvfs("/")
        total_bytes = int(fs.f_frsize) * int(fs.f_blocks)
        free_bytes = int(fs.f_frsize) * int(fs.f_bavail)
        used_bytes = max(0, total_bytes - free_bytes)
        metrics["device_root_total_bytes"] = str(total_bytes)
        metrics["device_root_free_bytes"] = str(free_bytes)
        metrics["device_root_used_bytes"] = str(used_bytes)
    except Exception:
        pass

    for temp_path in (
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/devices/virtual/thermal/thermal_zone0/temp",
    ):
        temp_mc = _read_int_file(temp_path)
        if temp_mc is None:
            continue
        metrics["device_cpu_temp_c"] = f"{(float(temp_mc) / 1000.0):.2f}"
        break

    vcgencmd = resolve_executable("vcgencmd", ["/usr/bin/vcgencmd"])
    if vcgencmd:
        exit_code, _, out = _run_capture_args([vcgencmd, "get_throttled"], 2000)
        if exit_code == 0:
            mm = re.search(r"0x([0-9a-fA-F]+)", out)
            if mm:
                flags = int(mm.group(1), 16)
                metrics["device_throttled_flags"] = str(flags)
                metrics["device_throttled_now"] = "1" if (flags & 0x1) else "0"
                metrics["device_under_voltage_now"] = "1" if (flags & 0x1) else "0"

    return metrics


def _collect_wifi_observability_metrics(iface: str, timeout_ms: int, iw_bin: str | None = None) -> dict[str, str]:
    metrics: dict[str, str] = {}
    if not parse_bool(os.environ.get("ENABLE_WIFI_EXTENDED_METRICS"), True):
        return metrics

    iface = normalize(iface)
    if not iface:
        return metrics

    iw_exec = iw_bin or resolve_executable("iw", ["/usr/sbin/iw", "/sbin/iw"])
    if iw_exec:
        link_code, _, link_out = _run_capture_args([iw_exec, "dev", iface, "link"], max(1000, timeout_ms))
        if link_code in (0, 124):
            metrics.update(_parse_iw_link_extended(link_out))
        info_code, _, info_out = _run_capture_args([iw_exec, "dev", iface, "info"], max(1000, timeout_ms))
        if info_code == 0:
            metrics.update(_parse_iw_info_extended(info_out))

    proc_wireless = _read_text_file("/proc/net/wireless")
    if proc_wireless:
        link, level = _parse_proc_net_wireless(proc_wireless, iface)
        if link is not None:
            metrics["wifi_link_quality"] = f"{float(link):.2f}"
        if level is not None:
            metrics.setdefault("wifi_avg_rssi_dbm", f"{float(level):.2f}")

    metrics.update(_collect_iface_counters(iface))
    metrics.update(_collect_device_status_metrics())
    return metrics


def _parse_bluetoothctl_scan(text: str) -> tuple[int, float | None]:
    # Count unique device addresses observed in output; average RSSI when available.
    addrs = set(re.findall(r"\b([0-9A-F]{2}(?::[0-9A-F]{2}){5})\b", text, flags=re.IGNORECASE))
    rssis: list[float] = []
    for m in re.finditer(r"\bRSSI:\s*(-?\d+(?:\.\d+)?)\b", text, flags=re.IGNORECASE):
        try:
            rssis.append(float(m.group(1)))
        except Exception:
            continue
    avg_rssi = (sum(rssis) / len(rssis)) if rssis else None
    return len(addrs), avg_rssi


def event_type_for_exit(exit_code: int) -> int:
    return fleet_gateway_v2_pb2.COMMAND_FINISHED if exit_code == 0 else fleet_gateway_v2_pb2.COMMAND_FAILED


def command_type_name(command_type: int) -> str:
    mapping = {
        int(fleet_gateway_v2_pb2.SHELL): "SHELL",
        int(fleet_gateway_v2_pb2.CAPTURE_CSI): "CAPTURE_CSI",
        int(fleet_gateway_v2_pb2.BLE_SCAN): "BLE_SCAN",
        int(fleet_gateway_v2_pb2.WIFI_SCAN): "WIFI_SCAN",
    }
    return mapping.get(int(command_type), "COMMAND_TYPE_UNSPECIFIED")


def reached_max_cycles(cycles: int, max_cycles: int) -> bool:
    return max_cycles > 0 and cycles >= max_cycles


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact_from_path(name: str, path: Path, run_id: str) -> fleet_gateway_v2_pb2.ArtifactRef:
    spool_uri = f"spool://{sanitize_name(run_id, 'run')}/artifacts/{sanitize_name(name, 'artifact')}"
    return fleet_gateway_v2_pb2.ArtifactRef(
        name=name,
        uri=spool_uri,
        size_bytes=path.stat().st_size,
        sha256=file_sha256(path),
    )


def event_to_dict(event: fleet_gateway_v2_pb2.Event) -> dict[str, Any]:
    return {
        "event_id": normalize(event.event_id),
        "run_id": normalize(event.run_id),
        "experiment_id": normalize(event.experiment_id),
        "policy_id": normalize(event.policy_id),
        "agent_id": normalize(event.agent_id),
        "timestamp": ts_to_iso(event.timestamp),
        "type": int(event.type),
        "group_id": normalize(event.group_id),
        "command_id": normalize(event.command_id),
        "exit_code": int(event.exit_code),
        "duration_ms": int(event.duration_ms),
        "message": normalize(event.message),
        "metrics": {normalize(k): normalize(v) for k, v in event.metrics.items() if normalize(k)},
    }


def event_from_dict(payload: dict[str, Any]) -> fleet_gateway_v2_pb2.Event:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    return fleet_gateway_v2_pb2.Event(
        event_id=normalize(payload.get("event_id")),
        run_id=normalize(payload.get("run_id")),
        experiment_id=normalize(payload.get("experiment_id")),
        policy_id=normalize(payload.get("policy_id")),
        agent_id=normalize(payload.get("agent_id")),
        timestamp=iso_to_ts(normalize(payload.get("timestamp"))),
        type=int(payload.get("type", 0) or 0),
        group_id=normalize(payload.get("group_id")),
        command_id=normalize(payload.get("command_id")),
        exit_code=int(payload.get("exit_code", 0) or 0),
        duration_ms=max(0, int(payload.get("duration_ms", 0) or 0)),
        message=normalize(payload.get("message")),
        metrics={normalize(k): normalize(v) for k, v in metrics.items() if normalize(k)},
    )


def report_to_dict(report: fleet_gateway_v2_pb2.Report) -> dict[str, Any]:
    return {
        "run_id": normalize(report.run_id),
        "experiment_id": normalize(report.experiment_id),
        "policy_id": normalize(report.policy_id),
        "agent_id": normalize(report.agent_id),
        "started_at": ts_to_iso(report.started_at),
        "finished_at": ts_to_iso(report.finished_at),
        "status": normalize(report.status),
        "events": [event_to_dict(ev) for ev in report.events],
        "artifacts": [
            {
                "name": normalize(art.name),
                "uri": normalize(art.uri),
                "size_bytes": int(art.size_bytes),
                "sha256": normalize(art.sha256),
            }
            for art in report.artifacts
        ],
        "summary_metrics": {normalize(k): normalize(v) for k, v in report.summary_metrics.items() if normalize(k)},
    }


def report_from_dict(payload: dict[str, Any]) -> fleet_gateway_v2_pb2.Report:
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    summary_metrics = payload.get("summary_metrics") if isinstance(payload.get("summary_metrics"), dict) else {}
    return fleet_gateway_v2_pb2.Report(
        run_id=normalize(payload.get("run_id")),
        experiment_id=normalize(payload.get("experiment_id")),
        policy_id=normalize(payload.get("policy_id")),
        agent_id=normalize(payload.get("agent_id")),
        started_at=iso_to_ts(normalize(payload.get("started_at"))),
        finished_at=iso_to_ts(normalize(payload.get("finished_at"))),
        status=normalize(payload.get("status")),
        events=[event_from_dict(ev) for ev in events if isinstance(ev, dict)],
        artifacts=[
            fleet_gateway_v2_pb2.ArtifactRef(
                name=normalize(art.get("name")),
                uri=normalize(art.get("uri")),
                size_bytes=max(0, int(art.get("size_bytes", 0) or 0)),
                sha256=normalize(art.get("sha256")),
            )
            for art in artifacts
            if isinstance(art, dict)
        ],
        summary_metrics={normalize(k): normalize(v) for k, v in summary_metrics.items() if normalize(k)},
    )


class RunStore:
    def __init__(self, root: Path, sent_retention_days: int) -> None:
        self.root = root
        self.pending_dir = root / "pending"
        self.sent_dir = root / "sent"
        self.sent_retention_days = max(1, int(sent_retention_days))
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.sent_dir.mkdir(parents=True, exist_ok=True)

    def _run_dir(self, run_id: str, *, sent: bool = False) -> Path:
        return (self.sent_dir if sent else self.pending_dir) / sanitize_name(run_id, "run")

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(path)

    def start_run(self, run_id: str, context: dict[str, Any]) -> Path:
        run_dir = self._run_dir(run_id)
        (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(
            run_dir / "context.json",
            {
                "state": "measuring",
                "updated_at": now_utc().isoformat().replace("+00:00", "Z"),
                **context,
            },
        )
        return run_dir

    def append_event(self, run_id: str, event: fleet_gateway_v2_pb2.Event) -> None:
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "events.ndjson").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event_to_dict(event), sort_keys=True))
            fh.write("\n")

    def write_command_log(
        self,
        run_id: str,
        cmd_id: str,
        cmdline: str,
        exit_code: int,
        duration_ms: int,
        message: str,
    ) -> fleet_gateway_v2_pb2.ArtifactRef:
        artifacts_dir = self._run_dir(run_id) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        safe_cmd = sanitize_name(cmd_id, "command")
        file_name = f"{safe_cmd}-{int(time.time() * 1000)}.log"
        path = artifacts_dir / file_name
        path.write_text(
            "\n".join(
                [
                    f"timestamp={now_utc().isoformat().replace('+00:00', 'Z')}",
                    f"command_id={cmd_id}",
                    f"cmdline={cmdline}",
                    f"exit_code={exit_code}",
                    f"duration_ms={duration_ms}",
                    "message:",
                    message,
                ]
            ),
            encoding="utf-8",
        )
        return artifact_from_path(file_name, path, run_id)

    def write_summary_artifact(self, run_id: str, payload: dict[str, Any]) -> fleet_gateway_v2_pb2.ArtifactRef:
        artifacts_dir = self._run_dir(run_id) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = artifacts_dir / "run-summary.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return artifact_from_path("run-summary.json", path, run_id)

    def write_text_artifact(self, run_id: str, prefix: str, content: str, *, suffix: str = ".txt") -> fleet_gateway_v2_pb2.ArtifactRef:
        artifacts_dir = self._run_dir(run_id) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time() * 1000)
        file_name = f"{sanitize_name(prefix, 'artifact')}-{stamp}{suffix}"
        path = artifacts_dir / file_name
        path.write_text(content or "", encoding="utf-8")
        return artifact_from_path(file_name, path, run_id)

    def import_file_artifact(
        self,
        run_id: str,
        source_path: Path,
        *,
        artifact_name: str | None = None,
    ) -> fleet_gateway_v2_pb2.ArtifactRef:
        artifacts_dir = self._run_dir(run_id) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        source = Path(source_path)
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(f"artifact source not found: {source}")

        requested_name = normalize(artifact_name) or source.name or "artifact.bin"
        suffix = Path(requested_name).suffix or source.suffix
        stem = Path(requested_name).stem or "artifact"
        file_name = f"{sanitize_name(stem, 'artifact')}{suffix}"
        dest = artifacts_dir / file_name
        if dest.exists():
            stamp = int(time.time() * 1000)
            file_name = f"{sanitize_name(stem, 'artifact')}-{stamp}{suffix}"
            dest = artifacts_dir / file_name

        shutil.copy2(source, dest)
        return artifact_from_path(file_name, dest, run_id)

    def artifact_path(self, run_id: str, artifact_name: str) -> Path | None:
        if not normalize(run_id) or not normalize(artifact_name):
            return None
        safe_name = Path(normalize(artifact_name)).name
        path = self._run_dir(run_id) / "artifacts" / safe_name
        if path.exists() and path.is_file():
            return path
        return None

    def run_artifacts_size_bytes(self, run_id: str, *, sent: bool = False) -> int:
        root = self._run_dir(run_id, sent=sent) / "artifacts"
        if not root.exists():
            return 0
        total = 0
        for child in root.rglob("*"):
            if child.is_file():
                try:
                    total += max(0, int(child.stat().st_size))
                except Exception:
                    continue
        return total

    def persist_report(self, report: fleet_gateway_v2_pb2.Report) -> None:
        run_dir = self._run_dir(report.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(run_dir / "report.json", report_to_dict(report))
        self._write_json_atomic(
            run_dir / "context.json",
            {
                "state": "measured",
                "updated_at": now_utc().isoformat().replace("+00:00", "Z"),
                "run_id": normalize(report.run_id),
                "policy_id": normalize(report.policy_id),
                "experiment_id": normalize(report.experiment_id),
            },
        )

    def pending_run_ids(self) -> list[str]:
        runs: list[tuple[float, str]] = []
        for child in self.pending_dir.iterdir():
            if not child.is_dir():
                continue
            report_path = child / "report.json"
            if not report_path.exists():
                continue
            runs.append((report_path.stat().st_mtime, child.name))
        runs.sort(key=lambda row: row[0])
        return [run_id for _, run_id in runs]

    def load_report(self, run_id: str) -> fleet_gateway_v2_pb2.Report | None:
        report_path = self._run_dir(run_id) / "report.json"
        if not report_path.exists():
            return None
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Failed to parse stored report for run_id=%s", run_id)
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return report_from_dict(payload)
        except Exception:
            log.exception("Failed to rebuild protobuf report for run_id=%s", run_id)
            return None

    def mark_sent(self, run_id: str) -> None:
        source = self._run_dir(run_id)
        if not source.exists():
            return
        target = self._run_dir(run_id, sent=True)
        if target.exists():
            shutil.rmtree(target)
        source.replace(target)
        context_path = target / "context.json"
        payload: dict[str, Any] = {
            "state": "reported",
            "updated_at": now_utc().isoformat().replace("+00:00", "Z"),
            "run_id": run_id,
        }
        if context_path.exists():
            try:
                current = json.loads(context_path.read_text(encoding="utf-8"))
                if isinstance(current, dict):
                    payload.update(current)
            except Exception:
                pass
        payload["state"] = "reported"
        payload["updated_at"] = now_utc().isoformat().replace("+00:00", "Z")
        self._write_json_atomic(context_path, payload)

    def prune_sent(self) -> None:
        cutoff = now_utc() - timedelta(days=self.sent_retention_days)
        for child in self.sent_dir.iterdir():
            if not child.is_dir():
                continue
            mtime = datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)


def flush_pending_reports(
    stub: fleet_gateway_v2_pb2_grpc.FleetManagerStub,
    agent_id: str,
    store: RunStore,
) -> set[str]:
    sent_run_ids: set[str] = set()
    report_timeout_s = max(10, parse_int(os.environ.get("REPORT_RPC_TIMEOUT_S"), 90))
    for run_id in store.pending_run_ids():
        report = store.load_report(run_id)
        if report is None:
            continue
        uploaded_count, failed_count = upload_report_artifacts_to_elab(report, store, fallback_agent_id=agent_id)
        if uploaded_count or failed_count:
            report.summary_metrics["artifacts_uploaded_elab"] = str(uploaded_count)
            report.summary_metrics["artifacts_upload_failed"] = str(failed_count)
            store.persist_report(report)
        try:
            resp = stub.PublishReport(
                fleet_gateway_v2_pb2.PublishReportRequest(agent_id=agent_id, report=report),
                timeout=float(report_timeout_s),
            )
        except grpc.RpcError as exc:
            log.warning("PublishReport replay failed for run_id=%s: %s", run_id, exc)
            break

        status = int(resp.status)
        if status in (
            int(fleet_gateway_v2_pb2.PublishReportResponse.ACCEPTED),
            int(fleet_gateway_v2_pb2.PublishReportResponse.DUPLICATE),
        ):
            store.mark_sent(run_id)
            sent_run_ids.add(run_id)
        else:
            log.warning(
                "PublishReport replay rejected for run_id=%s status=%s reason=%s",
                run_id,
                status,
                normalize(resp.reason),
            )
    store.prune_sent()
    return sent_run_ids


def collect_wifi_scan(
    iface: str,
    timeout_ms: int,
    execute_policy: bool,
    store: RunStore,
    run_id: str,
    cmd_id: str,
    *,
    persist_artifacts: bool = True,
    override_cmdline: str = "",
    override_argv: list[str] | None = None,
    override_env: dict[str, str] | None = None,
) -> tuple[int, int, str, dict[str, str], list[fleet_gateway_v2_pb2.ArtifactRef]]:
    metrics: dict[str, str] = {}
    artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []

    iface = normalize(iface)
    if not iface:
        return 0, 0, "wifi scan: no interface configured", {"wifi_ap_count": "0", "wifi_connected": "0"}, []

    # Optional policy override (useful when an agent wants to use a different tool).
    if execute_policy and (override_argv or (override_cmdline and not _is_trivial_cmdline(override_cmdline))):
        exit_code, duration_ms, short, full = run_command_capture(
            override_cmdline,
            override_argv or [],
            override_env or {},
            timeout_ms,
            execute_policy=True,
        )
        if persist_artifacts:
            try:
                artifacts.append(store.write_text_artifact(run_id, f"wifi-{cmd_id}-override", full))
            except Exception:
                log.exception("Failed to persist wifi override output artifact")

        # If the override output looks like iw scan output, parse it. Otherwise, fall back to default scan.
        ap_count, avg_rssi = _parse_iw_scan(full)
        if ap_count > 0 or avg_rssi is not None:
            metrics["wifi_ap_count"] = str(max(0, int(ap_count)))
            if avg_rssi is not None:
                metrics["wifi_avg_rssi_dbm"] = f"{avg_rssi:.2f}"
            metrics["wifi_connected"] = "1"
            metrics.update(_collect_wifi_observability_metrics(iface, timeout_ms))
            return exit_code, duration_ms, short or "wifi scan override", metrics, artifacts

    if not execute_policy:
        # Simulation mode for local containers (no RF access). Keep deterministic non-zero values to validate pipeline.
        duration_ms = min(timeout_ms, 1000)
        time.sleep(min(1.0, max(0.1, duration_ms / 1000.0)))
        wifi_ap_count = max(1, int(duration_ms / 250) + 3)
        metrics["wifi_ap_count"] = str(wifi_ap_count)
        metrics["wifi_avg_rssi_dbm"] = "-55.0"
        metrics["wifi_connected"] = "1"
        metrics.update(_collect_device_status_metrics())
        return 0, duration_ms, f"simulated wifi scan on {iface}", metrics, []

    iw_bin = resolve_executable("iw", ["/usr/sbin/iw", "/sbin/iw"])

    # Primary: active scan (may require CAP_NET_ADMIN); fall back to link status (should work unprivileged).
    if iw_bin:
        exit_code, duration_ms, out = _run_capture_args([iw_bin, "dev", iface, "scan"], timeout_ms)
    else:
        exit_code, duration_ms, out = 127, 0, "iw not found"
    if exit_code == 0:
        ap_count, avg_rssi = _parse_iw_scan(out)
        metrics["wifi_ap_count"] = str(max(0, int(ap_count)))
        if avg_rssi is not None:
            metrics["wifi_avg_rssi_dbm"] = f"{avg_rssi:.2f}"
        metrics["wifi_connected"] = "1"
        metrics.update(_collect_wifi_observability_metrics(iface, timeout_ms, iw_bin))
        msg = f"iw scan ok: ap_count={ap_count}"
        if persist_artifacts:
            try:
                artifacts.append(store.write_text_artifact(run_id, f"wifi-{cmd_id}-scan", out))
            except Exception:
                log.exception("Failed to persist wifi scan artifact")
        return 0, duration_ms, msg, metrics, artifacts

    # Fallback: current link (iw without scan privileges may still work for link status).
    if iw_bin:
        exit_code2, duration_ms2, out2 = _run_capture_args([iw_bin, "dev", iface, "link"], timeout_ms)
    else:
        exit_code2, duration_ms2, out2 = 127, 0, "iw not found"
    connected, avg_rssi = _parse_iw_link(out2)
    metrics["wifi_connected"] = str(int(bool(connected)))
    metrics["wifi_ap_count"] = "1" if connected else "0"
    if avg_rssi is not None:
        metrics["wifi_avg_rssi_dbm"] = f"{avg_rssi:.2f}"
    metrics.update(_parse_iw_link_extended(out2))
    metrics.update(_collect_wifi_observability_metrics(iface, timeout_ms, iw_bin))
    msg = "iw scan unavailable; used iw link"
    if persist_artifacts:
        try:
            artifacts.append(store.write_text_artifact(run_id, f"wifi-{cmd_id}-link", out2))
        except Exception:
            log.exception("Failed to persist wifi link artifact")

    if metrics.get("wifi_avg_rssi_dbm") is not None or metrics.get("wifi_connected") == "1":
        # Treat inability to scan as non-fatal for smoke testing on restricted environments.
        return 0, duration_ms2, msg, metrics, artifacts

    # Final fallback: parse /proc/net/wireless (common on lightweight images).
    try:
        proc_text = Path("/proc/net/wireless").read_text(encoding="utf-8", errors="replace")
        link, level = _parse_proc_net_wireless(proc_text, iface)
        if link is not None:
            metrics["wifi_link_quality"] = f"{link:.2f}"
        if level is not None:
            metrics["wifi_avg_rssi_dbm"] = f"{level:.2f}"
        metrics["wifi_connected"] = "1" if (link is not None or level is not None) else "0"
        metrics["wifi_ap_count"] = "1" if metrics["wifi_connected"] == "1" else "0"
        metrics.update(_collect_iface_counters(iface))
        metrics.update(_collect_device_status_metrics())
        msg = "iw unavailable; used /proc/net/wireless"
        if persist_artifacts:
            try:
                artifacts.append(store.write_text_artifact(run_id, f"wifi-{cmd_id}-proc-wireless", proc_text))
            except Exception:
                log.exception("Failed to persist proc wireless artifact")
        return 0, duration_ms2, msg, metrics, artifacts
    except Exception:
        pass

    return 0, duration_ms2, msg, metrics, artifacts


def _ingest_metrics_http(
    device_id: str,
    values: dict[str, float],
    *,
    source: str = "agent-wifi-sampler",
) -> None:
    if not values:
        return
    if not parse_bool(os.environ.get("ENABLE_HTTP_SAMPLE_INGEST"), True):
        return

    url = normalize(os.environ.get("FLEET_METRICS_INGEST_URL"))
    if not url:
        host = normalize(os.environ.get("FLEET_MANAGER_HOST"))
        if not host:
            return
        port = parse_int(os.environ.get("METRICS_PORT"), 9108)
        url = f"http://{host}:{max(1, port)}/ingest/v1/metrics"

    payload = {
        "source": source,
        "device_id": normalize(device_id).lower(),
        "values": values,
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")

    token = normalize(os.environ.get("INGEST_API_TOKEN"))
    if token:
        req.add_header("x-ingest-token", token)

    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            if int(getattr(resp, "status", 200)) >= 300:
                log.debug("metrics ingest returned status=%s", getattr(resp, "status", "unknown"))
    except Exception:
        log.debug("metrics ingest failed", exc_info=True)


def _post_json(url: str, payload: dict[str, Any], *, timeout: int = 15) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    token = normalize(os.environ.get("INGEST_API_TOKEN"))
    if token:
        req.add_header("x-ingest-token", token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}


def _artifact_upload_target(value: str) -> str:
    target = normalize(value).lower()
    if not target:
        return "elabftw"
    if target in {"none", "disabled", "off"}:
        return "none"
    return target


def _artifact_upload_endpoint_and_limit() -> tuple[str, int] | None:
    if not parse_bool(os.environ.get("ENABLE_ELAB_ARTIFACT_UPLOAD"), True):
        return None
    host = normalize(os.environ.get("FLEET_MANAGER_HOST"))
    if not host:
        return None
    port = parse_int(os.environ.get("METRICS_PORT"), 9108)
    endpoint = normalize(os.environ.get("FLEET_ARTIFACT_INGEST_URL")) or f"http://{host}:{max(1, port)}/ingest/v1/artifacts"
    max_bytes = max(1024, parse_int(os.environ.get("ARTIFACT_UPLOAD_MAX_BYTES"), 20 * 1024 * 1024))
    return endpoint, max_bytes


def _upload_spool_artifact_to_elab(
    artifact: fleet_gateway_v2_pb2.ArtifactRef,
    store: RunStore,
    *,
    run_id: str,
    experiment_numeric_id: int,
    agent_id: str,
    endpoint: str,
    max_bytes: int,
    timeout_s: int,
) -> str:
    if not normalize(artifact.uri).startswith("spool://"):
        return "skipped"

    path = store.artifact_path(run_id, artifact.name)
    if path is None:
        log.warning("artifact upload skipped: local file missing run_id=%s name=%s", run_id, artifact.name)
        return "failed"

    try:
        size_bytes = int(path.stat().st_size)
        if size_bytes > max_bytes:
            log.warning(
                "artifact upload skipped: too large run_id=%s name=%s size=%d max=%d",
                run_id,
                artifact.name,
                size_bytes,
                max_bytes,
            )
            return "failed"
        raw = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = {
            "experiment_id": str(experiment_numeric_id),
            "run_id": normalize(run_id),
            "agent_id": normalize(agent_id),
            "artifact_name": normalize(artifact.name) or path.name,
            "mime": mime,
            "size_bytes": size_bytes,
            "sha256": normalize(artifact.sha256),
            "comment": f"fleet-v3 run={run_id} agent={agent_id} artifact={normalize(artifact.name)}",
            "content_b64": base64.b64encode(raw).decode("ascii"),
        }
        response = _post_json(endpoint, payload, timeout=max(2, int(timeout_s)))
        if not response.get("ok"):
            log.warning("artifact upload rejected run_id=%s name=%s response=%s", run_id, artifact.name, response)
            return "failed"

        new_uri = normalize(response.get("artifact_uri") or response.get("location"))
        if new_uri:
            artifact.uri = new_uri
        if parse_bool(os.environ.get("ARTIFACT_EVICT_AFTER_UPLOAD"), False):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        return "uploaded"
    except Exception as exc:
        log.warning("artifact upload failed run_id=%s name=%s err=%s", run_id, artifact.name, exc)
        return "failed"


def opportunistic_upload_artifacts_to_elab(
    run_id: str,
    experiment_id: str,
    agent_id: str,
    artifacts: list[fleet_gateway_v2_pb2.ArtifactRef],
    store: RunStore,
    *,
    enabled: bool,
    target: str,
) -> tuple[int, int]:
    global _ARTIFACT_UPLOAD_NEXT_TRY_AT

    if not enabled or not artifacts:
        return (0, 0)
    if _artifact_upload_target(target) != "elabftw":
        return (0, 0)
    experiment_numeric_id = parse_experiment_numeric_id(experiment_id)
    if experiment_numeric_id is None:
        return (0, 0)

    config = _artifact_upload_endpoint_and_limit()
    if config is None:
        return (0, 0)

    now_s = time.time()
    if now_s < _ARTIFACT_UPLOAD_NEXT_TRY_AT:
        return (0, 0)

    endpoint, max_bytes = config
    timeout_s = max(2, parse_int(os.environ.get("ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S"), 3))
    uploaded = 0
    failed = 0

    for artifact in artifacts:
        status = _upload_spool_artifact_to_elab(
            artifact,
            store,
            run_id=run_id,
            experiment_numeric_id=experiment_numeric_id,
            agent_id=agent_id,
            endpoint=endpoint,
            max_bytes=max_bytes,
            timeout_s=timeout_s,
        )
        if status == "uploaded":
            uploaded += 1
        elif status == "failed":
            failed += 1

    if failed > 0 and uploaded == 0:
        backoff_s = max(3, parse_int(os.environ.get("ARTIFACT_UPLOAD_BACKOFF_S"), 15))
        _ARTIFACT_UPLOAD_NEXT_TRY_AT = now_s + float(backoff_s)
    else:
        _ARTIFACT_UPLOAD_NEXT_TRY_AT = now_s

    return (uploaded, failed)


def upload_report_artifacts_to_elab(
    report: fleet_gateway_v2_pb2.Report,
    store: RunStore,
    *,
    fallback_agent_id: str,
) -> tuple[int, int]:
    experiment_numeric_id = parse_experiment_numeric_id(report.experiment_id)
    if experiment_numeric_id is None:
        return (0, 0)

    config = _artifact_upload_endpoint_and_limit()
    if config is None:
        return (0, 0)
    endpoint, max_bytes = config

    uploaded = 0
    failed = 0
    agent_id = normalize(report.agent_id) or normalize(fallback_agent_id)
    timeout_s = max(10, min(60, parse_int(os.environ.get("ARTIFACT_UPLOAD_TIMEOUT_S"), 30)))

    for artifact in report.artifacts:
        status = _upload_spool_artifact_to_elab(
            artifact,
            store,
            run_id=report.run_id,
            experiment_numeric_id=experiment_numeric_id,
            agent_id=agent_id,
            endpoint=endpoint,
            max_bytes=max_bytes,
            timeout_s=timeout_s,
        )
        if status == "uploaded":
            uploaded += 1
        elif status == "failed":
            failed += 1

    return (uploaded, failed)


def collect_wifi_scan_series(
    iface: str,
    timeout_ms: int,
    execute_policy: bool,
    store: RunStore,
    run_id: str,
    cmd_id: str,
    *,
    sample_interval_s: int,
    sample_duration_s: int,
    device_id: str,
    emit_live_samples: bool,
    experiment_id: str = "",
    agent_id: str = "",
    upload_during_measure: bool = False,
    artifact_upload_target: str = "elabftw",
    override_cmdline: str = "",
    override_argv: list[str] | None = None,
    override_env: dict[str, str] | None = None,
) -> tuple[int, int, str, dict[str, str], list[fleet_gateway_v2_pb2.ArtifactRef]]:
    cfg_env = override_env or {}
    min_interval_s = max(1, parse_int(os.environ.get("WIFI_SAMPLE_MIN_INTERVAL_S"), 1))
    max_duration_s = max(1, parse_int(os.environ.get("WIFI_SAMPLE_MAX_DURATION_S"), 300))
    max_points = max(1, parse_int(os.environ.get("WIFI_SAMPLE_MAX_POINTS"), 600))

    requested_interval_s = max(1, sample_interval_s)
    requested_duration_s = max(1, sample_duration_s)
    effective_interval_s = max(min_interval_s, requested_interval_s)
    effective_duration_s = min(requested_duration_s, max_duration_s)
    effective_duration_s = min(effective_duration_s, max(1, int(timeout_ms / 1000)))
    requested_start_delay_s = max(
        0,
        parse_int(cfg_env.get("WIFI_SAMPLE_START_DELAY_S") or os.environ.get("WIFI_SAMPLE_START_DELAY_S"), 0),
    )
    requested_start_jitter_s = max(
        0,
        parse_int(cfg_env.get("WIFI_SAMPLE_START_JITTER_S") or os.environ.get("WIFI_SAMPLE_START_JITTER_S"), 0),
    )
    requested_start_at_epoch_s = max(
        0,
        parse_int(cfg_env.get("WIFI_SAMPLE_START_AT_EPOCH_S") or os.environ.get("WIFI_SAMPLE_START_AT_EPOCH_S"), 0),
    )
    start_jitter_draw_s = random.randint(0, requested_start_jitter_s) if requested_start_jitter_s > 0 else 0
    start_wait_epoch_needed_s = 0
    if requested_start_at_epoch_s > 0:
        now_epoch_s = int(time.time())
        if requested_start_at_epoch_s > now_epoch_s:
            start_wait_epoch_needed_s = requested_start_at_epoch_s - now_epoch_s

    # Keep preface wait bounded so a misconfigured start time does not stall a run indefinitely.
    max_preface_sleep_s = max(0, int(timeout_ms / 1000) - 1)
    start_wait_epoch_applied_s = min(start_wait_epoch_needed_s, max_preface_sleep_s)
    remaining_preface_s = max_preface_sleep_s - start_wait_epoch_applied_s
    start_delay_applied_s = min(requested_start_delay_s, remaining_preface_s)
    remaining_preface_s -= start_delay_applied_s
    start_jitter_applied_s = min(start_jitter_draw_s, remaining_preface_s)
    preface_sleep_applied_s = start_wait_epoch_applied_s + start_delay_applied_s + start_jitter_applied_s

    effective_duration_budget_s = max(1, effective_duration_s - preface_sleep_applied_s)
    effective_points = max(1, min(max_points, int(effective_duration_budget_s / max(1, effective_interval_s))))
    requested_artifact_stride = parse_int(
        cfg_env.get("WIFI_SAMPLE_ARTIFACT_STRIDE") or os.environ.get("WIFI_SAMPLE_ARTIFACT_STRIDE"),
        0,
    )
    artifact_stride = max(0, requested_artifact_stride)

    all_artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []
    rssi_samples: list[float] = []
    link_samples: list[float] = []
    ap_samples: list[int] = []
    connected_samples = 0
    scan_ok_samples = 0
    artifact_samples = 0
    artifacts_uploaded_during_measure = 0
    artifacts_upload_failed_during_measure = 0
    total_duration_ms = 0
    last_metrics: dict[str, str] = {}
    start_monotonic = time.monotonic()

    if preface_sleep_applied_s > 0:
        time.sleep(preface_sleep_applied_s)

    for idx in range(1, effective_points + 1):
        sample_start = time.monotonic()
        keep_sample_artifacts = False
        if artifact_stride <= 0:
            keep_sample_artifacts = idx == 1 or idx == effective_points
        elif artifact_stride == 1:
            keep_sample_artifacts = True
        else:
            keep_sample_artifacts = (idx == 1) or (idx == effective_points) or (idx % artifact_stride == 0)
        exit_code, duration_ms, _, metrics, artifacts = collect_wifi_scan(
            iface,
            min(timeout_ms, max(1000, effective_interval_s * 1000)),
            execute_policy,
            store,
            run_id,
            f"{cmd_id}-s{idx}",
            persist_artifacts=keep_sample_artifacts,
            override_cmdline=override_cmdline,
            override_argv=override_argv,
            override_env=override_env,
        )
        total_duration_ms += max(0, int(duration_ms))
        all_artifacts.extend(artifacts)
        if keep_sample_artifacts:
            artifact_samples += 1
        if upload_during_measure and artifacts:
            uploaded_now, failed_now = opportunistic_upload_artifacts_to_elab(
                run_id,
                experiment_id,
                agent_id,
                artifacts,
                store,
                enabled=True,
                target=artifact_upload_target,
            )
            artifacts_uploaded_during_measure += uploaded_now
            artifacts_upload_failed_during_measure += failed_now
        last_metrics = metrics or {}

        ap_count = parse_int(last_metrics.get("wifi_ap_count"), 0)
        ap_samples.append(max(0, ap_count))
        if normalize(last_metrics.get("wifi_connected")) == "1":
            connected_samples += 1
        if exit_code == 0:
            scan_ok_samples += 1

        rssi = None
        try:
            rssi = float(normalize(last_metrics.get("wifi_avg_rssi_dbm")))
        except Exception:
            rssi = None
        if rssi is not None:
            rssi_samples.append(rssi)

        link_quality = None
        try:
            link_quality = float(normalize(last_metrics.get("wifi_link_quality")))
        except Exception:
            link_quality = None
        if link_quality is not None:
            link_samples.append(link_quality)

        if emit_live_samples:
            values: dict[str, float] = {"wifi_rssi_sample_index": float(idx)}
            if rssi is not None:
                values["wifi_avg_rssi_dbm"] = float(rssi)
            if link_quality is not None:
                values["wifi_link_quality"] = float(link_quality)
            values["wifi_connected"] = 1.0 if normalize(last_metrics.get("wifi_connected")) == "1" else 0.0
            for metric_key in (
                "wifi_tx_bitrate_mbps",
                "wifi_rx_bitrate_mbps",
                "wifi_signal_dbm",
                "wifi_channel",
                "wifi_freq_mhz",
                "device_cpu_temp_c",
                "device_load1",
            ):
                numeric = _safe_float(last_metrics.get(metric_key))
                if numeric is None:
                    continue
                values[metric_key] = float(numeric)
            _ingest_metrics_http(
                device_id,
                values,
                source=normalize(last_metrics.get("wifi_sample_source")) or "agent-wifi-sampler",
            )

        elapsed = time.monotonic() - sample_start
        sleep_s = max(0.0, float(effective_interval_s) - elapsed)
        if idx < effective_points and sleep_s > 0:
            time.sleep(sleep_s)

    aggregate: dict[str, str] = {
        "wifi_ap_count": str(int(round(sum(ap_samples) / len(ap_samples)))) if ap_samples else "0",
        "wifi_connected": "1" if connected_samples > 0 else "0",
        "wifi_scan_ok": "1" if scan_ok_samples > 0 else "0",
        "wifi_rssi_samples": str(len(rssi_samples)),
        "wifi_sampling_interval_s_requested": str(requested_interval_s),
        "wifi_sampling_duration_s_requested": str(requested_duration_s),
        "wifi_sampling_interval_s_applied": str(effective_interval_s),
        "wifi_sampling_duration_s_applied": str(effective_duration_budget_s),
        "wifi_sampling_points_applied": str(effective_points),
        "wifi_sampling_artifact_stride_applied": str(artifact_stride),
        "wifi_sampling_artifact_samples": str(artifact_samples),
        "artifacts_uploaded_during_measure": str(artifacts_uploaded_during_measure),
        "artifacts_upload_failed_during_measure": str(artifacts_upload_failed_during_measure),
        "wifi_sampling_start_delay_s_requested": str(requested_start_delay_s),
        "wifi_sampling_start_delay_s_applied": str(start_delay_applied_s),
        "wifi_sampling_start_jitter_s_requested": str(requested_start_jitter_s),
        "wifi_sampling_start_jitter_s_applied": str(start_jitter_applied_s),
        "wifi_sampling_start_wait_epoch_s_requested": str(start_wait_epoch_needed_s),
        "wifi_sampling_start_wait_epoch_s_applied": str(start_wait_epoch_applied_s),
        "wifi_sampling_preface_sleep_s_applied": str(preface_sleep_applied_s),
    }
    if rssi_samples:
        aggregate["wifi_avg_rssi_dbm"] = f"{(sum(rssi_samples) / len(rssi_samples)):.2f}"
        aggregate["wifi_rssi_min_dbm"] = f"{min(rssi_samples):.2f}"
        aggregate["wifi_rssi_max_dbm"] = f"{max(rssi_samples):.2f}"
    if link_samples:
        aggregate["wifi_link_quality"] = f"{(sum(link_samples) / len(link_samples)):.2f}"

    wall_ms = int((time.monotonic() - start_monotonic) * 1000.0)
    message = (
        f"wifi sampled {effective_points}x in {effective_duration_budget_s}s "
        f"(interval={effective_interval_s}s, preface={preface_sleep_applied_s}s, rssi_samples={len(rssi_samples)})"
    )
    return 0, max(total_duration_ms, wall_ms), message, aggregate, all_artifacts


def collect_ble_scan(
    timeout_ms: int,
    execute_policy: bool,
    store: RunStore,
    run_id: str,
    cmd_id: str,
    *,
    override_cmdline: str = "",
    override_argv: list[str] | None = None,
    override_env: dict[str, str] | None = None,
) -> tuple[int, int, str, dict[str, str], list[fleet_gateway_v2_pb2.ArtifactRef]]:
    metrics: dict[str, str] = {}
    artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []

    if execute_policy and (override_argv or (override_cmdline and not _is_trivial_cmdline(override_cmdline))):
        exit_code, duration_ms, short, full = run_command_capture(
            override_cmdline,
            override_argv or [],
            override_env or {},
            timeout_ms,
            execute_policy=True,
        )
        try:
            artifacts.append(store.write_text_artifact(run_id, f"ble-{cmd_id}-override", full))
        except Exception:
            log.exception("Failed to persist ble override output artifact")

        adv_count, avg_rssi = _parse_bluetoothctl_scan(full)
        if adv_count > 0 or avg_rssi is not None:
            metrics["ble_adv_count"] = str(max(0, int(adv_count)))
            if avg_rssi is not None:
                metrics["ble_avg_rssi_dbm"] = f"{avg_rssi:.2f}"
            metrics["ble_scan_ok"] = "1"
            metrics.update(_collect_device_status_metrics())
            return exit_code, duration_ms, short or "ble scan override", metrics, artifacts

    if not execute_policy:
        duration_ms = min(timeout_ms, 1000)
        time.sleep(min(1.0, max(0.1, duration_ms / 1000.0)))
        ble_adv_count = max(1, int(duration_ms / 200) + 5)
        sim_metrics = _collect_device_status_metrics()
        sim_metrics.update({"ble_adv_count": str(ble_adv_count), "ble_avg_rssi_dbm": "-60.0", "ble_scan_ok": "1"})
        return (
            0,
            duration_ms,
            "simulated ble scan",
            sim_metrics,
            [],
        )

    # bluetoothctl is the most common unprivileged interface to BlueZ; it may still fail if bluetoothd isn't running.
    exit_code, duration_ms, out = _run_capture_args(
        ["bluetoothctl", "--timeout", str(max(1, int(timeout_ms / 1000))), "scan", "on"],
        timeout_ms,
    )
    adv_count, avg_rssi = _parse_bluetoothctl_scan(out)
    metrics["ble_adv_count"] = str(max(0, int(adv_count)))
    if avg_rssi is not None:
        metrics["ble_avg_rssi_dbm"] = f"{avg_rssi:.2f}"
    # bluetoothctl scan may be intentionally terminated by timeout; treat that as OK if output was captured.
    metrics["ble_scan_ok"] = "1" if exit_code in (0, 124) else "0"
    metrics.update(_collect_device_status_metrics())
    msg = f"bluetoothctl scan {'ok' if exit_code in (0,124) else 'failed'}: adv_count={adv_count}"
    try:
        artifacts.append(store.write_text_artifact(run_id, f"ble-{cmd_id}-scan", out))
    except Exception:
        log.exception("Failed to persist ble scan artifact")
    # Do not fail the run on BLE unavailability by default (many devices lack BLE).
    return 0, duration_ms, msg, metrics, artifacts


def collect_csi_capture(
    timeout_ms: int,
    execute_policy: bool,
    store: RunStore,
    run_id: str,
    cmd_id: str,
    *,
    cmdline: str = "",
    argv: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, int, str, dict[str, str], list[fleet_gateway_v2_pb2.ArtifactRef]]:
    metrics: dict[str, str] = {
        "csi_supported": "0",
        "csi_frames_count": "0",
        "csi_collector_configured": "0",
        "csi_capture_ok": "0",
        "csi_capture_disabled": "0",
        "csi_capture_timeout": "0",
        "csi_capture_evidence_ok": "0",
        "csi_collector_sudo_noninteractive": "0",
        "csi_collector_sudo_prompt": "0",
    }
    artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []
    cfg = {normalize(k): normalize(v) for k, v in (env or {}).items() if normalize(k)}

    if parse_bool(cfg.get("DISABLE_CSI_CAPTURE") or os.environ.get("DISABLE_CSI_CAPTURE"), False):
        metrics["csi_capture_disabled"] = "1"
        try:
            artifacts.append(
                store.write_text_artifact(
                    run_id,
                    f"csi-{cmd_id}-disabled",
                    "CSI capture disabled by configuration (DISABLE_CSI_CAPTURE=true).\n",
                )
            )
        except Exception:
            log.exception("Failed to persist csi disabled note artifact")
        metrics.update(_collect_device_status_metrics())
        return 0, 0, "csi capture disabled", metrics, artifacts

    if not execute_policy:
        duration_ms = min(timeout_ms, 1000)
        time.sleep(min(1.0, max(0.1, duration_ms / 1000.0)))
        frames = max(1, int(duration_ms / 10) + 50)
        metrics["csi_supported"] = "1"
        metrics["csi_frames_count"] = str(frames)
        metrics["csi_collector_configured"] = "1"
        metrics["csi_capture_ok"] = "1"
        metrics["csi_capture_evidence_ok"] = "1"
        metrics.update(_collect_device_status_metrics())
        return 0, duration_ms, "simulated csi capture", metrics, []

    collector_cmdline = ""
    if cmdline and not _is_trivial_cmdline(cmdline):
        collector_cmdline = cmdline
    if not collector_cmdline:
        collector_cmdline = normalize(cfg.get("CSI_COLLECTOR_CMD") or os.environ.get("CSI_COLLECTOR_CMD"))

    collector_argv: list[str] = [normalize(a) for a in (argv or []) if normalize(a)]
    if not collector_argv:
        parsed = normalize(cfg.get("CSI_COLLECTOR_ARGV") or os.environ.get("CSI_COLLECTOR_ARGV"))
        if parsed:
            try:
                collector_argv = [normalize(a) for a in shlex.split(parsed) if normalize(a)]
            except Exception:
                collector_argv = []

    output_patterns = []
    for key in ("CSI_OUTPUT_FILE", "CSI_OUTPUT_PATH", "CSI_OUTPUT_GLOB", "CSI_OUTPUT_GLOBS"):
        raw = normalize(cfg.get(key) or os.environ.get(key))
        if not raw:
            continue
        output_patterns.extend([normalize(row) for row in raw.split(",") if normalize(row)])
    output_patterns.extend(_extract_output_paths_from_command(collector_cmdline, collector_argv))

    frame_pattern = normalize(cfg.get("CSI_FRAMES_REGEX") or os.environ.get("CSI_FRAMES_REGEX"))
    if not frame_pattern:
        frame_pattern = r"\b(?:frames|csi_frames|csi_frames_count)\s*[=:]\s*(\d+)\b"
    min_frames_required = max(0, parse_int(cfg.get("CSI_MIN_FRAMES") or os.environ.get("CSI_MIN_FRAMES"), 0))
    min_output_files_required = max(
        0,
        parse_int(cfg.get("CSI_MIN_OUTPUT_FILES") or os.environ.get("CSI_MIN_OUTPUT_FILES"), 0),
    )
    require_evidence = parse_bool(cfg.get("CSI_REQUIRE_EVIDENCE") or os.environ.get("CSI_REQUIRE_EVIDENCE"), False)
    force_noninteractive_sudo = parse_bool(
        cfg.get("CSI_SUDO_NONINTERACTIVE") or os.environ.get("CSI_SUDO_NONINTERACTIVE"),
        True,
    )

    def parse_frames_from_text(payload: str) -> int | None:
        if not payload:
            return None
        candidates: list[int] = []
        try:
            for match in re.finditer(frame_pattern, payload, flags=re.IGNORECASE):
                groups = match.groups()
                value = groups[-1] if groups else match.group(0)
                parsed = _safe_int(value)
                if parsed is not None:
                    candidates.append(parsed)
        except re.error:
            pass
        # Conservative fallback for common log forms.
        for fallback in (
            r"\bCSI\s+frames\s*:\s*(\d+)\b",
            r"\bcsi[_ -]?packets?\s*[=:]\s*(\d+)\b",
            r"\bpackets\s*captured\s*[=:]\s*(\d+)\b",
        ):
            for match in re.finditer(fallback, payload, flags=re.IGNORECASE):
                parsed = _safe_int(match.group(1))
                if parsed is not None:
                    candidates.append(parsed)
        return max(candidates) if candidates else None

    def parse_frames_from_feitcsi_binary(path: Path) -> int | None:
        # FeitCSI .dat stream is a sequence of [272-byte header][csiDataSize bytes].
        header_len = 272
        try:
            file_size = max(0, int(path.stat().st_size))
        except Exception:
            return None
        if file_size < (header_len + 4):
            return None

        frames = 0
        consumed = 0
        try:
            with path.open("rb") as handle:
                while consumed + 4 <= file_size:
                    raw_size = handle.read(4)
                    if len(raw_size) < 4:
                        break
                    consumed += 4

                    csi_data_size = int.from_bytes(raw_size, byteorder="little", signed=False)
                    record_size = header_len + csi_data_size
                    if csi_data_size <= 0 or record_size <= header_len:
                        break

                    remaining = file_size - consumed
                    skip = record_size - 4
                    if skip > remaining:
                        break

                    handle.seek(skip, os.SEEK_CUR)
                    consumed += skip
                    frames += 1
        except Exception:
            return None

        return frames if frames > 0 else None

    def expand_output_paths(patterns: list[str]) -> list[Path]:
        results: list[Path] = []
        seen: set[str] = set()
        for pattern in patterns:
            expanded = os.path.expandvars(os.path.expanduser(pattern))
            matches = glob.glob(expanded)
            if not matches and Path(expanded).exists():
                matches = [expanded]
            for match in matches:
                p = Path(match)
                if not p.exists() or not p.is_file():
                    continue
                rp = str(p.resolve())
                if rp in seen:
                    continue
                seen.add(rp)
                results.append(p)
        results.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        max_files = max(1, parse_int(cfg.get("CSI_OUTPUT_MAX_FILES") or os.environ.get("CSI_OUTPUT_MAX_FILES"), 5))
        return results[:max_files]

    if collector_cmdline or collector_argv:
        metrics["csi_collector_configured"] = "1"
        if force_noninteractive_sudo:
            metrics["csi_collector_sudo_noninteractive"] = "1"
        exit_code, duration_ms, short, full = run_command_capture(
            collector_cmdline,
            collector_argv,
            cfg,
            timeout_ms,
            True,
            force_noninteractive_sudo=force_noninteractive_sudo,
        )
        metrics["csi_collector_exit_code"] = str(int(exit_code))
        metrics["csi_capture_ok"] = "1" if int(exit_code) == 0 else "0"
        metrics["csi_capture_timeout"] = "1" if int(exit_code) == 124 else "0"
        metrics["csi_supported"] = "1" if int(exit_code) != 127 else "0"
        if re.search(r"(a terminal is required|password is required|sudo: .*password)", full or "", flags=re.IGNORECASE):
            metrics["csi_collector_sudo_prompt"] = "1"

        frames_found = parse_frames_from_text(full)
        imported_files = 0
        imported_bytes = 0

        if frames_found is not None:
            metrics["csi_frames_count"] = str(max(0, int(frames_found)))

        output_files = expand_output_paths(output_patterns)
        max_parse_bytes = max(4096, parse_int(cfg.get("CSI_PARSE_MAX_BYTES") or os.environ.get("CSI_PARSE_MAX_BYTES"), 1024 * 1024))
        for idx, output_file in enumerate(output_files, start=1):
            try:
                artifacts.append(
                    store.import_file_artifact(
                        run_id,
                        output_file,
                        artifact_name=f"csi-{cmd_id}-{idx}-{output_file.name}",
                    )
                )
                imported_files += 1
                imported_bytes += max(0, int(output_file.stat().st_size))
            except Exception:
                log.exception("Failed to import CSI output artifact path=%s", output_file)

            if frames_found is None:
                try:
                    preview = output_file.read_bytes()[:max_parse_bytes].decode("utf-8", errors="replace")
                    parsed = parse_frames_from_text(preview)
                    if parsed is not None:
                        frames_found = parsed
                    else:
                        parsed = parse_frames_from_feitcsi_binary(output_file)
                        if parsed is not None:
                            frames_found = parsed
                    if frames_found is None and output_file.suffix.lower() in {".csv", ".txt", ".log", ".ndjson", ".jsonl"}:
                        line_count = 0
                        with output_file.open("rb") as handle:
                            for line in handle:
                                if not line.strip():
                                    continue
                                line_count += 1
                                if line_count >= 2_000_000:
                                    break
                        if line_count > 1:
                            header_like = preview.splitlines()[0] if preview.splitlines() else ""
                            if header_like and re.search(r"[A-Za-z]", header_like):
                                line_count = max(0, line_count - 1)
                        if line_count > 0:
                            frames_found = line_count
                except Exception:
                    pass

        if imported_files > 0:
            metrics["csi_output_files_count"] = str(imported_files)
            metrics["csi_output_bytes_total"] = str(imported_bytes)

        if frames_found is not None:
            metrics["csi_frames_count"] = str(max(0, int(frames_found)))

        frames_value = max(0, int(metrics.get("csi_frames_count", "0") or 0))
        evidence_ok = False
        if min_frames_required > 0 or min_output_files_required > 0:
            evidence_ok = (frames_value >= min_frames_required) and (imported_files >= min_output_files_required)
        else:
            evidence_ok = frames_value > 0 or imported_files > 0
        metrics["csi_capture_evidence_ok"] = "1" if evidence_ok else "0"

        if int(exit_code) == 124 and evidence_ok:
            # Timeout is expected for long-running collectors; keep timeout marker
            # but accept capture when artifacts/frames prove data was produced.
            exit_code = 0
            metrics["csi_capture_ok"] = "1"

        if require_evidence and not evidence_ok and int(exit_code) == 0:
            exit_code = 65
            metrics["csi_capture_ok"] = "0"

        if int(metrics.get("csi_capture_ok", "0")) == 1 and (frames_found or imported_files > 0):
            metrics["csi_supported"] = "1"

        try:
            artifacts.append(store.write_text_artifact(run_id, f"csi-{cmd_id}-output", full))
        except Exception:
            log.exception("Failed to persist csi output artifact")
        metrics.update(_collect_device_status_metrics())
        msg = short or "csi capture done"
        if require_evidence and not evidence_ok:
            msg = (
                f"csi evidence missing (frames={frames_value}, files={imported_files}, "
                f"min_frames={min_frames_required}, min_files={min_output_files_required})"
            )
        return int(exit_code), duration_ms, msg, metrics, artifacts

    # No capture tool configured.
    try:
        artifacts.append(
            store.write_text_artifact(
                run_id,
                f"csi-{cmd_id}-note",
                "CSI capture not configured on this agent.\n"
                "Set command/env CSI_COLLECTOR_CMD (and optional CSI_OUTPUT_PATH or CSI_OUTPUT_GLOB).\n",
            )
        )
    except Exception:
        log.exception("Failed to persist csi note artifact")
    metrics.update(_collect_device_status_metrics())
    return 0, 0, "csi capture not configured", metrics, artifacts


def main() -> None:
    fleet_host = os.environ.get("FLEET_MANAGER_HOST", "monad-fleet-service")
    fleet_port = int(os.environ.get("FLEET_MANAGER_PORT", "50060"))
    target = f"{fleet_host}:{fleet_port}"

    agent_id = normalize(os.environ.get("AGENT_ID") or os.environ.get("DEVICE_ID") or os.environ.get("DEVICE_MAC") or "agent-v2").lower()
    agent_version = os.environ.get("AGENT_VERSION", "0.2.0")
    requested_mode = mode_from_env(os.environ.get("CONTROL_PLANE_MODE", "RF_SHARING"))
    control_plane_iface = os.environ.get("CONTROL_PLANE_IFACE", "wlan0")
    capabilities = [c.strip() for c in os.environ.get("CAPABILITIES", "csi,ble,wifi").split(",") if c.strip()]
    execute_policy = os.environ.get("EXECUTE_POLICY", "false").lower() in {"1", "true", "yes"}
    max_cycles = int(os.environ.get("MAX_SYNC_CYCLES", "0"))
    default_poll_seconds = int(os.environ.get("DEFAULT_POLL_SECONDS", "30"))
    control_rpc_timeout_s = parse_timeout_seconds("CONTROL_RPC_TIMEOUT_S", 10)
    hello_rpc_timeout_s = parse_timeout_seconds("HELLO_RPC_TIMEOUT_S", control_rpc_timeout_s)
    assignment_rpc_timeout_s = parse_timeout_seconds("ASSIGNMENT_RPC_TIMEOUT_S", control_rpc_timeout_s)
    policy_rpc_timeout_s = parse_timeout_seconds("POLICY_RPC_TIMEOUT_S", max(15, control_rpc_timeout_s))
    ack_prepared_rpc_timeout_s = parse_timeout_seconds("ACK_PREPARED_RPC_TIMEOUT_S", control_rpc_timeout_s)
    data_root = Path(os.environ.get("DATA_ROOT", "./data"))
    sent_retention_days = int(os.environ.get("SENT_RETENTION_DAYS", "14"))
    global_upload_during_measure = parse_bool(os.environ.get("ARTIFACT_UPLOAD_DURING_MEASURE"), False)
    global_artifact_upload_target = _artifact_upload_target(os.environ.get("ARTIFACT_UPLOAD_TARGET"))
    run_artifact_soft_limit_bytes = max(0, parse_int(os.environ.get("RUN_ARTIFACT_SOFT_LIMIT_BYTES"), 0))

    fm_target_for_route = os.environ.get("FLEET_MANAGER_ROUTE_TARGET", fleet_host)

    store = RunStore(data_root, sent_retention_days=sent_retention_days)
    channel = grpc.insecure_channel(target)
    stub = fleet_gateway_v2_pb2_grpc.FleetManagerStub(channel)

    hello = stub.Hello(
        fleet_gateway_v2_pb2.HelloRequest(
            agent=fleet_gateway_v2_pb2.AgentDescriptor(
                agent_id=agent_id,
                hostname=socket.gethostname(),
                agent_version=agent_version,
                interfaces=detect_interfaces(),
                requested_mode=requested_mode,
                control_plane_iface=control_plane_iface,
                fleet_manager_target=target,
                capabilities=capabilities,
            )
        ),
        timeout=hello_rpc_timeout_s,
    )
    log.info("Hello(v2) ok: allowed_mode=%s reason=%s", int(hello.allowed_mode), hello.mode_reason)

    poll = int(hello.recommended_prepare_poll_sec or default_poll_seconds)
    last_policy_id = ""
    cycles = 0

    # Try to deliver any runs persisted from previous process/network interruptions.
    flush_pending_reports(stub, agent_id, store)

    while max_cycles <= 0 or cycles < max_cycles:
        cycles += 1

        assignment = stub.GetAssignment(
            fleet_gateway_v2_pb2.GetAssignmentRequest(agent_id=agent_id),
            timeout=assignment_rpc_timeout_s,
        )
        if assignment.status != fleet_gateway_v2_pb2.GetAssignmentResponse.ASSIGNED:
            log.info("No assignment")
            flush_pending_reports(stub, agent_id, store)
            if reached_max_cycles(cycles, max_cycles):
                break
            time.sleep(poll)
            continue
        log.info(
            "Assigned: experiment_id=%s policy_id=%s window=%s..%s",
            normalize(assignment.experiment_id),
            normalize(assignment.policy_id),
            ts_to_iso(assignment.measurement_from),
            ts_to_iso(assignment.measurement_to),
        )

        policy_resp = stub.GetPolicy(
            fleet_gateway_v2_pb2.GetPolicyRequest(
                agent_id=agent_id,
                experiment_id=assignment.experiment_id,
                last_policy_id=last_policy_id,
            ),
            timeout=policy_rpc_timeout_s,
        )
        if policy_resp.status == fleet_gateway_v2_pb2.GetPolicyResponse.NOT_MODIFIED:
            log.info("Policy not modified: %s", last_policy_id)
            flush_pending_reports(stub, agent_id, store)
            if reached_max_cycles(cycles, max_cycles):
                break
            time.sleep(poll)
            continue
        if policy_resp.status != fleet_gateway_v2_pb2.GetPolicyResponse.OK:
            log.info("No policy available")
            flush_pending_reports(stub, agent_id, store)
            if reached_max_cycles(cycles, max_cycles):
                break
            time.sleep(poll)
            continue

        policy = policy_resp.policy
        route_iface = route_interface_for_target(fm_target_for_route)
        route_verified = True
        if policy.allowed_mode == fleet_gateway_v2_pb2.DUAL_NIC:
            route_verified = bool(route_iface and route_iface == normalize(policy.control_plane_iface))

        prep = stub.AckPrepared(
            fleet_gateway_v2_pb2.AckPreparedRequest(
                agent_id=agent_id,
                experiment_id=policy.experiment_id,
                policy_id=policy.policy_id,
                preparation_id=str(uuid.uuid4()),
                mode=policy.allowed_mode,
                control_plane_iface=policy.control_plane_iface,
                route_verified=route_verified,
                checks_ok=["route_verified"] if route_verified else [],
                warnings=[] if route_verified else [f"route via {route_iface or 'unknown'}"],
            ),
            timeout=ack_prepared_rpc_timeout_s,
        )
        if prep.status != fleet_gateway_v2_pb2.AckPreparedResponse.ACCEPTED:
            log.warning("AckPrepared rejected: %s", prep.reason)
            flush_pending_reports(stub, agent_id, store)
            if reached_max_cycles(cycles, max_cycles):
                break
            time.sleep(poll)
            continue

        run_id = str(uuid.uuid4())
        store.start_run(
            run_id,
            {
                "run_id": run_id,
                "agent_id": agent_id,
                "policy_id": policy.policy_id,
                "experiment_id": policy.experiment_id,
                "started_at": now_utc().isoformat().replace("+00:00", "Z"),
            },
        )

        events: list[fleet_gateway_v2_pb2.Event] = []
        artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []

        def record(event: fleet_gateway_v2_pb2.Event) -> None:
            events.append(event)
            store.append_event(run_id, event)

        record(
            fleet_gateway_v2_pb2.Event(
                event_id=f"{run_id}:run:start",
                run_id=run_id,
                experiment_id=policy.experiment_id,
                policy_id=policy.policy_id,
                agent_id=agent_id,
                timestamp=now_ts(),
                type=fleet_gateway_v2_pb2.RUN_STARTED,
                message="run started",
            )
        )

        commands_failed = 0
        commands_total = 0
        wifi_ap_total = 0
        ble_adv_total = 0
        csi_frames_total = 0
        artifacts_uploaded_during_measure = 0
        artifacts_upload_failed_during_measure = 0
        run_storage_pressure_hits = 0
        latest_observed_metrics: dict[str, str] = {}
        for group in policy.command_groups:
            group_from = getattr(group, "from")
            if not in_window(group_from, group.to):
                continue

            for cmd in group.commands:
                commands_total += 1
                cmd_id = normalize(cmd.id) or f"cmd-{commands_total}"
                cmdline = normalize(cmd.cmdline)
                cmd_type_name = command_type_name(int(cmd.type))
                cmd_argv = [normalize(a) for a in getattr(cmd, "argv", []) if normalize(a)]
                cmd_env = {normalize(k): normalize(v) for k, v in getattr(cmd, "env", {}).items() if normalize(k)}
                upload_during_measure = parse_bool(
                    cmd_env.get("ARTIFACT_UPLOAD_DURING_MEASURE"),
                    global_upload_during_measure,
                )
                artifact_upload_target = _artifact_upload_target(
                    cmd_env.get("ARTIFACT_UPLOAD_TARGET") or global_artifact_upload_target
                )

                record(
                    fleet_gateway_v2_pb2.Event(
                        event_id=f"{run_id}:{cmd_id}:start",
                        run_id=run_id,
                        experiment_id=policy.experiment_id,
                        policy_id=policy.policy_id,
                        agent_id=agent_id,
                        timestamp=now_ts(),
                        type=fleet_gateway_v2_pb2.COMMAND_STARTED,
                        group_id=normalize(group.id),
                        command_id=cmd_id,
                        message=cmdline or (" ".join(cmd_argv) if cmd_argv else cmd_type_name),
                    )
                )

                timeout_ms = int(cmd.timeout_ms or 60000)
                extra_artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []

                if int(cmd.type) == int(fleet_gateway_v2_pb2.WIFI_SCAN):
                    wifi_iface = normalize(os.environ.get("WIFI_SCAN_IFACE") or control_plane_iface)
                    requested_interval_s = parse_int(
                        cmd_env.get("WIFI_SAMPLE_INTERVAL_S") or cmd_env.get("SAMPLE_INTERVAL_S"),
                        0,
                    )
                    requested_duration_s = parse_int(
                        cmd_env.get("WIFI_SAMPLE_DURATION_S") or cmd_env.get("SAMPLE_DURATION_S"),
                        0,
                    )
                    emit_live_samples = parse_bool(
                        cmd_env.get("WIFI_SAMPLE_EMIT_HTTP") or cmd_env.get("SAMPLE_EMIT_HTTP"),
                        True,
                    )
                    # In single-Wi-Fi control-plane mode, avoid extra live HTTP traffic during sampling.
                    # Samples are still persisted locally and uploaded after run completion/replay.
                    allow_wifi_live_ingest = parse_bool(
                        cmd_env.get("ALLOW_WIFI_LIVE_INGEST") or os.environ.get("ALLOW_WIFI_LIVE_INGEST"),
                        False,
                    )
                    if emit_live_samples and route_iface and route_iface.startswith("wl") and not allow_wifi_live_ingest:
                        emit_live_samples = False
                    if requested_interval_s > 0 and requested_duration_s > 0:
                        exit_code, duration_ms, message, parsed, extra_artifacts = collect_wifi_scan_series(
                            wifi_iface,
                            timeout_ms,
                            execute_policy,
                            store,
                            run_id,
                            cmd_id,
                            sample_interval_s=requested_interval_s,
                            sample_duration_s=requested_duration_s,
                            device_id=agent_id,
                            emit_live_samples=emit_live_samples,
                            experiment_id=policy.experiment_id,
                            agent_id=agent_id,
                            upload_during_measure=upload_during_measure,
                            artifact_upload_target=artifact_upload_target,
                            override_cmdline=cmdline,
                            override_argv=cmd_argv,
                            override_env=cmd_env,
                        )
                    else:
                        exit_code, duration_ms, message, parsed, extra_artifacts = collect_wifi_scan(
                            wifi_iface,
                            timeout_ms,
                            execute_policy,
                            store,
                            run_id,
                            cmd_id,
                            override_cmdline=cmdline,
                            override_argv=cmd_argv,
                            override_env=cmd_env,
                        )
                    wifi_ap_count = int(parsed.get("wifi_ap_count", "0") or 0)
                    wifi_ap_total += max(0, wifi_ap_count)
                    event_metrics = parsed
                elif int(cmd.type) == int(fleet_gateway_v2_pb2.BLE_SCAN):
                    exit_code, duration_ms, message, parsed, extra_artifacts = collect_ble_scan(
                        timeout_ms,
                        execute_policy,
                        store,
                        run_id,
                        cmd_id,
                        override_cmdline=cmdline,
                        override_argv=cmd_argv,
                        override_env=cmd_env,
                    )
                    ble_adv_count = int(parsed.get("ble_adv_count", "0") or 0)
                    ble_adv_total += max(0, ble_adv_count)
                    event_metrics = parsed
                elif int(cmd.type) == int(fleet_gateway_v2_pb2.CAPTURE_CSI):
                    exit_code, duration_ms, message, parsed, extra_artifacts = collect_csi_capture(
                        timeout_ms,
                        execute_policy,
                        store,
                        run_id,
                        cmd_id,
                        cmdline=cmdline,
                        argv=cmd_argv,
                        env=cmd_env,
                    )
                    csi_frames_count = int(parsed.get("csi_frames_count", "0") or 0)
                    csi_frames_total += max(0, csi_frames_count)
                    event_metrics = parsed
                else:
                    exec_cmdline = cmdline or (" ".join(cmd_argv) if cmd_argv else "echo no-op")
                    exit_code, duration_ms, message, full = run_command_capture(
                        exec_cmdline,
                        cmd_argv,
                        cmd_env,
                        timeout_ms,
                        execute_policy,
                    )
                    event_metrics = {}
                    # Store raw output for arbitrary shell commands too (useful for parsing later).
                    if full:
                        try:
                            extra_artifacts.append(store.write_text_artifact(run_id, f"cmd-{cmd_id}-output", full))
                        except Exception:
                            log.exception("Failed to persist command output artifact for cmd_id=%s", cmd_id)

                artifacts_uploaded_during_measure += max(
                    0,
                    parse_int((event_metrics or {}).get("artifacts_uploaded_during_measure"), 0),
                )
                artifacts_upload_failed_during_measure += max(
                    0,
                    parse_int((event_metrics or {}).get("artifacts_upload_failed_during_measure"), 0),
                )

                if exit_code != 0:
                    commands_failed += 1

                device_status = _collect_device_status_metrics()
                event_metrics = {
                    "duration_ms": str(max(0, int(duration_ms))),
                    "command_type": cmd_type_name,
                    **{normalize(k): normalize(v) for k, v in (event_metrics or {}).items() if normalize(k)},
                    **{normalize(k): normalize(v) for k, v in device_status.items() if normalize(k)},
                }
                for key, value in event_metrics.items():
                    if not normalize(key):
                        continue
                    latest_observed_metrics[normalize(key)] = normalize(value)

                record(
                    fleet_gateway_v2_pb2.Event(
                        event_id=f"{run_id}:{cmd_id}:end",
                        run_id=run_id,
                        experiment_id=policy.experiment_id,
                        policy_id=policy.policy_id,
                        agent_id=agent_id,
                        timestamp=now_ts(),
                        type=event_type_for_exit(exit_code),
                        group_id=normalize(group.id),
                        command_id=cmd_id,
                        exit_code=exit_code,
                        duration_ms=max(0, int(duration_ms)),
                        message=message,
                        metrics=event_metrics,
                    )
                )

                command_artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []
                try:
                    cmd_artifact = store.write_command_log(
                        run_id=run_id,
                        cmd_id=cmd_id,
                        cmdline=cmdline or (" ".join(cmd_argv) if cmd_argv else cmd_type_name),
                        exit_code=exit_code,
                        duration_ms=max(0, int(duration_ms)),
                        message=message,
                    )
                    command_artifacts.append(cmd_artifact)
                    artifacts.append(cmd_artifact)
                except Exception:
                    log.exception("Failed to persist command artifact for run_id=%s command_id=%s", run_id, cmd_id)

                for artifact in extra_artifacts:
                    command_artifacts.append(artifact)
                    artifacts.append(artifact)

                if run_artifact_soft_limit_bytes > 0:
                    run_artifacts_bytes = store.run_artifacts_size_bytes(run_id)
                    if run_artifacts_bytes >= run_artifact_soft_limit_bytes:
                        run_storage_pressure_hits += 1
                        latest_observed_metrics["run_artifacts_bytes_local"] = str(run_artifacts_bytes)
                        latest_observed_metrics["run_artifacts_soft_limit_bytes"] = str(run_artifact_soft_limit_bytes)
                        # Force opportunistic upload under storage pressure.
                        upload_during_measure = True

                if upload_during_measure and command_artifacts:
                    uploaded_now, failed_now = opportunistic_upload_artifacts_to_elab(
                        run_id,
                        policy.experiment_id,
                        agent_id,
                        command_artifacts,
                        store,
                        enabled=True,
                        target=artifact_upload_target,
                    )
                    artifacts_uploaded_during_measure += uploaded_now
                    artifacts_upload_failed_during_measure += failed_now

                if exit_code != 0 and group.failure_mode == fleet_gateway_v2_pb2.FAIL_FAST:
                    break

        status = "OK" if commands_failed == 0 else "FAILED"
        record(
            fleet_gateway_v2_pb2.Event(
                event_id=f"{run_id}:run:end",
                run_id=run_id,
                experiment_id=policy.experiment_id,
                policy_id=policy.policy_id,
                agent_id=agent_id,
                timestamp=now_ts(),
                type=fleet_gateway_v2_pb2.RUN_FINISHED,
                message=status,
                metrics={
                    "run_state": "COMPLETED" if commands_failed == 0 else "FAILED",
                    "commands_total": str(commands_total),
                    "commands_failed": str(commands_failed),
                    "wifi_ap_total": str(wifi_ap_total),
                    "ble_adv_total": str(ble_adv_total),
                    "csi_frames_total": str(csi_frames_total),
                },
            )
        )

        try:
            summary_artifact = store.write_summary_artifact(
                run_id,
                {
                    "run_id": run_id,
                    "status": status,
                    "commands_total": commands_total,
                    "commands_failed": commands_failed,
                    "wifi_ap_total": wifi_ap_total,
                    "ble_adv_total": ble_adv_total,
                    "csi_frames_total": csi_frames_total,
                    "artifacts_uploaded_during_measure": artifacts_uploaded_during_measure,
                    "artifacts_upload_failed_during_measure": artifacts_upload_failed_during_measure,
                    "run_storage_pressure_hits": run_storage_pressure_hits,
                    "latest_metrics": latest_observed_metrics,
                    "generated_at": now_utc().isoformat().replace("+00:00", "Z"),
                },
            )
            artifacts.append(summary_artifact)
            if global_upload_during_measure:
                uploaded_now, failed_now = opportunistic_upload_artifacts_to_elab(
                    run_id,
                    policy.experiment_id,
                    agent_id,
                    [summary_artifact],
                    store,
                    enabled=True,
                    target=global_artifact_upload_target,
                )
                artifacts_uploaded_during_measure += uploaded_now
                artifacts_upload_failed_during_measure += failed_now
        except Exception:
            log.exception("Failed to persist summary artifact for run_id=%s", run_id)

        latest_observed_metrics["run_artifacts_bytes_local"] = str(store.run_artifacts_size_bytes(run_id))
        if run_artifact_soft_limit_bytes > 0:
            latest_observed_metrics["run_artifacts_soft_limit_bytes"] = str(run_artifact_soft_limit_bytes)

        summary_metrics = {
            "commands_total": str(commands_total),
            "commands_failed": str(commands_failed),
            "artifacts_total": str(len(artifacts)),
            "artifacts_uploaded_during_measure": str(artifacts_uploaded_during_measure),
            "artifacts_upload_failed_during_measure": str(artifacts_upload_failed_during_measure),
            "run_storage_pressure_hits": str(run_storage_pressure_hits),
            "wifi_ap_total": str(wifi_ap_total),
            "ble_adv_total": str(ble_adv_total),
            "csi_frames_total": str(csi_frames_total),
            "spool_state": "pending_upload",
            "fleet_interface_version": "v3",
        }
        for key in (
            "wifi_avg_rssi_dbm",
            "wifi_signal_dbm",
            "wifi_link_quality",
            "wifi_tx_bitrate_mbps",
            "wifi_rx_bitrate_mbps",
            "wifi_channel",
            "wifi_freq_mhz",
            "wifi_tx_power_dbm",
            "wifi_if_rx_bytes",
            "wifi_if_tx_bytes",
            "ble_avg_rssi_dbm",
            "ble_scan_ok",
            "csi_supported",
            "csi_collector_configured",
            "csi_capture_ok",
            "csi_capture_disabled",
            "csi_output_files_count",
            "csi_output_bytes_total",
            "run_artifacts_bytes_local",
            "run_artifacts_soft_limit_bytes",
            "device_cpu_temp_c",
            "device_load1",
            "device_uptime_s",
            "device_mem_available_bytes",
        ):
            value = normalize(latest_observed_metrics.get(key))
            if value:
                summary_metrics[key] = value

        report = fleet_gateway_v2_pb2.Report(
            run_id=run_id,
            experiment_id=policy.experiment_id,
            policy_id=policy.policy_id,
            agent_id=agent_id,
            started_at=events[0].timestamp,
            finished_at=events[-1].timestamp,
            status=status,
            events=events,
            artifacts=artifacts,
            summary_metrics=summary_metrics,
        )

        store.persist_report(report)
        last_policy_id = policy.policy_id

        sent_ids = flush_pending_reports(stub, agent_id, store)
        if run_id in sent_ids:
            log.info("Run %s stored and reported", run_id)
        else:
            log.warning("Run %s stored locally and queued for later upload", run_id)

        if reached_max_cycles(cycles, max_cycles):
            break
        time.sleep(poll)


if __name__ == "__main__":
    main()
