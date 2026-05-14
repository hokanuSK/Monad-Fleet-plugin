# Experiment JSON Authoring Guide

This guide explains how to create a FleetManager experiment from JSON, what each part of the document means, and how the design JSON is normalized before the agent executes it.

Use this together with:

- [docs/experiment_execution_window_design.example.json](/Users/admin/FleetManager/docs/experiment_execution_window_design.example.json:1)
- [docs/experiment_json_v3_examples.md](/Users/admin/FleetManager/docs/experiment_json_v3_examples.md:1)

## Mental Model

There are three different layers involved:

1. eLab experiment record
   This is the actual experiment object stored in eLabFTW. It has a title, tags, body, and metadata.
2. design JSON
   This is the JSON you author under `fleet.policy`. It can contain both design commands like `SYNC` and `OBSERVE`, and executable commands like `WIFI_SCAN`, `BLE_SCAN`, `CAPTURE_CSI`, and `SHELL`.
3. normalized runtime policy
   This is what the fleet service serves to the agent. Design commands are not executed directly. They are transformed into runtime scheduling and inherited env values.

The current normalizer lives in [policy_design.py](/Users/admin/FleetManager/src/fleet-service/monad_fleet_service/policy_design.py:1). The helper that creates an eLab experiment from a JSON file lives in [design_experiment.py](/Users/admin/FleetManager/src/fleet-service/monad_fleet_service/design_experiment.py:1).

## Authoring Workflow

### 1. Start From The Checked-In Example

Copy the current example and edit it for your run:

```bash
cp docs/experiment_execution_window_design.example.json /tmp/my_experiment.json
```

The checked-in example is intentionally conservative:

- single-radio-friendly
- Wi-Fi on `wlan0`
- BLE on `hci0`
- no CSI in the runnable example yet

### 2. Set The Experiment Identity

Edit these first:

- `experiment_id`
  Stable logical name for the experiment definition.
- `SYNC.target_selector.device_ids`
  Which agent ids should receive the policy.

Example:

```json
{
  "experiment_id": "crowd-monitoring-baseline-lab-a",
  "command_groups": [
    {
      "commands": [
        {
          "id": "sync-maintenance-window",
          "type": "SYNC",
          "target_selector": {
            "device_ids": ["ag-pi-01"]
          }
        }
      ]
    }
  ]
}
```

`experiment_id` is part of the policy itself. `target_selector` now belongs to the `SYNC` design command, and the normalizer derives the runtime policy selector from it. The actual numeric eLab experiment id is assigned later when the helper creates the record.

### 3. Set The Time Windows

There are two main windows you author on the `SYNC` command:

- `validity_policy_window`
  The overall validity window for policy selection.
- `measure_window`
  The time in which measurement is expected to happen.

Example:

```json
{
  "id": "sync-maintenance-window",
  "type": "SYNC",
  "target_selector": {
    "device_ids": ["ag-pi-01"]
  },
  "validity_policy_window": {
    "from": "2026-04-28T18:00:00Z",
    "to": "2026-04-28T19:15:00Z"
  },
  "measure_window": {
    "from": "2026-04-28T18:00:00Z",
    "to": "2026-04-28T18:30:00Z"
  }
}
```

If you create the experiment through the helper, these windows can be retimed automatically by `EXPERIMENT_TIME_SHIFT_START_MINUTES`. The normalizer then derives runtime `range` and `reporting.measure_window` from these command-owned windows.

### 4. Add Design Commands

The current design layer uses two special commands:

- `SYNC`
  Describes upload timing and slotting.
- `OBSERVE`
  Describes observability, metrics, and log-bundling expectations.

These are written inside `command_groups[].commands[]`, but they are not executed directly by the agent.

### 5. Add Executable Commands

These are the commands the runtime can execute today:

- `WIFI_SCAN`
- `BLE_SCAN`
- `CAPTURE_CSI`
- `SHELL`

The current checked-in example uses:

