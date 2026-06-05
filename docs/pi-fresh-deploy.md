# Pi Fresh Deploy Runbook

Steps to deploy the monad fleet agent on a freshly imaged Pi (Ubuntu 24.04 or Raspberry Pi OS).
Verified on monad-04 (Ubuntu 24.04) 2026-05-21.

---

## Prerequisites

- Pi is on WireGuard VPN (wg0 tunnel to 10.200.0.1 must be up)
- Pi is reachable via `sshpass -p monad ssh -J ladamik@34.198.184.128 monad@<vpn-ip>`
- Mac has `sshpass` installed (`brew install hudochenkov/sshpass/sshpass`)

## 1. Clear stale SSH known_hosts (if Pi was reflashed)

```bash
ssh-keygen -R <vpn-ip>
ssh -o StrictHostKeyChecking=no ladamik@34.198.184.128 "ssh-keygen -R <vpn-ip> 2>/dev/null; true"
```

## 2. Grant passwordless sudo

Fresh Ubuntu installs require the password interactively. Use `printf` to pipe it:

```bash
sshpass -p monad ssh -o StrictHostKeyChecking=no -J ladamik@34.198.184.128 monad@<vpn-ip> \
  "printf 'monad\n' | sudo -S bash -c \
   'echo \"monad ALL=(ALL) NOPASSWD: ALL\" > /etc/sudoers.d/monad-nopasswd && chmod 440 /etc/sudoers.d/monad-nopasswd'"
```

After this, `sudo -n` works without password for all subsequent steps.

> **Note:** `echo monad | sudo -S` does NOT work inside a `bash -s <<'HEREDOC'` because the
> heredoc consumes stdin before sudo can read from it. Always use `printf 'monad\n' | sudo -S`
> or, after passwordless sudo is set, `sudo -n`.

## 3. Build and stream agent tarball

```bash
cd /path/to/FleetManager/src/device-sim
tar czf /tmp/monad-agent-deploy.tar.gz agent_v2/ agent_v2_client.py requirements.txt proto/

sshpass -p monad ssh -o StrictHostKeyChecking=no -J ladamik@34.198.184.128 monad@<vpn-ip> \
  "mkdir -p /home/monad/monad-fleet-agent && cat > /tmp/agent.tar.gz" \
  < /tmp/monad-agent-deploy.tar.gz

sshpass -p monad ssh -o StrictHostKeyChecking=no -J ladamik@34.198.184.128 monad@<vpn-ip> \
  "cd /home/monad/monad-fleet-agent && tar xzf /tmp/agent.tar.gz"
```

macOS `tar` emits `LIBARCHIVE.xattr` warnings on the Pi — benign, extraction succeeds.

## 4. Copy pre-compiled pb2 files from a working Pi

The `proto/` directory contains `.proto` sources but not the compiled `_pb2.py` files.
Copy them from another Pi (e.g. monad-03 at 10.200.0.12):

```bash
for FILE in fleet_gateway_pb2.py fleet_gateway_pb2_grpc.py \
            fleet_gateway_v2_pb2.py fleet_gateway_v2_pb2_grpc.py \
            fleet_gateway_v3_pb2.py fleet_gateway_v3_pb2_grpc.py; do
  sshpass -p monad ssh -o StrictHostKeyChecking=no -J ladamik@34.198.184.128 monad@10.200.0.12 \
    "cat /home/monad/monad-fleet-agent/$FILE" | \
  sshpass -p monad ssh -o StrictHostKeyChecking=no -J ladamik@34.198.184.128 monad@<vpn-ip> \
    "cat > /home/monad/monad-fleet-agent/$FILE"
done
```

> **TODO:** Add compiled pb2 files to the deploy tarball so this step is unnecessary.

## 5. Create Python venv and install dependencies

```bash
sshpass -p monad ssh -o StrictHostKeyChecking=no -J ladamik@34.198.184.128 monad@<vpn-ip> \
  "cd /home/monad/monad-fleet-agent && python3 -m venv venv && venv/bin/pip install -q -r requirements.txt"
```

> **Note:** `requirements.txt` does not explicitly list `protobuf` or `grpcio` — the venv
> will be missing them. Install manually after:
> ```bash
> venv/bin/pip install -q protobuf grpcio
> ```
> **TODO:** Add `protobuf` and `grpcio` to `requirements.txt`.

## 6. Write .env

Per-device values to change: `AGENT_ID`, `WIFI_SCAN_IFACE`, `MONAD_HOSTNAME`.

| Pi | VPN IP | AGENT_ID | WIFI_SCAN_IFACE |
|----|--------|----------|-----------------|
| monad-02 | 10.200.0.11 | 24:eb:16:e3:6a:07 | wlan0 |
| monad-03 | 10.200.0.12 | 2c:cf:67:80:f5:86 | wlp1s0 |
| monad-04 | 10.200.0.13 | 2c:cf:67:80:f4:bf | wlp1s0 |
| monad-05 | 10.200.0.14 | 2c:cf:67:81:00:6b | wlp1s0 |
| monad-06 | 10.200.0.15 | 2c:cf:67:81:00:d9 | wlp1s0 |

