"""5 GHz monitor-mode + channel-hopping capture for the agent's WIFI_SCAN command.

Used when the policy sets ``WIFI_SCAN_MODE=passive_monitor``. Flips a named
Wi-Fi interface (typically the Pi 5's M.2 AX210 on ``wlp1s0``, separate from
the Broadcom control plane on ``wlan0``) into monitor mode, runs ``tcpdump``
to write a pcap of every observed 802.11 frame, and channel-hops through a
configurable list of 5 GHz channels on a background thread.

Outputs land via the agent's existing RunStore:
 - ``wifi-5g-monitor.pcap`` -- raw frame capture; uploaded as an individual
   eLab artifact because of its non-bundled extension.
 - ``wifi-5g-channel-schedule.log`` -- JSONL with ``(channel, dwell_start_us,
   dwell_end_us, set_ok)`` rows; bundled into the wireless-evidence tarball.

Prometheus gauges (declared in :mod:`prom_exposition`) get updated by the
hopping thread so the live Grafana view shows current channel + a counter of
dwell changes. Per-channel frames/sec aggregations are intentionally deferred
to a later phase that pipes the live pcap through a parser; right now this
module just gets bytes onto disk and tags them with the dwell schedule.

Requires root (or CAP_NET_ADMIN) on the agent process to manipulate iface
state, plus ``iw`` and ``tcpdump`` installed on the Pi.
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


# US/EU UNII-1/2/3 channels that most chips can monitor without special
# regdomain tricks. Operators can override per policy via WIFI_SCAN_CHANNELS.
DEFAULT_5GHZ_CHANNELS: tuple[int, ...] = (
    36, 40, 44, 48,            # UNII-1
    52, 56, 60, 64,            # UNII-2A (DFS in most regdomains)
    100, 104, 108, 112,        # UNII-2C / UNII-2 extended (DFS)
    116, 120, 124, 128,
    132, 136, 140,
    149, 153, 157, 161, 165,   # UNII-3
)

DEFAULT_DWELL_S: float = 0.5
DEFAULT_CHANNEL_WIDTH_MHZ: int = 20


@dataclass
class CaptureHandles:
    """Bag of state returned by :func:`start_capture`; pass to :func:`stop_capture`."""
    iface: str
    pcap_path: str
    schedule_log_path: str
    tcpdump_proc: subprocess.Popen
    stop_event: threading.Event
    hop_thread: threading.Thread
    started_monotonic_ns: int = 0
    dwell_entries: list[dict] = field(default_factory=list)


def _run(cmd: list[str], timeout: float = 4.0) -> tuple[int, str, str]:
    """Run a command. Returns ``(rc, stdout, stderr)``; -1 on exception."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError as exc:
        return -1, "", f"executable not found: {exc}"
    except subprocess.TimeoutExpired as exc:
        return -1, "", f"timeout after {exc.timeout}s"
    except Exception as exc:  # pragma: no cover - defensive
        return -1, "", str(exc)


def parse_channel_list(raw: str) -> list[int]:
    """Parse a CSV of channel numbers. Empty / whitespace input returns the default 5 GHz list."""
    if not raw or not raw.strip():
        return list(DEFAULT_5GHZ_CHANNELS)
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out or list(DEFAULT_5GHZ_CHANNELS)


def set_monitor_mode(iface: str) -> tuple[bool, str]:
    """Switch ``iface`` into monitor mode.

    Sequence (kept simple to maximise compatibility across iwlwifi firmwares):
    ``ip link set down`` -> ``iw dev set type monitor`` -> ``ip link set up``.
    Requires root.
    """
    for cmd in (
        ["ip", "link", "set", iface, "down"],
        ["iw", "dev", iface, "set", "type", "monitor"],
        ["ip", "link", "set", iface, "up"],
    ):
        rc, _, err = _run(cmd)
        if rc != 0:
            return False, f'{" ".join(cmd)} failed rc={rc} err={err.strip()[:200]}'
    return True, "monitor mode set"


