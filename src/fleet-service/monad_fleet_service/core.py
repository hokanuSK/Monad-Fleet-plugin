import base64
import hashlib
import io
import json
import logging
import os
import re
import tarfile
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
import fleet_gateway_v3_pb2 as fleet_gateway_v2_pb2
import fleet_gateway_v3_pb2_grpc as fleet_gateway_v2_pb2_grpc


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
DEFAULT_RESOURCE_STATUS_IDS_TEAM1 = {
    "maintenance mode": 1,
    "operational": 2,
    "in stock": 3,
    "need to reorder": 4,
    "destroyed": 5,
    "processed": 6,
    "waiting": 7,
    "open": 8,
    "closed": 9,
}
PROM_REGISTRY = CollectorRegistry()

REPORTS_TOTAL = Counter(
    "monad_fleet_reports_total",
    "Total v3 reports processed by status.",
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
ARTIFACT_SERVER_SPOOL_DIR: Path | None = None
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
    experiment_id: int,
    artifact_name: str,
    run_id: str,
    agent_id: str,
    upload_phase: str,
    sha256_value: str,
    size_bytes: int,
) -> str:
    original = normalize_string(artifact_name) or "artifact.bin"

    run_raw = re.sub(r"[^a-zA-Z0-9]", "", normalize_string(run_id))
    run_short = (run_raw[:8] if run_raw else "run")

    agent_raw = normalize_device_id(agent_id).lower()
    agent_token = re.sub(r"[^a-z0-9]+", "-", agent_raw).strip("-") or "agent"

    p = Path(original)
    lower_name = p.name.lower()
    compound_ext = ""
    for candidate in (".tar.gz", ".tar.bz2", ".tar.xz", ".jsonl", ".ndjson"):
        if lower_name.endswith(candidate):
            compound_ext = p.name[-len(candidate) :]
            break
    ext = compound_ext or (p.suffix if p.suffix and len(p.suffix) <= 12 else "")
    stem = p.name[: -len(ext)] if ext else p.name
    stem_safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip("-_.") or "artifact"
    stem_kind = stem_safe.lower()
    artifact_kind = {
        "run-summary": "summary",
        "wireless-run-evidence-bundle": "wireless-evidence",
    }.get(stem_kind, stem_safe)

    sha_token = normalize_string(sha256_value).lower()
    content_tag = f"sha-{sha_token[:8]}" if sha_token else f"sz-{max(0, int(size_bytes))}"

    base = f"exp-{int(experiment_id):04d}__dev-{agent_token}__run-{run_short}__{artifact_kind}__{content_tag}"
    max_base_len = 180 - len(ext)
    if len(base) > max_base_len:
        base = base[:max_base_len].rstrip("-_.")
    return (base or "artifact") + ext


def safe_artifact_filename(value: Any, default: str = "artifact.bin") -> str:
    raw = Path(normalize_string(value) or default).name or default
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw).strip("-_.")
    if not safe:
        safe = default
    return safe[:180].rstrip("-_.") or default


def artifact_spool_root(experiment_id: int, run_id: str, agent_id: str) -> Path:
    root = ARTIFACT_SERVER_SPOOL_DIR
    if root is None:
        if ARTIFACT_INGEST_JOURNAL_PATH is not None:
            root = ARTIFACT_INGEST_JOURNAL_PATH.parent / "artifact-spool"
        else:
            root = Path("/tmp/monad-fleet-artifact-spool")
    run_token = re.sub(r"[^a-zA-Z0-9._-]+", "-", normalize_string(run_id)).strip("-_.") or "run"
    agent_token = re.sub(r"[^a-zA-Z0-9._-]+", "-", normalize_device_id(agent_id)).strip("-_.") or "agent"
    return root / f"experiment-{int(experiment_id)}" / run_token / agent_token


