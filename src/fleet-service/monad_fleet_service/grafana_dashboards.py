from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DashboardTemplate:
    filename: str
    slug: str
    title: str
    uses_pi_metrics: bool = False
    uses_fleet_device_metrics: bool = False


@dataclass
class DashboardProvisioningConfig:
    template_dir: Path
    output_root: Path
    instance_map: dict[str, str] = field(default_factory=dict)
    default_instance_regex: str = ""
    folder_name: str = ""
    device_regex: str = ""
    instance_regex: str = ""


TEMPLATES: tuple[DashboardTemplate, ...] = (
    DashboardTemplate("monad-pi-power-telemetry.json", "power", "Power", uses_pi_metrics=True),
    DashboardTemplate("monad-pi-ble-telemetry.json", "ble", "BLE", uses_pi_metrics=True),
    DashboardTemplate("monad-pi-wifi5g-telemetry.json", "wifi", "Wi-Fi", uses_pi_metrics=True),
    DashboardTemplate("monad-pi-csi-telemetry.json", "csi", "CSI", uses_fleet_device_metrics=True),
)

DEFAULT_INSTANCE_MAP: dict[str, str] = {
    "2c:cf:67:81:00:45": "monad-01",
    "24:eb:16:e3:6a:07": "monad-02",
    "2c:cf:67:80:f5:86": "monad-03",
    "2c:cf:67:80:f4:bf": "monad-04",
}
DEFAULT_INSTANCE_MAP_TEXT = ",".join(f"{key}={value}" for key, value in DEFAULT_INSTANCE_MAP.items())


def normalize_token(value: Any) -> str:
    return str(value or "").strip()


def normalize_device_id(value: Any) -> str:
    return normalize_token(value).lower()


def parse_iso_timestamp(value: Any) -> datetime | None:
    text = normalize_token(value)
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        rows = [normalize_device_id(row) for row in value]
    else:
        rows = [normalize_device_id(row) for row in re.split(r"[\s,]+", normalize_token(value))]

    seen: set[str] = set()
    out: list[str] = []
    for token in rows:
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def safe_slug(value: Any, default: str = "experiment") -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", normalize_token(value)).strip("-_").lower()
    return slug or default


def experiment_token(raw: Any) -> str:
    text = normalize_token(raw)
    match = re.search(r"(\d+)$", text)
    return match.group(1) if match else safe_slug(text, "current")


def uid_for(exp_token: str, slug: str) -> str:
    base = f"exp-{safe_slug(exp_token, 'current')}-{slug}"
    if len(base) <= 40:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    return f"exp-{digest}-{slug}"[:40].rstrip("-")


def regex_for(values: list[str], fallback: str = ".*") -> str:
    if not values:
        return fallback
    return "|".join(re.escape(value).replace(r"\-", "-") for value in values)


def parse_instance_map(raw: Any) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for row in re.split(r"[\n,;]+", normalize_token(raw)):
        if not row.strip():
            continue
        if "=" in row:
            key, value = row.split("=", 1)
        elif ":" in row and not re.fullmatch(r"[0-9a-fA-F:]{17}", row.strip()):
            key, value = row.split(":", 1)
        else:
            continue
        device_id = normalize_device_id(key)
        instance = normalize_token(value)
        if device_id and instance:
            mapping[device_id] = instance
    return mapping


