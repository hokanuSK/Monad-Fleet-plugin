# Monad Fleet v2 Phase-1 Operational Playbook

Date: February 22, 2026  
Status: active for current deployments

## 1. Purpose

This document is the practical operations guide for running Monad Fleet v2 with Raspberry Pi devices in the current Phase-1 scope.

It complements, but does not replace:

- `docs/monad_fleet_grpc_interface_v2.tex` (full protocol/profile spec)
- `docs/pi_artifacts_reference.md` (artifact naming and meaning)

## 2. Phase-1 Scope (What "ready" means now)

Phase-1 readiness is operational, not research-scale.

- Single Pi run is acceptable.
- Real-data gates are required.
- 5-minute runs are acceptable for sign-off.
- CSI can remain artifact-first.
- CSI time-series export to Prometheus/Mimir is not required.
- Multi-day and multi-participant coverage is deferred.

## 3. Current Reality Snapshot

From latest smoke validation:

- Local real measurement capture can succeed for Wi-Fi, BLE, and CSI.
- On Wi-Fi-only control-plane, strict CSI can disrupt connectivity.
- When disruption occurs, uploads may fail during measure/replay.
- Failed uploads are not data loss: runs remain in `data/pending/` for retry.

This means local evidence can be valid while end-to-end smoke fails due to transport instability.

## 4. Recommended Operating Modes

## Mode A (recommended): Ethernet control-plane + strict CSI

Use when you need strict end-to-end pass with CSI.

- `REQUIRE_REAL_CSI=true`
- `ALLOW_WIFI_DISRUPTIVE_CSI=false`
- Pi reaches Fleet over Ethernet path.

Expected outcome:

- Strongest chance of full smoke pass.
- Fewer SSH/report/upload interruptions.

## Mode B: Wi-Fi control-plane + strict Wi-Fi/BLE, deferred CSI strictness

Use when Ethernet is not available.

- Keep `REQUIRE_REAL_WIFI=true`, `REQUIRE_REAL_BLE=true`
- Set `REQUIRE_REAL_CSI=false` for acceptance runs
- Keep CSI capture optional as artifact-only evidence

Expected outcome:

- Stable operational pass for Phase-1.
- CSI reliability validation postponed.

## Mode C (diagnostic only): Wi-Fi control-plane + strict CSI + disruptive allowed

Use only for stress testing:

- `REQUIRE_REAL_CSI=true`
- `ALLOW_WIFI_DISRUPTIVE_CSI=true`

Expected outcome:

- May capture CSI successfully.
- Higher risk of upload/replay failures and pending runs.

## 5. Phase-1 Acceptance Command Set

## 5.1 Standard 5-minute run

```bash
PI_HOST=<pi-host-or-ip> \
DEVICE_PI=<agent_id_mac> \
TARGET_DEVICE_IDS_CSV=<agent_id_mac> \
SMOKE_PROFILE=wireless_spec \
WIRELESS_SPEC_DURATION_S=300 \
WIRELESS_SPEC_INTERVAL_S_PI=5 \
RUN_PI=true \
INJECT_HYPOTHETICAL_METRICS=false \
REAL_DATA_ENFORCE=true \
REQUIRE_REAL_WIFI=true \
REQUIRE_REAL_BLE=true \
REQUIRE_REAL_CSI=true \
DO_BUILD=false \
scripts/smoke/v3_end_to_end_smoke.sh
```

If Wi-Fi-only route is required and you accept disruption:

```bash
ALLOW_WIFI_DISRUPTIVE_CSI=true
```

## 5.2 Post-run verifier

```bash
PI_HOST=<pi-host-or-ip> \
SMOKE_EXPERIMENT_ID=<experiment_id> \
REAL_DATA_ENFORCE=true \
REQUIRE_REAL_WIFI=true \
REQUIRE_REAL_BLE=true \
REQUIRE_REAL_CSI=true \
scripts/rpi/verify_real_run.sh
```

