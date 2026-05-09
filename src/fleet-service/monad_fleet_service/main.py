from concurrent import futures
import os
from pathlib import Path

import grpc

import fleet_gateway_pb2_grpc
import fleet_gateway_v2_pb2_grpc
import fleet_gateway_v3_pb2_grpc

from . import core
from .servicer_v1 import FleetManagerServicer
from .servicer_v2 import FleetManagerServicerV2


def serve() -> None:
    base_url = os.environ.get("ELAB_BASE_URL") or os.environ.get("ELAB_API_BASE_URL") or "https://web/api/v2"
    api_key = os.environ.get("ELAB_API_KEY")
    verify_env = core.normalize_string(os.environ.get("ELAB_VERIFY_TLS", "false")).lower()
    verify_tls = verify_env in {"1", "true", "yes"}

    cfg = {
        "fleet_experiment_tag": os.environ.get("FLEET_EXPERIMENT_TAG", "fleet"),
        "resource_tag_prefix": os.environ.get("RESOURCE_TAG_PREFIX", "device:"),
        "policy_metadata_keys": [
            core.normalize_string(key)
            for key in os.environ.get(
                "POLICY_METADATA_KEYS",
                ",".join(core.DEFAULT_POLICY_METADATA_KEYS),
            ).split(",")
            if core.normalize_string(key)
        ],
        "poll_interval_s": int(os.environ.get("HELLO_POLL_INTERVAL_S", "30")),
        "required_min_agent_version": os.environ.get("REQUIRED_MIN_AGENT_VERSION", ""),
        "experiments_batch_size": int(os.environ.get("EXPERIMENTS_BATCH_SIZE", "200")),
        "max_event_history": int(os.environ.get("MAX_EVENT_HISTORY", "100")),
        "event_duration_minutes": int(os.environ.get("EVENT_DURATION_MINUTES", "60")),
        "book_max_minutes": int(os.environ.get("BOOK_MAX_MINUTES", "180")),
        "book_can_overlap": core.normalize_string(os.environ.get("BOOK_CAN_OVERLAP", "true")).lower() in {"1", "true", "yes"},
        "book_is_cancellable": core.normalize_string(os.environ.get("BOOK_IS_CANCELLABLE", "true")).lower() in {"1", "true", "yes"},
        "max_dedupe_events": int(os.environ.get("MAX_DEDUPE_EVENTS", "50000")),
        "allow_live_events": core.normalize_string(os.environ.get("ALLOW_LIVE_EVENTS", "false")).lower() in {"1", "true", "yes"},
        "enable_v2_device_state_patch": core.normalize_string(os.environ.get("ENABLE_V2_DEVICE_STATE_PATCH", "true")).lower()
        in {"1", "true", "yes"},
        "skip_expired_policies": core.normalize_string(os.environ.get("SKIP_EXPIRED_POLICIES", "true")).lower() in {"1", "true", "yes"},
        "metrics_bind": os.environ.get("METRICS_BIND", "0.0.0.0"),
        "metrics_port": int(os.environ.get("METRICS_PORT", "9108")),
        "metrics_exclude_prefixes": tuple(
            core.safe_metric_token(part, "")
            for part in os.environ.get("METRICS_EXCLUDE_PREFIXES", "").split(",")
            if core.safe_metric_token(part, "")
        ),
        "ingest_api_token": os.environ.get("INGEST_API_TOKEN", ""),
        "artifact_max_bytes": int(os.environ.get("ARTIFACT_MAX_BYTES", str(20 * 1024 * 1024))),
        "resource_status_id_map": core.parse_maybe_json(os.environ.get("RESOURCE_STATUS_ID_MAP_JSON"), {}),
    }

    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    enable_metrics_ingest_journal = core.normalize_string(
        os.environ.get("ENABLE_METRICS_INGEST_JOURNAL", "false")
    ).lower() in {"1", "true", "yes", "on"}
    core.INGEST_JOURNAL_PATH = data_dir / "ingest-metrics.ndjson" if enable_metrics_ingest_journal else None
    core.ARTIFACT_INGEST_JOURNAL_PATH = data_dir / "ingest-artifacts.ndjson"
    core.ARTIFACT_SERVER_SPOOL_DIR = data_dir / "artifact-spool"
    core.METRICS_EXCLUDE_PREFIXES = tuple(cfg.get("metrics_exclude_prefixes") or ())
    if not enable_metrics_ingest_journal:
        core.log.info("Metrics ingest journal disabled; Mimir is the durable metrics store")
    if core.METRICS_EXCLUDE_PREFIXES:
        core.log.info("Metrics export filter enabled, excluded prefixes: %s", ",".join(core.METRICS_EXCLUDE_PREFIXES))
    core.log.info("fleet.v3 device state metadata patching enabled=%s", cfg["enable_v2_device_state_patch"])
    state = core.LocalState(data_dir / "state.json", max_event_ids=cfg["max_dedupe_events"])

    elab_client = core.ElabFTWClient(base_url=base_url, api_key=api_key, verify_tls=verify_tls)
    core.ELAB_CLIENT_FOR_HTTP = elab_client
    v1_servicer = FleetManagerServicer(elab_client, state, cfg)
    v2_servicer = FleetManagerServicerV2(v1_servicer, cfg)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    fleet_gateway_pb2_grpc.add_FleetManagerServicer_to_server(v1_servicer, server)
    # Primary endpoint (v3).
    fleet_gateway_v3_pb2_grpc.add_FleetManagerServicer_to_server(v2_servicer, server)
    # Backward-compatible endpoint (v2) served by the same implementation.
    fleet_gateway_v2_pb2_grpc.add_FleetManagerServicer_to_server(v2_servicer, server)

    core.start_http_sidecar(cfg)

    port = int(os.environ.get("GATEWAY_PORT", "50060"))
    listen_addr = f"[::]:{port}"
    server.add_insecure_port(listen_addr)

    core.log.info("Starting Monad Fleet service on %s", listen_addr)
    server.start()
    server.wait_for_termination()
