#!/usr/bin/env python3
import base64
import glob
import hashlib
import io
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
import sqlite3
import subprocess
import tarfile
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

import fleet_gateway_v3_pb2 as fleet_gateway_v2_pb2
import fleet_gateway_v3_pb2_grpc as fleet_gateway_v2_pb2_grpc


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

_UPLOAD_WINDOW_ENV_KEYS = (
    "UPLOAD_WINDOW_FROM",
    "UPLOAD_WINDOW_TO",
    "UPLOAD_WINDOW_REQUIRED",
    "UPLOAD_SLOT_COUNT",
    "UPLOAD_SLOT_JITTER_S",
    "PROM_REMOTE_WRITE_ON_CMD",
    "PROM_REMOTE_WRITE_OFF_CMD",
    "PROM_REMOTE_WRITE_CMD_TIMEOUT_S",
)


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


def _parse_csv_tokens(value: Any) -> list[str]:
    text = normalize(value)
    if not text:
        return []
    return [normalize(part) for part in text.split(",") if normalize(part)]


def _normalize_sink_name(value: Any) -> str:
    name = normalize(value).lower()
    if not name:
        return ""
    if name in {"none", "disabled", "off", "null"}:
        return "none"
    if name in {"elab", "elabftw"}:
        return "elabftw"
    if name in {"fleet", "fleet_http", "fleet-http", "ingest", "http_ingest"}:
        return "fleet_http"
    if name in {"webhook", "http", "http_webhook"}:
        return "webhook"
    if name in {"sqlite", "sqlite3", "db"}:
        return "sqlite"
    return name


def _resolve_sink_list(primary: Any, fallback: Any = "") -> list[str]:
    tokens = _parse_csv_tokens(primary) or _parse_csv_tokens(fallback)
    if not tokens:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        normalized = _normalize_sink_name(token)
        if not normalized or normalized == "none" or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


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


def _parse_datetime_or_epoch(value: Any) -> datetime | None:
    text = normalize(value)
    if not text:
        return None

    if re.fullmatch(r"-?\d+", text):
        try:
            raw = int(text)
            if abs(raw) >= 1_000_000_000_000:
                raw = int(raw / 1000)
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except Exception:
            return None

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _stable_hash_int(*parts: Any) -> int:
    seed = "|".join(normalize(part).lower() for part in parts if normalize(part))
    if not seed:
        seed = "agent-v2"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _default_upload_window_config() -> dict[str, Any]:
    return {
        "upload_window_from": None,
        "upload_window_to": None,
        "upload_window_required": False,
        "upload_slot_count": 1,
        "upload_slot_jitter_s": 0,
        "prom_remote_write_on_cmd": "",
        "prom_remote_write_off_cmd": "",
        "prom_remote_write_cmd_timeout_s": 12,
    }


