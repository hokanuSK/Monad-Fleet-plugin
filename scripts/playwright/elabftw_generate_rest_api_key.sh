#!/usr/bin/env bash
set -euo pipefail

# Generate an eLabFTW REST API key via the browser UI using Playwright CLI.
#
# Output:
# - A per-run folder under artifacts/output/playwright/ containing screenshots + the API key file.

umask 077

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"

# ---- Config (override via env) ----
export TMPDIR="${PLAYWRIGHT_TMPDIR:-/private/tmp}"

ELAB_BASE_URL="${ELAB_BASE_URL:-https://localhost:8443}"
ELAB_EMAIL="${ELAB_EMAIL:-toto@yopmail.com}"
ELAB_PASSWORD="${ELAB_PASSWORD:-totototototo}"
ELAB_KEY_NAME="${ELAB_KEY_NAME:-codex-$(date +%Y%m%d_%H%M%S)}"
ELAB_KEY_CANWRITE="${ELAB_KEY_CANWRITE:-1}" # 0=Read Only, 1=Read/Write

HEADLESS="${HEADLESS:-true}"   # true/false
TRACE="${TRACE:-false}"        # true/false
KEEP_BROWSER_OPEN="${KEEP_BROWSER_OPEN:-false}" # true/false

# ---- Playwright CLI wrapper (Codex skill) ----
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
PWCLI="${PWCLI:-$CODEX_HOME/skills/playwright/scripts/playwright_cli.sh}"

if ! command -v npx >/dev/null 2>&1; then
  cat >&2 <<'EOF'
Error: npx is required but not found on PATH.

# Verify Node/npm are installed
node --version
npm --version

# If missing, install Node.js/npm, then:
npm install -g @playwright/cli@latest
playwright-cli --help
EOF
  exit 1
fi

if [[ ! -x "${PWCLI}" ]]; then
  echo "Error: Playwright CLI wrapper not found/executable at: ${PWCLI}" >&2
  echo "Hint: expected from Codex skill at: ~/.codex/skills/playwright/scripts/playwright_cli.sh" >&2
  exit 1
fi

if [[ "${HEADLESS}" != "true" && "${HEADLESS}" != "false" ]]; then
  echo "Error: HEADLESS must be 'true' or 'false' (got: ${HEADLESS})" >&2
  exit 1
fi
if [[ "${TRACE}" != "true" && "${TRACE}" != "false" ]]; then
  echo "Error: TRACE must be 'true' or 'false' (got: ${TRACE})" >&2
  exit 1
fi
if [[ "${KEEP_BROWSER_OPEN}" != "true" && "${KEEP_BROWSER_OPEN}" != "false" ]]; then
  echo "Error: KEEP_BROWSER_OPEN must be 'true' or 'false' (got: ${KEEP_BROWSER_OPEN})" >&2
  exit 1
fi
if [[ "${ELAB_KEY_CANWRITE}" != "0" && "${ELAB_KEY_CANWRITE}" != "1" ]]; then
  echo "Error: ELAB_KEY_CANWRITE must be '0' or '1' (got: ${ELAB_KEY_CANWRITE})" >&2
  exit 1
fi

run_id="$(date +%Y%m%d_%H%M%S)"
out_dir="${repo_root}/artifacts/output/playwright/elabftw_api_key/${run_id}"
mkdir -p "${out_dir}"

session="elabftw-${run_id}"

pw() {
  local cmd="${1:-}"
  shift || true
  if [[ "${cmd}" == "open" ]]; then
    "${PWCLI}" --session "${session}" open --config "${out_dir}/playwright-cli.json" "$@"
  else
    "${PWCLI}" --session "${session}" "${cmd}" "$@"
  fi
}

cat >"${out_dir}/playwright-cli.json" <<JSON
{
  "browser": {
    "launchOptions": {
      "headless": ${HEADLESS}
    },
    "contextOptions": {
      "ignoreHTTPSErrors": true,
      "viewport": { "width": 1280, "height": 720 }
    }
  }
}
JSON

