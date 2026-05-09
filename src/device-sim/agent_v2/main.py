from .core import *  # noqa: F401,F403
from .collectors import *  # noqa: F401,F403
from .sinks import *  # noqa: F401,F403
from . import core as core_mod
from . import sinks as sinks_mod


def _raw_command_artifact_stem(cmd_id: str) -> str:
    token = sanitize_name(cmd_id, "command")
    lowered = token.lower()
    if "rf-env" in lowered or ("environment" in lowered and "snapshot" in lowered):
        return "device-rf-environment-snapshot"
    return f"command-{token}-raw-output"


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
    policy_rpc_timeout_s = parse_timeout_seconds("POLICY_RPC_TIMEOUT_S", max(15, control_rpc_timeout_s))
    ack_prepared_rpc_timeout_s = parse_timeout_seconds("ACK_PREPARED_RPC_TIMEOUT_S", control_rpc_timeout_s)
    data_root = Path(os.environ.get("DATA_ROOT", "./data"))
    sent_retention_days = int(os.environ.get("SENT_RETENTION_DAYS", "14"))
    global_upload_during_measure = parse_bool(os.environ.get("ARTIFACT_UPLOAD_DURING_MEASURE"), False)
    global_artifact_upload_target = sinks_mod._artifact_upload_target(os.environ.get("ARTIFACT_UPLOAD_TARGET"))
    enable_command_status_reports = parse_bool(os.environ.get("ENABLE_COMMAND_STATUS_REPORTS"), True)
    enable_upload_status_reports = parse_bool(os.environ.get("ENABLE_UPLOAD_STATUS_REPORTS"), True)
    run_artifact_soft_limit_bytes = max(0, parse_int(os.environ.get("RUN_ARTIFACT_SOFT_LIMIT_BYTES"), 0))
    separate_measure_and_report = parse_bool(os.environ.get("SEPARATE_MEASURE_AND_REPORT"), False)
    restart_control_plane_before_send = parse_bool(
        os.environ.get("RESTART_CONTROL_PLANE_BEFORE_REPORT"),
        separate_measure_and_report,
    )
    restart_control_plane_iface = normalize(os.environ.get("CONTROL_PLANE_RESTART_IFACE") or control_plane_iface)
    restart_control_plane_down_s = max(0, parse_int(os.environ.get("CONTROL_PLANE_RESTART_DOWN_S"), 2))
    restart_control_plane_wait_s = max(0, parse_int(os.environ.get("CONTROL_PLANE_RESTART_WAIT_S"), 45))
    restart_control_plane_cmd = normalize(os.environ.get("CONTROL_PLANE_RESTART_CMD"))
    allow_wifi_control_plane_restart = parse_bool(os.environ.get("ALLOW_WIFI_CONTROL_PLANE_RESTART"), False)
    single_radio_mode = parse_bool(os.environ.get("SINGLE_RADIO_MODE"), False)
    single_radio_wifi_profile = normalize(os.environ.get("SINGLE_RADIO_WIFI_PROFILE") or "tplink24")
    single_radio_recovery_retries = max(1, parse_int(os.environ.get("SINGLE_RADIO_RECOVERY_RETRIES"), 3))
    single_radio_recovery_route_wait_s = max(
        5,
        parse_int(os.environ.get("SINGLE_RADIO_RECOVERY_ROUTE_WAIT_S"), restart_control_plane_wait_s),
    )
    single_radio_recovery_cmd = normalize(os.environ.get("SINGLE_RADIO_RECOVERY_CMD") or restart_control_plane_cmd)
    single_radio_route_gate = parse_bool(os.environ.get("SINGLE_RADIO_ROUTE_GATE"), single_radio_mode)
    single_radio_route_health_timeout_s = max(
        1,
        parse_int(os.environ.get("SINGLE_RADIO_ROUTE_HEALTH_TIMEOUT_S"), 5),
    )
    single_radio_reboot_on_recovery_fail = parse_bool(
        os.environ.get("SINGLE_RADIO_REBOOT_ON_RECOVERY_FAIL"),
        False,
    )
    single_radio_csi_kill_patterns = normalize(os.environ.get("CSI_KILL_PATTERNS"))
    if single_radio_mode:
        separate_measure_and_report = True
        restart_control_plane_before_send = True

    fm_target_for_route = os.environ.get("FLEET_MANAGER_ROUTE_TARGET", fleet_host)
    base_upload_cfg = core_mod._upload_window_config_from_env()
    active_upload_cfg = dict(base_upload_cfg)

    store = RunStore(data_root, sent_retention_days=sent_retention_days)
    channel = grpc.insecure_channel(target)
    stub = fleet_gateway_v2_pb2_grpc.FleetManagerStub(channel)

    def _is_unimplemented_rpc(exc: grpc.RpcError) -> bool:
        return exc.code() == grpc.StatusCode.UNIMPLEMENTED

    def report_command_status(
        *,
        event_id: str,
        run_id: str,
        experiment_id: str,
        policy_id: str,
        command_id: str,
        command_type: str,
        stage: int,
        message: str = "",
        metrics: dict[str, str] | None = None,
        duration_ms: int = 0,
        exit_code: int = 0,
        measure_type: str = "",
    ) -> None:
        if not enable_command_status_reports:
            return
        payload_metrics = {
            normalize(k): normalize(v)
            for k, v in (metrics or {}).items()
            if normalize(k)
        }
        try:
            ack = stub.ReportCommandStatus(
                fleet_gateway_v2_pb2.ReportCommandStatusRequest(
                    event_id=normalize(event_id),
                    agent_id=agent_id,
                    run_id=normalize(run_id),
                    experiment_id=normalize(experiment_id),
                    policy_id=normalize(policy_id),
                    command_id=normalize(command_id),
                    command_type=normalize(command_type),
                    measure_type=normalize(measure_type),
                    stage=int(stage),
                    exit_code=int(exit_code),
                    duration_ms=max(0, int(duration_ms)),
                    message=normalize(message),
                    metrics=payload_metrics,
                ),
                timeout=control_rpc_timeout_s,
            )
            if not bool(getattr(ack, "ok", False)):
                log.warning(
                    "ReportCommandStatus rejected run_id=%s command_id=%s reason=%s",
                    normalize(run_id),
                    normalize(command_id),
                    normalize(getattr(ack, "reason", "")),
                )
        except grpc.RpcError as exc:
            if _is_unimplemented_rpc(exc):
                log.debug("ReportCommandStatus not implemented on server; continuing")
                return
            log.warning(
                "ReportCommandStatus failed run_id=%s command_id=%s: %s",
                normalize(run_id),
                normalize(command_id),
                exc,
            )

    def report_upload_status_from_report(run_id: str, report: fleet_gateway_v2_pb2.Report, metrics_state: str, reason: str) -> None:
        if not enable_upload_status_reports:
            return
        token = normalize(metrics_state).upper()
        status_value = fleet_gateway_v2_pb2.UPLOAD_PENDING
        if token == "UPLOADED":
            status_value = fleet_gateway_v2_pb2.UPLOAD_ACK
        elif token in {"UPLOAD_FAILED", "ERROR", "FAILED"}:
            status_value = fleet_gateway_v2_pb2.UPLOAD_ERROR
        elif token == "PENDING":
            status_value = fleet_gateway_v2_pb2.UPLOAD_PENDING

        try:
            ack = stub.ReportUploadStatus(
                fleet_gateway_v2_pb2.ReportUploadStatusRequest(
                    event_id=f"{normalize(run_id)}:upload:metrics:{token.lower() or 'pending'}",
                    agent_id=agent_id,
                    run_id=normalize(run_id),
                    experiment_id=normalize(report.experiment_id),
                    policy_id=normalize(report.policy_id),
                    payload_kind=fleet_gateway_v2_pb2.METRICS_LOGS,
                    status=status_value,
                    message=normalize(reason) or f"metrics upload status={token or 'PENDING'}",
                    metrics={
                        "metrics_upload_state": token or "PENDING",
                        "upload_hook_reason": normalize(reason),
                    },
                ),
                timeout=control_rpc_timeout_s,
            )
            if not bool(getattr(ack, "ok", False)):
                log.warning(
                    "ReportUploadStatus rejected run_id=%s reason=%s",
                    normalize(run_id),
                    normalize(getattr(ack, "reason", "")),
                )
        except grpc.RpcError as exc:
            if _is_unimplemented_rpc(exc):
                log.debug("ReportUploadStatus not implemented on server; continuing")
                return
            log.warning("ReportUploadStatus failed run_id=%s: %s", normalize(run_id), exc)

    def report_artifact_upload_status_from_report(
        report: fleet_gateway_v2_pb2.Report,
        artifact: fleet_gateway_v2_pb2.ArtifactRef,
        artifact_status: str,
        reason: str,
    ) -> None:
        if not enable_upload_status_reports:
            return
        token = normalize(artifact_status).lower()
        status_value = fleet_gateway_v2_pb2.UPLOAD_PENDING
        if token in {"uploaded", "spooled"}:
            status_value = fleet_gateway_v2_pb2.UPLOAD_ACK
        elif token == "failed":
            status_value = fleet_gateway_v2_pb2.UPLOAD_ERROR
        elif token == "skipped":
            status_value = fleet_gateway_v2_pb2.UPLOAD_SKIPPED

        run_id = normalize(report.run_id)
        artifact_name = normalize(artifact.name) or "artifact"
        event_id = f"{run_id}:artifact:{artifact_name}:{token or 'pending'}"
        try:
            ack = stub.ReportArtifactUploadStatus(
                fleet_gateway_v2_pb2.ReportArtifactUploadStatusRequest(
                    event_id=event_id,
                    agent_id=agent_id,
                    run_id=run_id,
                    experiment_id=normalize(report.experiment_id),
                    policy_id=normalize(report.policy_id),
                    artifact_name=artifact_name,
                    artifact_uri=normalize(artifact.uri),
                    artifact_size_bytes=max(0, int(artifact.size_bytes or 0)),
                    artifact_sha256=normalize(artifact.sha256),
                    status=status_value,
                    message=normalize(reason) or f"artifact upload status={token or 'pending'}",
                    metrics={
                        "artifact_upload_status": token or "pending",
                        "artifact_upload_reason": normalize(reason),
                    },
                ),
                timeout=control_rpc_timeout_s,
            )
            if not bool(getattr(ack, "ok", False)):
                log.warning(
                    "ReportArtifactUploadStatus rejected run_id=%s artifact=%s reason=%s",
                    run_id,
                    artifact_name,
                    normalize(getattr(ack, "reason", "")),
                )
        except grpc.RpcError as exc:
            if _is_unimplemented_rpc(exc):
                log.debug("ReportArtifactUploadStatus not implemented on server; continuing")
                return
            log.warning(
                "ReportArtifactUploadStatus failed run_id=%s artifact=%s: %s",
                run_id,
                artifact_name,
                exc,
            )

    def flush_pending_reports_guarded(reason: str) -> set[str]:
        upload_allowed, upload_reason = core_mod._upload_window_send_allowed(active_upload_cfg, agent_id)
        if not upload_allowed:
            log.info(
                "Skip flush_pending_reports (%s): upload gate=%s",
                normalize(reason),
                normalize(upload_reason),
            )
            return set()

        if single_radio_route_gate:
            ready, route_iface = wait_for_route_ready(
                fm_target_for_route,
                expected_iface=restart_control_plane_iface if restart_control_plane_iface else "",
                timeout_s=single_radio_route_health_timeout_s,
                require_ipv4=True,
            )
            if not ready:
                log.warning(
                    "Skip flush_pending_reports (%s): route not ready target=%s expected_iface=%s last_iface=%s",
                    reason,
                    normalize(fm_target_for_route),
                    normalize(restart_control_plane_iface),
                    normalize(route_iface),
                )
                return set()
        metrics_upload_state = "UPLOADED"
        remote_write_on_ok, remote_write_on_reason = core_mod._set_prom_remote_write_mode(
            active_upload_cfg,
            enabled=True,
            reason=reason,
        )
        if remote_write_on_ok is False:
            metrics_upload_state = "UPLOAD_FAILED"
        elif remote_write_on_ok is None:
            metrics_upload_state = "UPLOADED"

        if remote_write_on_ok in {True, False}:
            log.info(
                "remote_write_on (%s): %s",
                normalize(reason),
                normalize(remote_write_on_reason),
            )
        try:
            return flush_pending_reports(
                stub,
                agent_id,
                store,
                metrics_upload_state=metrics_upload_state,
                report_upload_status_hook=report_upload_status_from_report,
                artifact_upload_status_hook=report_artifact_upload_status_from_report,
            )
        finally:
            remote_write_off_ok, remote_write_off_reason = core_mod._set_prom_remote_write_mode(
                active_upload_cfg,
                enabled=False,
                reason=reason,
            )
            if remote_write_off_ok in {True, False}:
                log.info(
                    "remote_write_off (%s): %s",
                    normalize(reason),
                    normalize(remote_write_off_reason),
                )

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
    cached_policy: fleet_gateway_v2_pb2.Policy | None = None
    cycles = 0

    # Try to deliver any runs persisted from previous process/network interruptions.
    flush_pending_reports_guarded("startup")

    while max_cycles <= 0 or cycles < max_cycles:
        cycles += 1

        policy_resp = stub.GetPolicy(
            fleet_gateway_v2_pb2.GetPolicyRequest(
                agent_id=agent_id,
                last_policy_id=last_policy_id,
            ),
            timeout=policy_rpc_timeout_s,
        )
        if policy_resp.status == fleet_gateway_v2_pb2.GetPolicyResponse.NO_WORK:
            log.info("No assignment/work")
            flush_pending_reports_guarded("no-work")
            if reached_max_cycles(cycles, max_cycles):
                break
            time.sleep(poll)
            continue
        log.info(
            "Assignment context: experiment_id=%s policy_id=%s window=%s..%s",
            normalize(policy_resp.experiment_id),
            normalize(policy_resp.policy_id),
            ts_to_iso(policy_resp.measurement_from),
            ts_to_iso(policy_resp.measurement_to),
        )
        policy: fleet_gateway_v2_pb2.Policy | None = None
        if policy_resp.status == fleet_gateway_v2_pb2.GetPolicyResponse.NOT_MODIFIED:
            if normalize(policy_resp.policy_id):
                last_policy_id = normalize(policy_resp.policy_id)
            if len(policy_resp.policy.command_groups) > 0:
                policy = policy_resp.policy
                log.info("Policy not modified: server returned executable policy_id=%s", last_policy_id)
            elif cached_policy is not None and normalize(cached_policy.policy_id) == normalize(last_policy_id):
                policy = fleet_gateway_v2_pb2.Policy()
                policy.CopyFrom(cached_policy)
                log.info("Policy not modified: using locally cached policy_id=%s", last_policy_id)
            else:
                log.warning("Policy not modified but no cached policy body is available; skipping execution")
                flush_pending_reports_guarded("policy-not-modified-no-cache")
                if reached_max_cycles(cycles, max_cycles):
                    break
                time.sleep(poll)
                continue
        elif policy_resp.status != fleet_gateway_v2_pb2.GetPolicyResponse.OK:
            log.info("No policy available")
            flush_pending_reports_guarded("policy-unavailable")
            if reached_max_cycles(cycles, max_cycles):
                break
            time.sleep(poll)
            continue
        else:
            policy = policy_resp.policy
            cached_policy = fleet_gateway_v2_pb2.Policy()
            cached_policy.CopyFrom(policy)

        if policy is None:
            log.warning("No executable policy payload after policy resolution; skipping cycle")
            flush_pending_reports_guarded("policy-empty")
            if reached_max_cycles(cycles, max_cycles):
                break
            time.sleep(poll)
            continue

        policy_upload_overrides = core_mod._extract_policy_upload_window_overrides(policy)
        active_upload_cfg = core_mod._apply_upload_window_overrides(base_upload_cfg, policy_upload_overrides)
        if policy_upload_overrides:
            upload_from = active_upload_cfg.get("upload_window_from")
            upload_to = active_upload_cfg.get("upload_window_to")
            log.info(
                "Applied upload window config from policy: from=%s to=%s slots=%s jitter_s=%s required=%s",
                upload_from.isoformat().replace("+00:00", "Z") if isinstance(upload_from, datetime) else "",
                upload_to.isoformat().replace("+00:00", "Z") if isinstance(upload_to, datetime) else "",
                int(active_upload_cfg.get("upload_slot_count", 1) or 1),
                int(active_upload_cfg.get("upload_slot_jitter_s", 0) or 0),
                "true" if bool(active_upload_cfg.get("upload_window_required", False)) else "false",
            )
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
            flush_pending_reports_guarded("ack-prepared-rejected")
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
        core_mod._set_prom_remote_write_mode(active_upload_cfg, enabled=False, reason="measure-window")

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
        command_sink_metrics_sent = 0
        command_sink_events_sent = 0
        command_sink_artifacts_uploaded = 0
        command_sink_artifacts_upload_failed = 0
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
                measure_type = ""
                if int(cmd.type) == int(fleet_gateway_v2_pb2.WIFI_SCAN):
                    measure_type = "WIFI"
                elif int(cmd.type) == int(fleet_gateway_v2_pb2.BLE_SCAN):
                    measure_type = "BLE"
                elif int(cmd.type) == int(fleet_gateway_v2_pb2.CAPTURE_CSI):
                    measure_type = "CSI"
                cmd_argv = [normalize(a) for a in getattr(cmd, "argv", []) if normalize(a)]
                cmd_env = {normalize(k): normalize(v) for k, v in getattr(cmd, "env", {}).items() if normalize(k)}
                upload_during_measure = parse_bool(
                    cmd_env.get("ARTIFACT_UPLOAD_DURING_MEASURE"),
                    global_upload_during_measure,
                )
                artifact_upload_target = sinks_mod._artifact_upload_target(
                    cmd_env.get("ARTIFACT_UPLOAD_TARGET") or global_artifact_upload_target
                )
                command_start_message = cmdline or (" ".join(cmd_argv) if cmd_argv else cmd_type_name)
                command_start_event_id = f"{run_id}:{cmd_id}:start"

                record(
                    fleet_gateway_v2_pb2.Event(
                        event_id=command_start_event_id,
                        run_id=run_id,
                        experiment_id=policy.experiment_id,
                        policy_id=policy.policy_id,
                        agent_id=agent_id,
                        timestamp=now_ts(),
                        type=fleet_gateway_v2_pb2.COMMAND_STARTED,
                        group_id=normalize(group.id),
                        command_id=cmd_id,
                        message=command_start_message,
                    )
                )
                report_command_status(
                    event_id=command_start_event_id,
                    run_id=run_id,
                    experiment_id=policy.experiment_id,
                    policy_id=policy.policy_id,
                    command_id=cmd_id,
                    command_type=cmd_type_name,
                    stage=fleet_gateway_v2_pb2.COMMAND_STATUS_STAGE_STARTED,
                    message=command_start_message,
                    metrics={"group_id": normalize(group.id)},
                    measure_type=measure_type,
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
                            extra_artifacts.append(
                                store.write_text_artifact(
                                    run_id,
                                    _raw_command_artifact_stem(cmd_id),
                                    full,
                                    include_timestamp=False,
                                )
                            )
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

                device_status = core_mod._collect_device_status_metrics()
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

                command_end_event_id = f"{run_id}:{cmd_id}:end"
                command_end_stage = (
                    fleet_gateway_v2_pb2.COMMAND_STATUS_STAGE_FINISHED
                    if int(exit_code) == 0
                    else fleet_gateway_v2_pb2.COMMAND_STATUS_STAGE_FAILED
                )
                record(
                    fleet_gateway_v2_pb2.Event(
                        event_id=command_end_event_id,
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
                report_command_status(
                    event_id=command_end_event_id,
                    run_id=run_id,
                    experiment_id=policy.experiment_id,
                    policy_id=policy.policy_id,
                    command_id=cmd_id,
                    command_type=cmd_type_name,
                    stage=command_end_stage,
                    message=message,
                    metrics=event_metrics,
                    duration_ms=max(0, int(duration_ms)),
                    exit_code=int(exit_code),
                    measure_type=measure_type,
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

                sink_publish_stats = publish_command_data_to_sinks(
                    cmd_env=cmd_env,
                    run_id=run_id,
                    experiment_id=policy.experiment_id,
                    policy_id=policy.policy_id,
                    group_id=normalize(group.id),
                    command_id=cmd_id,
                    command_type=cmd_type_name,
                    agent_id=agent_id,
                    exit_code=exit_code,
                    duration_ms=max(0, int(duration_ms)),
                    message=message,
                    event_metrics=event_metrics,
                    command_artifacts=command_artifacts,
                    store=store,
                    data_root=data_root,
                )
                command_sink_metrics_sent += max(0, int(sink_publish_stats.get("metrics_sent", 0)))
                command_sink_events_sent += max(0, int(sink_publish_stats.get("events_sent", 0)))
                command_sink_artifacts_uploaded += max(0, int(sink_publish_stats.get("artifacts_uploaded", 0)))
                command_sink_artifacts_upload_failed += max(0, int(sink_publish_stats.get("artifacts_upload_failed", 0)))

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

        merged_artifacts_count = 0
        server_side_bundling = sinks_mod._server_side_artifact_bundling_enabled()
        if sinks_mod._merge_text_artifacts_enabled() and not server_side_bundling:
            artifacts, merged_artifacts_count = merge_text_artifacts_for_run(run_id, artifacts, store)

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
                    "command_sink_metrics_sent": command_sink_metrics_sent,
                    "command_sink_events_sent": command_sink_events_sent,
                    "command_sink_artifacts_uploaded": command_sink_artifacts_uploaded,
                    "command_sink_artifacts_upload_failed": command_sink_artifacts_upload_failed,
                    "merged_artifacts_count": merged_artifacts_count,
                    "server_side_artifact_bundling": 1 if server_side_bundling else 0,
                    "run_storage_pressure_hits": run_storage_pressure_hits,
                    "metrics_destination": {
                        "primary": "mimir",
                        "path": "agent/fleet-http or report -> Fleet /metrics -> Prometheus remote_write -> Mimir -> Grafana",
                        "note": "Numeric telemetry values are intentionally omitted from this artifact; query Mimir/Grafana for metrics.",
                    },
                    "generated_at": now_utc().isoformat().replace("+00:00", "Z"),
                },
            )
            artifacts.append(summary_artifact)
            if global_upload_during_measure and not server_side_bundling:
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
            "command_sink_metrics_sent": str(command_sink_metrics_sent),
            "command_sink_events_sent": str(command_sink_events_sent),
            "command_sink_artifacts_uploaded": str(command_sink_artifacts_uploaded),
            "command_sink_artifacts_upload_failed": str(command_sink_artifacts_upload_failed),
            "merged_artifacts_count": str(merged_artifacts_count),
            "server_side_artifact_bundling": "1" if server_side_bundling else "0",
            "run_storage_pressure_hits": str(run_storage_pressure_hits),
            "wifi_ap_total": str(wifi_ap_total),
            "ble_adv_total": str(ble_adv_total),
            "csi_frames_total": str(csi_frames_total),
            "metrics_upload_state": "PENDING",
            "spool_state": "pending_upload",
            "measure_report_separated": "1" if separate_measure_and_report else "0",
            "reconnect_before_report": "1" if restart_control_plane_before_send else "0",
            "single_radio_mode": "1" if single_radio_mode else "0",
            "single_radio_route_gate": "1" if single_radio_route_gate else "0",
            "fleet_interface_version": "v3",
        }
        upload_from = active_upload_cfg.get("upload_window_from")
        upload_to = active_upload_cfg.get("upload_window_to")
        if isinstance(upload_from, datetime):
            summary_metrics["upload_window_from"] = upload_from.isoformat().replace("+00:00", "Z")
        if isinstance(upload_to, datetime):
            summary_metrics["upload_window_to"] = upload_to.isoformat().replace("+00:00", "Z")
        summary_metrics["upload_slot_count"] = str(int(active_upload_cfg.get("upload_slot_count", 1) or 1))
        summary_metrics["upload_slot_jitter_s"] = str(int(active_upload_cfg.get("upload_slot_jitter_s", 0) or 0))
        summary_metrics["upload_window_required"] = "1" if bool(active_upload_cfg.get("upload_window_required", False)) else "0"
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

        reconnect_ok = True
        if restart_control_plane_before_send:
            if single_radio_mode:
                reconnect_ok = recover_single_radio_control_plane(
                    restart_control_plane_iface,
                    fm_target_for_route,
                    wifi_profile=single_radio_wifi_profile,
                    max_attempts=single_radio_recovery_retries,
                    down_s=restart_control_plane_down_s,
                    route_wait_s=single_radio_recovery_route_wait_s,
                    custom_cmd=single_radio_recovery_cmd,
                    csi_kill_patterns=single_radio_csi_kill_patterns,
                )
            else:
                reconnect_ok = restart_control_plane_before_report(
                    restart_control_plane_iface,
                    fm_target_for_route,
                    down_s=restart_control_plane_down_s,
                    wait_s=restart_control_plane_wait_s,
                    custom_cmd=restart_control_plane_cmd,
                    allow_wifi_restart=allow_wifi_control_plane_restart,
                )
            if reconnect_ok:
                try:
                    channel.close()
                except Exception:
                    pass
                channel = grpc.insecure_channel(target)
                stub = fleet_gateway_v2_pb2_grpc.FleetManagerStub(channel)
            elif single_radio_mode and single_radio_reboot_on_recovery_fail:
                reboot_msg = (
                    f"single-radio recovery failed run_id={run_id} "
                    f"iface={restart_control_plane_iface} target={normalize(fm_target_for_route)}"
                )
                if request_controlled_reboot(reboot_msg):
                    # Give reboot command a small head-start and stop local loop.
                    time.sleep(2)
                    return

        sent_ids = flush_pending_reports_guarded("post-run")
        if run_id in sent_ids:
            log.info("Run %s stored and reported", run_id)
        else:
            if restart_control_plane_before_send and not reconnect_ok:
                log.warning(
                    "Run %s stored locally (reconnect failed) and queued for later upload",
                    run_id,
                )
            else:
                log.warning("Run %s stored locally and queued for later upload", run_id)

        if reached_max_cycles(cycles, max_cycles):
            break
        time.sleep(poll)


if __name__ == "__main__":
    main()
