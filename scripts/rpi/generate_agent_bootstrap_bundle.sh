#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

DEVICE_CSV="${DEVICE_CSV:-${ROOT_DIR}/scripts/rpi/agent_bundle_devices.example.csv}"
OUT_DIR="${OUT_DIR:-${ROOT_DIR}/artifacts/output/rpi-agent-bootstrap/${TIMESTAMP}}"
FLEET_MANAGER_HOST="${FLEET_MANAGER_HOST:-$(bash "${ROOT_DIR}/scripts/rpi/default_fleet_manager_host.sh")}"
FLEET_MANAGER_PORT="${FLEET_MANAGER_PORT:-$(bash "${ROOT_DIR}/scripts/rpi/default_fleet_manager_port.sh")}"
PI_USER_DEFAULT="${PI_USER_DEFAULT:-monad}"
PI_DIR_DEFAULT="${PI_DIR_DEFAULT:-/home/${PI_USER_DEFAULT}/monad-fleet-agent}"
EXECUTE_POLICY_DEFAULT="${EXECUTE_POLICY_DEFAULT:-true}"
MAX_SYNC_CYCLES_DEFAULT="${MAX_SYNC_CYCLES_DEFAULT:-0}"
SENT_RETENTION_DAYS_DEFAULT="${SENT_RETENTION_DAYS_DEFAULT:-14}"
ARTIFACT_UPLOAD_DURING_MEASURE_DEFAULT="${ARTIFACT_UPLOAD_DURING_MEASURE_DEFAULT:-true}"
ARTIFACT_EVICT_AFTER_UPLOAD_DEFAULT="${ARTIFACT_EVICT_AFTER_UPLOAD_DEFAULT:-true}"
RUN_ARTIFACT_SOFT_LIMIT_BYTES_DEFAULT="${RUN_ARTIFACT_SOFT_LIMIT_BYTES_DEFAULT:-0}"
STRIP_WG_DNS_DEFAULT="${STRIP_WG_DNS_DEFAULT:-true}"

usage() {
  cat <<EOF
Usage: scripts/rpi/generate_agent_bootstrap_bundle.sh

Generate per-device agent bootstrap bundles for freshly flashed Ubuntu devices
that already have network access and WireGuard configured.

Environment:
  DEVICE_CSV         input CSV
                     default: ${DEVICE_CSV}
  OUT_DIR            output directory
                     default: ${OUT_DIR}
  FLEET_MANAGER_HOST Fleet manager host written into bundle env
                     default: ${FLEET_MANAGER_HOST}
  FLEET_MANAGER_PORT Fleet manager port written into bundle env
                     default: ${FLEET_MANAGER_PORT}
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ ! -f "${DEVICE_CSV}" ]]; then
  echo "ERROR: device CSV not found: ${DEVICE_CSV}" >&2
  exit 2
fi

mkdir -p "${OUT_DIR}"

export ROOT_DIR TIMESTAMP DEVICE_CSV OUT_DIR FLEET_MANAGER_HOST FLEET_MANAGER_PORT
export PI_USER_DEFAULT PI_DIR_DEFAULT EXECUTE_POLICY_DEFAULT MAX_SYNC_CYCLES_DEFAULT
export SENT_RETENTION_DAYS_DEFAULT ARTIFACT_UPLOAD_DURING_MEASURE_DEFAULT
export ARTIFACT_EVICT_AFTER_UPLOAD_DEFAULT RUN_ARTIFACT_SOFT_LIMIT_BYTES_DEFAULT
export STRIP_WG_DNS_DEFAULT

python3 <<'PY'
import csv
import os
from pathlib import Path

root_dir = Path(os.environ["ROOT_DIR"])
timestamp = os.environ["TIMESTAMP"]
device_csv = Path(os.environ["DEVICE_CSV"])
out_dir = Path(os.environ["OUT_DIR"])

defaults = {
    "fleet_manager_host": os.environ["FLEET_MANAGER_HOST"],
    "fleet_manager_port": os.environ["FLEET_MANAGER_PORT"],
    "pi_user": os.environ["PI_USER_DEFAULT"],
    "pi_dir": os.environ["PI_DIR_DEFAULT"],
    "execute_policy": os.environ["EXECUTE_POLICY_DEFAULT"],
    "max_sync_cycles": os.environ["MAX_SYNC_CYCLES_DEFAULT"],
    "sent_retention_days": os.environ["SENT_RETENTION_DAYS_DEFAULT"],
    "artifact_upload_during_measure": os.environ["ARTIFACT_UPLOAD_DURING_MEASURE_DEFAULT"],
    "artifact_evict_after_upload": os.environ["ARTIFACT_EVICT_AFTER_UPLOAD_DEFAULT"],
    "run_artifact_soft_limit_bytes": os.environ["RUN_ARTIFACT_SOFT_LIMIT_BYTES_DEFAULT"],
    "strip_wg_dns": os.environ["STRIP_WG_DNS_DEFAULT"],
}

required_columns = [
    "hostname",
    "agent_id",
    "control_plane_iface",
    "wifi_scan_iface",
    "capabilities",
    "artifact_upload_target",
    "default_metrics_sinks",
    "grpc_dns_resolver",
]

optional_columns = [
    "prom_pi_id",
    "prom_site",
    "prom_instance",
    "prom_metrics_port",
    "prom_exposition_port",
    "prom_remote_write_url",
]

