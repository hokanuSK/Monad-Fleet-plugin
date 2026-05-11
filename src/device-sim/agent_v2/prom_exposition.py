"""Prometheus exposition endpoint for the device agent.

Path (per the architecture decision in the 5 GHz + BLE + power measurement plan):

    agent /metrics  -->  Pi-side Prometheus (operator-deployed)
                          --remote_write-->  Mimir on EC2  -->  Grafana

The endpoint serves metrics in the standard Prometheus exposition format on
``PROM_EXPOSITION_BIND_HOST:PROM_EXPOSITION_PORT`` (default
``127.0.0.1:9110``). Per-Pi disambiguation labels (``agent_id``, ``pi_id``,
``site``, etc.) are intentionally **not** baked into metrics here -- they are
applied at scrape time via the Pi-side Prometheus's ``external_labels`` /
``relabel_configs``. This keeps cardinality predictable and stops one agent
from impersonating another by setting its own labels.

The legacy ``_ingest_metrics_http`` JSON push (sinks._ingest_metrics_http -->
fleet-service ``/ingest/v1/metrics``) is **not** replaced by this module; it
stays only for sub-second mid-command sample bursts (``WIFI_SAMPLE_EMIT_HTTP=1``)
where the Prometheus scrape cadence is too coarse.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        start_http_server,
    )

    _PROM_AVAILABLE = True
except Exception:  # pragma: no cover - prometheus_client is optional at runtime
    CollectorRegistry = None  # type: ignore[assignment,misc]
    Counter = None  # type: ignore[assignment,misc]
    Gauge = None  # type: ignore[assignment,misc]
    start_http_server = None  # type: ignore[assignment]
    _PROM_AVAILABLE = False


log = logging.getLogger(__name__)


# Module-level state, populated by start_exposition_server() the first time it runs.
_REGISTRY: Optional["CollectorRegistry"] = None
_SERVER_STARTED: bool = False
_BOUND_HOST: str = ""
_BOUND_PORT: int = 0


# Power telemetry gauges. Other modules import these and call .set(value) when
# a sample is read; updates between scrapes are coalesced by Prometheus.
power_voltage_volts = None
power_under_voltage = None
power_throttled_state = None
cpu_temp_celsius = None
pmic_3v3_sys_current_amps = None
pmic_3v3_sys_voltage_volts = None
wifi5g_current_channel = None
wifi5g_dwell_changes_total = None
wifi5g_capture_active = None
ble_tx_total = None
ble_advertise_active = None


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        log.warning("Invalid integer for %s=%r; falling back to %s", name, raw, default)
        return default


def _build_metrics(registry: "CollectorRegistry") -> None:
    """Declare the initial set of gauges on the registry.

    Called once on first server start. Stored in module globals so the agent's
    measurement code (e.g. ``core._collect_device_status_metrics``) can reach
    them without re-importing or re-registering.
    """
    global power_voltage_volts, power_under_voltage
    global power_throttled_state, cpu_temp_celsius
    global pmic_3v3_sys_current_amps, pmic_3v3_sys_voltage_volts

    power_voltage_volts = Gauge(
        "monad_pi_voltage_volts",
        "Pi core supply voltage as reported by `vcgencmd measure_volts core`.",
        registry=registry,
    )
    power_under_voltage = Gauge(
        "monad_pi_under_voltage",
        "1 when `vcgencmd get_throttled` reports an under-voltage condition right now, else 0.",
        registry=registry,
    )
    power_throttled_state = Gauge(
        "monad_pi_throttled_state",
        "Raw integer returned by `vcgencmd get_throttled` (bitfield).",
        registry=registry,
    )
    cpu_temp_celsius = Gauge(
        "monad_pi_cpu_temp_celsius",
        "Pi CPU temperature in degrees Celsius (sourced from /sys/class/thermal).",
        registry=registry,
    )
    pmic_3v3_sys_current_amps = Gauge(
        "monad_pi_pmic_3v3_sys_current_amps",
        "Pi 5 PMIC 3V3_SYS rail current in amps. On the official RPi M.2 HAT+ "
        "this rail feeds the M.2 slot directly, so AX210 power can be approximated "
        "by subtracting the Pi-only baseline (typically 20-40 mA, characterized once "
        "with the AX210 disabled). Pi 5 only; gauge stays at 0 on older hardware.",
        registry=registry,
    )
    pmic_3v3_sys_voltage_volts = Gauge(
        "monad_pi_pmic_3v3_sys_voltage_volts",
        "Pi 5 PMIC 3V3_SYS rail voltage in volts (typically ~3.30 V).",
        registry=registry,
    )

    global wifi5g_current_channel, wifi5g_dwell_changes_total, wifi5g_capture_active
    wifi5g_current_channel = Gauge(
        "monad_pi_wifi5g_current_channel",
        "Channel number the 5 GHz monitor-mode capture is dwelling on right now. "
        "0 when no capture is active.",
        registry=registry,
    )
    wifi5g_dwell_changes_total = Counter(
        "monad_pi_wifi5g_dwell_changes_total",
        "Cumulative count of successful channel-hop transitions during 5 GHz "
        "monitor-mode capture. A flat curve while WIFI_SCAN is running means the "
        "hop thread is stuck or the iface refused channel changes.",
        registry=registry,
    )
    wifi5g_capture_active = Gauge(
        "monad_pi_wifi5g_capture_active",
        "1 when tcpdump-backed monitor-mode capture is running, else 0. Useful for "
        "annotating Grafana panels with capture windows.",
        registry=registry,
    )

    global ble_tx_total, ble_advertise_active
    ble_tx_total = Counter(
        "monad_pi_ble_tx_total",
        "Cumulative count of BLE advertisement payload-refresh events. The Pi "
        "broadcasts on its hardware advertising interval but only refreshes the "
        "adv_id payload every BLE_ADV_UPDATE_INTERVAL_S seconds; this counter "
        "ticks once per refresh, not once per radio broadcast. A flat curve "
        "during BLE_SCAN_MODE=advertise means the bluetoothctl pipeline is "
        "stuck.",
        registry=registry,
    )
    ble_advertise_active = Gauge(
        "monad_pi_ble_advertise_active",
        "1 while the BLE advertise runner is broadcasting, else 0. Useful for "
        "annotating Grafana panels with advertise windows.",
        registry=registry,
    )


def start_exposition_server() -> bool:
    """Start the agent Prometheus exposition listener.

    Reads ``PROM_EXPOSITION_ENABLED`` / ``PROM_EXPOSITION_BIND_HOST`` /
    ``PROM_EXPOSITION_PORT`` from the environment. Returns True if a listener
    is running by the time this returns, False otherwise (disabled,
    prometheus_client missing, bind failure).

    Idempotent: subsequent calls are no-ops and just return the current state.
    """
    global _REGISTRY, _SERVER_STARTED, _BOUND_HOST, _BOUND_PORT

    if _SERVER_STARTED:
        return True

    if not _bool_env("PROM_EXPOSITION_ENABLED", True):
        log.info("Prometheus exposition disabled via PROM_EXPOSITION_ENABLED=false")
        return False

    if not _PROM_AVAILABLE:
        log.warning(
            "prometheus_client is not installed; agent /metrics endpoint will not be served. "
            "Install it via `pip install prometheus-client` to enable scrape-based metrics."
        )
        return False

    bind_host = (os.environ.get("PROM_EXPOSITION_BIND_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = _int_env("PROM_EXPOSITION_PORT", 9110)

    _REGISTRY = CollectorRegistry()
    _build_metrics(_REGISTRY)

    try:
        start_http_server(port, addr=bind_host, registry=_REGISTRY)
    except OSError as exc:
        log.warning(
            "Could not bind Prometheus exposition listener at %s:%s (%s). "
            "Agent will continue; the new measurement metrics will not reach Mimir.",
            bind_host,
            port,
            exc,
        )
        _REGISTRY = None
        return False

    _SERVER_STARTED = True
    _BOUND_HOST = bind_host
    _BOUND_PORT = port
    log.info("Prometheus exposition listening on http://%s:%s/metrics", bind_host, port)
    return True


def get_registry() -> Optional["CollectorRegistry"]:
    """Return the active registry, or None if the listener never started."""
    return _REGISTRY


def is_running() -> bool:
    return _SERVER_STARTED


def bound_endpoint() -> tuple[str, int]:
    return _BOUND_HOST, _BOUND_PORT
