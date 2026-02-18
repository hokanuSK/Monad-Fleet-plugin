# Monad Fleet Service

Python gRPC service that integrates devices with eLabFTW for Wi-Fi sensing automation.

## Implemented MVP
- gRPC API:
  - `Hello(AgentInfo) -> ServerConfig`
  - `GetPolicy(PolicyRequest) -> PolicyResponse`
  - `PublishEvent(Event) -> Ack`
- Versioned API:
  - `fleet.v1.FleetManager` (legacy-compatible)
  - `fleet.v2.FleetManager` (standardized PREPARE/REPORT draft)
- HTTP sidecar:
  - `GET /metrics` (Prometheus scrape endpoint, default port `9108`)
  - `POST /ingest/v1/metrics` (lightweight JSON ingest for constrained senders)
- Device resource discovery/creation in eLabFTW Items/Resources.
- Device `last_seen_at` and capabilities updates on `Hello`.
- Policy fetch from fleet-tagged experiments (`fleet` by default), using experiment metadata from JSON editor/custom fields.
  - Default metadata keys checked in order: `fleet.policy`, `policy`, `fleet_policy`, `policy_json`.
- Canonical policy hashing (`policy_revision = sha256:<hash>`).
- Event ingestion with idempotency (`event_id`) and scheduler event metadata updates.
- Local file state only (`/data/state.json`) for dedupe/run mapping. No separate DB service.
- Metadata write compatibility for some eLabFTW builds:
  - Item/event metadata writes are sent as JSON string.
  - Other metadata object writes retry once with metadata serialized as JSON string.
- `fleet.v2` safety defaults:
  - `PublishEvents` is disabled by default for RF-sharing deployments.
  - `DUAL_NIC` prepare ack requires `route_verified=true` and ethernet control-plane interface.

## Environment variables
- `ELAB_BASE_URL` (default: `https://web/api/v2`)
- `ELAB_API_KEY`
- `ELAB_VERIFY_TLS` (`false` by default)
- `GATEWAY_PORT` (`50060` by default)
- `FLEET_EXPERIMENT_TAG` (`fleet`)
- `RESOURCE_TAG_PREFIX` (`device:`)
- `POLICY_METADATA_KEYS` (`fleet.policy,policy,fleet_policy,policy_json`)
- `DATA_DIR` (`/data`)
- `HELLO_POLL_INTERVAL_S` (`30`)
- `REQUIRED_MIN_AGENT_VERSION` (empty by default)
- `ALLOW_LIVE_EVENTS` (`false`)
- `METRICS_BIND` (`0.0.0.0`)
- `METRICS_PORT` (`9108`)
- `INGEST_API_TOKEN` (empty by default; set to require `x-ingest-token` on HTTP ingest)

## Build/run
The protobuf code is generated during Docker build:
```bash
docker compose build monad-fleet-service
docker compose up monad-fleet-service
```
