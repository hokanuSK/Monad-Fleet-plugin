"""BLE advertise mode for the agent's BLE_SCAN command (Scenario B).

Activated when the policy sets ``BLE_SCAN_MODE=advertise``. Drives BlueZ via
``bluetoothctl`` in a piped subprocess to broadcast LE advertisements whose
local name carries a monotonically-increasing ``adv_id``. The Pi keeps a
local log of every ``adv_id`` it published (with monotonic + wallclock
timestamps); the phone-side receiver records the same ``adv_id`` plus rx
timestamps and RSSI, and offline analysis joins on ``adv_id`` to extract
time-of-flight / RSSI deltas.

Why ``bluetoothctl`` and not direct D-Bus calls: D-Bus would be the canonical
path, but BlueZ's ``org.bluez.LEAdvertisement1`` interface requires a
GLib main loop or async D-Bus client. Adding that dependency to the agent
just for this one feature isn't worth it on first pass; ``bluetoothctl`` is
already installed on monad-03 (BlueZ 5.72) and accepts piped stdin commands.

Why we re-set the advertisement *name* instead of issuing a new advertisement
per packet: the BLE chip broadcasts at the hardware advertising interval
(every 50-1000 ms typically). We can't write a new ``adv_id`` on every
hardware broadcast -- ``bluetoothctl`` is too slow. Instead, we set a fresh
``name`` payload every ``BLE_ADV_UPDATE_INTERVAL_S`` seconds; receivers see
the same ``adv_id`` repeated for one update interval (giving them multiple
RSSI samples per adv_id), then a new ``adv_id`` on the next interval.

Requires root (or membership in the ``bluetooth`` group) and a working
``hci0`` adapter. Soft-fails (returns ``None``) if any prerequisite is
missing so callers can continue the run without advertising.
"""
from __future__ import annotations

import json
import logging
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)


DEFAULT_UPDATE_INTERVAL_S: float = 2.0
DEFAULT_PAYLOAD_PREFIX: str = "monad-pi"


@dataclass
class AdvertiseHandles:
    """Bag of state for an in-flight BLE advertise run."""
    log_path: str
    btctl_proc: subprocess.Popen
    stop_event: threading.Event
    runner_thread: threading.Thread
    started_monotonic_ns: int = 0
    events: list[dict] = field(default_factory=list)


def _send(proc: subprocess.Popen, line: str) -> None:
    """Push a single command line into ``bluetoothctl``'s stdin."""
    if proc.stdin is None:
        return
    try:
        proc.stdin.write((line + "\n").encode("utf-8"))
        proc.stdin.flush()
    except Exception:
        log.debug("ble_advertise: stdin write failed for line=%r", line, exc_info=True)


def _setup_pipeline(proc: subprocess.Popen, payload_prefix: str) -> None:
    """Issue the once-per-run bluetoothctl bootstrap sequence."""
    _send(proc, "power on")
    _send(proc, "menu advertise")
    _send(proc, "clear")
    _send(proc, f"name {payload_prefix}-init")
    _send(proc, "back")
    # Give BlueZ a beat to apply the menu changes before flipping advertise on.
    time.sleep(0.4)
    _send(proc, "advertise on")


def _teardown_pipeline(proc: subprocess.Popen) -> None:
    _send(proc, "advertise off")
    time.sleep(0.2)
    _send(proc, "quit")


def _update_name(proc: subprocess.Popen, name: str) -> None:
    """Rewrite the advertised local name; the chip will pick the new payload
    up on its next broadcast slot."""
    _send(proc, "menu advertise")
    _send(proc, f"name {name}")
    _send(proc, "back")


def _runner_loop(
    handles: AdvertiseHandles,
    duration_s: float,
    update_interval_s: float,
    payload_prefix: str,
) -> None:
    """Background thread body: roll the ``adv_id`` payload every interval."""
    # Lazy import so a missing prometheus_client doesn't crash the thread.
    try:
        from . import prom_exposition as _prom
    except Exception:
        _prom = None  # type: ignore[assignment]

    deadline_ns = handles.started_monotonic_ns + int(duration_s * 1_000_000_000)
    seq = 0
    while not handles.stop_event.is_set() and time.monotonic_ns() < deadline_ns:
        seq += 1
        adv_id = f"{payload_prefix}-{seq:08x}"
        send_ts_us = int(time.time() * 1_000_000)
        _update_name(handles.btctl_proc, adv_id)
        handles.events.append(
            {
                "seq": seq,
                "adv_id": adv_id,
                "send_ts_us": send_ts_us,
                "monotonic_us": int(time.monotonic_ns() / 1000),
            }
        )

        if _prom is not None:
            try:
                if getattr(_prom, "ble_tx_total", None) is not None:
                    _prom.ble_tx_total.inc()
            except Exception:
                pass

        # Sleep in small slices so stop_event can interrupt promptly.
        slice_deadline_ns = time.monotonic_ns() + int(update_interval_s * 1_000_000_000)
        while time.monotonic_ns() < slice_deadline_ns and not handles.stop_event.is_set():
            time.sleep(min(0.1, update_interval_s))


