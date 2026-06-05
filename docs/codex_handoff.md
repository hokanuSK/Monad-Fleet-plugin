# Codex Handoff — 5 GHz + BLE Fleet Measurement System

**Date:** 2026-05-21 (updated)  
**Branch:** `codex/agent-bootstrap-runtime`  
**Last experiment submitted:** exp131 (eLabFTW experiment id=131, window 23:48Z–02:48Z May 20–21)

---

## System Overview

5-Pi fleet running passive 5 GHz WiFi monitor + BLE scan experiments.

| Pi | VPN IP | OS | Management iface | Measurement iface | BLE role |
|---|---|---|---|---|---|
| monad-02 | 10.200.0.11 | Raspberry Pi OS | wlan0 (brcmfmac) | wlan0 — **same radio, problematic** | advertiser |
| monad-03 | 10.200.0.12 | Ubuntu 24.04 | wlan0 (brcmfmac) | wlp1s0 (Intel AX210/iwlwifi) | listener |
| monad-04 | 10.200.0.13 | Ubuntu 24.04 | wlan0 (brcmfmac) | wlp1s0 (Intel AX210/iwlwifi) | listener |
| monad-05 | 10.200.0.14 | Ubuntu 24.04 | wlan0 (brcmfmac) | wlp1s0 (Intel AX210/iwlwifi) | listener |
| monad-06 | 10.200.0.15 | Ubuntu 24.04 | wlan0 (brcmfmac) | wlp1s0 (Intel AX210/iwlwifi) | listener |

All 5 Pis are **physically in the same room**. As of 2026-05-21:
- monad-03/05/06: stable, actively scanning
- monad-04: **hardware issue** — eth0 has no cable (NO-CARRIER), WireGuard runs over wlan0 (2.4 GHz WiFi) making it flaky; wlp1s0 (PCIe scan card) also shows NO-CARRIER intermittently — card may be loose. **Fix: plug ethernet cable into monad-04 and reseat PCIe card.**
- monad-02: wlan0 creates wlan0mon VIF for 5 GHz scanning (brcmfmac path); channel changes succeed in practice on newer firmware but theoretically limited per the hardware note below.

Fleet service and eLabFTW run on EC2 at `34.198.184.128`. WireGuard VPN: EC2 = 10.200.0.1 gateway, gRPC port 50060.

---

## How to Deploy Code to Pis

Dev Mac has no VPN. All deploys go Mac → EC2 → Pis.

```bash
# 1. Stage files on EC2
scp src/device-sim/agent_v2/wifi5g_capture.py \
    src/device-sim/agent_v2/main.py \
    src/device-sim/agent_v2/collectors.py \
    src/device-sim/agent_v2/core.py \
    ladamik@34.198.184.128:/tmp/

# 2. Push to all Pis from EC2 (EC2 has SSH key auth to Pis as monad@<ip>, no password)
ssh ladamik@34.198.184.128 bash << 'RELAY'
AGENT_DIR="/home/monad/monad-fleet-agent/agent_v2"
FILES="wifi5g_capture.py main.py collectors.py core.py"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=8"

for IP in 10.200.0.11 10.200.0.13 10.200.0.14 10.200.0.15; do
  echo "=== $IP ==="
  for F in $FILES; do
    scp $SSH_OPTS /tmp/$F monad@$IP:/tmp/$F
  done
  ssh $SSH_OPTS monad@$IP "
    for F in $FILES; do sudo -n cp /tmp/\$F $AGENT_DIR/\$F 2>/dev/null; done
    sudo -n systemctl restart monad-fleet-agent 2>/dev/null
    sleep 1; systemctl is-active monad-fleet-agent
  "
done
RELAY
```

Note: `sshpass` is NOT available on EC2. For password-auth deploys the **Mac must be the origin** using `sshpass -p "$PI_PASSWORD" ssh -J ladamik@34.198.184.128 monad@<ip>`. From EC2 directly, use key auth (EC2 key already in monad-03/05/06 authorized_keys; monad-04 needs it added).

SSH Pi login: user=`monad`, password from local ops credential store, proxy=`ladamik@34.198.184.128`.

Sudo on all Pis: use `printf "$PI_PASSWORD\n" | sudo -S` — do NOT use `echo "$PI_PASSWORD" | sudo -S` inside a heredoc (heredoc consumes stdin before sudo can read it). Grant passwordless sudo first on fresh installs: `printf "$PI_PASSWORD\n" | sudo -S bash -c 'echo "monad ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/monad-nopasswd && chmod 440 /etc/sudoers.d/monad-nopasswd'`

