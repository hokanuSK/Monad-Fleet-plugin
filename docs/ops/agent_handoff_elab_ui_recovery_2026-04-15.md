# Agent Handoff: eLab UI/HTML Recovery (2026-04-15)

## Summary
- Public host: `https://elab-monad-fleet.32.195.200.179.sslip.io`
- Reported symptom: "HTML not showing" (unstyled/broken UI).
- Actual failure mode: login HTML rendered, but core frontend assets returned `404`.
- Final state: fixed and verified; DB schema upgraded to `206`; UI assets return `200`.

## What Failed
Initial live checks showed:
- `GET /login.php` -> `200`
- `GET /assets/vendor.bundle.js` -> `404`
- `GET /assets/main.bundle.js` -> `404`
- `GET /assets/vendor.min.css` -> `404`
- `GET /assets/elabftw.min.css` -> `404`

Inside running `web` container (`elabftw/elabimg:custom`), `/elabftw/web/assets` contained only:
- `fonts/`
- `images/`

No JS/CSS bundles existed in that image.

## Root Cause
- Active deployment path on EC2 was `/home/ubuntu/FleetManager_deploy` (not `/home/ubuntu/FleetManager`).
- Active web image in `.env.aws` was `ELAB_WEB_IMAGE=elabftw/elabimg:custom`.
- That custom image lacked compiled frontend bundles, so eLab served HTML referencing files that did not exist.

## Actions Taken
1. Confirmed active compose stack and env in `/home/ubuntu/FleetManager_deploy`.
2. Replaced broken custom image with official image:
   - `ELAB_WEB_IMAGE=elabftw/elabimg:5.5.8`
3. Recreated services:
   - `web`
   - `reverse-proxy`
4. Ran DB migration:
   - `docker exec elabftw-aws-web-1 sh -lc 'cd /elabftw && bin/console db:update --no-interaction'`
5. Migration failed at `schema203.sql` because `items_types.canbook` was missing in DB (schema drift).
6. Applied compatibility repair in MySQL:
   - `ALTER TABLE items_types ADD COLUMN canbook JSON NULL;`
   - `UPDATE items_types SET canbook = canread WHERE canbook IS NULL;`
7. Re-ran `db:update` successfully to schema `206`.

## Verification
After repair:
- Config schema row:
  - `schema = 206`
- Public checks:
  - `GET /` -> `302`
  - `GET /login.php` -> `200`
  - `GET /assets/vendor.bundle.js` -> `200`
  - `GET /assets/main.bundle.js` -> `200`
  - `GET /assets/vendor.min.css` -> `200`
  - `GET /assets/elabftw.min.css` -> `200`
- "Almost there / Database update required" banner no longer present.

## Operational Notes
- EC2 access was done via EC2 Instance Connect temporary key injection (`ubuntu` user), because direct SSH key access was not initially available.
- Compose project uses:
  - `docker-compose.aws.yml`
  - `docker-compose.proxy.yml`
  - `.env.aws`
  in `/home/ubuntu/FleetManager_deploy`.

## Follow-ups
1. Rebuild/fix `elabftw/elabimg:custom` so it includes frontend bundles before switching back from `5.5.8`.
2. Document and remove DB schema drift source (`items_types.canbook` missing at schema `202`).
3. Rotate/revoke exposed AWS access keys shared during incident handling.
