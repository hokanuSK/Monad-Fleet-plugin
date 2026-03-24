#!/usr/bin/env bash
set -euo pipefail

CONTEXT_PATH="${1:-artifacts/output/ops/latest_task_context.json}"
NOTION_API_TOKEN="${NOTION_API_TOKEN:-}"
NOTION_TASKS_DB_ID="${NOTION_TASKS_DB_ID:-4a5936bb0baa4614bb61bc8daed18cee}"

if [[ ! -f "${CONTEXT_PATH}" ]]; then
  echo "ERROR: task context not found: ${CONTEXT_PATH}"
  exit 1
fi

if [[ -z "${NOTION_API_TOKEN}" ]]; then
  echo "WARN: NOTION_API_TOKEN is empty; skip task sync."
  exit 1
fi

python3 - "${CONTEXT_PATH}" "${NOTION_API_TOKEN}" "${NOTION_TASKS_DB_ID}" <<'PY'
import json
import sys
import urllib.error
import urllib.request

ctx_path, token, db_id = sys.argv[1], sys.argv[2], sys.argv[3]
with open(ctx_path, "r", encoding="utf-8") as fh:
    ctx = json.load(fh)

title = (ctx.get("title") or "Follow-up task").strip()
details = [
    f"generated_at_utc: {ctx.get('generated_at_utc', '')}",
    f"priority: {ctx.get('priority', '')}",
    f"status: {ctx.get('status', '')}",
    f"owner: {ctx.get('owner', '')}",
    f"due_date: {ctx.get('due_date', '')}",
    f"source: {ctx.get('source', '')}",
    f"reference: {ctx.get('reference', '')}",
    f"notes: {ctx.get('notes', '')}",
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
    print(f"ERROR: Notion task sync failed HTTP {exc.code}: {error_body}")
    raise SystemExit(1)
except Exception as exc:
    print(f"ERROR: Notion task sync failed: {exc}")
    raise SystemExit(1)

print("notion_task_page_url", body.get("url", ""))
PY
