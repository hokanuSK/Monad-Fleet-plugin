# AWS EC2 Deploy Runbook

This runbook deploys the current repo state on one EC2 host using Docker Compose.

## Files Added
- `docker-compose.aws.yml`
- `.env.aws.example`
- `scripts/aws/deploy_ec2_stack.sh`
- `scripts/aws/build_elabimg_hypernext.sh`
- `scripts/aws/elabftw_bootstrap_admin.sh`
- `scripts/aws/verify_ec2_stack.sh`

## 1) Prepare Host
Install Docker Engine + Compose plugin on the EC2 instance, then clone this repo.

## 2) Prepare Secrets
Create env file from template:

```bash
cp .env.aws.example .env.aws
```

Set strong secrets in `.env.aws`:
- `MYSQL_ROOT_PASSWORD`
- `MYSQL_PASSWORD`
- `ELAB_SECRET_KEY`
- `GRAFANA_ADMIN_PASSWORD`
- `ELAB_API_KEY`
- `ELAB_ADMIN_PASSWORD`

Set final URL values:
- `ELAB_SITE_URL` (for example `https://elab.example.com`)
- `ELAB_SERVER_NAME` (for example `elab.example.com`)

Optional helper for `ELAB_SECRET_KEY`:

```bash
openssl rand -hex 48
```

## 3) Deploy

```bash
scripts/aws/deploy_ec2_stack.sh
```

Behavior:
- builds `monad-fleet-service` + `model-device` images,
- starts the stack from `docker-compose.aws.yml`,
- bootstraps first eLabFTW admin only when users table is empty,
- runs post-deploy verification checks.

If you need eLabFTW from `hokanuSK/elabftw:hypernext`, build that image first during deploy:

```bash
BUILD_ELAB_IMAGE=true scripts/aws/deploy_ec2_stack.sh
```

## 4) Re-run Commands
Deploy without rebuild:

```bash
BUILD_IMAGES=false scripts/aws/deploy_ec2_stack.sh
```

Build only the custom eLabFTW image:

```bash
scripts/aws/build_elabimg_hypernext.sh
```

Run bootstrap only:

```bash
scripts/aws/elabftw_bootstrap_admin.sh
```

Run verification only:

```bash
scripts/aws/verify_ec2_stack.sh
```

## 5) Notes
- Keep `.env.aws` out of git.
- Recommended production ingress: domain + TLS termination (ALB/ACM or reverse proxy).
- If the app renders HTML without CSS/JS, verify asset URLs from `scripts/aws/verify_ec2_stack.sh`.
