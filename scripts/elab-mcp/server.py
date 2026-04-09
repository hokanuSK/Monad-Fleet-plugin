#!/usr/bin/env python3
"""Read-only MCP server for eLabFTW policy/artifact diagnostics in FleetManager."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("elab-mcp")


DEFAULT_POLICY_METADATA_KEYS = (
    "fleet.policy",
    "policy",
    "fleet_policy",
    "policy_json",
)


def normalize_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_device_id(value: Any) -> str:
    return normalize_string(value).lower()


def stable_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parse_bool(value: Any, default: bool = False) -> bool:
    text = normalize_string(value).lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


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


def read_metadata_value(metadata: dict[str, Any], dotted_key: str) -> Any:
    current: Any = metadata
    for token in normalize_string(dotted_key).split("."):
        if not token:
            continue
        if not isinstance(current, dict) or token not in current:
            return None
        current = current[token]
    return current


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


def parse_iso_to_unix(value: Any) -> float:
    text = normalize_string(value)
    if not text:
        return 0.0
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


@dataclass
class ElabConfig:
    base_url: str
    base_url_fallbacks: list[str]
    api_key: str
    verify_tls: bool
    fleet_experiment_tag: str
    resource_tag_prefix: str
    policy_metadata_keys: list[str]
    experiments_batch_size: int
    request_timeout_s: int
    artifact_journal_path: Path
    enable_write: bool

    @classmethod
    def from_env_and_repo(cls, repo_root: Path) -> "ElabConfig":
        discovered = discover_compose_env(repo_root / "docker-compose.yml")

        base_url = (
            normalize_string(os.environ.get("ELAB_BASE_URL"))
            or normalize_string(os.environ.get("ELAB_API_BASE_URL"))
            or normalize_string(discovered.get("ELAB_BASE_URL"))
            or "https://web/api/v2"
        )
        parsed_base = urlparse.urlparse(base_url)
        fallback_urls: list[str] = []

        explicit_fallbacks = normalize_string(os.environ.get("ELAB_MCP_BASE_URL_FALLBACKS"))
        if explicit_fallbacks:
            for row in explicit_fallbacks.split(","):
                candidate = normalize_string(row).rstrip("/")
                if candidate and candidate not in fallback_urls and candidate != base_url.rstrip("/"):
                    fallback_urls.append(candidate)

        if parsed_base.hostname in {"web", "elabftw"}:
            for netloc in ("localhost:8443", "127.0.0.1:8443"):
                candidate = parsed_base._replace(netloc=netloc).geturl().rstrip("/")
                if candidate and candidate not in fallback_urls and candidate != base_url.rstrip("/"):
                    fallback_urls.append(candidate)

        api_key = normalize_string(os.environ.get("ELAB_API_KEY")) or normalize_string(discovered.get("ELAB_API_KEY"))
        verify_tls = parse_bool(
            os.environ.get("ELAB_VERIFY_TLS", discovered.get("ELAB_VERIFY_TLS", "false")),
            default=False,
        )
        fleet_experiment_tag = normalize_string(
            os.environ.get("FLEET_EXPERIMENT_TAG", discovered.get("FLEET_EXPERIMENT_TAG", "fleet"))
        ) or "fleet"
        resource_tag_prefix = normalize_string(
            os.environ.get("RESOURCE_TAG_PREFIX", discovered.get("RESOURCE_TAG_PREFIX", "device:"))
        ) or "device:"

        keys_env = normalize_string(os.environ.get("POLICY_METADATA_KEYS", discovered.get("POLICY_METADATA_KEYS", "")))
        if keys_env:
            policy_metadata_keys = [normalize_string(x) for x in keys_env.split(",") if normalize_string(x)]
        else:
            policy_metadata_keys = list(DEFAULT_POLICY_METADATA_KEYS)

        experiments_batch_size = max(1, int(normalize_string(os.environ.get("EXPERIMENTS_BATCH_SIZE", "200")) or "200"))
        request_timeout_s = max(1, int(normalize_string(os.environ.get("ELAB_MCP_TIMEOUT_S", "20")) or "20"))
        enable_write = parse_bool(os.environ.get("ELAB_MCP_ENABLE_WRITE", "false"), default=False)

        explicit_journal = normalize_string(os.environ.get("ELAB_MCP_ARTIFACT_JOURNAL_PATH"))
        if explicit_journal:
            journal_path = Path(explicit_journal).expanduser().resolve()
        else:
            preferred = repo_root / "data" / "fleet" / "ingest-artifacts.ndjson"
            fallback = repo_root / "data" / "ingest-artifacts.ndjson"
            journal_path = preferred if preferred.exists() else fallback

        return cls(
            base_url=base_url.rstrip("/"),
            base_url_fallbacks=fallback_urls,
            api_key=api_key,
            verify_tls=verify_tls,
            fleet_experiment_tag=fleet_experiment_tag,
            resource_tag_prefix=resource_tag_prefix,
            policy_metadata_keys=policy_metadata_keys,
            experiments_batch_size=experiments_batch_size,
            request_timeout_s=request_timeout_s,
            artifact_journal_path=journal_path,
            enable_write=enable_write,
        )


def discover_compose_env(compose_path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not compose_path.exists():
        return result

    try:
        lines = compose_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return result

    targets = {
        "ELAB_BASE_URL",
        "ELAB_API_KEY",
        "ELAB_VERIFY_TLS",
        "FLEET_EXPERIMENT_TAG",
        "RESOURCE_TAG_PREFIX",
        "POLICY_METADATA_KEYS",
    }

    for line in lines:
        stripped = line.strip()
        if "=" not in stripped:
            continue

        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        stripped = stripped.strip("\"'")
        if "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = normalize_string(key)
        value = normalize_string(value).strip("\"'")
        if key in targets and key not in result:
            result[key] = value

    return result


class ElabHttpClient:
    def __init__(self, cfg: ElabConfig):
        self._cfg = cfg

    def _build_url(self, base_url: str, path: str, params: dict[str, Any] | None = None) -> str:
        qs = ""
        if params:
            clean = {}
            for key, value in params.items():
                if value is None:
                    continue
                clean[key] = value
            if clean:
                qs = "?" + urlparse.urlencode(clean, doseq=True)
        return f"{base_url}{path}{qs}"

    def _candidate_base_urls(self) -> list[str]:
        rows = [self._cfg.base_url, *self._cfg.base_url_fallbacks]
        seen: set[str] = set()
        result: list[str] = []
        for row in rows:
            token = normalize_string(row).rstrip("/")
            if not token or token in seen:
                continue
            seen.add(token)
            result.append(token)
        return result

    def _request_once(
        self,
        *,
        base_url: str,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        url = self._build_url(base_url, path, params=params)
        headers = {"Accept": "application/json"}
        if self._cfg.api_key:
            headers["Authorization"] = self._cfg.api_key

        data = None
        if json_body is not None:
            data = stable_dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urlrequest.Request(url=url, method=method.upper(), headers=headers, data=data)
        context: ssl.SSLContext | None
        if self._cfg.verify_tls:
            context = ssl.create_default_context()
        else:
            context = ssl._create_unverified_context()  # noqa: SLF001 - required for local self-signed eLab

        try:
            with urlrequest.urlopen(req, timeout=self._cfg.request_timeout_s, context=context) as response:
                raw = response.read().decode("utf-8", errors="replace")
                response_headers = {k.lower(): v for k, v in response.headers.items()}
        except urlerror.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"HTTP {exc.code} for {path}: {body}") from exc
        except Exception as exc:
            raise RuntimeError(f"request failed for {path}: {exc}") from exc

        if not normalize_string(raw):
            return None, response_headers
        try:
            return json.loads(raw), response_headers
        except Exception as exc:
            raise RuntimeError(f"invalid JSON response for {path}: {exc}") from exc

    def request_json_with_meta(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str], str]:
        candidates = self._candidate_base_urls()
        last_error: Exception | None = None

        for base_url in candidates:
            try:
                payload, headers = self._request_once(
                    base_url=base_url,
                    method=method,
                    path=path,
                    params=params,
                    json_body=json_body,
                )
                return payload, headers, base_url
            except RuntimeError as exc:
                last_error = exc
                if str(exc).startswith("HTTP "):
                    raise
                continue

        if last_error is None:
            raise RuntimeError(f"request failed for {path}: no candidate base URL")
        raise RuntimeError(f"request failed for {path}: {last_error}")

    def request_json(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        payload, _, _ = self.request_json_with_meta(method=method, path=path, params=params, json_body=json_body)
        return payload


class ElabDiagnostics:
    def __init__(self, cfg: ElabConfig):
        self._cfg = cfg
        self._http = ElabHttpClient(cfg)

    def _parse_policy_dict(self, raw_value: Any) -> dict[str, Any] | None:
        candidate = parse_maybe_json(raw_value, None)
        if not isinstance(candidate, dict):
            return None

        if isinstance(candidate.get("policy"), dict):
            candidate = candidate["policy"]
        elif "value" in candidate:
            nested = parse_maybe_json(candidate.get("value"), None)
            if isinstance(nested, dict):
                candidate = nested

        if not isinstance(candidate, dict):
            return None
        return json.loads(json.dumps(candidate))

    def _extract_policy_from_experiment(self, experiment: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        metadata = parse_maybe_json(experiment.get("metadata"), {})
        if not isinstance(metadata, dict):
            metadata = {}

        for key in self._cfg.policy_metadata_keys:
            raw_value = read_metadata_value(metadata, key)
            policy = self._parse_policy_dict(raw_value)
            if policy is not None:
                return policy, key

        if isinstance(metadata.get("command_groups"), list):
            policy = {"command_groups": metadata.get("command_groups")}
            for optional_key in ("target_selector", "range", "notes"):
                if optional_key in metadata:
                    policy[optional_key] = metadata[optional_key]
            return policy, "metadata_root"

        return None, ""

    def _policy_summary(self, policy: dict[str, Any]) -> dict[str, Any]:
        groups = policy.get("command_groups") if isinstance(policy, dict) else []
        if not isinstance(groups, list):
            groups = []

        command_types: dict[str, int] = {}
        command_count = 0
        for group in groups:
            if not isinstance(group, dict):
                continue
            commands = group.get("commands")
            rows: list[dict[str, Any]] = []
            if isinstance(commands, list):
                rows = [row for row in commands if isinstance(row, dict)]
            elif isinstance(commands, dict):
                rows = [row for row in commands.values() if isinstance(row, dict)]
            for cmd in rows:
                command_count += 1
                cmd_type = normalize_string(cmd.get("type") or cmd.get("command_type") or "SHELL").upper()
                command_types[cmd_type] = command_types.get(cmd_type, 0) + 1

        range_obj = policy.get("range") if isinstance(policy.get("range"), dict) else {}
        range_from = normalize_string(range_obj.get("from"))
        range_to = normalize_string(range_obj.get("to"))

        return {
            "command_groups_total": len(groups),
            "commands_total": command_count,
            "command_types": command_types,
            "range_from": range_from,
            "range_to": range_to,
        }

    def _canonical_policy(self, policy_raw: dict[str, Any], experiment_id: int) -> tuple[dict[str, Any], str]:
        canonical_payload = json.loads(json.dumps(policy_raw))
        canonical_payload["experiment_id"] = f"elabftw:{experiment_id}"
        canonical_payload.pop("policy_revision", None)

        canonical_without_revision = stable_dumps(canonical_payload)
        policy_revision = f"sha256:{hashlib.sha256(canonical_without_revision.encode('utf-8')).hexdigest()}"
        canonical_payload["policy_revision"] = policy_revision
        return canonical_payload, policy_revision

    def list_items(self, *, search: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "type": "resources",
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
        }
        if normalize_string(search):
            params["search"] = normalize_string(search)
        data = self._http.request_json("GET", "/items", params=params)
        return data if isinstance(data, list) else []

    def list_experiments_by_tag(self, tag: str, *, limit: int, offset: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
        }
        if normalize_string(tag):
            params["tags[]"] = [normalize_string(tag)]
        data = self._http.request_json("GET", "/experiments", params=params)
        return data if isinstance(data, list) else []

    def get_experiment(self, experiment_id: int) -> dict[str, Any]:
        data = self._http.request_json("GET", f"/experiments/{int(experiment_id)}")
        if not isinstance(data, dict):
            raise RuntimeError(f"unexpected experiment payload for id={experiment_id}")
        return data

    def get_experiment_uploads(self, experiment_id: int) -> list[dict[str, Any]]:
        data = self._http.request_json("GET", f"/experiments/{int(experiment_id)}/uploads")
        return data if isinstance(data, list) else []

    def _find_device_item(self, device_id: str) -> dict[str, Any] | None:
        candidates = self.list_items(search=device_id, limit=100, offset=0)
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

    def _item_matches_experiment_tags(self, item: dict[str, Any], experiment: dict[str, Any]) -> bool:
        prefix = self._cfg.resource_tag_prefix
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

    def _fetch_fleet_experiments(self) -> list[dict[str, Any]]:
        limit = self._cfg.experiments_batch_size
        offset = 0
        rows: list[dict[str, Any]] = []
        while True:
            batch = self.list_experiments_by_tag(self._cfg.fleet_experiment_tag, limit=limit, offset=offset)
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < limit:
                break
            offset += len(batch)
        return rows

    def _select_policy_for_device(self, item: dict[str, Any], device_id: str) -> dict[str, Any] | None:
        experiments = self._fetch_fleet_experiments()
        candidates: list[dict[str, Any]] = []

        for experiment_ref in experiments:
            exp_id = experiment_ref.get("id")
            if exp_id is None:
                continue
            try:
                experiment = self.get_experiment(int(exp_id))
            except Exception:
                log.exception("failed to fetch experiment id=%s", exp_id)
                continue

            if not self._item_matches_experiment_tags(item, experiment):
                continue

            policy_raw, source_key = self._extract_policy_from_experiment(experiment)
            if not policy_raw:
                continue

            if not self._item_matches_policy_selector(item, policy_raw, device_id):
                continue

            canonical_policy, policy_revision = self._canonical_policy(policy_raw, int(exp_id))
            modified_at = normalize_string(experiment.get("modified_at") or experiment_ref.get("modified_at"))
            candidates.append(
                {
                    "modified_at": modified_at,
                    "sort_key": parse_iso_to_unix(modified_at),
                    "experiment": experiment,
                    "source_key": source_key,
                    "policy": canonical_policy,
                    "policy_revision": policy_revision,
                }
            )

        if not candidates:
            return None

        candidates.sort(key=lambda row: (row.get("sort_key", 0.0), normalize_string(row.get("modified_at"))), reverse=True)
        return candidates[0]

    def _classify_upload(self, upload: dict[str, Any]) -> str:
        name = normalize_string(upload.get("real_name") or upload.get("filename")).lower()
        comment = normalize_string(upload.get("comment")).lower()

        if ("run-summary" in name) or ("artifact=run-summary" in comment):
            return "run_summary"
        if ("wifi" in name) or ("artifact=wifi" in comment):
            return "wifi"
        if ("ble" in name) or ("artifact=ble" in comment):
            return "ble"
        if ("csi" in name) or ("artifact=csi" in comment):
            return "csi"
        return "other"

    def _uploads_by_class(self, uploads: list[dict[str, Any]]) -> dict[str, int]:
        result = {"wifi": 0, "ble": 0, "csi": 0, "run_summary": 0, "other": 0}
        for upload in uploads:
            cls = self._classify_upload(upload)
            result[cls] = result.get(cls, 0) + 1
        return result

    def _read_journal_entries(self) -> list[dict[str, Any]]:
        path = self._cfg.artifact_journal_path
        if not path.exists():
            return []
        try:
            rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return []

        entries: list[dict[str, Any]] = []
        for raw in rows:
            if not normalize_string(raw):
                continue
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
        return entries

    def _journal_matches(self, experiment_id: int, run_id: str, agent_id: str = "") -> dict[str, Any]:
        run_token = normalize_string(run_id)
        agent_token = normalize_device_id(agent_id)
        if not run_token:
            return {"matched": False, "entries_total": 0, "by_class": {}}

        rows = self._read_journal_entries()
        matched: list[dict[str, Any]] = []
        for row in rows:
            if int(row.get("experiment_id", -1) or -1) != int(experiment_id):
                continue
            if normalize_string(row.get("run_id")) != run_token:
                continue
            if agent_token and normalize_device_id(row.get("agent_id")) not in {"", agent_token}:
                continue
            matched.append(row)

        by_class = {"wifi": 0, "ble": 0, "csi": 0, "run_summary": 0, "other": 0}
        for row in matched:
            artifact_name = normalize_string(row.get("artifact_name"))
            cls = self._classify_upload({"real_name": artifact_name, "comment": ""})
            by_class[cls] = by_class.get(cls, 0) + 1

        return {
            "matched": len(matched) > 0,
            "entries_total": len(matched),
            "by_class": by_class,
            "journal_path": str(self._cfg.artifact_journal_path),
        }

    def get_experiment_summary(self, experiment_id: int) -> dict[str, Any]:
        experiment = self.get_experiment(experiment_id)
        return {
            "id": experiment.get("id"),
            "title": experiment.get("title"),
            "status": experiment.get("status"),
            "tags": parse_tags(experiment.get("tags")),
            "modified_at": experiment.get("modified_at"),
            "created_at": experiment.get("created_at"),
            "has_metadata": bool(normalize_string(experiment.get("metadata"))),
        }

    def get_experiment_metadata(self, experiment_id: int) -> dict[str, Any]:
        experiment = self.get_experiment(experiment_id)
        metadata = parse_maybe_json(experiment.get("metadata"), {})
        if not isinstance(metadata, dict):
            metadata = {}

        policy_raw, source_key = self._extract_policy_from_experiment(experiment)
        policy_summary = {}
        policy_revision = ""
        canonical_policy: dict[str, Any] | None = None

        if policy_raw:
            canonical_policy, policy_revision = self._canonical_policy(policy_raw, int(experiment_id))
            policy_summary = self._policy_summary(canonical_policy)

        return {
            "experiment_id": int(experiment_id),
            "policy_found": policy_raw is not None,
            "policy_source_key": source_key,
            "policy_revision_sha256": policy_revision,
            "policy_summary": policy_summary,
            "metadata": metadata,
            "canonical_policy": canonical_policy,
            "metadata_keys_available": self._cfg.policy_metadata_keys,
        }

    def get_experiment_uploads_summary(self, experiment_id: int, limit: int = 15) -> dict[str, Any]:
        uploads = self.get_experiment_uploads(experiment_id)
        rows = sorted(
            uploads,
            key=lambda row: parse_iso_to_unix(row.get("created_at") or row.get("updated_at") or ""),
            reverse=True,
        )
        by_class = self._uploads_by_class(rows)
        latest = []
        for row in rows[: max(1, int(limit))]:
            latest.append(
                {
                    "id": row.get("id"),
                    "real_name": row.get("real_name"),
                    "comment": normalize_string(row.get("comment")),
                    "created_at": row.get("created_at"),
                    "size": row.get("filesize"),
                    "classification": self._classify_upload(row),
                }
            )
        return {
            "experiment_id": int(experiment_id),
            "uploads_total": len(rows),
            "by_class": by_class,
            "latest_uploads": latest,
        }

    def get_latest_policy(self, device_id: str) -> dict[str, Any]:
        token = normalize_device_id(device_id)
        if not token:
            raise ValueError("device_id is required")

        item = self._find_device_item(token)
        if not item:
            return {
                "device_id": token,
                "matched": False,
                "reason_if_none": "device item not found in eLab resources",
            }

        selected = self._select_policy_for_device(item, token)
        if not selected:
            return {
                "device_id": token,
                "matched": False,
                "reason_if_none": "no matching fleet policy for this device",
                "item_id": item.get("id"),
            }

        experiment = selected["experiment"]
        return {
            "device_id": token,
            "matched": True,
            "item_id": item.get("id"),
            "matched_experiment_id": experiment.get("id"),
            "matched_experiment_uri": f"elabftw:{experiment.get('id')}",
            "experiment_modified_at": experiment.get("modified_at"),
            "policy_revision": selected["policy_revision"],
            "policy_source_key": selected["source_key"],
            "selector_match": True,
            "policy_summary": self._policy_summary(selected["policy"]),
            "policy": selected["policy"],
        }

    def get_diagnostics(self, device_id: str, run_id: str = "") -> dict[str, Any]:
        latest = self.get_latest_policy(device_id)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        if not latest.get("matched"):
            return {
                "timestamp": now,
                "device_id": normalize_device_id(device_id),
                "run_id": normalize_string(run_id),
                "policy_status": "missing",
                "artifact_status": "none",
                "latest_policy": latest,
                "journal_match": {"matched": False, "entries_total": 0, "by_class": {}},
                "next_actions": [
                    "Check that the experiment has the fleet tag and valid metadata policy JSON.",
                    "Check target_selector and resource tags for the selected device.",
                    "Confirm ELAB_API_KEY and ELAB_BASE_URL are valid for this environment.",
                ],
            }

        experiment_id = parse_experiment_numeric_id(latest.get("matched_experiment_id"))
        if experiment_id is None:
            return {
                "timestamp": now,
                "device_id": normalize_device_id(device_id),
                "run_id": normalize_string(run_id),
                "policy_status": "invalid",
                "artifact_status": "none",
                "latest_policy": latest,
                "journal_match": {"matched": False, "entries_total": 0, "by_class": {}},
                "next_actions": [
                    "Inspect policy payload; experiment id is not parseable.",
                    "Validate metadata policy format in eLabFTW.",
                ],
            }

        uploads = self.get_experiment_uploads_summary(experiment_id)
        by_class = uploads.get("by_class", {})
        expected = ["run_summary"]
        latest_policy = latest.get("policy")
        if isinstance(latest_policy, dict):
            expected = self._expected_artifact_classes_from_policy(latest_policy)

        missing = [k for k in expected if int(by_class.get(k, 0)) <= 0]
        artifact_status = "ok"
        if int(uploads.get("uploads_total", 0)) <= 0:
            artifact_status = "none"
        elif missing:
            artifact_status = "missing_expected"

        journal = self._journal_matches(experiment_id, run_id, normalize_device_id(device_id))

        next_actions: list[str] = []
        if missing:
            next_actions.append(f"Missing artifact classes: {', '.join(missing)}.")
            next_actions.append("Check agent report replay and artifact upload flags in agent environment.")
        if normalize_string(run_id) and not journal.get("matched"):
            next_actions.append("No journal match for run_id; verify run_id/agent_id and ingest upload path.")
        if not next_actions:
            next_actions.append("Policy and artifacts look consistent for this experiment/device.")

        return {
            "timestamp": now,
            "device_id": normalize_device_id(device_id),
            "run_id": normalize_string(run_id),
            "policy_status": "ok",
            "artifact_status": artifact_status,
            "expected_artifact_classes": expected,
            "missing_artifact_classes": missing,
            "latest_policy": latest,
            "uploads": uploads,
            "journal_match": journal,
            "next_actions": next_actions,
        }

    def _expected_artifact_classes_from_policy(self, policy: dict[str, Any]) -> list[str]:
        summary = self._policy_summary(policy)
        command_types = summary.get("command_types")
        if not isinstance(command_types, dict):
            command_types = {}

        expected = {"run_summary"}
        if int(command_types.get("WIFI_SCAN", 0)) > 0:
            expected.add("wifi")
        if int(command_types.get("BLE_SCAN", 0)) > 0:
            expected.add("ble")
        if int(command_types.get("CAPTURE_CSI", 0)) > 0:
            expected.add("csi")
        return sorted(expected)

    def validate_experiment(self, experiment_id: int) -> dict[str, Any]:
        metadata_payload = self.get_experiment_metadata(experiment_id)
        uploads_payload = self.get_experiment_uploads_summary(experiment_id)
        by_class = uploads_payload.get("by_class", {})

        expected = ["run_summary"]
        canonical_policy = metadata_payload.get("canonical_policy")
        if isinstance(canonical_policy, dict):
            expected = self._expected_artifact_classes_from_policy(canonical_policy)

        missing = [cls for cls in expected if int(by_class.get(cls, 0)) <= 0]
        status = "ok" if not missing else "missing_expected_artifacts"
        if not metadata_payload.get("policy_found"):
            status = "policy_missing"

        return {
            "experiment_id": int(experiment_id),
            "status": status,
            "policy_found": bool(metadata_payload.get("policy_found")),
            "policy_source_key": metadata_payload.get("policy_source_key"),
            "policy_revision_sha256": metadata_payload.get("policy_revision_sha256"),
            "expected_artifact_classes": expected,
            "missing_artifact_classes": missing,
            "uploads_total": uploads_payload.get("uploads_total", 0),
            "uploads_by_class": by_class,
            "next_actions": (
                ["Check metadata policy JSON and policy metadata keys."]
                if status == "policy_missing"
                else (
                    [f"Missing artifact classes: {', '.join(missing)}.", "Check agent execution and artifact upload settings."]
                    if missing
                    else ["Experiment metadata/policy/uploads are consistent."]
                )
            ),
        }

    def get_journal_tail(
        self,
        *,
        limit: int = 20,
        experiment_id: int | None = None,
        run_id: str = "",
        agent_id: str = "",
    ) -> dict[str, Any]:
        rows = self._read_journal_entries()
        filtered: list[dict[str, Any]] = []
        run_token = normalize_string(run_id)
        agent_token = normalize_device_id(agent_id)

        for row in rows:
            if experiment_id is not None and int(row.get("experiment_id", -1) or -1) != int(experiment_id):
                continue
            if run_token and normalize_string(row.get("run_id")) != run_token:
                continue
            if agent_token and normalize_device_id(row.get("agent_id")) != agent_token:
                continue
            filtered.append(row)

        filtered = filtered[-max(1, int(limit)) :]
        mapped = []
        for row in filtered:
            mapped.append(
                {
                    "received_at": row.get("received_at"),
                    "experiment_id": row.get("experiment_id"),
                    "run_id": row.get("run_id"),
                    "agent_id": row.get("agent_id"),
                    "artifact_name": row.get("artifact_name"),
                    "stored_artifact_name": row.get("stored_artifact_name"),
                    "upload_id": row.get("upload_id"),
                    "classification": self._classify_upload(
                        {"real_name": row.get("artifact_name"), "comment": ""}
                    ),
                }
            )

        return {
            "journal_path": str(self._cfg.artifact_journal_path),
            "journal_exists": self._cfg.artifact_journal_path.exists(),
            "entries_total": len(mapped),
            "entries": mapped,
        }

    def create_policy_experiment(
        self,
        *,
        title: str,
        tags: list[str],
        body: str,
        policy: dict[str, Any],
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(policy, dict) or not policy:
            raise ValueError("policy must be a non-empty object")

        title_token = normalize_string(title)
        if not title_token:
            title_token = f"Fleet MCP Policy {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}"

        clean_tags = [normalize_string(tag) for tag in tags if normalize_string(tag)]
        if self._cfg.fleet_experiment_tag not in clean_tags:
            clean_tags.insert(0, self._cfg.fleet_experiment_tag)

        body_token = normalize_string(body) or "Created by scripts/elab-mcp/server.py"
        metadata = {"fleet": {"policy": policy}}

        post_payload = {
            "title": title_token,
            "tags": clean_tags,
            "body": body_token,
            "metadata": "{}",
        }
        patch_payload = {"metadata": stable_dumps(metadata)}

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "write_enabled": self._cfg.enable_write,
                "post_payload": post_payload,
                "patch_payload": patch_payload,
                "note": "No request sent. Set dry_run=false and ELAB_MCP_ENABLE_WRITE=true to execute.",
            }

        if not self._cfg.enable_write:
            raise ValueError("write tools disabled (set ELAB_MCP_ENABLE_WRITE=true)")

        _, headers, base_url_used = self._http.request_json_with_meta(
            method="POST",
            path="/experiments",
            json_body=post_payload,
        )
        location = normalize_string(headers.get("location"))
        match = re.search(r"/experiments/(\d+)$", location)
        if not match:
            raise RuntimeError(f"could not parse experiment id from Location header: {location}")
        experiment_id = int(match.group(1))

        self._http.request_json(
            method="PATCH",
            path=f"/experiments/{experiment_id}",
            json_body=patch_payload,
        )

        return {
            "ok": True,
            "dry_run": False,
            "experiment_id": experiment_id,
            "location": location,
            "base_url_used": base_url_used,
            "tags": clean_tags,
            "policy_revision_sha256": hashlib.sha256(stable_dumps(metadata).encode("utf-8")).hexdigest(),
        }

    def get_config_summary(self) -> dict[str, Any]:
        return {
            "base_url": self._cfg.base_url,
            "base_url_fallbacks": self._cfg.base_url_fallbacks,
            "api_key_present": bool(self._cfg.api_key),
            "verify_tls": self._cfg.verify_tls,
            "fleet_experiment_tag": self._cfg.fleet_experiment_tag,
            "resource_tag_prefix": self._cfg.resource_tag_prefix,
            "policy_metadata_keys": self._cfg.policy_metadata_keys,
            "artifact_journal_path": str(self._cfg.artifact_journal_path),
            "artifact_journal_exists": self._cfg.artifact_journal_path.exists(),
            "enable_write": self._cfg.enable_write,
        }

    def get_help(self) -> dict[str, Any]:
        return {
            "server": "fleetmanager-elab-mcp",
            "mode": "read-write-optional" if self._cfg.enable_write else "read-only",
            "examples": [
                "elab://experiments/123",
                "elab://experiments/123/metadata",
                "elab://experiments/123/uploads",
                "elab://fleet/latest-policy?device_id=02:42:ac:14:00:04",
                "elab://fleet/diagnostics?device_id=02:42:ac:14:00:04&run_id=abcd-1234",
                "elab://fleet/validate?experiment_id=123",
                "elab://fleet/journal?limit=25&experiment_id=123",
            ],
        }

    def read_uri(self, uri: str) -> dict[str, Any]:
        parsed = urlparse.urlparse(uri)
        if normalize_string(parsed.scheme) != "elab":
            raise ValueError("unsupported URI scheme; expected elab://")

        host = normalize_string(parsed.netloc)
        path_parts = [part for part in parsed.path.split("/") if part]
        query = urlparse.parse_qs(parsed.query, keep_blank_values=True)

        if host == "config":
            return self.get_config_summary()
        if host == "diagnostics" and path_parts == ["help"]:
            return self.get_help()

        if host == "experiments":
            if len(path_parts) == 1:
                exp_id = int(path_parts[0])
                return self.get_experiment_summary(exp_id)
            if len(path_parts) == 2 and path_parts[1] == "metadata":
                exp_id = int(path_parts[0])
                return self.get_experiment_metadata(exp_id)
            if len(path_parts) == 2 and path_parts[1] == "uploads":
                exp_id = int(path_parts[0])
                limit_raw = normalize_string((query.get("limit") or ["15"])[0])
                limit = int(limit_raw) if limit_raw.isdigit() else 15
                return self.get_experiment_uploads_summary(exp_id, limit=limit)
            raise ValueError("unsupported experiments URI")

        if host == "fleet":
            if path_parts == ["latest-policy"]:
                device_id = normalize_string((query.get("device_id") or [""])[0])
                return self.get_latest_policy(device_id)
            if path_parts == ["diagnostics"]:
                device_id = normalize_string((query.get("device_id") or [""])[0])
                run_id = normalize_string((query.get("run_id") or [""])[0])
                return self.get_diagnostics(device_id=device_id, run_id=run_id)
            if path_parts == ["validate"]:
                exp_raw = normalize_string((query.get("experiment_id") or [""])[0])
                if not exp_raw.isdigit():
                    raise ValueError("experiment_id query param is required")
                return self.validate_experiment(int(exp_raw))
            if path_parts == ["journal"]:
                limit_raw = normalize_string((query.get("limit") or ["20"])[0])
                exp_raw = normalize_string((query.get("experiment_id") or [""])[0])
                run_id = normalize_string((query.get("run_id") or [""])[0])
                agent_id = normalize_string((query.get("agent_id") or [""])[0])
                limit = int(limit_raw) if limit_raw.isdigit() else 20
                experiment_id = int(exp_raw) if exp_raw.isdigit() else None
                return self.get_journal_tail(
                    limit=limit,
                    experiment_id=experiment_id,
                    run_id=run_id,
                    agent_id=agent_id,
                )
            raise ValueError("unsupported fleet URI")

        raise ValueError("unsupported URI")


class McpServer:
    def __init__(self, diagnostics: ElabDiagnostics):
        self._diagnostics = diagnostics
        self._protocol_version = "2024-11-05"
        self._shutdown = False

    def _resource_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "uri": "elab://config",
                "name": "eLab MCP Config",
                "description": "Resolved runtime configuration used by this MCP server.",
                "mimeType": "application/json",
            },
            {
                "uri": "elab://diagnostics/help",
                "name": "eLab Diagnostics Help",
                "description": "Quick examples for supported URIs and tool calls.",
                "mimeType": "application/json",
            },
        ]

    def _resource_templates(self) -> list[dict[str, Any]]:
        templates = [
            {
                "uriTemplate": "elab://experiments/{id}",
                "name": "Experiment Summary",
                "description": "Basic experiment fields for quick checks.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "elab://experiments/{id}/metadata",
                "name": "Experiment Metadata + Policy",
                "description": "Raw metadata and normalized policy extraction details.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "elab://experiments/{id}/uploads?limit={n}",
                "name": "Experiment Uploads",
                "description": "Uploads summary with artifact class breakdown.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "elab://fleet/latest-policy?device_id={id}",
                "name": "Latest Policy for Device",
                "description": "Policy selected for the device using current Fleet matching logic.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "elab://fleet/diagnostics?device_id={id}&run_id={run_id}",
                "name": "Policy + Artifact Diagnostics",
                "description": "Aggregated diagnostics for policy assignment and artifacts.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "elab://fleet/validate?experiment_id={id}",
                "name": "Experiment Validation",
                "description": "Validate policy metadata and expected artifact classes for one experiment.",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "elab://fleet/journal?limit={n}&experiment_id={id}&run_id={run_id}&agent_id={agent_id}",
                "name": "Artifact Journal Tail",
                "description": "Read filtered tail of artifact ingest journal.",
                "mimeType": "application/json",
            },
        ]
        return templates

    def _tools(self) -> list[dict[str, Any]]:
        tools = [
            {
                "name": "elab_read",
                "description": "Read any supported elab:// URI and return JSON.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"uri": {"type": "string"}},
                    "required": ["uri"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "elab_get_latest_policy",
                "description": "Resolve current policy for a device_id.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"device_id": {"type": "string"}},
                    "required": ["device_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "elab_get_diagnostics",
                "description": "Run aggregate policy/artifact diagnostics for a device.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "device_id": {"type": "string"},
                        "run_id": {"type": "string"},
                    },
                    "required": ["device_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "elab_get_experiment_uploads",
                "description": "Get upload summary by class for an experiment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "experiment_id": {"type": "integer"},
                        "limit": {"type": "integer", "minimum": 1},
                    },
                    "required": ["experiment_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "elab_validate_experiment",
                "description": "Validate one experiment: policy metadata and expected artifacts.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"experiment_id": {"type": "integer"}},
                    "required": ["experiment_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "elab_get_journal_tail",
                "description": "Read filtered artifact ingest journal tail.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1},
                        "experiment_id": {"type": "integer"},
                        "run_id": {"type": "string"},
                        "agent_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "elab_create_policy_experiment",
                "description": "Create a new experiment and patch fleet.policy metadata (dry_run by default).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "body": {"type": "string"},
                        "policy": {"type": "object"},
                        "dry_run": {"type": "boolean"},
                    },
                    "required": ["policy"],
                    "additionalProperties": False,
                },
            },
        ]
        return tools

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "elab_read":
            uri = normalize_string(arguments.get("uri"))
            if not uri:
                raise ValueError("uri is required")
            return self._diagnostics.read_uri(uri)

        if name == "elab_get_latest_policy":
            device_id = normalize_string(arguments.get("device_id"))
            return self._diagnostics.get_latest_policy(device_id)

        if name == "elab_get_diagnostics":
            device_id = normalize_string(arguments.get("device_id"))
            run_id = normalize_string(arguments.get("run_id"))
            return self._diagnostics.get_diagnostics(device_id=device_id, run_id=run_id)

        if name == "elab_get_experiment_uploads":
            exp_id_raw = arguments.get("experiment_id")
            if not isinstance(exp_id_raw, int):
                raise ValueError("experiment_id must be integer")
            limit_raw = arguments.get("limit", 15)
            limit = int(limit_raw) if isinstance(limit_raw, int) else 15
            return self._diagnostics.get_experiment_uploads_summary(exp_id_raw, limit=limit)

        if name == "elab_validate_experiment":
            exp_id_raw = arguments.get("experiment_id")
            if not isinstance(exp_id_raw, int):
                raise ValueError("experiment_id must be integer")
            return self._diagnostics.validate_experiment(exp_id_raw)

        if name == "elab_get_journal_tail":
            limit_raw = arguments.get("limit", 20)
            limit = int(limit_raw) if isinstance(limit_raw, int) else 20
            exp_id = arguments.get("experiment_id")
            if exp_id is not None and not isinstance(exp_id, int):
                raise ValueError("experiment_id must be integer when provided")
            run_id = normalize_string(arguments.get("run_id"))
            agent_id = normalize_string(arguments.get("agent_id"))
            return self._diagnostics.get_journal_tail(
                limit=limit,
                experiment_id=exp_id,
                run_id=run_id,
                agent_id=agent_id,
            )

        if name == "elab_create_policy_experiment":
            policy = arguments.get("policy")
            if not isinstance(policy, dict):
                raise ValueError("policy must be an object")
            title = normalize_string(arguments.get("title"))
            body = normalize_string(arguments.get("body"))
            tags_raw = arguments.get("tags")
            tags = tags_raw if isinstance(tags_raw, list) else []
            clean_tags = [normalize_string(row) for row in tags if normalize_string(row)]
            dry_run = True if arguments.get("dry_run") is None else bool(arguments.get("dry_run"))
            return self._diagnostics.create_policy_experiment(
                title=title,
                tags=clean_tags,
                body=body,
                policy=policy,
                dry_run=dry_run,
            )

        raise ValueError(f"unknown tool: {name}")

    def _handle_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            client_protocol = normalize_string(params.get("protocolVersion"))
            if client_protocol:
                self._protocol_version = client_protocol
            return {
                "protocolVersion": self._protocol_version,
                "capabilities": {
                    "resources": {"subscribe": False, "listChanged": False},
                    "tools": {"listChanged": False},
                },
                "serverInfo": {"name": "fleetmanager-elab-mcp", "version": "0.1.0"},
            }

        if method == "ping":
            return {}

        if method == "resources/list":
            return {"resources": self._resource_specs()}

        if method in {"resources/templates/list", "resourceTemplates/list"}:
            return {"resourceTemplates": self._resource_templates()}

        if method == "resources/read":
            uri = normalize_string(params.get("uri"))
            if not uri:
                raise ValueError("uri is required")
            payload = self._diagnostics.read_uri(uri)
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": stable_dumps(payload),
                    }
                ]
            }

        if method == "tools/list":
            return {"tools": self._tools()}

        if method == "tools/call":
            name = normalize_string(params.get("name"))
            arguments = params.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            payload = self._call_tool(name, arguments)
            return {
                "content": [{"type": "text", "text": stable_dumps(payload)}],
                "structuredContent": payload,
                "isError": False,
            }

        if method == "shutdown":
            self._shutdown = True
            return {}

        raise KeyError(f"unsupported method: {method}")

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        _ = params
        if method in {"notifications/initialized", "initialized"}:
            return
        if method in {"notifications/cancelled"}:
            return

    def _send(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()

    def _send_response(self, request_id: Any, result: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _send_error(self, request_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _read_message(self) -> dict[str, Any] | None:
        first = sys.stdin.buffer.readline()
        if not first:
            return None

        if first.lstrip().startswith(b"{"):
            raw = first
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_error(None, -32700, "parse error")
                return {}

        headers: dict[str, str] = {}
        line = first
        while line and line not in {b"\r\n", b"\n"}:
            try:
                text = line.decode("utf-8", errors="replace").strip()
                if ":" in text:
                    key, value = text.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            except Exception:
                pass
            line = sys.stdin.buffer.readline()
            if not line:
                return None

        raw_len = normalize_string(headers.get("content-length"))
        if not raw_len.isdigit():
            self._send_error(None, -32600, "missing content-length")
            return {}
        length = int(raw_len)
        if length <= 0:
            self._send_error(None, -32600, "invalid content-length")
            return {}

        body = sys.stdin.buffer.read(length)
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            self._send_error(None, -32700, "parse error")
            return {}

    def run(self) -> None:
        while not self._shutdown:
            message = self._read_message()
            if message is None:
                break
            if not message:
                continue

            request_id = message.get("id")
            method = normalize_string(message.get("method"))
            params = message.get("params")
            if not isinstance(params, dict):
                params = {}

            if not method:
                if request_id is not None:
                    self._send_error(request_id, -32600, "invalid request")
                continue

            if request_id is None:
                try:
                    self._handle_notification(method, params)
                except Exception as exc:
                    log.warning("notification failed: %s", exc)
                continue

            try:
                result = self._handle_request(method, params)
                self._send_response(request_id, result)
            except ValueError as exc:
                self._send_error(request_id, -32602, str(exc))
            except KeyError as exc:
                self._send_error(request_id, -32601, str(exc))
            except Exception as exc:
                log.exception("request failed: method=%s", method)
                self._send_error(request_id, -32603, f"internal error: {exc}")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = ElabConfig.from_env_and_repo(repo_root)
    log.info(
        "Starting eLab MCP server base_url=%s fallbacks=%s verify_tls=%s api_key_present=%s enable_write=%s",
        cfg.base_url,
        cfg.base_url_fallbacks,
        cfg.verify_tls,
        bool(cfg.api_key),
        cfg.enable_write,
    )
    diagnostics = ElabDiagnostics(cfg)
    server = McpServer(diagnostics)
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