---

## EC2 Service Management (CRITICAL — read before touching Docker)

**Always use `.env.aws` and `docker-compose.aws.yml`.** The `.env` file in `/home/ladamik/FleetManager_deploy/` is a trap — it only has `GRAFANA_EXPERIMENT_INSTANCE_MAP` and missing MySQL credentials. Using it for compose will silently wipe MySQL passwords from container env and cause cascading failures.

```bash
# Correct way to manage services
cd /home/ladamik/FleetManager_deploy
docker compose -f docker-compose.aws.yml --env-file .env.aws up -d
docker compose -f docker-compose.aws.yml --env-file .env.aws up -d <service>  # single service
```

**MySQL ibdata1 lock (recurring crash pattern):** If you see `Unable to lock ./ibdata1 error: 11` in MySQL logs, a previous MySQL container was not fully stopped before the new one started. Fix:
```bash
docker ps -a --filter name=mysql --format '{{.Names}}' | xargs -r docker rm -f
# Then restart cleanly — MySQL data volume is safe, only the lock is the problem
docker compose -f docker-compose.aws.yml --env-file .env.aws up -d
```

**MySQL health check races on startup:** The compose health check has no `start_period`, so it can exhaust retries while MySQL is still initializing. This has been fixed by adding `start_period: 60s` to the healthcheck in `docker-compose.aws.yml`. If MySQL is stuck in `health: starting` for >90s, the ibdata1 lock issue is more likely the cause.

**`GRAFANA_EXPERIMENT_INSTANCE_MAP`** maps Pi MAC addresses to hostnames for fleet service metric labeling. It lives in `.env.aws` and must include all active Pis:
```
GRAFANA_EXPERIMENT_INSTANCE_MAP=2c:cf:67:81:00:45=monad-01,24:eb:16:e3:6a:07=monad-02,2c:cf:67:80:f5:86=monad-03,2c:cf:67:80:f4:bf=monad-04,2c:cf:67:81:00:6b=monad-05,2c:cf:67:81:00:d9=monad-06
```
Any Pi missing from this map will have metrics labeled with its MAC address rather than hostname and won't appear correctly in Grafana.

**Fleet service parse errors from old experiments:** The fleet service iterates ALL fleet-tagged experiments on every GetPolicy call. Old experiments with 2.4 GHz channels (1,6,11) in passive_monitor mode throw `ValueError` each cycle, adding ~100ms overhead per bad experiment. These must have the `fleet` tag removed:
```bash
# Remove fleet tag from a bad experiment (tag_id=1 is always the 'fleet' tag)
curl -sk -X DELETE -H "Authorization: $ELAB_API_KEY" \
  "https://elab-monad-fleet.34.198.184.128.sslip.io:9443/api/v2/experiments/<id>/tags/1"
```

---

## How to Schedule a New Experiment

The experiment policy JSON lives at `artifacts/tmp/exp_5ghz_ble_02_03_04_05_06_20m.json`. The submit script auto-retimes all windows to start 10 seconds before now.

```bash
# 1. Create a new policy JSON with a unique experiment_id (timestamp-stamped)
python3 -c "
import json, datetime, pathlib
src = pathlib.Path('artifacts/tmp/exp_5ghz_ble_02_03_04_05_06_20m.json')
doc = json.loads(src.read_text())
now = datetime.datetime.now(datetime.timezone.utc)
stamp = now.strftime('%Y%m%dT%H%M%SZ')
doc['fleet']['policy']['experiment_id'] = f'exp-monad-5ghz-ble-20m-{stamp}'
out = pathlib.Path(f'artifacts/tmp/exp_5ghz_ble_{stamp}.json')
out.write_text(json.dumps(doc, indent=2))
print(out)
"

# 2. Submit to eLabFTW (creates experiment, attaches policy, adds tags)
ELAB_BASE_URL="https://elab-monad-fleet.34.198.184.128.sslip.io:9443/api/v2" \
ELAB_API_KEY="<key-from-env>" \
python3 artifacts/tmp/submit_design_policy.py \
  artifacts/tmp/exp_5ghz_ble_<stamp>.json \
  "5GHz+BLE 5-Pi 20m"
```

