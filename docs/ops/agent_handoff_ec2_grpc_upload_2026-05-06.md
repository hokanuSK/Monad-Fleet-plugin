# EC2 gRPC Upload Handoff - 2026-05-06

## Deployment

- EC2 host: `34.198.184.128`
- Deploy dir: `/home/ladamik/FleetManager_deploy`
- Docker context on EC2:
  - `export PATH="$HOME/bin:$PATH" DOCKER_HOST=unix:///run/user/1002/docker.sock`
- eLabFTW:
  - VPN: `https://10.200.0.1:9443`
  - Public: `https://elab-monad-fleet.34.198.184.128.sslip.io:9443`
- Grafana:
  - Public: `http://34.198.184.128:3000`
- Fleet:
  - gRPC: `10.200.0.1:50060`
  - Metrics: `http://10.200.0.1:9108/metrics`

Do not store eLabFTW admin passwords or API keys in docs/Notion/Git.

## Code State

- Commit: `7ea9e9a Add gRPC artifact upload path`
- Adds `UploadArtifact(stream UploadArtifactChunk)` to v2/v3 proto.
- Device artifact body upload path:
  - `ARTIFACT_UPLOAD_TARGET=fleet_grpc`
  - non-summary artifacts are streamed to Fleet and spooled server-side,
  - `run-summary.json` finalizes the server-side bundle upload into eLabFTW.
- Runtime status chatter can be disabled:
  - `ENABLE_COMMAND_STATUS_REPORTS=false`
  - `ENABLE_UPLOAD_STATUS_REPORTS=false`
- Pi deploy/smoke scripts now support:
  - `SSH_PROXY_JUMP`
  - `SSH_USER_KNOWN_HOSTS_FILE`

## VPN/Device State

- EC2 `wg0`: MTU `1280`
- Pi1 `10.200.0.10` / `2c:cf:67:81:00:45`: MTU `1280`
- Pi2 `10.200.0.11` / `24:eb:16:e3:6a:07`: MTU `1280`
- Pi1 had no `python3.12-venv` and apt could not reach `ports.ubuntu.com`; fixed by copying arm64 Python 3.12 wheels and unpacking them into `/home/monad/monad-fleet-agent/venv`.
- Both Pis have regenerated `fleet_gateway_v3_pb2*.py` with `UploadArtifact`.

## Experiment 8 Result

- eLabFTW experiment: `8`
- Title: `Fleet Dual Pi 1min maintenance upload 20260506T162844Z`
- Link: `https://elab-monad-fleet.34.198.184.128.sslip.io:9443/experiments.php?mode=view&id=8`
- Measurement window: `2026-05-06T16:29:29Z..2026-05-06T16:30:29Z`
- Upload window: `2026-05-06T16:30:29Z..2026-05-06T16:36:29Z`
- Context: `/home/ladamik/FleetManager_deploy/artifacts/output/ops/ec2_dual_pi_1min_maintenance_experiment_8_20260506T162844Z.json`

Run IDs:

- Pi1 measurement run: `c38fba42-f9f9-4fb6-a863-6ec0d2b61eac`
- Pi2 measurement run: `fccbac3d-755d-4966-9f79-156be091d10b`
- Pre-window no-command runs also uploaded summaries:
  - Pi1: `0f41b225-abe8-43b5-8738-26fdbb1d23a6`
  - Pi2: `8adb4e21-d8ad-42e7-85a8-2c323c0715cd`

eLabFTW uploads for experiment 8:

- `run-fccbac3d__report-replay__ag-e36a07__wireless-run-evidence-bundle__e5b08645.tar.gz`
- `run-c38fba42__report-replay__ag-810045__wireless-run-evidence-bundle__6d6143d8.tar.gz`
- `run-fccbac3d__report-replay__ag-e36a07__run-summary__8c872082.json`
- `run-c38fba42__report-replay__ag-810045__run-summary__564e3190.json`
- plus pre-window summary-only uploads for `8adb4e21...` and `0f41b225...`

Standard check:

- Pi2: `status=OK`, `commands_total=3`, `commands_failed=0`, `wifi_scan_ok=1`, `ble_scan_ok=1`, `wifi_ap_total=5`, `ble_adv_total=21`, `artifacts_total=8`, `artifacts_uploaded_elab=8`, `artifacts_upload_failed=0`.
- Pi1: `status=OK`, `commands_total=3`, `commands_failed=0`, `wifi_scan_ok=1`, `ble_scan_ok=0`, `wifi_ap_total=0`, `ble_adv_total=0`, `artifacts_total=8`, `artifacts_uploaded_elab=8`, `artifacts_upload_failed=0`.
- Mimir query confirmed `artifacts_uploaded_elab=8` for both device IDs.
- Grafana health endpoint returned `200`.

## Caveats / Follow-up

- Experiments 5/6/7 were disabled by clearing metadata because their policy selection ranges caused stale policy selection during retries.
- The helper used for experiment 8 should keep `policy.range` scoped to the measurement window only. Upload validity belongs in reporting/upload fields and command env, not the selector range.
- The agent does not wait for future measurement windows after fetching a policy. Start one-cycle runs inside the measurement window or update the agent to sleep until `measurement_from`.
- Pi1 produced a successful run and uploaded artifacts, but BLE/AP discovery returned zero observed devices. Treat Pi1 radio discovery as a follow-up if the next experiment needs both Pis to see nonzero WiFi AP/BLE counts.

## Pi1 Missing Data Diagnosis

The missing Pi1 data is not a gRPC or eLabFTW upload problem. Pi1 successfully uploaded the run summary and evidence bundle through Fleet gRPC. The missing part is RF sensor content.

Pi1 dependency check:

```text
missing:/usr/bin/bluetoothctl
missing:/usr/bin/btmgmt
missing:/usr/bin/hcitool
missing:/usr/bin/nmcli
missing:/usr/sbin/iw
missing:/sbin/iw
missing:/usr/sbin/rfkill
missing:/usr/bin/rfkill
```

Pi2 dependency check:

```text
present:/usr/bin/bluetoothctl
present:/usr/bin/btmgmt
present:/usr/bin/hcitool
present:/usr/bin/nmcli
present:/usr/sbin/iw
present:/sbin/iw
present:/usr/sbin/rfkill
missing:/usr/bin/rfkill
```

Pi1 run `c38fba42-f9f9-4fb6-a863-6ec0d2b61eac` shell artifact showed:

```text
sh: 1: iw: not found
sh: 1: bluetoothctl: not found
```

Interpretation:

- `status=OK` and `commands_failed=0` only means the command pipeline completed without fatal process failures.
- Pi1 `wifi_scan_ok=1` came from fallback link/RSSI evidence, not AP discovery.
- Pi1 `wifi_ap_total=0`, `ble_scan_ok=0`, and `ble_adv_total=0` are expected with the missing userspace tools.
- Pi2 has the required tools and produced nonzero WiFi/BLE observations.

Recommended Pi1 package fix:

```bash
sudo apt update
sudo apt install -y bluez iw rfkill network-manager wireless-regdb
sudo apt install -y pi-bluetooth
```

If `apt` still hangs on Pi1, first check route/DNS:

```bash
ip route
resolvectl status || cat /etc/resolv.conf
curl -4I http://ports.ubuntu.com/ubuntu-ports/
```

If online apt remains unavailable, use offline `.deb` provisioning from a working arm64 Ubuntu/Raspberry Pi host and install via:

```bash
sudo dpkg -i ./*.deb
sudo apt -f install
```

Recommended code/process follow-up:

- Add agent preflight metrics for required tool presence:
  - `tool_iw_present`
  - `tool_bluetoothctl_present`
  - `tool_rfkill_present`
  - `tool_nmcli_present`
- Mark run `PARTIAL` when required RF tools are missing.
- Split WiFi success semantics:
  - `wifi_link_ok`
  - `wifi_ap_scan_ok`
  - `wifi_scan_source=iw|proc_net_wireless|fallback`
- Add an agent option to wait until `measurement_from` before executing one-cycle policies, instead of producing pre-window no-command runs.