rows = []
with device_csv.open(newline="", encoding="utf-8") as fh:
    reader = csv.DictReader(fh)
    missing = [name for name in required_columns if name not in (reader.fieldnames or [])]
    if missing:
        raise SystemExit(f"ERROR: missing CSV columns: {', '.join(missing)}")
    for raw in reader:
        hostname = (raw.get("hostname") or "").strip()
        if not hostname:
            continue
        row = {name: (raw.get(name) or "").strip() for name in required_columns}
        for name in optional_columns:
            row[name] = (raw.get(name) or "").strip()
        row["control_plane_iface"] = row["control_plane_iface"] or "wg0"
        row["wifi_scan_iface"] = row["wifi_scan_iface"] or row["control_plane_iface"]
        row["capabilities"] = row["capabilities"] or "csi,ble,wifi"
        row["artifact_upload_target"] = row["artifact_upload_target"] or "fleet_http"
        row["default_metrics_sinks"] = row["default_metrics_sinks"] or "fleet_http"
        row["grpc_dns_resolver"] = row["grpc_dns_resolver"] or "native"
        row["prom_pi_id"] = row["prom_pi_id"] or row["hostname"]
        row["prom_site"] = row["prom_site"] or "monad-fleet"
        row["prom_instance"] = row["prom_instance"] or row["hostname"]
        row["prom_metrics_port"] = row["prom_metrics_port"] or "9110"
        row["prom_exposition_port"] = row["prom_exposition_port"] or row["prom_metrics_port"]
        row["prom_remote_write_url"] = row["prom_remote_write_url"] or "http://10.200.0.1:9009/api/v1/push"
        rows.append(row)

manifest_path = out_dir / "manifest.csv"
with manifest_path.open("w", newline="", encoding="utf-8") as fh:
    writer = csv.DictWriter(fh, fieldnames=required_columns + optional_columns)
    writer.writeheader()
    writer.writerows(rows)

for row in rows:
    device_dir = out_dir / row["hostname"]
    device_dir.mkdir(parents=True, exist_ok=True)

    env_lines = [
        f"MONAD_HOSTNAME='{row['hostname']}'",
        f"MONAD_AGENT_ID='{row['agent_id']}'",
        f"MONAD_PI_USER='{defaults['pi_user']}'",
        f"MONAD_PI_DIR='{defaults['pi_dir']}'",
        f"MONAD_FLEET_MANAGER_HOST='{defaults['fleet_manager_host']}'",
        f"MONAD_FLEET_MANAGER_PORT='{defaults['fleet_manager_port']}'",
        "MONAD_CONTROL_PLANE_MODE='RF_SHARING'",
        f"MONAD_CONTROL_PLANE_IFACE='{row['control_plane_iface']}'",
        f"MONAD_WIFI_SCAN_IFACE='{row['wifi_scan_iface']}'",
        f"MONAD_CAPABILITIES='{row['capabilities']}'",
        f"MONAD_EXECUTE_POLICY='{defaults['execute_policy']}'",
        f"MONAD_MAX_SYNC_CYCLES='{defaults['max_sync_cycles']}'",
        f"MONAD_SENT_RETENTION_DAYS='{defaults['sent_retention_days']}'",
        f"MONAD_ARTIFACT_UPLOAD_TARGET='{row['artifact_upload_target']}'",
        f"MONAD_ARTIFACT_UPLOAD_DURING_MEASURE='{defaults['artifact_upload_during_measure']}'",
        f"MONAD_ARTIFACT_EVICT_AFTER_UPLOAD='{defaults['artifact_evict_after_upload']}'",
        f"MONAD_RUN_ARTIFACT_SOFT_LIMIT_BYTES='{defaults['run_artifact_soft_limit_bytes']}'",
        f"MONAD_DEFAULT_METRICS_SINKS='{row['default_metrics_sinks']}'",
        f"MONAD_GRPC_DNS_RESOLVER='{row['grpc_dns_resolver']}'",
        f"MONAD_STRIP_WG_DNS='{defaults['strip_wg_dns']}'",
        f"MONAD_PROM_PI_ID='{row['prom_pi_id']}'",
        f"MONAD_PROM_SITE='{row['prom_site']}'",
        f"MONAD_PROM_INSTANCE='{row['prom_instance']}'",
        f"MONAD_PROM_METRICS_PORT='{row['prom_metrics_port']}'",
        f"MONAD_PROM_EXPOSITION_PORT='{row['prom_exposition_port']}'",
        f"MONAD_PROM_REMOTE_WRITE_URL='{row['prom_remote_write_url']}'",
    ]
    (device_dir / "agent.env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    readme = f"""# Agent Bootstrap Bundle: {row['hostname']}

This bundle assumes:

- Ubuntu is already flashed and booted
- the device already has working network access
- WireGuard is already configured and can reach {defaults['fleet_manager_host']}:{defaults['fleet_manager_port']}
- Prometheus will be configured to scrape 127.0.0.1:{row['prom_metrics_port']} and remote_write to {row['prom_remote_write_url']}

Install from the operator machine with:

```bash
PI_HOST=<reachable-host-or-ip> \\
  scripts/rpi/install_flashed_ubuntu_agent.sh {device_dir}
```
"""
    (device_dir / "README.md").write_text(readme, encoding="utf-8")

top_readme = f"""# Monad Agent Bootstrap Bundles

Generated: {timestamp}

Scope:

- runtime stabilization on fresh Ubuntu
- agent deploy + venv creation
- systemd service installation

WireGuard provisioning is intentionally out of scope for these bundles.

Per-device bundle format:

- `agent.env` — device-specific deployment parameters
- `README.md` — install example

One-command install:

```bash
PI_HOST=<reachable-host-or-ip> \\
  scripts/rpi/install_flashed_ubuntu_agent.sh {out_dir}/<hostname>
```
"""
(out_dir / "README.md").write_text(top_readme, encoding="utf-8")
PY

echo "Generated agent bootstrap bundles in: ${OUT_DIR}"
echo "Manifest: ${OUT_DIR}/manifest.csv"
