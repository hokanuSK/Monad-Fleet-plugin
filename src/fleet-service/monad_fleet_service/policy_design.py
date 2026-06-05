from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)


RUNTIME_COMMAND_TYPES = {"SHELL", "CAPTURE_CSI", "BLE_SCAN", "WIFI_SCAN"}
DESIGN_COMMAND_TYPES = {"SYNC", "OBSERVE"}
DEFAULT_5GHZ_CHANNELS: tuple[int, ...] = (
    36, 40, 44, 48,
    52, 56, 60, 64,
    100, 104, 108, 112,
    116, 120, 124, 128,
    132, 136, 140,
    149, 153, 157, 161, 165,
)
ALLOWED_5GHZ_CHANNELS = set(DEFAULT_5GHZ_CHANNELS)


def _deep_copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_env(env_map: Any) -> dict[str, str]:
    if not isinstance(env_map, dict):
        return {}
    out: dict[str, str] = {}
    for raw_key, raw_value in env_map.items():
        key = _normalize(raw_key)
        if not key:
            continue
        out[key] = _normalize(raw_value)
    return out


def _parse_bool(value: Any) -> bool | None:
    text = _normalize(value).lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _parse_int(value: Any) -> int | None:
    text = _normalize(value)
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None


def _parse_iso(value: Any) -> datetime | None:
    text = _normalize(value)
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_reporting(policy: dict[str, Any]) -> dict[str, Any]:
    reporting = policy.get("reporting")
    if not isinstance(reporting, dict):
        reporting = {}
        policy["reporting"] = reporting
    return reporting


def _ensure_execution(policy: dict[str, Any]) -> dict[str, Any]:
    execution = policy.get("execution")
    if not isinstance(execution, dict):
        execution = {}
        policy["execution"] = execution
    return execution


def _window_has_times(window: Any) -> bool:
    if not isinstance(window, dict):
        return False
    return bool(
        _normalize(window.get("from"))
        or _normalize(window.get("to"))
        or _normalize(window.get("start"))
        or _normalize(window.get("end"))
    )


def _copy_window(window: dict[str, Any]) -> dict[str, str]:
    start = _normalize(window.get("from") or window.get("start"))
    end = _normalize(window.get("to") or window.get("end"))
    out: dict[str, str] = {}
    if start:
        out["from"] = start
    if end:
        out["to"] = end
    return out


