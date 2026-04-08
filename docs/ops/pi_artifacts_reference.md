# Raspberry Pi Artifact Reference (Fleet v2)

This document explains what each uploaded artifact means for Pi smoke runs, including CSI timeout semantics and duplicate-upload behavior.

## CSI Timeout Message

When you see:

`collector timeout tolerated because capture evidence was present (frames=<N>, files=<M>)`

it means:

- The CSI collector process hit the command timeout window.
- The run still produced valid evidence (`csi_frames_total > 0` and/or CSI output files).
- The command is accepted as successful (`csi_capture_ok=1`) for real-data validation.

The accompanying line:

`collector_output: command timed out`

is the raw collector execution outcome, not a data-loss signal by itself.

## Wi-Fi Success Semantics (Current)

`wifi_scan_ok=1` means Wi-Fi sensing command execution produced usable evidence.

For monitor-mode runs this can be true even when `wifi_connected=0`, because channel/frequency evidence is still valid sensing data.

If you need associated/station-only Wi-Fi for acceptance, enable:

- `REQUIRE_WIFI_CONNECTED=true` in smoke/verifier runs.

## Artifact Types

Common artifact name patterns and meaning:

- `wifi-*-scan-*.txt`
  - Raw `iw dev <iface> scan` output.
- `wifi-*-link-*.txt`
  - Raw `iw dev <iface> link` output fallback.
- `wifi-*-proc-wireless-*.txt`
  - `/proc/net/wireless` fallback snapshot.
- `wifi-*-debug-*.txt`
  - Debug bundle with both scan/link outputs when Wi-Fi evidence is insufficient.
- `wifi-*.log`
  - Per-command execution log (duration/exit/message).
- `ble-*-scan-*.txt`
  - Raw BLE scan output.
- `ble-*.log`
  - BLE command execution log.
- `csi-*-output-*.txt`
  - CSI collector stdout/stderr (plus timeout-evidence summary when applicable).
- `csi-*-<index>-*.dat` (or other collector output files)
  - Binary/raw CSI capture output imported from collector path/glob.
- `csi-*.log`
  - CSI command execution log.
- `rf-env-snapshot-*.log`
  - RF environment snapshot command output.
- `cmd-*-output-*.txt`
  - Generic shell command captured output.
- `run-summary.json`
  - Final run summary (status, totals, key metrics, latest observed values).

## Why Artifacts Can Appear Duplicated in eLabFTW

In Wi-Fi-disruptive CSI runs, artifact upload is effectively at-least-once:

1. Optional opportunistic uploads during measurement.
2. Final upload/replay when reporting pending run data.

If connectivity drops/restores mid-run, the same artifact can be submitted more than once.

## Current Deduplication Behavior

`/ingest/v1/artifacts` now deduplicates by:

- `experiment_id`
- `run_id`
- `agent_id`
- `artifact_name`
- `sha256` (or `size_bytes` when sha is absent)

If a matching artifact already exists in the ingest journal, the endpoint returns the existing upload reference instead of creating a new eLabFTW upload entry.

## Uploaded Filename Format in eLabFTW

When an artifact is uploaded, Fleet now stores it with a descriptive filename:

`run-<run8>__<phase>__ag-<agent6>__<original-stem>__<sha8>.<ext>`

Example:

`run-5eaeda62__measure-live__ag-80f5d8__wifi-wifi-sensing-series-s30-scan-1771705349840__4aa1c90b.txt`

Notes:

- `<phase>` is `measure-live` for opportunistic uploads during measurement and `report-replay` for final replay uploads.
- `<sha8>` makes same-name artifacts distinguishable by content.
- Dedupe still prevents repeated uploads of the exact same artifact content for the same run/agent/name key.

## Artifact Bundling (Reduced Count)

To reduce per-run artifact fan-out, the agent now merges text artifacts by default before final report upload:

- merge target: command logs and text outputs (`.log`, `.txt`, `.json` except `run-summary.json`)
- bundled artifact name prefix: `wifi-ble-csi-artifacts-bundle-*.tar.gz`
- archive contents:
  - `entries/<original-artifact-files>`
  - `manifest.json` (metadata: names/checksums/sizes)
- kept separately: binary capture outputs (for example `csi-csi-capture-1-csi.dat`) and `run-summary.json`
- source merged text files are removed locally by default after archive creation

This typically reduces one run to a small set (for example: bundle + CSI binary + run-summary).
In the current strict single-Pi default profile, this is typically exactly 3 uploaded artifacts.
Disable with `MERGE_TEXT_ARTIFACTS=false` if you need every text artifact uploaded separately.
Set `MERGE_TEXT_ARTIFACTS_DELETE_SOURCES=false` to keep original text files on the Pi in addition to the bundle.

## Inspecting the Bundle

The bundle is a regular gzip-compressed tar archive. Example:

```bash
tar -tzf wifi-ble-csi-artifacts-bundle-<timestamp>.tar.gz
tar -xOf wifi-ble-csi-artifacts-bundle-<timestamp>.tar.gz manifest.json
```

Expected layout:

- `entries/<original-text-artifacts>`
- `manifest.json`

## Quick Validation Checklist

For a strict real run, check `run-summary.json`:

- `wifi_scan_ok=1`
- `wifi_connected=1` only when `REQUIRE_WIFI_CONNECTED=true`
- `ble_scan_ok=1`
- `csi_capture_ok=1` (when CSI required)
- `csi_frames_total >= required minimum`
- `csi_output_files_count >= required minimum`
