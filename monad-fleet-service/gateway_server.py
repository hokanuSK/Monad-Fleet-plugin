import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent import futures
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import grpc
import requests
import urllib3
from google.protobuf.timestamp_pb2 import Timestamp
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, generate_latest
from urllib3.exceptions import InsecureRequestWarning

import fleet_gateway_pb2
import fleet_gateway_pb2_grpc
import fleet_gateway_v2_pb2
import fleet_gateway_v2_pb2_grpc


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("monad-fleet-service")


TERMINAL_STATUSES = {"COMPLETED", "FAILED", "PARTIAL"}
KNOWN_STATUSES = {"PLANNED", "RUNNING", "COMPLETED", "FAILED", "PARTIAL"}
DEFAULT_POLICY_METADATA_KEYS = (
    "fleet.policy",
    "policy",
    "fleet_policy",
    "policy_json",
)
PROM_REGISTRY = CollectorRegistry()

REPORTS_TOTAL = Counter(
    "monad_fleet_reports_total",
    "Total v2 reports processed by status.",
    ["status"],
    registry=PROM_REGISTRY,
)
EVENTS_TOTAL = Counter(
    "monad_fleet_events_total",
    "Total event messages ingested.",
    ["event_type"],
    registry=PROM_REGISTRY,
)
METRIC_UPDATES_TOTAL = Counter(
    "monad_fleet_metric_updates_total",
    "Total numeric metric updates accepted by source.",
    ["source"],
    registry=PROM_REGISTRY,
)
INGEST_HTTP_REQUESTS_TOTAL = Counter(
    "monad_fleet_ingest_http_requests_total",
    "Total HTTP ingest requests by status code.",
    ["status_code"],
    registry=PROM_REGISTRY,
)
ARTIFACT_UPLOADS_TOTAL = Counter(
    "monad_fleet_artifact_uploads_total",
    "Total artifact upload ingest attempts by status.",
    ["status"],
    registry=PROM_REGISTRY,
)
METRIC_VALUE = Gauge(
    "monad_fleet_metric_value",
    "Latest numeric metric value for device+metric.",
    ["device_id", "metric"],
    registry=PROM_REGISTRY,
)
DEVICE_LAST_SEEN_UNIX = Gauge(
    "monad_fleet_device_last_seen_unix",
    "Last observed timestamp per device.",
    ["device_id"],
    registry=PROM_REGISTRY,
)

INGEST_JOURNAL_PATH: Path | None = None
ARTIFACT_INGEST_JOURNAL_PATH: Path | None = None
INGEST_JOURNAL_LOCK = threading.Lock()
ELAB_CLIENT_FOR_HTTP: Any = None
METRICS_EXCLUDE_PREFIXES: tuple[str, ...] = ()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def normalize_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_device_id(value: Any) -> str:
    return normalize_string(value).lower()


def looks_like_mac(value: str) -> bool:
    return re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", normalize_device_id(value)) is not None


def parse_maybe_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return default
    return default


def parse_tags(raw_tags: Any) -> list[str]:
    if isinstance(raw_tags, list):
        if raw_tags and isinstance(raw_tags[0], dict):
            return [normalize_string(tag.get("tag")) for tag in raw_tags if normalize_string(tag.get("tag"))]
        return [normalize_string(tag) for tag in raw_tags if normalize_string(tag)]
    if isinstance(raw_tags, str):
        return [part.strip() for part in raw_tags.split("|") if part.strip()]
    return []