def restore_managed_mode(iface: str) -> tuple[bool, str]:
    """Best-effort: switch ``iface`` back to managed mode and bring it up.

    Errors are swallowed; the goal is to leave the Pi in a usable state even
    if some step fails (e.g. driver refuses managed mode while a userspace
    process still holds an open socket).
    """
    last_err = ""
    for cmd in (
        ["ip", "link", "set", iface, "down"],
        ["iw", "dev", iface, "set", "type", "managed"],
        ["ip", "link", "set", iface, "up"],
    ):
        rc, _, err = _run(cmd)
        if rc != 0:
            last_err = err.strip()[:200]
    return True, last_err or "restored"


def set_channel(iface: str, channel: int, width_mhz: int = DEFAULT_CHANNEL_WIDTH_MHZ) -> tuple[bool, str]:
    """Tune ``iface`` to ``channel``. ``width_mhz`` supports 20 / 40 (HT40+).

    Returns ``(ok, message)``. Failures most commonly indicate a DFS channel
    in a regdomain that doesn't allow passive-monitor on that band, or that
    the iface isn't in monitor mode (callers should call :func:`set_monitor_mode`
    first).
    """
    cmd = ["iw", "dev", iface, "set", "channel", str(channel)]
    if width_mhz == 40:
        cmd.append("HT40+")
    rc, _, err = _run(cmd, timeout=2.0)
    if rc != 0:
        return False, err.strip()[:200]
    return True, "ok"


def _channel_hop_loop(
    handles: CaptureHandles,
    channels: list[int],
    dwell_s: float,
    width_mhz: int,
) -> None:
    """Background thread body: cycle through channels until ``stop_event`` is set."""
    # Lazy import so a missing prometheus_client doesn't take this module down.
    try:
        from . import prom_exposition as _prom
    except Exception:  # pragma: no cover - defensive
        _prom = None  # type: ignore[assignment]

    idx = 0
    while not handles.stop_event.is_set():
        ch = channels[idx % len(channels)]
        ok, msg = set_channel(handles.iface, ch, width_mhz)
        start_us = int(time.monotonic_ns() / 1000)

        if _prom is not None:
            try:
                if getattr(_prom, "wifi5g_current_channel", None) is not None:
                    _prom.wifi5g_current_channel.set(ch)
                if getattr(_prom, "wifi5g_dwell_changes_total", None) is not None and ok:
                    _prom.wifi5g_dwell_changes_total.inc()
            except Exception:
                pass

        # Block in small slices so stop_event can interrupt promptly.
        deadline_ns = time.monotonic_ns() + int(dwell_s * 1_000_000_000)
        while time.monotonic_ns() < deadline_ns and not handles.stop_event.is_set():
            time.sleep(min(0.1, dwell_s))

        end_us = int(time.monotonic_ns() / 1000)
        handles.dwell_entries.append(
            {
                "channel": ch,
                "set_ok": ok,
                "set_msg": msg if not ok else "",
                "dwell_start_us": start_us,
                "dwell_end_us": end_us,
            }
        )
        idx += 1