def server_spool_uri(experiment_id: int, run_id: str, agent_id: str, stored_name: str) -> str:
    run_token = re.sub(r"[^a-zA-Z0-9._-]+", "-", normalize_string(run_id)).strip("-_.") or "run"
    agent_token = re.sub(r"[^a-zA-Z0-9._-]+", "-", normalize_device_id(agent_id)).strip("-_.") or "agent"
    return (
        f"fleet-spool://experiments/{int(experiment_id)}/runs/{run_token}/"
        f"agents/{agent_token}/artifacts/{safe_artifact_filename(stored_name)}"
    )


def artifact_description(name: str) -> str:
    stem = Path(normalize_string(name)).stem.lower()
    if stem == "run-summary":
        return "Machine-readable summary of run status, command counts, and aggregate metrics."
    if stem.startswith("command-") and stem.endswith("-execution-status"):
        return "Command wrapper log with command id, command line, exit code, duration, and short message."
    if stem.startswith("wifi-link-status"):
        return "Wi-Fi link snapshot from iw, including SSID, channel/frequency, RSSI, and bitrate when available."
    if stem.startswith("wifi-access-point-scan"):
        return "Wi-Fi access point scan output from iw."
    if stem.startswith("wifi-proc-wireless-status"):
        return "Fallback Wi-Fi status from /proc/net/wireless."
    if stem.startswith("wifi-diagnostics-debug"):
        return "Wi-Fi diagnostic output captured when scan/link evidence was missing."
    if stem.startswith("ble-discovery-raw-bluetoothctl-scan"):
        return "Raw bluetoothctl discovery output captured during the BLE scan window."
    if stem.startswith("csi-status-disabled"):
        return "CSI status note showing capture was disabled by configuration."
    if stem.startswith("csi-status-not-configured"):
        return "CSI status note showing no collector command or output path was configured."
    if stem.startswith("csi-collector-output"):
        return "CSI collector stdout/stderr and timeout/evidence notes."
    if stem.startswith("csi-capture-raw-data-file"):
        return "Raw CSI capture output file imported from the collector."
    if stem.startswith("device-rf-environment-snapshot"):
        return "Device RF environment snapshot with kernel, Wi-Fi interface, link, wireless, and Bluetooth state."
    if stem.startswith("command-") and stem.endswith("-raw-output"):
        return "Raw stdout/stderr from a shell command artifact."
    return "Run evidence artifact captured by the device agent."


