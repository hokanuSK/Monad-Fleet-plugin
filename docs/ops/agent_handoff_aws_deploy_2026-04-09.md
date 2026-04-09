# AWS Deploy Handoff (2026-04-09)

## Goal
Deploy `hokanuSK/Monad-Fleet-plugin` (`develop`) to AWS EC2, using eLabFTW image source from `hokanuSK/elabftw` branch `hypernext`.

## Repo Update (2026-04-09, develop)
AWS deployment starter artifacts were added to this repo:
- `docker-compose.aws.yml`
- `.env.aws.example`
- `scripts/aws/deploy_ec2_stack.sh`
- `scripts/aws/build_elabimg_hypernext.sh`
- `scripts/aws/elabftw_bootstrap_admin.sh`
- `scripts/aws/verify_ec2_stack.sh`
- `docs/runbooks/aws_ec2_deploy.md`
- `infra/aws/docker/fleet-service.Dockerfile`
- `infra/aws/docker/device-sim.Dockerfile`

Intent:
- stop relying on ad-hoc host edits,
- deploy with one reproducible script,
- include first-admin bootstrap and asset checks,
- avoid Docker build failures caused by `apps/*/proto` symlinks by using AWS-specific Dockerfiles with repo-root build context.

Newest commit check on `develop`:
- `git submodule update --init --recursive` still fails for `elabimg` commit `b1a95b121cf7a511b83ee88dab7d223a47371dfd` (missing upstream).
- Workaround is codified in `scripts/aws/build_elabimg_hypernext.sh` (direct clone of upstream `elabimg` + source tarball patch to `hokanuSK/elabftw` branch `hypernext`).

## Current Active Environment
- Region: `us-east-1`
- Active instance: `i-0b68042aa66951809` (`t3.micro`)
- Active public IP: `32.195.200.179`
- Web URL: `https://32.195.200.179:9443`
- SSH:
  - `ssh -i /tmp/fleetmanager-key-20260409000516.pem ubuntu@32.195.200.179`
- Security group: `sg-022af3bf0b59fb506`
  - Open: `22, 3000, 50060, 9009, 9090, 8443, 9443`

## Runtime Status
From active host `docker compose ps`:
- `web` (`elabftw`) up and healthy
- `mysql` up and healthy
- `monad-fleet-service`, `model-device`, `prometheus`, `mimir`, `grafana` up

Validation done on host:
- `curl -k -I https://127.0.0.1:9443` -> `HTTP/2 200`
- `curl -k -I https://32.195.200.179:9443` (from host) -> `HTTP/2 200`
- `curl http://127.0.0.1:3000/api/health` -> Grafana health JSON
- `curl http://127.0.0.1:9108/metrics` returns fleet metrics

## Current Bootstrap State
- First eLabFTW sysadmin was created manually via:
  - `docker exec elabftw bin/init db:install -n --reset -e <email> -f <first> -l <last> -p <password> -t <team>`
- In this run, login works and first admin exists.
- This step is currently manual and should be automated in-project for reproducible production deploys.

## What Must Be Added For AWS Production
1. First-admin bootstrap automation
- Add a script, for example `scripts/aws/elabftw_bootstrap_admin.sh`, that:
  - waits for `web` and `mysql` health,
  - checks whether user table is empty,
  - creates first sysadmin only when `users_count=0`,
  - never prints password in logs.
- Inputs via env:
  - `ELAB_ADMIN_EMAIL`, `ELAB_ADMIN_FIRSTNAME`, `ELAB_ADMIN_LASTNAME`, `ELAB_ADMIN_PASSWORD`, `ELAB_TEAM_NAME`.

2. Secrets externalization
- Remove hardcoded secrets from `docker-compose.yml`:
  - `MYSQL_ROOT_PASSWORD`, `MYSQL_PASSWORD`, `DB_PASSWORD`, `SECRET_KEY`, `ELAB_API_KEY`.
- Load secrets from `.env` (not committed) or AWS SSM/Secrets Manager.
- Add `.env.aws.example` with placeholders only.

3. Production compose profile
- Add `docker-compose.aws.prod.yml` with:
  - stable public port mapping (`443:443` or ALB-only ingress),
  - no localhost-only bindings for public service,
  - explicit restart policies and healthchecks for all core services,
  - resource limits for `web` and `mysql`.

4. TLS and hostname
- Use domain + ACM + ALB (recommended) or terminate TLS on instance proxy.
- Set `SITE_URL` and `SERVER_NAME` to final domain, not raw IP.
- Keep `DISABLE_HTTPS=false`.

5. Data durability and recovery
- Move MySQL and uploads to durable storage strategy:
  - regular snapshots/backups,
  - documented restore runbook.