def start_capture(
    *,
    iface: str,
    pcap_path: str,
    schedule_log_path: str,
    channels: Optional[list[int]] = None,
    dwell_s: float = DEFAULT_DWELL_S,
    width_mhz: int = DEFAULT_CHANNEL_WIDTH_MHZ,
) -> Optional[CaptureHandles]:
    """Begin a 5 GHz monitor-mode capture.

    Performs the iface state change to monitor mode, spawns ``tcpdump`` writing
    to ``pcap_path``, and starts a background channel-hopping thread. Returns a
    :class:`CaptureHandles` handle that the caller passes to :func:`stop_capture`
    when the measure window ends.

    Returns ``None`` if any prerequisite step fails (missing tools, ``iw`` set
    failure, ``tcpdump`` couldn't start). Callers should treat that as a soft
    error and continue with no capture rather than aborting the whole run.
    """
    if shutil.which("iw") is None:
        log.warning("wifi5g_capture: 'iw' not on PATH; skipping monitor capture")
        return None
    if shutil.which("tcpdump") is None:
        log.warning("wifi5g_capture: 'tcpdump' not on PATH; skipping monitor capture")
        return None

    ok, msg = set_monitor_mode(iface)
    if not ok:
        log.warning("wifi5g_capture: could not set monitor mode on %s: %s", iface, msg)
        return None

    ch_list = list(channels) if channels else list(DEFAULT_5GHZ_CHANNELS)
    if not ch_list:
        log.warning("wifi5g_capture: empty channel list; aborting")
        restore_managed_mode(iface)
        return None

    # Park on the first channel so tcpdump starts seeing frames immediately.
    set_channel(iface, ch_list[0], width_mhz)

    # tcpdump writes pcap directly; -U flushes per packet so partial files are
    # valid if we get killed. -s 256 captures management+control headers and a
    # bit of data payload without exploding pcap size.
    cmd = [
        "tcpdump",
        "-i", iface,
        "-w", pcap_path,
        "-U",
        "-s", "256",
        "-n",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        log.warning("wifi5g_capture: tcpdump spawn failed: %s", exc)
        restore_managed_mode(iface)
        return None

    # Give tcpdump a moment to bind to the iface; if it dies immediately, bail.
    time.sleep(0.5)
    if proc.poll() is not None:
        err = proc.stderr.read() if proc.stderr else ""
        log.warning("wifi5g_capture: tcpdump exited rc=%s err=%s", proc.returncode, err.strip()[:200])
        restore_managed_mode(iface)
        return None

    handles = CaptureHandles(
        iface=iface,
        pcap_path=pcap_path,
        schedule_log_path=schedule_log_path,
        tcpdump_proc=proc,
        stop_event=threading.Event(),
        hop_thread=None,  # type: ignore[arg-type]
        started_monotonic_ns=time.monotonic_ns(),
    )
    hop = threading.Thread(
        target=_channel_hop_loop,
        args=(handles, ch_list, dwell_s, width_mhz),
        name="wifi5g-hop",
        daemon=True,
    )
    handles.hop_thread = hop
    hop.start()
    log.info(
        "wifi5g_capture: started iface=%s pcap=%s channels=%s dwell_s=%.2f",
        iface,
        pcap_path,
        ch_list,
        dwell_s,
    )
    return handles


def stop_capture(handles: CaptureHandles) -> dict:
    """Clean shutdown: stop the hop thread, terminate tcpdump, restore managed mode.

    Returns a small dict of summary stats (``pcap_bytes``, ``dwell_changes``,
    ``elapsed_s``) suitable for inclusion in ``event_metrics``.
    """
    handles.stop_event.set()
    handles.hop_thread.join(timeout=2.0)

    # Terminate tcpdump gracefully so the pcap is flushed.
    try:
        handles.tcpdump_proc.send_signal(signal.SIGINT)
        handles.tcpdump_proc.wait(timeout=4.0)
    except Exception:
        try:
            handles.tcpdump_proc.kill()
            handles.tcpdump_proc.wait(timeout=2.0)
        except Exception:
            pass

    restore_managed_mode(handles.iface)

    # Flush the dwell schedule to disk as JSONL.
    try:
        with open(handles.schedule_log_path, "w", encoding="utf-8") as fh:
            for entry in handles.dwell_entries:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        log.exception("wifi5g_capture: failed to write schedule log to %s", handles.schedule_log_path)

    pcap_bytes = 0
    try:
        pcap_bytes = Path(handles.pcap_path).stat().st_size
    except Exception:
        pass

    elapsed_s = max(0.0, (time.monotonic_ns() - handles.started_monotonic_ns) / 1_000_000_000)
    return {
        "pcap_bytes": pcap_bytes,
        "dwell_changes": len(handles.dwell_entries),
        "elapsed_s": elapsed_s,
    }