def stable_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def safe_metric_token(value: Any, default: str = "unknown") -> str:
    text = normalize_string(value).lower()
    text = re.sub(r"[^a-z0-9_]", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or default


def to_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = normalize_string(value)
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


def parse_experiment_numeric_id(value: Any) -> int | None:
    text = normalize_string(value)
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


def append_ingest_journal(payload: dict[str, Any]) -> None:
    path = INGEST_JOURNAL_PATH
    if path is None:
        return
    line = stable_dumps(payload) + "\n"
    with INGEST_JOURNAL_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def append_artifact_journal(payload: dict[str, Any]) -> None:
    path = ARTIFACT_INGEST_JOURNAL_PATH
    if path is None:
        return
    line = stable_dumps(payload) + "\n"
    with INGEST_JOURNAL_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def find_artifact_upload_in_journal(
    *,
    experiment_id: int,
    run_id: str,
    agent_id: str,
    artifact_name: str,
    sha256_value: str,
    size_bytes: int,
) -> dict[str, Any] | None:
    path = ARTIFACT_INGEST_JOURNAL_PATH
    if path is None or not path.exists():
        return None

    target_run = normalize_string(run_id)
    target_agent = normalize_device_id(agent_id)
    target_name = normalize_string(artifact_name)
    target_sha = normalize_string(sha256_value)
    target_size = max(0, int(size_bytes))

    with INGEST_JOURNAL_LOCK:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return None

    for raw in reversed(lines):
        if not normalize_string(raw):
            continue
        try:
            entry = json.loads(raw)
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue

        if int(entry.get("experiment_id", -1)) != int(experiment_id):
            continue
        if normalize_string(entry.get("run_id")) != target_run:
            continue
        if normalize_device_id(entry.get("agent_id")) != target_agent:
            continue
        if normalize_string(entry.get("artifact_name")) != target_name:
            continue

        entry_sha = normalize_string(entry.get("sha256"))
        entry_size = int(entry.get("size_bytes", 0) or 0)
        if target_sha:
            if entry_sha != target_sha:
                continue
        else:
            if max(0, entry_size) != target_size:
                continue

        location = normalize_string(entry.get("location"))
        upload_id = entry.get("upload_id")
        if not location and not isinstance(upload_id, int):
            continue
        return entry

    return None


def build_uploaded_artifact_name(
    *,
    artifact_name: str,
    run_id: str,
    agent_id: str,
    upload_phase: str,
    sha256_value: str,
    size_bytes: int,
) -> str:
    original = normalize_string(artifact_name) or "artifact.bin"
    phase_token = safe_metric_token(upload_phase, "report").replace("_", "-")

    run_raw = re.sub(r"[^a-zA-Z0-9]", "", normalize_string(run_id))
    run_short = (run_raw[:8] if run_raw else "run")

    agent_raw = re.sub(r"[^a-zA-Z0-9]", "", normalize_device_id(agent_id))
    agent_short = (agent_raw[-6:] if agent_raw else "agent")

    p = Path(original)
    ext = p.suffix if p.suffix and len(p.suffix) <= 12 else ""
    stem = p.name[: -len(ext)] if ext else p.name
    stem_safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip("-_.") or "artifact"

    sha_token = normalize_string(sha256_value).lower()
    content_tag = sha_token[:8] if sha_token else f"sz{max(0, int(size_bytes))}"

    base = f"run-{run_short}__{phase_token}__ag-{agent_short}__{stem_safe}__{content_tag}"
    max_base_len = 180 - len(ext)
    if len(base) > max_base_len:
        base = base[:max_base_len].rstrip("-_.")
    return (base or "artifact") + ext


def mark_device_seen(device_id: str) -> None:
    token = normalize_device_id(device_id)
    if not token:
        return
    DEVICE_LAST_SEEN_UNIX.labels(device_id=token).set(time.time())


def should_export_metric(metric_name: str) -> bool:
    token = safe_metric_token(metric_name, "")
    if not token:
        return False
    if not METRICS_EXCLUDE_PREFIXES:
        return True
    for prefix in METRICS_EXCLUDE_PREFIXES:
        if prefix and token.startswith(prefix):
            return False
    return True


def ingest_numeric_metric(source: str, device_id: str, metric_name: str, value: float) -> None:
    source_token = safe_metric_token(source, "unknown")
    metric_token = safe_metric_token(metric_name, "metric")
    if not should_export_metric(metric_token):
        return
    device_token = normalize_device_id(device_id) or "unknown"
    METRIC_UPDATES_TOTAL.labels(source=source_token).inc()
    METRIC_VALUE.labels(device_id=device_token, metric=metric_token).set(float(value))


def ingest_external_payload(payload: dict[str, Any]) -> int:
    if not isinstance(payload, dict):
        return 0

    source = normalize_string(payload.get("source") or "external")
    device_id = normalize_device_id(payload.get("device_id") or payload.get("agent_id") or "unknown")
    mark_device_seen(device_id)

    accepted = 0
    values_map = payload.get("values")
    if isinstance(values_map, dict):
        for key, raw_value in values_map.items():
            numeric = to_float(raw_value)
            if numeric is None:
                continue
            ingest_numeric_metric(source, device_id, normalize_string(key), numeric)
            accepted += 1

    metrics_rows = payload.get("metrics")
    if isinstance(metrics_rows, list):
        for row in metrics_rows:
            if not isinstance(row, dict):
                continue
            name = normalize_string(row.get("name") or row.get("metric"))
            numeric = to_float(row.get("value"))
            if not name or numeric is None:
                continue
            ingest_numeric_metric(source, device_id, name, numeric)
            accepted += 1

    append_ingest_journal(
        {
            "received_at": utc_now_iso(),
            "device_id": device_id,
            "source": source,
            "accepted_points": accepted,
            "payload": payload,
        }
    )
    return accepted


def read_metadata_value(metadata: Any, key: str) -> Any:
    if not isinstance(metadata, dict):
        return None

    # direct lookup
    if key in metadata:
        return metadata[key]

    # dot-path lookup
    current: Any = metadata
    for token in key.split("."):
        if not isinstance(current, dict) or token not in current:
            current = None
            break
        current = current[token]
    if current is not None:
        return current

    extra_fields = metadata.get("extra_fields")
    if isinstance(extra_fields, dict):
        if key in extra_fields:
            value = extra_fields[key]
            if isinstance(value, dict) and "value" in value:
                return value.get("value")
            return value

        lower = key.lower()
        for name, value in extra_fields.items():
            if str(name).lower() == lower:
                if isinstance(value, dict) and "value" in value:
                    return value.get("value")
                return value
    return None


def event_time_iso(unix_time_ms: int) -> str:
    if unix_time_ms <= 0:
        return utc_now_iso()
    return datetime.fromtimestamp(unix_time_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_timestamp(value: Any) -> datetime | None:
    text = normalize_string(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def timestamp_from_unix_ms(unix_time_ms: int) -> Timestamp:
    ts = Timestamp()
    if unix_time_ms <= 0:
        ts.FromDatetime(utc_now())
        return ts
    dt = datetime.fromtimestamp(unix_time_ms / 1000, tz=timezone.utc)
    ts.FromDatetime(dt)
    return ts


def timestamp_from_iso(value: Any) -> Timestamp:
    ts = Timestamp()
    dt = parse_iso_timestamp(value) or utc_now()
    ts.FromDatetime(dt)
    return ts


def unix_ms_from_timestamp(ts: Timestamp) -> int:
    if not ts:
        return int(time.time() * 1000)
    return int(ts.seconds * 1000 + ts.nanos / 1_000_000)


def mode_enum_to_text(mode_value: int) -> str:
    if mode_value == fleet_gateway_v2_pb2.DUAL_NIC:
        return "DUAL_NIC"
    if mode_value == fleet_gateway_v2_pb2.RF_SHARING:
        return "RF_SHARING"
    return "CONTROL_PLANE_MODE_UNSPECIFIED"


class LocalState:
    def __init__(self, path: Path, max_event_ids: int = 50000):
        self._path = path
        self._max_event_ids = max_event_ids
        self._lock = threading.Lock()
        self._data = {
            "seen_events": {},
            "seen_preparations": {},
            "seen_reports": {},
            "runs": {},
        }
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self._data["seen_events"] = payload.get("seen_events", {}) or {}
                self._data["seen_preparations"] = payload.get("seen_preparations", {}) or {}
                self._data["seen_reports"] = payload.get("seen_reports", {}) or {}
                self._data["runs"] = payload.get("runs", {}) or {}
        except Exception:
            log.exception("Failed to load local state; starting with empty state")

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(stable_dumps(self._data), encoding="utf-8")
        tmp_path.replace(self._path)

    def _prune_seen_events_locked(self) -> None:
        seen = self._data.get("seen_events", {})
        if not isinstance(seen, dict):
            self._data["seen_events"] = {}
            return
        if len(seen) <= self._max_event_ids:
            return

        # Keep newest ids by timestamp.
        ordered = sorted(seen.items(), key=lambda pair: int(pair[1]), reverse=True)
        kept = dict(ordered[: self._max_event_ids])
        self._data["seen_events"] = kept

    def has_event(self, event_id: str) -> bool:
        if not event_id:
            return False
        with self._lock:
            seen = self._data.get("seen_events", {})
            return event_id in seen

    def mark_event(self, event_id: str) -> None:
        if not event_id:
            return
        with self._lock:
            seen = self._data.setdefault("seen_events", {})
            seen[event_id] = int(time.time() * 1000)
            self._prune_seen_events_locked()
            self._save_locked()

    def has_preparation(self, preparation_id: str) -> bool:
        token = normalize_string(preparation_id)
        if not token:
            return False
        with self._lock:
            seen = self._data.get("seen_preparations", {})
            return token in seen

    def mark_preparation(self, preparation_id: str) -> None:
        token = normalize_string(preparation_id)
        if not token:
            return
        with self._lock:
            seen = self._data.setdefault("seen_preparations", {})
            seen[token] = int(time.time() * 1000)
            self._save_locked()

    def has_report(self, report_key: str) -> bool:
        token = normalize_string(report_key)
        if not token:
            return False
        with self._lock:
            seen = self._data.get("seen_reports", {})
            return token in seen

    def mark_report(self, report_key: str) -> None:
        token = normalize_string(report_key)
        if not token:
            return
        with self._lock:
            seen = self._data.setdefault("seen_reports", {})
            seen[token] = int(time.time() * 1000)
            self._save_locked()

    def get_run_event_id(self, run_key: str) -> int | None:
        with self._lock:
            runs = self._data.get("runs", {})
            record = runs.get(run_key)
            if not isinstance(record, dict):
                return None
            event_id = record.get("event_id")
            if isinstance(event_id, int):
                return event_id
            return None

    def set_run_event_id(self, run_key: str, event_id: int, status: str) -> None:
        with self._lock:
            runs = self._data.setdefault("runs", {})
            runs[run_key] = {
                "event_id": event_id,
                "status": status,
                "updated_at": utc_now_iso(),
            }
            self._save_locked()


class ElabFTWClient:
    def __init__(self, base_url: str, api_key: str | None, verify_tls: bool = False):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._verify_tls = verify_tls

        log.info("Configuring eLabFTW client base_url=%s verify_tls=%s", self._base_url, self._verify_tls)
        if not self._verify_tls:
            urllib3.disable_warnings(InsecureRequestWarning)
        if not self._api_key:
            log.warning("ELAB_API_KEY is not set; API calls will fail with 401")

    def _coerce_metadata_payload(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        if "metadata" not in payload:
            return payload

        metadata = payload.get("metadata")
        if isinstance(metadata, str):
            return payload

        try:
            metadata_str = stable_dumps(metadata)
        except Exception:
            return payload

        patched_payload = dict(payload)
        patched_payload["metadata"] = metadata_str
        return patched_payload

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self._base_url}{path}"
        headers = kwargs.pop("headers", {}) or {}
        if self._api_key:
            headers["Authorization"] = self._api_key

        request_json = kwargs.get("json")
        response = requests.request(
            method,
            url,
            headers=headers,
            timeout=20,
            verify=self._verify_tls,
            **kwargs,
        )

        # Compatibility fallback: some eLabFTW builds reject object metadata in
        # PATCH/POST and expect metadata as a JSON string.
        if not response.ok and method.upper() in {"PATCH", "POST", "PUT"}:
            coerced_json = self._coerce_metadata_payload(request_json)
            if isinstance(coerced_json, dict) and coerced_json is not request_json:
                retry_kwargs = dict(kwargs)
                retry_kwargs["json"] = coerced_json
                retry = requests.request(
                    method,
                    url,
                    headers=headers,
                    timeout=20,
                    verify=self._verify_tls,
                    **retry_kwargs,
                )
                if retry.ok:
                    log.warning(
                        "Recovered %s %s by serializing metadata payload for eLabFTW compatibility",
                        method.upper(),
                        path,
                    )
                    return retry
                response = retry

        if not response.ok:
            body_preview = response.text[:500]
            raise RuntimeError(f"HTTP {response.status_code} {response.reason} for {path}: {body_preview}")

        return response

    def request_json(self, method: str, path: str, **kwargs) -> Any:
        response = self._request(method, path, **kwargs)
        if not response.text:
            return None
        return response.json()

    def list_items(self, *, search: str | None = None, tags: list[str] | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "type": "resources",
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
        }
        if search:
            params["search"] = search
        if tags:
            params["tags[]"] = tags
        data = self.request_json("GET", "/items", params=params)
        return data if isinstance(data, list) else []

    def create_device_item(self, device_id: str) -> dict[str, Any]:
        payload = {
            "type": "resources",
            "title": f"Device {device_id}",
            "is_bookable": 1,
            "book_max_minutes": 180,
            "book_can_overlap": 1,
            "book_is_cancellable": 1,
            "body": stable_dumps(
                {
                    "device_id": device_id,
                    "mac_address": device_id if looks_like_mac(device_id) else "",
                    "created_by": "monad-fleet-service",
                }
            ),
            "metadata": stable_dumps(
                {
                    "device_id": device_id,
                    "mac_address": device_id if looks_like_mac(device_id) else "",
                    "last_seen_at": utc_now_iso(),
                }
            ),
        }
        return self.request_json("POST", "/items", json=payload) or {}

    def patch_item_metadata(self, item_id: int, metadata: dict[str, Any]) -> dict[str, Any]:
        payload = {"metadata": stable_dumps(metadata)}
        return self.request_json("PATCH", f"/items/{item_id}", json=payload) or {}

    def patch_item_body(self, item_id: int, body: str) -> dict[str, Any]:
        payload = {"body": body}
        return self.request_json("PATCH", f"/items/{item_id}", json=payload) or {}

    def patch_item_fields(self, item_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        return self.request_json("PATCH", f"/items/{item_id}", json=fields) or {}

    def list_experiments_by_tag(self, tag: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
        }
        if tag:
            params["tags[]"] = tag
        data = self.request_json("GET", "/experiments", params=params)
        return data if isinstance(data, list) else []

    def get_experiment(self, experiment_id: int) -> dict[str, Any] | None:
        data = self.request_json("GET", f"/experiments/{experiment_id}")
        return data if isinstance(data, dict) else None

    def create_event_for_item(self, item_id: int, payload: dict[str, Any]) -> int:
        def parse_location_event_id(response_obj: requests.Response) -> int:
            location = response_obj.headers.get("location", "")
            match = re.search(r"/event/(\d+)$", location)
            if not match:
                raise RuntimeError(f"Could not parse event id from Location header: {location}")
            return int(match.group(1))

        try:
            response = self._request("POST", f"/events/{item_id}", json=payload)
            return parse_location_event_id(response)
        except Exception as exc:
            # Some eLabFTW versions reject ISO-8601 with timezone suffix for scheduler create.
            dt_payload = dict(payload)

            def normalize_event_dt(raw_value: Any) -> Any:
                value = normalize_string(raw_value)
                if not value:
                    return raw_value
                try:
                    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    return dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    return raw_value

            dt_payload["start"] = normalize_event_dt(payload.get("start"))
            dt_payload["end"] = normalize_event_dt(payload.get("end"))

            if dt_payload.get("start") == payload.get("start") and dt_payload.get("end") == payload.get("end"):
                raise exc

            response = self._request("POST", f"/events/{item_id}", json=dt_payload)
            return parse_location_event_id(response)

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        data = self.request_json("GET", f"/event/{event_id}")
        return data if isinstance(data, dict) else None

    def patch_event_metadata(self, event_id: int, metadata: dict[str, Any]) -> dict[str, Any] | None:
        return self.request_json("PATCH", f"/event/{event_id}", json={"metadata": stable_dumps(metadata)})

    def upload_experiment_artifact(
        self,
        experiment_id: int,
        filename: str,
        content: bytes,
        *,
        comment: str = "",
        mime: str = "application/octet-stream",
    ) -> dict[str, Any]:
        files = {
            "file": (normalize_string(filename) or "artifact.bin", content, normalize_string(mime) or "application/octet-stream")
        }
        data = {}
        if normalize_string(comment):
            data["comment"] = normalize_string(comment)

        response = self._request("POST", f"/experiments/{int(experiment_id)}/uploads", files=files, data=data)
        location = normalize_string(response.headers.get("location"))
        upload_id = None
        match = re.search(r"/uploads/(\d+)$", location)
        if match:
            upload_id = int(match.group(1))

        details: dict[str, Any] = {}
        if upload_id is not None:
            fetched = self.request_json("GET", f"/experiments/{int(experiment_id)}/uploads/{upload_id}")
            if isinstance(fetched, dict):
                details.update(fetched)
        details["location"] = location
        if upload_id is not None:
            details["upload_id"] = upload_id
        return details


class MetricsIngestHttpHandler(BaseHTTPRequestHandler):
    cfg: dict[str, Any] = {}

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        log.info("http-ingest: " + format, *args)

    def _write_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_failed(self) -> bool:
        expected = normalize_string(self.cfg.get("ingest_api_token"))
        if not expected:
            return False
        provided = normalize_string(self.headers.get("x-ingest-token"))
        return provided != expected

    def _read_json_body(self) -> Any:
        raw_len = normalize_string(self.headers.get("Content-Length"))
        content_len = int(raw_len) if raw_len.isdigit() else 0
        if content_len <= 0:
            raise ValueError("empty body")
        return json.loads(self.rfile.read(content_len).decode("utf-8"))

    def _handle_metrics_ingest(self) -> None:
        try:
            payload = self._read_json_body()
        except ValueError:
            INGEST_HTTP_REQUESTS_TOTAL.labels(status_code="400").inc()
            self._write_json(400, {"ok": False, "error": "empty body"})
            return
        except Exception:
            INGEST_HTTP_REQUESTS_TOTAL.labels(status_code="400").inc()
            self._write_json(400, {"ok": False, "error": "invalid json"})
            return

        accepted = 0
        if isinstance(payload, dict):
            accepted = ingest_external_payload(payload)
        elif isinstance(payload, list):
            for row in payload:
                if isinstance(row, dict):
                    accepted += ingest_external_payload(row)
        else:
            INGEST_HTTP_REQUESTS_TOTAL.labels(status_code="400").inc()
            self._write_json(400, {"ok": False, "error": "payload must be object or list"})
            return

        INGEST_HTTP_REQUESTS_TOTAL.labels(status_code="202").inc()
        self._write_json(202, {"ok": True, "accepted_points": accepted})

    def _handle_artifact_ingest(self) -> None:
        global ELAB_CLIENT_FOR_HTTP
        if ELAB_CLIENT_FOR_HTTP is None:
            ARTIFACT_UPLOADS_TOTAL.labels(status="rejected").inc()
            self._write_json(503, {"ok": False, "error": "elab client unavailable"})
            return

        try:
            payload = self._read_json_body()
        except ValueError:
            ARTIFACT_UPLOADS_TOTAL.labels(status="rejected").inc()
            self._write_json(400, {"ok": False, "error": "empty body"})
            return
        except Exception:
            ARTIFACT_UPLOADS_TOTAL.labels(status="rejected").inc()
            self._write_json(400, {"ok": False, "error": "invalid json"})
            return

        if not isinstance(payload, dict):
            ARTIFACT_UPLOADS_TOTAL.labels(status="rejected").inc()
            self._write_json(400, {"ok": False, "error": "payload must be object"})
            return

        exp_id = parse_experiment_numeric_id(payload.get("experiment_id"))
        run_id = normalize_string(payload.get("run_id"))
        agent_id = normalize_device_id(payload.get("agent_id"))
        artifact_name = normalize_string(payload.get("artifact_name"))
        upload_phase = normalize_string(payload.get("upload_phase")) or "report_replay"
        mime = normalize_string(payload.get("mime")) or "application/octet-stream"
        comment = normalize_string(payload.get("comment"))
        content_b64 = normalize_string(payload.get("content_b64"))
        sha256_value = normalize_string(payload.get("sha256"))
        size_hint = normalize_string(payload.get("size_bytes"))

        if exp_id is None or not artifact_name or not content_b64:
            ARTIFACT_UPLOADS_TOTAL.labels(status="rejected").inc()
            self._write_json(400, {"ok": False, "error": "experiment_id, artifact_name, content_b64 are required"})
            return

        try:
            raw = base64.b64decode(content_b64.encode("utf-8"), validate=True)
        except Exception:
            ARTIFACT_UPLOADS_TOTAL.labels(status="rejected").inc()
            self._write_json(400, {"ok": False, "error": "invalid base64 content"})
            return

        max_bytes = max(1024, int(self.cfg.get("artifact_max_bytes", 20 * 1024 * 1024)))
        if len(raw) > max_bytes:
            ARTIFACT_UPLOADS_TOTAL.labels(status="rejected").inc()
            self._write_json(413, {"ok": False, "error": f"artifact too large ({len(raw)} > {max_bytes})"})
            return

        if not comment:
            comment = f"fleet-v3 run={run_id} agent={agent_id} artifact={artifact_name} sha256={sha256_value} size={size_hint}"

        existing = find_artifact_upload_in_journal(
            experiment_id=exp_id,
            run_id=run_id,
            agent_id=agent_id,
            artifact_name=artifact_name,
            sha256_value=sha256_value,
            size_bytes=len(raw),
        )
        if existing is not None:
            existing_location = normalize_string(existing.get("location"))
            existing_upload_id = existing.get("upload_id")
            existing_artifact_uri = ""
            if isinstance(existing_upload_id, int):
                existing_artifact_uri = f"elabftw://experiments/{exp_id}/uploads/{existing_upload_id}"
            elif existing_location:
                existing_artifact_uri = existing_location
            existing_stored_name = normalize_string(existing.get("stored_artifact_name")) or normalize_string(
                existing.get("artifact_name")
            )
            ARTIFACT_UPLOADS_TOTAL.labels(status="deduplicated").inc()
            self._write_json(
                202,
                {
                    "ok": True,
                    "deduplicated": True,
                    "experiment_id": exp_id,
                    "upload_id": existing_upload_id,
                    "artifact_uri": existing_artifact_uri,
                    "location": existing_location,
                    "stored_artifact_name": existing_stored_name,
                },
            )
            return

        upload_filename = build_uploaded_artifact_name(
            artifact_name=artifact_name,
            run_id=run_id,
            agent_id=agent_id,
            upload_phase=upload_phase,
            sha256_value=sha256_value,
            size_bytes=len(raw),
        )

        try:
            uploaded = ELAB_CLIENT_FOR_HTTP.upload_experiment_artifact(
                exp_id,
                upload_filename,
                raw,
                comment=comment,
                mime=mime,
            )
        except Exception as exc:
            ARTIFACT_UPLOADS_TOTAL.labels(status="error").inc()
            self._write_json(502, {"ok": False, "error": f"upload failed: {exc}"})
            return

        location = normalize_string(uploaded.get("location"))
        upload_id = uploaded.get("upload_id")
        artifact_uri = ""
        if isinstance(upload_id, int):
            artifact_uri = f"elabftw://experiments/{exp_id}/uploads/{upload_id}"
        elif location:
            artifact_uri = location

        append_artifact_journal(
            {
                "received_at": utc_now_iso(),
                "experiment_id": exp_id,
                "run_id": run_id,
                "agent_id": agent_id,
                "artifact_name": artifact_name,
                "stored_artifact_name": upload_filename,
                "upload_phase": upload_phase,
                "sha256": sha256_value,
                "size_bytes": len(raw),
                "upload_id": upload_id,
                "location": location,
            }
        )
        ARTIFACT_UPLOADS_TOTAL.labels(status="uploaded").inc()
        self._write_json(
            202,
            {
                "ok": True,
                "experiment_id": exp_id,
                "upload_id": upload_id,
                "artifact_uri": artifact_uri,
                "location": location,
                "stored_artifact_name": upload_filename,
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._write_json(200, {"ok": True, "time": utc_now_iso()})
            return
        if path == "/metrics":
            body = generate_latest(PROM_REGISTRY)
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._write_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in {"/ingest/v1/metrics", "/ingest/v1/artifacts"}:
            INGEST_HTTP_REQUESTS_TOTAL.labels(status_code="404").inc()
            self._write_json(404, {"ok": False, "error": "not found"})
            return

        if self._auth_failed():
            INGEST_HTTP_REQUESTS_TOTAL.labels(status_code="401").inc()
            self._write_json(401, {"ok": False, "error": "unauthorized"})
            return

        if path == "/ingest/v1/artifacts":
            self._handle_artifact_ingest()
            return

        self._handle_metrics_ingest()


def start_http_sidecar(cfg: dict[str, Any]) -> ThreadingHTTPServer:
    port = int(cfg.get("metrics_port", 9108))
    bind = normalize_string(cfg.get("metrics_bind", "0.0.0.0")) or "0.0.0.0"

    class BoundHandler(MetricsIngestHttpHandler):
        pass

    BoundHandler.cfg = cfg
    httpd = ThreadingHTTPServer((bind, port), BoundHandler)
    thread = threading.Thread(target=httpd.serve_forever, name="http-sidecar", daemon=True)
    thread.start()
    log.info("HTTP sidecar started on http://%s:%d (metrics + ingest)", bind, port)
    return httpd


class FleetManagerServicer(fleet_gateway_pb2_grpc.FleetManagerServicer):
    def __init__(self, elab_client: ElabFTWClient, local_state: LocalState, cfg: dict[str, Any]):
        self._elab_client = elab_client
        self._state = local_state
        self._cfg = cfg
        self._metadata_patch_supported = True
        policy_keys = cfg.get("policy_metadata_keys") or []
        if not isinstance(policy_keys, list):
            policy_keys = []
        normalized_keys = [normalize_string(key) for key in policy_keys if normalize_string(key)]
        self._policy_metadata_keys = normalized_keys or list(DEFAULT_POLICY_METADATA_KEYS)

    def _find_device_item(self, device_id: str) -> dict[str, Any] | None:
        candidates = self._elab_client.list_items(search=device_id, limit=100)
        needle = normalize_device_id(device_id)

        for item in candidates:
            metadata = parse_maybe_json(item.get("metadata"), {})
            body = parse_maybe_json(item.get("body"), {})

            values = [
                read_metadata_value(metadata, "device_id"),
                read_metadata_value(metadata, "mac_address"),
                read_metadata_value(metadata, "mac"),
                read_metadata_value(metadata, "wifi_mac"),
                body.get("device_id") if isinstance(body, dict) else None,
                body.get("mac_address") if isinstance(body, dict) else None,
                body.get("mac") if isinstance(body, dict) else None,
            ]

            for value in values:
                if normalize_device_id(value) == needle:
                    return item

        return None

    def _update_item_presence(self, item: dict[str, Any], agent_info: fleet_gateway_pb2.AgentInfo | None) -> None:
        item_id = item.get("id")
        if not item_id:
            return

        desired_book_max_minutes = max(1, int(self._cfg.get("book_max_minutes", 180)))
        desired_book_can_overlap = 1 if bool(self._cfg.get("book_can_overlap", True)) else 0
        desired_book_is_cancellable = 1 if bool(self._cfg.get("book_is_cancellable", True)) else 0
        current_is_bookable = int(item.get("is_bookable") or 0)
        current_book_max_minutes = int(item.get("book_max_minutes") or 0)
        current_book_can_overlap = int(item.get("book_can_overlap") or 0)
        current_book_is_cancellable = int(item.get("book_is_cancellable") or 0)

        if (
            current_is_bookable != 1
            or current_book_max_minutes != desired_book_max_minutes
            or current_book_can_overlap != desired_book_can_overlap
            or current_book_is_cancellable != desired_book_is_cancellable
        ):
            try:
                patched = self._elab_client.patch_item_fields(
                    int(item_id),
                    {
                        "is_bookable": 1,
                        "book_max_minutes": desired_book_max_minutes,
                        "book_can_overlap": desired_book_can_overlap,
                        "book_is_cancellable": desired_book_is_cancellable,
                    },
                )
                if isinstance(patched, dict):
                    item.update(patched)
            except Exception as exc:
                log.warning("patch_item_fields(bookable) failed for item_id=%s: %s", item_id, exc)

        metadata = parse_maybe_json(item.get("metadata"), {})
        if not isinstance(metadata, dict):
            metadata = {}
        body = parse_maybe_json(item.get("body"), {})
        if not isinstance(body, dict):
            body = {}

        device_id = ""
        if agent_info and agent_info.device_id:
            device_id = normalize_device_id(agent_info.device_id)

        if device_id:
            metadata["device_id"] = device_id
            if looks_like_mac(device_id):
                metadata["mac_address"] = device_id

        metadata["last_seen_at"] = utc_now_iso()

        if agent_info:
            if agent_info.device_type:
                metadata["device_type"] = normalize_string(agent_info.device_type)
                body["device_type"] = normalize_string(agent_info.device_type)
            if agent_info.agent_version:
                metadata["agent_version"] = normalize_string(agent_info.agent_version)
                body["agent_version"] = normalize_string(agent_info.agent_version)
            if agent_info.capabilities:
                metadata["capabilities"] = dict(agent_info.capabilities)
                body["capabilities"] = dict(agent_info.capabilities)
            if agent_info.unix_time_ms > 0:
                metadata["last_agent_time_ms"] = int(agent_info.unix_time_ms)
                body["last_agent_time_ms"] = int(agent_info.unix_time_ms)

        body["last_seen_at"] = metadata["last_seen_at"]
        if device_id:
            body["device_id"] = device_id
            if looks_like_mac(device_id):
                body["mac_address"] = device_id

        if self._metadata_patch_supported:
            try:
                self._elab_client.patch_item_metadata(int(item_id), metadata)
                return
            except Exception as exc:
                log.warning("patch_item_metadata failed for item_id=%s: %s", item_id, exc)
                self._metadata_patch_supported = False

        try:
            self._elab_client.patch_item_body(int(item_id), stable_dumps(body))
        except Exception as exc:
            log.warning("patch_item_body fallback failed for item_id=%s: %s", item_id, exc)

    def _get_or_create_device_item(self, device_id: str, agent_info: fleet_gateway_pb2.AgentInfo | None) -> dict[str, Any]:
        item = self._find_device_item(device_id)
        if item is None:
            log.info("Creating new resource item for device_id=%s", device_id)
            item = self._elab_client.create_device_item(device_id)

        self._update_item_presence(item, agent_info)
        return item

    def _fetch_fleet_experiments(self) -> list[dict[str, Any]]:
        tag = self._cfg["fleet_experiment_tag"]
        limit = int(self._cfg["experiments_batch_size"])
        offset = 0
        rows: list[dict[str, Any]] = []

        while True:
            batch = self._elab_client.list_experiments_by_tag(tag, limit=limit, offset=offset)
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < limit:
                break
            offset += len(batch)

        return rows

    def _coerce_policy_dict(self, raw_value: Any) -> dict[str, Any] | None:
        candidate = parse_maybe_json(raw_value, None)
        if not isinstance(candidate, dict):
            return None

        # Support wrappers like {"policy": {...}} or {"value": "{...json...}"}.
        if isinstance(candidate.get("policy"), dict):
            candidate = candidate["policy"]
        elif "value" in candidate:
            nested = parse_maybe_json(candidate.get("value"), None)
            if isinstance(nested, dict):
                candidate = nested

        if not isinstance(candidate, dict):
            return None

        return json.loads(json.dumps(candidate))

    def _extract_policy_from_experiment(self, experiment: dict[str, Any]) -> dict[str, Any] | None:
        metadata = parse_maybe_json(experiment.get("metadata"), {})
        if not isinstance(metadata, dict):
            metadata = {}

        for key in self._policy_metadata_keys:
            raw_value = read_metadata_value(metadata, key)
            policy = self._coerce_policy_dict(raw_value)
            if policy is not None:
                return policy

        # Fallback for setups where policy fields are directly in metadata root.
        if isinstance(metadata.get("command_groups"), list):
            policy = {
                "command_groups": metadata.get("command_groups"),
            }
            for optional_key in ("target_selector", "range", "notes"):
                if optional_key in metadata:
                    policy[optional_key] = metadata[optional_key]
            return policy

        return None

    def _load_policy_context(self, experiment: dict[str, Any]) -> dict[str, Any] | None:
        policy_raw = self._extract_policy_from_experiment(experiment)
        if policy_raw is None:
            return None

        canonical_payload = json.loads(json.dumps(policy_raw))
        canonical_payload["experiment_id"] = f"elabftw:{experiment['id']}"
        canonical_payload.pop("policy_revision", None)

        canonical_without_revision = stable_dumps(canonical_payload)
        policy_revision = f"sha256:{hashlib.sha256(canonical_without_revision.encode('utf-8')).hexdigest()}"

        canonical_payload["policy_revision"] = policy_revision
        canonical_with_revision = stable_dumps(canonical_payload)

        return {
            "policy": canonical_payload,
            "policy_json": canonical_with_revision.encode("utf-8"),
            "policy_revision": policy_revision,
            "experiment": experiment,
        }

    def _item_matches_experiment_tags(self, item: dict[str, Any], experiment: dict[str, Any]) -> bool:
        prefix = self._cfg["resource_tag_prefix"]
        experiment_tags = parse_tags(experiment.get("tags"))
        wanted_tags = [tag for tag in experiment_tags if tag.startswith(prefix)]
        if not wanted_tags:
            return True

        item_tags = set(parse_tags(item.get("tags")))
        return any(tag in item_tags for tag in wanted_tags)

    def _item_matches_policy_selector(self, item: dict[str, Any], policy: dict[str, Any], device_id: str) -> bool:
        selector = policy.get("target_selector")
        if not isinstance(selector, dict):
            return True

        metadata = parse_maybe_json(item.get("metadata"), {})
        if not isinstance(metadata, dict):
            metadata = {}

        location = normalize_string(read_metadata_value(metadata, "location"))
        device_type = normalize_string(
            read_metadata_value(metadata, "device_type") or read_metadata_value(metadata, "type")
        )

        locations = {normalize_string(value) for value in selector.get("locations", []) if normalize_string(value)}
        device_types = {normalize_string(value) for value in selector.get("device_types", []) if normalize_string(value)}
        device_ids = {normalize_device_id(value) for value in selector.get("device_ids", []) if normalize_string(value)}

        if locations and location not in locations:
            return False
        if device_types and device_type not in device_types:
            return False
        if device_ids and normalize_device_id(device_id) not in device_ids:
            return False

        return True

    def _select_policy_for_device(self, item: dict[str, Any], device_id: str) -> dict[str, Any] | None:
        experiments = self._fetch_fleet_experiments()
        candidates: list[tuple[str, dict[str, Any]]] = []

        for experiment_ref in experiments:
            exp_id = experiment_ref.get("id")
            if exp_id is None:
                continue

            experiment = self._elab_client.get_experiment(int(exp_id))
            if not experiment:
                continue

            if not self._item_matches_experiment_tags(item, experiment):
                continue

            try:
                policy_ctx = self._load_policy_context(experiment)
            except Exception:
                log.exception("Failed to parse policy for experiment %s", exp_id)
                continue

            if not policy_ctx:
                continue

            if not self._item_matches_policy_selector(item, policy_ctx["policy"], device_id):
                continue

            modified_at = normalize_string(experiment.get("modified_at") or experiment_ref.get("modified_at"))
            candidates.append((modified_at, policy_ctx))

        if not candidates:
            return None

        candidates.sort(key=lambda row: row[0], reverse=True)
        return candidates[0][1]

    def _run_key(self, event: fleet_gateway_pb2.Event) -> str:
        return f"{event.experiment_id}|{event.policy_revision}|{normalize_device_id(event.device_id)}"

    def _status_from_event(self, event: fleet_gateway_pb2.Event) -> str:
        metrics = dict(event.metrics)
        run_state = normalize_string(metrics.get("run_state")).upper()
        if run_state in KNOWN_STATUSES:
            return run_state

        event_type = normalize_string(event.type).upper()
        if event_type == "ERROR":
            return "FAILED"
        if event_type == "COMMAND_FINISHED":
            return "RUNNING" if event.exit_code == 0 else "PARTIAL"
        if event_type in {"COMMAND_STARTED", "ARTIFACT_UPLOADED", "HEARTBEAT"}:
            return "RUNNING"

        return "RUNNING"

    def _event_payload_dict(self, event: fleet_gateway_pb2.Event) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "type": event.type,
            "command_id": event.command_id,
            "exit_code": int(event.exit_code),
            "unix_time_ms": int(event.unix_time_ms),
            "timestamp": event_time_iso(int(event.unix_time_ms)),
            "stdout_tail": event.stdout_tail,
            "stderr_tail": event.stderr_tail,
            "metrics": dict(event.metrics),
            "artifacts": dict(event.artifacts),
        }

    def _ensure_run_event(self, item: dict[str, Any], event: fleet_gateway_pb2.Event, status: str) -> int:
        run_key = self._run_key(event)
        existing = self._state.get_run_event_id(run_key)
        if existing:
            return existing

        item_id = int(item["id"])
        now = utc_now()
        end = now + timedelta(minutes=int(self._cfg["event_duration_minutes"]))
        metadata = {
            "status": "RUNNING" if status != "PLANNED" else "PLANNED",
            "experiment_id": event.experiment_id,
            "policy_revision": event.policy_revision,
            "device": {
                "item_id": item_id,
                "device_id": normalize_device_id(event.device_id),
                "mac_address": normalize_device_id(event.device_id) if looks_like_mac(event.device_id) else "",
            },
            "started_at": utc_now_iso(),
            "last_update": utc_now_iso(),
            "events": [],
            "summary": {
                "events_total": 0,
                "commands_failed": 0,
                "artifacts_total": 0,
            },
        }

        payload = {
            "title": f"Fleet run {event.experiment_id} ({event.device_id})",
            "start": now.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "metadata": metadata,
        }

        created_event_id = self._elab_client.create_event_for_item(item_id, payload)
        self._state.set_run_event_id(run_key, created_event_id, status)
        return created_event_id

    def _update_run_event(self, event_id: int, event: fleet_gateway_pb2.Event, status: str) -> str:
        current_event = self._elab_client.get_event(event_id) or {}
        metadata = parse_maybe_json(current_event.get("metadata"), {})
        if not isinstance(metadata, dict):
            metadata = {}

        # Never downgrade status due to late artifact uploads or other out-of-order events.
        # Prefer terminal / more severe states when merging.
        current_status = normalize_string(metadata.get("status")).upper()
        incoming_status = normalize_string(status).upper() or "RUNNING"
        severity = {
            "PLANNED": 0,
            "RUNNING": 1,
            "COMPLETED": 2,
            "PARTIAL": 3,
            "FAILED": 4,
        }
        if current_status in severity and incoming_status in severity:
            merged_status = incoming_status if severity[incoming_status] >= severity[current_status] else current_status
        else:
            merged_status = incoming_status or current_status or "RUNNING"

        history = metadata.get("events")
        if not isinstance(history, list):
            history = []

        payload = self._event_payload_dict(event)
        history.append(payload)
        history = history[-int(self._cfg["max_event_history"]):]

        summary = metadata.get("summary")
        if not isinstance(summary, dict):
            summary = {}

        summary["events_total"] = int(summary.get("events_total", 0)) + 1
        if normalize_string(event.type).upper() == "ARTIFACT_UPLOADED":
            summary["artifacts_total"] = int(summary.get("artifacts_total", 0)) + 1
        if normalize_string(event.type).upper() == "COMMAND_FINISHED" and int(event.exit_code) != 0:
            summary["commands_failed"] = int(summary.get("commands_failed", 0)) + 1
        if normalize_string(event.type).upper() == "ERROR":
            summary["commands_failed"] = int(summary.get("commands_failed", 0)) + 1

        metadata["status"] = merged_status
        metadata["last_update"] = utc_now_iso()
        metadata["last_event"] = payload
        metadata["events"] = history
        metadata["summary"] = summary

        if merged_status in TERMINAL_STATUSES:
            metadata["completed_at"] = utc_now_iso()

        self._elab_client.patch_event_metadata(event_id, metadata)
        return merged_status

    def Hello(self, request, context):
        device_id = normalize_device_id(request.device_id)
        if not device_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "device_id is required")

        try:
            self._get_or_create_device_item(device_id, request)
            mark_device_seen(device_id)
            return fleet_gateway_pb2.ServerConfig(
                server_unix_time_ms=int(time.time() * 1000),
                poll_interval_s=int(self._cfg["poll_interval_s"]),
                required_min_agent_version=self._cfg["required_min_agent_version"],
            )
        except Exception as exc:
            log.exception("Hello failed for device_id=%s", device_id)
            context.abort(grpc.StatusCode.INTERNAL, f"Hello failed: {exc}")

    def GetPolicy(self, request, context):
        device_id = normalize_device_id(request.device_id)
        if not device_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "device_id is required")

        try:
            item = self._get_or_create_device_item(device_id, None)
            policy_ctx = self._select_policy_for_device(item, device_id)

            if not policy_ctx:
                return fleet_gateway_pb2.PolicyResponse(
                    not_modified=True,
                    policy_revision=request.last_policy_revision,
                    policy_json=b"",
                )

            revision = policy_ctx["policy_revision"]
            if normalize_string(request.last_policy_revision) == revision:
                return fleet_gateway_pb2.PolicyResponse(
                    not_modified=True,
                    policy_revision=revision,
                    policy_json=b"",
                )

            return fleet_gateway_pb2.PolicyResponse(
                not_modified=False,
                policy_revision=revision,
                policy_json=policy_ctx["policy_json"],
            )
        except Exception as exc:
            log.exception("GetPolicy failed for device_id=%s", device_id)
            context.abort(grpc.StatusCode.INTERNAL, f"GetPolicy failed: {exc}")

    def PublishEvent(self, request, context):
        return self._ingest_v1_event(request)

    def _ingest_v1_event(self, request: fleet_gateway_pb2.Event) -> fleet_gateway_pb2.Ack:
        device_id = normalize_device_id(request.device_id)
        event_id = normalize_string(request.event_id)
        experiment_id = normalize_string(request.experiment_id)
        policy_revision = normalize_string(request.policy_revision)

        if not device_id:
            return fleet_gateway_pb2.Ack(ok=False, message="device_id is required")
        if not event_id:
            return fleet_gateway_pb2.Ack(ok=False, message="event_id is required")
        if not experiment_id or not policy_revision:
            return fleet_gateway_pb2.Ack(ok=False, message="experiment_id and policy_revision are required")

        if self._state.has_event(event_id):
            return fleet_gateway_pb2.Ack(ok=True, message="duplicate event ignored")

        try:
            item = self._get_or_create_device_item(device_id, None)
            status = self._status_from_event(request)

            run_event_id = self._ensure_run_event(item, request, status)
            merged_status = self._update_run_event(run_event_id, request, status)

            self._state.mark_event(event_id)
            self._state.set_run_event_id(self._run_key(request), run_event_id, merged_status)
            mark_device_seen(device_id)
            EVENTS_TOTAL.labels(event_type=safe_metric_token(request.type, "unknown")).inc()

            for key, raw_value in dict(request.metrics).items():
                numeric = to_float(raw_value)
                if numeric is None:
                    continue
                ingest_numeric_metric("event", device_id, normalize_string(key), numeric)

            return fleet_gateway_pb2.Ack(ok=True, message=f"stored in event/{run_event_id}")
        except Exception as exc:
            log.exception("PublishEvent failed for device_id=%s", device_id)
            return fleet_gateway_pb2.Ack(ok=False, message=f"PublishEvent failed: {exc}")


class FleetManagerServicerV2(fleet_gateway_v2_pb2_grpc.FleetManagerServicer):
    def __init__(self, core: FleetManagerServicer, cfg: dict[str, Any]):
        self._core = core
        self._cfg = cfg
        self._state = core._state

    def _choose_allowed_mode(self, agent: fleet_gateway_v2_pb2.AgentDescriptor) -> tuple[int, str]:
        requested = int(agent.requested_mode)
        interfaces = list(agent.interfaces)
        has_ethernet = any(normalize_string(iface.kind).lower() == "ethernet" for iface in interfaces)
        iface_name = normalize_string(agent.control_plane_iface).lower()

        if requested == fleet_gateway_v2_pb2.DUAL_NIC:
            if not has_ethernet:
                return fleet_gateway_v2_pb2.RF_SHARING, "DUAL_NIC denied: no ethernet interface advertised"
            if iface_name and not iface_name.startswith("eth"):
                return fleet_gateway_v2_pb2.RF_SHARING, "DUAL_NIC denied: control_plane_iface is not ethernet"
            return fleet_gateway_v2_pb2.DUAL_NIC, "DUAL_NIC accepted"

        return fleet_gateway_v2_pb2.RF_SHARING, "RF_SHARING mode"

    def _agent_to_v1(self, agent: fleet_gateway_v2_pb2.AgentDescriptor) -> fleet_gateway_pb2.AgentInfo:
        capabilities = {normalize_string(cap): "1" for cap in agent.capabilities if normalize_string(cap)}
        return fleet_gateway_pb2.AgentInfo(
            device_id=normalize_device_id(agent.agent_id),
            agent_version=normalize_string(agent.agent_version),
            device_type=normalize_string(agent.hostname) or "agent-v2",
            capabilities=capabilities,
            unix_time_ms=int(time.time() * 1000),
        )

    def _update_v2_presence(self, item: dict[str, Any], agent: fleet_gateway_v2_pb2.AgentDescriptor) -> None:
        item_id = item.get("id")
        if not item_id:
            return
        metadata = parse_maybe_json(item.get("metadata"), {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["hostname"] = normalize_string(agent.hostname)
        metadata["agent_version"] = normalize_string(agent.agent_version)
        metadata["control_plane_iface"] = normalize_string(agent.control_plane_iface)
        metadata["requested_mode"] = mode_enum_to_text(int(agent.requested_mode))
        metadata["fleet_manager_target"] = normalize_string(agent.fleet_manager_target)
        metadata["capabilities_v2"] = [normalize_string(cap) for cap in agent.capabilities if normalize_string(cap)]
        metadata["interfaces"] = [
            {
                "name": normalize_string(iface.name),
                "mac": normalize_string(iface.mac),
                "kind": normalize_string(iface.kind),
                "driver": normalize_string(iface.driver),
                "firmware": normalize_string(iface.firmware),
            }
            for iface in agent.interfaces
        ]
        self._core._elab_client.patch_item_metadata(int(item_id), metadata)

    def _resolve_policy_ctx(self, agent_id: str) -> dict[str, Any] | None:
        item = self._core._get_or_create_device_item(agent_id, None)
        if not item:
            return None
        return self._core._select_policy_for_device(item, agent_id)

    def _measurement_window(self, policy: dict[str, Any]) -> tuple[str, str]:
        range_obj = policy.get("range") if isinstance(policy, dict) else {}
        if isinstance(range_obj, dict):
            start = normalize_string(range_obj.get("from"))
            end = normalize_string(range_obj.get("to"))
            if start and end:
                return start, end

        starts: list[str] = []
        ends: list[str] = []
        groups = policy.get("command_groups") if isinstance(policy, dict) else []
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                group_range = group.get("range")
                if not isinstance(group_range, dict):
                    continue
                s = normalize_string(group_range.get("from"))
                e = normalize_string(group_range.get("to"))
                if s:
                    starts.append(s)
                if e:
                    ends.append(e)
        return (min(starts) if starts else utc_now_iso(), max(ends) if ends else utc_now_iso())

    def _command_type(self, cmd: dict[str, Any]) -> int:
        raw = normalize_string(cmd.get("type") or cmd.get("command_type") or "SHELL").upper()
        mapping = {
            "SHELL": fleet_gateway_v2_pb2.SHELL,
            "CAPTURE_CSI": fleet_gateway_v2_pb2.CAPTURE_CSI,
            "BLE_SCAN": fleet_gateway_v2_pb2.BLE_SCAN,
            "WIFI_SCAN": fleet_gateway_v2_pb2.WIFI_SCAN,
        }
        return mapping.get(raw, fleet_gateway_v2_pb2.SHELL)

    def _failure_mode(self, group: dict[str, Any]) -> int:
        raw = normalize_string(group.get("failure_mode")).upper()
        if raw == "CONTINUE_ON_ERROR":
            return fleet_gateway_v2_pb2.CONTINUE_ON_ERROR
        if raw == "FAIL_FAST":
            return fleet_gateway_v2_pb2.FAIL_FAST
        return fleet_gateway_v2_pb2.FAIL_FAST

    def _iter_commands(self, group: dict[str, Any]) -> list[dict[str, Any]]:
        commands = group.get("commands", [])
        if isinstance(commands, list):
            return [row for row in commands if isinstance(row, dict)]
        if isinstance(commands, dict):
            rows: list[dict[str, Any]] = []
            for command_id, payload in commands.items():
                if not isinstance(payload, dict):
                    continue
                row = dict(payload)
                row.setdefault("id", normalize_string(command_id))
                rows.append(row)
            rows.sort(key=lambda row: int(row.get("order", 999999)))
            return rows
        return []

    def _policy_to_v2(self, policy: dict[str, Any], policy_id: str) -> fleet_gateway_v2_pb2.Policy:
        start_iso, end_iso = self._measurement_window(policy)
        command_groups: list[fleet_gateway_v2_pb2.CommandGroup] = []
        for group in policy.get("command_groups", []):
            if not isinstance(group, dict):
                continue
            commands_msg: list[fleet_gateway_v2_pb2.Command] = []
            for cmd in self._iter_commands(group):
                retry = cmd.get("retry")
                retries = int(cmd.get("retries", 0))
                backoff_ms = 0
                if isinstance(retry, dict):
                    retries = int(retry.get("max_attempts", retries) or retries)
                    backoff_ms = int(retry.get("backoff_ms", 0) or 0)
                timeout_ms = int(cmd.get("timeout_ms", 0) or 0)
                if timeout_ms <= 0:
                    timeout_ms = int(cmd.get("timeout_s", 60) or 60) * 1000
                env = cmd.get("env")
                if not isinstance(env, dict):
                    env = {}
                argv = cmd.get("argv")
                if not isinstance(argv, list):
                    argv = []
                expected_artifacts = []
                for art in cmd.get("expected_artifacts", []):
                    if not isinstance(art, dict):
                        continue
                    expected_artifacts.append(
                        fleet_gateway_v2_pb2.ArtifactSpec(
                            name=normalize_string(art.get("name")),
                            path_or_glob=normalize_string(art.get("path_or_glob") or art.get("path")),
                            mime=normalize_string(art.get("mime")),
                        )
                    )
                commands_msg.append(
                    fleet_gateway_v2_pb2.Command(
                        id=normalize_string(cmd.get("id") or cmd.get("name")),
                        type=self._command_type(cmd),
                        cmdline=normalize_string(cmd.get("cmdline") or cmd.get("cmd")),
                        argv=[normalize_string(arg) for arg in argv if normalize_string(arg)],
                        env={normalize_string(k): normalize_string(v) for k, v in env.items() if normalize_string(k)},
                        timeout_ms=max(0, timeout_ms),
                        retry=fleet_gateway_v2_pb2.RetryPolicy(max_attempts=max(1, retries + 1), backoff_ms=max(0, backoff_ms)),
                        expected_artifacts=expected_artifacts,
                    )
                )
            group_range = group.get("range") if isinstance(group.get("range"), dict) else {}
            group_start = normalize_string(group_range.get("from")) or start_iso
            group_end = normalize_string(group_range.get("to")) or end_iso
            command_groups.append(
                fleet_gateway_v2_pb2.CommandGroup(
                    id=normalize_string(group.get("id") or group.get("name")),
                    name=normalize_string(group.get("name") or group.get("id")),
                    **{
                        "from": timestamp_from_iso(group_start),
                        "to": timestamp_from_iso(group_end),
                    },
                    failure_mode=self._failure_mode(group),
                    commands=commands_msg,
                )
            )

        allowed_mode = fleet_gateway_v2_pb2.RF_SHARING
        control_plane_iface = ""
        policy_mode = normalize_string(policy.get("control_plane_mode")).upper()
        if policy_mode == "DUAL_NIC":
            allowed_mode = fleet_gateway_v2_pb2.DUAL_NIC
            control_plane_iface = normalize_string(policy.get("control_plane_iface"))

        return fleet_gateway_v2_pb2.Policy(
            experiment_id=normalize_string(policy.get("experiment_id")),
            policy_id=policy_id,
            issued_at=timestamp_from_iso(utc_now_iso()),
            measurement_from=timestamp_from_iso(start_iso),
            measurement_to=timestamp_from_iso(end_iso),
            command_groups=command_groups,
            allowed_mode=allowed_mode,
            control_plane_iface=control_plane_iface,
        )

    def _report_key(self, report: fleet_gateway_v2_pb2.Report, fallback_agent_id: str) -> str:
        run_id = normalize_string(report.run_id)
        agent_id = normalize_device_id(report.agent_id or fallback_agent_id)
        policy_id = normalize_string(report.policy_id)
        return f"{run_id}|{agent_id}|{policy_id}"

    def _event_type_name(self, event_type: int) -> str:
        mapping = {
            fleet_gateway_v2_pb2.RUN_STARTED: "RUN_STARTED",
            fleet_gateway_v2_pb2.RUN_FINISHED: "RUN_FINISHED",
            fleet_gateway_v2_pb2.COMMAND_STARTED: "COMMAND_STARTED",
            fleet_gateway_v2_pb2.COMMAND_FINISHED: "COMMAND_FINISHED",
            fleet_gateway_v2_pb2.COMMAND_FAILED: "COMMAND_FAILED",
            fleet_gateway_v2_pb2.ARTIFACT_UPLOADED: "ARTIFACT_UPLOADED",
            fleet_gateway_v2_pb2.ERROR: "ERROR",
        }
        return mapping.get(int(event_type), "EVENT_TYPE_UNSPECIFIED")

    def _to_v1_event(
        self,
        event: fleet_gateway_v2_pb2.Event,
        fallback_agent_id: str,
        fallback_experiment_id: str,
        fallback_policy_id: str,
        fallback_run_id: str,
    ) -> fleet_gateway_pb2.Event:
        event_id = normalize_string(event.event_id)
        run_id = normalize_string(event.run_id or fallback_run_id)
        if not event_id:
            seed = f"{run_id}|{normalize_string(event.command_id)}|{self._event_type_name(int(event.type))}|{unix_ms_from_timestamp(event.timestamp)}"
            digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
            event_id = f"v2:{digest}"

        metrics = {normalize_string(k): normalize_string(v) for k, v in event.metrics.items() if normalize_string(k)}
        if run_id:
            metrics.setdefault("run_id", run_id)
        if event.duration_ms > 0:
            metrics.setdefault("duration_ms", str(int(event.duration_ms)))

        return fleet_gateway_pb2.Event(
            event_id=event_id,
            experiment_id=normalize_string(event.experiment_id or fallback_experiment_id),
            policy_revision=normalize_string(event.policy_id or fallback_policy_id),
            device_id=normalize_device_id(event.agent_id or fallback_agent_id),
            unix_time_ms=unix_ms_from_timestamp(event.timestamp),
            type=self._event_type_name(int(event.type)),
            command_id=normalize_string(event.command_id),
            exit_code=int(event.exit_code),
            stdout_tail=normalize_string(event.message),
            stderr_tail="",
            metrics=metrics,
            artifacts={},
        )

    def _artifact_event(
        self,
        run_id: str,
        report: fleet_gateway_v2_pb2.Report,
        fallback_agent_id: str,
        artifact: fleet_gateway_v2_pb2.ArtifactRef,
        index: int,
    ) -> fleet_gateway_pb2.Event:
        event_id = f"{run_id}:artifact:{index}:{normalize_string(artifact.name)}"
        return fleet_gateway_pb2.Event(
            event_id=event_id,
            experiment_id=normalize_string(report.experiment_id),
            policy_revision=normalize_string(report.policy_id),
            device_id=normalize_device_id(report.agent_id or fallback_agent_id),
            unix_time_ms=int(time.time() * 1000),
            type="ARTIFACT_UPLOADED",
            command_id=normalize_string(artifact.name),
            exit_code=0,
            stdout_tail=normalize_string(artifact.uri),
            stderr_tail="",
            metrics={
                "run_id": normalize_string(run_id),
                "size_bytes": str(int(artifact.size_bytes)),
                "sha256": normalize_string(artifact.sha256),
            },
            artifacts={"uri": normalize_string(artifact.uri)},
        )

    def _record_report_metrics(self, report: fleet_gateway_v2_pb2.Report, fallback_agent_id: str) -> None:
        device_id = normalize_device_id(report.agent_id or fallback_agent_id) or "unknown"
        mark_device_seen(device_id)
        REPORTS_TOTAL.labels(status=safe_metric_token(report.status, "unknown")).inc()

        for key, raw_value in dict(report.summary_metrics).items():
            numeric = to_float(raw_value)
            if numeric is None:
                continue
            ingest_numeric_metric("report", device_id, normalize_string(key), numeric)

        for event in report.events:
            for key, raw_value in dict(event.metrics).items():
                numeric = to_float(raw_value)
                if numeric is None:
                    continue
                ingest_numeric_metric("report_event", device_id, normalize_string(key), numeric)

    def Hello(self, request, context):
        agent = request.agent
        agent_id = normalize_device_id(agent.agent_id)
        if not agent_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "agent.agent_id is required")
        try:
            v1 = self._agent_to_v1(agent)
            item = self._core._get_or_create_device_item(agent_id, v1)
            self._update_v2_presence(item, agent)
            allowed_mode, reason = self._choose_allowed_mode(agent)
            mark_device_seen(agent_id)
            return fleet_gateway_v2_pb2.HelloResponse(
                server_version="monad-fleet-service-v2-draft",
                server_time=timestamp_from_unix_ms(int(time.time() * 1000)),
                recommended_prepare_poll_sec=max(1, int(self._cfg["poll_interval_s"])),
                max_batch_size=200,
                allowed_mode=allowed_mode,
                mode_reason=reason,
            )
        except Exception as exc:
            log.exception("Hello(v2) failed for agent_id=%s", agent_id)
            context.abort(grpc.StatusCode.INTERNAL, f"Hello(v2) failed: {exc}")

    def GetAssignment(self, request, context):
        agent_id = normalize_device_id(request.agent_id)
        if not agent_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "agent_id is required")
        try:
            policy_ctx = self._resolve_policy_ctx(agent_id)
            if not policy_ctx:
                return fleet_gateway_v2_pb2.GetAssignmentResponse(
                    status=fleet_gateway_v2_pb2.GetAssignmentResponse.NO_WORK
                )
            policy = policy_ctx["policy"]
            start_iso, end_iso = self._measurement_window(policy)
            return fleet_gateway_v2_pb2.GetAssignmentResponse(
                status=fleet_gateway_v2_pb2.GetAssignmentResponse.ASSIGNED,
                experiment_id=normalize_string(policy.get("experiment_id")),
                policy_id=normalize_string(policy_ctx["policy_revision"]),
                measurement_from=timestamp_from_iso(start_iso),
                measurement_to=timestamp_from_iso(end_iso),
            )
        except Exception as exc:
            log.exception("GetAssignment(v2) failed for agent_id=%s", agent_id)
            context.abort(grpc.StatusCode.INTERNAL, f"GetAssignment(v2) failed: {exc}")

    def GetPolicy(self, request, context):
        agent_id = normalize_device_id(request.agent_id)
        if not agent_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "agent_id is required")
        try:
            policy_ctx = self._resolve_policy_ctx(agent_id)
            if not policy_ctx:
                return fleet_gateway_v2_pb2.GetPolicyResponse(
                    status=fleet_gateway_v2_pb2.GetPolicyResponse.NO_POLICY
                )
            policy_id = normalize_string(policy_ctx["policy_revision"])
            if normalize_string(request.last_policy_id) == policy_id:
                return fleet_gateway_v2_pb2.GetPolicyResponse(
                    status=fleet_gateway_v2_pb2.GetPolicyResponse.NOT_MODIFIED
                )
            policy = policy_ctx["policy"]
            if normalize_string(request.experiment_id):
                if normalize_string(policy.get("experiment_id")) != normalize_string(request.experiment_id):
                    return fleet_gateway_v2_pb2.GetPolicyResponse(
                        status=fleet_gateway_v2_pb2.GetPolicyResponse.NO_POLICY
                    )
            return fleet_gateway_v2_pb2.GetPolicyResponse(
                status=fleet_gateway_v2_pb2.GetPolicyResponse.OK,
                policy=self._policy_to_v2(policy, policy_id),
            )
        except Exception as exc:
            log.exception("GetPolicy(v2) failed for agent_id=%s", agent_id)
            context.abort(grpc.StatusCode.INTERNAL, f"GetPolicy(v2) failed: {exc}")

    def AckPrepared(self, request, context):
        agent_id = normalize_device_id(request.agent_id)
        preparation_id = normalize_string(request.preparation_id)
        if not agent_id or not preparation_id:
            return fleet_gateway_v2_pb2.AckPreparedResponse(
                status=fleet_gateway_v2_pb2.AckPreparedResponse.REJECTED,
                reason="agent_id and preparation_id are required",
            )
        if self._state.has_preparation(preparation_id):
            return fleet_gateway_v2_pb2.AckPreparedResponse(
                status=fleet_gateway_v2_pb2.AckPreparedResponse.ACCEPTED,
                reason="duplicate preparation acknowledged",
            )

        requested_mode = int(request.mode)
        iface = normalize_string(request.control_plane_iface).lower()
        if requested_mode == fleet_gateway_v2_pb2.DUAL_NIC:
            if not request.route_verified:
                return fleet_gateway_v2_pb2.AckPreparedResponse(
                    status=fleet_gateway_v2_pb2.AckPreparedResponse.REJECTED,
                    reason="DUAL_NIC requires route_verified=true",
                )
            if iface and not iface.startswith("eth"):
                return fleet_gateway_v2_pb2.AckPreparedResponse(
                    status=fleet_gateway_v2_pb2.AckPreparedResponse.REJECTED,
                    reason="DUAL_NIC requires ethernet control_plane_iface",
                )

        policy_ctx = self._resolve_policy_ctx(agent_id)
        if not policy_ctx:
            return fleet_gateway_v2_pb2.AckPreparedResponse(
                status=fleet_gateway_v2_pb2.AckPreparedResponse.REJECTED,
                reason="no policy assigned",
            )
        if normalize_string(request.policy_id) != normalize_string(policy_ctx["policy_revision"]):
            return fleet_gateway_v2_pb2.AckPreparedResponse(
                status=fleet_gateway_v2_pb2.AckPreparedResponse.REJECTED,
                reason="policy_id mismatch",
            )

        self._state.mark_preparation(preparation_id)
        return fleet_gateway_v2_pb2.AckPreparedResponse(
            status=fleet_gateway_v2_pb2.AckPreparedResponse.ACCEPTED,
            reason="prepared accepted",
        )

    def PublishReport(self, request, context):
        report = request.report
        agent_id = normalize_device_id(request.agent_id or report.agent_id)
        run_id = normalize_string(report.run_id)
        if not agent_id or not run_id:
            return fleet_gateway_v2_pb2.PublishReportResponse(
                status=fleet_gateway_v2_pb2.PublishReportResponse.REJECTED,
                reason="agent_id and report.run_id are required",
            )

        report_key = self._report_key(report, agent_id)
        if self._state.has_report(report_key):
            return fleet_gateway_v2_pb2.PublishReportResponse(
                status=fleet_gateway_v2_pb2.PublishReportResponse.DUPLICATE,
                reason="duplicate report ignored",
            )

        had_failure = False
        if len(report.events) == 0:
            synthetic = fleet_gateway_v2_pb2.Event(
                event_id=f"{run_id}:summary",
                run_id=run_id,
                experiment_id=report.experiment_id,
                policy_id=report.policy_id,
                agent_id=report.agent_id or agent_id,
                timestamp=report.finished_at if report.finished_at.seconds or report.finished_at.nanos else timestamp_from_unix_ms(int(time.time() * 1000)),
                type=fleet_gateway_v2_pb2.RUN_FINISHED,
                message=normalize_string(report.status),
                metrics={k: normalize_string(v) for k, v in report.summary_metrics.items()},
            )
            events = [synthetic]
        else:
            events = list(report.events)

        for ev in events:
            v1 = self._to_v1_event(
                ev,
                fallback_agent_id=agent_id,
                fallback_experiment_id=normalize_string(report.experiment_id),
                fallback_policy_id=normalize_string(report.policy_id),
                fallback_run_id=run_id,
            )
            ack = self._core._ingest_v1_event(v1)
            if not ack.ok:
                had_failure = True
                log.warning("PublishReport(v2) event ingest failed: %s", ack.message)

        for idx, artifact in enumerate(report.artifacts, start=1):
            ack = self._core._ingest_v1_event(self._artifact_event(run_id, report, agent_id, artifact, idx))
            if not ack.ok:
                had_failure = True
                log.warning("PublishReport(v2) artifact ingest failed: %s", ack.message)

        if had_failure:
            return fleet_gateway_v2_pb2.PublishReportResponse(
                status=fleet_gateway_v2_pb2.PublishReportResponse.REJECTED,
                reason="one or more events failed to ingest",
            )

        self._state.mark_report(report_key)
        self._record_report_metrics(report, agent_id)
        return fleet_gateway_v2_pb2.PublishReportResponse(
            status=fleet_gateway_v2_pb2.PublishReportResponse.ACCEPTED,
            reason="report accepted",
        )

    def PublishEvents(self, request, context):
        if not bool(self._cfg.get("allow_live_events", False)):
            acks = [
                fleet_gateway_v2_pb2.EventAck(
                    event_id=normalize_string(ev.event_id),
                    status=fleet_gateway_v2_pb2.EventAck.REJECTED,
                    reason="live events disabled",
                )
                for ev in request.events
            ]
            return fleet_gateway_v2_pb2.PublishEventsResponse(acks=acks)

        agent_id = normalize_device_id(request.agent_id)
        acks: list[fleet_gateway_v2_pb2.EventAck] = []
        for ev in request.events:
            v1 = self._to_v1_event(
                ev,
                fallback_agent_id=agent_id,
                fallback_experiment_id=normalize_string(ev.experiment_id),
                fallback_policy_id=normalize_string(ev.policy_id),
                fallback_run_id=normalize_string(ev.run_id),
            )
            ack = self._core._ingest_v1_event(v1)
            status = fleet_gateway_v2_pb2.EventAck.ACCEPTED if ack.ok else fleet_gateway_v2_pb2.EventAck.REJECTED
            if ack.ok and "duplicate" in normalize_string(ack.message).lower():
                status = fleet_gateway_v2_pb2.EventAck.DUPLICATE
            acks.append(
                fleet_gateway_v2_pb2.EventAck(
                    event_id=normalize_string(ev.event_id) or normalize_string(v1.event_id),
                    status=status,
                    reason=normalize_string(ack.message),
                )
            )
        return fleet_gateway_v2_pb2.PublishEventsResponse(acks=acks)


def serve():
    base_url = os.environ.get("ELAB_BASE_URL") or os.environ.get("ELAB_API_BASE_URL") or "https://web/api/v2"
    api_key = os.environ.get("ELAB_API_KEY")
    verify_env = normalize_string(os.environ.get("ELAB_VERIFY_TLS", "false")).lower()
    verify_tls = verify_env in {"1", "true", "yes"}

    cfg = {
        "fleet_experiment_tag": os.environ.get("FLEET_EXPERIMENT_TAG", "fleet"),
        "resource_tag_prefix": os.environ.get("RESOURCE_TAG_PREFIX", "device:"),
        "policy_metadata_keys": [
            normalize_string(key)
            for key in os.environ.get(
                "POLICY_METADATA_KEYS",
                ",".join(DEFAULT_POLICY_METADATA_KEYS),
            ).split(",")
            if normalize_string(key)
        ],
        "poll_interval_s": int(os.environ.get("HELLO_POLL_INTERVAL_S", "30")),
        "required_min_agent_version": os.environ.get("REQUIRED_MIN_AGENT_VERSION", ""),
        "experiments_batch_size": int(os.environ.get("EXPERIMENTS_BATCH_SIZE", "200")),
        "max_event_history": int(os.environ.get("MAX_EVENT_HISTORY", "100")),
        "event_duration_minutes": int(os.environ.get("EVENT_DURATION_MINUTES", "60")),
        "book_max_minutes": int(os.environ.get("BOOK_MAX_MINUTES", "180")),
        "book_can_overlap": normalize_string(os.environ.get("BOOK_CAN_OVERLAP", "true")).lower() in {"1", "true", "yes"},
        "book_is_cancellable": normalize_string(os.environ.get("BOOK_IS_CANCELLABLE", "true")).lower() in {"1", "true", "yes"},
        "max_dedupe_events": int(os.environ.get("MAX_DEDUPE_EVENTS", "50000")),
        "allow_live_events": normalize_string(os.environ.get("ALLOW_LIVE_EVENTS", "false")).lower() in {"1", "true", "yes"},
        "metrics_bind": os.environ.get("METRICS_BIND", "0.0.0.0"),
        "metrics_port": int(os.environ.get("METRICS_PORT", "9108")),
        "metrics_exclude_prefixes": tuple(
            safe_metric_token(part, "")
            for part in os.environ.get("METRICS_EXCLUDE_PREFIXES", "").split(",")
            if safe_metric_token(part, "")
        ),
        "ingest_api_token": os.environ.get("INGEST_API_TOKEN", ""),
        "artifact_max_bytes": int(os.environ.get("ARTIFACT_MAX_BYTES", str(20 * 1024 * 1024))),
    }

    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    global INGEST_JOURNAL_PATH, ARTIFACT_INGEST_JOURNAL_PATH
    INGEST_JOURNAL_PATH = data_dir / "ingest-metrics.ndjson"
    ARTIFACT_INGEST_JOURNAL_PATH = data_dir / "ingest-artifacts.ndjson"
    global METRICS_EXCLUDE_PREFIXES
    METRICS_EXCLUDE_PREFIXES = tuple(cfg.get("metrics_exclude_prefixes") or ())
    if METRICS_EXCLUDE_PREFIXES:
        log.info("Metrics export filter enabled, excluded prefixes: %s", ",".join(METRICS_EXCLUDE_PREFIXES))
    state = LocalState(data_dir / "state.json", max_event_ids=cfg["max_dedupe_events"])

    elab_client = ElabFTWClient(base_url=base_url, api_key=api_key, verify_tls=verify_tls)
    global ELAB_CLIENT_FOR_HTTP
    ELAB_CLIENT_FOR_HTTP = elab_client
    v1_servicer = FleetManagerServicer(elab_client, state, cfg)
    v2_servicer = FleetManagerServicerV2(v1_servicer, cfg)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    fleet_gateway_pb2_grpc.add_FleetManagerServicer_to_server(
        v1_servicer,
        server,
    )
    fleet_gateway_v2_pb2_grpc.add_FleetManagerServicer_to_server(
        v2_servicer,
        server,
    )

    start_http_sidecar(cfg)

    port = int(os.environ.get("GATEWAY_PORT", "50060"))
    listen_addr = f"[::]:{port}"
    server.add_insecure_port(listen_addr)

    log.info("Starting Monad Fleet service on %s", listen_addr)
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