- Add scripts:
  - `scripts/aws/backup_mysql.sh`
  - `scripts/aws/restore_mysql.sh`

6. Deployment entrypoint script
- Add one reproducible script, for example `scripts/aws/deploy_production.sh`, to:
  - clone/update correct branch,
  - ensure `elabimg` source patch for `hypernext`,
  - build images,
  - run compose,
  - run admin bootstrap script,
  - run smoke checks and print service URLs.

7. Post-deploy smoke checks
- Add `scripts/aws/verify_production.sh`:
  - verify `https://<domain>/login.php` returns `200`,
  - verify `web` healthy,
  - verify fleet metrics endpoint and Grafana health.

8. Access and cost hygiene
- Restrict security group ingress to required CIDRs for SSH/admin.
- Keep only one active EC2 instance per environment.
- Add cleanup script for stale instances/volumes.

## Important Fixes Applied
1. Submodule issue workaround
- `develop` points `elabimg` submodule to missing commit (`b1a95b...`).
- Workaround on host: replaced submodule content with direct clone:
  - `git clone --depth 1 https://github.com/elabftw/elabimg.git elabimg`

2. eLabFTW source switched to requested fork/branch source
- Patched `elabimg/Dockerfile` tarball source to:
  - `https://github.com/hokanuSK/elabftw/tarball/$ELABFTW_VERSION`
- Built image with:
  - `ELABFTW_VERSION=hypernext`

3. Tar extraction pattern patched for fork tarball naming
- Changed:
  - `mv elabftw-* src`
- To:
  - `mv *elabftw* src`

4. Docker build-context symlink issue fixed
- Replaced proto symlinks in app contexts with real files:
  - `apps/fleet-service/proto/*`
  - `apps/device-sim/proto/*`

5. Resource constraints addressed
- Added 4G swap on active instance.
- Root EBS volume expanded from 8G -> 30G and filesystem grown online.

6. Port mapping conflict resolved
- Base compose binds `127.0.0.1:8443`.
- Override now publishes external `9443:443` to avoid conflict.

7. Runtime fix for missing PHP vendor deps
- Because build used `BUILD_ALL=0`, dependencies were missing initially.
- Installed in running `web` container:
  - `/usr/bin/php84 -d memory_limit=256M -d open_basedir="" /usr/local/bin/composer install --prefer-dist --no-cache --no-progress --no-dev -a`

## Active Host Working Tree (Expected Drift)
On active host repo:
- Branch: `develop`
- Commit: `30abc9b`
- Local changes:
  - `D apps/device-sim/proto`
  - `D apps/fleet-service/proto`
  - `M elabimg`
  - `?? docker-compose.aws.override.yml`

This drift is intentional for deployment workaround and not committed.

## Reproduce / Re-enter Commands
On active instance:
```bash
cd ~/FleetManager
sudo docker compose -f docker-compose.yml -f docker-compose.aws.override.yml ps
sudo docker compose -f docker-compose.yml -f docker-compose.aws.override.yml logs --tail 120 web
```

Create first admin user (only if `users_count=0`):
```bash
cd ~/FleetManager
# WARNING: --reset is destructive; use only on first-time fresh environment bootstrap.
sudo docker exec elabftw bin/init db:install -n --reset \
  -e "$ELAB_ADMIN_EMAIL" \
  -f "$ELAB_ADMIN_FIRSTNAME" \
  -l "$ELAB_ADMIN_LASTNAME" \
  -p "$ELAB_ADMIN_PASSWORD" \
  -t "$ELAB_TEAM_NAME"
```

Verify first admin exists:
```bash
sudo docker exec mysql mysql -uelabftw -p"$MYSQL_PASSWORD" -Delabftw -e \
  "SELECT COUNT(*) AS users_count FROM users; SELECT userid,email,is_sysadmin,validated FROM users;"
```

If web fails with missing vendor again:
```bash
cd ~/FleetManager
sudo docker compose -f docker-compose.yml -f docker-compose.aws.override.yml exec -T web sh -lc \
  'cd /elabftw && /usr/bin/php84 -d memory_limit=256M -d open_basedir="" /usr/local/bin/composer install --prefer-dist --no-cache --no-progress --no-dev -a'
```

## Additional Instances / Cleanup
There is another running instance from failed path:
- `i-0268d0b4994b2bd94` (public IP currently `3.235.141.250`)

Recommendation:
- Keep only active instance `i-0b68042aa66951809`.
- Stop/terminate the old instance to avoid extra cost.

## Local Reference File
Local env snapshot for this handoff:
- `/tmp/fleetmanager_aws_deploy_current.env`
