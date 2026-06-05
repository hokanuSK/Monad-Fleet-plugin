from .core import *  # noqa: F401,F403
from . import core as core_mod

_ARTIFACT_UPLOAD_NEXT_TRY_AT = core_mod._ARTIFACT_UPLOAD_NEXT_TRY_AT


def _normalize_sink_name(value: Any) -> str:
    return core_mod._normalize_sink_name(value)


def _resolve_sink_list(primary: Any, fallback: Any = "") -> list[str]:
    return core_mod._resolve_sink_list(primary, fallback)


def _safe_float(value: Any) -> float | None:
    return core_mod._safe_float(value)


def _report_artifact_transfer_counts(report: fleet_gateway_v2_pb2.Report) -> tuple[int, int, int]:
    total = max(0, len(report.artifacts))
    uploaded = max(
        parse_int(report.summary_metrics.get("artifacts_uploaded_elab"), 0),
        parse_int(report.summary_metrics.get("artifacts_transferred_fleet_or_elab"), 0),
    )
    failed = max(0, parse_int(report.summary_metrics.get("artifacts_upload_failed"), 0))
    return total, max(0, int(uploaded)), failed


def report_ready_for_closeout(report: fleet_gateway_v2_pb2.Report) -> bool:
    total, uploaded, failed = _report_artifact_transfer_counts(report)
    if failed > 0:
        return False
    if total <= 0:
        return True
    return uploaded >= total


def _publish_report_with_confirmation(
    stub: fleet_gateway_v2_pb2_grpc.FleetManagerStub,
    agent_id: str,
    report: fleet_gateway_v2_pb2.Report,
    *,
    timeout_s: int,
) -> fleet_gateway_v2_pb2.PublishReportResponse:
    request = fleet_gateway_v2_pb2.PublishReportRequest(agent_id=agent_id, report=report)
    try:
        return stub.PublishReport(request, timeout=float(timeout_s))
    except grpc.RpcError as exc:
        if exc.code() != grpc.StatusCode.DEADLINE_EXCEEDED:
            raise

    confirm_retries = max(1, parse_int(os.environ.get("REPORT_RPC_CONFIRM_RETRIES"), 2))
    confirm_timeout_s = max(
        5,
        parse_int(
            os.environ.get("REPORT_RPC_CONFIRM_TIMEOUT_S"),
            min(max(5, int(timeout_s)), 15),
        ),
    )
    last_exc: grpc.RpcError | None = None
    for attempt in range(1, confirm_retries + 1):
        try:
            log.info(
                "PublishReport confirmation retry %d/%d run_id=%s timeout=%ss",
                attempt,
                confirm_retries,
                normalize(report.run_id),
                confirm_timeout_s,
            )
            return stub.PublishReport(request, timeout=float(confirm_timeout_s))
        except grpc.RpcError as exc:
            last_exc = exc
            if exc.code() != grpc.StatusCode.DEADLINE_EXCEEDED:
                raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("PublishReport confirmation unexpectedly produced no result")

def flush_pending_reports(
    stub: fleet_gateway_v2_pb2_grpc.FleetManagerStub,
    agent_id: str,
    store: RunStore,
    *,
    metrics_upload_state: str = "",
    report_upload_status_hook: Any = None,
    artifact_upload_status_hook: Any = None,
) -> set[str]:
    sent_run_ids: set[str] = set()
    report_timeout_s = max(10, parse_int(os.environ.get("REPORT_RPC_TIMEOUT_S"), 90))
    desired_metrics_state = normalize(metrics_upload_state).upper()
    for run_id in store.pending_run_ids():
        report = store.load_report(run_id)
        if report is None:
            continue
        report_changed = False
        if desired_metrics_state and normalize(report.summary_metrics.get("metrics_upload_state")).upper() != desired_metrics_state:
            report.summary_metrics["metrics_upload_state"] = desired_metrics_state
            report_changed = True
        effective_metrics_state = normalize(report.summary_metrics.get("metrics_upload_state")).upper()
        if not effective_metrics_state:
            effective_metrics_state = desired_metrics_state or "PENDING"
        if callable(report_upload_status_hook):
            try:
                report_upload_status_hook(run_id, report, effective_metrics_state, "report_replay")
            except Exception:
                log.debug("report_upload_status_hook failed run_id=%s", run_id, exc_info=True)

        uploaded_count, failed_count = upload_report_artifacts_to_elab(
            report,
            store,
            fallback_agent_id=agent_id,
            artifact_upload_stub=stub,
            artifact_status_hook=artifact_upload_status_hook,
        )
        if uploaded_count or failed_count:
            report.summary_metrics["artifacts_transferred_fleet_or_elab"] = str(uploaded_count)
            report.summary_metrics["artifacts_uploaded_elab"] = str(uploaded_count)
            report.summary_metrics["artifacts_upload_failed"] = str(failed_count)
            report_changed = True
        if report_changed:
            store.persist_report(report)
        if failed_count > 0 and _server_side_artifact_bundling_enabled():
            log.warning(
                "Deferring PublishReport until artifact finalization succeeds run_id=%s failed_artifacts=%d",
                normalize(report.run_id),
                failed_count,
            )
            continue
        try:
            resp = _publish_report_with_confirmation(
                stub,
                agent_id,
                report,
                timeout_s=report_timeout_s,
            )
        except grpc.RpcError as exc:
            log.warning("PublishReport replay failed for run_id=%s: %s", run_id, exc)
            continue

        status = int(resp.status)
        if status in (
            int(fleet_gateway_v2_pb2.PublishReportResponse.ACCEPTED),
            int(fleet_gateway_v2_pb2.PublishReportResponse.DUPLICATE),
        ):
            store.mark_sent(run_id)
            sent_run_ids.add(run_id)
        else:
            log.warning(
                "PublishReport replay rejected for run_id=%s status=%s reason=%s",
                run_id,
                status,
                normalize(resp.reason),
            )
    store.prune_sent()
    return sent_run_ids


