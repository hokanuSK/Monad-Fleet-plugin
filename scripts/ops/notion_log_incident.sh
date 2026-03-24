#!/usr/bin/env bash
set -euo pipefail

CONTEXT_PATH="${1:-output/ops/latest_incident_context.json}"
NOTION_API_TOKEN="${NOTION_API_TOKEN:-}"
NOTION_INCIDENTS_DB_ID="${NOTION_INCIDENTS_DB_ID:-da72692b8106430bb1e3892866d41d20}"

if [[ ! -f "${CONTEXT_PATH}" ]]; then
  echo "ERROR: incident context not found: ${CONTEXT_PATH}"
  exit 1
fi

if [[ -z "${NOTION_API_TOKEN}" ]]; then
  echo "WARN: NOTION_API_TOKEN is empty; skip incident sync."
  exit 1
fi

python3 - "${CONTEXT_PATH}" "${NOTION_API_TOKEN}" "${NOTION_INCIDENTS_DB_ID}" <<'PY'
import json
import sys
import urllib.error
import urllib.request

ctx_path, token, db_id = sys.argv[1], sys.argv[2], sys.argv[3]
with open(ctx_path, "r", encoding="utf-8") as fh:
    ctx = json.load(fh)

severity = (ctx.get("severity") or "S3").strip()
status = (ctx.get("status") or "open").strip()
experiment_id = (ctx.get("experiment_id") or "").strip()
title = f"{severity} incident: smoke failure"

details = [
    f"generated_at_utc: {ctx.get('generated_at_utc', '')}",
    f"status: {status}",
    f"run_id: {ctx.get('run_id', '')}",
    f"experiment_id: {experiment_id}",
    f"symptom: {ctx.get('symptom', '')}",
    f"root_cause: {ctx.get('root_cause', '')}",
    f"fix: {ctx.get('fix', '')}",
    f"prevention: {ctx.get('prevention', '')}",
    f"links: {ctx.get('links', '')}",
]
context = ctx.get("context") or {}
if context:
    details.append("context:")
    for key, value in context.items():
        details.append(f"  {key}: {value}")

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
    print(f"ERROR: Notion incident sync failed HTTP {exc.code}: {error_body}")
    raise SystemExit(1)
except Exception as exc:
    print(f"ERROR: Notion incident sync failed: {exc}")
    raise SystemExit(1)

print("notion_incident_page_url", body.get("url", ""))
PY
