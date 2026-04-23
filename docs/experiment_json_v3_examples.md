# FleetManager Experiment JSON

This document describes the current design-first experiment JSON entrypoint.
The checked-in example is:

- `docs/examples/experiment_execution_window_design.json`

The current command order in that file is:

1. `SYNC`
2. `OBSERVE`
3. `WIFI_SCAN`
4. `BLE_SCAN`
5. `SHELL`

The checked-in example is intentionally single-radio-friendly for the current Pi flow, so it uses `wlan0` for Wi-Fi work, `hci0` for BLE, and omits CSI for now.

## Proto Compatibility

The current proto does not need a new executable command type for `SYNC` or `OBSERVE`.

- `SYNC` is a design command. The fleet service normalizes it into `reporting.upload_window`, slotting, jitter, and upload requirements before the agent receives the runtime policy.
- `OBSERVE` is a design command. The fleet service fans its env into the executable measurement commands so observability settings travel with the runtime policy.
- Executable runtime commands remain the existing command types such as `WIFI_SCAN`, `BLE_SCAN`, `CAPTURE_CSI`, and `SHELL`.

That means the current diagram-first JSON is compatible with the current runtime path without a proto change.

Main scheduling field:

- `SYNC_CRON`
- Build and verify it here: https://crontab.guru/

## Canonical Checked-In Example