def _ingest_metrics_http(
    device_id: str,
    values: dict[str, float],
    *,
    source: str = "agent-wifi-sampler",
    endpoint: str = "",
    ingest_token: str = "",
    timeout_s: int = 3,
) -> None:
    if not values:
        return
    if not parse_bool(os.environ.get("ENABLE_HTTP_SAMPLE_INGEST"), True):
        return

    url = normalize(endpoint) or normalize(os.environ.get("FLEET_METRICS_INGEST_URL"))
    if not url:
        host = normalize(os.environ.get("FLEET_MANAGER_HOST"))
        if not host:
            return
        port = parse_int(os.environ.get("METRICS_PORT"), 9108)
        url = f"http://{host}:{max(1, port)}/ingest/v1/metrics"

    payload = {
        "source": source,
        "device_id": normalize(device_id).lower(),
        "values": values,
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")

    token = normalize(ingest_token) or normalize(os.environ.get("INGEST_API_TOKEN"))
    if token:
        req.add_header("x-ingest-token", token)

    try:
        with urllib.request.urlopen(req, timeout=max(1, int(timeout_s))) as resp:
            if int(getattr(resp, "status", 200)) >= 300:
                log.debug("metrics ingest returned status=%s", getattr(resp, "status", "unknown"))
    except Exception:
        log.debug("metrics ingest failed", exc_info=True)


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: int = 15,
    token: str = "",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    auth = normalize(token) or normalize(os.environ.get("INGEST_API_TOKEN"))
    if auth:
        req.add_header("x-ingest-token", auth)
    if headers:
        for key, value in headers.items():
            k = normalize(key)
            v = normalize(value)
            if not k or not v:
                continue
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}


def _artifact_upload_target(value: str) -> str:
    target = _normalize_sink_name(value)
    if not target:
        return "elabftw"
    if target == "none":
        return "none"
    if target == "fleet_http":
        return "fleet_http"
    if target == "fleet_grpc":
        return "fleet_grpc"
    if target == "elabftw":
        return "elabftw"
    return target


def _artifact_upload_endpoint_and_limit(override_env: dict[str, str] | None = None) -> tuple[str, int] | None:
    env = override_env or {}
    if not parse_bool(env.get("ENABLE_ELAB_ARTIFACT_UPLOAD") or os.environ.get("ENABLE_ELAB_ARTIFACT_UPLOAD"), True):
        return None
    host = normalize(env.get("FLEET_MANAGER_HOST") or os.environ.get("FLEET_MANAGER_HOST"))
    if not host:
        return None
    port = parse_int(env.get("FLEET_ARTIFACT_INGEST_PORT") or os.environ.get("FLEET_ARTIFACT_INGEST_PORT"), 9108)
    endpoint = normalize(env.get("FLEET_ARTIFACT_INGEST_URL") or os.environ.get("FLEET_ARTIFACT_INGEST_URL")) or f"http://{host}:{max(1, port)}/ingest/v1/artifacts"
    max_bytes = max(
        1024,
        parse_int(
            env.get("ARTIFACT_UPLOAD_MAX_BYTES") or os.environ.get("ARTIFACT_UPLOAD_MAX_BYTES"),
            512 * 1024 * 1024,
        ),
    )
    return endpoint, max_bytes