def _merge_selector_values(current: Any, incoming: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for source in (current, incoming):
        if not isinstance(source, list):
            continue
        for raw_value in source:
            value = _normalize(raw_value)
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
    return out


def _merge_target_selector(policy: dict[str, Any], selector: dict[str, Any]) -> None:
    existing = policy.get("target_selector")
    merged = _deep_copy_json(existing) if isinstance(existing, dict) else {}
    for key in ("device_ids", "device_types", "locations", "agent_ids"):
        values = _merge_selector_values(merged.get(key), selector.get(key))
        if values:
            merged[key] = values
    policy["target_selector"] = merged


def _apply_sync_windows(policy: dict[str, Any], sync_cmd: dict[str, Any]) -> None:
    selector = sync_cmd.get("target_selector")
    if isinstance(selector, dict):
        _merge_target_selector(policy, selector)

    validity_window = sync_cmd.get("validity_policy_window")
    if _window_has_times(validity_window):
        policy["range"] = _copy_window(validity_window)

    measure_window = sync_cmd.get("measure_window")
    if not _window_has_times(measure_window):
        measure_window = sync_cmd.get("measurement_window")
    if not _window_has_times(measure_window):
        measure_window = sync_cmd.get("reporting")
    if _window_has_times(measure_window):
        reporting = _ensure_reporting(policy)
        reporting["measure_window"] = _copy_window(measure_window)

    upload_window = sync_cmd.get("upload_window")
    if _window_has_times(upload_window):
        reporting = _ensure_reporting(policy)
        reporting["upload_window"] = _copy_window(upload_window)


def _reporting_measure_window(policy: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    reporting = policy.get("reporting")
    if isinstance(reporting, dict):
        measure_window = reporting.get("measure_window")
        if not isinstance(measure_window, dict):
            measure_window = reporting.get("measurement_window") if isinstance(reporting.get("measurement_window"), dict) else {}
        start_dt = _parse_iso(measure_window.get("from") or measure_window.get("start"))
        end_dt = _parse_iso(measure_window.get("to") or measure_window.get("end"))
        if start_dt or end_dt:
            return start_dt, end_dt

    range_obj = policy.get("range")
    if isinstance(range_obj, dict):
        return _parse_iso(range_obj.get("from")), _parse_iso(range_obj.get("to"))
    return None, None


def _apply_sync_reporting(policy: dict[str, Any], sync_env: dict[str, str]) -> dict[str, str]:
    derived_env: dict[str, str] = {}

    reporting = _ensure_reporting(policy)
    measure_from_dt, measure_to_dt = _reporting_measure_window(policy)

    range_obj = policy.get("range") if isinstance(policy.get("range"), dict) else {}
    range_to_dt = _parse_iso(range_obj.get("to"))

    upload_window = reporting.get("upload_window")
    if not isinstance(upload_window, dict):
        upload_window = {}

    upload_from_dt = _parse_iso(upload_window.get("from") or upload_window.get("start"))
    upload_to_dt = _parse_iso(upload_window.get("to") or upload_window.get("end"))

    if upload_from_dt is None:
        upload_from_dt = measure_to_dt or measure_from_dt

    if upload_to_dt is None:
        duration_minutes = _parse_int(sync_env.get("SYNC_WINDOW_DURATION_MINUTES"))
        if upload_from_dt is not None and duration_minutes and duration_minutes > 0:
            upload_to_dt = upload_from_dt + timedelta(minutes=duration_minutes)
        elif range_to_dt is not None:
            upload_to_dt = range_to_dt
        else:
            upload_to_dt = upload_from_dt

    if range_to_dt is not None and upload_to_dt is not None and upload_to_dt > range_to_dt:
        upload_to_dt = range_to_dt
    if upload_from_dt is not None and upload_to_dt is not None and upload_to_dt < upload_from_dt:
        upload_to_dt = upload_from_dt

    upload_from_iso = _to_iso(upload_from_dt)
    upload_to_iso = _to_iso(upload_to_dt)
    if upload_from_iso or upload_to_iso:
        reporting["upload_window"] = {
            "from": upload_from_iso or upload_to_iso,
            "to": upload_to_iso or upload_from_iso,
        }
    if upload_from_iso:
        derived_env["UPLOAD_WINDOW_FROM"] = upload_from_iso
    if upload_to_iso:
        derived_env["UPLOAD_WINDOW_TO"] = upload_to_iso

    require_upload_window = _parse_bool(sync_env.get("SYNC_REQUIRE_UPLOAD_WINDOW"))
    if require_upload_window is not None:
        reporting["require_upload_window"] = require_upload_window
        derived_env["UPLOAD_WINDOW_REQUIRED"] = "true" if require_upload_window else "false"

    slot_count = _parse_int(sync_env.get("SYNC_SLOT_COUNT"))
    slot_jitter_s = _parse_int(sync_env.get("SYNC_SLOT_JITTER_S"))
    if slot_count is not None or slot_jitter_s is not None:
        slotting = reporting.get("slotting")
        if not isinstance(slotting, dict):
            slotting = {}
        if slot_count is not None and slot_count > 0:
            slotting["slot_count"] = max(1, slot_count)
            derived_env["UPLOAD_SLOT_COUNT"] = str(max(1, slot_count))
        if slot_jitter_s is not None and slot_jitter_s >= 0:
            slotting["jitter_s"] = max(0, slot_jitter_s)
            derived_env["UPLOAD_SLOT_JITTER_S"] = str(max(0, slot_jitter_s))
        if slotting:
            reporting["slotting"] = slotting

    execution = _ensure_execution(policy)
    execution_mode = _normalize(execution.get("mode")).lower()
    if not execution_mode:
        execution_mode = _normalize(sync_env.get("SYNC_EXECUTION_MODE") or sync_env.get("EXECUTION_MODE")).lower()
    if not execution_mode:
        execution_mode = "once"
    if execution_mode not in {"once", "recurring"}:
        execution_mode = "once"
    execution["mode"] = execution_mode
    derived_env["EXECUTION_MODE"] = execution_mode

    interval_s = _parse_int(execution.get("interval_s"))
    if interval_s is None:
        interval_s = _parse_int(sync_env.get("SYNC_EXECUTION_INTERVAL_S") or sync_env.get("EXECUTION_INTERVAL_S"))
    if execution_mode == "recurring" and interval_s is not None and interval_s > 0:
        execution["interval_s"] = max(1, interval_s)
        derived_env["EXECUTION_INTERVAL_S"] = str(max(1, interval_s))

    max_runs = _parse_int(execution.get("max_runs"))
    if max_runs is None:
        max_runs = _parse_int(sync_env.get("SYNC_EXECUTION_MAX_RUNS") or sync_env.get("EXECUTION_MAX_RUNS"))
    if max_runs is not None and max_runs >= 0:
        execution["max_runs"] = max(0, max_runs)
        derived_env["EXECUTION_MAX_RUNS"] = str(max(0, max_runs))

    return derived_env


def _iter_group_commands(group: dict[str, Any]) -> list[dict[str, Any]]:
    commands = group.get("commands")
    if isinstance(commands, list):
        return [row for row in commands if isinstance(row, dict)]
    if isinstance(commands, dict):
        rows: list[dict[str, Any]] = []
        for command_id, payload in commands.items():
            if not isinstance(payload, dict):
                continue
            row = _deep_copy_json(payload)
            row.setdefault("id", _normalize(command_id))
            rows.append(row)
        rows.sort(key=lambda row: int(row.get("order", 999999)))
        return rows
    return []


def _parse_channel_csv(value: Any) -> list[int]:
    text = _normalize(value)
    if not text:
        return []
    out: list[int] = []
    for part in text.split(","):
        token = _normalize(part)
        if not token:
            continue
        try:
            out.append(int(token))
        except Exception:
            continue
    return out


def _enforce_wifi_measurement_policy(policy: dict[str, Any]) -> None:
    groups = policy.get("command_groups")
    if not isinstance(groups, list):
        return

    default_channels_csv = ",".join(str(ch) for ch in DEFAULT_5GHZ_CHANNELS)
    for group in groups:
        if not isinstance(group, dict):
            continue
        commands = group.get("commands")
        if not isinstance(commands, list):
            continue
        for cmd in commands:
            if not isinstance(cmd, dict):
                continue
            cmd_type = _normalize(cmd.get("type") or cmd.get("command_type")).upper()
            if cmd_type != "WIFI_SCAN":
                continue
            env = cmd.get("env")
            if not isinstance(env, dict):
                env = {}
                cmd["env"] = env
            scan_mode = _normalize(env.get("WIFI_SCAN_MODE")).lower()
            if scan_mode != "passive_monitor":
                continue

            if not _normalize(env.get("WIFI_SCAN_CHANNEL_DWELL_S")):
                env["WIFI_SCAN_CHANNEL_DWELL_S"] = "0.5"
            if not _normalize(env.get("WIFI_SCAN_USE_MEASURE_WINDOW")):
                env["WIFI_SCAN_USE_MEASURE_WINDOW"] = "true"

            channels = _parse_channel_csv(env.get("WIFI_SCAN_CHANNELS"))
            if not channels:
                env["WIFI_SCAN_CHANNELS"] = default_channels_csv
                channels = list(DEFAULT_5GHZ_CHANNELS)

            invalid = [str(ch) for ch in channels if ch not in ALLOWED_5GHZ_CHANNELS]
            if invalid:
                cmd_id = _normalize(cmd.get("id") or cmd.get("name")) or "wifi-scan"
                log.warning(
                    "%s: passive_monitor mode has non-5GHz channels (%s); overriding to default 5GHz channel list",
                    cmd_id, ",".join(invalid),
                )
                env["WIFI_SCAN_CHANNELS"] = default_channels_csv


def _enforce_ble_measurement_policy(policy: dict[str, Any]) -> None:
    groups = policy.get("command_groups")
    if not isinstance(groups, list):
        return
    for group in groups:
        if not isinstance(group, dict):
            continue
        commands = group.get("commands")
        if not isinstance(commands, list):
            continue
        for cmd in commands:
            if not isinstance(cmd, dict):
                continue
            cmd_type = _normalize(cmd.get("type") or cmd.get("command_type")).upper()
            if cmd_type != "BLE_SCAN":
                continue
            env = cmd.get("env")
            if not isinstance(env, dict):
                env = {}
                cmd["env"] = env
            if not _normalize(env.get("BLE_SCAN_USE_MEASURE_WINDOW")):
                env["BLE_SCAN_USE_MEASURE_WINDOW"] = "true"


def _promote_shell_env_cmdline(cmd: dict[str, Any], cmd_env: dict[str, str]) -> dict[str, str]:
    if _normalize(cmd.get("cmdline") or cmd.get("cmd")):
        return cmd_env

    env_cmdline_key = ""
    for key in ("cmdline", "CMDLINE"):
        if _normalize(cmd_env.get(key)):
            env_cmdline_key = key
            break
    if not env_cmdline_key:
        return cmd_env

    cmd["cmdline"] = cmd_env[env_cmdline_key]
    cleaned_env = dict(cmd_env)
    cleaned_env.pop(env_cmdline_key, None)
    return cleaned_env


def policy_uses_design_commands(policy: dict[str, Any]) -> bool:
    if not isinstance(policy, dict):
        return False
    for group in policy.get("command_groups", []):
        if not isinstance(group, dict):
            continue
        for cmd in _iter_group_commands(group):
            cmd_type = _normalize(cmd.get("type") or cmd.get("command_type")).upper()
            if cmd_type in DESIGN_COMMAND_TYPES:
                return True
    return False


def runtime_policy_from_design(policy: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(policy, dict):
        return {}

    result = _deep_copy_json(policy)
    _enforce_wifi_measurement_policy(result)
    _enforce_ble_measurement_policy(result)
    if not policy_uses_design_commands(result):
        return result

    transformed_groups: list[dict[str, Any]] = []
    for raw_group in result.get("command_groups", []):
        if not isinstance(raw_group, dict):
            continue
        group = _deep_copy_json(raw_group)
        inherited_env: dict[str, str] = {}
        transformed_commands: list[dict[str, Any]] = []

        for raw_cmd in _iter_group_commands(raw_group):
            cmd = _deep_copy_json(raw_cmd)
            cmd_type = _normalize(cmd.get("type") or cmd.get("command_type") or "SHELL").upper()
            cmd_env = _normalize_env(cmd.get("env"))

            if cmd_type == "SYNC":
                _apply_sync_windows(result, cmd)
                inherited_env.update(cmd_env)
                inherited_env.update(_apply_sync_reporting(result, cmd_env))
                continue

            if cmd_type == "OBSERVE":
                inherited_env.update(cmd_env)
                continue

            if cmd_type not in RUNTIME_COMMAND_TYPES:
                continue

            if cmd_type == "SHELL":
                cmd_env = _promote_shell_env_cmdline(cmd, cmd_env)

            merged_env = dict(inherited_env)
            merged_env.update(cmd_env)
            if merged_env:
                cmd["env"] = merged_env
            elif "env" in cmd:
                cmd.pop("env", None)
            transformed_commands.append(cmd)

        if transformed_commands:
            group["commands"] = transformed_commands
            transformed_groups.append(group)

    result["command_groups"] = transformed_groups
    _enforce_wifi_measurement_policy(result)
    _enforce_ble_measurement_policy(result)
    return result
