# Agent Handoff: Pi Single-Radio CSI Status (2026-02-23)

This note is the operational handoff for next Codex windows/agents.

## Scope

- Device: Raspberry Pi (`monad-rpi5`)
- Control-plane: Wi-Fi on `wlan0` (single-radio constraint)
- Fleet host: `192.168.0.70:50060`
- Agent ID (pinned): `24:eb:16:e3:6a:07`

## Confirmed Progress

1. Agent/reporting flow is now robust enough to finish runs and replay reports from local spool.
2. Corrupt pending reports no longer block replay loops (quarantine path implemented in `device-sim/agent_v2_client.py`).
3. CSI collector process is wrapped with timeout/cleanup safeguards and single-radio recovery is active.
4. Evidence from run `8c0451ec-e893-4d27-b13d-3f9e789c135c` confirms `measureinject` was active:
   - `Injecting VHT` count: `200`
   - `Enabling CSI measurement` present
   - `Disabling CSI measurement` present

## Current Blocker

- CSI data evidence is still missing in real run:
  - `csi_frames_total=0`
  - `csi_output_files_count=0`
  - Run marked failed by evidence gate.

Interpretation:
- TX/injection path is working.
- RX/CSI capture pipeline did not produce parsable output for that run.

## Reproducible Evidence (latest deep check)

- Run directory:
  - `/home/admin/monad-fleet-agent/data/sent/8c0451ec-e893-4d27-b13d-3f9e789c135c`
- Bundle:
  - `artifacts/wifi-ble-csi-artifacts-bundle-1771816206821.tar.gz`
- CSI output entry in tar:
  - `entries/csi-csi-capture-output-1771816206791.txt`
- CSI command log entry in tar:
  - `entries/csi-capture-1771816206810.log`

## Next Actions (for next agent window)

1. Keep `AGENT_ID=24:eb:16:e3:6a:07` pinned in smoke runs (avoid MAC drift during CSI mode).
2. Run live check during capture to observe `/tmp/csi.dat` existence/size growth in real time.
3. Prefer external traffic source during capture (not only self-injection on same radio).
4. Keep recovery/offline-first mode enabled; do not disable spool replay logic.

## Quick Commands

Connectivity:

```bash
ssh -o ConnectTimeout=6 -o BatchMode=yes admin@192.168.0.211 'echo ONLINE && hostname && date -u +%FT%TZ'
```

Inspect current agent service:

```bash
ssh admin@192.168.0.211 'systemctl is-active monad-fleet-agent.service; journalctl -u monad-fleet-agent.service -n 80 --no-pager'
```

Inspect CSI evidence from the known run:

```bash
ssh admin@192.168.0.211 'tar -xOzf /home/admin/monad-fleet-agent/data/sent/8c0451ec-e893-4d27-b13d-3f9e789c135c/artifacts/wifi-ble-csi-artifacts-bundle-1771816206821.tar.gz entries/csi-csi-capture-output-1771816206791.txt | egrep -n "Injecting VHT|Enabling CSI measurement|Disabling CSI measurement" | tail -n 20'
```