```json
{
  "fleet": {
    "policy": {
      "experiment_id": "exp-execution-window-design-demo",
      "target_selector": {
        "device_ids": [
          "ag-pi-01"
        ]
      },
      "range": {
        "from": "2026-04-22T03:00:00Z",
        "to": "2026-04-22T04:15:00Z"
      },
      "reporting": {
        "measure_window": {
          "from": "2026-04-22T03:00:00Z",
          "to": "2026-04-22T03:30:00Z"
        }
      },
      "command_groups": [
        {
          "id": "execution-window-demo",
          "failure_mode": "FAIL_FAST",
          "commands": [
            {
              "id": "sync-maintenance-window",
              "type": "SYNC",
              "timeout_s": 5,
              "retries": 0,
              "env": {
                "SYNC_TIMEZONE": "UTC",
                "SYNC_CRON": "30 3 * * *",
                "SYNC_CRON_DESCRIPTION": "Every day at 3:30 AM UTC",
                "SYNC_WINDOW_DURATION_MINUTES": "45",
                "SYNC_SLOT_COUNT": "2",
                "SYNC_SLOT_JITTER_S": "15",
                "SYNC_REQUIRE_UPLOAD_WINDOW": "true"
              }
            },
            {
              "id": "observe-maintenance-sinks",
              "type": "OBSERVE",
              "timeout_s": 10,
              "retries": 0,
              "env": {
                "OBSERVE_METRICS_SINK": "mimir",
                "OBSERVE_PROMETHEUS_JOB": "rpi-rf-measurement",
                "OBSERVE_METRICS_FLUSH_INTERVAL_S": "60",
                "OBSERVE_METRIC_GROUPS": "run,device,wifi,ble",
                "OBSERVE_CAPTURE_CHANNEL_UTILIZATION": "true",
                "OBSERVE_HASH_MAC_ADDRESSES": "true",
                "OBSERVE_HASH_ROTATION": "daily_salt",
                "OBSERVE_CAPTURE_PAYLOADS": "false",
                "OBSERVE_EXPECTED_WIFI_METRICS": "rssi_dbm,channel,frequency_mhz,frame_type,retry_flag,phy_rate,channel_utilization",
                "OBSERVE_EXPECTED_BLE_METRICS": "rssi_dbm,adv_type,uuid,major_minor,tx_power",
                "OBSERVE_EXPECTED_DEVICE_METRICS": "cpu_load,memory_free,disk_free,temp_c,uptime_s,interface_errors,ntp_offset_ms",
                "OBSERVE_METRICS_PROBE_URL": "http://mimir:9009/ready",
                "PROM_REMOTE_WRITE_ON_CMD": "sudo systemctl start vmagent",
                "PROM_REMOTE_WRITE_OFF_CMD": "sudo systemctl stop vmagent",
                "PROM_REMOTE_WRITE_CMD_TIMEOUT_S": "12",
                "OBSERVE_LOG_SOURCES": "command_stdout,command_stderr,journal,file",
                "OBSERVE_LOG_JOURNAL_UNITS": "monad-fleet-agent.service,vmagent.service",
                "OBSERVE_LOG_FILES": "/var/log/syslog,/var/log/messages,/tmp/runs/wifi/*.pcap,/tmp/runs/ble/*.jsonl",
                "OBSERVE_LOG_FORMAT": "jsonl",
                "OBSERVE_LOG_OUTPUT_DIR": "/tmp/observe-logs",
                "OBSERVE_LOG_BUNDLE_NAME": "observe-logs-bundle",
                "OBSERVE_LOG_UPLOAD_TARGET": "elabftw",
                "OBSERVE_LOG_CAPTURE_COMMAND_OUTPUT": "true",
                "OBSERVE_LOG_CAPTURE_WINDOW": "measure_window"
              },
              "expected_artifacts": [
                {
                  "name": "observe-logs-bundle",
                  "path_or_glob": "/tmp/observe-logs/observe-logs-bundle.tar.gz",
                  "mime": "application/gzip"
                },
                {
                  "name": "observe-logs-index",
                  "path_or_glob": "/tmp/observe-logs/*.jsonl",
                  "mime": "application/x-ndjson"
                }
              ]
            },
            {
              "id": "wifi-scan-baseline",
              "type": "WIFI_SCAN",
              "timeout_s": 70,
              "retries": 0,
              "env": {
                "RF_PROFILE": "rpi-rf-measurement",
                "RF_PHASE": "baseline",
                "RF_ACTIVITY_LABEL": "empty-room",
                "RF_ROOM_ID": "room-lab-01",
                "RF_SCENARIO_ID": "crowd-monitoring-v1",
                "RF_RUN_LABEL": "run-2026-04-22-a",
                "WIFI_SCAN_IFACE": "wlan0",
                "WIFI_SCAN_MODE": "passive_monitor",
                "WIFI_SCAN_CHANNELS": "1,6,11",
                "WIFI_SCAN_CHANNEL_DWELL_S": "1",
                "WIFI_SCAN_CHANNEL_WIDTH_MHZ": "20",
                "WIFI_SCAN_DURATION_S": "60",
                "WIFI_SCAN_HASH_MAC_ADDRESSES": "true",
                "WIFI_SCAN_HASH_ROTATION": "daily_salt",
                "WIFI_SCAN_CAPTURE_PAYLOADS": "false",
                "WIFI_SCAN_LOG_FIELDS": "timestamp_utc,hashed_mac,rssi_dbm,channel,frequency_mhz,frame_type,ssid,vendor_oui,retry_flag,phy_rate,antenna_id",
                "WIFI_SCAN_OUTPUT_FORMAT": "pcap"
              }
            },
            {
              "id": "ble-scan-baseline",
              "type": "BLE_SCAN",
              "timeout_s": 25,
              "retries": 0,
              "env": {
                "RF_PROFILE": "rpi-rf-measurement",
                "RF_PHASE": "baseline",
                "RF_ACTIVITY_LABEL": "empty-room",
                "RF_ROOM_ID": "room-lab-01",
                "RF_SCENARIO_ID": "crowd-monitoring-v1",
                "RF_RUN_LABEL": "run-2026-04-22-a",
                "BLE_SCAN_IFACE": "hci0",
                "BLE_SCAN_MODE": "continuous",
                "BLE_SCAN_DURATION_S": "60",
                "BLE_SCAN_HASH_MAC_ADDRESSES": "true",
                "BLE_SCAN_HASH_ROTATION": "daily_salt",
                "BLE_SCAN_LOG_FIELDS": "timestamp_utc,hashed_mac,rssi_dbm,adv_type,uuid,major_minor,tx_power,manufacturer_data",
                "BLE_SCAN_OUTPUT_FORMAT": "jsonl"
              }
            },
            {
              "id": "shell-device-capabilities",
              "type": "SHELL",
              "timeout_s": 20,
              "retries": 0,
              "cmdline": "sh -lc 'echo === hostname ===; hostname; echo === kernel ===; uname -a; echo === interfaces ===; ip -br link; echo === wifi ===; iw dev || true; echo === bluetooth ===; bluetoothctl list || true'",
              "env": {
                "SHELL_EXPECTED_INTERFACES": "wlan0,hci0",
                "SHELL_CAPTURE_HARDWARE_SERIAL": "true",
                "SHELL_CAPTURE_SOFTWARE_VERSIONS": "true"
              }
            }
          ]
        }
      ]
    }
  }
}
```

## How To Read This Example

- `fleet.policy`: the experiment policy object stored in eLabFTW
- `experiment_id`: stable identifier for this experiment definition
- `target_selector.device_ids`: which device or devices should receive the policy
- `range`: overall time range in which this policy is valid for selection
- `reporting.measure_window`: when the measurement part of the run is expected to happen
- `command_groups`: ordered execution steps in the design JSON
- `timeout_s`: hard per-command runtime limit
- `retries`: how many retries the command gets after the first attempt
- `env`: command-specific configuration values
- `expected_artifacts`: files the command should leave behind for later upload and verification

## Command-By-Command Explanation

