#!/usr/bin/env python3
"""Generate Grafana dashboards under a folder named for one eLabFTW experiment."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_SRC = REPO_ROOT / "src/fleet-service"
DEFAULT_TEMPLATE_DIR = REPO_ROOT / "infrastructure/observability/grafana/provisioning/dashboards/static"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "infrastructure/observability/grafana/provisioning/dashboards/experiments"

if str(SERVICE_SRC) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC))

from monad_fleet_service.grafana_dashboards import (  # noqa: E402
    DashboardProvisioningConfig,
    DEFAULT_INSTANCE_MAP_TEXT,
    device_ids_from_policy,
    generate_experiment_dashboards,
    normalize_list,
    parse_instance_map,
)


def _load_policy(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise RuntimeError(f"policy JSON must be an object: {path}")

    fleet = doc.get("fleet")
    if isinstance(fleet, dict) and isinstance(fleet.get("policy"), dict):
        return fleet["policy"]
    if isinstance(doc.get("policy"), dict):
        return doc["policy"]
    return doc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-id", required=True, help="eLabFTW experiment id, for example 7 or elabftw:7")
    parser.add_argument("--device-ids", default="", help="Comma or whitespace separated target device ids")
    parser.add_argument("--policy-json", type=Path, default=None, help="Optional experiment/policy JSON to derive device ids")
    parser.add_argument("--device-regex", default="", help="Override Grafana device_id regex")
    parser.add_argument("--instance-regex", default="", help="Override Pi-side Prometheus instance regex")
    parser.add_argument(
        "--instance-map",
        default=os.environ.get("GRAFANA_EXPERIMENT_INSTANCE_MAP", DEFAULT_INSTANCE_MAP_TEXT),
        help="device_id=prometheus_instance pairs, comma or newline separated",
    )
    parser.add_argument("--default-instance-regex", default="", help="Fallback instance regex when no map entry matches")
    parser.add_argument("--folder-name", default="", help="Override generated Grafana folder/directory name")
    parser.add_argument("--template-dir", type=Path, default=DEFAULT_TEMPLATE_DIR)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    device_ids = normalize_list(args.device_ids)
    if not device_ids and args.policy_json is not None:
        device_ids = device_ids_from_policy(_load_policy(args.policy_json))

    config = DashboardProvisioningConfig(
        template_dir=args.template_dir,
        output_root=args.output_root,
        instance_map=parse_instance_map(args.instance_map),
        default_instance_regex=args.default_instance_regex,
        folder_name=args.folder_name,
        device_regex=args.device_regex,
        instance_regex=args.instance_regex,
    )
    written = generate_experiment_dashboards(
        experiment_id=args.experiment_id,
        device_ids=device_ids,
        config=config,
    )
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
