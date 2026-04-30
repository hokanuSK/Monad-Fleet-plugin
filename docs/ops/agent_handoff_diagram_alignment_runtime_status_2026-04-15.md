# Agent Handoff: Diagram Alignment Runtime Status + Cached Policy Execution (2026-04-15)

## Goal
Align runtime implementation with active Mermaid lifecycle/runtime diagrams in `docs/diagrams/`.

Primary target flows from diagram source of truth (`sequence-lifecycle-v2-normalized.mmd`):
- explicit runtime command status reporting (`ReportCommandStatus`)
- explicit maintenance upload status reporting (`ReportUploadStatus`, `ReportArtifactUploadStatus`)
- `GetPolicyResponse.NOT_MODIFIED` execution path using local cached policy body

## Scope Completed
Changes were implemented across proto contract, fleet service v2 adapter, v1 summary mapping, and v2 agent runtime.

### 1) Protobuf contract updates
File:
- `shared/proto/fleet_gateway_v2.proto`

Added `fleet.v2.FleetManager` RPCs:
- `ReportCommandStatus(ReportCommandStatusRequest) -> StatusAck`
- `ReportUploadStatus(ReportUploadStatusRequest) -> StatusAck`
- `ReportArtifactUploadStatus(ReportArtifactUploadStatusRequest) -> StatusAck`

Added message/enum types:
- `StatusAck`
- `CommandStatusStage`
  - `COMMAND_STATUS_STAGE_STARTED`
  - `COMMAND_STATUS_STAGE_FINISHED`
  - `COMMAND_STATUS_STAGE_FAILED`
- `ReportCommandStatusRequest`
- `UploadPayloadKind`
  - `METRICS_LOGS`
  - `ARTIFACT`
- `UploadStatus`
  - `UPLOAD_ACK`
  - `UPLOAD_ERROR`
  - `UPLOAD_PENDING`
  - `UPLOAD_SKIPPED`
- `ReportUploadStatusRequest`
- `ReportArtifactUploadStatusRequest`

### 2) Fleet v2 servicer status ingestion bridge
File:
- `src/fleet-service/monad_fleet_service/servicer_v2.py`

Added new handlers:
- `ReportCommandStatus`
- `ReportUploadStatus`
- `ReportArtifactUploadStatus`

Implementation strategy:
- v2 status RPC payloads are transformed into `fleet.v1.Event` records.
- events are ingested through existing `self._core._ingest_v1_event(...)` path.
- this preserves existing dedupe and metadata patch logic without introducing a second status pipeline.

Mapping details:
- `ReportCommandStatus.stage`:
  - `STARTED` -> `COMMAND_STARTED`
  - `FINISHED` -> `COMMAND_FINISHED`
  - `FAILED` -> `COMMAND_FAILED`
- `ReportUploadStatus.status`:
  - `ACK` / `SKIPPED` -> `COMMAND_FINISHED` (`exit_code=0`, `command_id=upload:<kind>`)
  - `ERROR` -> `COMMAND_FAILED` (`exit_code=1`)
  - `PENDING` / unspecified -> `COMMAND_STARTED`
- `ReportArtifactUploadStatus.status`:
  - `ACK` / `SKIPPED` -> `ARTIFACT_UPLOADED`
  - otherwise -> `ERROR`

Input validation for all three RPCs:
- requires `agent_id`, `run_id`, `experiment_id`, `policy_id`
- returns `StatusAck(ok=false, reason=...)` on invalid request

### 3) Fleet v1 summary/status compatibility update
File:
- `src/fleet-service/monad_fleet_service/servicer_v1.py`

Adjusted status mapping:
- `_status_from_event(...)` now maps `COMMAND_FAILED` -> `PARTIAL`

Adjusted run summary counters:
- `_update_run_event(...)` now increments `commands_failed` for `COMMAND_FAILED` events.

Reason:
- v2 bridge now emits explicit `COMMAND_FAILED`; summary/status logic must count it as failure.

### 4) Agent v2 command runtime status reporting
File:
- `src/device-sim/agent_v2/main.py`

Added RPC helper methods:
- `_is_unimplemented_rpc(...)`
- `report_command_status(...)`
- `report_upload_status_from_report(...)`
- `report_artifact_upload_status_from_report(...)`

Execution loop changes:
- command start now emits `ReportCommandStatus(..., stage=STARTED)`
- command end now emits `ReportCommandStatus(..., stage=FINISHED|FAILED)`
- `measure_type` is included for WIFI/BLE/CSI command types

Event id convention for idempotent retries:
- start: `{run_id}:{command_id}:start`
- end: `{run_id}:{command_id}:end`
- upload metrics/logs: `{run_id}:upload:metrics_logs:<status>`
- artifact upload: `{run_id}:artifact:{artifact_name}:<status>`

Backward compatibility behavior:
- if server returns gRPC `UNIMPLEMENTED` on new status RPCs, agent logs debug and continues.

### 5) Agent v2 maintenance replay status hooks
File:
- `src/device-sim/agent_v2/sinks.py`

`flush_pending_reports(...)` extended with optional hooks:
- `report_upload_status_hook`
- `artifact_upload_status_hook`

Behavior:
- during pending replay, run-level metrics/logs upload status is reported via hook
- per-artifact upload outcome is reported via hook from `upload_report_artifacts_to_elab(...)`

### 6) NOT_MODIFIED cached policy execution path
File:
- `src/device-sim/agent_v2/main.py`

Added `cached_policy` behavior:
- on `GetPolicyResponse.OK`, policy body is cached locally in-memory
- on `GetPolicyResponse.NOT_MODIFIED`:
  - if server includes executable policy body, execute it
  - else if cached policy exists with matching `policy_id`, execute cached policy
  - else skip execution and run maintenance-only flush path

Reason:
- aligns agent behavior with lifecycle diagrams where `NOT_MODIFIED` means reuse prior policy, not always no-op.

## Documentation updates in this pass
- `src/fleet-service/README.md`
  - expanded v2 API docs and status/event mapping semantics
  - documented `NOT_MODIFIED` cache contract and `COMMAND_FAILED` runtime implications
- `docs/diagrams/README.md`
  - content guardrails now explicitly include `ReportUploadStatus`, `ReportArtifactUploadStatus`, and cached-policy `NOT_MODIFIED` behavior

## Validation performed
### Local static checks
- `python3 -m compileall` on changed Python files: passed (using `PYTHONPYCACHEPREFIX=/tmp`)

### Proto/stub/build checks
- local `python3 -m grpc_tools.protoc ...` failed in this environment:
  - cause: `grpc_tools` module not installed in host Python
- Docker build check attempted, but blocked by existing build-context/symlink issue:
  - error pattern: `COPY proto ./proto` not found in BuildKit context
  - this is an environment/repo-context problem observed independently from new logic changes

## Known risks / open items
1. Proto stub regeneration is not validated locally until build-context issue is resolved.
2. Full container e2e smoke for new status RPC path is pending successful image build.
3. Status-to-event mapping intentionally reuses legacy ingestion path; if future changes split pipelines, these mappings must be kept synchronized.

## Recommended next execution steps
1. Fix Docker build context for `proto` copy in affected Dockerfiles/build contexts.
2. Rebuild `monad-fleet-service` and `model-device` images.
3. Run:
   - `scripts/smoke/v3_end_to_end_smoke.sh`
4. Confirm in logs/metadata:
   - command STARTED/FINISHED/FAILED statuses appear during execution
   - maintenance upload status events appear for metrics/logs and artifacts
   - `NOT_MODIFIED` cycle executes from cache when policy body is omitted
