from .core import *  # noqa: F401,F403
from .policy_design import runtime_policy_from_design

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

        canonical_payload = runtime_policy_from_design(policy_raw)
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

    def _policy_measurement_window(self, policy: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
        range_obj = policy.get("range") if isinstance(policy, dict) else {}
        if isinstance(range_obj, dict):
            start_dt = ensure_utc_timestamp(parse_iso_timestamp(range_obj.get("from")))
            end_dt = ensure_utc_timestamp(parse_iso_timestamp(range_obj.get("to")))
            if start_dt is not None or end_dt is not None:
                return start_dt, end_dt

        starts: list[datetime] = []
        ends: list[datetime] = []
        groups = policy.get("command_groups") if isinstance(policy, dict) else []
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                group_range = group.get("range")
                if not isinstance(group_range, dict):
                    continue
                start_dt = ensure_utc_timestamp(parse_iso_timestamp(group_range.get("from")))
                end_dt = ensure_utc_timestamp(parse_iso_timestamp(group_range.get("to")))
                if start_dt is not None:
                    starts.append(start_dt)
                if end_dt is not None:
                    ends.append(end_dt)
        return (min(starts) if starts else None, max(ends) if ends else None)

    def _policy_window_bucket(self, policy: dict[str, Any], now_utc: datetime) -> int:
        start_dt, end_dt = self._policy_measurement_window(policy)
        if end_dt is not None and now_utc > end_dt:
            return 0  # expired
        if start_dt is not None and now_utc < start_dt:
            return 1  # future
        return 2  # active or unspecified

    def _select_policy_for_device(self, item: dict[str, Any], device_id: str) -> dict[str, Any] | None:
        experiments = self._fetch_fleet_experiments()
        now_utc = utc_now()
        skip_expired = bool(self._cfg.get("skip_expired_policies", True))
        candidates: list[tuple[int, int, int, int, dict[str, Any]]] = []

        for experiment_ref in experiments:
            exp_id = experiment_ref.get("id")
            if exp_id is None:
                continue

            experiment = experiment_ref if isinstance(experiment_ref, dict) else None
            if not isinstance(experiment, dict):
                continue

            # /experiments list already contains metadata/tags on current eLabFTW builds.
            # Avoid N+1 GET /experiments/{id} calls, which can exceed gRPC deadlines
            # when many historical fleet experiments exist.
            if "metadata" not in experiment or "tags" not in experiment:
                fetched = self._elab_client.get_experiment(int(exp_id))
                if not fetched:
                    continue
                experiment = fetched

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

            window_bucket = self._policy_window_bucket(policy_ctx["policy"], now_utc)
            if skip_expired and window_bucket == 0:
                continue

            start_dt, _ = self._policy_measurement_window(policy_ctx["policy"])
            start_rank = int(start_dt.timestamp()) if start_dt is not None else 0
            exp_id_rank = parse_experiment_numeric_id(experiment.get("id") or exp_id) or 0
            modified_dt = ensure_utc_timestamp(
                parse_iso_timestamp(experiment.get("modified_at") or experiment_ref.get("modified_at"))
            )
            modified_rank = int(modified_dt.timestamp()) if modified_dt is not None else 0

            candidates.append((window_bucket, start_rank, exp_id_rank, modified_rank, policy_ctx))

        if not candidates:
            return None

        candidates.sort(key=lambda row: row[:4], reverse=True)
        return candidates[0][4]

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
        if event_type == "COMMAND_FAILED":
            return "PARTIAL"
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
        event_type = normalize_string(event.type).upper()
        if event_type == "COMMAND_FINISHED" and int(event.exit_code) != 0:
            summary["commands_failed"] = int(summary.get("commands_failed", 0)) + 1
        if event_type == "COMMAND_FAILED":
            summary["commands_failed"] = int(summary.get("commands_failed", 0)) + 1
        if event_type == "ERROR":
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

