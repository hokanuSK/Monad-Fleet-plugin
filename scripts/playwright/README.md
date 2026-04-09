# Playwright Automations (CLI)

This folder contains Playwright-driven browser automations for the local FleetManager dev stack.

Artifacts (screenshots, traces, extracted values) are written under `artifacts/output/playwright/`.

## Prereqs

1. Bring up the stack (eLabFTW must be reachable at `https://localhost:8443`):

```bash
cd /Users/admin/FleetManager
docker compose -f infrastructure/docker-compose.yml up -d
docker compose -f infrastructure/docker-compose.yml ps
```

2. Ensure `npx` is available (comes with Node.js/npm):

```bash
command -v npx
```

## Generate an eLabFTW REST API Key (UI)

This automates:

1. Open eLabFTW login page
2. Log in (defaults match the eLabFTW `db:populate` demo user)
3. Navigate to `My Profile` (`ucp.php`) -> `API keys`
4. Generate a key and save it to a file (keys are only shown once)

Run:

```bash
cd /Users/admin/FleetManager

# Optional overrides:
# ELAB_EMAIL / ELAB_PASSWORD: credentials
# ELAB_KEY_NAME: the name shown in the eLabFTW UI
# ELAB_KEY_CANWRITE: 0 (read-only) or 1 (read/write)
# HEADLESS: true/false
# TRACE: true/false (saves a trace)
# KEEP_BROWSER_OPEN: true/false

ELAB_BASE_URL=https://localhost:8443 \
ELAB_EMAIL=toto@yopmail.com \
ELAB_PASSWORD=totototototo \
ELAB_KEY_NAME="monad-fleet-service" \
ELAB_KEY_CANWRITE=1 \
HEADLESS=false \
TRACE=true \
scripts/playwright/elabftw_generate_rest_api_key.sh
```

Output:

- A per-run folder under `artifacts/output/playwright/elabftw_api_key/<timestamp>/`
- The generated key is in `artifacts/output/playwright/elabftw_api_key/<timestamp>/elabftw_rest_api_key.txt`

Next:

- Update `ELAB_API_KEY` in `/Users/admin/FleetManager/infrastructure/docker-compose.yml` for the `monad-fleet-service` container (do not commit real secrets).