def file_sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def store_server_spooled_artifact(
    *,
    experiment_id: int,
    run_id: str,
    agent_id: str,
    artifact_name: str,
    raw: bytes,
    mime: str,
    comment: str,
    upload_phase: str,
    sha256_value: str,
) -> dict[str, Any]:
    root = artifact_spool_root(experiment_id, run_id, agent_id)
    root.mkdir(parents=True, exist_ok=True)

    actual_sha = file_sha256_bytes(raw)
    sha = normalize_string(sha256_value).lower() or actual_sha
    stored_name = safe_artifact_filename(artifact_name)
    candidate = root / stored_name
    if candidate.exists():
        try:
            existing_sha = file_sha256_bytes(candidate.read_bytes())
        except Exception:
            existing_sha = ""
        if existing_sha != actual_sha:
            p = Path(stored_name)
            suffix = p.suffix
            stem = p.name[: -len(suffix)] if suffix else p.name
            stored_name = safe_artifact_filename(f"{stem}__{actual_sha[:8]}{suffix}")
            candidate = root / stored_name

    tmp = candidate.with_name(candidate.name + ".tmp")
    tmp.write_bytes(raw)
    tmp.replace(candidate)

    meta = {
        "received_at": utc_now_iso(),
        "experiment_id": int(experiment_id),
        "run_id": normalize_string(run_id),
        "agent_id": normalize_device_id(agent_id),
        "artifact_name": normalize_string(artifact_name),
        "stored_spool_name": stored_name,
        "upload_phase": normalize_string(upload_phase) or "measure_live",
        "mime": normalize_string(mime) or "application/octet-stream",
        "comment": normalize_string(comment),
        "sha256": sha,
        "actual_sha256": actual_sha,
        "size_bytes": len(raw),
        "server_spool_uri": server_spool_uri(experiment_id, run_id, agent_id, stored_name),
    }
    (root / f"{stored_name}.meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2), encoding="utf-8")
    append_artifact_journal({**meta, "status": "spooled"})
    return meta


def load_server_spooled_artifacts(experiment_id: int, run_id: str, agent_id: str) -> list[dict[str, Any]]:
    root = artifact_spool_root(experiment_id, run_id, agent_id)
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for meta_path in sorted(root.glob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        stored_name = safe_artifact_filename(meta.get("stored_spool_name"))
        data_path = root / stored_name
        if not data_path.exists() or not data_path.is_file():
            continue
        meta["path"] = str(data_path)
        rows.append(meta)
    return rows


def upload_raw_artifact_to_elab(
    *,
    experiment_id: int,
    run_id: str,
    agent_id: str,
    artifact_name: str,
    raw: bytes,
    mime: str,
    comment: str,
    upload_phase: str,
) -> dict[str, Any]:
    if ELAB_CLIENT_FOR_HTTP is None:
        raise RuntimeError("elab client unavailable")

    sha256_value = file_sha256_bytes(raw)
    existing = find_artifact_upload_in_journal(
        experiment_id=experiment_id,
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
            existing_artifact_uri = f"elabftw://experiments/{experiment_id}/uploads/{existing_upload_id}"
        elif existing_location:
            existing_artifact_uri = existing_location
        return {
            "deduplicated": True,
            "upload_id": existing_upload_id,
            "artifact_uri": existing_artifact_uri,
            "location": existing_location,
            "stored_artifact_name": normalize_string(existing.get("stored_artifact_name")) or artifact_name,
            "sha256": sha256_value,
            "size_bytes": len(raw),
        }

    upload_filename = build_uploaded_artifact_name(
        experiment_id=experiment_id,
        artifact_name=artifact_name,
        run_id=run_id,
        agent_id=agent_id,
        upload_phase=upload_phase,
        sha256_value=sha256_value,
        size_bytes=len(raw),
    )
    uploaded = ELAB_CLIENT_FOR_HTTP.upload_experiment_artifact(
        experiment_id,
        upload_filename,
        raw,
        comment=comment,
        mime=mime,
    )
    location = normalize_string(uploaded.get("location"))
    upload_id = uploaded.get("upload_id")
    artifact_uri = ""
    if isinstance(upload_id, int):
        artifact_uri = f"elabftw://experiments/{experiment_id}/uploads/{upload_id}"
    elif location:
        artifact_uri = location

    append_artifact_journal(
        {
            "received_at": utc_now_iso(),
            "experiment_id": experiment_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "artifact_name": artifact_name,
            "stored_artifact_name": upload_filename,
            "upload_phase": upload_phase,
            "sha256": sha256_value,
            "size_bytes": len(raw),
            "upload_id": upload_id,
            "location": location,
            "status": "uploaded",
        }
    )
    return {
        "deduplicated": False,
        "upload_id": upload_id,
        "artifact_uri": artifact_uri,
        "location": location,
        "stored_artifact_name": upload_filename,
        "sha256": sha256_value,
        "size_bytes": len(raw),
    }


def build_server_artifact_bundle(run_id: str, rows: list[dict[str, Any]]) -> bytes:
    manifest_entries = []
    payloads: list[tuple[dict[str, Any], bytes]] = []
    for row in rows:
        path = Path(normalize_string(row.get("path")))
        if not path.exists() or not path.is_file():
            continue
        raw = path.read_bytes()
        name = normalize_string(row.get("artifact_name")) or path.name
        stored_name = safe_artifact_filename(row.get("stored_spool_name") or name)
        manifest_entries.append(
            {
                "name": name,
                "stored_name": stored_name,
                "sha256": normalize_string(row.get("actual_sha256") or row.get("sha256")) or file_sha256_bytes(raw),
                "size_bytes": len(raw),
                "description": artifact_description(name),
            }
        )
        payloads.append(({"stored_name": stored_name}, raw))

    manifest = {
        "schema": "fleet.v2.artifact_bundle.v2",
        "run_id": normalize_string(run_id),
        "created_at": utc_now_iso(),
        "format": "tar.gz",
        "entries_dir": "entries/",
        "entries": manifest_entries,
    }

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for row, raw in payloads:
            info = tarfile.TarInfo(name=f"entries/{safe_artifact_filename(row.get('stored_name'))}")
            info.size = len(raw)
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(raw))
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        info.mtime = int(time.time())
        archive.addfile(info, io.BytesIO(manifest_bytes))
    return buffer.getvalue()


def finalize_server_spooled_run(
    *,
    experiment_id: int,
    run_id: str,
    agent_id: str,
    summary_raw: bytes,
    summary_mime: str,
    summary_comment: str,
) -> dict[str, Any]:
    rows = load_server_spooled_artifacts(experiment_id, run_id, agent_id)
    text_suffixes = {".txt", ".log", ".json"}
    text_rows: list[dict[str, Any]] = []
    raw_rows: list[dict[str, Any]] = []
    for row in rows:
        name = normalize_string(row.get("artifact_name"))
        if name == "run-summary.json":
            continue
        suffix = Path(name).suffix.lower()
        if suffix in text_suffixes:
            text_rows.append(row)
        else:
            raw_rows.append(row)

    uploads: list[dict[str, Any]] = []
    if text_rows:
        bundle_raw = build_server_artifact_bundle(run_id, text_rows)
        bundle_upload = upload_raw_artifact_to_elab(
            experiment_id=experiment_id,
            run_id=run_id,
            agent_id=agent_id,
            artifact_name="wireless-run-evidence-bundle.tar.gz",
            raw=bundle_raw,
            mime="application/gzip",
            comment=f"fleet-v3 run={run_id} agent={agent_id} artifact=wireless-run-evidence-bundle.tar.gz",
            upload_phase="report_replay",
        )
        uploads.append({"artifact_name": "wireless-run-evidence-bundle.tar.gz", **bundle_upload})

    for row in raw_rows:
        path = Path(normalize_string(row.get("path")))
        raw = path.read_bytes()
        name = normalize_string(row.get("artifact_name")) or path.name
        upload = upload_raw_artifact_to_elab(
            experiment_id=experiment_id,
            run_id=run_id,
            agent_id=agent_id,
            artifact_name=name,
            raw=raw,
            mime=normalize_string(row.get("mime")) or "application/octet-stream",
            comment=normalize_string(row.get("comment"))
            or f"fleet-v3 run={run_id} agent={agent_id} artifact={name}",
            upload_phase="report_replay",
        )
        uploads.append({"artifact_name": name, **upload})

    summary_upload = upload_raw_artifact_to_elab(
        experiment_id=experiment_id,
        run_id=run_id,
        agent_id=agent_id,
        artifact_name="run-summary.json",
        raw=summary_raw,
        mime=summary_mime or "application/json",
        comment=summary_comment or f"fleet-v3 run={run_id} agent={agent_id} artifact=run-summary.json",
        upload_phase="report_replay",
    )
    append_artifact_journal(
        {
            "received_at": utc_now_iso(),
            "experiment_id": experiment_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "artifact_name": "run-summary.json",
            "status": "server_finalized",
            "bundled_artifacts_count": len(text_rows),
            "raw_artifacts_count": len(raw_rows),
            "uploads_count": len(uploads) + 1,
        }
    )
    return {
        "summary_upload": summary_upload,
        "uploads": uploads,
        "bundled_artifacts_count": len(text_rows),
        "raw_artifacts_count": len(raw_rows),
    }


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


def ensure_utc_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
        method_upper = normalize_string(method).upper()
        normalized_path = normalize_string(path)
        path_no_query = normalized_path.split("?", 1)[0]
        if method_upper == "PATCH" and (
            path_no_query == "/experiments" or path_no_query.startswith("/experiments/")
        ):
            raise RuntimeError(
                "Fleet service is forbidden to PATCH /experiments metadata; use item/event metadata instead"
            )

        url = f"{self._base_url}{path}"
        headers = kwargs.pop("headers", {}) or {}
        if self._api_key:
            headers["Authorization"] = self._api_key

        request_json = kwargs.get("json")
        response = requests.request(
            method_upper,
            url,
            headers=headers,
            timeout=20,
            verify=self._verify_tls,
            **kwargs,
        )

        # Compatibility fallback: some eLabFTW builds reject object metadata in
        # PATCH/POST and expect metadata as a JSON string.
        if not response.ok and method_upper in {"PATCH", "POST", "PUT"}:
            coerced_json = self._coerce_metadata_payload(request_json)
            if isinstance(coerced_json, dict) and coerced_json is not request_json:
                retry_kwargs = dict(kwargs)
                retry_kwargs["json"] = coerced_json
                retry = requests.request(
                    method_upper,
                    url,
                    headers=headers,
                    timeout=20,
                    verify=self._verify_tls,
                    **retry_kwargs,
                )
                if retry.ok:
                    log.warning(
                        "Recovered %s %s by serializing metadata payload for eLabFTW compatibility",
                        method_upper,
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

        if Path(artifact_name).name == "run-summary.json":
            try:
                finalized = finalize_server_spooled_run(
                    experiment_id=exp_id,
                    run_id=run_id,
                    agent_id=agent_id,
                    summary_raw=raw,
                    summary_mime=mime,
                    summary_comment=comment,
                )
            except Exception as exc:
                ARTIFACT_UPLOADS_TOTAL.labels(status="error").inc()
                self._write_json(502, {"ok": False, "error": f"server finalize failed: {exc}"})
                return

            summary_upload = finalized.get("summary_upload") if isinstance(finalized.get("summary_upload"), dict) else {}
            ARTIFACT_UPLOADS_TOTAL.labels(status="uploaded").inc()
            self._write_json(
                202,
                {
                    "ok": True,
                    "experiment_id": exp_id,
                    "upload_id": summary_upload.get("upload_id"),
                    "artifact_uri": normalize_string(summary_upload.get("artifact_uri")),
                    "location": normalize_string(summary_upload.get("location")),
                    "stored_artifact_name": normalize_string(summary_upload.get("stored_artifact_name")),
                    "server_finalized": True,
                    "bundled_artifacts_count": int(finalized.get("bundled_artifacts_count", 0) or 0),
                    "raw_artifacts_count": int(finalized.get("raw_artifacts_count", 0) or 0),
                    "finalized_uploads": finalized.get("uploads", []),
                },
            )
            return

        # Non-summary artifacts are staged in Fleet for both in-measurement and
        # report-replay transfers. The summary artifact is the run-finalization
        # signal that creates the coarse eLabFTW bundle.
        meta = store_server_spooled_artifact(
            experiment_id=exp_id,
            run_id=run_id,
            agent_id=agent_id,
            artifact_name=artifact_name,
            raw=raw,
            mime=mime,
            comment=comment,
            upload_phase=upload_phase,
            sha256_value=sha256_value,
        )
        ARTIFACT_UPLOADS_TOTAL.labels(status="spooled").inc()
        self._write_json(
            202,
            {
                "ok": True,
                "spooled": True,
                "experiment_id": exp_id,
                "artifact_uri": normalize_string(meta.get("server_spool_uri")),
                "location": normalize_string(meta.get("server_spool_uri")),
                "stored_artifact_name": normalize_string(meta.get("stored_spool_name")),
                "sha256": normalize_string(meta.get("actual_sha256") or meta.get("sha256")),
                "size_bytes": int(meta.get("size_bytes", len(raw)) or len(raw)),
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