def _upload_spool_artifact_to_elab(
    artifact: fleet_gateway_v2_pb2.ArtifactRef,
    store: RunStore,
    *,
    run_id: str,
    experiment_numeric_id: int,
    agent_id: str,
    upload_phase: str,
    endpoint: str,
    max_bytes: int,
    timeout_s: int,
    ingest_token: str = "",
) -> str:
    if not normalize(artifact.uri).startswith("spool://"):
        return "skipped"

    path = store.artifact_path(run_id, artifact.name)
    if path is None:
        log.warning("artifact upload skipped: local file missing run_id=%s name=%s", run_id, artifact.name)
        return "skipped"

    try:
        size_bytes = int(path.stat().st_size)
        if size_bytes > max_bytes:
            log.warning(
                "artifact upload skipped: too large run_id=%s name=%s size=%d max=%d",
                run_id,
                artifact.name,
                size_bytes,
                max_bytes,
            )
            return "failed"
        raw = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = {
            "experiment_id": str(experiment_numeric_id),
            "run_id": normalize(run_id),
            "agent_id": normalize(agent_id),
            "artifact_name": normalize(artifact.name) or path.name,
            "upload_phase": normalize(upload_phase) or "report_replay",
            "mime": mime,
            "size_bytes": size_bytes,
            "sha256": normalize(artifact.sha256),
            "comment": f"fleet-v3 run={run_id} agent={agent_id} artifact={normalize(artifact.name)}",
            "content_b64": base64.b64encode(raw).decode("ascii"),
        }
        response = _post_json(
            endpoint,
            payload,
            timeout=max(2, int(timeout_s)),
            token=normalize(ingest_token),
        )
        if not response.get("ok"):
            log.warning("artifact upload rejected run_id=%s name=%s response=%s", run_id, artifact.name, response)
            return "failed"

        new_uri = normalize(response.get("artifact_uri") or response.get("location"))
        if new_uri:
            artifact.uri = new_uri
        if parse_bool(os.environ.get("ARTIFACT_EVICT_AFTER_UPLOAD"), False):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        return "spooled" if response.get("spooled") else "uploaded"
    except Exception as exc:
        log.warning("artifact upload failed run_id=%s name=%s err=%s", run_id, artifact.name, exc)
        return "failed"


def _upload_spool_artifact_to_fleet_grpc(
    artifact: fleet_gateway_v2_pb2.ArtifactRef,
    store: RunStore,
    *,
    stub: Any,
    run_id: str,
    experiment_id: str,
    policy_id: str,
    agent_id: str,
    upload_phase: str,
    max_bytes: int,
    timeout_s: int,
) -> str:
    if not normalize(artifact.uri).startswith("spool://"):
        return "skipped"
    if stub is None or not hasattr(stub, "UploadArtifact"):
        log.warning("artifact gRPC upload skipped: Fleet stub does not expose UploadArtifact")
        return "failed"

    path = store.artifact_path(run_id, artifact.name)
    if path is None:
        log.warning("artifact gRPC upload skipped: local file missing run_id=%s name=%s", run_id, artifact.name)
        return "skipped"

    try:
        size_bytes = int(path.stat().st_size)
        if size_bytes > max_bytes:
            log.warning(
                "artifact gRPC upload skipped: too large run_id=%s name=%s size=%d max=%d",
                run_id,
                artifact.name,
                size_bytes,
                max_bytes,
            )
            return "failed"
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunk_bytes = max(512, parse_int(os.environ.get("GRPC_ARTIFACT_CHUNK_BYTES"), 1024))

        def stream_messages():
            yield fleet_gateway_v2_pb2.UploadArtifactChunk(
                header=fleet_gateway_v2_pb2.ArtifactUploadHeader(
                    agent_id=normalize(agent_id),
                    run_id=normalize(run_id),
                    experiment_id=normalize(experiment_id),
                    policy_id=normalize(policy_id),
                    artifact_name=normalize(artifact.name) or path.name,
                    size_bytes=max(0, size_bytes),
                    sha256=normalize(artifact.sha256),
                    mime=mime,
                    upload_phase=normalize(upload_phase) or "report_replay",
                    comment=f"fleet-v3 run={run_id} agent={agent_id} artifact={normalize(artifact.name)}",
                )
            )
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(chunk_bytes)
                    if not chunk:
                        break
                    yield fleet_gateway_v2_pb2.UploadArtifactChunk(content=chunk)

        response = stub.UploadArtifact(stream_messages(), timeout=max(2, int(timeout_s)))
        if int(response.status) != int(fleet_gateway_v2_pb2.UploadArtifactResponse.ACCEPTED):
            log.warning(
                "artifact gRPC upload rejected run_id=%s name=%s reason=%s",
                run_id,
                artifact.name,
                normalize(getattr(response, "reason", "")),
            )
            return "failed"
        new_uri = normalize(response.artifact_uri or response.location)
        if new_uri:
            artifact.uri = new_uri
        if parse_bool(os.environ.get("ARTIFACT_EVICT_AFTER_UPLOAD"), False):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        return "uploaded" if bool(getattr(response, "server_finalized", False)) else "spooled"
    except Exception as exc:
        log.warning("artifact gRPC upload failed run_id=%s name=%s err=%s", run_id, artifact.name, exc)
        return "failed"


