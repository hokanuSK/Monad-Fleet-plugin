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
- `https://elab-monad-fleet.34.198.184.128.sslip.io:9443/login.php` with `--resolve ...:127.0.0.1` -> `200`
- `https://127.0.0.1:9443/api/v2/experiments` with the remote API key -> `200`
- `http://127.0.0.1:9108/metrics` -> `200`
- `http://127.0.0.1:3000/api/health` -> `200`
- `http://127.0.0.1:9009/ready` -> `200`

Open blocker for external access:

- External curls from the local machine to `34.198.184.128:9443` and `34.198.184.128:9108` timed out.
- The EC2 host is listening on `0.0.0.0:9443`, `0.0.0.0:50060`, `0.0.0.0:9108`, `0.0.0.0:3000`, and `0.0.0.0:9009`, so this is an AWS security group issue.
- Local AWS credentials are invalid (`AuthFailure`).
- The EC2 instance role cannot modify security groups (`ec2:AuthorizeSecurityGroupIngress` denied).

Required AWS security group rules:

- Add inbound `TCP 9443` to `sg-09900ca13e1d76374` for eLabFTW web/API access.
- Add inbound `TCP 50060` to `sg-09900ca13e1d76374` for the real Pi fleet gRPC agents.
- Prefer restricting sources to the operator/Pi public IPs. For a quick experiment, `0.0.0.0/0` works but exposes the service broadly.
- Keep `3000`, `9108`, and `9009` private unless remote Grafana/metrics access is explicitly needed.

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

1. Open inbound `9443/tcp` and `50060/tcp` in AWS security group `sg-09900ca13e1d76374`.
2. Re-check external access:

```bash
curl -k -I --max-time 15 https://elab-monad-fleet.34.198.184.128.sslip.io:9443/login.php
```

3. Configure the Pi agents to use:

```text
FLEET_MANAGER_HOST=34.198.184.128
FLEET_MANAGER_PORT=50060
```

4. Schedule the two-Pi eLabFTW experiment against the new base URL:

```text
https://elab-monad-fleet.34.198.184.128.sslip.io:9443/api/v2
```

Known real Pi device IDs from previous runs:

- `2c:cf:67:81:00:45`
- `24:eb:16:e3:6a:07`

Keep CSI disabled for the immediate two-Pi run unless the CSI blocker from the previous handoff has been resolved.
