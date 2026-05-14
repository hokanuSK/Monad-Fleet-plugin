from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import urllib3

from .grafana_dashboards import (
    DashboardProvisioningConfig,
    DEFAULT_INSTANCE_MAP_TEXT,
    generate_experiment_dashboards_from_policy,
    parse_instance_map,
)


urllib3.disable_warnings()

def _repo_root_guess() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "infrastructure/observability/grafana/provisioning/dashboards/static").exists():
            return parent
    return Path.cwd()


REPO_ROOT = _repo_root_guess()
DEFAULT_GRAFANA_TEMPLATE_DIR = REPO_ROOT / "infrastructure/observability/grafana/provisioning/dashboards/static"
DEFAULT_GRAFANA_OUTPUT_ROOT = REPO_ROOT / "infrastructure/observability/grafana/provisioning/dashboards/experiments"


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _deep_copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _parse_int(value: Any, default: int) -> int:
    text = _normalize(value)
    if not text:
        return default
    try:
        return int(text)
    except Exception:
        return default


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


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


def _load_document() -> dict[str, Any]:
    payload_b64 = _normalize(os.environ.get("EXPERIMENT_JSON_B64"))
    if not payload_b64:
        raise RuntimeError("EXPERIMENT_JSON_B64 is required")
    raw = base64.b64decode(payload_b64.encode("utf-8"))
    loaded = json.loads(raw.decode("utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError("experiment JSON must decode to an object")
    return loaded


def _resolve_policy(doc: dict[str, Any]) -> dict[str, Any]:
    fleet = doc.get("fleet")
    if isinstance(fleet, dict) and isinstance(fleet.get("policy"), dict):
        return fleet["policy"]
    if isinstance(doc.get("policy"), dict):
        return doc["policy"]
    return doc


def _ensure_wrapper(doc: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    fleet = doc.get("fleet")
    if not isinstance(fleet, dict):
        fleet = {}
        doc["fleet"] = fleet
    fleet["policy"] = policy
    return doc


def _shift_window_dict(window: dict[str, Any], *, delta: timedelta, start_key: str = "from", end_key: str = "to") -> None:
    start_dt = _parse_iso(window.get(start_key))
    end_dt = _parse_iso(window.get(end_key))
    if start_dt is not None:
        window[start_key] = _to_iso(start_dt + delta)
    if end_dt is not None:
        window[end_key] = _to_iso(end_dt + delta)


def _window_has_times(window: Any) -> bool:
    if not isinstance(window, dict):
        return False
    return bool(
        _normalize(window.get("from"))
        or _normalize(window.get("to"))
        or _normalize(window.get("start"))
        or _normalize(window.get("end"))
    )


def _shift_flexible_window(window: dict[str, Any], *, delta: timedelta) -> None:
    start_key = "from" if "from" in window or "to" in window else "start"
    end_key = "to" if start_key == "from" else "end"
    _shift_window_dict(window, delta=delta, start_key=start_key, end_key=end_key)


def _iter_sync_commands(policy: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    groups = policy.get("command_groups")
    if not isinstance(groups, list):
        return out
    for group in groups:
        if not isinstance(group, dict):
            continue
        commands = _iter_group_commands(group)
        for command in commands:
            cmd_type = _normalize(command.get("type") or command.get("command_type")).upper()
            if cmd_type == "SYNC":
                out.append(command)
        group["commands"] = commands
    return out


def _sync_measure_window(command: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("measure_window", "measurement_window", "reporting"):
        window = command.get(key)
        if _window_has_times(window):
            return window
    return None


def _retime_policy(policy: dict[str, Any], *, now: datetime) -> None:
    start_offset_minutes = _parse_int(os.environ.get("EXPERIMENT_TIME_SHIFT_START_MINUTES"), -2)
    desired_start = now + timedelta(minutes=start_offset_minutes)

    range_obj = policy.get("range")
    if not isinstance(range_obj, dict):
        range_obj = None

    reporting = policy.get("reporting")
    if not isinstance(reporting, dict):
        reporting = None

    measure_window = None
    if isinstance(reporting, dict):
        candidate = reporting.get("measure_window")
        if isinstance(candidate, dict):
            measure_window = candidate
        elif isinstance(reporting.get("measurement_window"), dict):
            measure_window = reporting["measurement_window"]

    sync_commands = _iter_sync_commands(policy)
    sync_validity_windows = [
        cmd["validity_policy_window"]
        for cmd in sync_commands
        if _window_has_times(cmd.get("validity_policy_window"))
    ]
    sync_measure_windows = [
        window
        for cmd in sync_commands
        for window in [_sync_measure_window(cmd)]
        if window is not None
    ]

    reference_start = (
        (_parse_iso(range_obj.get("from")) if isinstance(range_obj, dict) else None)
        or (_parse_iso(measure_window.get("from") or measure_window.get("start")) if isinstance(measure_window, dict) else None)
        or (_parse_iso(sync_validity_windows[0].get("from") or sync_validity_windows[0].get("start")) if sync_validity_windows else None)
        or (_parse_iso(sync_measure_windows[0].get("from") or sync_measure_windows[0].get("start")) if sync_measure_windows else None)
    )

    if reference_start is None:
        reference_start = desired_start
    delta = desired_start - reference_start

    if isinstance(range_obj, dict):
        _shift_window_dict(range_obj, delta=delta)

    if isinstance(measure_window, dict):
        _shift_flexible_window(measure_window, delta=delta)
        if isinstance(reporting, dict) and reporting.get("measure_window") is measure_window:
            reporting["measure_window"] = measure_window
        elif isinstance(reporting, dict) and reporting.get("measurement_window") is measure_window:
            reporting["measurement_window"] = measure_window

    if isinstance(reporting, dict):
        upload_window = reporting.get("upload_window")
        if isinstance(upload_window, dict):
            _shift_flexible_window(upload_window, delta=delta)

    for command in sync_commands:
        for key in ("validity_policy_window", "measure_window", "measurement_window", "reporting", "upload_window"):
            window = command.get(key)
            if isinstance(window, dict):
                _shift_flexible_window(window, delta=delta)

    groups = policy.get("command_groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_range = group.get("range")
            if isinstance(group_range, dict):
                _shift_flexible_window(group_range, delta=delta)

    if range_obj is None and not sync_validity_windows:
        range_obj = {}
        policy["range"] = range_obj

    if isinstance(range_obj, dict) and not _normalize(range_obj.get("from")):
        range_obj["from"] = _to_iso(desired_start)
    if isinstance(range_obj, dict) and not _normalize(range_obj.get("to")):
        default_end = desired_start + timedelta(minutes=75)
        range_obj["to"] = _to_iso(default_end)

    if reporting is None and not sync_measure_windows:
        reporting = {}
        policy["reporting"] = reporting

    if (
        isinstance(reporting, dict)
        and not isinstance(reporting.get("measure_window"), dict)
        and not isinstance(reporting.get("measurement_window"), dict)
    ):
        start_iso = _normalize(range_obj.get("from")) if isinstance(range_obj, dict) else _to_iso(desired_start)
        reporting["measure_window"] = {
            "from": start_iso,
            "to": _to_iso(desired_start + timedelta(minutes=30)),
        }


def _apply_target_overrides(policy: dict[str, Any]) -> None:
    explicit_csv = _normalize(os.environ.get("TARGET_DEVICE_IDS_CSV"))
    device_ids = [row.strip().lower() for row in explicit_csv.split(",") if row.strip()]
    if not device_ids:
        device_pi = _normalize(os.environ.get("DEVICE_PI")).lower()
        run_pi = _normalize(os.environ.get("RUN_PI")).lower() in {"1", "true", "yes", "on"}
        if run_pi and device_pi:
            device_ids = [device_pi]
    if not device_ids:
        return

    selector = policy.get("target_selector")
    if not isinstance(selector, dict):
        selector = None
        for command in _iter_sync_commands(policy):
            candidate = command.get("target_selector")
            if isinstance(candidate, dict):
                selector = candidate
                break
    if selector is None:
        selector = {}
        sync_commands = _iter_sync_commands(policy)
        if sync_commands:
            sync_commands[0]["target_selector"] = selector
        else:
            policy["target_selector"] = selector
    selector["device_ids"] = device_ids


def _apply_command_env_overrides(policy: dict[str, Any]) -> None:
    common_rf_keys = ("RF_PROFILE", "RF_PHASE", "RF_ACTIVITY_LABEL", "RF_ROOM_ID", "RF_SCENARIO_ID", "RF_RUN_LABEL")
    csi_iface_override = _normalize(os.environ.get("CSI_IFACE") or os.environ.get("CSI_MEASURE_IFACE"))

    groups = policy.get("command_groups")
    if not isinstance(groups, list):
        return

    for group in groups:
        if not isinstance(group, dict):
            continue
        commands = _iter_group_commands(group)
        for command in commands:
            cmd_env = command.get("env")
            if not isinstance(cmd_env, dict):
                cmd_env = {}
                command["env"] = cmd_env

            for raw_key in list(cmd_env.keys()):
                key = _normalize(raw_key)
                if not key:
                    continue
                override = os.environ.get(key)
                if _normalize(override):
                    cmd_env[key] = _normalize(override)

            cmd_type = _normalize(command.get("type") or command.get("command_type")).upper()
            if cmd_type in {"WIFI_SCAN", "BLE_SCAN", "CAPTURE_CSI"}:
                for key in common_rf_keys:
                    if _normalize(os.environ.get(key)):
                        cmd_env[key] = _normalize(os.environ.get(key))
            if cmd_type == "CAPTURE_CSI" and csi_iface_override:
                cmd_env["CSI_IFACE"] = csi_iface_override

        group["commands"] = commands


def _patched_document() -> tuple[dict[str, Any], dict[str, Any], datetime]:
    now = datetime.now(timezone.utc)
    doc = _load_document()
    policy = _resolve_policy(doc)
    if not isinstance(policy, dict):
        raise RuntimeError("experiment JSON does not contain a policy object")
    policy = _deep_copy_json(policy)
    _retime_policy(policy, now=now)
    _apply_target_overrides(policy)
    _apply_command_env_overrides(policy)
    policy["experiment_id"] = _normalize(policy.get("experiment_id")) or f"design-json-{now.strftime('%Y%m%dT%H%M%SZ')}"
    if isinstance(doc.get("fleet"), dict) and isinstance(doc["fleet"].get("policy"), dict):
        doc = _ensure_wrapper(doc, policy)
    else:
        doc = {"fleet": {"policy": policy}}
    return doc, policy, now


def _create_experiment(metadata: dict[str, Any], *, now: datetime) -> int:
    base = _normalize(os.environ.get("ELAB_BASE_URL") or "https://web/api/v2").rstrip("/")
    key = _normalize(os.environ.get("ELAB_API_KEY"))
    if not key:
        raise RuntimeError("ELAB_API_KEY is required")

    title_prefix = _normalize(os.environ.get("SMOKE_TITLE_PREFIX") or "Fleet Design Run")
    title = f"{title_prefix} {now.strftime('%Y-%m-%d %H:%M:%SZ')}"
    tags_csv = _normalize(os.environ.get("SMOKE_TAGS_CSV") or "fleet,smoke:v3,created-by:monad-fleet,design-json")
    tags = [row.strip() for row in tags_csv.split(",") if row.strip()]
    if "fleet" not in tags:
        tags.insert(0, "fleet")

    body = (
        "Smoke experiment created from a diagram-driven experiment JSON file. "
        "Safe to delete after verification."
    )

    create_resp = requests.post(
        f"{base}/experiments",
        headers={"Authorization": key},
        json={
            "title": title,
            "tags": tags,
            "body": body,
            "metadata": "{}",
        },
        verify=False,
        timeout=20,
    )
    create_resp.raise_for_status()

    location = _normalize(create_resp.headers.get("location"))
    match = re.search(r"/experiments/(\d+)$", location)
    if not match:
        raise RuntimeError(f"Could not parse experiment id from Location header: {location}")
    experiment_id = int(match.group(1))

    patch_resp = requests.patch(
        f"{base}/experiments/{experiment_id}",
        headers={"Authorization": key},
        json={"metadata": json.dumps(metadata, sort_keys=True, separators=(",", ":"))},
        verify=False,
        timeout=20,
    )
    patch_resp.raise_for_status()
    return experiment_id


def _maybe_provision_grafana_dashboards(experiment_id: int, policy: dict[str, Any]) -> None:
    enabled = _parse_bool_env(
        "PROVISION_GRAFANA_EXPERIMENT_DASHBOARDS",
        _parse_bool_env("GRAFANA_EXPERIMENT_DASHBOARDS_ENABLED", False),
    )
    if not enabled:
        return

    template_dir = Path(os.environ.get("GRAFANA_DASHBOARD_TEMPLATE_DIR") or DEFAULT_GRAFANA_TEMPLATE_DIR)
    output_root = Path(os.environ.get("GRAFANA_DASHBOARD_OUTPUT_ROOT") or DEFAULT_GRAFANA_OUTPUT_ROOT)
    config = DashboardProvisioningConfig(
        template_dir=template_dir,
        output_root=output_root,
        instance_map=parse_instance_map(os.environ.get("GRAFANA_EXPERIMENT_INSTANCE_MAP", DEFAULT_INSTANCE_MAP_TEXT)),
        default_instance_regex=_normalize(os.environ.get("GRAFANA_EXPERIMENT_DEFAULT_INSTANCE_REGEX")),
        folder_name=_normalize(os.environ.get("GRAFANA_EXPERIMENT_FOLDER_NAME")),
        device_regex=_normalize(os.environ.get("GRAFANA_EXPERIMENT_DEVICE_REGEX")),
        instance_regex=_normalize(os.environ.get("GRAFANA_EXPERIMENT_INSTANCE_REGEX")),
    )
    written = generate_experiment_dashboards_from_policy(
        experiment_id=experiment_id,
        policy=policy,
        config=config,
    )
    print(f"Provisioned Grafana dashboards for experiment {experiment_id}: {len(written)} files")


def main() -> None:
    metadata_doc, policy, now = _patched_document()
    experiment_id = _create_experiment(metadata_doc, now=now)
    _maybe_provision_grafana_dashboards(experiment_id, policy)
    print(experiment_id)


if __name__ == "__main__":
    main()
