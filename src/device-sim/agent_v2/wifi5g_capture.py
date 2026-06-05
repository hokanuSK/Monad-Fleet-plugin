"""5 GHz monitor-mode + channel-hopping capture for the agent's WIFI_SCAN command.

Used when the policy sets ``WIFI_SCAN_MODE=passive_monitor``. Creates a
transient monitor-mode virtual interface (e.g. ``wlan0mon``) on top of the
named parent interface (e.g. ``wlan0`` / AX210), leaving the parent in managed
mode so VPN / control-plane connectivity is preserved. Runs ``tcpdump`` on the
monitor vif to write a pcap of every observed 802.11 frame, and channel-hops
through a configurable list of 5 GHz channels on a background thread.

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
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# sbin paths are often absent from non-root PATH on Debian/Raspberry Pi OS.
_SBIN_SEARCH = ("/usr/sbin", "/sbin")

# On Ubuntu the agent runs as a non-root user and iw/ip need CAP_NET_ADMIN.
# Prepend "sudo -n" so that a NOPASSWD sudoers rule covers it.
# On Pi OS the agent typically runs as root or has NOPASSWD for all commands,
# so we skip the prefix when already root to avoid double-sudo overhead.
_SUDO_PREFIX: list[str] = [] if os.geteuid() == 0 else ["sudo", "-n"]


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
    parent_iface: str       # physical interface left in managed mode (e.g. wlan0)
    iface: str              # transient monitor vif used for capture (e.g. wlan0mon)
    pcap_path: str
    schedule_log_path: str
    tcpdump_proc: subprocess.Popen
    stop_event: threading.Event
    hop_thread: threading.Thread
    started_monotonic_ns: int = 0
    dwell_entries: list[dict] = field(default_factory=list)


def _list_pcap_segments(base_path: str) -> list[Path]:
    """Return the ordered set of pcap segment files produced for ``base_path``.

    Without rotation, tcpdump writes exactly ``base_path``.
    With ``-C`` rotation enabled, tcpdump keeps the base file and appends
    numeric suffixes (for example ``.pcap1``, ``.pcap2``). We keep ordering
    deterministic so callers can publish artifacts with stable part numbers.
    """
    base = Path(base_path)
    parent = base.parent
    prefix = base.name
    rows: list[tuple[int, Path]] = []
    try:
        for child in parent.iterdir():
            if not child.is_file():
                continue
            if child.name == prefix:
                rows.append((0, child))
                continue
            if not child.name.startswith(prefix):
                continue
            suffix = child.name[len(prefix):]
            if not suffix.isdigit():
                continue
            rows.append((int(suffix), child))
    except FileNotFoundError:
        return []
    rows.sort(key=lambda item: item[0])
    return [path for _, path in rows]


def _find_tool(name: str) -> Optional[str]:
    """Locate ``name`` on PATH and in common sbin directories."""
    found = shutil.which(name)
    if found:
        return found
    for d in _SBIN_SEARCH:
        candidate = Path(d) / name
        if candidate.exists():
            return str(candidate)
    return None


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


def _iface_driver(iface: str) -> str:
    """Return the kernel driver name for ``iface`` (empty string on failure)."""
    try:
        import subprocess as _sp
        out = _sp.check_output(["ethtool", "-i", iface], text=True, timeout=3,
                               stderr=_sp.DEVNULL)
        for line in out.splitlines():
            if line.startswith("driver:"):
                return line.split(":", 1)[1].strip().lower()
    except Exception:
        pass
    return ""


def _iface_is_associated(iface: str) -> bool:
    """Return True if ``iface`` is currently associated with an AP.

    Used to prevent type-change monitor mode on management interfaces (e.g.
    monad-02 where the AX210 is wlan0 and also carries WireGuard).  For those
    we fall back to VIF-based monitor which preserves the STA association.
    """
    iw = _find_tool("iw")
    if not iw:
        return False
    try:
        rc, out, _ = _run([iw, "dev", iface, "link"])
        return rc == 0 and ("Connected" in out or "SSID" in out)
    except Exception:
        return False


def create_monitor_vif(parent_iface: str, mon_iface: Optional[str] = None) -> tuple[bool, str, str]:
    """Create a monitor-mode capture interface off ``parent_iface``.

    For most drivers (brcmfmac, ath9k, …) this creates a transient VIF
    (``<parent>mon``) that leaves ``parent_iface`` in managed mode so the
    control plane stays up.

    Intel AX210 / iwlwifi requires a direct type-change (``iw dev set type
    monitor``) because the VIF approach creates the interface but the hardware
    never passes frames to userspace.  In that case ``parent_iface`` is brought
    down briefly, its type flipped to ``monitor``, and the function returns
    ``mon_iface == parent_iface`` as the signal to :func:`delete_monitor_vif`
    that a type-restore is needed rather than a VIF deletion.

    Returns ``(ok, capture_iface, message)``.
    """
    iw = _find_tool("iw")
    if not iw:
        return False, "", "iw not found on PATH or in /sbin:/usr/sbin"
    mon = mon_iface or (parent_iface + "mon")

    # Clean up any stale vif from a previous interrupted run.
    _run(_SUDO_PREFIX + ["ip", "link", "set", mon, "down"])
    _run(_SUDO_PREFIX + [iw, "dev", mon, "del"])

    # iwlwifi (Intel AX210): VIF-based monitor creates the interface but the
    # hardware silently produces empty pcaps.  Use direct type-change instead.
    # Exception: if the interface is already associated with an AP (e.g. monad-02
    # where wlan0 is both AX210 and management/WireGuard), taking it down would
    # drop the VPN.  In that case use VIF approach; it will capture on the AP's
    # current channel without disrupting connectivity.
    use_type_change = (
        _iface_driver(parent_iface) == "iwlwifi"
        and not _iface_is_associated(parent_iface)
    )

    if not use_type_change:
        rc, _, err = _run(_SUDO_PREFIX + [iw, "dev", parent_iface, "interface", "add", mon, "type", "monitor"])
        if rc == 0:
            rc2, _, err2 = _run(_SUDO_PREFIX + ["ip", "link", "set", mon, "up"])
            if rc2 == 0:
                _run(_SUDO_PREFIX + [iw, "dev", mon, "set", "monitor", "otherbss", "fcsfail"])
                return True, mon, "monitor vif created"
            _run(_SUDO_PREFIX + [iw, "dev", mon, "del"])
        # VIF creation failed; fall through to type-change.
        use_type_change = True

    # Direct type-change: parent_iface goes down briefly, type set to monitor.
    _run(_SUDO_PREFIX + ["ip", "link", "set", parent_iface, "down"])
    rc, _, err = _run(_SUDO_PREFIX + [iw, "dev", parent_iface, "set", "type", "monitor"])
    if rc != 0:
        _run(_SUDO_PREFIX + ["ip", "link", "set", parent_iface, "up"])
        return False, parent_iface, f"set type monitor failed rc={rc} err={err.strip()[:200]}"
    _run(_SUDO_PREFIX + ["ip", "link", "set", parent_iface, "up"])
    _run(_SUDO_PREFIX + [iw, "dev", parent_iface, "set", "monitor", "otherbss", "fcsfail"])
    # Return parent_iface as capture iface; caller detects mon==parent as type-change mode.
    return True, parent_iface, "monitor mode via type change (iwlwifi)"


def delete_monitor_vif(mon_iface: str, parent_iface: str = "") -> None:
    """Remove the monitor interface created by :func:`create_monitor_vif`.

    When ``parent_iface`` is given and equals ``mon_iface`` (type-change mode),
    restores the interface to managed mode instead of deleting a VIF.
    """
    iw = _find_tool("iw") or "iw"
    if parent_iface and mon_iface == parent_iface:
        # Restore parent from monitor back to managed.
        _run(_SUDO_PREFIX + ["ip", "link", "set", mon_iface, "down"])
        _run(_SUDO_PREFIX + [iw, "dev", mon_iface, "set", "type", "managed"])
        _run(_SUDO_PREFIX + ["ip", "link", "set", mon_iface, "up"])
    else:
        _run(_SUDO_PREFIX + ["ip", "link", "set", mon_iface, "down"])
        _run(_SUDO_PREFIX + [iw, "dev", mon_iface, "del"])


def set_channel(iface: str, channel: int, width_mhz: int = DEFAULT_CHANNEL_WIDTH_MHZ) -> tuple[bool, str]:
    """Tune monitor ``iface`` to ``channel``. ``width_mhz`` supports 20 / 40 (HT40+).

    Returns ``(ok, message)``. Failures most commonly indicate a DFS channel
    in a regdomain that doesn't allow passive-monitor on that band, or that
    the iface isn't in monitor mode (callers should call
    :func:`create_monitor_vif` first).
    """
    iw = _find_tool("iw") or "iw"
    cmd = _SUDO_PREFIX + [iw, "dev", iface, "set", "channel", str(channel)]
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
    rotate_mb: int = 0,
) -> Optional[CaptureHandles]:
    """Begin a 5 GHz monitor-mode capture.

    Creates a transient monitor virtual interface (``<iface>mon``) off the
    named parent interface so the parent stays in managed mode (preserving VPN
    / control-plane connectivity). Spawns ``tcpdump`` on the monitor vif and
    starts a background channel-hopping thread. Returns a
    :class:`CaptureHandles` handle that the caller passes to
    :func:`stop_capture` when the measure window ends.

    Returns ``None`` if any prerequisite step fails (missing tools, vif
    creation failure, tcpdump couldn't start). Callers should treat that as a
    soft error rather than aborting the whole run.
    """
    iw = _find_tool("iw")
    if iw is None:
        log.warning("wifi5g_capture: 'iw' not found on PATH or in /sbin:/usr/sbin; skipping monitor capture")
        return None
    tcpdump = _find_tool("tcpdump")
    if tcpdump is None:
        log.warning("wifi5g_capture: 'tcpdump' not found; skipping monitor capture")
        return None

    ok, mon_iface, msg = create_monitor_vif(iface)
    if not ok:
        log.warning("wifi5g_capture: could not create monitor vif on %s: %s", iface, msg)
        return None

    ch_list = list(channels) if channels else list(DEFAULT_5GHZ_CHANNELS)
    if not ch_list:
        log.warning("wifi5g_capture: empty channel list; aborting")
        delete_monitor_vif(mon_iface, parent_iface=iface)
        return None

    # Park on the first channel so tcpdump starts seeing frames immediately.
    set_channel(mon_iface, ch_list[0], width_mhz)

    # tcpdump writes pcap directly; -U flushes per packet so partial files are
    # valid if we get killed. -s 256 captures management+control headers and a
    # bit of data payload without exploding pcap size.
    # On Ubuntu (non-root) tcpdump needs CAP_NET_RAW; use sudo -n same as iw/ip.
    cmd = _SUDO_PREFIX + [
        tcpdump,
        "-i", mon_iface,
        "-w", pcap_path,
        "-U",
        "-s", "256",
        "-n",
    ]
    if int(rotate_mb or 0) > 0:
        cmd.extend(["-C", str(int(rotate_mb))])
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        log.warning("wifi5g_capture: tcpdump spawn failed: %s", exc)
        delete_monitor_vif(mon_iface, parent_iface=iface)
        return None

    # Give tcpdump a moment to bind to the iface; if it dies immediately, bail.
    time.sleep(0.5)
    if proc.poll() is not None:
        err = proc.stderr.read() if proc.stderr else ""
        log.warning("wifi5g_capture: tcpdump exited rc=%s err=%s", proc.returncode, err.strip()[:200])
        delete_monitor_vif(mon_iface, parent_iface=iface)
        return None

    handles = CaptureHandles(
        parent_iface=iface,
        iface=mon_iface,
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
        "wifi5g_capture: started parent=%s monitor=%s pcap=%s channels=%s dwell_s=%.2f",
        iface,
        mon_iface,
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

    delete_monitor_vif(handles.iface, parent_iface=handles.parent_iface)

    # Flush the dwell schedule to disk as JSONL.
    try:
        with open(handles.schedule_log_path, "w", encoding="utf-8") as fh:
            for entry in handles.dwell_entries:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        log.exception("wifi5g_capture: failed to write schedule log to %s", handles.schedule_log_path)

    pcap_paths = _list_pcap_segments(handles.pcap_path)
    pcap_bytes = 0
    for path in pcap_paths:
        try:
            pcap_bytes += max(0, int(path.stat().st_size))
        except Exception:
            continue

    elapsed_s = max(0.0, (time.monotonic_ns() - handles.started_monotonic_ns) / 1_000_000_000)
    return {
        "pcap_bytes": pcap_bytes,
        "pcap_segment_count": len(pcap_paths),
        "pcap_segment_paths": [str(path) for path in pcap_paths],
        "dwell_changes": len(handles.dwell_entries),
        "elapsed_s": elapsed_s,
    }