def _artifact_transfer_ok(status: str) -> bool:
    return status in {"uploaded", "spooled"}


def _artifact_status_reason(status: str) -> str:
    if status == "uploaded":
        return "uploaded_to_elab"
    if status == "spooled":
        return "spooled_to_fleet"
    if status == "failed":
        return "upload_failed"
    return "skipped"


def opportunistic_upload_artifacts_to_elab(
    run_id: str,
    experiment_id: str,
    agent_id: str,
    artifacts: list[fleet_gateway_v2_pb2.ArtifactRef],
    store: RunStore,
    *,
    enabled: bool,
    target: str,
) -> tuple[int, int]:
    global _ARTIFACT_UPLOAD_NEXT_TRY_AT

    if not enabled or not artifacts:
        return (0, 0)
    if _artifact_upload_target(target) not in {"elabftw", "fleet_http"}:
        return (0, 0)
    experiment_numeric_id = parse_experiment_numeric_id(experiment_id)
    if experiment_numeric_id is None:
        return (0, 0)

    config = _artifact_upload_endpoint_and_limit()
    if config is None:
        return (0, 0)

    now_s = time.time()
    if now_s < _ARTIFACT_UPLOAD_NEXT_TRY_AT:
        return (0, 0)

    endpoint, max_bytes = config
    timeout_s = max(2, parse_int(os.environ.get("ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S"), 30))
    ingest_token = normalize(os.environ.get("INGEST_API_TOKEN"))
    uploaded = 0
    failed = 0

    for artifact in artifacts:
        status = _upload_spool_artifact_to_elab(
            artifact,
            store,
            run_id=run_id,
            experiment_numeric_id=experiment_numeric_id,
            agent_id=agent_id,
            upload_phase="measure_live",
            endpoint=endpoint,
            max_bytes=max_bytes,
            timeout_s=timeout_s,
            ingest_token=ingest_token,
        )
        if _artifact_transfer_ok(status):
            uploaded += 1
        elif status == "failed":
            failed += 1

    if failed > 0 and uploaded == 0:
        backoff_s = max(3, parse_int(os.environ.get("ARTIFACT_UPLOAD_BACKOFF_S"), 15))
        _ARTIFACT_UPLOAD_NEXT_TRY_AT = now_s + float(backoff_s)
    else:
        _ARTIFACT_UPLOAD_NEXT_TRY_AT = now_s

    return (uploaded, failed)


def upload_report_artifacts_to_elab(
    report: fleet_gateway_v2_pb2.Report,
    store: RunStore,
    *,
    fallback_agent_id: str,
    artifact_upload_stub: Any = None,
    artifact_status_hook: Any = None,
) -> tuple[int, int]:
    experiment_numeric_id = parse_experiment_numeric_id(report.experiment_id)
    if experiment_numeric_id is None:
        return (0, 0)

    target = _artifact_upload_target(os.environ.get("ARTIFACT_UPLOAD_TARGET"))
    max_bytes = max(1024, parse_int(os.environ.get("ARTIFACT_UPLOAD_MAX_BYTES"), 512 * 1024 * 1024))
    endpoint = ""
    if target != "fleet_grpc":
        config = _artifact_upload_endpoint_and_limit()
        if config is None:
            return (0, 0)
        endpoint, max_bytes = config

    uploaded = 0
    failed = 0
    agent_id = normalize(report.agent_id) or normalize(fallback_agent_id)
    timeout_s = max(10, parse_int(os.environ.get("ARTIFACT_UPLOAD_TIMEOUT_S"), 300))
    ingest_token = normalize(os.environ.get("INGEST_API_TOKEN"))

    artifacts = sorted(
        report.artifacts,
        key=lambda a: 1 if Path(normalize(a.name) or "").name == "run-summary.json" else 0,
    )
    for artifact in artifacts:
        if target == "fleet_grpc":
            status = _upload_spool_artifact_to_fleet_grpc(
                artifact,
                store,
                stub=artifact_upload_stub,
                run_id=report.run_id,
                experiment_id=report.experiment_id,
                policy_id=report.policy_id,
                agent_id=agent_id,
                upload_phase="report_replay",
                max_bytes=max_bytes,
                timeout_s=timeout_s,
            )
        else:
            status = _upload_spool_artifact_to_elab(
                artifact,
                store,
                run_id=report.run_id,
                experiment_numeric_id=experiment_numeric_id,
                agent_id=agent_id,
                upload_phase="report_replay",
                endpoint=endpoint,
                max_bytes=max_bytes,
                timeout_s=timeout_s,
                ingest_token=ingest_token,
            )
        if callable(artifact_status_hook):
            try:
                reason = _artifact_status_reason(status)
                artifact_status_hook(report, artifact, status, reason)
            except Exception:
                log.debug(
                    "artifact_status_hook failed run_id=%s artifact=%s",
                    normalize(report.run_id),
                    normalize(artifact.name),
                    exc_info=True,
                )
        if _artifact_transfer_ok(status):
            uploaded += 1
        elif status == "failed":
            failed += 1

    return (uploaded, failed)