- `sync-maintenance-window`: defines upload scheduling. `SYNC_CRON` is the maintenance schedule, `SYNC_WINDOW_DURATION_MINUTES` is the upload window length, and `SYNC_SLOT_COUNT` plus `SYNC_SLOT_JITTER_S` define device staggering.
- `observe-maintenance-sinks`: defines observability behavior. It chooses metric groups, remote-write control commands, and which logs should be bundled and uploaded as artifacts.
- `wifi-scan-baseline`: defines the single-radio Wi-Fi measurement in the checked-in example. This is where the user sets the interface, scan mode, channel list, dwell time, duration, hashing policy, and output format.
- `ble-scan-baseline`: defines the BLE measurement. This is where the user sets the BLE interface, scan mode, duration, hashing policy, and output format.
- `shell-device-capabilities`: utility command for collecting host and interface information around the measurement run.

## Fields The User Will Usually Edit First

- `experiment_id`: give the run family a stable name
- `target_selector.device_ids`: choose the real target device or device group
- `range.from` and `range.to`: choose when the experiment is valid
- `reporting.measure_window.from` and `reporting.measure_window.to`: choose when measurement should happen
- `SYNC_TIMEZONE` and `SYNC_CRON`: choose the maintenance schedule
- `SYNC_WINDOW_DURATION_MINUTES`: choose how long uploads are allowed after the cron tick
- `WIFI_SCAN_IFACE`, `WIFI_SCAN_CHANNELS`, `WIFI_SCAN_CHANNEL_DWELL_S`, `WIFI_SCAN_CHANNEL_WIDTH_MHZ`: choose how Wi-Fi measurement runs
- `BLE_SCAN_IFACE`, `BLE_SCAN_MODE`, `BLE_SCAN_DURATION_S`: choose how BLE measurement behaves
- `OBSERVE_METRIC_GROUPS` and `OBSERVE_METRICS_FLUSH_INTERVAL_S`: choose what is exported to Prometheus, Mimir, and Grafana
- `OBSERVE_LOG_JOURNAL_UNITS` and `OBSERVE_LOG_FILES`: choose which logs should be bundled for eLabFTW

## Metrics And Logs In This Example

- Metrics from `WIFI_SCAN` and `BLE_SCAN` are intended for Prometheus and then Mimir/Grafana.
- Logs are not sent to Mimir. In this design they are bundled by the observability configuration and uploaded as eLabFTW artifacts.
- `expected_artifacts` on `observe-maintenance-sinks` shows the intended log bundle outputs.

## Adding CSI Later

CSI is still part of the longer-term command set, but it is intentionally not in the current checked-in example.

- When CSI is re-enabled in the example, it should be added as the executable runtime command `CAPTURE_CSI`.
- `SYNC` and `OBSERVE` still remain design commands that get normalized before dispatch.
- That keeps the proto surface stable while allowing the example JSON to grow back to Wi-Fi + BLE + CSI when the Pi flow is ready.

## Schedule Replacements For `SYNC`

Replace only the `sync-maintenance-window.env` block values below.

### Daily at 03:30 UTC

```json
{
  "SYNC_TIMEZONE": "UTC",
  "SYNC_CRON": "30 3 * * *",
  "SYNC_CRON_DESCRIPTION": "Every day at 3:30 AM UTC",
  "SYNC_WINDOW_DURATION_MINUTES": "45",
  "SYNC_SLOT_COUNT": "2",
  "SYNC_SLOT_JITTER_S": "15",
  "SYNC_REQUIRE_UPLOAD_WINDOW": "true"
}
```

### Every 4 Hours

```json
{
  "SYNC_TIMEZONE": "UTC",
  "SYNC_CRON": "0 */4 * * *",
  "SYNC_CRON_DESCRIPTION": "At minute 0 of every 4th hour",
  "SYNC_WINDOW_DURATION_MINUTES": "30",
  "SYNC_SLOT_COUNT": "1",
  "SYNC_SLOT_JITTER_S": "0",
  "SYNC_REQUIRE_UPLOAD_WINDOW": "true"
}
```

### Weekdays at 22:00 Local Time

```json
{
  "SYNC_TIMEZONE": "Europe/Bratislava",
  "SYNC_CRON": "0 22 * * 1-5",
  "SYNC_CRON_DESCRIPTION": "Monday through Friday at 10:00 PM local time",
  "SYNC_WINDOW_DURATION_MINUTES": "60",
  "SYNC_SLOT_COUNT": "3",
  "SYNC_SLOT_JITTER_S": "20",
  "SYNC_REQUIRE_UPLOAD_WINDOW": "true"
}
```

### Weekly Sunday Maintenance

```json
{
  "SYNC_TIMEZONE": "UTC",
  "SYNC_CRON": "0 2 * * 0",
  "SYNC_CRON_DESCRIPTION": "Every Sunday at 2:00 AM UTC",
  "SYNC_WINDOW_DURATION_MINUTES": "120",
  "SYNC_SLOT_COUNT": "4",
  "SYNC_SLOT_JITTER_S": "30",
  "SYNC_REQUIRE_UPLOAD_WINDOW": "true"
}
```
