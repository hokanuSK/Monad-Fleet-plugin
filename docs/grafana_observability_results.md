# Grafana Observability Results

This guide explains what the Monad Fleet Grafana dashboard shows, how to read the wireless smoke-run panels, and what was visible from the AWS deployment during the 2026-04-30 check.

## Dashboard

- Local Grafana: `http://localhost:3000`
- AWS Grafana: `https://grafana-monad-fleet.16.171.70.171.sslip.io`
- Dashboard UID: `monad-fleet-wireless-v3`
- Dashboard path: `/d/monad-fleet-wireless-v3/monad-fleet-wireless-sensing-pi-smoke-v3`
- Provisioned file: `infrastructure/observability/grafana/provisioning/dashboards/monad-fleet-wireless-sensing-v3.json`

The dashboard reads from the `Mimir` datasource. Fleet metrics flow as:

```text
agent report or HTTP ingest -> monad-fleet-service /metrics -> Prometheus scrape -> Mimir remote_write -> Grafana
```

Both local and AWS compose stacks include Prometheus so Fleet `/metrics` samples are retained in Mimir for Grafana. Existing eLabFTW uploads and Fleet in-memory gauges do not automatically backfill Mimir history after a service restart. Metric ingest journaling is disabled by default; enable `ENABLE_METRICS_INGEST_JOURNAL=true` only for short debugging sessions.

## Panel Guide

- `Wi-Fi RSSI (dBm)`: signal strength from the latest Wi-Fi scan/report. Values closer to zero are stronger.
- `BLE Advertisements`: BLE advertisement count from either `ble_adv_count` or `ble_adv_total`.
- `Wi-Fi AP Count`: access points observed by the Wi-Fi scan, accepting both `wifi_ap_count` and `wifi_ap_total`.
- `Wi-Fi Connected`: `1` means the Pi was associated to Wi-Fi when the metric was reported.
- `BLE Scan OK`: `1` means the BLE command completed successfully.
- `CSI Frames Total`: CSI frame count when CSI metrics are available. Raw CSI captures remain artifacts; numeric CSI counters are eligible for Prometheus/Mimir export.
- `Run Command Health`: compares `commands_total` and `commands_failed` for the selected device.
- `All Device Metrics Snapshot`: instant table of every current numeric metric for the selected device.
- `Device Health`: CPU temperature, load, memory, and uptime when agents provide `device_*` metrics.
- `Device Last Seen`: most recent Fleet observation time for the selected device.
- `Service Counters`: service-level report, event, metric-ingest, and artifact counters.

## Localhost Pi Run On 2026-04-30

Fresh localhost run after enabling compose-managed Prometheus:

- Experiment: `elabftw:4`
- Run id: `7b282376-afd6-4de5-ad3f-a7ad33befdaf`
- Device: `24:eb:16:e3:6a:07`
- Profile: `wireless_spec`
- Status: `OK`, `REAL_DATA_CHECK=PASS`
- Prometheus scrape target: `monad-fleet-service:9108`, health `up`
- Mimir verification: `monad_fleet_metric_value` queries returned the run metrics
- eLabFTW uploads: `10`

Key values visible in Fleet, Prometheus, Mimir, and Grafana:

| Metric | Value | What It Means |
| --- | ---: | --- |
| `wifi_avg_rssi_dbm` | `-76.1` | Weak but valid 5 GHz Wi-Fi signal during the sample window. |
| `wifi_ap_total` / `wifi_ap_count` | `2` | Two APs observed in the Wi-Fi evidence path. |
| `wifi_sampling_points_applied` | `10` | Ten Wi-Fi samples were collected over the 30 second window. |
| `wifi_connected` | `1` | Pi was associated to Wi-Fi while reporting. |
| `ble_adv_total` / `ble_adv_count` | `30` | BLE advertisements observed during the scan. |
| `ble_scan_ok` | `1` | BLE command completed successfully. |
| `commands_total` | `4` | Wi-Fi, BLE, CSI-disabled marker, and environment snapshot commands reported. |
| `commands_failed` | `0` | No command failures reported. |
| `device_cpu_temp_c` | `52.35` | Pi CPU temperature at report time. |
| `device_load1` | `0.12` | Low system load during report. |

CSI was intentionally disabled for this graphing run (`REQUIRE_REAL_CSI=false`, `DISABLE_CSI_CAPTURE=true`). The run uploaded CSI-disabled marker artifacts, but Grafana CSI panels should not be used as evidence of real CSI frame capture for this run.

## Useful PromQL

Use these in Grafana Explore:

```promql
monad_fleet_metric_value{device_id=~"$device_id",metric="wifi_avg_rssi_dbm"}
monad_fleet_metric_value{device_id=~"$device_id",metric=~"wifi_ap_count|wifi_ap_total|ble_adv_count|ble_adv_total"}
monad_fleet_metric_value{device_id=~"$device_id",metric=~"commands_total|commands_failed"}
monad_fleet_metric_value{device_id=~"$device_id",metric=~"device_cpu_temp_c|device_load1|device_mem_available_bytes|device_uptime_s"}
monad_fleet_device_last_seen_unix{device_id=~"$device_id"}
sum by (source) (rate(monad_fleet_metric_updates_total[5m]))
```

For the 2026-04-30 localhost run, set the dashboard time range to include `2026-04-30 14:03-14:06 UTC` and select device `24:eb:16:e3:6a:07`.

## AWS Check On 2026-04-30

Reachable public endpoints:

- Grafana health: `ok`, version `11.1.4`
- Mimir readiness: `ready`
- Fleet metrics endpoint: reachable at `https://metrics-monad-fleet.16.171.70.171.sslip.io/metrics`

Observed limitation during this check:

- Mimir returned no metric names from `/prometheus/api/v1/label/__name__/values`.
- Mimir returned no `monad_fleet_metric_value` series.
- Fleet `/metrics` only exposed `monad_fleet_device_last_seen_unix{device_id="02:42:ac:14:00:04"}` at check time.
- SSH to `ubuntu@16.171.70.171` could not be used because the expected local key was not present.

This means AWS Grafana was up, but there was no retained AWS Mimir history to graph for the latest real Pi run from the live endpoint.

## Last Recorded AWS Pi Evidence

The latest local ops artifact for AWS Pi work is `artifacts/output/ops/aws_pi_experiment_23_20260427T2350Z.json`.

Related successful AWS Pi run recorded there:

- Experiment: `elabftw:22`
- Run id: `e4265508-b7ea-4f64-ab89-0a5c38fd8dbb`
- Device: `24:eb:16:e3:6a:07`
- `commands_total`: `3`
- `commands_failed`: `0`
- `wifi_ap_total`: `6`
- `wifi_avg_rssi_dbm`: `-74.0`
- `wifi_channel`: `48`
- `wifi_freq_mhz`: `5240`
- `ble_adv_total`: `42`

Interpretation: experiment `22` is the best available successful AWS Pi metrics reference in local artifacts. It shows a clean Wi-Fi/BLE run with no command failures, weak-but-valid 5 GHz Wi-Fi signal, six observed APs, and BLE activity.