def _resolve_command_sink_sets(cmd_env: dict[str, str]) -> dict[str, list[str]]:
    shared_fallback = _resolve_sink_list(
        cmd_env.get("DATA_SINKS"),
        os.environ.get("DATA_SINKS") or os.environ.get("DEFAULT_DATA_SINKS"),
    )
    fallback_csv = ",".join(shared_fallback)
    metrics = _resolve_sink_list(
        cmd_env.get("METRICS_SINKS"),
        os.environ.get("METRICS_SINKS") or os.environ.get("DEFAULT_METRICS_SINKS") or fallback_csv,
    )
    events = _resolve_sink_list(
        cmd_env.get("EVENT_SINKS"),
        os.environ.get("EVENT_SINKS") or os.environ.get("DEFAULT_EVENT_SINKS") or fallback_csv,
    )
    artifacts = _resolve_sink_list(
        cmd_env.get("ARTIFACT_SINKS"),
        os.environ.get("ARTIFACT_SINKS") or os.environ.get("DEFAULT_ARTIFACT_SINKS") or fallback_csv,
    )
    return {
        "metrics": metrics,
        "events": events,
        "artifacts": artifacts,
    }


def _resolve_sink_config(cmd_env: dict[str, str], data_root: Path) -> dict[str, Any]:
    def pick(name: str, *, fallback_env: str = "") -> str:
        if normalize(cmd_env.get(name)):
            return normalize(cmd_env.get(name))
        if fallback_env and normalize(os.environ.get(fallback_env)):
            return normalize(os.environ.get(fallback_env))
        return normalize(os.environ.get(name))

    sqlite_default = str(data_root / "sinks" / "agent_sink.db")
    return {
        "webhook_url": pick("WEBHOOK_URL", fallback_env="DATA_WEBHOOK_URL"),
        "webhook_token": pick("WEBHOOK_TOKEN"),
        "webhook_timeout_s": max(2, parse_int(pick("WEBHOOK_TIMEOUT_S"), 10)),
        "webhook_artifact_inline_max_bytes": max(
            0,
            parse_int(pick("WEBHOOK_ARTIFACT_INLINE_MAX_BYTES"), 0),
        ),
        "ingest_token": pick("INGEST_API_TOKEN"),
        "fleet_metrics_ingest_url": pick("FLEET_METRICS_INGEST_URL"),
        "fleet_artifact_ingest_url": pick("FLEET_ARTIFACT_INGEST_URL"),
        "sqlite_path": pick("SQLITE_SINK_PATH") or sqlite_default,
        "artifact_upload_timeout_s": max(2, parse_int(pick("ARTIFACT_UPLOAD_TIMEOUT_S"), 15)),
    }


def _numeric_metrics_from_map(metrics: dict[str, str]) -> dict[str, float]:
    numeric: dict[str, float] = {}
    for key, value in metrics.items():
        metric_key = normalize(key)
        if not metric_key:
            continue
        parsed = _safe_float(value)
        if parsed is None:
            continue
        numeric[metric_key] = float(parsed)
    return numeric