1. `SYNC`
2. `OBSERVE`
3. `WIFI_SCAN`
4. `BLE_SCAN`
5. `SHELL`

CSI is intentionally omitted from the runnable example for now, but the normalizer and runtime still support `CAPTURE_CSI` as an executable command type.

### 6. Set Per-Command Behavior

Every command usually has these fields:

- `id`
  Stable command identifier for logs and events.
- `type`
  Command kind.
- `timeout_s`
  Hard runtime limit.
- `retries`
  Retry count after the first attempt.
- `cmdline`
  Only for commands that need a shell command, such as `SHELL` or some CSI variants.
  Prefer this as a top-level command field. The normalizer also accepts `SHELL.env.cmdline` for compatibility with early draft JSON and promotes it to the runtime command line.
- `env`
  Command-specific configuration.
- `expected_artifacts`
  Files expected to exist after the command.

### 7. Create The Experiment Record In eLabFTW

The helper expects the JSON file as base64 in `EXPERIMENT_JSON_B64` and then creates a real eLab experiment with the policy stored in metadata.

The easiest supported full-flow entrypoint is:

```bash
PI_HOST=monad-rpi5.local \
EXPERIMENT_JSON_FILE=docs/experiment_execution_window_design.example.json \
scripts/smoke/pi_execution_window_design.sh
```

If you only want to create the eLab experiment from the JSON file and not run the Pi flow, use the container-side helper directly:

```bash
EXPERIMENT_JSON_B64="$(base64 < /tmp/my_experiment.json | tr -d '\n')" \
docker compose -f infrastructure/docker-compose.yml exec -T monad-fleet-service sh -lc \
  "EXPERIMENT_JSON_B64='${EXPERIMENT_JSON_B64}' \
   TARGET_DEVICE_IDS_CSV='ag-pi-01' \
   SMOKE_TITLE_PREFIX='Fleet Design JSON' \
   SMOKE_TAGS_CSV='fleet,design-json,created-by:monad-fleet' \
   EXPERIMENT_TIME_SHIFT_START_MINUTES='-2' \
   python -m monad_fleet_service.design_experiment"
```

That command prints the new numeric eLab experiment id.

## What Each Top-Level Field Means

### `fleet.policy`

This is the actual Fleet policy payload. The current helper accepts either:

- `{ "fleet": { "policy": { ... } } }`
- `{ "policy": { ... } }`
- `{ ... }` where the document itself is already the policy object

The helper normalizes that into the `fleet.policy` wrapper before storing it.

### `experiment_id`

Human-controlled logical identifier for the policy family.

Use it for:

- grouping related runs
- distinguishing baseline vs follow-up variants
- stable naming across recreated eLab experiments

Do not confuse this with the numeric eLab experiment id.

### `target_selector`

Controls which devices can match the policy. In the current authoring shape, write it on the `SYNC` command. Runtime normalization hoists it to policy-level `target_selector` before fleet selection.

Current important field:

- `device_ids`

Example:

```json
{
  "id": "sync-maintenance-window",
  "type": "SYNC",
  "target_selector": {
    "device_ids": ["ag-pi-01", "ag-pi-02"]
  }
}
```

The helper can override this with `TARGET_DEVICE_IDS_CSV`.

### `SYNC.validity_policy_window`

Overall time in which the policy is eligible for assignment.

Think of it as:

- `SYNC.measure_window`: when the device should measure
- `SYNC.validity_policy_window`: when the policy is valid at all

### `SYNC.measure_window`

The intended measurement phase window.

This is the main input used by `SYNC` normalization when the runtime derives upload timing.

`SYNC.reporting` is accepted as an authoring alias for the same window because the current design draft used that name. Prefer `measure_window` in new JSON so the field name stays explicit.

### `command_groups`

Ordered groups of work. The agent executes commands in group order and then command order within the group.

Current important fields:

- `id`
- `failure_mode`
- `commands`

Optional targeting fields:

- `target_selector` on the command group
- `target_selector` on an individual command