The script prints `{"experiment_id": N, "sharelink": "...", "policy_id": "..."}`.

**Important:** `mode=once` means each agent executes a policy exactly once per unique `experiment_id`. Always bump `experiment_id` (step 1) before resubmitting — otherwise agents skip it as "already executed."

The policy window is 20 minutes. Agents poll every ~30 seconds and will pick up the new policy within one poll cycle.

### Direct API Experiment Creation (correct format)

The policy **must** go in the `metadata` field (not `body`). Use per-Pi `command_groups` to specify per-device `WIFI_SCAN_IFACE`. The working reference format (exact structure from exp127/131):

```python
import json, os, subprocess
from datetime import datetime, timezone, timedelta

now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
end = now + timedelta(hours=2)
fr = now.strftime('%Y-%m-%dT%H:%M:%SZ')
to = end.strftime('%Y-%m-%dT%H:%M:%SZ')

CHANNELS = "36,40,44,48,52,56,60,64,100,104,108,112,116,120,124,128,132,136,140,149,153,157,161,165"
ALL_IDS = ["24:eb:16:e3:6a:07","2c:cf:67:80:f5:86","2c:cf:67:80:f4:bf","2c:cf:67:81:00:6b","2c:cf:67:81:00:d9"]

def wifi_group(name, mac, iface):
    return {
        "id": f"wifi-5g-{name}",
        "commands": [{"id": f"wifi-5g-{name}", "env": {
            "WIFI_SCAN_MODE": "passive_monitor", "WIFI_SCAN_IFACE": iface,
            "UPLOAD_SLOT_COUNT": "1", "WIFI_SCAN_CHANNELS": CHANNELS,
            "WIFI_SCAN_RUN_ASYNC": "true", "UPLOAD_SLOT_JITTER_S": "0",
            "ARTIFACT_UPLOAD_TARGET": "fleet_http", "UPLOAD_WINDOW_REQUIRED": "false",
            "WIFI_SCAN_HASH_ROTATION": "daily_salt", "WIFI_SCAN_OUTPUT_FORMAT": "pcap",
            "WIFI_SCAN_CHANNEL_DWELL_S": "0.2", "WIFI_SCAN_CAPTURE_PAYLOADS": "false",
            "ARTIFACT_EVICT_AFTER_UPLOAD": "true", "WIFI_SCAN_CHANNEL_WIDTH_MHZ": "20",
            "WIFI_SCAN_HASH_MAC_ADDRESSES": "true", "WIFI_SCAN_USE_MEASURE_WINDOW": "true",
            "ARTIFACT_UPLOAD_DURING_MEASURE": "true", "ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S": "3"
        }, "type": "WIFI_SCAN", "retries": 0, "timeout_s": 7200}],
        "failure_mode": "CONTINUE_ON_ERROR",
        "target_selector": {"device_ids": [mac]}
    }

payload = {
    "title": f"5GHz WiFi Scan – All 5 Pis – {fr} (2h)",
    "tags": ["fleet", "5ghz", "wifi-only", "monad-all"],
    "metadata": {   # <-- MUST be dict here, NOT json.dumps(). json.dumps(payload) encodes it once.
        "fleet": {
            "policy": {
                "range": {"from": fr, "to": to},
                "execution": {"mode": "once"},
                "experiment_id": "exp-all-5pi-5ghz-night",  # bump this to re-run
                "command_groups": [
                    {"id": "shared-sync", "commands": [{"id": "sync-all",
                        "env": {"SYNC_TIMEZONE": "UTC", "SYNC_SLOT_COUNT": "1",
                                "SYNC_SLOT_JITTER_S": "0", "SYNC_REQUIRE_UPLOAD_WINDOW": "false"},
                        "type": "SYNC", "retries": 0, "timeout_s": 5,
                        "measure_window": {"from": fr, "to": to},
                        "target_selector": {"device_ids": ALL_IDS},
                        "validity_policy_window": {"from": fr, "to": to}
                    }], "failure_mode": "CONTINUE_ON_ERROR"},
                    wifi_group("monad-02", "24:eb:16:e3:6a:07", "wlan0"),   # brcmfmac VIF path
                    wifi_group("monad-03", "2c:cf:67:80:f5:86", "wlp1s0"),  # iwlwifi type-change
                    wifi_group("monad-04", "2c:cf:67:80:f4:bf", "wlp1s0"),
                    wifi_group("monad-05", "2c:cf:67:81:00:6b", "wlp1s0"),
                    wifi_group("monad-06", "2c:cf:67:81:00:d9", "wlp1s0"),
                ],
                "target_selector": {"device_ids": ALL_IDS}
            }
        }
    },
    "linked_items": [{"itemid": 3}, {"itemid": 6}, {"itemid": 4}, {"itemid": 7}, {"itemid": 8}]
}
# eLabFTW item IDs: monad-02=3, monad-03=6, monad-04=4, monad-05=7, monad-06=8
# API key: read from ELAB_API_KEY in the local shell or remote .env.aws; do not write it into docs.

result = subprocess.run([
    "curl", "-sk", "-X", "POST",
    "-H", f"Authorization: {os.environ['ELAB_API_KEY']}",
    "-H", "Content-Type: application/json",
    "https://elab-monad-fleet.34.198.184.128.sslip.io:9443/api/v2/experiments",
    "-d", json.dumps(payload), "-w", "\nHTTP:%{http_code}"
], capture_output=True, text=True)
```