def _build_artifact_payload_rows(
    run_id: str,
    command_id: str,
    artifacts: list[fleet_gateway_v2_pb2.ArtifactRef],
    store: RunStore,
    *,
    inline_max_bytes: int = 0,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for artifact in artifacts:
        row: dict[str, Any] = {
            "command_id": normalize(command_id),
            "name": normalize(artifact.name),
            "uri": normalize(artifact.uri),
            "size_bytes": int(artifact.size_bytes or 0),
            "sha256": normalize(artifact.sha256),
        }
        path = store.artifact_path(run_id, artifact.name)
        if path is not None:
            row["local_path"] = str(path)
            if inline_max_bytes > 0:
                try:
                    size_bytes = int(path.stat().st_size)
                    if size_bytes <= inline_max_bytes:
                        row["content_b64"] = base64.b64encode(path.read_bytes()).decode("ascii")
                except Exception:
                    pass
        rows.append(row)
    return rows


def _write_command_payload_to_sqlite(
    sqlite_path: str,
    *,
    envelope: dict[str, Any],
    metric_values: dict[str, float],
    artifact_rows: list[dict[str, Any]],
) -> None:
    db_text = normalize(sqlite_path)
    if not db_text:
        return
    db_path = Path(db_text)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS command_events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "recorded_at TEXT NOT NULL,"
                "run_id TEXT NOT NULL,"
                "experiment_id TEXT,"
                "policy_id TEXT,"
                "agent_id TEXT,"
                "group_id TEXT,"
                "command_id TEXT,"
                "command_type TEXT,"
                "exit_code INTEGER,"
                "duration_ms INTEGER,"
                "message TEXT,"
                "payload_json TEXT NOT NULL"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS command_metrics ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "recorded_at TEXT NOT NULL,"
                "run_id TEXT NOT NULL,"
                "command_id TEXT NOT NULL,"
                "metric TEXT NOT NULL,"
                "value REAL NOT NULL"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS command_artifacts ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "recorded_at TEXT NOT NULL,"
                "run_id TEXT NOT NULL,"
                "command_id TEXT NOT NULL,"
                "name TEXT NOT NULL,"
                "uri TEXT,"
                "size_bytes INTEGER,"
                "sha256 TEXT,"
                "local_path TEXT"
                ")"
            )

            recorded_at = normalize(envelope.get("timestamp"))
            run_id = normalize(envelope.get("run_id"))
            command_id = normalize(envelope.get("command_id"))

            conn.execute(
                "INSERT INTO command_events "
                "(recorded_at, run_id, experiment_id, policy_id, agent_id, group_id, command_id, command_type, exit_code, duration_ms, message, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    recorded_at,
                    run_id,
                    normalize(envelope.get("experiment_id")),
                    normalize(envelope.get("policy_id")),
                    normalize(envelope.get("agent_id")),
                    normalize(envelope.get("group_id")),
                    command_id,
                    normalize(envelope.get("command_type")),
                    int(envelope.get("exit_code", 0) or 0),
                    max(0, int(envelope.get("duration_ms", 0) or 0)),
                    normalize(envelope.get("message")),
                    json.dumps(envelope, separators=(",", ":"), ensure_ascii=False),
                ),
            )
            for key, value in metric_values.items():
                conn.execute(
                    "INSERT INTO command_metrics (recorded_at, run_id, command_id, metric, value) VALUES (?, ?, ?, ?, ?)",
                    (recorded_at, run_id, command_id, normalize(key), float(value)),
                )
            for row in artifact_rows:
                conn.execute(
                    "INSERT INTO command_artifacts "
                    "(recorded_at, run_id, command_id, name, uri, size_bytes, sha256, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        recorded_at,
                        run_id,
                        command_id,
                        normalize(row.get("name")),
                        normalize(row.get("uri")),
                        max(0, int(row.get("size_bytes", 0) or 0)),
                        normalize(row.get("sha256")),
                        normalize(row.get("local_path")),
                    ),
                )
    finally:
        conn.close()


def _post_webhook_payload(
    webhook_url: str,
    payload: dict[str, Any],
    *,
    timeout_s: int,
    webhook_token: str = "",
) -> None:
    url = normalize(webhook_url)
    if not url:
        return
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    token = normalize(webhook_token)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("x-webhook-token", token)
    with urllib.request.urlopen(req, timeout=max(2, int(timeout_s))) as resp:
        if int(getattr(resp, "status", 200)) >= 300:
            raise RuntimeError(f"webhook status={getattr(resp, 'status', 'unknown')}")


def _upload_artifacts_for_command_sink(
    *,
    run_id: str,
    experiment_id: str,
    agent_id: str,
    artifacts: list[fleet_gateway_v2_pb2.ArtifactRef],
    store: RunStore,
    override_env: dict[str, str],
    upload_phase: str,
    ingest_token: str,
    timeout_s: int,
) -> tuple[int, int]:
    if not artifacts:
        return (0, 0)
    experiment_numeric_id = parse_experiment_numeric_id(experiment_id)
    if experiment_numeric_id is None:
        return (0, 0)
    config = _artifact_upload_endpoint_and_limit(override_env=override_env)
    if config is None:
        return (0, 0)
    endpoint, max_bytes = config
    uploaded = 0
    failed = 0
    for artifact in artifacts:
        status = _upload_spool_artifact_to_elab(
            artifact,
            store,
            run_id=run_id,
            experiment_numeric_id=experiment_numeric_id,
            agent_id=agent_id,
            upload_phase=upload_phase,
            endpoint=endpoint,
            max_bytes=max_bytes,
            timeout_s=timeout_s,
            ingest_token=ingest_token,
        )
        if _artifact_transfer_ok(status):
            uploaded += 1
        elif status == "failed":
            failed += 1
    return (uploaded, failed)


