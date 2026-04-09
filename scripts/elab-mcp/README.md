# eLabFTW MCP Server (Read-Only by Default)

Tento server pridava read-only MCP introspekciu pre eLabFTW policy/artifact debug bez zasahu do existujucich runtime logik.

## Co poskytuje

- Resources:
  - `elab://config`
  - `elab://diagnostics/help`
- Resource templates:
  - `elab://experiments/{id}`
  - `elab://experiments/{id}/metadata`
  - `elab://experiments/{id}/uploads?limit={n}`
  - `elab://fleet/latest-policy?device_id={id}`
  - `elab://fleet/diagnostics?device_id={id}&run_id={run_id}`
  - `elab://fleet/validate?experiment_id={id}`
  - `elab://fleet/journal?limit={n}&experiment_id={id}&run_id={run_id}&agent_id={agent_id}`
- Tools:
  - `elab_read`
  - `elab_get_latest_policy`
  - `elab_get_diagnostics`
  - `elab_get_experiment_uploads`
  - `elab_validate_experiment`
  - `elab_get_journal_tail`
  - `elab_create_policy_experiment` (`dry_run=true` default)

## Konfiguracia

Server cita nastavenia z ENV, a ked chybaju, skusi fallback z `infrastructure/docker-compose.yml`.

- `ELAB_BASE_URL` (fallback z compose; default `https://web/api/v2`)
- `ELAB_API_KEY` (fallback z compose)
- `ELAB_VERIFY_TLS` (`false` default)
- `FLEET_EXPERIMENT_TAG` (default `fleet`)
- `RESOURCE_TAG_PREFIX` (default `device:`)
- `POLICY_METADATA_KEYS` (default `fleet.policy,policy,fleet_policy,policy_json`)
- `ELAB_MCP_ARTIFACT_JOURNAL_PATH` (optional override)
- `ELAB_MCP_TIMEOUT_S` (default `20`)
- `ELAB_MCP_BASE_URL_FALLBACKS` (optional CSV)
- `ELAB_MCP_ENABLE_WRITE` (default `false`; required for real write execution)

Default journal path:
- preferovane `data/fleet/ingest-artifacts.ndjson`
- fallback `data/ingest-artifacts.ndjson`

## Spustenie

```bash
python3 scripts/elab-mcp/server.py
```

## Docker Compose (on-demand)

V `infrastructure/docker-compose.yml` je pridana sluzba `elab-mcp` pod profilom `mcp`, aby sa nespustala pri beznom `docker compose up`.

Spustenie cez Compose (stdio MCP):

```bash
docker compose -f infrastructure/docker-compose.yml run --rm -T elab-mcp
```

Priklad MCP konfiguracie:

```json
{
  "mcpServers": {
    "elab-fleet": {
      "command": "docker",
      "args": ["compose", "run", "--rm", "-T", "elab-mcp"]
    }
  }
}
```

## Smoke test MCP handshake

Lokalne:

```bash
MODE=local scripts/smoke/mcp_elab_smoke.sh
```

Cez Docker Compose:

```bash
MODE=compose scripts/smoke/mcp_elab_smoke.sh
```

## Poznamky

- Server je default read-only.
- Diagnostika (`fleet/diagnostics`, `fleet/validate`) urcuje ocakavane artifact triedy podla command typov v policy (nie fixne `wifi+ble+csi`), cize nevznika false-positive pri `rssi` profile.
- Write flow je dostupny len cez `elab_create_policy_experiment`:
  - defaultne bezi v `dry_run=true` (bez side effectu),
  - realne write volania su povolene len ked `ELAB_MCP_ENABLE_WRITE=true` a `dry_run=false`.
- Implementacia je izolovana v `scripts/elab-mcp/` a nemenila existujuce sluzby (`monad-fleet-service`, `device-sim`, smoke skripty).
