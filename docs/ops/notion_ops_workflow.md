# Notion Ops Workflow

This document defines how FleetManager agents should capture operational context in Notion.

## Scope

- Keep execution memory in Notion (runs, incidents, decisions, tasks).
- Keep code/proto/scripts in Git.
- Keep experiment metadata/artifacts in eLabFTW.
- Keep time-series metrics in Prometheus/Grafana.

## Environment Flags

Smoke script supports optional Notion sync:

- `NOTION_SYNC=true|false` (default `false`)
- `NOTION_SYNC_STRICT=true|false` (default `false`)
- `NOTION_API_TOKEN=<integration_token>` (required for API write)
- `NOTION_RUNS_DB_ID` (default `8dc731920bf34966bb711472100c7057`)
- `NOTION_INCIDENTS_DB_ID` (default `da72692b8106430bb1e3892866d41d20`)
- `NOTION_TASKS_DB_ID` (default `4a5936bb0baa4614bb61bc8daed18cee`)
- `OPS_RUN_ID` (optional explicit run id for cross-linking)

Ops export files:

- `OPS_OUTPUT_DIR` (default `output/ops`)
- `OPS_RUN_CONTEXT_PATH` (default `output/ops/latest_run_context.json`)
- `OPS_INCIDENT_CONTEXT_PATH` (default `output/ops/latest_incident_context.json`)
- `OPS_TASK_CONTEXT_PATH` (default `output/ops/latest_task_context.json`)

## Runtime Behavior

When `scripts/smoke/v3_end_to_end_smoke.sh` exits:

1. It writes `latest_run_context.json`.
2. If `NOTION_SYNC=true`, it calls:
   - `scripts/ops/notion_log_run.sh`
3. If smoke failed:
   - it writes `latest_incident_context.json`
   - it writes `latest_task_context.json`
   - if `NOTION_SYNC=true`, it calls:
     - `scripts/ops/notion_log_incident.sh`
     - `scripts/ops/notion_log_task.sh`

If `NOTION_SYNC_STRICT=true`, successful smoke exits can be turned into failure when Notion hook fails.

## Manual Execution

Run hooks directly:

```bash
scripts/ops/notion_log_run.sh output/ops/latest_run_context.json
scripts/ops/notion_log_incident.sh output/ops/latest_incident_context.json
scripts/ops/notion_log_task.sh output/ops/latest_task_context.json
```

## Fallback Mode

If Notion API is unavailable or token is missing:

- smoke still writes `output/ops/*.json`
- hooks return warning/error
- these JSON files are the canonical fallback handoff for manual Notion entry

## Naming Rules

Every operational record should include:

- `run_id` (when available)
- `experiment_id` (`SMOKE_EXPERIMENT_ID`)
- `device_id`/target device context
- profile (`rssi|full|wireless_spec|rfpaper`)
- pass/fail result