def _apply_upload_window_overrides(base_cfg: dict[str, Any], source: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(base_cfg)
    src = source or {}
    if not isinstance(src, dict):
        return cfg

    start_dt = _parse_datetime_or_epoch(src.get("UPLOAD_WINDOW_FROM"))
    end_dt = _parse_datetime_or_epoch(src.get("UPLOAD_WINDOW_TO"))
    if start_dt is not None:
        cfg["upload_window_from"] = start_dt
    if end_dt is not None:
        cfg["upload_window_to"] = end_dt

    required_raw = normalize(src.get("UPLOAD_WINDOW_REQUIRED"))
    if required_raw:
        cfg["upload_window_required"] = parse_bool(required_raw, cfg.get("upload_window_required", False))

    slot_count_raw = normalize(src.get("UPLOAD_SLOT_COUNT"))
    if slot_count_raw:
        try:
            cfg["upload_slot_count"] = max(1, int(slot_count_raw))
        except Exception:
            pass

    jitter_raw = normalize(src.get("UPLOAD_SLOT_JITTER_S"))
    if jitter_raw:
        try:
            cfg["upload_slot_jitter_s"] = max(0, int(jitter_raw))
        except Exception:
            pass

    on_cmd = normalize(src.get("PROM_REMOTE_WRITE_ON_CMD"))
    off_cmd = normalize(src.get("PROM_REMOTE_WRITE_OFF_CMD"))
    if on_cmd:
        cfg["prom_remote_write_on_cmd"] = on_cmd
    if off_cmd:
        cfg["prom_remote_write_off_cmd"] = off_cmd

    cmd_timeout_raw = normalize(src.get("PROM_REMOTE_WRITE_CMD_TIMEOUT_S"))
    if cmd_timeout_raw:
        try:
            cfg["prom_remote_write_cmd_timeout_s"] = max(2, int(cmd_timeout_raw))
        except Exception:
            pass

    return cfg


def _upload_window_config_from_env() -> dict[str, Any]:
    return _apply_upload_window_overrides(_default_upload_window_config(), dict(os.environ))


def _extract_policy_upload_window_overrides(policy: fleet_gateway_v2_pb2.Policy) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for group in policy.command_groups:
        for cmd in group.commands:
            env_map = dict(getattr(cmd, "env", {}) or {})
            if not isinstance(env_map, dict):
                continue
            for key in _UPLOAD_WINDOW_ENV_KEYS:
                if key in overrides:
                    continue
                value = normalize(env_map.get(key))
                if value:
                    overrides[key] = value
    return overrides


def _upload_slot_window_bounds(
    cfg: dict[str, Any],
    agent_id: str,
) -> tuple[datetime | None, datetime | None]:
    start_dt = cfg.get("upload_window_from")
    end_dt = cfg.get("upload_window_to")
    if not isinstance(start_dt, datetime) or not isinstance(end_dt, datetime):
        return None, None
    if end_dt <= start_dt:
        return start_dt, end_dt

    slot_count = max(1, int(cfg.get("upload_slot_count", 1) or 1))
    slot_index = _stable_hash_int(agent_id, "upload-slot") % slot_count
    duration_s = max(0.0, float((end_dt - start_dt).total_seconds()))
    if duration_s <= 0.0:
        return start_dt, end_dt

    slot_s = duration_s / float(slot_count)
    slot_start = start_dt + timedelta(seconds=(slot_s * slot_index))
    slot_end = start_dt + timedelta(seconds=(slot_s * (slot_index + 1)))

    jitter_cap_s = max(0, int(cfg.get("upload_slot_jitter_s", 0) or 0))
    slot_span_s = max(0, int((slot_end - slot_start).total_seconds()))
    if jitter_cap_s > 0 and slot_span_s > 1:
        applied_cap = min(jitter_cap_s, max(0, slot_span_s - 1))
        if applied_cap > 0:
            jitter_s = _stable_hash_int(agent_id, "upload-jitter", int(start_dt.timestamp())) % (applied_cap + 1)
            slot_start = slot_start + timedelta(seconds=jitter_s)
    return slot_start, slot_end


def _upload_window_send_allowed(
    cfg: dict[str, Any],
    agent_id: str,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    now_dt = now or now_utc()
    start_dt = cfg.get("upload_window_from")
    end_dt = cfg.get("upload_window_to")
    required = bool(cfg.get("upload_window_required", False))

    if not isinstance(start_dt, datetime) or not isinstance(end_dt, datetime):
        if required:
            return False, "window_required_missing"
        return True, "window_not_configured"

    if end_dt <= start_dt:
        return False, "window_invalid"

    if now_dt < start_dt:
        return False, "before_upload_window"
    if now_dt > end_dt:
        return False, "after_upload_window"

    slot_start, slot_end = _upload_slot_window_bounds(cfg, agent_id)
    if slot_start is None or slot_end is None:
        return True, "window_active_no_slot"
    if slot_end <= slot_start:
        return False, "slot_invalid"
    if now_dt < slot_start:
        return False, "before_device_slot"
    if now_dt > slot_end:
        return False, "after_device_slot"
    return True, "slot_active"


def route_interface_for_target(target_host: str) -> str:
    if not target_host:
        return ""
    route_target = _resolve_route_probe_target(target_host)
    if not route_target:
        return ""
    try:
        out = subprocess.check_output(["ip", "route", "get", route_target], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""
    match = re.search(r"\bdev\s+(\S+)", out)
    return match.group(1) if match else ""


def _resolve_route_probe_target(target_host: str) -> str:
    target = normalize(target_host)
    if not target:
        return ""
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", target) or ":" in target:
        return target
    ipv6_candidate = ""
    try:
        infos = socket.getaddrinfo(target, None, type=socket.SOCK_STREAM)
    except Exception:
        infos = []
    for family, _, _, _, sockaddr in infos:
        if not sockaddr:
            continue
        candidate = normalize(sockaddr[0])
        if not candidate:
            continue
        if family == socket.AF_INET:
            return candidate
        if family == socket.AF_INET6 and not ipv6_candidate:
            ipv6_candidate = candidate
    if ipv6_candidate:
        return ipv6_candidate

    for cmd in (["getent", "ahostsv4", target], ["getent", "hosts", target]):
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        except Exception:
            continue
        for raw in (out or "").splitlines():
            line = normalize(raw)
            if not line:
                continue
            candidate = normalize(line.split()[0])
            if not candidate:
                continue
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", candidate) or ":" in candidate:
                return candidate
    return target


def default_ipv4_gateway(*, iface: str = "") -> str:
    cmd = ["ip", "-4", "route", "show", "default"]
    if normalize(iface):
        cmd.extend(["dev", normalize(iface)])
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""
    match = re.search(r"\bvia\s+(\d+\.\d+\.\d+\.\d+)\b", out)
    return match.group(1) if match else ""


def _iface_exists(iface: str) -> bool:
    name = normalize(iface)
    return bool(name and Path(f"/sys/class/net/{name}").exists())


def _list_iw_dev_interfaces(iw_bin: str | None) -> list[tuple[str, str]]:
    if not iw_bin:
        return []
    rc, _, out = _run_capture_args([iw_bin, "dev"], 3000)
    if rc != 0:
        return []

    interfaces: list[tuple[str, str]] = []
    current_name = ""
    current_type = ""
    for raw in (out or "").splitlines():
        line = raw.strip()
        if line.startswith("Interface "):
            if current_name:
                interfaces.append((current_name, current_type))
            current_name = normalize(line.split("Interface ", 1)[1])
            current_type = ""
            continue
        if line.startswith("type ") and current_name:
            current_type = normalize(line.split("type ", 1)[1]).lower()

    if current_name:
        interfaces.append((current_name, current_type))
    return interfaces


def _resolve_wifi_scan_iface(requested_iface: str, iw_bin: str | None) -> tuple[str, dict[str, str], str]:
    requested = normalize(requested_iface)
    metrics: dict[str, str] = {
        "wifi_scan_iface_requested": requested,
    }
    if _iface_exists(requested):
        metrics["wifi_scan_iface_used"] = requested
        return requested, metrics, ""

    candidates_managed: list[str] = []
    candidates_other: list[str] = []
    for name, iface_type in _list_iw_dev_interfaces(iw_bin):
        if not _iface_exists(name):
            continue
        iface_type_norm = normalize(iface_type).lower()
        if iface_type_norm in {"managed", "station"}:
            candidates_managed.append(name)
        elif iface_type_norm and iface_type_norm != "monitor":
            candidates_other.append(name)
        elif name.startswith("wl") and "mon" not in name.lower():
            candidates_other.append(name)

    if not candidates_managed and not candidates_other:
        try:
            for p in sorted(Path("/sys/class/net").iterdir()):
                name = normalize(p.name)
                if not name or not name.startswith("wl"):
                    continue
                if "mon" in name.lower():
                    continue
                candidates_other.append(name)
        except Exception:
            pass

    fallback = (candidates_managed + candidates_other)[0] if (candidates_managed or candidates_other) else ""
    if fallback:
        metrics["wifi_scan_iface_used"] = fallback
        metrics["wifi_scan_iface_fallback"] = "1"
        note = f"configured wifi iface '{requested or '<unset>'}' missing; using '{fallback}'"
        return fallback, metrics, note

    metrics["wifi_scan_iface_used"] = ""
    metrics["wifi_scan_iface_missing"] = "1"
    note = f"wifi scan failed: interface '{requested or '<unset>'}' missing and no fallback interface found"
    return "", metrics, note


def _find_alternate_wifi_iface(current_iface: str, iw_bin: str | None) -> str:
    current = normalize(current_iface)
    managed: list[str] = []
    other: list[str] = []
    for name, iface_type in _list_iw_dev_interfaces(iw_bin):
        if not _iface_exists(name) or name == current:
            continue
        iface_type_norm = normalize(iface_type).lower()
        if iface_type_norm in {"managed", "station"}:
            managed.append(name)
        elif iface_type_norm and iface_type_norm != "monitor":
            other.append(name)
        elif name.startswith("wl") and "mon" not in name.lower():
            other.append(name)

    if not managed and not other:
        try:
            for p in sorted(Path("/sys/class/net").iterdir()):
                name = normalize(p.name)
                if not name or name == current or not name.startswith("wl"):
                    continue
                if "mon" in name.lower():
                    continue
                other.append(name)
        except Exception:
            pass

    return (managed + other)[0] if (managed or other) else ""


def _run_local_cmd(cmd: list[str], *, timeout_s: int = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_s)),
            check=False,
        )
        return int(proc.returncode), _combined_output(proc.stdout, proc.stderr)
    except Exception as exc:
        return 127, str(exc)


def _set_prom_remote_write_mode(
    cfg: dict[str, Any],
    *,
    enabled: bool,
    reason: str = "",
) -> tuple[bool | None, str]:
    key = "prom_remote_write_on_cmd" if enabled else "prom_remote_write_off_cmd"
    cmd = normalize(cfg.get(key))
    if not cmd:
        return None, "not_configured"

    timeout_s = max(2, int(cfg.get("prom_remote_write_cmd_timeout_s", 12) or 12))
    rc, out = _run_local_cmd(["bash", "-lc", cmd], timeout_s=timeout_s)
    if rc == 0:
        return True, "ok"

    mode = "on" if enabled else "off"
    log.warning(
        "Prometheus remote_write toggle failed mode=%s reason=%s rc=%s out=%s",
        mode,
        normalize(reason),
        rc,
        _short_message(out, 400),
    )
    return False, f"rc={rc}"


def restart_control_plane_before_report(
    iface: str,
    target_host: str,
    *,
    down_s: int,
    wait_s: int,
    custom_cmd: str,
    allow_wifi_restart: bool,
) -> bool:
    iface = normalize(iface)
    if not iface and not custom_cmd:
        log.warning("Pre-report reconnect skipped: no interface configured")
        return False

    if custom_cmd:
        log.info("Pre-report reconnect: running custom command")
        rc, out = _run_local_cmd(["bash", "-lc", custom_cmd], timeout_s=max(10, wait_s + down_s + 10))
        if rc != 0:
            log.warning("Pre-report reconnect custom command failed rc=%s out=%s", rc, _short_message(out, 500))
            return False
    else:
        if iface.startswith("wl") and not allow_wifi_restart:
            log.warning(
                "Pre-report reconnect skipped on Wi-Fi iface=%s (set ALLOW_WIFI_CONTROL_PLANE_RESTART=true to force)",
                iface,
            )
            return False
        down_ok = False
        for down_cmd in (["sudo", "-n", "ip", "link", "set", iface, "down"], ["ip", "link", "set", iface, "down"]):
            rc, out = _run_local_cmd(down_cmd, timeout_s=10)
            if rc == 0:
                down_ok = True
                break
            log.debug("Pre-report reconnect down failed cmd=%s rc=%s out=%s", down_cmd, rc, _short_message(out, 300))
        if not down_ok:
            log.warning("Pre-report reconnect failed: unable to set %s down", iface)
            return False

        if down_s > 0:
            time.sleep(max(0, down_s))

        up_ok = False
        for up_cmd in (["sudo", "-n", "ip", "link", "set", iface, "up"], ["ip", "link", "set", iface, "up"]):
            rc, out = _run_local_cmd(up_cmd, timeout_s=10)
            if rc == 0:
                up_ok = True
                break
            log.debug("Pre-report reconnect up failed cmd=%s rc=%s out=%s", up_cmd, rc, _short_message(out, 300))
        if not up_ok:
            log.warning("Pre-report reconnect failed: unable to set %s up", iface)
            return False

    if not target_host:
        return True

    deadline = time.time() + max(0, wait_s)
    while time.time() <= deadline:
        route_iface = route_interface_for_target(target_host)
        if route_iface:
            log.info("Pre-report reconnect route restored via %s", route_iface)
            return True
        time.sleep(1)

    log.warning("Pre-report reconnect did not restore route to %s within %ss", target_host, wait_s)
    return False


def _iface_has_ipv4(iface: str) -> bool:
    name = normalize(iface)
    if not name:
        return False
    rc, out = _run_local_cmd(["ip", "-4", "-o", "addr", "show", "dev", name], timeout_s=5)
    if rc != 0:
        return False
    return bool(re.search(r"\binet\s+\d+\.\d+\.\d+\.\d+/\d+\b", out or ""))


def wait_for_route_ready(
    target_host: str,
    *,
    expected_iface: str = "",
    timeout_s: int = 30,
    require_ipv4: bool = True,
) -> tuple[bool, str]:
    host = normalize(target_host)
    expect = normalize(expected_iface)
    if not host:
        return True, ""

    deadline = time.time() + max(1, int(timeout_s))
    last_iface = ""
    while time.time() <= deadline:
        route_iface = route_interface_for_target(host)
        last_iface = route_iface
        if route_iface:
            if expect and route_iface != expect:
                time.sleep(1)
                continue
            if require_ipv4 and not _iface_has_ipv4(route_iface):
                time.sleep(1)
                continue
            return True, route_iface
        time.sleep(1)
    return False, last_iface


def _run_first_success(commands: list[list[str]], *, timeout_s: int = 15) -> tuple[bool, int, str, list[str]]:
    last_rc = 127
    last_out = ""
    last_cmd: list[str] = []
    for cmd in commands:
        if not cmd:
            continue
        rc, out = _run_local_cmd(cmd, timeout_s=timeout_s)
        last_rc = rc
        last_out = out
        last_cmd = cmd
        if rc == 0:
            return True, rc, out, cmd
    return False, last_rc, last_out, last_cmd


def _detect_active_wifi_profile(iface: str) -> str:
    name = normalize(iface)
    for cmd in (
        ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", name],
        ["sudo", "-n", "nmcli", "-g", "GENERAL.CONNECTION", "device", "show", name],
    ):
        if not name:
            break
        rc, out = _run_local_cmd(cmd, timeout_s=8)
        if rc != 0:
            continue
        for row in (out or "").splitlines():
            candidate = normalize(row)
            if candidate and candidate not in {"--", "(unknown)", "(null)"}:
                return candidate

    for cmd in (
        ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
        ["sudo", "-n", "nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
    ):
        rc, out = _run_local_cmd(cmd, timeout_s=8)
        if rc != 0:
            continue
        for row in (out or "").splitlines():
            line = normalize(row)
            if not line:
                continue
            parts = line.rsplit(":", 1)
            if len(parts) != 2:
                continue
            profile_name = normalize(parts[0])
            profile_type = normalize(parts[1]).lower()
            if profile_name and profile_type == "wifi":
                return profile_name
    return ""


def cleanup_csi_collectors(reason: str, *, patterns_csv: str = "") -> tuple[int, list[str]]:
    raw_patterns = [normalize(row) for row in normalize(patterns_csv).split(",") if normalize(row)]
    patterns = raw_patterns or ["feitcsi"]
    killed = 0
    notes: list[str] = []
    for pattern in patterns:
        ok, rc, out, cmd = _run_first_success(
            [
                ["pkill", "-f", pattern],
                ["sudo", "-n", "pkill", "-f", pattern],
            ],
            timeout_s=10,
        )
        if ok:
            killed += 1
            notes.append(f"{' '.join(cmd)} rc={rc}")
            continue
        # rc=1 from pkill commonly means "no process matched"; treat as informational.
        if rc == 1:
            notes.append(f"no-match pattern={pattern}")
        else:
            notes.append(f"failed pattern={pattern} rc={rc} out={_short_message(out, 200)}")
    if killed > 0:
        log.info("CSI cleanup (%s): killed=%d notes=%s", normalize(reason) or "unspecified", killed, "; ".join(notes))
    else:
        log.debug("CSI cleanup (%s): no active collectors (%s)", normalize(reason) or "unspecified", "; ".join(notes))
    return killed, notes


def recover_single_radio_control_plane(
    iface: str,
    target_host: str,
    *,
    wifi_profile: str,
    max_attempts: int,
    down_s: int,
    route_wait_s: int,
    custom_cmd: str,
    csi_kill_patterns: str,
) -> bool:
    control_iface = normalize(iface)
    route_target = normalize(target_host)
    profile_raw = normalize(wifi_profile)
    profile_auto = profile_raw.lower() in {"", "auto", "detect", "active", "default"}
    if not control_iface:
        log.warning("Single-radio recovery skipped: no control iface configured")
        return False

    attempts = max(1, int(max_attempts))
    base_route_wait_s = max(5, int(route_wait_s))
    fallback_route_wait_s = max(8, min(20, base_route_wait_s // 2))
    for attempt in range(1, attempts + 1):
        profile = profile_raw
        if profile_auto:
            profile = _detect_active_wifi_profile(control_iface)
            if profile:
                log.info(
                    "Single-radio recovery auto-detected Wi-Fi profile '%s' on iface=%s",
                    profile,
                    control_iface,
                )
            elif attempt == 1:
                log.warning(
                    "Single-radio recovery could not auto-detect active Wi-Fi profile on iface=%s; "
                    "falling back to 'nmcli device connect'",
                    control_iface,
                )

        log.info(
            "Single-radio recovery attempt %d/%d iface=%s profile=%s target=%s",
            attempt,
            attempts,
            control_iface,
            profile or "<unset>",
            route_target or "<unset>",
        )

        cleanup_csi_collectors("single-radio-recovery", patterns_csv=csi_kill_patterns)

        down_ok, down_rc, down_out, _ = _run_first_success(
            [
                ["sudo", "-n", "ip", "link", "set", control_iface, "down"],
                ["ip", "link", "set", control_iface, "down"],
            ],
            timeout_s=12,
        )
        if not down_ok:
            log.warning(
                "Single-radio recovery: failed to set iface down (%s) rc=%s out=%s",
                control_iface,
                down_rc,
                _short_message(down_out, 240),
            )
        if down_s > 0:
            time.sleep(max(0, int(down_s)))

        up_ok, up_rc, up_out, _ = _run_first_success(
            [
                ["sudo", "-n", "ip", "link", "set", control_iface, "up"],
                ["ip", "link", "set", control_iface, "up"],
            ],
            timeout_s=12,
        )
        if not up_ok:
            log.warning(
                "Single-radio recovery: failed to set iface up (%s) rc=%s out=%s",
                control_iface,
                up_rc,
                _short_message(up_out, 240),
                )

        nm_connect_ok = False
        if profile:
            nm_ok, nm_rc, nm_out, _ = _run_first_success(
                [
                    ["nmcli", "connection", "up", profile],
                    ["sudo", "-n", "nmcli", "connection", "up", profile],
                ],
                timeout_s=20,
            )
            if not nm_ok:
                log.warning(
                    "Single-radio recovery: nmcli up failed profile=%s rc=%s out=%s",
                    profile,
                    nm_rc,
                    _short_message(nm_out, 320),
                )
            else:
                nm_connect_ok = True

        if not nm_connect_ok:
            nm_dev_ok, nm_dev_rc, nm_dev_out, _ = _run_first_success(
                [
                    ["nmcli", "device", "connect", control_iface],
                    ["sudo", "-n", "nmcli", "device", "connect", control_iface],
                ],
                timeout_s=20,
            )
            if not nm_dev_ok:
                log.warning(
                    "Single-radio recovery: nmcli device connect failed iface=%s rc=%s out=%s",
                    control_iface,
                    nm_dev_rc,
                    _short_message(nm_dev_out, 320),
                )

        if custom_cmd:
            rc, out = _run_local_cmd(["bash", "-lc", custom_cmd], timeout_s=max(10, route_wait_s + 10))
            if rc != 0:
                log.warning("Single-radio recovery custom cmd failed rc=%s out=%s", rc, _short_message(out, 320))

        ready, route_iface = wait_for_route_ready(
            route_target,
            expected_iface=control_iface,
            timeout_s=base_route_wait_s,
            require_ipv4=True,
        )
        if ready:
            log.info("Single-radio recovery succeeded on iface=%s", route_iface or control_iface)
            return True
        log.warning(
            "Single-radio recovery attempt %d/%d initial stage did not restore route to %s; "
            "running hard-reset fallbacks",
            attempt,
            attempts,
            route_target or "<unset>",
        )

        # Fallback 1: ask NetworkManager to reconnect the device directly.
        _run_first_success(
            [
                ["nmcli", "device", "disconnect", control_iface],
                ["sudo", "-n", "nmcli", "device", "disconnect", control_iface],
            ],
            timeout_s=12,
        )
        _run_first_success(
            [
                ["nmcli", "device", "connect", control_iface],
                ["sudo", "-n", "nmcli", "device", "connect", control_iface],
            ],
            timeout_s=20,
        )
        if profile:
            _run_first_success(
                [
                    ["nmcli", "connection", "up", profile],
                    ["sudo", "-n", "nmcli", "connection", "up", profile],
                ],
                timeout_s=20,
            )
        ready, route_iface = wait_for_route_ready(
            route_target,
            expected_iface=control_iface,
            timeout_s=fallback_route_wait_s,
            require_ipv4=True,
        )
        if ready:
            log.info("Single-radio recovery succeeded after nmcli reconnect on iface=%s", route_iface or control_iface)
            return True

        # Fallback 2: hard-reset Wi-Fi radio using rfkill.
        rf_block_ok, _, _, _ = _run_first_success(
            [
                ["sudo", "-n", "rfkill", "block", "wifi"],
                ["rfkill", "block", "wifi"],
            ],
            timeout_s=10,
        )
        if rf_block_ok:
            time.sleep(1)
        _run_first_success(
            [
                ["sudo", "-n", "rfkill", "unblock", "wifi"],
                ["rfkill", "unblock", "wifi"],
            ],
            timeout_s=10,
        )
        time.sleep(1)
        _run_first_success(
            [
                ["sudo", "-n", "ip", "link", "set", control_iface, "up"],
                ["ip", "link", "set", control_iface, "up"],
            ],
            timeout_s=12,
        )
        if profile:
            _run_first_success(
                [
                    ["nmcli", "connection", "up", profile],
                    ["sudo", "-n", "nmcli", "connection", "up", profile],
                ],
                timeout_s=20,
            )
        ready, route_iface = wait_for_route_ready(
            route_target,
            expected_iface=control_iface,
            timeout_s=fallback_route_wait_s,
            require_ipv4=True,
        )
        if ready:
            log.info("Single-radio recovery succeeded after rfkill reset on iface=%s", route_iface or control_iface)
            return True

        # Fallback 3: restart NetworkManager and bring profile up once more.
        _run_first_success(
            [
                ["sudo", "-n", "systemctl", "restart", "NetworkManager"],
                ["systemctl", "restart", "NetworkManager"],
            ],
            timeout_s=25,
        )
        time.sleep(2)
        if profile:
            _run_first_success(
                [
                    ["nmcli", "connection", "up", profile],
                    ["sudo", "-n", "nmcli", "connection", "up", profile],
                ],
                timeout_s=20,
            )
        ready, route_iface = wait_for_route_ready(
            route_target,
            expected_iface=control_iface,
            timeout_s=fallback_route_wait_s,
            require_ipv4=True,
        )
        if ready:
            log.info(
                "Single-radio recovery succeeded after NetworkManager restart on iface=%s",
                route_iface or control_iface,
            )
            return True

        log.warning(
            "Single-radio recovery attempt %d/%d failed after hard-reset fallbacks target=%s",
            attempt,
            attempts,
            route_target or "<unset>",
        )

    return False


def request_controlled_reboot(reason: str) -> bool:
    msg = normalize(reason) or "single-radio recovery failure"
    marker = Path("/tmp/fleet-agent-reboot-request.txt")
    try:
        marker.write_text(f"{now_utc().isoformat().replace('+00:00', 'Z')} {msg}\n", encoding="utf-8")
    except Exception:
        pass
    log.warning("Requesting controlled reboot: %s", msg)
    ok, rc, out, cmd = _run_first_success(
        [
            ["sudo", "-n", "systemctl", "reboot"],
            ["sudo", "-n", "reboot"],
            ["reboot"],
        ],
        timeout_s=15,
    )
    if ok:
        log.warning("Reboot command issued via: %s", " ".join(cmd))
        return True
    log.warning("Reboot request failed rc=%s out=%s", rc, _short_message(out, 300))
    return False


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


def _read_pi_pmic_rails(rails: tuple[str, ...] = ("3V3_SYS",)) -> dict[str, dict[str, float]]:
    """Best-effort read of named Pi 5 PMIC ADC rails via `vcgencmd pmic_read_adc`.

    Returns a dict like ``{"3V3_SYS": {"current_a": 0.087, "voltage_v": 3.302}}``
    for each rail in ``rails`` that vcgencmd reports. Rails not present in the
    output (or older hardware that lacks ``pmic_read_adc``) are simply omitted;
    callers should treat empty values as "not available" rather than zero.

    The expected output format from vcgencmd looks like::

        3V3_SYS_A current(1)=0.08685777A
        3V3_SYS_V volt(9)=3.30260900V
    """
    out: dict[str, dict[str, float]] = {}
    vcgencmd = resolve_executable("vcgencmd", ["/usr/bin/vcgencmd"])
    if not vcgencmd:
        return out
    rc, _, raw = _run_capture_args([vcgencmd, "pmic_read_adc"], 3000)
    if rc != 0 or not raw:
        return out
    wanted = {r.upper() for r in rails}
    for line in raw.splitlines():
        m = re.search(
            r"^\s*([A-Z0-9_]+?)_(A|V)\s+(?:current|volt)\(\d+\)=([\d.]+)\s*[AV]\s*$",
            line,
        )
        if not m:
            continue
        rail = m.group(1).upper()
        if rail not in wanted:
            continue
        kind = m.group(2)
        try:
            val = float(m.group(3))
        except ValueError:
            continue
        out.setdefault(rail, {})[("current_a" if kind == "A" else "voltage_v")] = val
    return out


def _collect_device_status_metrics() -> dict[str, str]:
    metrics: dict[str, str] = {}
    if not parse_bool(os.environ.get("ENABLE_DEVICE_STATUS_METRICS"), True):
        return metrics

    # Lazy import so a missing prometheus_client doesn't break this function.
    try:
        from . import prom_exposition as _prom
    except Exception:
        _prom = None  # type: ignore[assignment]

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
        temp_c = float(temp_mc) / 1000.0
        metrics["device_cpu_temp_c"] = f"{temp_c:.2f}"
        if _prom is not None and getattr(_prom, "cpu_temp_celsius", None) is not None:
            try:
                _prom.cpu_temp_celsius.set(temp_c)
            except Exception:
                pass
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
                if _prom is not None:
                    try:
                        if getattr(_prom, "power_throttled_state", None) is not None:
                            _prom.power_throttled_state.set(flags)
                        if getattr(_prom, "power_under_voltage", None) is not None:
                            _prom.power_under_voltage.set(1.0 if (flags & 0x1) else 0.0)
                    except Exception:
                        pass

        exit_code, _, out = _run_capture_args([vcgencmd, "measure_volts", "core"], 2000)
        if exit_code == 0:
            mm = re.search(r"volt=([\d.]+)", out)
            if mm:
                volts = float(mm.group(1))
                metrics["device_core_volts"] = f"{volts:.4f}"
                if _prom is not None and getattr(_prom, "power_voltage_volts", None) is not None:
                    try:
                        _prom.power_voltage_volts.set(volts)
                    except Exception:
                        pass

    # Pi 5 PMIC ADC: 3V3_SYS rail is the M.2 slot's feed on the official RPi
    # M.2 HAT+. With no other 3.3 V loads on this Pi (operator-confirmed) the
    # rail is dominated by AX210 + a small Pi baseline; subtract a once-measured
    # baseline in Grafana to approximate AX210 watts to ~+/- 10 %.
    pmic = _read_pi_pmic_rails(("3V3_SYS",))
    rail = pmic.get("3V3_SYS")
    if rail is not None:
        if "current_a" in rail:
            metrics["device_pmic_3v3_sys_current_a"] = f"{rail['current_a']:.4f}"
            if _prom is not None and getattr(_prom, "pmic_3v3_sys_current_amps", None) is not None:
                try:
                    _prom.pmic_3v3_sys_current_amps.set(rail["current_a"])
                except Exception:
                    pass
        if "voltage_v" in rail:
            metrics["device_pmic_3v3_sys_voltage_v"] = f"{rail['voltage_v']:.4f}"
            if _prom is not None and getattr(_prom, "pmic_3v3_sys_voltage_volts", None) is not None:
                try:
                    _prom.pmic_3v3_sys_voltage_volts.set(rail["voltage_v"])
                except Exception:
                    pass

    return metrics


def _collect_hardware_inventory() -> dict[str, Any]:
    """Best-effort enumeration of radios + I2C devices for run-summary.json.

    Each key is always present; the value is empty when the underlying tool
    is missing or returns nothing. This is observation only -- nothing in the
    runtime gates on these values.
    """
    inv: dict[str, Any] = {
        "kernel": "",
        "model": "",
        "wifi_interfaces": [],
        "bluetooth_adapters": [],
        "i2c_devices_bus1": [],
    }

    iw_bin = resolve_executable("iw", ["/usr/sbin/iw", "/sbin/iw"])
    if iw_bin:
        rc, _, out = _run_capture_args([iw_bin, "dev"], 3000)
        if rc == 0 and out:
            interfaces: list[dict[str, str]] = []
            current: dict[str, str] | None = None
            for line in out.splitlines():
                stripped = line.strip()
                if stripped.startswith("Interface "):
                    name = stripped[len("Interface "):].strip()
                    if name:
                        current = {"name": name}
                        interfaces.append(current)
                elif current is not None and stripped.startswith("type "):
                    current["type"] = stripped[len("type "):].strip()
                elif current is not None and stripped.startswith("addr "):
                    current["addr"] = stripped[len("addr "):].strip()
            inv["wifi_interfaces"] = interfaces

    btctl = resolve_executable("bluetoothctl", ["/usr/bin/bluetoothctl"])
    if btctl:
        rc, _, out = _run_capture_args([btctl, "list"], 3000)
        if rc == 0 and out:
            adapters: list[dict[str, str]] = []
            for line in out.splitlines():
                m = re.match(
                    r"^Controller\s+([0-9A-Fa-f:]{17})\s+(.+?)(?:\s+\[.*\])?\s*$",
                    line.strip(),
                )
                if m:
                    adapters.append({"address": m.group(1), "name": m.group(2)})
            inv["bluetooth_adapters"] = adapters

    i2cdetect = resolve_executable("i2cdetect", ["/usr/sbin/i2cdetect", "/sbin/i2cdetect"])
    if i2cdetect:
        rc, _, out = _run_capture_args([i2cdetect, "-y", "1"], 3000)
        if rc == 0 and out:
            addrs: list[str] = []
            for line in out.splitlines():
                m = re.match(r"^([0-9a-fA-F]{2}):\s*(.*)$", line)
                if not m:
                    continue
                base = int(m.group(1), 16)
                fields = m.group(2).strip().split()
                for i, field in enumerate(fields):
                    if re.match(r"^[0-9a-fA-F]{2}$", field):
                        addrs.append(f"0x{(base + i):02x}")
            inv["i2c_devices_bus1"] = addrs

    rc, _, out = _run_capture_args(["uname", "-a"], 1500)
    if rc == 0 and out:
        first = out.strip().splitlines()[:1]
        if first:
            inv["kernel"] = first[0]

    model = _read_text_file("/sys/firmware/devicetree/base/model")
    if model:
        inv["model"] = model.strip().rstrip("\x00")

    return inv


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
        self.failed_dir = root / "failed"
        self.execution_index_path = root / "execution-index.json"
        self.sent_retention_days = max(1, int(sent_retention_days))
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.sent_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)

    def _run_dir(self, run_id: str, *, sent: bool = False) -> Path:
        return (self.sent_dir if sent else self.pending_dir) / sanitize_name(run_id, "run")

    def _write_text_atomic(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        self._write_text_atomic(path, json.dumps(payload, sort_keys=True, indent=2))

    def _load_execution_index(self) -> dict[str, Any]:
        if not self.execution_index_path.exists():
            return {}
        try:
            payload = json.loads(self.execution_index_path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Failed to read execution index")
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_execution_index(self, payload: dict[str, Any]) -> None:
        self._write_json_atomic(self.execution_index_path, payload)

    def get_execution_record(self, execution_key: str) -> dict[str, Any]:
        key = normalize(execution_key)
        if not key:
            return {}
        payload = self._load_execution_index()
        row = payload.get(key)
        return row if isinstance(row, dict) else {}

    def mark_execution_state(
        self,
        execution_key: str,
        *,
        state: str,
        run_id: str = "",
        policy_id: str = "",
        experiment_id: str = "",
        measurement_from: str = "",
        measurement_to: str = "",
    ) -> dict[str, Any]:
        key = normalize(execution_key)
        if not key:
            return {}
        payload = self._load_execution_index()
        current = payload.get(key)
        entry = dict(current) if isinstance(current, dict) else {"execution_key": key}
        now_iso = now_utc().isoformat().replace("+00:00", "Z")
        entry["execution_key"] = key
        entry["state"] = normalize(state) or "unknown"
        entry["updated_at"] = now_iso
        if run_id:
            entry["run_id"] = normalize(run_id)
        if policy_id:
            entry["policy_id"] = normalize(policy_id)
        if experiment_id:
            entry["experiment_id"] = normalize(experiment_id)
        if measurement_from:
            entry["measurement_from"] = normalize(measurement_from)
        if measurement_to:
            entry["measurement_to"] = normalize(measurement_to)
        if state == "in_progress":
            entry["last_started_at"] = now_iso
            entry["started_runs"] = max(0, int(entry.get("started_runs", 0) or 0)) + 1
        elif state in {"completed", "reported"}:
            entry["last_completed_at"] = now_iso
            if state == "completed":
                entry["completed_runs"] = max(0, int(entry.get("completed_runs", 0) or 0)) + 1
        elif state == "failed":
            entry["last_failed_at"] = now_iso
            entry["failed_runs"] = max(0, int(entry.get("failed_runs", 0) or 0)) + 1
        payload[key] = entry
        self._write_execution_index(payload)
        return entry

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
            fh.flush()
            os.fsync(fh.fileno())

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
        stem = f"command-{safe_cmd}-execution-status"
        file_name = f"{stem}.log"
        path = artifacts_dir / file_name
        if path.exists():
            file_name = f"{stem}-{int(time.time() * 1000)}.log"
            path = artifacts_dir / file_name
        self._write_text_atomic(
            path,
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
        )
        return artifact_from_path(file_name, path, run_id)

    def write_summary_artifact(self, run_id: str, payload: dict[str, Any]) -> fleet_gateway_v2_pb2.ArtifactRef:
        artifacts_dir = self._run_dir(run_id) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = artifacts_dir / "run-summary.json"
        self._write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True))
        return artifact_from_path("run-summary.json", path, run_id)

    def write_text_artifact(
        self,
        run_id: str,
        prefix: str,
        content: str,
        *,
        suffix: str = ".txt",
        include_timestamp: bool = True,
    ) -> fleet_gateway_v2_pb2.ArtifactRef:
        artifacts_dir = self._run_dir(run_id) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        stem = sanitize_name(prefix, "artifact")
        if include_timestamp:
            file_name = f"{stem}-{int(time.time() * 1000)}{suffix}"
        else:
            file_name = f"{stem}{suffix}"
        path = artifacts_dir / file_name
        if path.exists():
            file_name = f"{stem}-{int(time.time() * 1000)}{suffix}"
            path = artifacts_dir / file_name
        self._write_text_atomic(path, content or "")
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
        context_path = run_dir / "context.json"
        current_context: dict[str, Any] = {}
        if context_path.exists():
            try:
                loaded = json.loads(context_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current_context = loaded
            except Exception:
                pass
        self._write_json_atomic(run_dir / "report.json", report_to_dict(report))
        self._write_json_atomic(
            context_path,
            {
                **current_context,
                "state": "measured",
                "updated_at": now_utc().isoformat().replace("+00:00", "Z"),
                "run_id": normalize(report.run_id),
                "policy_id": normalize(report.policy_id),
                "experiment_id": normalize(report.experiment_id),
            },
        )
        execution_key = normalize(current_context.get("execution_key"))
        if execution_key:
            self.mark_execution_state(
                execution_key,
                state="completed",
                run_id=normalize(report.run_id),
                policy_id=normalize(report.policy_id),
                experiment_id=normalize(report.experiment_id),
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
            raw = report_path.read_text(encoding="utf-8")
        except Exception as exc:
            log.exception("Failed to read stored report for run_id=%s", run_id)
            self.quarantine_pending_run(run_id, "report_read_error", f"{type(exc).__name__}: {exc}")
            return None
        if not raw.strip():
            log.error("Stored report is empty for run_id=%s; quarantining pending run", run_id)
            self.quarantine_pending_run(run_id, "report_empty")
            return None
        try:
            payload = json.loads(raw)
        except Exception as exc:
            log.exception("Failed to parse stored report for run_id=%s", run_id)
            self.quarantine_pending_run(run_id, "report_invalid_json", f"{type(exc).__name__}: {exc}")
            return None
        if not isinstance(payload, dict):
            log.error("Stored report payload is not an object for run_id=%s; quarantining", run_id)
            self.quarantine_pending_run(run_id, "report_invalid_shape")
            return None
        try:
            return report_from_dict(payload)
        except Exception as exc:
            log.exception("Failed to rebuild protobuf report for run_id=%s", run_id)
            self.quarantine_pending_run(run_id, "report_invalid_proto", f"{type(exc).__name__}: {exc}")
            return None

    def quarantine_pending_run(self, run_id: str, reason: str, detail: str = "") -> None:
        source = self._run_dir(run_id)
        if not source.exists():
            return
        target = self.failed_dir / sanitize_name(run_id, "run")
        if target.exists():
            target = self.failed_dir / f"{sanitize_name(run_id, 'run')}-{int(time.time() * 1000)}"
        try:
            source.replace(target)
        except Exception:
            log.exception("Failed to move pending run to failed run_id=%s", run_id)
            return

        context_path = target / "context.json"
        payload: dict[str, Any] = {
            "state": "failed",
            "updated_at": now_utc().isoformat().replace("+00:00", "Z"),
            "run_id": run_id,
            "failure_reason": normalize(reason),
        }
        if context_path.exists():
            try:
                current = json.loads(context_path.read_text(encoding="utf-8"))
                if isinstance(current, dict):
                    payload.update(current)
            except Exception:
                pass
        payload["state"] = "failed"
        payload["updated_at"] = now_utc().isoformat().replace("+00:00", "Z")
        payload["failure_reason"] = normalize(reason)
        self._write_json_atomic(context_path, payload)
        if detail:
            self._write_text_atomic(
                target / "failure-reason.txt",
                f"{now_utc().isoformat().replace('+00:00', 'Z')} {normalize(reason)}\n{detail}\n",
            )
        execution_key = normalize(payload.get("execution_key"))
        if execution_key:
            self.mark_execution_state(
                execution_key,
                state="failed",
                run_id=normalize(run_id),
                policy_id=normalize(payload.get("policy_id")),
                experiment_id=normalize(payload.get("experiment_id")),
            )
        log.warning("Quarantined pending run run_id=%s reason=%s", run_id, normalize(reason))

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
        execution_key = normalize(payload.get("execution_key"))
        if execution_key:
            self.mark_execution_state(
                execution_key,
                state="reported",
                run_id=normalize(run_id),
                policy_id=normalize(payload.get("policy_id")),
                experiment_id=normalize(payload.get("experiment_id")),
            )

    def prune_sent(self) -> None:
        cutoff = now_utc() - timedelta(days=self.sent_retention_days)
        for child in self.sent_dir.iterdir():
            if not child.is_dir():
                continue
            mtime = datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