def start_advertise(
    *,
    log_path: str,
    duration_s: float,
    update_interval_s: float = DEFAULT_UPDATE_INTERVAL_S,
    payload_prefix: str = DEFAULT_PAYLOAD_PREFIX,
) -> Optional[AdvertiseHandles]:
    """Begin a BLE advertise run.

    Returns ``None`` if ``bluetoothctl`` isn't available or fails to spawn;
    callers should treat that as a soft error.
    """
    if shutil.which("bluetoothctl") is None:
        log.warning("ble_advertise: 'bluetoothctl' not on PATH; skipping advertise")
        return None

    try:
        proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        log.warning("ble_advertise: spawn failed: %s", exc)
        return None

    # Give bluetoothctl a beat to print its banner and accept stdin.
    time.sleep(0.3)
    if proc.poll() is not None:
        log.warning("ble_advertise: bluetoothctl exited immediately rc=%s", proc.returncode)
        return None

    _setup_pipeline(proc, payload_prefix)
    # Longer wait so BlueZ has time to enable the advertisement before we verify.
    time.sleep(1.0)
    if proc.poll() is not None:
        log.warning(
            "ble_advertise: bluetoothctl exited during setup rc=%s",
            proc.returncode,
        )
        return None

    # Verify advertising is active via btmgmt before we start counting tx.
    # Retry once after a short delay to allow BlueZ to propagate the change.
    _btmgmt = shutil.which("btmgmt")
    if _btmgmt:
        _advertising_confirmed = False
        for _attempt in range(2):
            try:
                _info = subprocess.run(
                    [_btmgmt, "info"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                _in_current = False
                for _line in (_info.stdout + _info.stderr).splitlines():
                    if "current settings" in _line.lower():
                        _in_current = True
                    if _in_current and "advertising" in _line.lower():
                        _advertising_confirmed = True
                        break
                if _advertising_confirmed:
                    break
            except Exception:
                log.debug("ble_advertise: btmgmt info check failed", exc_info=True)
                break
            if _attempt == 0:
                time.sleep(1.0)
        if not _advertising_confirmed:
            log.warning(
                "ble_advertise: btmgmt info current settings do not include 'advertising'; "
                "advertise on failed — aborting to avoid false tx counts"
            )
            try:
                _teardown_pipeline(proc)
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except Exception:
                    pass
            return None
        log.info("ble_advertise: advertising confirmed via btmgmt info")

    handles = AdvertiseHandles(
        log_path=log_path,
        btctl_proc=proc,
        stop_event=threading.Event(),
        runner_thread=None,  # type: ignore[arg-type]
        started_monotonic_ns=time.monotonic_ns(),
    )

    try:
        from . import prom_exposition as _prom
        if getattr(_prom, "ble_advertise_active", None) is not None:
            _prom.ble_advertise_active.set(1.0)
    except Exception:
        pass

    runner = threading.Thread(
        target=_runner_loop,
        args=(handles, duration_s, update_interval_s, payload_prefix),
        name="ble-advertise",
        daemon=True,
    )
    handles.runner_thread = runner
    runner.start()
    log.info(
        "ble_advertise: started log=%s duration_s=%.1f update_interval_s=%.2f prefix=%s",
        log_path,
        duration_s,
        update_interval_s,
        payload_prefix,
    )
    return handles


def stop_advertise(handles: AdvertiseHandles) -> dict:
    """Clean shutdown: stop the runner, turn advertising off, persist log.

    Returns ``{tx_count, elapsed_s, log_path}`` for inclusion in
    ``event_metrics``.
    """
    handles.stop_event.set()
    handles.runner_thread.join(timeout=2.0)

    try:
        _teardown_pipeline(handles.btctl_proc)
        handles.btctl_proc.wait(timeout=3.0)
    except Exception:
        try:
            handles.btctl_proc.send_signal(signal.SIGTERM)
            handles.btctl_proc.wait(timeout=2.0)
        except Exception:
            try:
                handles.btctl_proc.kill()
                handles.btctl_proc.wait(timeout=1.0)
            except Exception:
                pass

    try:
        from . import prom_exposition as _prom
        if getattr(_prom, "ble_advertise_active", None) is not None:
            _prom.ble_advertise_active.set(0.0)
    except Exception:
        pass

    try:
        with open(handles.log_path, "w", encoding="utf-8") as fh:
            for entry in handles.events:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        log.exception("ble_advertise: failed to write log to %s", handles.log_path)

    elapsed_s = max(0.0, (time.monotonic_ns() - handles.started_monotonic_ns) / 1_000_000_000)
    return {
        "tx_count": len(handles.events),
        "elapsed_s": elapsed_s,
        "log_path": handles.log_path,
    }