def publish_command_data_to_sinks(
    *,
    cmd_env: dict[str, str],
    run_id: str,
    experiment_id: str,
    policy_id: str,
    group_id: str,
    command_id: str,
    command_type: str,
    agent_id: str,
    exit_code: int,
    duration_ms: int,
    message: str,
    event_metrics: dict[str, str],
    command_artifacts: list[fleet_gateway_v2_pb2.ArtifactRef],
    store: RunStore,
    data_root: Path,
) -> dict[str, int]:
    sink_sets = _resolve_command_sink_sets(cmd_env)
    requested = sink_sets["metrics"] or sink_sets["events"] or sink_sets["artifacts"]
    if not requested:
        return {"metrics_sent": 0, "events_sent": 0, "artifacts_uploaded": 0, "artifacts_upload_failed": 0}

    sink_cfg = _resolve_sink_config(cmd_env, data_root)
    metrics_numeric = _numeric_metrics_from_map(event_metrics)
    metrics_numeric["command_exit_code"] = float(int(exit_code))
    metrics_numeric["command_duration_ms"] = float(max(0, int(duration_ms)))
    artifact_rows = _build_artifact_payload_rows(
        run_id,
        command_id,
        command_artifacts,
        store,
        inline_max_bytes=max(0, int(sink_cfg["webhook_artifact_inline_max_bytes"])),
    )

    envelope = {
        "schema": "fleet.v2.command_result.v1",
        "timestamp": now_utc().isoformat().replace("+00:00", "Z"),
        "run_id": normalize(run_id),
        "experiment_id": normalize(experiment_id),
        "policy_id": normalize(policy_id),
        "agent_id": normalize(agent_id),
        "group_id": normalize(group_id),
        "command_id": normalize(command_id),
        "command_type": normalize(command_type),
        "exit_code": int(exit_code),
        "duration_ms": max(0, int(duration_ms)),
        "message": normalize(message),
        "metrics": {normalize(k): normalize(v) for k, v in event_metrics.items() if normalize(k)},
        "artifacts": artifact_rows,
    }

    stats = {
        "metrics_sent": 0,
        "events_sent": 0,
        "artifacts_uploaded": 0,
        "artifacts_upload_failed": 0,
    }

    if "fleet_http" in sink_sets["metrics"] and metrics_numeric:
        try:
            _ingest_metrics_http(
                agent_id,
                metrics_numeric,
                source=f"agent-command:{normalize(command_type).lower()}",
                endpoint=normalize(sink_cfg["fleet_metrics_ingest_url"]),
                ingest_token=normalize(sink_cfg["ingest_token"]),
                timeout_s=3,
            )
            stats["metrics_sent"] += 1
        except Exception:
            log.debug("Command metrics ingest failed run_id=%s command_id=%s", run_id, command_id, exc_info=True)

    needs_webhook = any("webhook" in sink_sets[k] for k in ("metrics", "events", "artifacts"))
    if needs_webhook and normalize(sink_cfg["webhook_url"]):
        try:
            _post_webhook_payload(
                normalize(sink_cfg["webhook_url"]),
                envelope,
                timeout_s=max(2, int(sink_cfg["webhook_timeout_s"])),
                webhook_token=normalize(sink_cfg["webhook_token"]),
            )
            if "webhook" in sink_sets["metrics"]:
                stats["metrics_sent"] += 1
            if "webhook" in sink_sets["events"]:
                stats["events_sent"] += 1
            if "webhook" in sink_sets["artifacts"] and artifact_rows:
                stats["artifacts_uploaded"] += len(artifact_rows)
        except Exception:
            log.debug("Webhook sink failed run_id=%s command_id=%s", run_id, command_id, exc_info=True)

    needs_sqlite = any("sqlite" in sink_sets[k] for k in ("metrics", "events", "artifacts"))
    if needs_sqlite and normalize(sink_cfg["sqlite_path"]):
        try:
            sqlite_metrics = metrics_numeric if "sqlite" in sink_sets["metrics"] else {}
            sqlite_artifacts = artifact_rows if "sqlite" in sink_sets["artifacts"] else []
            sqlite_envelope = envelope if "sqlite" in sink_sets["events"] else {**envelope, "metrics": {}, "artifacts": []}
            _write_command_payload_to_sqlite(
                normalize(sink_cfg["sqlite_path"]),
                envelope=sqlite_envelope,
                metric_values=sqlite_metrics,
                artifact_rows=sqlite_artifacts,
            )
            if sqlite_metrics:
                stats["metrics_sent"] += 1
            if "sqlite" in sink_sets["events"]:
                stats["events_sent"] += 1
            if sqlite_artifacts:
                stats["artifacts_uploaded"] += len(sqlite_artifacts)
        except Exception:
            log.debug("SQLite sink failed run_id=%s command_id=%s", run_id, command_id, exc_info=True)

    artifact_upload_sinks = {sink for sink in sink_sets["artifacts"] if sink in {"elabftw", "fleet_http"}}
    if artifact_upload_sinks and command_artifacts:
        uploaded, failed = _upload_artifacts_for_command_sink(
            run_id=run_id,
            experiment_id=experiment_id,
            agent_id=agent_id,
            artifacts=command_artifacts,
            store=store,
            override_env=cmd_env,
            upload_phase="command_sink",
            ingest_token=normalize(sink_cfg["ingest_token"]),
            timeout_s=max(2, int(sink_cfg["artifact_upload_timeout_s"])),
        )
        stats["artifacts_uploaded"] += uploaded
        stats["artifacts_upload_failed"] += failed

    return stats