Write the .env via heredoc into a single SSH command (not inside `bash -s`):

```bash
sshpass -p monad ssh -o StrictHostKeyChecking=no -J ladamik@34.198.184.128 monad@<vpn-ip> \
  "cat > /home/monad/monad-fleet-agent/.env" <<'ENV'
FLEET_MANAGER_HOST=10.200.0.1
FLEET_MANAGER_PORT=50060
GRPC_DNS_RESOLVER=native
AGENT_ID=<mac>
CONTROL_PLANE_MODE=RF_SHARING
CONTROL_PLANE_IFACE=wg0
WIFI_SCAN_IFACE=<iface>
MAX_SYNC_CYCLES=0
EXECUTE_POLICY=true
CAPABILITIES=csi,ble,wifi
DATA_ROOT=/home/monad/monad-fleet-agent/data
SENT_RETENTION_DAYS=14
ARTIFACT_UPLOAD_TARGET=fleet_http
ARTIFACT_UPLOAD_DURING_MEASURE=true
ARTIFACT_EVICT_AFTER_UPLOAD=true
ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S=3
ARTIFACT_UPLOAD_BACKOFF_S=15
GRPC_ARTIFACT_CHUNK_BYTES=1024
ENABLE_COMMAND_STATUS_REPORTS=true
ENABLE_UPLOAD_STATUS_REPORTS=true
RUN_ARTIFACT_SOFT_LIMIT_BYTES=0
UPLOAD_WINDOW_FROM=
UPLOAD_WINDOW_TO=
UPLOAD_WINDOW_REQUIRED=false
UPLOAD_SLOT_COUNT=1
UPLOAD_SLOT_JITTER_S=0
PROM_REMOTE_WRITE_ON_CMD=""
PROM_REMOTE_WRITE_OFF_CMD=""
PROM_REMOTE_WRITE_CMD_TIMEOUT_S=12
METRICS_PORT=9110
DEFAULT_METRICS_SINKS=fleet_http
METRICS_SINKS=
MONAD_HOSTNAME=<hostname>
FLEET_ARTIFACT_INGEST_URL=http://10.200.0.1:9108/ingest/v1/artifacts
ARTIFACT_UPLOAD_MAX_BYTES=524288000
HELLO_RPC_TIMEOUT_S=60
CONTROL_RPC_TIMEOUT_S=30
SINGLE_RADIO_MODE=false
ENV
```

## 7. Install and start systemd service

Write service file to /tmp first (no sudo), then copy:

```bash
sshpass -p monad ssh -o StrictHostKeyChecking=no -J ladamik@34.198.184.128 monad@<vpn-ip> \
  "cat > /tmp/monad-fleet-agent.service" <<'SVC'
[Unit]
Description=Monad Fleet Agent v2
After=network-online.target wg-quick@wg0.service
Wants=network-online.target wg-quick@wg0.service

[Service]
Type=simple
User=monad
WorkingDirectory=/home/monad/monad-fleet-agent
EnvironmentFile=/home/monad/monad-fleet-agent/.env
ExecStart=/home/monad/monad-fleet-agent/venv/bin/python -u /home/monad/monad-fleet-agent/agent_v2_client.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVC

sshpass -p monad ssh -o StrictHostKeyChecking=no -J ladamik@34.198.184.128 monad@<vpn-ip> \
  "sudo -n cp /tmp/monad-fleet-agent.service /etc/systemd/system/ && \
   sudo -n systemctl daemon-reload && \
   sudo -n systemctl enable monad-fleet-agent && \
   sudo -n systemctl start monad-fleet-agent && \
   sleep 3 && systemctl is-active monad-fleet-agent"
```

## 8. Verify

```bash
sshpass -p monad ssh -o StrictHostKeyChecking=no -J ladamik@34.198.184.128 monad@<vpn-ip> \
  "journalctl -u monad-fleet-agent -n 20 --no-pager"
```

Expect to see `Assignment context: experiment_id=elabftw:XXX` within ~60s if a `fleet`-tagged
experiment is active and within its window.

---

## Known issues / TODOs

- **pb2 files not in tarball**: Must be copied from a working Pi. Add to deploy artifact.
- **`protobuf` + `grpcio` missing from requirements.txt**: Must be pip-installed separately.
- **EC2 relay has no `sshpass`**: All password-auth SSH to Pis must go from the Mac via
  `-J ladamik@34.198.184.128`, not from inside EC2.
- **`sudo -S` in `bash -s` heredoc fails**: The heredoc consumes stdin. Use `printf 'monad\n' | sudo -S`
  or write to /tmp then `sudo -n cp`.
- **Host key changes on reflash**: Run `ssh-keygen -R <vpn-ip>` on both Mac and EC2 before reconnecting.