cd "${out_dir}"

export ELAB_BASE_URL ELAB_EMAIL ELAB_PASSWORD ELAB_KEY_NAME ELAB_KEY_CANWRITE

login_url="${ELAB_BASE_URL%/}/login.php?letmein"
ucp_url="${ELAB_BASE_URL%/}/ucp.php"

json_string() {
  python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$1"
}

ELAB_BASE_URL_JS="$(json_string "${ELAB_BASE_URL%/}")"
ELAB_EMAIL_JS="$(json_string "${ELAB_EMAIL}")"
ELAB_PASSWORD_JS="$(json_string "${ELAB_PASSWORD}")"
ELAB_KEY_NAME_JS="$(json_string "${ELAB_KEY_NAME}")"
ELAB_KEY_CANWRITE_JS="$(json_string "${ELAB_KEY_CANWRITE}")"

if [[ "${TRACE}" == "true" ]]; then
  pw tracing-start
fi

pw open "${login_url}"

# Use run-code for stable selectors (avoid snapshot element refs).
pw run-code "async (page) => {
    await page.waitForSelector('form#login', { timeout: 30_000 });
    await page.fill('#email', ${ELAB_EMAIL_JS});
    await page.fill('#password', ${ELAB_PASSWORD_JS});
    const navPromise = page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 30_000 }).catch(() => null);
    await page.click(\"form#login button[type='submit']\");
    await navPromise;
  }"

pw run-code "async (page) => {
    await page.goto(${ELAB_BASE_URL_JS} + '/ucp.php', { waitUntil: 'domcontentloaded' });
    // The UCP uses a loading spinner while fetching user data.
    await page.waitForSelector('#loading-spinner', { state: 'hidden', timeout: 30_000 }).catch(() => {});
    await page.waitForSelector('ul.tabbed-menu', { timeout: 30_000 });

    await page.click(\"button[data-action='switch-tab'][data-tabtarget='3']\");
    await page.waitForSelector('#apikeyName', { state: 'visible', timeout: 30_000 });
    await page.fill('#apikeyName', ${ELAB_KEY_NAME_JS});
    await page.selectOption('#apikeyCanwrite', (${ELAB_KEY_CANWRITE_JS} === '1') ? '1' : '0');
    await page.click(\"button[data-action='create-apikey']\");

    // The key is only shown once; wait for the value to be populated.
    await page.waitForFunction(() => {
      const el = document.querySelector('#newApiKeyInput');
      return el && el.value && el.value.length > 0;
    }, null, { timeout: 30_000 });
  }"

pw screenshot

# Extract the one-time key from the page.
api_key="$(pw eval '() => document.querySelector("#newApiKeyInput")?.value' | tail -n 1 | tr -d '\r')"
# Some CLIs wrap strings; strip a single pair of surrounding quotes if present.
api_key="${api_key%\"}"
api_key="${api_key#\"}"

if [[ -z "${api_key}" || "${api_key}" == "undefined" || "${api_key}" == "null" ]]; then
  echo "Error: Failed to extract API key from the page (got: '${api_key}')." >&2
  echo "Artifacts are in: ${out_dir}" >&2
  exit 1
fi

key_file="${out_dir}/elabftw_rest_api_key.txt"
printf '%s\n' "${api_key}" >"${key_file}"
chmod 600 "${key_file}"

if [[ "${TRACE}" == "true" ]]; then
  pw tracing-stop
fi

if [[ "${KEEP_BROWSER_OPEN}" != "true" ]]; then
  pw close || true
fi

cat <<EOF
OK: Generated eLabFTW REST API key
- Key file: ${key_file}
- Artifacts: ${out_dir}

Next:
- Set ELAB_API_KEY in infrastructure/docker-compose.yml (monad-fleet-service) to the generated key (do not commit real secrets).
EOF