def _merge_text_artifacts_enabled() -> bool:
    return parse_bool(os.environ.get("MERGE_TEXT_ARTIFACTS"), True)


def _server_side_artifact_bundling_enabled() -> bool:
    return parse_bool(os.environ.get("SERVER_SIDE_ARTIFACT_BUNDLING"), True)


def _merge_text_artifacts_delete_sources() -> bool:
    return parse_bool(os.environ.get("MERGE_TEXT_ARTIFACTS_DELETE_SOURCES"), True)


def _artifact_description(name: str) -> str:
    stem = Path(normalize(name)).stem.lower()
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


def merge_text_artifacts_for_run(
    run_id: str,
    artifacts: list[fleet_gateway_v2_pb2.ArtifactRef],
    store: RunStore,
) -> tuple[list[fleet_gateway_v2_pb2.ArtifactRef], int]:
    if not artifacts:
        return artifacts, 0

    mergeable_suffixes = {".txt", ".log", ".json"}
    keep_names = {"run-summary.json"}
    kept: list[fleet_gateway_v2_pb2.ArtifactRef] = []
    merge_candidates: list[tuple[fleet_gateway_v2_pb2.ArtifactRef, Path]] = []

    for artifact in artifacts:
        name = normalize(artifact.name)
        if not name:
            kept.append(artifact)
            continue
        if normalize(artifact.uri) and not normalize(artifact.uri).startswith("spool://"):
            kept.append(artifact)
            continue
        suffix = Path(name).suffix.lower()
        if name in keep_names or suffix not in mergeable_suffixes:
            kept.append(artifact)
            continue

        path = store.artifact_path(run_id, name)
        if path is None:
            kept.append(artifact)
            continue

        merge_candidates.append((artifact, path))

    if not merge_candidates:
        return artifacts, 0

    bundle_entries = [
        {
            "name": normalize(artifact.name),
            "sha256": normalize(artifact.sha256),
            "size_bytes": int(artifact.size_bytes or 0),
            "description": _artifact_description(normalize(artifact.name)),
        }
        for artifact, _ in merge_candidates
    ]
    bundle_manifest = {
        "schema": "fleet.v2.artifact_bundle.v2",
        "run_id": normalize(run_id),
        "created_at": now_utc().isoformat().replace("+00:00", "Z"),
        "format": "tar.gz",
        "entries_dir": "entries/",
        "entries": bundle_entries,
    }

    artifacts_dir = merge_candidates[0][1].parent
    bundle_name = "wireless-run-evidence-bundle.tar.gz"
    bundle_path = artifacts_dir / bundle_name
    if bundle_path.exists():
        stamp = int(time.time() * 1000)
        bundle_name = f"wireless-run-evidence-bundle-{stamp}.tar.gz"
        bundle_path = artifacts_dir / bundle_name
    bundle_tmp_path = artifacts_dir / f"{bundle_name}.tmp"

    try:
        with tarfile.open(bundle_tmp_path, mode="w:gz") as archive:
            for _, path in merge_candidates:
                archive.add(path, arcname=f"entries/{path.name}", recursive=False)
            manifest_bytes = json.dumps(bundle_manifest, indent=2, sort_keys=True).encode("utf-8")
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest_bytes)
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(manifest_bytes))
        bundle_tmp_path.replace(bundle_path)
    except Exception:
        log.exception("Failed to create merged artifact bundle for run_id=%s", run_id)
        try:
            if bundle_tmp_path.exists():
                bundle_tmp_path.unlink()
            if bundle_path.exists():
                bundle_path.unlink()
        except Exception:
            pass
        return artifacts, 0

    if _merge_text_artifacts_delete_sources():
        for _, path in merge_candidates:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                log.debug("Failed to remove merged source artifact path=%s", path)

    kept.append(artifact_from_path(bundle_name, bundle_path, run_id))
    return kept, len(bundle_entries)