## 5.3 Phase-1 pass criteria

A run is Phase-1 pass only if:

- Wi-Fi command evidence exists and `wifi_scan_ok=1`.
- BLE command evidence exists and `ble_scan_ok=1`.
- CSI command evidence exists and `csi_capture_ok=1` (when CSI required).
- CSI evidence exists (`csi_frames_total >= min` or output files present).
- `run-summary.json` exists.
- Report replay reaches Fleet (`ACCEPTED` or `DUPLICATE`) and run leaves `pending/`.

## 6. Pending Run Recovery (No Data Loss Path)

When upload/report fails mid-run, use replay-only sync.

## 6.1 Inspect pending queue on Pi

```bash
ssh admin@<pi-host-or-ip> \
  'bash -lc "ls -1 /home/admin/monad-fleet-agent/data/pending 2>/dev/null"'
```

## 6.2 Replay without new measurement

Important: set Fleet host reachable from Pi network (do not rely on Docker-internal DNS from the Pi host shell).

```bash
ssh admin@<pi-host-or-ip> 'bash -lc "
  cd /home/admin/monad-fleet-agent &&
  source venv/bin/activate &&
  FLEET_MANAGER_HOST=<reachable_fleet_host_ip_or_name> \
  FLEET_MANAGER_PORT=50060 \
  MAX_SYNC_CYCLES=1 \
  EXECUTE_POLICY=false \
  python -u agent_v2_client.py
"'
```

## 6.3 Confirm replay outcome

```bash
ssh admin@<pi-host-or-ip> 'bash -lc "
  echo PENDING:;
  ls -1 /home/admin/monad-fleet-agent/data/pending 2>/dev/null || true;
  echo SENT_LATEST:;
  ls -1t /home/admin/monad-fleet-agent/data/sent 2>/dev/null | head -n 5
"'
```

## 7. Artifact Expectations (Current Defaults)

Typical strict real run after bundling:

- `wifi-ble-csi-artifacts-bundle-*.tar.gz`
- `csi-csi-capture-*.dat` (or collector outputs)
- `run-summary.json`

If upload is interrupted, these remain locally under:

- `/home/admin/monad-fleet-agent/data/pending/<run_id>/artifacts/`

## 8. Troubleshooting Quick Table

## Symptom: `Could not resolve hostname monad-rpi5.local`

- Cause: mDNS name not resolvable from control host.
- Action: use direct IP in `PI_HOST` (for example link-local `169.254.x.x`).

## Symptom: strict CSI gate blocks run on Wi-Fi route

- Cause: safety gate with `REQUIRE_REAL_CSI=true` on Wi-Fi control-plane.
- Action:
  - Preferred: move control-plane to Ethernet.
  - Alternate: `ALLOW_WIFI_DISRUPTIVE_CSI=true`.

## Symptom: `Network is unreachable` during artifact upload

- Cause: CSI activity disrupted Wi-Fi control-plane.
- Action: keep run artifacts in pending, restore route, execute replay-only sync.

## Symptom: replay fails with `DNS resolution failed for monad-fleet-service`

- Cause: Pi shell cannot resolve Docker service name.
- Action: set `FLEET_MANAGER_HOST` to reachable host/IP from Pi.

## Symptom: smoke reports zero metrics and only run-summary uploaded

- Cause: policy assignment mismatch, no commands executed.
- Action: force target by setting:
  - `DEVICE_PI=<actual_agent_id>`
  - `TARGET_DEVICE_IDS_CSV=<same_agent_id>`

## 9. Next Steps (After Phase-1)

1. Standardize Ethernet control-plane for strict CSI certification.
2. Add automatic replay job on Pi startup/service recovery.
3. Add explicit Fleet endpoint override in smoke helper to avoid DNS ambiguity.
4. Move to Phase-2: labeling pipeline + multi-day/multi-participant protocol.

