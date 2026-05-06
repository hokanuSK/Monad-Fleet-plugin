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
- Devices should keep using VPN-local Fleet endpoints:
  - `FLEET_MANAGER_HOST=10.200.0.1`
  - `FLEET_MANAGER_PORT=50060`
  - `FLEET_ARTIFACT_INGEST_URL=http://10.200.0.1:9108/ingest/v1/artifacts`

Public internet access:

- Grafana is publicly reachable:
  - `http://34.198.184.128:3000/api/health` -> `200`
  - Browser URL: `http://34.198.184.128:3000`
- Remote `.env.aws` was restored to advertise public eLabFTW URLs:
  - `ELAB_SITE_URL=https://elab-monad-fleet.34.198.184.128.sslip.io:9443`
  - `ELAB_SERVER_NAME=elab-monad-fleet.34.198.184.128.sslip.io`
- eLabFTW is healthy on the host and over VPN, but public `9443/tcp` currently times out from the internet.
- Required AWS security-group change for public eLabFTW:
  - Add inbound `TCP 9443` to `sg-09900ca13e1d76374`.
  - Source can be the operator IP for restricted access, or `0.0.0.0/0` for broad temporary public access.
- Public `443/tcp` is reachable to the instance but no process is listening there. With current rootless Docker and no sudo, binding `443` is not possible (`net.ipv4.ip_unprivileged_port_start=1024`).
- The EC2 instance role cannot modify security groups (`ec2:AuthorizeSecurityGroupIngress` denied), and local AWS credentials were invalid (`AuthFailure`), so this rule must be added from AWS credentials/console with EC2 security-group permissions.

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

1. Keep device traffic on VPN-local addresses.
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
https://elab-monad-fleet.34.198.184.128.sslip.io:9443/api/v2
```

If creating/scheduling from a host on the VPN, `https://10.200.0.1:9443/api/v2` also works, but public UI links should use the public sslip hostname.

Known real Pi device IDs from previous runs:

- `2c:cf:67:81:00:45`
- `24:eb:16:e3:6a:07`

Keep CSI disabled for the immediate two-Pi run unless the CSI blocker from the previous handoff has been resolved.

## Two-Pi VPN no-CSI run on EC2 experiment 1 (2026-05-06)

Experiment created on the fresh EC2 eLabFTW instance:

- eLabFTW experiment id: `1`
- title: `Fleet Dual Pi VPN no-CSI 20260506T110635Z`
- public UI link: `https://elab-monad-fleet.34.198.184.128.sslip.io:9443/experiments.php?mode=view&id=1`
- remote context: `/home/ladamik/FleetManager_deploy/artifacts/output/ops/ec2_dual_pi_no_csi_experiment_1_20260506T110635Z.json`

Fleet state on EC2 shows both Pi agents completed:

- Pi1 `2c:cf:67:81:00:45`
  - run id: `73a54b36-4dc1-4081-9992-f06cd4611b8f`
  - status: `COMPLETED`
  - eLab uploads: `wireless-run-evidence-bundle.tar.gz`, `run-summary.json`
- Pi2 `24:eb:16:e3:6a:07`
  - run id: `0d0a98c9-824b-4fea-b61f-5638468e14d1`
  - status: `COMPLETED`
  - report was accepted by Fleet after EC2-side replay from the saved Pi2 `report.json`
  - eLab upload: `run-pi2-summary.json` recovery summary
  - Pi2 local spool was marked sent after Fleet accepted the replay, so it should not keep retrying this run

Verifier output on EC2:

- `/home/ladamik/FleetManager_deploy/artifacts/output/ops/ec2_dual_pi_no_csi_experiment_1_uploads.json`
- latest verifier result: `uploads_total=3`, agents `24:eb:16:e3:6a:07` and `2c:cf:67:81:00:45`

Important Pi2 network finding:

- Pi2 reconnected successfully and reached Fleet over `wg0`.
- Small requests worked, but larger HTTP/gRPC/SCP payloads stalled or timed out.
- MTU check from Pi2:
  - `ping -M do -s 1372 10.200.0.1` lost packets
  - `ping -M do -s 1200 10.200.0.1` passed
- Observed MTUs:
  - EC2 `wg0`: `8921`
  - Pi2 `wg0`: `1420`
- Temporary MTU correction could not be applied from this session because both EC2 and Pi2 require sudo for `ip link set`.
- Before the next artifact-heavy run, persist a conservative WireGuard MTU, for example `MTU = 1280`, on the EC2 and Pi peer configs, then restart WireGuard.

Pi2 deployment note:

- Pi2 had `/home/monad/monad-fleet-agent` and global Python dependencies, but no `.env`, no venv, and no installed `monad-fleet-agent.service`.
- Before recurring scheduled runs, deploy/install Pi2 properly:

```bash
PI_HOST=10.200.0.11 scripts/rpi/deploy_agent.sh
PI_HOST=10.200.0.11 FLEET_MANAGER_HOST=10.200.0.1 scripts/rpi/install_systemd_service.sh
```

Do not store eLabFTW credentials or API keys in this handoff or Notion.
