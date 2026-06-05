#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from dataclasses import dataclass

import requests


DEFAULT_BASE_URL = "https://localhost:8443/api/v2"
DEFAULT_VERIFY_TLS = False


@dataclass
class StatusDef:
    title: str
    color: str


RESOURCE_STATUSES = [
    StatusDef("Operational", "2e7d32"),
    StatusDef("Waiting", "ef6c00"),
    StatusDef("Open", "1565c0"),
    StatusDef("Processed", "6a1b9a"),
    StatusDef("Maintenance mode", "c62828"),
]

EXPERIMENT_STATUSES = [
    StatusDef("Queued", "ef6c00"),
    StatusDef("Running", "1565c0"),
    StatusDef("Done", "2e7d32"),
    StatusDef("Failed", "c62828"),
]


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ElabClient:
    def __init__(self) -> None:
        self.base_url = os.environ.get("ELAB_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.api_key = os.environ["ELAB_API_KEY"]
        self.session = requests.Session()
        self.session.verify = env_bool("ELAB_VERIFY_TLS", DEFAULT_VERIFY_TLS)
        self.session.headers.update({"Authorization": self.api_key})

    def request(self, method: str, path: str, **kwargs):
        response = self.session.request(method, f"{self.base_url}{path}", timeout=30, **kwargs)
        response.raise_for_status()
        if not response.text:
            return None
        return response.json()


def ensure_statuses(client: ElabClient, team_id: int, endpoint: str, wanted: list[StatusDef]) -> dict[str, int]:
    existing = client.request("GET", f"/teams/{team_id}/{endpoint}") or []
    by_title = {row["title"]: int(row["id"]) for row in existing if row.get("title")}
    for status in wanted:
        if status.title in by_title:
            continue
        location = client.request(
            "POST",
            f"/teams/{team_id}/{endpoint}",
            json={"title": status.title, "color": status.color},
        )
        created = client.request("GET", location["location"].replace(client.base_url, ""))
        by_title[status.title] = int(created["id"])
    return by_title


def create_resource_example(client: ElabClient, title: str, status_id: int, runtime_state: str, tags: list[str]) -> int:
    payload = {
        "title": title,
        "body": json.dumps({"example": True, "kind": "device-resource", "runtime_state": runtime_state}),
        "status": status_id,
        "metadata": json.dumps(
            {
                "device_id": title.lower().replace(" ", "-"),
                "fleet_v2_runtime": {
                    "device_state": runtime_state,
                    "agent_id": title.lower().replace(" ", "-"),
                    "example_seed": True,
                },
            }
        ),
        "tags": tags,
        "is_bookable": 1,
    }
    created = client.request("POST", "/items", json=payload)
    return int(created["id"])


def create_experiment_example(client: ElabClient, title: str, status_id: int, tags: list[str]) -> int:
    payload = {
        "title": title,
        "body": f"Localhost example for list-state UI: {title}",
        "status": status_id,
        "tags": tags,
        "metadata": json.dumps({"example_seed": True, "ui_check": "list-state"}),
    }
    created = client.request("POST", "/experiments", json=payload)
    return int(created["id"])


def main() -> None:
    client = ElabClient()
    me = client.request("GET", "/users/me")
    team_id = int(me["teams"][0]["id"])

    resource_status_ids = ensure_statuses(client, team_id, "items_status", RESOURCE_STATUSES)
    experiment_status_ids = ensure_statuses(client, team_id, "experiments_status", EXPERIMENT_STATUSES)

    created_resources = [
        create_resource_example(
            client,
            "Device MONAD-01",
            resource_status_ids["Operational"],
            "ONLINE",
            ["fleet", "ui-example", "device-state"],
        ),
        create_resource_example(
            client,
            "Device MONAD-02",
            resource_status_ids["Waiting"],
            "WAITING_POLICY",
            ["fleet", "ui-example", "device-state"],
        ),
        create_resource_example(
            client,
            "Device MONAD-03",
            resource_status_ids["Maintenance mode"],
            "PREPARE_REJECTED",
            ["fleet", "ui-example", "device-state"],
        ),
    ]

    created_experiments = [
        create_experiment_example(
            client,
            "Fleet UI Example - Queued",
            experiment_status_ids["Queued"],
            ["fleet", "ui-example", "experiment-state"],
        ),
        create_experiment_example(
            client,
            "Fleet UI Example - Running",
            experiment_status_ids["Running"],
            ["fleet", "ui-example", "experiment-state"],
        ),
        create_experiment_example(
            client,
            "Fleet UI Example - Failed",
            experiment_status_ids["Failed"],
            ["fleet", "ui-example", "experiment-state"],
        ),
    ]

    print("Created resources:", created_resources)
    print("Created experiments:", created_experiments)
    print("Check on localhost:")
    print("  https://localhost:8443/database.php?tags%5B%5D=ui-example&scope=3")
    print("  https://localhost:8443/experiments.php?tags%5B%5D=ui-example")


if __name__ == "__main__":
    main()
