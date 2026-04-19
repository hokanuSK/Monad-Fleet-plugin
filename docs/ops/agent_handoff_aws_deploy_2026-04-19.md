# AWS Deploy Handoff (2026-04-19)

## Scope
Provision and deploy FleetManager stack to a new AWS account/instance in `eu-north-1`, with reverse proxy + TLS on `sslip.io` hostnames.

## AWS Provisioning (new account)
- IAM principal used for provisioning: `arn:aws:iam::890381434495:user/agent2`
- Region: `eu-north-1`
- Instance:
  - `InstanceId`: `i-0bdf5d0df9c9b2010`
  - Type: `t3.small`
  - Public IP: `16.171.70.171`
  - AZ: `eu-north-1c`
  - Root volume: `30GB gp3`
- Networking:
  - VPC: `vpc-065560b3e8a51ecb7` (default)
  - Subnet: `subnet-0d96df56c06f6db39`
  - Security Group: `sg-07a91a82e649233c3`
  - Inbound opened: `22, 80, 443, 3000, 50060, 9009, 9090, 9443, 9108`
- SSH key pair:
  - Key name: `fleetmanager-key-20260419205532`
  - Local private key path: `/tmp/fleetmanager-key-20260419205532.pem`
  - Local metadata env: `/tmp/fleet_ec2_meta.sh`

## Deployment Outcome
Deployed on host `ubuntu@16.171.70.171` from repo branch `develop`.

Primary URLs:
- eLabFTW: `https://elab-monad-fleet.16.171.70.171.sslip.io`
- Grafana: `https://grafana-monad-fleet.16.171.70.171.sslip.io`
- Mimir: `https://mimir-monad-fleet.16.171.70.171.sslip.io`
- Fleet metrics (proxy): `https://metrics-monad-fleet.16.171.70.171.sslip.io/metrics`
- Fleet gRPC: `16.171.70.171:50060`

Service status (host-side `docker compose ps`):
- `mysql`: up (healthy)
- `web`: up (healthy)
- `monad-fleet-service`: up
- `model-device`: up
- `mimir`: up
- `grafana`: up
- `reverse-proxy`: up

External checks from local:
- `ELAB_LOGIN_HTTP=200`
- `GRAFANA_HEALTH_HTTP=200`
- `METRICS_HTTP=200`
- `nc 16.171.70.171 50060` succeeded

## Script Fixes Applied (and pushed)
Two deployment reliability fixes were made and pushed to `origin/develop`:

1. `41b38ea` — force compose project root in AWS scripts
- Files:
  - `scripts/aws/deploy_ec2_stack.sh`
  - `scripts/aws/elabftw_bootstrap_admin.sh`
  - `scripts/aws/verify_ec2_stack.sh`
- Change: add `--project-directory "$ROOT_DIR"` to `docker compose` commands.
- Why: avoids broken relative path resolution when compose file is under `infrastructure/aws/`.

2. `1fe6a32` — fix reverse-proxy TLS verification checks
- File:
  - `scripts/aws/verify_ec2_stack.sh`
- Change: replace `Host`-header-only checks on `https://127.0.0.1` with `curl --resolve host:443:127.0.0.1`.
- Why: TLS SNI must match host; without SNI, Caddy can return TLS internal-error alert.

## Credentials Handling
- Remote credential snapshot file (contains passwords):
  - `/tmp/fleetmanager_deploy_credentials_20260419190354.txt`
- Local copied credential snapshot (contains passwords):
  - `/tmp/fleetmanager_deploy_credentials_new_account_20260419212110.txt`

Do not commit these files.

## Notes
- First deploy verification failed initially because certificates were still being issued; after issuance and verify-script SNI fix, verification succeeds.
- Reverse proxy certificates were obtained from Let's Encrypt (seen in Caddy logs).
- If future deployments should be fully IaC, next step is to create a CloudFormation template for EC2 + SG + key pair + outputs and optionally SSM-backed secret injection.
