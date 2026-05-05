# EC2 Deployment Handoff - 2026-05-05

Target:

- EC2 public IP: `34.198.184.128`
- SSH user: `ladamik`
- Instance ID: `i-08dc3dd367365bac3`
- Region: `us-east-1`
- Instance type observed after resize: `m7i-flex.large` (`2 vCPU`, `7.6 GiB RAM`)
- Security group: `sg-09900ca13e1d76374` (`fleetmanager-ec2-FleetManagerSecurityGroup-qiRLZcBha5g9`)

Deployment path:

- `/home/ladamik/FleetManager_deploy`
- Remote env file: `/home/ladamik/FleetManager_deploy/.env.aws`
- The env file contains secrets and must not be copied into git or logs.

What was deployed:

- Fresh stack on `34.198.184.128`; no old database data or artifacts were migrated.
- Current local FleetManager checkout was synced to the EC2 host.
- eLabFTW custom image was built from `hokanuSK/elabftw` `master` at commit `841494b39e14963149b2b5bdb2416dc0066fe779`.
- eLabFTW admin bootstrap completed.
- A write-capable eLabFTW API key was created in the remote DB and written only to remote `.env.aws`.

Local code changes made for deploy:

- Added `.dockerignore` so `artifacts/`, `data/`, and the eLabFTW submodule are not sent in Docker build contexts.
- Updated `scripts/aws/build_elabimg_hypernext.sh`:
  - supports a `curl`/`tar` fallback when `git` is unavailable on the EC2 host,
  - explicitly runs eLabFTW `mkbuildinfo.php` so `src/Elabftw/BuildInfo.php` exists in the custom image.

Runtime status:

```bash
ssh ladamik@34.198.184.128
export PATH="$HOME/bin:$PATH"
export DOCKER_HOST=unix:///run/user/1002/docker.sock
cd /home/ladamik/FleetManager_deploy
docker compose --project-directory "$PWD" --env-file "$PWD/.env.aws" -f "$PWD/infrastructure/aws/docker-compose.aws.yml" ps
```

Expected running services:

- `mysql`
- `web`
- `monad-fleet-service`
- `model-device`
- `mimir`
- `prometheus`
- `grafana`

Internal verification passed from the EC2 host:

- `https://127.0.0.1:9443/login.php` -> `200`
- `https://10.200.0.1:9443/login.php` -> serving over WireGuard/VPN
- `https://elab-monad-fleet.34.198.184.128.sslip.io:9443/login.php` with `--resolve ...:127.0.0.1` -> `200`
- `https://127.0.0.1:9443/api/v2/experiments` with the remote API key -> `200`
- `http://127.0.0.1:9108/metrics` -> `200`
- `http://127.0.0.1:3000/api/health` -> `200`
- `http://127.0.0.1:9009/ready` -> `200`

VPN access verification:

- EC2 WireGuard interface: `wg0 = 10.200.0.1/24`.
- `monad-01` (`10.200.0.10`) verified:
  - `https://10.200.0.1:9443/login.php` -> `200`
  - `http://10.200.0.1:9108/metrics` -> `200`
  - `10.200.0.1:50060` -> TCP open
- `monad-02` (`10.200.0.11`) verified:
  - `https://10.200.0.1:9443/login.php` -> `200`
  - `http://10.200.0.1:9108/metrics` -> `200`
  - `10.200.0.1:50060` -> TCP open
- Remote `.env.aws` was updated to advertise the VPN URL:
  - `ELAB_SITE_URL=https://10.200.0.1:9443`
  - `ELAB_SERVER_NAME=10.200.0.1`

Public internet access note:

- External curls from the local machine to `34.198.184.128:9443` and `34.198.184.128:9108` timed out.
- This is acceptable for the intended VPN-only path.
- Do not open public AWS security-group rules for the Pi run unless explicit public access is required later.
- If public access is later required, the relevant security group is `sg-09900ca13e1d76374`; the instance role cannot modify it.

Rootless Docker persistence:

- `ladamik` has rootless Docker and `Linger=no`.
- `loginctl enable-linger ladamik` failed without sudo: `Access denied`.
- A temporary detached keepalive loop is running to keep the user manager/rootless Docker alive:
  - PID file: `/tmp/fleetmanager-rootless-docker-keepalive.pid`
  - log: `/tmp/fleetmanager-rootless-docker-keepalive.log`
- Proper fix, run with sudo/admin privileges:

```bash
sudo loginctl enable-linger ladamik
ssh ladamik@34.198.184.128 'systemctl --user enable --now docker.service && loginctl show-user ladamik -p Linger'
ssh ladamik@34.198.184.128 'kill "$(cat /tmp/fleetmanager-rootless-docker-keepalive.pid)"'
```

Next steps for the two-Pi experiment:

1. Keep the run on VPN-local addresses.
2. Re-check VPN access from each Pi:

```bash
curl -k -I --max-time 15 https://10.200.0.1:9443/login.php
curl -sS --max-time 10 http://10.200.0.1:9108/metrics | head
timeout 3 bash -lc '</dev/tcp/10.200.0.1/50060' && echo grpc=open
```

3. Configure the Pi agents to use:

```text
FLEET_MANAGER_HOST=10.200.0.1
FLEET_MANAGER_PORT=50060
FLEET_ARTIFACT_INGEST_URL=http://10.200.0.1:9108/ingest/v1/artifacts
```

4. Schedule the two-Pi eLabFTW experiment against the new base URL:

```text
https://10.200.0.1:9443/api/v2
```

Known real Pi device IDs from previous runs:

- `2c:cf:67:81:00:45`
- `24:eb:16:e3:6a:07`

Keep CSI disabled for the immediate two-Pi run unless the CSI blocker from the previous handoff has been resolved.