The fleet service **auto-provisions a Grafana dashboard** when it successfully parses a new fleet-tagged experiment — watch for `Grafana dashboard auto-provisioned experiment=NNN` in fleet service logs.

To end an experiment early, remove the `fleet` tag (tag_id=1):
```bash
curl -sk -X DELETE -H "Authorization: $KEY" \
  "https://.../api/v2/experiments/<id>/tags/1"
```

---

---

## Agent Operational Runbook

### Execution Index Stale State

The agent records each policy execution in `/home/monad/monad-fleet-agent/data/execution-index.json`. Two failure modes:

**Stuck `in_progress`** — agent restarted mid-scan; subprocess is dead but index shows `in_progress`. On next cycle, agent checks if `now > measure_to`; if window is still open, it skips forever.

**Stuck `completed`** — agent crashed before it could report; state is `completed` but it was never actually reported.

Fix for both: delete the index and restart.
```bash
sshpass -p "$PI_PASSWORD" ssh -J ladamik@34.198.184.128 monad@<ip> \
  'rm -f /home/monad/monad-fleet-agent/data/execution-index.json && \
   sudo -n systemctl restart monad-fleet-agent'
```

### parse_iso Bug (FIXED 2026-05-21)

`parse_iso()` was called in `main.py:75,93` inside `_execution_skip_reason()` but was never defined anywhere — caused `NameError` crash every time an agent with an existing execution-index entry received a new policy. Fixed by adding to `src/device-sim/agent_v2/core.py`:

```python
def parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(normalize(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
```

This fix has been pushed to all Pis but **not yet committed to git**. Commit it.

### GetPolicy DEADLINE_EXCEEDED Pattern

If an agent logs `GetPolicy RPC failed: StatusCode.DEADLINE_EXCEEDED` repeatedly:
1. Check fleet service container is running: `docker ps | grep fleet`
2. Check MySQL is healthy: `docker ps | grep mysql` — if `(unhealthy)`, fix MySQL first (see EC2 section above)
3. Check eLabFTW web is up: `docker ps | grep web`
4. Check for old experiments causing parse errors: `docker logs elabftw-aws-monad-fleet-service-1 2>&1 | grep "Failed to parse" | grep -oP "experiment \d+" | sort -u` — remove fleet tags from those experiments

If fleet service is healthy but agent still DEADLINE_EXCEEDED, check the Pi's WireGuard link quality: `ping -c 10 10.200.0.1` from the Pi.

### Pending Upload Stuck

If agent logs `Deferring PublishReport until artifact finalization succeeds` in a loop:
```bash
# Clear pending uploads and execution index
sshpass -p "$PI_PASSWORD" ssh -J ladamik@34.198.184.128 monad@<ip> \
  'rm -rf /home/monad/monad-fleet-agent/data/pending/ \
         /home/monad/monad-fleet-agent/data/execution-index.json && \
   sudo -n systemctl restart monad-fleet-agent'
```

---

## Known Issues

### 1. BLE Dashboard Broken for Ubuntu Listener Devices (monad-03..06)

**Root cause:** The Grafana BLE dashboard (`monad-pi-ble-telemetry.json`) only shows TX/advertiser metrics:
- `monad_pi_ble_tx_total` — cumulative advertisement updates
- `monad_pi_ble_advertise_active` — 1 while advertising