def walk_dicts(node: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(node, dict):
        rows.append(node)
        for value in node.values():
            rows.extend(walk_dicts(value))
    elif isinstance(node, list):
        for item in node:
            rows.extend(walk_dicts(item))
    return rows


def walk_targets(node: Any) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if isinstance(node.get("targets"), list):
            targets.extend(row for row in node["targets"] if isinstance(row, dict))
        for value in node.values():
            targets.extend(walk_targets(value))
    elif isinstance(node, list):
        for item in node:
            targets.extend(walk_targets(item))
    return targets


def device_ids_from_policy(policy: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def add(value: Any) -> None:
        for device_id in normalize_list(value):
            if device_id in seen:
                continue
            seen.add(device_id)
            out.append(device_id)

    for node in walk_dicts(policy):
        selector = node.get("target_selector")
        if isinstance(selector, dict):
            add(selector.get("device_ids"))
        add(node.get("device_ids"))
        add(node.get("agent_ids"))

    for target in walk_targets(policy):
        for key in ("id", "device_id", "agent_id", "mac", "mac_address", "wifi_mac"):
            add(target.get(key))

    return out


def policy_time_range(policy: dict[str, Any]) -> tuple[str, str] | None:
    measure_starts: list[datetime] = []
    measure_ends: list[datetime] = []
    fallback_starts: list[datetime] = []
    fallback_ends: list[datetime] = []

    def collect_window(window: Any, *, preferred: bool) -> None:
        if not isinstance(window, dict):
            return
        start_dt = parse_iso_timestamp(window.get("from") or window.get("start"))
        end_dt = parse_iso_timestamp(window.get("to") or window.get("end"))
        if start_dt is not None:
            (measure_starts if preferred else fallback_starts).append(start_dt)
        if end_dt is not None:
            (measure_ends if preferred else fallback_ends).append(end_dt)

    reporting = policy.get("reporting")
    if isinstance(reporting, dict):
        collect_window(reporting.get("measure_window"), preferred=True)
        collect_window(reporting.get("measurement_window"), preferred=True)

    for node in walk_dicts(policy):
        collect_window(node.get("measure_window"), preferred=True)
        collect_window(node.get("measurement_window"), preferred=True)

    if measure_starts or measure_ends:
        starts = measure_starts
        ends = measure_ends
    else:
        collect_window(policy.get("range"), preferred=False)
        for node in walk_dicts(policy):
            collect_window(node.get("validity_policy_window"), preferred=False)
            collect_window(node.get("upload_window"), preferred=False)
        starts = fallback_starts
        ends = fallback_ends

    if not starts and not ends:
        return None

    start_dt = min(starts) if starts else min(ends)
    end_dt = max(ends) if ends else max(starts)
    if start_dt is None or end_dt is None:
        return None
    if end_dt < start_dt:
        end_dt = start_dt
    return (iso_utc(start_dt), iso_utc(end_dt))


def instance_regex_for_devices(
    device_ids: list[str],
    *,
    instance_map: dict[str, str],
    fallback: str = "",
) -> str:
    instances = instances_for_devices(device_ids, instance_map=instance_map)
    if instances:
        return regex_for(instances)
    return fallback or regex_for(device_ids)


def instances_for_devices(
    device_ids: list[str],
    *,
    instance_map: dict[str, str],
) -> list[str]:
    instances: list[str] = []
    seen: set[str] = set()
    for device_id in device_ids:
        instance = normalize_token(instance_map.get(normalize_device_id(device_id)))
        if not instance or instance in seen:
            continue
        seen.add(instance)
        instances.append(instance)
    return sorted(instances)


def find_variable(dashboard: dict[str, Any], name: str) -> dict[str, Any] | None:
    templating = dashboard.setdefault("templating", {})
    variables = templating.setdefault("list", [])
    if not isinstance(variables, list):
        templating["list"] = []
        variables = templating["list"]
    for variable in variables:
        if isinstance(variable, dict) and variable.get("name") == name:
            return variable
    return None


def ensure_custom_device_variable(dashboard: dict[str, Any], device_ids: list[str], device_regex: str) -> None:
    templating = dashboard.setdefault("templating", {})
    variables = templating.setdefault("list", [])
    if not isinstance(variables, list):
        variables = []
        templating["list"] = variables

    variable = find_variable(dashboard, "device_id")
    if variable is None:
        variable = {
            "name": "device_id",
            "label": "Experiment device",
            "type": "custom",
            "hide": 0,
            "includeAll": True,
            "multi": True,
            "skipUrlSync": False,
        }
        variables.insert(0, variable)

    variable.update(
        {
            "type": "custom",
            "label": "Experiment device",
            "query": ",".join(device_ids) if device_ids else ".*",
            "includeAll": True,
            "multi": True,
            "allValue": device_regex,
            "current": {"selected": True, "text": "All", "value": "$__all"},
            "options": [],
        }
    )


def scope_fleet_device_variable(dashboard: dict[str, Any], device_ids: list[str], device_regex: str) -> None:
    ensure_custom_device_variable(dashboard, device_ids, device_regex)
    variable = find_variable(dashboard, "device_id")
    if variable is None:
        return
    query = f'label_values(monad_fleet_device_last_seen_unix{{device_id=~"{device_regex}"}}, device_id)'
    variable.update(
        {
            "type": "query",
            "definition": query,
            "query": {
                "query": query,
                "refId": "PrometheusVariableQueryEditor-VariableQuery",
            },
            "refresh": 2,
            "regex": "",
            "sort": 1,
        }
    )


def scope_instance_variable(dashboard: dict[str, Any], instance_regex: str) -> None:
    variable = find_variable(dashboard, "instance")
    if variable is None:
        return
    variable.update(
        {
            "includeAll": True,
            "multi": True,
            "allValue": instance_regex,
            "regex": f"/({instance_regex})/" if instance_regex != ".*" else "",
            "current": {"selected": True, "text": "All", "value": "$__all"},
        }
    )


def ensure_custom_instance_variable(dashboard: dict[str, Any], instances: list[str], instance_regex: str) -> None:
    variable = find_variable(dashboard, "instance")
    if variable is None:
        return
    variable.update(
        {
            "type": "custom",
            "label": normalize_token(variable.get("label")) or "Instance",
            "query": ",".join(instances) if instances else ".*",
            "definition": ",".join(instances) if instances else ".*",
            "includeAll": True,
            "multi": True,
            "allValue": instance_regex,
            "current": {"selected": True, "text": "All", "value": "$__all"},
            "options": [],
            "refresh": 0,
            "regex": "",
            "sort": 1,
        }
    )


def rewrite_pi_expr(expr: str) -> str:
    placeholders: list[tuple[str, str]] = []

    def stash(value: str) -> str:
        token = f"__MONAD_PROMQL_{len(placeholders)}__"
        placeholders.append((token, value))
        return token

    def rewrite_range_function(match: re.Match[str]) -> str:
        func, metric, window = match.group(1), match.group(2), match.group(3)
        return stash(
            f'({func}({metric}{{agent_id=~"$device_id"}}[{window}]) '
            f'or {func}({metric}{{instance=~"$instance"}}[{window}]))'
        )

    expr = re.sub(
        r"\b(rate|increase)\((monad_pi_[a-zA-Z0-9_:]+)\{instance=~\"\$instance\"\}\[([^\]]+)\]\)",
        rewrite_range_function,
        expr,
    )
    expr = re.sub(
        r"\b(monad_pi_[a-zA-Z0-9_:]+)\{instance=~\"\$instance\"\}",
        r'(\1{agent_id=~"$device_id"} or \1{instance=~"$instance"})',
        expr,
    )
    for token, value in placeholders:
        expr = expr.replace(token, value)
    return expr


def rewrite_pi_targets(dashboard: dict[str, Any]) -> None:
    for target in walk_targets(dashboard):
        expr = target.get("expr")
        if isinstance(expr, str) and "monad_pi_" in expr:
            target["expr"] = rewrite_pi_expr(expr)


def stamp_dashboard(
    dashboard: dict[str, Any],
    *,
    template: DashboardTemplate,
    experiment_id: str,
    exp_token: str,
    device_ids: list[str],
    device_regex: str,
    instances: list[str],
    instance_regex: str,
    time_range: tuple[str, str] | None,
) -> dict[str, Any]:
    dashboard["uid"] = uid_for(exp_token, template.slug)
    dashboard["title"] = f"Experiment {exp_token} - {template.title}"
    dashboard["version"] = 1
    dashboard["refresh"] = dashboard.get("refresh") or "10s"
    dashboard["time"] = (
        {"from": time_range[0], "to": time_range[1]}
        if time_range is not None
        else {"from": "now-2h", "to": "now"}
    )

    device_text = ", ".join(device_ids) if device_ids else "all devices"
    prefix = f"Experiment {experiment_id}; folder experiment-{exp_token}; devices: {device_text}."
    existing_description = normalize_token(dashboard.get("description"))
    dashboard["description"] = f"{prefix} {existing_description}".strip()

    tags = list(dashboard.get("tags") or [])
    for tag in ("monad", "experiment", f"experiment:{exp_token}"):
        if tag not in tags:
            tags.append(tag)
    dashboard["tags"] = tags

    if template.uses_fleet_device_metrics:
        scope_fleet_device_variable(dashboard, device_ids, device_regex)
    if template.uses_pi_metrics:
        ensure_custom_device_variable(dashboard, device_ids, device_regex)
        if instances:
            ensure_custom_instance_variable(dashboard, instances, instance_regex)
        else:
            scope_instance_variable(dashboard, instance_regex)
        rewrite_pi_targets(dashboard)

    return dashboard


def generate_experiment_dashboards(
    *,
    experiment_id: Any,
    device_ids: list[str] | None,
    config: DashboardProvisioningConfig,
    time_range: tuple[str, str] | None = None,
) -> list[Path]:
    exp_token = experiment_token(experiment_id)
    folder_name = config.folder_name or f"experiment-{exp_token}"
    normalized_devices = normalize_list(device_ids or [])
    device_regex = config.device_regex or regex_for(normalized_devices)
    instances = instances_for_devices(
        normalized_devices,
        instance_map=config.instance_map,
    )
    instance_regex = config.instance_regex or instance_regex_for_devices(
        normalized_devices,
        instance_map=config.instance_map,
        fallback=config.default_instance_regex,
    )

    output_dir = config.output_root / safe_slug(folder_name, f"experiment-{exp_token}")
    output_dir.mkdir(parents=True, exist_ok=True)
    for old in output_dir.glob("*.json"):
        old.unlink(missing_ok=True)

    written: list[Path] = []
    for template in TEMPLATES:
        src = config.template_dir / template.filename
        if not src.exists():
            raise FileNotFoundError(f"missing Grafana dashboard template: {src}")
        dashboard = json.loads(src.read_text(encoding="utf-8"))
        stamped = stamp_dashboard(
            dashboard,
            template=template,
            experiment_id=normalize_token(experiment_id),
            exp_token=exp_token,
            device_ids=normalized_devices,
            device_regex=device_regex,
            instances=instances,
            instance_regex=instance_regex,
            time_range=time_range,
        )
        dst = output_dir / template.filename
        dst.write_text(json.dumps(stamped, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        written.append(dst)
    return written


def generate_experiment_dashboards_from_policy(
    *,
    experiment_id: Any,
    policy: dict[str, Any],
    config: DashboardProvisioningConfig,
) -> list[Path]:
    return generate_experiment_dashboards(
        experiment_id=experiment_id,
        device_ids=device_ids_from_policy(policy),
        config=config,
        time_range=policy_time_range(policy),
    )
