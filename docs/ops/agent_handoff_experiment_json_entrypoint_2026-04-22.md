# Agent Handoff: Experiment JSON Entrypoint Cleanup (2026-04-22)

## User Intent from Thread

- Keep one user-facing entrypoint JSON in `docs/experiment_json_v3_examples.md`.
- Keep structure parseable by current runtime (no invented fields).
- Show commands in the same execution order as diagrams.
- Keep env parameters as small as possible.
- Make naming human-readable.
- Keep scheduling for maintenance window setup (cron-style).

## Context Reviewed

- Diagrams:
  - `docs/diagrams/sequence-execution-runtime.mmd`
  - `docs/diagrams/sequence-lifecycle-v2-normalized.mmd`
  - `docs/diagrams/sequence-maintenance-runtime.mmd`
  - `docs/diagrams/sequence-policy-authoring-prepare.mmd`
  - `docs/diagrams/state-run-lifecycle.mmd`
- Proto:
  - `shared/proto/fleet_gateway_v3.proto`
- Runtime parse/translation path:
  - `src/fleet-service/monad_fleet_service/servicer_v2.py`
  - `src/device-sim/agent_v2/main.py`
  - `src/device-sim/agent_v2/core.py`
- Existing sample run metadata:
  - `artifacts/output/room_mock_216_experiment.json`
- Relevant docs/PDF inventory checked:
  - `docs/monad_fleet_grpc_interface_v2.tex` (protocol spec source)
  - `docs/knowledge-base/*.pdf` (present, mostly research context, not schema-defining for policy JSON)

## Current State (Important)

- `docs/experiment_json_v3_examples.md` is currently inconsistent and needs cleanup:
  - duplicate JSON block was inserted mid-document,
  - mixed field styles (`timeout_s`/`retries` and `timeout_ms` mentions),
  - text says "minimal parseable" but includes conflicting guidance.
- Git status currently shows:
  - modified diagrams (`sequence-execution-runtime.mmd`, `sequence-lifecycle-v2-normalized.mmd`, related SVGs),
  - untracked `docs/experiment_json_v3_examples.md`.

## Runtime Truth (What Actually Parses Today)

1. Executable command types in proto/runtime:
   - `SHELL`, `WIFI_SCAN`, `BLE_SCAN`, `CAPTURE_CSI`
2. Diagram-only (not proto command enums yet):
   - `SYNC`, `OBSERVE` shown in diagrams as conceptual lanes
3. Policy parser compatibility in `servicer_v2.py`:
   - accepts both `timeout_ms` and fallback `timeout_s`
   - accepts both `retry` object and legacy `retries`
4. Reporting window handling:
   - `reporting.measure_window` + `reporting.upload_window` + `slotting` are parsed and mapped to env
5. `metrics_sink`:
   - accepted at `policy.metrics_sink` (preferred), and also from `reporting.metrics_sink`
6. `sync_schedule_hint`:
   - currently documentation/user-hint level; not consumed into runtime scheduling logic directly by parser code.

## Conflict to Resolve Before Finalizing JSON

- User asked for "all used commands in same order as diagram".
- Strict runtime-parseable JSON cannot include `SYNC`/`OBSERVE` as command `type` today.
- Practical choices:
  1. Keep command list runtime-strict (`SHELL`, `WIFI_SCAN`, `BLE_SCAN`, `CAPTURE_CSI`) and explain `SYNC`/`OBSERVE` as future diagram stages.
  2. Represent `SYNC`/`OBSERVE` behavior through `SHELL` commands with explicit IDs (still parseable today).

## How I Would Continue (Next Pass)

1. Rewrite `docs/experiment_json_v3_examples.md` to a single clean document (no duplicate blocks).
2. Keep one canonical JSON only (single fenced `json` block) with stable structure:
   - `fleet.policy.experiment_id`
   - `target_selector`
   - `range`
   - `reporting` (`measure_window`, `upload_window`, `require_upload_window`, `slotting`, `sync_schedule_hint`)
   - `metrics_sink`
   - one `command_groups` list
3. Use runtime-safe command fields only:
   - `id`, `type`, `timeout_s` (or `timeout_ms`), `retries` (or `retry`), `env`, `expected_artifacts` (CSI)
4. Keep command order deterministic and readable:
   - `SHELL` (optional preflight/setup if chosen)
   - `WIFI_SCAN` baseline
   - `BLE_SCAN` baseline
   - `WIFI_SCAN` activity
   - `BLE_SCAN` activity
   - `CAPTURE_CSI` activity
   - `SHELL` (optional finalize marker if chosen)
5. Minimize env keys by factoring shared RF context keys to essentials only.
6. Keep scheduling examples as replacements of `reporting.sync_schedule_hint` only (no extra schema).
7. Validate final doc by extracting the canonical JSON and checking parseability (`jq`) plus field compatibility against parser logic.

## Suggested Naming Convention (for the next edit)

- Group:
  - `rf-maintenance-window-demo`
- Commands:
  - `setup-maintenance-context` (SHELL, optional)
  - `wifi-baseline-empty-room`
  - `ble-baseline-empty-room`
  - `wifi-activity-scripted-walk`
  - `ble-activity-scripted-walk`
  - `csi-activity-scripted-walk`
  - `finalize-run-context` (SHELL, optional)
- Schedule:
  - `daily-early-morning`, `weekdays-after-hours`, `weekly-deep-maintenance`

## Resume Checklist

- [ ] Decide whether to include optional `SHELL` setup/finalize commands.
- [ ] Replace current `docs/experiment_json_v3_examples.md` with one clean canonical version.
- [ ] Re-run consistency check for diagram wording vs supported command types.
- [ ] Keep only one maintained entrypoint JSON in the doc.
