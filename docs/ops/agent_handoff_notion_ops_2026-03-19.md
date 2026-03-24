# Agent Handoff: Notion Ops Flow + Smoke Validation (2026-03-19)

This note is the operational handoff for next Codex windows/agents.

## Scope

- Local Docker stack (`web`, `monad-fleet-service`, `model-device`, Prometheus/Mimir/Grafana)
- Smoke orchestrator: `scripts/smoke/v3_end_to_end_smoke.sh`
- Notion ops capture layer (run/incident/task)

## Implemented in This Session

1. Added Notion ops controls to smoke flow:
   - `NOTION_SYNC`, `NOTION_SYNC_STRICT`, `NOTION_API_TOKEN`
   - `OPS_RUN_ID`, `OPS_OUTPUT_DIR`, `OPS_*_CONTEXT_PATH`
2. Added exit hooks/traps that always emit ops context JSON:
   - `latest_run_context.json`
   - `latest_incident_context.json` (on failure)
   - `latest_task_context.json` (on failure)
3. Added Notion hook scripts:
   - `scripts/ops/notion_log_run.sh`
   - `scripts/ops/notion_log_incident.sh`
   - `scripts/ops/notion_log_task.sh`
4. Added strict behavior:
   - if smoke succeeds but Notion hook fails and `NOTION_SYNC_STRICT=true`, exit code becomes `90`.
5. Fixed failure classification edge case:
   - any exit before `SMOKE_LAST_STEP=8_done` is treated as failure for ops capture.
6. Updated timestamps in embedded Python snippets to timezone-aware UTC (`datetime.now(timezone.utc)`).

## Runtime Verification Performed

1. Success path, fallback mode (`NOTION_SYNC=false`):
   - smoke completed, run context written.
   - experiment id: `203` (cleaned up).
2. Success path, sync non-strict (`NOTION_SYNC=true`, `NOTION_SYNC_STRICT=false`, no token):
   - smoke completed, warning from hook, exit `0`.
   - experiment id: `204` (cleaned up).
3. Success path, sync strict (`NOTION_SYNC=true`, `NOTION_SYNC_STRICT=true`, no token):
   - smoke completed, hook warning, final exit `90` (expected).
   - experiment id: `205` (cleaned up).
4. Failure path (input validation failure):
   - mismatch `PI_HOSTS_CSV` vs `AGENT_IDS_CSV` produced exit `1`.
   - run + incident + task contexts written; hook warnings observed (no token).
5. Failure edge case check (`RSSI_DURATION_S=abc` with `set -u` style failure):
   - verified failure now correctly writes run + incident + task contexts and exits non-zero.

## Current Status

- End-to-end ops flow is implemented and runtime-verified for:
  - pass path,
  - fail path,
  - strict/non-strict Notion behavior.
- Real Notion API write is **not** validated yet in this environment because `NOTION_API_TOKEN` was not set.

## Open Items for Next Session

1. Validate live Notion writes with real token:
   - run smoke with `NOTION_SYNC=true NOTION_SYNC_STRICT=true`.
2. Verify Notion DB schema compatibility:
   - scripts currently write to `Name` title property.
3. Optional improvement:
   - capture real agent `run_id` automatically (today `OPS_RUN_ID` is external input).
4. Optional improvement:
   - implement upsert/search by `run_id` instead of always creating new pages.

## Quick Start for Next Session

```bash
# 1) Export token (do not commit it)
export NOTION_API_TOKEN="<token>"

# 2) Run strict smoke with ops sync
OPS_RUN_ID="ops-live-$(date +%Y%m%d-%H%M%S)" \
NOTION_SYNC=true \
NOTION_SYNC_STRICT=true \
RUN_PI=false \
SKIP_CORE_SERVICES_RESTART=true \
DO_BUILD=false \
RESET_STATE=false \
RESET_METRICS=false \
RESET_GRAFANA=false \
CLEANUP=true \
SMOKE_PROFILE=rssi \
RSSI_DURATION_S=10 \
RSSI_INTERVAL_S=5 \
scripts/smoke/v3_end_to_end_smoke.sh

# 3) Fallback artifacts if API sync fails
ls -la output/ops/
cat output/ops/latest_run_context.json
```

