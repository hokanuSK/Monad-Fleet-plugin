# Device Simulator / Agent (v3 proto)

Runtime client for Fleet manager integration.

## Entry points
- `sim_device_client.py`: legacy `fleet.v1` simulator flow
- `agent_v2_client.py`: wrapper that starts `agent_v2.main`
- `agent_v2/`: prepare/execute/maintenance runtime implementation (uses `fleet.v3` proto)

## Agent lifecycle summary
- PREPARE:
  - calls `GetPolicy(agent, last_policy_id)`
  - acknowledges with `AckPrepared(...)` when applicable
- EXECUTION:
  - executes command groups from policy
  - emits command runtime status via `ReportCommandStatus`
- MAINTENANCE:
  - replays pending runs from local spool
  - uploads metrics/logs and artifacts
  - publishes final report via `PublishReport`

## `NOT_MODIFIED` policy behavior
`agent_v2.main` maintains an in-memory `cached_policy`:
- `OK`: cache latest policy body
- `NOT_MODIFIED`:
  - use server-provided policy body when present, otherwise
  - execute cached policy when `policy_id` matches, otherwise
  - skip execution and run maintenance-only replay

## Runtime status reporting (diagram-aligned)
`agent_v2.main` reports:
- command status: `ReportCommandStatus`
  - start -> `COMMAND_STATUS_STAGE_STARTED`
  - end success -> `COMMAND_STATUS_STAGE_FINISHED`
  - end failure -> `COMMAND_STATUS_STAGE_FAILED`
- maintenance metrics/logs upload status: `ReportUploadStatus`
- maintenance artifact upload status: `ReportArtifactUploadStatus`

When connected to an older server that does not implement these RPCs, `UNIMPLEMENTED` is tolerated and execution continues.

## Local spool layout
Default root: `DATA_ROOT` (default `./data`)
- pending runs: `DATA_ROOT/pending/<run_id>/`
- sent runs: `DATA_ROOT/sent/<run_id>/`
- per-run files: `context.json`, `events.ndjson`, `report.json`, `artifacts/`

## Key runtime knobs
- `FLEET_MANAGER_HOST`, `FLEET_MANAGER_PORT`
- `AGENT_ID`, `AGENT_VERSION`
- `CONTROL_PLANE_MODE`, `CONTROL_PLANE_IFACE`
- `CAPABILITIES`
- `MAX_SYNC_CYCLES`
- `EXECUTE_POLICY`
- `DATA_ROOT`, `SENT_RETENTION_DAYS`