These selectors are evaluated server-side during runtime policy emission. A matched
device still has to satisfy the policy-level selector first, then:

- non-matching command groups are omitted for that device
- non-matching commands are omitted for that device
- empty groups are omitted for that device

Example:

```json
{
  "id": "ble-role-split",
  "failure_mode": "CONTINUE_ON_ERROR",
  "commands": [
    {
      "id": "ble-advertise-monad-02",
      "type": "BLE_SCAN",
      "target_selector": {
        "device_ids": ["24:eb:16:e3:6a:07"]
      },
      "env": {
        "BLE_SCAN_MODE": "advertise"
      }
    },
    {
      "id": "ble-collect-monad-03",
      "type": "BLE_SCAN",
      "target_selector": {
        "device_ids": ["2c:cf:67:80:f5:86"]
      },
      "env": {
        "BLE_SCAN_MODE": "continuous"
      }
    }
  ]
}
```

### `failure_mode`

The checked-in example uses `FAIL_FAST`, meaning a failure should stop later commands in the group. If you change this later, document the intended failure semantics clearly because it changes how the run behaves operationally.

## What Gets Normalized

`SYNC` and `OBSERVE` are design commands. They are consumed by the fleet service and removed from the executable runtime command list.

### `SYNC` normalization

`SYNC` contributes to:

- `range`
- `reporting.measure_window`
- `reporting.upload_window`
- `reporting.require_upload_window`
- `reporting.slotting.slot_count`
- `reporting.slotting.jitter_s`

It also injects inherited runtime env values such as:

- `UPLOAD_WINDOW_FROM`
- `UPLOAD_WINDOW_TO`
- `UPLOAD_WINDOW_REQUIRED`
- `UPLOAD_SLOT_COUNT`
- `UPLOAD_SLOT_JITTER_S`

If `reporting.upload_window.from` is not explicitly authored, the normalizer derives it from `SYNC.measure_window.to`. If no upload duration is provided, upload ends at `SYNC.validity_policy_window.to`.

### `OBSERVE` normalization

`OBSERVE` does not create a separate runtime command. Instead, its `env` is inherited by later executable commands in the same group.

That means settings like:

- `OBSERVE_METRICS_SINK`
- `OBSERVE_METRIC_GROUPS`
- `OBSERVE_LOG_*`
- `PROM_REMOTE_WRITE_*`

end up attached to runtime commands such as `WIFI_SCAN`, `BLE_SCAN`, or `SHELL`.

### Artifact normalization

Metrics are not eLabFTW artifacts. Wi-Fi RSSI, BLE RSSI, CSI summary counters, command status, and device health metrics should go through the agent metrics path into Prometheus, then Mimir for Grafana.

eLabFTW should receive coarse run evidence only:

- measurement output artifacts, such as a Wi-Fi scan log or PCAP, a BLE scan JSONL/log, or CSI files when CSI is re-enabled
- one merged text/log bundle named like `wireless-run-evidence-bundle.tar.gz`
- one `run-summary.json` for run status and pointers, not a duplicate telemetry store

For Pi design runs, `ARTIFACT_UPLOAD_DURING_MEASURE=false` keeps pieces on the device until report replay. Fleet then stages those pieces server-side and uploads the same normalized eLabFTW artifact set.

If a Pi needs to upload during measurement because local storage is limited, keep `ARTIFACT_UPLOAD_DURING_MEASURE=true` and set `ARTIFACT_EVICT_AFTER_UPLOAD=true`. Fleet stages those in-run artifacts under its data directory and uploads the same coarse `wireless-run-evidence-bundle.tar.gz` plus `run-summary.json` to eLabFTW when it receives the run summary.

### Runtime command filtering

After normalization, only these command types remain executable:

- `WIFI_SCAN`
- `BLE_SCAN`
- `CAPTURE_CSI`
- `SHELL`

## Before / After Example

### Authored design intent

You write:

```json
{
  "type": "SYNC",
  "validity_policy_window": {
    "from": "2026-04-22T03:00:00Z",
    "to": "2026-04-22T04:15:00Z"
  },
  "measure_window": {
    "from": "2026-04-22T03:00:00Z",
    "to": "2026-04-22T03:30:00Z"
  },
  "env": {
    "SYNC_CRON": "30 3 * * *",
    "SYNC_REQUIRE_UPLOAD_WINDOW": "true"
  }
}
```

### Normalized runtime effect

The runtime policy gets:

```json
{
  "reporting": {
    "measure_window": {
      "from": "2026-04-22T03:00:00Z",
      "to": "2026-04-22T03:30:00Z"
    },
    "upload_window": {
      "from": "2026-04-22T03:30:00Z",
      "to": "2026-04-22T04:15:00Z"
    },
    "require_upload_window": true
  }
}
```

And a later executable command receives inherited env like:

```json
{
  "type": "WIFI_SCAN",
  "env": {
    "SYNC_CRON": "30 3 * * *",
    "UPLOAD_WINDOW_FROM": "2026-04-22T03:30:00Z",
    "UPLOAD_WINDOW_TO": "2026-04-22T04:15:00Z",
    "UPLOAD_WINDOW_REQUIRED": "true",
    "OBSERVE_METRICS_SINK": "mimir",
    "WIFI_SCAN_IFACE": "wlan0"
  }
}
```

## Minimal Template

Use this when starting from scratch:

```json
{
  "fleet": {
    "policy": {
      "experiment_id": "replace-me",

      "command_groups": [
        {
          "id": "main",
          "failure_mode": "FAIL_FAST",
          "commands": [
            {
              "id": "sync-window",
              "type": "SYNC",
              "timeout_s": 5,
              "retries": 0,
              "target_selector": {
                "device_ids": ["ag-pi-01"]
              },
              "validity_policy_window": {
                "from": "2026-04-28T18:00:00Z",
                "to": "2026-04-28T19:15:00Z"
              },
              "measure_window": {
                "from": "2026-04-28T18:00:00Z",
                "to": "2026-04-28T18:30:00Z"
              },
              "env": {
                "SYNC_TIMEZONE": "UTC",
                "SYNC_CRON": "30 3 * * *",
                "SYNC_REQUIRE_UPLOAD_WINDOW": "true"
              }
            },
            {
              "id": "observe-sinks",
              "type": "OBSERVE",
              "timeout_s": 10,
              "retries": 0,
              "env": {
                "OBSERVE_METRICS_SINK": "mimir",
                "OBSERVE_METRIC_GROUPS": "run,device,wifi,ble"
              }
            },
            {
              "id": "wifi-scan",
              "type": "WIFI_SCAN",
              "timeout_s": 60,
              "retries": 0,
              "env": {
                "WIFI_SCAN_IFACE": "wlan0",
                "WIFI_SCAN_CHANNELS": "1,6,11",
                "WIFI_SCAN_DURATION_S": "60"
              }
            }
          ]
        }
      ]
    }
  }
}
```


## Current Limits And Gotchas

- `SYNC` and `OBSERVE` are not executed as standalone runtime commands.
- The current runnable example is Wi-Fi + BLE + `SHELL`, not CSI.
- `EXPERIMENT_TIME_SHIFT_START_MINUTES` can retime the authored `SYNC` windows at creation time, so the stored eLab experiment may differ from the static example file.
- Top-level `range` and `reporting.measure_window` are still accepted for older JSON, but the preferred current shape keeps those windows on `SYNC` and derives runtime fields.
- `TARGET_DEVICE_IDS_CSV` can override `SYNC.target_selector.device_ids` at creation time.
- `WIFI_SCAN_IFACE`, `BLE_SCAN_IFACE`, and CSI-related env values can also be overridden at creation time by the helper.

## Next Step

When you are ready, we can take your target experiment JSON and normalize it section by section:

1. authored design JSON
2. expected normalized runtime policy
3. final device-specific overrides at creation time
