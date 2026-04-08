# CSI Future Work Notes (2026-03-18)

## Snapshot
- Pi target: `192.168.0.211` (`monad-rpi5`)
- Confirmed: `measureinject` TX loop runs (historically `Injecting VHT` repeated, enable/disable markers present).
- Still failing: CSI evidence gate (`frames=0`, `files=0`) in real run.
- Latest compatibility check: `COMPAT_STATUS=WARN`, reason `kernel_module_symbol_mismatch`.
- No current OOM signal in latest snapshot (`OOM_KILL_COUNT=0`, `OOM_INVOKE_COUNT=0`).

## What Was Confirmed
1. TX/injection path works.
2. Problem is in RX/CSI capture path or output persistence path.
3. Manual low-level probe can disrupt connectivity hard enough that Pi does not auto-return over Wi-Fi.

## New Process Guardrails Added
1. `scripts/rpi/manual_csi_probe.sh`
- Added explicit post-probe Wi-Fi recovery options:
  - `PROBE_WIFI_IFACE` (default `wlan0`)
  - `PROBE_WIFI_PROFILE` (auto-detect if empty)
  - `PROBE_RECOVER_WIFI` (default `true`)
- Added `recover.log` capture to probe summary for debugging recovery behavior.

2. `scripts/smoke/pi_wifi_csi_stable.sh`
- Added mandatory pre-attempt manual log collection before each attempt (`PRE_ATTEMPT_MANUAL_LOGS=true` by default).
- Captures host/network snapshot, queue state, agent journal tail, and FeitCSI-related dmesg tail.

## Priority Next Steps
1. Bring Pi back online with RJ45 connected and keep Ethernet available during CSI testing.
2. Re-run manual probe first to validate the new recovery logic:
```bash
PI_HOST=<pi-ip> PROBE_SECONDS=45 PROBE_VERBOSE=false scripts/rpi/manual_csi_probe.sh
```
3. Immediately verify probe artifacts on Pi:
- `frames.txt`
- `size_bytes.txt`
- `rc.txt`
- `recover.log`
- `feitcsi.log`
4. If probe is stable, run full smoke using Ethernet control-plane route and keep CSI on Wi-Fi capture.
5. Compare run summary metrics:
- `csi_capture_ok`
- `csi_frames_total`
- `csi_output_files_count`
- `csi_collector_exit_code`

## Decision Tree
1. If `csi_probe.dat` is missing or zero bytes:
- Failure is before parser (collector/output path).
- Focus on collector command, permissions, target path, timeout window.

2. If file exists but `frames=0`:
- Data is not parseable as expected CSI records.
- Validate format assumptions and collector output mode consistency.

3. If file has frames in manual probe but smoke still shows zero:
- Problem is likely in agent integration path (artifact pickup, parsing regex, evidence gating).

4. If SSH drops and Pi stays offline after probe:
- Treat as recovery failure first.
- Resolve interface/profile recovery before continuing CSI evidence debugging.

## Minimal Success Criteria for Next Session
1. Manual probe completes and Pi remains reachable after capture.
2. At least one run with non-zero CSI evidence (`files>=1`, `frames>=1`).
3. No manual reboot required between two consecutive CSI attempts.

## Commands To Reuse
Compatibility snapshot:
```bash
PI_HOST=<pi-ip> scripts/rpi/verify_feitcsi_compat.sh
```

Manual probe:
```bash
PI_HOST=<pi-ip> PROBE_SECONDS=45 PROBE_VERBOSE=false scripts/rpi/manual_csi_probe.sh
```

Stable smoke wrapper:
```bash
PI_HOSTS_CSV=<pi-ip> scripts/smoke/pi_wifi_csi_stable.sh
```