These metrics are set exclusively in `src/device-sim/agent_v2/ble_advertise.py` which only runs for `BLE_SCAN_MODE=advertise` (monad-02 only). Ubuntu listener devices run `BLE_SCAN_MODE=continuous` — **no real-time Prometheus metrics are emitted during their scan**. The dashboard shows nothing for monad-03..06.

**Fix needed (3 files):**

**`prom_exposition.py`** — add two new metrics alongside `ble_tx_total`:
```python
ble_rx_total = Counter(
    "monad_pi_ble_rx_total",
    "Cumulative BLE [CHG] Name: events detected by a listener device.",
    registry=registry,
)
ble_scan_active = Gauge(
    "monad_pi_ble_scan_active",
    "1 while BLE continuous scan is running, else 0.",
    registry=registry,
)
```

**`collectors.py`** — in `collect_ble_scan` continuous path, set `ble_scan_active=1` before bluetoothctl starts, then after `_btproc.communicate()` call increments `ble_rx_total` by the detected [CHG] Name count, then set `ble_scan_active=0`. The detection count comes from `core._parse_bluetoothctl_scan(out)` which already counts `[CHG] Name:` events.

**`monad-pi-ble-telemetry.json`** — duplicate the TX panels, substituting `monad_pi_ble_tx_total` → `monad_pi_ble_rx_total` and `monad_pi_ble_advertise_active` → `monad_pi_ble_scan_active`. Add an `instance` variable that can filter to listener vs advertiser devices.

After editing the dashboard JSON, deploy it:
```bash
scp infrastructure/observability/grafana/provisioning/dashboards/static/monad-pi-ble-telemetry.json \
    ladamik@34.198.184.128:/home/ladamik/FleetManager_deploy/infrastructure/observability/grafana/provisioning/dashboards/static/
# Grafana hot-reloads provisioned dashboards — no container restart needed.
```

---

### 2. WiFi Dashboard Looks Different for monad-02 vs Ubuntu Pis

**Root cause:** monad-02 has a single Broadcom radio (brcmfmac/wlan0). That radio is connected to the 2.4 GHz management AP for WireGuard. There is no dedicated measurement radio.

The architecture requires a dedicated radio for 5 GHz measurement. Ubuntu Pis (monad-03..06) have two radios: wlan0 (brcmfmac, 2.4 GHz, connected to AP) and wlp1s0 (Intel AX210, PCIe, dedicated 5 GHz monitor). monad-02 only has wlan0.

When `wifi5g_capture.py` runs on monad-02 it creates a `wlan0mon` VIF via the brcmfmac VIF path (not iwlwifi type-change). The VIF creation succeeds and tcpdump starts, **but the physical radio is locked to the AP's 2.4 GHz channel**. All `set_channel()` calls to hop 5 GHz channels fail because brcmfmac won't change frequency while the STA (wlan0) holds an association. Result:
- `monad_pi_wifi5g_dwell_changes_total` stays flat — no successful hops
- `monad_pi_wifi5g_current_channel` never advances beyond channel 0 or the initial attempt
- tcpdump captures only 2.4 GHz management frames (or nothing) — not 5 GHz data

Ubuntu Pis hop freely because wlp1s0 has no STA association and the AX210 supports all 5 GHz channels 36–165.

**Options to fix:**

**Option A (recommended hardware):** Add an Intel AX210 M.2 adapter to monad-02 via the RPi M.2 HAT+, same as monad-03..06. Then update the fleet config for monad-02 to use `wlp1s0` as measurement iface.

**Option B (skip WiFi on monad-02):** Set `WIFI_SCAN_MODE=disabled` (or omit the WIFI_SCAN command group) for monad-02 in the experiment JSON. monad-02 is the BLE advertiser — its role doesn't require WiFi sensing.

**Option C (move management to Ethernet):** Connect monad-02 to Ethernet and run WireGuard over eth0 instead of wlan0. This frees wlan0 for 5 GHz measurement. Not practical if monad-02 is wireless-only in deployment.

Until fixed, expect monad-02's WiFi dashboard to show zero hops and a flat dwell-changes curve.

---

## File Map

