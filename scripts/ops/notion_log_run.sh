#!/usr/bin/env bash
set -euo pipefail

CONTEXT_PATH="${1:-output/ops/latest_run_context.json}"
NOTION_API_TOKEN="${NOTION_API_TOKEN:-}"
NOTION_RUNS_DB_ID="${NOTION_RUNS_DB_ID:-8dc731920bf34966bb711472100c7057}"

if [[ ! -f "${CONTEXT_PATH}" ]]; then
  echo "ERROR: run context not found: ${CONTEXT_PATH}"
  exit 1
fi

if [[ -z "${NOTION_API_TOKEN}" ]]; then
  echo "WARN: NOTION_API_TOKEN is empty; skip run sync."
  exit 1
fi

python3 - "${CONTEXT_PATH}" "${NOTION_API_TOKEN}" "${NOTION_RUNS_DB_ID}" <<'PY'
import json
import sys
import urllib.error
import urllib.request

ctx_path, token, db_id = sys.argv[1], sys.argv[2], sys.argv[3]
with open(ctx_path, "r", encoding="utf-8") as fh:
    ctx = json.load(fh)

run_id = (ctx.get("run_id") or "").strip() or "n/a"
result = (ctx.get("result") or "unknown").strip()
profile = (ctx.get("smoke_profile") or "unknown").strip()
experiment_id = (ctx.get("smoke_experiment_id") or "").strip()

title = f"run:{run_id} result:{result} profile:{profile}"
details = [
    f"generated_at_utc: {ctx.get('generated_at_utc', '')}",
    f"started_at_utc: {ctx.get('started_at_utc', '')}",
    f"result: {result}",
    f"run_id: {run_id}",
    f"experiment_id: {experiment_id}",
    f"smoke_profile: {profile}",
    f"run_pi: {ctx.get('run_pi', '')}",
    f"run_pi_passive: {ctx.get('run_pi_passive', '')}",
    f"device_model: {ctx.get('device_model', '')}",
    f"device_pi: {ctx.get('device_pi', '')}",
    f"target_device_ids_csv: {ctx.get('target_device_ids_csv', '')}",
    f"last_step: {ctx.get('last_step', '')}",
    f"last_command: {ctx.get('last_command', '')}",
]

payload = {
    "parent": {"database_id": db_id},
    "properties": {
        "Name": {
            "title": [
                {"type": "text", "text": {"content": title}},
            ]
        }
    },
    "children": [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": "\n".join(details)[:1900]},
                    }
                ]
            },
        }
    ],
}

req = urllib.request.Request(
    "https://api.notion.com/v1/pages",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    error_body = exc.read().decode("utf-8", errors="replace")
    print(f"ERROR: Notion run sync failed HTTP {exc.code}: {error_body}")
    raise SystemExit(1)
except Exception as exc:
    print(f"ERROR: Notion run sync failed: {exc}")
    raise SystemExit(1)

print("notion_run_page_url", body.get("url", ""))
PY
