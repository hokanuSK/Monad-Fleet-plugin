# Monad Fleet Service

Python gRPC service that integrates devices with eLabFTW for Wi-Fi/BLE/CSI fleet automation.

## Implemented API surface
- `fleet.v1.FleetManager` (legacy-compatible)
  - `Hello(AgentInfo) -> ServerConfig`
  - `GetPolicy(PolicyRequest) -> PolicyResponse`
  - `PublishEvent(Event) -> Ack`
- `fleet.v3.FleetManager` (primary; PREPARE/EXECUTION/MAINTENANCE split)
  - `GetPolicy(GetPolicyRequest) -> GetPolicyResponse`
  - `AckPrepared(AckPreparedRequest) -> AckPreparedResponse`
  - `ReportCommandStatus(ReportCommandStatusRequest) -> StatusAck`
  - `ReportUploadStatus(ReportUploadStatusRequest) -> StatusAck`
  - `ReportArtifactUploadStatus(ReportArtifactUploadStatusRequest) -> StatusAck`
  - `PublishReport(PublishReportRequest) -> PublishReportResponse`
  - Server also exposes `fleet.v2.FleetManager` as a backward-compatible endpoint.

## `fleet.v3` runtime behavior
- PREPARE:
  - device resource discovery/creation in eLabFTW Items/Resources
  - periodic presence updates (`last_seen_at`, capabilities, `fleet_v2_runtime` metadata)
  - policy selection from fleet-tagged experiments (`fleet` by default)
  - canonical policy hashing (`policy_revision = sha256:<hash>`)
  - diagram-first design commands (`SYNC`, `OBSERVE`) are normalized into executable runtime policy before dispatch
- EXECUTION:
  - `ReportCommandStatus` records command start/finish/failure in near-real-time
  - status calls are normalized into legacy event ingestion path (`_ingest_v1_event`) to keep one metadata/update pipeline
- MAINTENANCE:
  - `ReportUploadStatus` reports metrics sink upload state (`ACK`, `ERROR`, `PENDING`, `SKIPPED`)
  - `ReportArtifactUploadStatus` reports per-artifact upload state and optional URI/hash/size metadata
  - `PublishReport` remains the authoritative run close-out + dedupe decision (`ACCEPTED`/`DUPLICATE`/`REJECTED`)

## Status mapping (v3 status RPC -> ingested event)
- `ReportCommandStatus`:
  - `COMMAND_STATUS_STAGE_STARTED` -> `COMMAND_STARTED`
  - `COMMAND_STATUS_STAGE_FINISHED` -> `COMMAND_FINISHED`
  - `COMMAND_STATUS_STAGE_FAILED` -> `COMMAND_FAILED`
- `ReportUploadStatus`:
  - `UPLOAD_ACK` / `UPLOAD_SKIPPED` -> `COMMAND_FINISHED` (`command_id=upload:<kind>`, `exit_code=0`)
  - `UPLOAD_ERROR` -> `COMMAND_FAILED` (`exit_code=1`)
  - `UPLOAD_PENDING` / unspecified -> `COMMAND_STARTED`
- `ReportArtifactUploadStatus`:
  - `UPLOAD_ACK` / `UPLOAD_SKIPPED` -> `ARTIFACT_UPLOADED`
  - `UPLOAD_ERROR` / `UPLOAD_PENDING` / unspecified -> `ERROR`

## Event, state, and idempotency notes
- Event idempotency remains keyed by `event_id`.
- Runtime summary/status updates now treat `COMMAND_FAILED` as failure:
  - run status mapping: `COMMAND_FAILED` -> `PARTIAL`
  - run summary counter: `commands_failed` increments on `COMMAND_FAILED`
- `GetPolicyResponse.NOT_MODIFIED` contract:
  - server may return only `policy_id` (without policy body)
  - clients are expected to reuse locally cached policy body for the same `policy_id`

## HTTP sidecar
- `GET /metrics` (Prometheus scrape endpoint, default `9108`)
- `POST /ingest/v1/metrics` (lightweight JSON ingest for constrained senders)
- `POST /ingest/v1/artifacts` (JSON+base64 artifact ingest; uploads to eLabFTW experiment attachments)

## Environment variables
- `ELAB_BASE_URL` (default: `https://web/api/v2`)
- `ELAB_API_KEY`
- `ELAB_VERIFY_TLS` (`false` by default)
- `ELAB_REQUEST_TIMEOUT_S` (`20`; AWS compose defaults to `180` for large artifact finalization)
- `GATEWAY_PORT` (`50060` by default)
- `FLEET_EXPERIMENT_TAG` (`fleet`)
- `RESOURCE_TAG_PREFIX` (`device:`)
- `POLICY_METADATA_KEYS` (`fleet.policy,policy,fleet_policy,policy_json`)
- `DATA_DIR` (`/data`)
- `HELLO_POLL_INTERVAL_S` (`30`; AWS compose defaults to `60`)
- `DEVICE_ITEM_CACHE_TTL_S` (`600`; caches eLabFTW resource lookups so Hello/GetPolicy do not hammer MySQL)
- `REQUIRED_MIN_AGENT_VERSION` (empty by default)
- `ALLOW_LIVE_EVENTS` (`false`)
- `ENABLE_V2_DEVICE_STATE_PATCH` (`true`; set `false` to disable v2 item metadata runtime-state patching)
- `RESOURCE_STATUS_ID_MAP_JSON` (optional JSON object for runtime-state -> eLab status id resolution by status title, e.g. `{"waiting":7,"operational":2,"open":8,"processed":6,"maintenance mode":1}`)
- `METRICS_BIND` (`0.0.0.0`)
- `METRICS_PORT` (`9108`)
- `METRICS_EXCLUDE_PREFIXES` (empty by default; comma-separated metric-name prefixes to suppress from Prometheus/Mimir export)
- `ENABLE_METRICS_INGEST_JOURNAL` (`false`; set `true` only for short-lived HTTP ingest debugging)
- `INGEST_API_TOKEN` (empty by default; set to require `x-ingest-token` on HTTP ingest)
- `ARTIFACT_MAX_BYTES` (max accepted artifact bytes for `/ingest/v1/artifacts`, default `20971520`; AWS compose defaults to `536870912` for PCAP uploads)

## Build/run
Protobuf code is generated during Docker build from repo-root context (`shared/proto` is copied directly):

```bash
docker compose -f infrastructure/docker-compose.yml build monad-fleet-service model-device
docker compose -f infrastructure/docker-compose.yml up -d monad-fleet-service model-device
```