| File | Purpose |
|---|---|
| `src/device-sim/agent_v2/wifi5g_capture.py` | Monitor VIF creation, tcpdump, channel hopping. Detects iwlwifi via ethtool and uses type-change instead of VIF. |
| `src/device-sim/agent_v2/ble_advertise.py` | BLE advertising (monad-02). Sets `ble_tx_total` and `ble_advertise_active` Prometheus metrics. |
| `src/device-sim/agent_v2/collectors.py` | BLE scan (listener path), WiFi scan dispatch. |
| `src/device-sim/agent_v2/core.py` | `_parse_bluetoothctl_scan` — counts `[CHG] Name:` events (not unique MACs). |
| `src/device-sim/agent_v2/prom_exposition.py` | Defines all Prometheus metrics, `record_ble_run_metrics`, `record_wifi_run_metrics`. |
| `src/device-sim/agent_v2/main.py` | Main agent loop. Hello() retries with 20-attempt/30 s backoff. GetPolicy() catches RpcError. |
| `artifacts/tmp/exp_5ghz_ble_02_03_04_05_06_20m.json` | Template policy for 5-Pi 20 min experiment. |
| `artifacts/tmp/submit_design_policy.py` | eLabFTW submit script — retimes windows, POSTs experiment, PATCHes metadata. |
| `infrastructure/observability/grafana/provisioning/dashboards/static/monad-pi-ble-telemetry.json` | BLE Grafana dashboard (TX-only, needs RX panels). |
| `infrastructure/observability/grafana/provisioning/dashboards/static/monad-pi-wifi5g-telemetry.json` | WiFi 5 GHz Grafana dashboard. |

---

## eLabFTW / API

- Base URL: `https://elab-monad-fleet.34.198.184.128.sslip.io:9443/api/v2`
- API key in env: `ELAB_API_KEY`
- Experiment browser: `https://elab-monad-fleet.34.198.184.128.sslip.io:9443/experiments.php`

---

## File Map

| File | Purpose |
|---|---|
| `docs/pi-fresh-deploy.md` | Full runbook for fresh Pi Ubuntu 24.04 deploy (created 2026-05-21). |

(other entries already above)

---

## Metrics Flow

```
Pi agent → fleet_http sink → fleet service :9108/ingest → :9108/metrics (relabeled instance=hostname)
→ Prometheus scrapes :9108 → remote_write → Mimir :9009 → Grafana
```

Pi metrics port `:9110` is NOT scraped by Prometheus. The fleet service re-labels using `GRAFANA_EXPERIMENT_INSTANCE_MAP` (MAC → hostname). Grafana dashboards use `label_values(metric, instance)` for auto-discovery. Experiment-specific dashboards are **auto-provisioned** by the fleet service on first valid parse.

---

## Pending (as of 2026-05-21)

1. **Commit `parse_iso` fix** — `src/device-sim/agent_v2/core.py` deployed to all Pis but not committed. Without it, agents crash with `NameError` on any policy with an existing execution-index entry.
2. **Add `protobuf` + `grpcio` to `src/device-sim/requirements.txt`** — missing; fresh Pi installs need manual pip install.
3. **Add pb2 files to deploy tarball** — `fleet_gateway_*_pb2.py` must be copied from a working Pi on fresh deploys. Add to tarball or generate at deploy time.
4. **monad-04 hardware** — plug ethernet into eth0 (stabilizes WireGuard); reseat PCIe card (wlp1s0 NO-CARRIER). Until fixed monad-04 will miss experiments when 2.4 GHz WiFi blips.
5. **EC2 SSH key on monad-04** — add EC2 pubkey `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHuDRLJrO2MktJBNTmh72w7XcBJyHQwai9WQb5mPMQ4/ ladamik@ip-172-31-8-46.ec2.internal` to monad-04's `~/.ssh/authorized_keys`.
6. **Retire old bad experiments** — exps 9, 12, 13, 51, 57, 60, 62, 65, 66 may still appear in fleet service parse-error logs. Confirm their `fleet` tags are removed.
7. **MySQL stability on EC2** — `start_period: 60s` added to health check fixes the startup race. The ibdata1 lock issue is fixed by always doing `docker rm -f` before recreating. Consider adding a proper `stop_grace_period` to the compose service.
8. **Implement BLE listener Prometheus metrics** — see Known Issue 1.
9. **Decide monad-02 WiFi approach** — see Known Issue 2 (Option B quickest).
10. **Schedule next experiment** — monad-04 and monad-06 missed exp131; create a new exp once services are stable.
