from .core import *  # noqa: F401,F403
from .servicer_v1 import FleetManagerServicer

class FleetManagerServicerV2(fleet_gateway_v2_pb2_grpc.FleetManagerServicer):
    def __init__(self, core: FleetManagerServicer, cfg: dict[str, Any]):
        self._core = core
        self._cfg = cfg
        self._state = core._state
        raw_map = cfg.get("resource_status_id_map") or {}
        normalized_map: dict[str, int] = {}
        if isinstance(raw_map, dict):
            for raw_key, raw_value in raw_map.items():
                key = normalize_string(raw_key).lower()
                if not key:
                    continue
                try:
                    value = int(raw_value)
                except Exception:
                    continue
                if value > 0:
                    normalized_map[key] = value
        self._resource_status_id_map = normalized_map

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

    def _resolve_item_and_policy_ctx(self, agent_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        item = self._core._get_or_create_device_item(agent_id, None)
        if not item:
            return None, None
        return item, self._core._select_policy_for_device(item, agent_id)

    def _resource_status_title_for_runtime_state(self, state: str) -> str | None:
        token = normalize_string(state).upper()
        mapping = {
            "ONLINE": "Operational",
            "ASSIGNED": "Open",
            "IDLE": "Operational",
            "POLICY_READY": "Open",
            "POLICY_CACHED": "Open",
            "WAITING_POLICY": "Waiting",
            "PREPARED": "Open",
            "PREPARE_REJECTED": "Maintenance mode",
            "REPORT_ACCEPTED": "Processed",
            "REPORT_DUPLICATE": "Processed",
            "REPORT_REJECTED": "Maintenance mode",
        }
        return mapping.get(token)

    def _resolve_resource_status_id(self, item: dict[str, Any], status_title: str | None) -> int | None:
        title = normalize_string(status_title)
        if not title:
            return None
        title_key = title.lower()

        override_id = self._resource_status_id_map.get(title_key)
        if override_id is not None:
            return int(override_id)

        team_value = item.get("team")
        try:
            team_id = int(team_value)
        except Exception:
            team_id = 0

        # Local Compose bootstrap seed for team 1 matches these IDs.
        if team_id == 1:
            fallback = DEFAULT_RESOURCE_STATUS_IDS_TEAM1.get(title_key)
            if fallback is not None:
                return int(fallback)
        return None

    def _set_device_runtime_state(
        self,
        agent_id: str,
        state: str,
        *,
        item: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if not bool(self._cfg.get("enable_v2_device_state_patch", True)):
            return

        token = normalize_device_id(agent_id)
        if not token:
            return

        def strip_timestamps(value: Any) -> Any:
            if isinstance(value, dict):
                cleaned: dict[str, Any] = {}
                for raw_key, raw_value in value.items():
                    key = normalize_string(raw_key)
                    if key == "at" or key.endswith("_at"):
                        continue
                    cleaned[key] = strip_timestamps(raw_value)
                return cleaned
            if isinstance(value, list):
                return [strip_timestamps(row) for row in value]
            return value

        try:
            current_item = item if isinstance(item, dict) else self._core._find_device_item(token)
            if current_item is None:
                current_item = self._core._get_or_create_device_item(token, None)
            if not isinstance(current_item, dict):
                return

            item_id = current_item.get("id")
            if not item_id:
                return

            metadata = parse_maybe_json(current_item.get("metadata"), {})
            if not isinstance(metadata, dict):
                metadata = {}

            runtime = metadata.get("fleet_v2_runtime")
            if not isinstance(runtime, dict):
                runtime = {}
            runtime_current = parse_maybe_json(stable_dumps(runtime), {})
            if not isinstance(runtime_current, dict):
                runtime_current = {}
            runtime_next = parse_maybe_json(stable_dumps(runtime_current), {})
            if not isinstance(runtime_next, dict):
                runtime_next = {}

            runtime_next["agent_id"] = token
            runtime_next["device_state"] = normalize_string(state).upper() or "UNKNOWN"
            if isinstance(details, dict):
                for key, value in details.items():
                    if value is None:
                        continue
                    runtime_next[key] = value

            target_status_id: int | None = None
            target_status_title = self._resource_status_title_for_runtime_state(state)
            if target_status_title:
                target_status_id = self._resolve_resource_status_id(current_item, target_status_title)

            current_status_value = current_item.get("status")
            try:
                current_status_id = int(current_status_value) if current_status_value is not None else None
            except Exception:
                current_status_id = None
            should_patch_status = target_status_id is not None and current_status_id != int(target_status_id)

            current_semantic = strip_timestamps(runtime_current)
            next_semantic = strip_timestamps(runtime_next)
            if stable_dumps(current_semantic) == stable_dumps(next_semantic) and not should_patch_status:
                return

            runtime_next["state_updated_at"] = utc_now_iso()
            metadata["fleet_v2_runtime"] = runtime_next
            patch_fields: dict[str, Any] = {"metadata": stable_dumps(metadata)}
            if should_patch_status and target_status_id is not None:
                patch_fields["status"] = int(target_status_id)
            self._core._elab_client.patch_item_fields(int(item_id), patch_fields)
        except Exception as exc:
            log.warning(
                "Failed to patch fleet_v2_runtime state for agent_id=%s state=%s: %s",
                token,
                normalize_string(state),
                exc,
            )

    def _measurement_window(self, policy: dict[str, Any]) -> tuple[str, str]:
        reporting = policy.get("reporting") if isinstance(policy, dict) else {}
        if isinstance(reporting, dict):
            measure_window = reporting.get("measure_window")
            if not isinstance(measure_window, dict):
                measure_window = reporting.get("measurement_window") if isinstance(reporting.get("measurement_window"), dict) else {}
            start = normalize_string(measure_window.get("from") or measure_window.get("start"))
            end = normalize_string(measure_window.get("to") or measure_window.get("end"))
            if start and end:
                return start, end

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

    def _reporting_upload_window(self, policy: dict[str, Any], start_iso: str, end_iso: str) -> tuple[str, str]:
        reporting = policy.get("reporting") if isinstance(policy, dict) else {}
        if not isinstance(reporting, dict):
            return normalize_string(start_iso), normalize_string(end_iso)

        upload_window = reporting.get("upload_window")
        if not isinstance(upload_window, dict):
            upload_window = reporting.get("upload") if isinstance(reporting.get("upload"), dict) else {}

        upload_from = normalize_string(
            upload_window.get("from")
            or upload_window.get("start")
            or reporting.get("upload_from")
        )
        upload_to = normalize_string(
            upload_window.get("to")
            or upload_window.get("end")
            or reporting.get("upload_to")
        )

        return (upload_from or normalize_string(start_iso), upload_to or normalize_string(end_iso))

    def _metrics_sink_remote_write_cmds(self, policy: dict[str, Any], reporting: dict[str, Any]) -> tuple[str, str]:
        remote_write = reporting.get("remote_write") if isinstance(reporting.get("remote_write"), dict) else {}
        metrics_sink = policy.get("metrics_sink") if isinstance(policy, dict) else {}
        if not isinstance(metrics_sink, dict):
            metrics_sink = {}
        if not metrics_sink and isinstance(reporting.get("metrics_sink"), dict):
            metrics_sink = reporting.get("metrics_sink")

        local_prom = metrics_sink.get("local_prometheus")
        if not isinstance(local_prom, dict):
            local_prom = {}

        candidates = [
            remote_write,
            metrics_sink.get("remote_write"),
            metrics_sink.get("remote_write_window"),
            local_prom.get("remote_write"),
            local_prom.get("remote_write_window"),
            local_prom.get("window_gate"),
        ]
        resolved: dict[str, Any] = {}
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate:
                resolved = candidate
                break

        on_cmd = normalize_string(
            resolved.get("on_cmd")
            or resolved.get("enable_cmd")
            or local_prom.get("remote_write_on_cmd")
            or metrics_sink.get("remote_write_on_cmd")
        )
        off_cmd = normalize_string(
            resolved.get("off_cmd")
            or resolved.get("disable_cmd")
            or local_prom.get("remote_write_off_cmd")
            or metrics_sink.get("remote_write_off_cmd")
        )
        return on_cmd, off_cmd

    def _policy_reporting_env(self, policy: dict[str, Any], start_iso: str, end_iso: str) -> dict[str, str]:
        reporting = policy.get("reporting") if isinstance(policy, dict) else {}
        metrics_sink = policy.get("metrics_sink") if isinstance(policy, dict) else {}
        if not isinstance(reporting, dict):
            reporting = {}
        if not isinstance(metrics_sink, dict):
            metrics_sink = {}
        if not reporting and not metrics_sink:
            return {}

        out: dict[str, str] = {}
        if reporting:
            upload_from, upload_to = self._reporting_upload_window(policy, start_iso, end_iso)
            if upload_from:
                out["UPLOAD_WINDOW_FROM"] = upload_from
            if upload_to:
                out["UPLOAD_WINDOW_TO"] = upload_to

            slotting = reporting.get("slotting")
            if not isinstance(slotting, dict):
                slotting = {}

            slot_count_value = (
                slotting.get("slot_count")
                or slotting.get("slots")
                or reporting.get("slot_count")
                or reporting.get("slots")
            )
            try:
                slot_count = max(1, int(slot_count_value or 1))
                out["UPLOAD_SLOT_COUNT"] = str(slot_count)
            except Exception:
                pass

            jitter_value = (
                slotting.get("jitter_s")
                or slotting.get("jitter_seconds")
                or reporting.get("jitter_s")
                or reporting.get("jitter_seconds")
            )
            try:
                jitter_s = max(0, int(jitter_value or 0))
                out["UPLOAD_SLOT_JITTER_S"] = str(jitter_s)
            except Exception:
                pass

            require_window_raw = reporting.get("require_upload_window")
            if require_window_raw is None:
                require_window_raw = True
            out["UPLOAD_WINDOW_REQUIRED"] = "true" if normalize_string(require_window_raw).lower() in {"1", "true", "yes", "on"} else "false"

        on_cmd, off_cmd = self._metrics_sink_remote_write_cmds(policy, reporting)
        if on_cmd:
            out["PROM_REMOTE_WRITE_ON_CMD"] = on_cmd
        if off_cmd:
            out["PROM_REMOTE_WRITE_OFF_CMD"] = off_cmd

        return out

    def _report_metrics_upload_state(self, report: fleet_gateway_v2_pb2.Report) -> str:
        value = normalize_string(report.summary_metrics.get("metrics_upload_state")).upper()
        if value in {"UPLOADED", "UPLOAD_FAILED", "PENDING"}:
            return value
        return "UNKNOWN"

    def _status_ack_from_v1(self, ack: fleet_gateway_pb2.Ack) -> fleet_gateway_v2_pb2.StatusAck:
        return fleet_gateway_v2_pb2.StatusAck(
            ok=bool(ack.ok),
            reason=normalize_string(ack.message),
        )

    def _upload_status_text(self, status_value: int) -> str:
        mapping = {
            int(fleet_gateway_v2_pb2.UPLOAD_ACK): "ACK",
            int(fleet_gateway_v2_pb2.UPLOAD_ERROR): "ERROR",
            int(fleet_gateway_v2_pb2.UPLOAD_PENDING): "PENDING",
            int(fleet_gateway_v2_pb2.UPLOAD_SKIPPED): "SKIPPED",
        }
        return mapping.get(int(status_value), "UNSPECIFIED")

    def _upload_payload_kind_text(self, kind_value: int) -> str:
        mapping = {
            int(fleet_gateway_v2_pb2.METRICS_LOGS): "metrics",
            int(fleet_gateway_v2_pb2.ARTIFACT): "artifact",
        }
        return mapping.get(int(kind_value), "unspecified")

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
        reporting_env = self._policy_reporting_env(policy, start_iso, end_iso)
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
                env_map = {normalize_string(k): normalize_string(v) for k, v in env.items() if normalize_string(k)}
                merged_env = dict(reporting_env)
                merged_env.update(env_map)
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
                command_type = self._command_type(cmd)
                cmdline = normalize_string(cmd.get("cmdline") or cmd.get("cmd"))
                if not cmdline and command_type == fleet_gateway_v2_pb2.SHELL:
                    cmdline = normalize_string(env_map.get("cmdline") or env_map.get("CMDLINE"))
                commands_msg.append(
                    fleet_gateway_v2_pb2.Command(
                        id=normalize_string(cmd.get("id") or cmd.get("name")),
                        type=command_type,
                        cmdline=cmdline,
                        argv=[normalize_string(arg) for arg in argv if normalize_string(arg)],
                        env=merged_env,
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
            self._set_device_runtime_state(
                agent_id,
                "ONLINE",
                item=item,
                details={
                    "last_hello": {
                        "allowed_mode": mode_enum_to_text(allowed_mode),
                        "mode_reason": normalize_string(reason),
                        "requested_mode": mode_enum_to_text(int(agent.requested_mode)),
                        "control_plane_iface": normalize_string(agent.control_plane_iface),
                        "fleet_manager_target": normalize_string(agent.fleet_manager_target),
                        "at": utc_now_iso(),
                    }
                },
            )
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

    def GetPolicy(self, request, context):
        agent_id = normalize_device_id(request.agent_id)
        if not agent_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "agent_id is required")
        try:
            item, policy_ctx = self._resolve_item_and_policy_ctx(agent_id)
            if not policy_ctx:
                self._set_device_runtime_state(
                    agent_id,
                    "IDLE",
                    item=item,
                    details={
                        "last_assignment": {
                            "status": "NO_WORK",
                            "at": utc_now_iso(),
                        },
                        "last_policy_fetch": {
                            "status": "NO_WORK",
                            "at": utc_now_iso(),
                        }
                    },
                )
                return fleet_gateway_v2_pb2.GetPolicyResponse(
                    status=fleet_gateway_v2_pb2.GetPolicyResponse.NO_WORK
                )
            policy = policy_ctx["policy"]
            start_iso, end_iso = self._measurement_window(policy)
            experiment_id = normalize_string(policy.get("experiment_id"))
            policy_id = normalize_string(policy_ctx["policy_revision"])
            self._set_device_runtime_state(
                agent_id,
                "ASSIGNED",
                item=item,
                details={
                    "last_assignment": {
                        "status": "ASSIGNED",
                        "experiment_id": experiment_id,
                        "policy_id": policy_id,
                        "measurement_from": start_iso,
                        "measurement_to": end_iso,
                        "at": utc_now_iso(),
                    }
                },
            )
            requested_experiment_id = normalize_string(request.experiment_id)
            if requested_experiment_id and requested_experiment_id != experiment_id:
                self._set_device_runtime_state(
                    agent_id,
                    "WAITING_POLICY",
                    item=item,
                    details={
                        "last_policy_fetch": {
                            "status": "NO_WORK",
                            "reason": "experiment_id mismatch",
                            "requested_experiment_id": requested_experiment_id,
                            "resolved_experiment_id": experiment_id,
                            "policy_id": policy_id,
                            "at": utc_now_iso(),
                        }
                    },
                )
                return fleet_gateway_v2_pb2.GetPolicyResponse(
                    status=fleet_gateway_v2_pb2.GetPolicyResponse.NO_WORK,
                    experiment_id=experiment_id,
                    policy_id=policy_id,
                    measurement_from=timestamp_from_iso(start_iso),
                    measurement_to=timestamp_from_iso(end_iso),
                )
            if normalize_string(request.last_policy_id) == policy_id:
                self._set_device_runtime_state(
                    agent_id,
                    "POLICY_CACHED",
                    item=item,
                    details={
                        "last_policy_fetch": {
                            "status": "NOT_MODIFIED",
                            "policy_id": policy_id,
                            "experiment_id": experiment_id,
                            "at": utc_now_iso(),
                        }
                    },
                )
                return fleet_gateway_v2_pb2.GetPolicyResponse(
                    status=fleet_gateway_v2_pb2.GetPolicyResponse.NOT_MODIFIED,
                    experiment_id=experiment_id,
                    policy_id=policy_id,
                    measurement_from=timestamp_from_iso(start_iso),
                    measurement_to=timestamp_from_iso(end_iso),
                )
            self._set_device_runtime_state(
                agent_id,
                "POLICY_READY",
                item=item,
                details={
                    "last_policy_fetch": {
                        "status": "OK",
                        "policy_id": policy_id,
                        "experiment_id": experiment_id,
                        "at": utc_now_iso(),
                    }
                },
            )
            return fleet_gateway_v2_pb2.GetPolicyResponse(
                status=fleet_gateway_v2_pb2.GetPolicyResponse.OK,
                policy=self._policy_to_v2(policy, policy_id),
                experiment_id=experiment_id,
                policy_id=policy_id,
                measurement_from=timestamp_from_iso(start_iso),
                measurement_to=timestamp_from_iso(end_iso),
            )
        except Exception as exc:
            log.exception("GetPolicy(v2) failed for agent_id=%s", agent_id)
            context.abort(grpc.StatusCode.INTERNAL, f"GetPolicy(v2) failed: {exc}")

    def ReportCommandStatus(self, request, context):
        agent_id = normalize_device_id(request.agent_id)
        run_id = normalize_string(request.run_id)
        experiment_id = normalize_string(request.experiment_id)
        policy_id = normalize_string(request.policy_id)
        command_id = normalize_string(request.command_id)
        if not agent_id or not run_id or not experiment_id or not policy_id:
            return fleet_gateway_v2_pb2.StatusAck(
                ok=False,
                reason="agent_id, run_id, experiment_id and policy_id are required",
            )

        stage_value = int(request.stage)
        stage_text = {
            int(fleet_gateway_v2_pb2.COMMAND_STATUS_STAGE_STARTED): "COMMAND_STARTED",
            int(fleet_gateway_v2_pb2.COMMAND_STATUS_STAGE_FINISHED): "COMMAND_FINISHED",
            int(fleet_gateway_v2_pb2.COMMAND_STATUS_STAGE_FAILED): "COMMAND_FAILED",
        }.get(stage_value, "")
        if not stage_text:
            return fleet_gateway_v2_pb2.StatusAck(ok=False, reason="stage is required")

        event_type = stage_text
        exit_code = int(request.exit_code)
        if stage_text == "COMMAND_STARTED":
            exit_code = 0
        elif stage_text == "COMMAND_FAILED" and exit_code == 0:
            exit_code = 1

        metrics = {
            normalize_string(k): normalize_string(v)
            for k, v in dict(request.metrics).items()
            if normalize_string(k)
        }
        metrics.setdefault("run_id", run_id)
        cmd_type = normalize_string(request.command_type)
        measure_type = normalize_string(request.measure_type)
        if cmd_type:
            metrics.setdefault("command_type", cmd_type)
        if measure_type:
            metrics.setdefault("measure_type", measure_type)
        metrics.setdefault("status_stage", stage_text)
        message = normalize_string(request.message) or f"{command_id or 'command'} {stage_text}"
        event_id = normalize_string(request.event_id) or f"{run_id}:{command_id or 'command'}:{stage_text.lower()}"
        event = fleet_gateway_pb2.Event(
            event_id=event_id,
            experiment_id=experiment_id,
            policy_revision=policy_id,
            device_id=agent_id,
            unix_time_ms=int(time.time() * 1000),
            type=event_type,
            command_id=command_id,
            exit_code=exit_code,
            stdout_tail=message,
            stderr_tail="",
            metrics=metrics,
            artifacts={},
        )

        try:
            ack = self._core._ingest_v1_event(event)
            return self._status_ack_from_v1(ack)
        except Exception as exc:
            log.exception("ReportCommandStatus(v2) failed for agent_id=%s run_id=%s", agent_id, run_id)
            context.abort(grpc.StatusCode.INTERNAL, f"ReportCommandStatus(v2) failed: {exc}")

    def ReportUploadStatus(self, request, context):
        agent_id = normalize_device_id(request.agent_id)
        run_id = normalize_string(request.run_id)
        experiment_id = normalize_string(request.experiment_id)
        policy_id = normalize_string(request.policy_id)
        if not agent_id or not run_id or not experiment_id or not policy_id:
            return fleet_gateway_v2_pb2.StatusAck(
                ok=False,
                reason="agent_id, run_id, experiment_id and policy_id are required",
            )

        payload_kind = self._upload_payload_kind_text(int(request.payload_kind))
        status_text = self._upload_status_text(int(request.status))
        command_id = f"upload:{payload_kind}"
        if status_text == "ACK":
            event_type = "COMMAND_FINISHED"
            exit_code = 0
        elif status_text == "SKIPPED":
            event_type = "COMMAND_FINISHED"
            exit_code = 0
        elif status_text == "ERROR":
            event_type = "COMMAND_FAILED"
            exit_code = 1
        else:
            event_type = "COMMAND_STARTED"
            exit_code = 0

        metrics = {
            normalize_string(k): normalize_string(v)
            for k, v in dict(request.metrics).items()
            if normalize_string(k)
        }
        metrics.setdefault("run_id", run_id)
        metrics.setdefault("upload_payload_kind", payload_kind)
        metrics.setdefault("upload_status", status_text)

        message = normalize_string(request.message) or f"{payload_kind} upload {status_text}"
        event_id = normalize_string(request.event_id) or f"{run_id}:{command_id}:{status_text.lower()}"
        event = fleet_gateway_pb2.Event(
            event_id=event_id,
            experiment_id=experiment_id,
            policy_revision=policy_id,
            device_id=agent_id,
            unix_time_ms=int(time.time() * 1000),
            type=event_type,
            command_id=command_id,
            exit_code=exit_code,
            stdout_tail=message,
            stderr_tail="",
            metrics=metrics,
            artifacts={},
        )

        try:
            ack = self._core._ingest_v1_event(event)
            return self._status_ack_from_v1(ack)
        except Exception as exc:
            log.exception("ReportUploadStatus(v2) failed for agent_id=%s run_id=%s", agent_id, run_id)
            context.abort(grpc.StatusCode.INTERNAL, f"ReportUploadStatus(v2) failed: {exc}")

    def ReportArtifactUploadStatus(self, request, context):
        agent_id = normalize_device_id(request.agent_id)
        run_id = normalize_string(request.run_id)
        experiment_id = normalize_string(request.experiment_id)
        policy_id = normalize_string(request.policy_id)
        if not agent_id or not run_id or not experiment_id or not policy_id:
            return fleet_gateway_v2_pb2.StatusAck(
                ok=False,
                reason="agent_id, run_id, experiment_id and policy_id are required",
            )

        artifact_name = normalize_string(request.artifact_name) or "artifact"
        status_text = self._upload_status_text(int(request.status))
        event_type = "ARTIFACT_UPLOADED" if status_text in {"ACK", "SKIPPED"} else "ERROR"
        exit_code = 0 if status_text in {"ACK", "SKIPPED"} else 1

        metrics = {
            normalize_string(k): normalize_string(v)
            for k, v in dict(request.metrics).items()
            if normalize_string(k)
        }
        metrics.setdefault("run_id", run_id)
        metrics.setdefault("upload_payload_kind", "artifact")
        metrics.setdefault("upload_status", status_text)
        metrics.setdefault("artifact_name", artifact_name)
        if int(request.artifact_size_bytes or 0) > 0:
            metrics.setdefault("artifact_size_bytes", str(int(request.artifact_size_bytes)))
        artifact_sha = normalize_string(request.artifact_sha256)
        if artifact_sha:
            metrics.setdefault("artifact_sha256", artifact_sha)

        artifacts = {}
        artifact_uri = normalize_string(request.artifact_uri)
        if artifact_uri:
            artifacts["uri"] = artifact_uri

        message = normalize_string(request.message) or f"artifact {artifact_name} upload {status_text}"
        event_id = normalize_string(request.event_id) or f"{run_id}:artifact:{artifact_name}:{status_text.lower()}"
        event = fleet_gateway_pb2.Event(
            event_id=event_id,
            experiment_id=experiment_id,
            policy_revision=policy_id,
            device_id=agent_id,
            unix_time_ms=int(time.time() * 1000),
            type=event_type,
            command_id=artifact_name,
            exit_code=exit_code,
            stdout_tail=message,
            stderr_tail="",
            metrics=metrics,
            artifacts=artifacts,
        )

        try:
            ack = self._core._ingest_v1_event(event)
            return self._status_ack_from_v1(ack)
        except Exception as exc:
            log.exception("ReportArtifactUploadStatus(v2) failed for agent_id=%s run_id=%s", agent_id, run_id)
            context.abort(grpc.StatusCode.INTERNAL, f"ReportArtifactUploadStatus(v2) failed: {exc}")

    def UploadArtifact(self, request_iterator, context):
        try:
            first = next(request_iterator)
        except StopIteration:
            ARTIFACT_UPLOADS_TOTAL.labels(status="rejected").inc()
            return fleet_gateway_v2_pb2.UploadArtifactResponse(
                status=fleet_gateway_v2_pb2.UploadArtifactResponse.REJECTED,
                reason="empty upload stream",
            )

        if first.WhichOneof("payload") != "header":
            ARTIFACT_UPLOADS_TOTAL.labels(status="rejected").inc()
            return fleet_gateway_v2_pb2.UploadArtifactResponse(
                status=fleet_gateway_v2_pb2.UploadArtifactResponse.REJECTED,
                reason="first upload stream message must be header",
            )

        header = first.header
        exp_id = parse_experiment_numeric_id(header.experiment_id)
        run_id = normalize_string(header.run_id)
        agent_id = normalize_device_id(header.agent_id)
        artifact_name = normalize_string(header.artifact_name)
        if exp_id is None or not run_id or not agent_id or not artifact_name:
            ARTIFACT_UPLOADS_TOTAL.labels(status="rejected").inc()
            return fleet_gateway_v2_pb2.UploadArtifactResponse(
                status=fleet_gateway_v2_pb2.UploadArtifactResponse.REJECTED,
                reason="experiment_id, run_id, agent_id, and artifact_name are required",
            )

        max_bytes = max(1024, int(self._cfg.get("artifact_max_bytes", 20 * 1024 * 1024)))
        chunks = []
        size_bytes = 0
        for msg in request_iterator:
            payload_kind = msg.WhichOneof("payload")
            if payload_kind == "header":
                ARTIFACT_UPLOADS_TOTAL.labels(status="rejected").inc()
                return fleet_gateway_v2_pb2.UploadArtifactResponse(
                    status=fleet_gateway_v2_pb2.UploadArtifactResponse.REJECTED,
                    reason="duplicate upload header",
                )
            if payload_kind != "content":
                continue
            raw_chunk = bytes(msg.content)
            size_bytes += len(raw_chunk)
            if size_bytes > max_bytes:
                ARTIFACT_UPLOADS_TOTAL.labels(status="rejected").inc()
                return fleet_gateway_v2_pb2.UploadArtifactResponse(
                    status=fleet_gateway_v2_pb2.UploadArtifactResponse.REJECTED,
                    reason=f"artifact too large ({size_bytes} > {max_bytes})",
                )
            chunks.append(raw_chunk)

        raw = b"".join(chunks)
        expected_size = int(header.size_bytes or 0)
        if expected_size > 0 and expected_size != len(raw):
            ARTIFACT_UPLOADS_TOTAL.labels(status="rejected").inc()
            return fleet_gateway_v2_pb2.UploadArtifactResponse(
                status=fleet_gateway_v2_pb2.UploadArtifactResponse.REJECTED,
                reason=f"size mismatch ({len(raw)} != {expected_size})",
            )

        actual_sha256 = hashlib.sha256(raw).hexdigest()
        expected_sha256 = normalize_string(header.sha256)
        if expected_sha256 and expected_sha256 != actual_sha256:
            ARTIFACT_UPLOADS_TOTAL.labels(status="rejected").inc()
            return fleet_gateway_v2_pb2.UploadArtifactResponse(
                status=fleet_gateway_v2_pb2.UploadArtifactResponse.REJECTED,
                reason="sha256 mismatch",
                sha256=actual_sha256,
                size_bytes=len(raw),
            )

        mime = normalize_string(header.mime) or "application/octet-stream"
        upload_phase = normalize_string(header.upload_phase) or "grpc_upload"
        comment = normalize_string(header.comment)
        if not comment:
            comment = (
                f"fleet-v3 run={run_id} agent={agent_id} artifact={artifact_name} "
                f"sha256={actual_sha256} size={len(raw)}"
            )

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
                return fleet_gateway_v2_pb2.UploadArtifactResponse(
                    status=fleet_gateway_v2_pb2.UploadArtifactResponse.REJECTED,
                    reason=f"server finalize failed: {exc}",
                )

            summary_upload = finalized.get("summary_upload") if isinstance(finalized.get("summary_upload"), dict) else {}
            ARTIFACT_UPLOADS_TOTAL.labels(status="uploaded").inc()
            return fleet_gateway_v2_pb2.UploadArtifactResponse(
                status=fleet_gateway_v2_pb2.UploadArtifactResponse.ACCEPTED,
                reason="artifact uploaded and run finalized",
                artifact_uri=normalize_string(summary_upload.get("artifact_uri")),
                location=normalize_string(summary_upload.get("location")),
                stored_artifact_name=normalize_string(summary_upload.get("stored_artifact_name")),
                sha256=actual_sha256,
                size_bytes=len(raw),
                server_finalized=True,
                bundled_artifacts_count=int(finalized.get("bundled_artifacts_count", 0) or 0),
                raw_artifacts_count=int(finalized.get("raw_artifacts_count", 0) or 0),
            )

        meta = store_server_spooled_artifact(
            experiment_id=exp_id,
            run_id=run_id,
            agent_id=agent_id,
            artifact_name=artifact_name,
            raw=raw,
            mime=mime,
            comment=comment,
            upload_phase=upload_phase,
            sha256_value=expected_sha256,
        )
        ARTIFACT_UPLOADS_TOTAL.labels(status="spooled").inc()
        spool_uri = normalize_string(meta.get("server_spool_uri"))
        return fleet_gateway_v2_pb2.UploadArtifactResponse(
            status=fleet_gateway_v2_pb2.UploadArtifactResponse.ACCEPTED,
            reason="artifact spooled",
            artifact_uri=spool_uri,
            location=spool_uri,
            stored_artifact_name=normalize_string(meta.get("stored_spool_name")),
            sha256=normalize_string(meta.get("actual_sha256") or meta.get("sha256")),
            size_bytes=int(meta.get("size_bytes", len(raw)) or len(raw)),
        )

    def AckPrepared(self, request, context):
        agent_id = normalize_device_id(request.agent_id)
        preparation_id = normalize_string(request.preparation_id)
        if not agent_id or not preparation_id:
            self._set_device_runtime_state(
                agent_id,
                "PREPARE_REJECTED",
                details={
                    "last_prepare": {
                        "status": "REJECTED",
                        "reason": "agent_id and preparation_id are required",
                        "preparation_id": preparation_id,
                        "policy_id": normalize_string(request.policy_id),
                        "experiment_id": normalize_string(request.experiment_id),
                        "at": utc_now_iso(),
                    }
                },
            )
            return fleet_gateway_v2_pb2.AckPreparedResponse(
                status=fleet_gateway_v2_pb2.AckPreparedResponse.REJECTED,
                reason="agent_id and preparation_id are required",
            )
        if self._state.has_preparation(preparation_id):
            self._set_device_runtime_state(
                agent_id,
                "PREPARED",
                details={
                    "last_prepare": {
                        "status": "ACCEPTED",
                        "reason": "duplicate preparation acknowledged",
                        "preparation_id": preparation_id,
                        "policy_id": normalize_string(request.policy_id),
                        "experiment_id": normalize_string(request.experiment_id),
                        "mode": mode_enum_to_text(int(request.mode)),
                        "control_plane_iface": normalize_string(request.control_plane_iface),
                        "route_verified": bool(request.route_verified),
                        "at": utc_now_iso(),
                    }
                },
            )
            return fleet_gateway_v2_pb2.AckPreparedResponse(
                status=fleet_gateway_v2_pb2.AckPreparedResponse.ACCEPTED,
                reason="duplicate preparation acknowledged",
            )

        requested_mode = int(request.mode)
        iface = normalize_string(request.control_plane_iface).lower()
        if requested_mode == fleet_gateway_v2_pb2.DUAL_NIC:
            if not request.route_verified:
                self._set_device_runtime_state(
                    agent_id,
                    "PREPARE_REJECTED",
                    details={
                        "last_prepare": {
                            "status": "REJECTED",
                            "reason": "DUAL_NIC requires route_verified=true",
                            "preparation_id": preparation_id,
                            "policy_id": normalize_string(request.policy_id),
                            "experiment_id": normalize_string(request.experiment_id),
                            "mode": mode_enum_to_text(requested_mode),
                            "control_plane_iface": normalize_string(request.control_plane_iface),
                            "route_verified": bool(request.route_verified),
                            "at": utc_now_iso(),
                        }
                    },
                )
                return fleet_gateway_v2_pb2.AckPreparedResponse(
                    status=fleet_gateway_v2_pb2.AckPreparedResponse.REJECTED,
                    reason="DUAL_NIC requires route_verified=true",
                )
            if iface and not iface.startswith("eth"):
                self._set_device_runtime_state(
                    agent_id,
                    "PREPARE_REJECTED",
                    details={
                        "last_prepare": {
                            "status": "REJECTED",
                            "reason": "DUAL_NIC requires ethernet control_plane_iface",
                            "preparation_id": preparation_id,
                            "policy_id": normalize_string(request.policy_id),
                            "experiment_id": normalize_string(request.experiment_id),
                            "mode": mode_enum_to_text(requested_mode),
                            "control_plane_iface": normalize_string(request.control_plane_iface),
                            "route_verified": bool(request.route_verified),
                            "at": utc_now_iso(),
                        }
                    },
                )
                return fleet_gateway_v2_pb2.AckPreparedResponse(
                    status=fleet_gateway_v2_pb2.AckPreparedResponse.REJECTED,
                    reason="DUAL_NIC requires ethernet control_plane_iface",
                )

        policy_ctx = self._resolve_policy_ctx(agent_id)
        if not policy_ctx:
            self._set_device_runtime_state(
                agent_id,
                "PREPARE_REJECTED",
                details={
                    "last_prepare": {
                        "status": "REJECTED",
                        "reason": "no policy assigned",
                        "preparation_id": preparation_id,
                        "policy_id": normalize_string(request.policy_id),
                        "experiment_id": normalize_string(request.experiment_id),
                        "mode": mode_enum_to_text(requested_mode),
                        "control_plane_iface": normalize_string(request.control_plane_iface),
                        "route_verified": bool(request.route_verified),
                        "at": utc_now_iso(),
                    }
                },
            )
            return fleet_gateway_v2_pb2.AckPreparedResponse(
                status=fleet_gateway_v2_pb2.AckPreparedResponse.REJECTED,
                reason="no policy assigned",
            )
        if normalize_string(request.policy_id) != normalize_string(policy_ctx["policy_revision"]):
            self._set_device_runtime_state(
                agent_id,
                "PREPARE_REJECTED",
                details={
                    "last_prepare": {
                        "status": "REJECTED",
                        "reason": "policy_id mismatch",
                        "preparation_id": preparation_id,
                        "policy_id": normalize_string(request.policy_id),
                        "expected_policy_id": normalize_string(policy_ctx["policy_revision"]),
                        "experiment_id": normalize_string(request.experiment_id),
                        "mode": mode_enum_to_text(requested_mode),
                        "control_plane_iface": normalize_string(request.control_plane_iface),
                        "route_verified": bool(request.route_verified),
                        "at": utc_now_iso(),
                    }
                },
            )
            return fleet_gateway_v2_pb2.AckPreparedResponse(
                status=fleet_gateway_v2_pb2.AckPreparedResponse.REJECTED,
                reason="policy_id mismatch",
            )

        self._state.mark_preparation(preparation_id)
        self._set_device_runtime_state(
            agent_id,
            "PREPARED",
            details={
                "last_prepare": {
                    "status": "ACCEPTED",
                    "reason": "prepared accepted",
                    "preparation_id": preparation_id,
                    "policy_id": normalize_string(request.policy_id),
                    "experiment_id": normalize_string(request.experiment_id),
                    "mode": mode_enum_to_text(requested_mode),
                    "control_plane_iface": normalize_string(request.control_plane_iface),
                    "route_verified": bool(request.route_verified),
                    "at": utc_now_iso(),
                }
            },
        )
        return fleet_gateway_v2_pb2.AckPreparedResponse(
            status=fleet_gateway_v2_pb2.AckPreparedResponse.ACCEPTED,
            reason="prepared accepted",
        )

    def PublishReport(self, request, context):
        report = request.report
        agent_id = normalize_device_id(request.agent_id or report.agent_id)
        run_id = normalize_string(report.run_id)
        metrics_upload_state = self._report_metrics_upload_state(report)
        if not agent_id or not run_id:
            self._set_device_runtime_state(
                agent_id,
                "REPORT_REJECTED",
                details={
                    "last_report": {
                        "status": "REJECTED",
                        "reason": "agent_id and report.run_id are required",
                        "run_id": run_id,
                        "policy_id": normalize_string(report.policy_id),
                        "experiment_id": normalize_string(report.experiment_id),
                        "metrics_upload_state": metrics_upload_state,
                        "at": utc_now_iso(),
                    }
                },
            )
            return fleet_gateway_v2_pb2.PublishReportResponse(
                status=fleet_gateway_v2_pb2.PublishReportResponse.REJECTED,
                reason="agent_id and report.run_id are required",
            )

        self._set_device_runtime_state(
            agent_id,
            "REPORT_RECEIVED",
            details={
                "last_report": {
                    "status": "RECEIVED",
                    "reason": "report received",
                    "run_id": run_id,
                    "policy_id": normalize_string(report.policy_id),
                    "experiment_id": normalize_string(report.experiment_id),
                    "report_status": normalize_string(report.status),
                    "metrics_upload_state": metrics_upload_state,
                    "at": utc_now_iso(),
                }
            },
        )

        report_key = self._report_key(report, agent_id)
        if self._state.has_report(report_key):
            self._set_device_runtime_state(
                agent_id,
                "REPORT_DUPLICATE",
                details={
                    "last_report": {
                        "status": "DUPLICATE",
                        "reason": "duplicate report ignored",
                        "run_id": run_id,
                        "policy_id": normalize_string(report.policy_id),
                        "experiment_id": normalize_string(report.experiment_id),
                        "report_status": normalize_string(report.status),
                        "metrics_upload_state": metrics_upload_state,
                        "at": utc_now_iso(),
                    }
                },
            )
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
            self._set_device_runtime_state(
                agent_id,
                "REPORT_REJECTED",
                details={
                    "last_report": {
                        "status": "REJECTED",
                        "reason": "one or more events failed to ingest",
                        "run_id": run_id,
                        "policy_id": normalize_string(report.policy_id),
                        "experiment_id": normalize_string(report.experiment_id),
                        "report_status": normalize_string(report.status),
                        "metrics_upload_state": metrics_upload_state,
                        "at": utc_now_iso(),
                    }
                },
            )
            return fleet_gateway_v2_pb2.PublishReportResponse(
                status=fleet_gateway_v2_pb2.PublishReportResponse.REJECTED,
                reason="one or more events failed to ingest",
            )

        self._state.mark_report(report_key)
        self._record_report_metrics(report, agent_id)
        self._set_device_runtime_state(
            agent_id,
            "REPORT_ACCEPTED",
            details={
                "last_report": {
                    "status": "ACCEPTED",
                    "reason": "report accepted",
                    "run_id": run_id,
                    "policy_id": normalize_string(report.policy_id),
                    "experiment_id": normalize_string(report.experiment_id),
                    "report_status": normalize_string(report.status),
                    "metrics_upload_state": metrics_upload_state,
                    "events_count": len(report.events),
                    "artifacts_count": len(report.artifacts),
                    "at": utc_now_iso(),
                }
            },
        )
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
