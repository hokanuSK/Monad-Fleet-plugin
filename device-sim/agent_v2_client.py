#!/usr/bin/env python3
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

import fleet_gateway_v2_pb2
import fleet_gateway_v2_pb2_grpc


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agent-v2")

# "Placeholder" cmdlines used in early smoke policies. For typed commands, we treat these as hints,
# not mandatory implementations.
_TRIVIAL_CMD_RE = re.compile(r"^\s*(?:echo\b|true\s*$|:\s*$)", re.IGNORECASE)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_ts() -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(now_utc())
    return ts


def ts_to_datetime(ts: Timestamp) -> datetime:
    return datetime.fromtimestamp(ts.seconds + ts.nanos / 1_000_000_000, tz=timezone.utc)


def ts_to_iso(ts: Timestamp) -> str:
    return ts_to_datetime(ts).isoformat().replace("+00:00", "Z")


def iso_to_ts(value: str) -> Timestamp:
    ts = Timestamp()
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    ts.FromDatetime(dt)
    return ts


def in_window(start: Timestamp, end: Timestamp) -> bool:
    if (start.seconds == 0 and start.nanos == 0) or (end.seconds == 0 and end.nanos == 0):
        return True
    now = now_utc()
    return ts_to_datetime(start) <= now <= ts_to_datetime(end)


def normalize(text: Any) -> str:
    if text is None:
        return ""
    return str(text).strip()


def sanitize_name(text: Any, default: str) -> str:
    value = normalize(text)
    if not value:
        value = default
    safe = re.sub(r"[^a-zA-Z0-9._-]", "-", value)
    return safe.strip("-") or default


def mode_from_env(raw: str) -> int:
    text = normalize(raw).upper()
    if text == "DUAL_NIC":
        return fleet_gateway_v2_pb2.DUAL_NIC
    return fleet_gateway_v2_pb2.RF_SHARING


def route_interface_for_target(target_host: str) -> str:
    if not target_host:
        return ""
    try:
        out = subprocess.check_output(["ip", "route", "get", target_host], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""
    match = re.search(r"\bdev\s+(\S+)", out)
    return match.group(1) if match else ""


def detect_interfaces() -> list[fleet_gateway_v2_pb2.NetworkInterface]:
    result: list[fleet_gateway_v2_pb2.NetworkInterface] = []
    try:
        out = subprocess.check_output(["ip", "-o", "link", "show"], text=True)
    except Exception:
        return result

    for line in out.splitlines():
        match = re.match(r"^\d+:\s+([^:]+):", line)
        if not match:
            continue
        name = match.group(1)
        if name == "lo":
            continue
        mac_match = re.search(r"link/\w+\s+([0-9a-f:]{17})", line.lower())
        mac = mac_match.group(1) if mac_match else ""
        kind = "wifi" if name.startswith("wl") else "ethernet" if name.startswith(("eth", "en")) else "unknown"
        result.append(
            fleet_gateway_v2_pb2.NetworkInterface(
                name=name,
                mac=mac,
                kind=kind,
                driver="",
                firmware="",
            )
        )
    return result


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _combined_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    out = _as_text(stdout).strip()
    err = _as_text(stderr).strip()
    if out and err:
        return out + "\n" + err
    return out or err


def _short_message(text: str, limit: int = 1000) -> str:
    msg = (text or "").strip()
    if len(msg) <= limit:
        return msg
    return msg[:limit] + "…"


def _is_trivial_cmdline(cmdline: str) -> bool:
    return bool(_TRIVIAL_CMD_RE.match(cmdline or ""))


def run_command_capture(
    cmdline: str,
    argv: list[str],
    env: dict[str, str],
    timeout_ms: int,
    execute_policy: bool,
) -> tuple[int, int, str, str]:
    if not execute_policy:
        time.sleep(min(1.0, max(0.1, timeout_ms / 1000.0)))
        simulated = f"simulated: {cmdline or ' '.join(argv) or '<no-op>'}"
        return 0, min(timeout_ms, 1000), simulated, simulated

    started = time.time()
    try:
        timeout_s = max(1, int(timeout_ms / 1000))
        child_env = dict(os.environ)
        child_env.update({normalize(k): normalize(v) for k, v in (env or {}).items() if normalize(k)})

        if argv:
            proc = subprocess.run(  # noqa: S603
                [normalize(a) for a in argv if normalize(a)],
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=child_env,
                check=False,
            )
        else:
            proc = subprocess.run(  # noqa: S602
                cmdline,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=child_env,
                check=False,
            )
        duration_ms = int((time.time() - started) * 1000)
        full = _combined_output(proc.stdout, proc.stderr)
        return int(proc.returncode), duration_ms, _short_message(full), full
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.time() - started) * 1000)
        # Best-effort: keep partial output if available.
        # TimeoutExpired may contain stdout/stderr when capture_output is used.
        full = _combined_output(getattr(exc, "stdout", None), getattr(exc, "stderr", None)) or "command timed out"
        return 124, duration_ms, "command timed out", full


def _run_capture_args(args: list[str], timeout_ms: int) -> tuple[int, int, str]:
    started = time.time()
    try:
        proc = subprocess.run(  # noqa: S603
            [normalize(a) for a in args if normalize(a)],
            shell=False,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_ms / 1000)),
            check=False,
        )
        duration_ms = int((time.time() - started) * 1000)
        return int(proc.returncode), duration_ms, _combined_output(proc.stdout, proc.stderr)
    except FileNotFoundError:
        duration_ms = int((time.time() - started) * 1000)
        return 127, duration_ms, f"missing command: {args[0] if args else '<unknown>'}"
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.time() - started) * 1000)
        out = _combined_output(getattr(exc, "stdout", None), getattr(exc, "stderr", None))
        return 124, duration_ms, out or "command timed out"


def resolve_executable(name: str, extra_candidates: list[str] | None = None) -> str | None:
    path = shutil.which(name)
    if path:
        return path
    for candidate in extra_candidates or []:
        try:
            p = Path(candidate)
            if p.exists() and os.access(str(p), os.X_OK):
                return str(p)
        except Exception:
            continue
    return None


def _parse_iw_scan(text: str) -> tuple[int, float | None]:
    bss_count = len(re.findall(r"^BSS\\s+[0-9a-f:]{17}", text, flags=re.IGNORECASE | re.MULTILINE))
    rssis: list[float] = []
    for m in re.finditer(r"\\bsignal:\\s*(-?\\d+(?:\\.\\d+)?)\\s*dBm\\b", text, flags=re.IGNORECASE):
        try:
            rssis.append(float(m.group(1)))
        except Exception:
            continue
    avg_rssi = (sum(rssis) / len(rssis)) if rssis else None
    return bss_count, avg_rssi


def _parse_iw_link(text: str) -> tuple[int, float | None]:
    connected = 1 if re.search(r"\\bConnected to\\b", text) else 0
    m = re.search(r"\\bsignal:\\s*(-?\\d+(?:\\.\\d+)?)\\s*dBm\\b", text, flags=re.IGNORECASE)
    avg_rssi = None
    if m:
        try:
            avg_rssi = float(m.group(1))
        except Exception:
            avg_rssi = None
    return connected, avg_rssi


def _parse_proc_net_wireless(text: str, iface: str) -> tuple[float | None, float | None]:
    # /proc/net/wireless columns:
    # iface: status link level noise ...
    # Example:
    # wlan0: 0000   47.  -63.  -256        0      0      0      0      0        0
    iface = normalize(iface)
    if not iface:
        return None, None
    for line in (text or "").splitlines():
        if not line.strip().startswith(iface + ":"):
            continue
        # Keep it permissive; values may be floats or ints.
        m = re.search(rf"^{re.escape(iface)}:\s+\S+\s+([0-9.]+)\s+(-?[0-9.]+)\s+(-?[0-9.]+)", line.strip())
        if not m:
            return None, None
        try:
            link = float(m.group(1))
        except Exception:
            link = None
        try:
            level = float(m.group(2))
        except Exception:
            level = None
        return link, level
    return None, None


def _parse_bluetoothctl_scan(text: str) -> tuple[int, float | None]:
    # Count unique device addresses observed in output; average RSSI when available.
    addrs = set(re.findall(r"\\b([0-9A-F]{2}(?::[0-9A-F]{2}){5})\\b", text, flags=re.IGNORECASE))
    rssis: list[float] = []
    for m in re.finditer(r"\\bRSSI:\\s*(-?\\d+(?:\\.\\d+)?)\\b", text, flags=re.IGNORECASE):
        try:
            rssis.append(float(m.group(1)))
        except Exception:
            continue
    avg_rssi = (sum(rssis) / len(rssis)) if rssis else None
    return len(addrs), avg_rssi


def event_type_for_exit(exit_code: int) -> int:
    return fleet_gateway_v2_pb2.COMMAND_FINISHED if exit_code == 0 else fleet_gateway_v2_pb2.COMMAND_FAILED


def command_type_name(command_type: int) -> str:
    mapping = {
        int(fleet_gateway_v2_pb2.SHELL): "SHELL",
        int(fleet_gateway_v2_pb2.CAPTURE_CSI): "CAPTURE_CSI",
        int(fleet_gateway_v2_pb2.BLE_SCAN): "BLE_SCAN",
        int(fleet_gateway_v2_pb2.WIFI_SCAN): "WIFI_SCAN",
    }
    return mapping.get(int(command_type), "COMMAND_TYPE_UNSPECIFIED")


def reached_max_cycles(cycles: int, max_cycles: int) -> bool:
    return max_cycles > 0 and cycles >= max_cycles


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact_from_path(name: str, path: Path, run_id: str) -> fleet_gateway_v2_pb2.ArtifactRef:
    spool_uri = f"spool://{sanitize_name(run_id, 'run')}/artifacts/{sanitize_name(name, 'artifact')}"
    return fleet_gateway_v2_pb2.ArtifactRef(
        name=name,
        uri=spool_uri,
        size_bytes=path.stat().st_size,
        sha256=file_sha256(path),
    )


def event_to_dict(event: fleet_gateway_v2_pb2.Event) -> dict[str, Any]:
    return {
        "event_id": normalize(event.event_id),
        "run_id": normalize(event.run_id),
        "experiment_id": normalize(event.experiment_id),
        "policy_id": normalize(event.policy_id),
        "agent_id": normalize(event.agent_id),
        "timestamp": ts_to_iso(event.timestamp),
        "type": int(event.type),
        "group_id": normalize(event.group_id),
        "command_id": normalize(event.command_id),
        "exit_code": int(event.exit_code),
        "duration_ms": int(event.duration_ms),
        "message": normalize(event.message),
        "metrics": {normalize(k): normalize(v) for k, v in event.metrics.items() if normalize(k)},
    }


def event_from_dict(payload: dict[str, Any]) -> fleet_gateway_v2_pb2.Event:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    return fleet_gateway_v2_pb2.Event(
        event_id=normalize(payload.get("event_id")),
        run_id=normalize(payload.get("run_id")),
        experiment_id=normalize(payload.get("experiment_id")),
        policy_id=normalize(payload.get("policy_id")),
        agent_id=normalize(payload.get("agent_id")),
        timestamp=iso_to_ts(normalize(payload.get("timestamp"))),
        type=int(payload.get("type", 0) or 0),
        group_id=normalize(payload.get("group_id")),
        command_id=normalize(payload.get("command_id")),
        exit_code=int(payload.get("exit_code", 0) or 0),
        duration_ms=max(0, int(payload.get("duration_ms", 0) or 0)),
        message=normalize(payload.get("message")),
        metrics={normalize(k): normalize(v) for k, v in metrics.items() if normalize(k)},
    )


def report_to_dict(report: fleet_gateway_v2_pb2.Report) -> dict[str, Any]:
    return {
        "run_id": normalize(report.run_id),
        "experiment_id": normalize(report.experiment_id),
        "policy_id": normalize(report.policy_id),
        "agent_id": normalize(report.agent_id),
        "started_at": ts_to_iso(report.started_at),
        "finished_at": ts_to_iso(report.finished_at),
        "status": normalize(report.status),
        "events": [event_to_dict(ev) for ev in report.events],
        "artifacts": [
            {
                "name": normalize(art.name),
                "uri": normalize(art.uri),
                "size_bytes": int(art.size_bytes),
                "sha256": normalize(art.sha256),
            }
            for art in report.artifacts
        ],
        "summary_metrics": {normalize(k): normalize(v) for k, v in report.summary_metrics.items() if normalize(k)},
    }


def report_from_dict(payload: dict[str, Any]) -> fleet_gateway_v2_pb2.Report:
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    summary_metrics = payload.get("summary_metrics") if isinstance(payload.get("summary_metrics"), dict) else {}
    return fleet_gateway_v2_pb2.Report(
        run_id=normalize(payload.get("run_id")),
        experiment_id=normalize(payload.get("experiment_id")),
        policy_id=normalize(payload.get("policy_id")),
        agent_id=normalize(payload.get("agent_id")),
        started_at=iso_to_ts(normalize(payload.get("started_at"))),
        finished_at=iso_to_ts(normalize(payload.get("finished_at"))),
        status=normalize(payload.get("status")),
        events=[event_from_dict(ev) for ev in events if isinstance(ev, dict)],
        artifacts=[
            fleet_gateway_v2_pb2.ArtifactRef(
                name=normalize(art.get("name")),
                uri=normalize(art.get("uri")),
                size_bytes=max(0, int(art.get("size_bytes", 0) or 0)),
                sha256=normalize(art.get("sha256")),
            )
            for art in artifacts
            if isinstance(art, dict)
        ],
        summary_metrics={normalize(k): normalize(v) for k, v in summary_metrics.items() if normalize(k)},
    )


class RunStore:
    def __init__(self, root: Path, sent_retention_days: int) -> None:
        self.root = root
        self.pending_dir = root / "pending"
        self.sent_dir = root / "sent"
        self.sent_retention_days = max(1, int(sent_retention_days))
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.sent_dir.mkdir(parents=True, exist_ok=True)

    def _run_dir(self, run_id: str, *, sent: bool = False) -> Path:
        return (self.sent_dir if sent else self.pending_dir) / sanitize_name(run_id, "run")

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(path)

    def start_run(self, run_id: str, context: dict[str, Any]) -> Path:
        run_dir = self._run_dir(run_id)
        (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(
            run_dir / "context.json",
            {
                "state": "measuring",
                "updated_at": now_utc().isoformat().replace("+00:00", "Z"),
                **context,
            },
        )
        return run_dir

    def append_event(self, run_id: str, event: fleet_gateway_v2_pb2.Event) -> None:
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "events.ndjson").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event_to_dict(event), sort_keys=True))
            fh.write("\n")

    def write_command_log(
        self,
        run_id: str,
        cmd_id: str,
        cmdline: str,
        exit_code: int,
        duration_ms: int,
        message: str,
    ) -> fleet_gateway_v2_pb2.ArtifactRef:
        artifacts_dir = self._run_dir(run_id) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        safe_cmd = sanitize_name(cmd_id, "command")
        file_name = f"{safe_cmd}-{int(time.time() * 1000)}.log"
        path = artifacts_dir / file_name
        path.write_text(
            "\n".join(
                [
                    f"timestamp={now_utc().isoformat().replace('+00:00', 'Z')}",
                    f"command_id={cmd_id}",
                    f"cmdline={cmdline}",
                    f"exit_code={exit_code}",
                    f"duration_ms={duration_ms}",
                    "message:",
                    message,
                ]
            ),
            encoding="utf-8",
        )
        return artifact_from_path(file_name, path, run_id)

    def write_summary_artifact(self, run_id: str, payload: dict[str, Any]) -> fleet_gateway_v2_pb2.ArtifactRef:
        artifacts_dir = self._run_dir(run_id) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = artifacts_dir / "run-summary.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return artifact_from_path("run-summary.json", path, run_id)

    def write_text_artifact(self, run_id: str, prefix: str, content: str, *, suffix: str = ".txt") -> fleet_gateway_v2_pb2.ArtifactRef:
        artifacts_dir = self._run_dir(run_id) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time() * 1000)
        file_name = f"{sanitize_name(prefix, 'artifact')}-{stamp}{suffix}"
        path = artifacts_dir / file_name
        path.write_text(content or "", encoding="utf-8")
        return artifact_from_path(file_name, path, run_id)

    def persist_report(self, report: fleet_gateway_v2_pb2.Report) -> None:
        run_dir = self._run_dir(report.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(run_dir / "report.json", report_to_dict(report))
        self._write_json_atomic(
            run_dir / "context.json",
            {
                "state": "measured",
                "updated_at": now_utc().isoformat().replace("+00:00", "Z"),
                "run_id": normalize(report.run_id),
                "policy_id": normalize(report.policy_id),
                "experiment_id": normalize(report.experiment_id),
            },
        )

    def pending_run_ids(self) -> list[str]:
        runs: list[tuple[float, str]] = []
        for child in self.pending_dir.iterdir():
            if not child.is_dir():
                continue
            report_path = child / "report.json"
            if not report_path.exists():
                continue
            runs.append((report_path.stat().st_mtime, child.name))
        runs.sort(key=lambda row: row[0])
        return [run_id for _, run_id in runs]

    def load_report(self, run_id: str) -> fleet_gateway_v2_pb2.Report | None:
        report_path = self._run_dir(run_id) / "report.json"
        if not report_path.exists():
            return None
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Failed to parse stored report for run_id=%s", run_id)
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return report_from_dict(payload)
        except Exception:
            log.exception("Failed to rebuild protobuf report for run_id=%s", run_id)
            return None

    def mark_sent(self, run_id: str) -> None:
        source = self._run_dir(run_id)
        if not source.exists():
            return
        target = self._run_dir(run_id, sent=True)
        if target.exists():
            shutil.rmtree(target)
        source.replace(target)
        context_path = target / "context.json"
        payload: dict[str, Any] = {
            "state": "reported",
            "updated_at": now_utc().isoformat().replace("+00:00", "Z"),
            "run_id": run_id,
        }
        if context_path.exists():
            try:
                current = json.loads(context_path.read_text(encoding="utf-8"))
                if isinstance(current, dict):
                    payload.update(current)
            except Exception:
                pass
        payload["state"] = "reported"
        payload["updated_at"] = now_utc().isoformat().replace("+00:00", "Z")
        self._write_json_atomic(context_path, payload)

    def prune_sent(self) -> None:
        cutoff = now_utc() - timedelta(days=self.sent_retention_days)
        for child in self.sent_dir.iterdir():
            if not child.is_dir():
                continue
            mtime = datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)


def flush_pending_reports(
    stub: fleet_gateway_v2_pb2_grpc.FleetManagerStub,
    agent_id: str,
    store: RunStore,
) -> set[str]:
    sent_run_ids: set[str] = set()
    for run_id in store.pending_run_ids():
        report = store.load_report(run_id)
        if report is None:
            continue
        try:
            resp = stub.PublishReport(
                fleet_gateway_v2_pb2.PublishReportRequest(agent_id=agent_id, report=report),
                timeout=30,
            )
        except grpc.RpcError as exc:
            log.warning("PublishReport replay failed for run_id=%s: %s", run_id, exc)
            break

        status = int(resp.status)
        if status in (
            int(fleet_gateway_v2_pb2.PublishReportResponse.ACCEPTED),
            int(fleet_gateway_v2_pb2.PublishReportResponse.DUPLICATE),
        ):
            store.mark_sent(run_id)
            sent_run_ids.add(run_id)
        else:
            log.warning(
                "PublishReport replay rejected for run_id=%s status=%s reason=%s",
                run_id,
                status,
                normalize(resp.reason),
            )
    store.prune_sent()
    return sent_run_ids


def collect_wifi_scan(
    iface: str,
    timeout_ms: int,
    execute_policy: bool,
    store: RunStore,
    run_id: str,
    cmd_id: str,
    *,
    override_cmdline: str = "",
    override_argv: list[str] | None = None,
    override_env: dict[str, str] | None = None,
) -> tuple[int, int, str, dict[str, str], list[fleet_gateway_v2_pb2.ArtifactRef]]:
    metrics: dict[str, str] = {}
    artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []

    iface = normalize(iface)
    if not iface:
        return 0, 0, "wifi scan: no interface configured", {"wifi_ap_count": "0", "wifi_connected": "0"}, []

    # Optional policy override (useful when an agent wants to use a different tool).
    if execute_policy and (override_argv or (override_cmdline and not _is_trivial_cmdline(override_cmdline))):
        exit_code, duration_ms, short, full = run_command_capture(
            override_cmdline,
            override_argv or [],
            override_env or {},
            timeout_ms,
            execute_policy=True,
        )
        try:
            artifacts.append(store.write_text_artifact(run_id, f"wifi-{cmd_id}-override", full))
        except Exception:
            log.exception("Failed to persist wifi override output artifact")

        # If the override output looks like iw scan output, parse it. Otherwise, fall back to default scan.
        ap_count, avg_rssi = _parse_iw_scan(full)
        if ap_count > 0 or avg_rssi is not None:
            metrics["wifi_ap_count"] = str(max(0, int(ap_count)))
            if avg_rssi is not None:
                metrics["wifi_avg_rssi_dbm"] = f"{avg_rssi:.2f}"
            metrics["wifi_connected"] = "1"
            return exit_code, duration_ms, short or "wifi scan override", metrics, artifacts

    if not execute_policy:
        # Simulation mode for local containers (no RF access). Keep deterministic non-zero values to validate pipeline.
        duration_ms = min(timeout_ms, 1000)
        time.sleep(min(1.0, max(0.1, duration_ms / 1000.0)))
        wifi_ap_count = max(1, int(duration_ms / 250) + 3)
        metrics["wifi_ap_count"] = str(wifi_ap_count)
        metrics["wifi_avg_rssi_dbm"] = "-55.0"
        metrics["wifi_connected"] = "1"
        return 0, duration_ms, f"simulated wifi scan on {iface}", metrics, []

    iw_bin = resolve_executable("iw", ["/usr/sbin/iw", "/sbin/iw"])

    # Primary: active scan (may require CAP_NET_ADMIN); fall back to link status (should work unprivileged).
    if iw_bin:
        exit_code, duration_ms, out = _run_capture_args([iw_bin, "dev", iface, "scan"], timeout_ms)
    else:
        exit_code, duration_ms, out = 127, 0, "iw not found"
    if exit_code == 0:
        ap_count, avg_rssi = _parse_iw_scan(out)
        metrics["wifi_ap_count"] = str(max(0, int(ap_count)))
        if avg_rssi is not None:
            metrics["wifi_avg_rssi_dbm"] = f"{avg_rssi:.2f}"
        metrics["wifi_connected"] = "1"
        msg = f"iw scan ok: ap_count={ap_count}"
        try:
            artifacts.append(store.write_text_artifact(run_id, f"wifi-{cmd_id}-scan", out))
        except Exception:
            log.exception("Failed to persist wifi scan artifact")
        return 0, duration_ms, msg, metrics, artifacts

    # Fallback: current link (iw without scan privileges may still work for link status).
    if iw_bin:
        exit_code2, duration_ms2, out2 = _run_capture_args([iw_bin, "dev", iface, "link"], timeout_ms)
    else:
        exit_code2, duration_ms2, out2 = 127, 0, "iw not found"
    connected, avg_rssi = _parse_iw_link(out2)
    metrics["wifi_connected"] = str(int(bool(connected)))
    metrics["wifi_ap_count"] = "1" if connected else "0"
    if avg_rssi is not None:
        metrics["wifi_avg_rssi_dbm"] = f"{avg_rssi:.2f}"
    msg = "iw scan unavailable; used iw link"
    try:
        artifacts.append(store.write_text_artifact(run_id, f"wifi-{cmd_id}-link", out2))
    except Exception:
        log.exception("Failed to persist wifi link artifact")

    if metrics.get("wifi_avg_rssi_dbm") is not None or metrics.get("wifi_connected") == "1":
        # Treat inability to scan as non-fatal for smoke testing on restricted environments.
        return 0, duration_ms2, msg, metrics, artifacts

    # Final fallback: parse /proc/net/wireless (common on lightweight images).
    try:
        proc_text = Path("/proc/net/wireless").read_text(encoding="utf-8", errors="replace")
        link, level = _parse_proc_net_wireless(proc_text, iface)
        if link is not None:
            metrics["wifi_link_quality"] = f"{link:.2f}"
        if level is not None:
            metrics["wifi_avg_rssi_dbm"] = f"{level:.2f}"
        metrics["wifi_connected"] = "1" if (link is not None or level is not None) else "0"
        metrics["wifi_ap_count"] = "1" if metrics["wifi_connected"] == "1" else "0"
        msg = "iw unavailable; used /proc/net/wireless"
        try:
            artifacts.append(store.write_text_artifact(run_id, f"wifi-{cmd_id}-proc-wireless", proc_text))
        except Exception:
            log.exception("Failed to persist proc wireless artifact")
        return 0, duration_ms2, msg, metrics, artifacts
    except Exception:
        pass

    return 0, duration_ms2, msg, metrics, artifacts


def collect_ble_scan(
    timeout_ms: int,
    execute_policy: bool,
    store: RunStore,
    run_id: str,
    cmd_id: str,
    *,
    override_cmdline: str = "",
    override_argv: list[str] | None = None,
    override_env: dict[str, str] | None = None,
) -> tuple[int, int, str, dict[str, str], list[fleet_gateway_v2_pb2.ArtifactRef]]:
    metrics: dict[str, str] = {}
    artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []

    if execute_policy and (override_argv or (override_cmdline and not _is_trivial_cmdline(override_cmdline))):
        exit_code, duration_ms, short, full = run_command_capture(
            override_cmdline,
            override_argv or [],
            override_env or {},
            timeout_ms,
            execute_policy=True,
        )
        try:
            artifacts.append(store.write_text_artifact(run_id, f"ble-{cmd_id}-override", full))
        except Exception:
            log.exception("Failed to persist ble override output artifact")

        adv_count, avg_rssi = _parse_bluetoothctl_scan(full)
        if adv_count > 0 or avg_rssi is not None:
            metrics["ble_adv_count"] = str(max(0, int(adv_count)))
            if avg_rssi is not None:
                metrics["ble_avg_rssi_dbm"] = f"{avg_rssi:.2f}"
            metrics["ble_scan_ok"] = "1"
            return exit_code, duration_ms, short or "ble scan override", metrics, artifacts

    if not execute_policy:
        duration_ms = min(timeout_ms, 1000)
        time.sleep(min(1.0, max(0.1, duration_ms / 1000.0)))
        ble_adv_count = max(1, int(duration_ms / 200) + 5)
        return (
            0,
            duration_ms,
            "simulated ble scan",
            {"ble_adv_count": str(ble_adv_count), "ble_avg_rssi_dbm": "-60.0", "ble_scan_ok": "1"},
            [],
        )

    # bluetoothctl is the most common unprivileged interface to BlueZ; it may still fail if bluetoothd isn't running.
    exit_code, duration_ms, out = _run_capture_args(
        ["bluetoothctl", "--timeout", str(max(1, int(timeout_ms / 1000))), "scan", "on"],
        timeout_ms,
    )
    adv_count, avg_rssi = _parse_bluetoothctl_scan(out)
    metrics["ble_adv_count"] = str(max(0, int(adv_count)))
    if avg_rssi is not None:
        metrics["ble_avg_rssi_dbm"] = f"{avg_rssi:.2f}"
    # bluetoothctl scan may be intentionally terminated by timeout; treat that as OK if output was captured.
    metrics["ble_scan_ok"] = "1" if exit_code in (0, 124) else "0"
    msg = f"bluetoothctl scan {'ok' if exit_code in (0,124) else 'failed'}: adv_count={adv_count}"
    try:
        artifacts.append(store.write_text_artifact(run_id, f"ble-{cmd_id}-scan", out))
    except Exception:
        log.exception("Failed to persist ble scan artifact")
    # Do not fail the run on BLE unavailability by default (many devices lack BLE).
    return 0, duration_ms, msg, metrics, artifacts


def collect_csi_capture(
    timeout_ms: int,
    execute_policy: bool,
    store: RunStore,
    run_id: str,
    cmd_id: str,
    *,
    cmdline: str = "",
    argv: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, int, str, dict[str, str], list[fleet_gateway_v2_pb2.ArtifactRef]]:
    metrics: dict[str, str] = {"csi_supported": "0", "csi_frames_count": "0"}
    artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []

    if not execute_policy:
        duration_ms = min(timeout_ms, 1000)
        time.sleep(min(1.0, max(0.1, duration_ms / 1000.0)))
        frames = max(1, int(duration_ms / 10) + 50)
        metrics["csi_supported"] = "1"
        metrics["csi_frames_count"] = str(frames)
        return 0, duration_ms, "simulated csi capture", metrics, []

    if cmdline and not _is_trivial_cmdline(cmdline):
        exit_code, duration_ms, short, full = run_command_capture(cmdline, argv or [], env or {}, timeout_ms, True)
        # Heuristic: look for "frames=<n>" in output; callers can standardize this in their tooling.
        m = re.search(r"\\b(?:frames|csi_frames|csi_frames_count)\\s*[=:]\\s*(\\d+)\\b", full, flags=re.IGNORECASE)
        if m:
            metrics["csi_supported"] = "1"
            metrics["csi_frames_count"] = str(int(m.group(1)))
        try:
            artifacts.append(store.write_text_artifact(run_id, f"csi-{cmd_id}-output", full))
        except Exception:
            log.exception("Failed to persist csi output artifact")
        return int(exit_code), duration_ms, short or "csi capture done", metrics, artifacts

    # No capture tool configured.
    try:
        artifacts.append(store.write_text_artifact(run_id, f"csi-{cmd_id}-note", "CSI capture not configured on this agent.\n"))
    except Exception:
        log.exception("Failed to persist csi note artifact")
    return 0, 0, "csi capture not configured", metrics, artifacts


def main() -> None:
    fleet_host = os.environ.get("FLEET_MANAGER_HOST", "monad-fleet-service")
    fleet_port = int(os.environ.get("FLEET_MANAGER_PORT", "50060"))
    target = f"{fleet_host}:{fleet_port}"

    agent_id = normalize(os.environ.get("AGENT_ID") or os.environ.get("DEVICE_ID") or os.environ.get("DEVICE_MAC") or "agent-v2").lower()
    agent_version = os.environ.get("AGENT_VERSION", "0.2.0")
    requested_mode = mode_from_env(os.environ.get("CONTROL_PLANE_MODE", "RF_SHARING"))
    control_plane_iface = os.environ.get("CONTROL_PLANE_IFACE", "wlan0")
    capabilities = [c.strip() for c in os.environ.get("CAPABILITIES", "csi,ble,wifi").split(",") if c.strip()]
    execute_policy = os.environ.get("EXECUTE_POLICY", "false").lower() in {"1", "true", "yes"}
    max_cycles = int(os.environ.get("MAX_SYNC_CYCLES", "0"))
    default_poll_seconds = int(os.environ.get("DEFAULT_POLL_SECONDS", "30"))
    data_root = Path(os.environ.get("DATA_ROOT", "./data"))
    sent_retention_days = int(os.environ.get("SENT_RETENTION_DAYS", "14"))

    fm_target_for_route = os.environ.get("FLEET_MANAGER_ROUTE_TARGET", fleet_host)

    store = RunStore(data_root, sent_retention_days=sent_retention_days)
    channel = grpc.insecure_channel(target)
    stub = fleet_gateway_v2_pb2_grpc.FleetManagerStub(channel)

    hello = stub.Hello(
        fleet_gateway_v2_pb2.HelloRequest(
            agent=fleet_gateway_v2_pb2.AgentDescriptor(
                agent_id=agent_id,
                hostname=socket.gethostname(),
                agent_version=agent_version,
                interfaces=detect_interfaces(),
                requested_mode=requested_mode,
                control_plane_iface=control_plane_iface,
                fleet_manager_target=target,
                capabilities=capabilities,
            )
        ),
        timeout=10,
    )
    log.info("Hello(v2) ok: allowed_mode=%s reason=%s", int(hello.allowed_mode), hello.mode_reason)

    poll = int(hello.recommended_prepare_poll_sec or default_poll_seconds)
    last_policy_id = ""
    cycles = 0

    # Try to deliver any runs persisted from previous process/network interruptions.
    flush_pending_reports(stub, agent_id, store)

    while max_cycles <= 0 or cycles < max_cycles:
        cycles += 1

        assignment = stub.GetAssignment(
            fleet_gateway_v2_pb2.GetAssignmentRequest(agent_id=agent_id),
            timeout=10,
        )
        if assignment.status != fleet_gateway_v2_pb2.GetAssignmentResponse.ASSIGNED:
            log.info("No assignment")
            flush_pending_reports(stub, agent_id, store)
            if reached_max_cycles(cycles, max_cycles):
                break
            time.sleep(poll)
            continue

        policy_resp = stub.GetPolicy(
            fleet_gateway_v2_pb2.GetPolicyRequest(
                agent_id=agent_id,
                experiment_id=assignment.experiment_id,
                last_policy_id=last_policy_id,
            ),
            timeout=15,
        )
        if policy_resp.status == fleet_gateway_v2_pb2.GetPolicyResponse.NOT_MODIFIED:
            log.info("Policy not modified: %s", last_policy_id)
            flush_pending_reports(stub, agent_id, store)
            if reached_max_cycles(cycles, max_cycles):
                break
            time.sleep(poll)
            continue
        if policy_resp.status != fleet_gateway_v2_pb2.GetPolicyResponse.OK:
            log.info("No policy available")
            flush_pending_reports(stub, agent_id, store)
            if reached_max_cycles(cycles, max_cycles):
                break
            time.sleep(poll)
            continue

        policy = policy_resp.policy
        route_iface = route_interface_for_target(fm_target_for_route)
        route_verified = True
        if policy.allowed_mode == fleet_gateway_v2_pb2.DUAL_NIC:
            route_verified = bool(route_iface and route_iface == normalize(policy.control_plane_iface))

        prep = stub.AckPrepared(
            fleet_gateway_v2_pb2.AckPreparedRequest(
                agent_id=agent_id,
                experiment_id=policy.experiment_id,
                policy_id=policy.policy_id,
                preparation_id=str(uuid.uuid4()),
                mode=policy.allowed_mode,
                control_plane_iface=policy.control_plane_iface,
                route_verified=route_verified,
                checks_ok=["route_verified"] if route_verified else [],
                warnings=[] if route_verified else [f"route via {route_iface or 'unknown'}"],
            ),
            timeout=10,
        )
        if prep.status != fleet_gateway_v2_pb2.AckPreparedResponse.ACCEPTED:
            log.warning("AckPrepared rejected: %s", prep.reason)
            flush_pending_reports(stub, agent_id, store)
            if reached_max_cycles(cycles, max_cycles):
                break
            time.sleep(poll)
            continue

        run_id = str(uuid.uuid4())
        store.start_run(
            run_id,
            {
                "run_id": run_id,
                "agent_id": agent_id,
                "policy_id": policy.policy_id,
                "experiment_id": policy.experiment_id,
                "started_at": now_utc().isoformat().replace("+00:00", "Z"),
            },
        )

        events: list[fleet_gateway_v2_pb2.Event] = []
        artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []

        def record(event: fleet_gateway_v2_pb2.Event) -> None:
            events.append(event)
            store.append_event(run_id, event)

        record(
            fleet_gateway_v2_pb2.Event(
                event_id=f"{run_id}:run:start",
                run_id=run_id,
                experiment_id=policy.experiment_id,
                policy_id=policy.policy_id,
                agent_id=agent_id,
                timestamp=now_ts(),
                type=fleet_gateway_v2_pb2.RUN_STARTED,
                message="run started",
            )
        )

        commands_failed = 0
        commands_total = 0
        wifi_ap_total = 0
        ble_adv_total = 0
        csi_frames_total = 0
        for group in policy.command_groups:
            group_from = getattr(group, "from")
            if not in_window(group_from, group.to):
                continue

            for cmd in group.commands:
                commands_total += 1
                cmd_id = normalize(cmd.id) or f"cmd-{commands_total}"
                cmdline = normalize(cmd.cmdline)
                cmd_type_name = command_type_name(int(cmd.type))
                cmd_argv = [normalize(a) for a in getattr(cmd, "argv", []) if normalize(a)]
                cmd_env = {normalize(k): normalize(v) for k, v in getattr(cmd, "env", {}).items() if normalize(k)}

                record(
                    fleet_gateway_v2_pb2.Event(
                        event_id=f"{run_id}:{cmd_id}:start",
                        run_id=run_id,
                        experiment_id=policy.experiment_id,
                        policy_id=policy.policy_id,
                        agent_id=agent_id,
                        timestamp=now_ts(),
                        type=fleet_gateway_v2_pb2.COMMAND_STARTED,
                        group_id=normalize(group.id),
                        command_id=cmd_id,
                        message=cmdline or (" ".join(cmd_argv) if cmd_argv else cmd_type_name),
                    )
                )

                timeout_ms = int(cmd.timeout_ms or 60000)
                extra_artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []

                if int(cmd.type) == int(fleet_gateway_v2_pb2.WIFI_SCAN):
                    wifi_iface = normalize(os.environ.get("WIFI_SCAN_IFACE") or control_plane_iface)
                    exit_code, duration_ms, message, parsed, extra_artifacts = collect_wifi_scan(
                        wifi_iface,
                        timeout_ms,
                        execute_policy,
                        store,
                        run_id,
                        cmd_id,
                        override_cmdline=cmdline,
                        override_argv=cmd_argv,
                        override_env=cmd_env,
                    )
                    wifi_ap_count = int(parsed.get("wifi_ap_count", "0") or 0)
                    wifi_ap_total += max(0, wifi_ap_count)
                    event_metrics = parsed
                elif int(cmd.type) == int(fleet_gateway_v2_pb2.BLE_SCAN):
                    exit_code, duration_ms, message, parsed, extra_artifacts = collect_ble_scan(
                        timeout_ms,
                        execute_policy,
                        store,
                        run_id,
                        cmd_id,
                        override_cmdline=cmdline,
                        override_argv=cmd_argv,
                        override_env=cmd_env,
                    )
                    ble_adv_count = int(parsed.get("ble_adv_count", "0") or 0)
                    ble_adv_total += max(0, ble_adv_count)
                    event_metrics = parsed
                elif int(cmd.type) == int(fleet_gateway_v2_pb2.CAPTURE_CSI):
                    exit_code, duration_ms, message, parsed, extra_artifacts = collect_csi_capture(
                        timeout_ms,
                        execute_policy,
                        store,
                        run_id,
                        cmd_id,
                        cmdline=cmdline,
                        argv=cmd_argv,
                        env=cmd_env,
                    )
                    csi_frames_count = int(parsed.get("csi_frames_count", "0") or 0)
                    csi_frames_total += max(0, csi_frames_count)
                    event_metrics = parsed
                else:
                    exec_cmdline = cmdline or (" ".join(cmd_argv) if cmd_argv else "echo no-op")
                    exit_code, duration_ms, message, full = run_command_capture(
                        exec_cmdline,
                        cmd_argv,
                        cmd_env,
                        timeout_ms,
                        execute_policy,
                    )
                    event_metrics = {}
                    # Store raw output for arbitrary shell commands too (useful for parsing later).
                    if full:
                        try:
                            extra_artifacts.append(store.write_text_artifact(run_id, f"cmd-{cmd_id}-output", full))
                        except Exception:
                            log.exception("Failed to persist command output artifact for cmd_id=%s", cmd_id)

                if exit_code != 0:
                    commands_failed += 1

                event_metrics = {
                    "duration_ms": str(max(0, int(duration_ms))),
                    "command_type": cmd_type_name,
                    **{normalize(k): normalize(v) for k, v in (event_metrics or {}).items() if normalize(k)},
                }

                record(
                    fleet_gateway_v2_pb2.Event(
                        event_id=f"{run_id}:{cmd_id}:end",
                        run_id=run_id,
                        experiment_id=policy.experiment_id,
                        policy_id=policy.policy_id,
                        agent_id=agent_id,
                        timestamp=now_ts(),
                        type=event_type_for_exit(exit_code),
                        group_id=normalize(group.id),
                        command_id=cmd_id,
                        exit_code=exit_code,
                        duration_ms=max(0, int(duration_ms)),
                        message=message,
                        metrics=event_metrics,
                    )
                )

                try:
                    artifacts.append(
                        store.write_command_log(
                            run_id=run_id,
                            cmd_id=cmd_id,
                            cmdline=cmdline or (" ".join(cmd_argv) if cmd_argv else cmd_type_name),
                            exit_code=exit_code,
                            duration_ms=max(0, int(duration_ms)),
                            message=message,
                        )
                    )
                except Exception:
                    log.exception("Failed to persist command artifact for run_id=%s command_id=%s", run_id, cmd_id)

                artifacts.extend(extra_artifacts)

                if exit_code != 0 and group.failure_mode == fleet_gateway_v2_pb2.FAIL_FAST:
                    break

        status = "OK" if commands_failed == 0 else "FAILED"
        record(
            fleet_gateway_v2_pb2.Event(
                event_id=f"{run_id}:run:end",
                run_id=run_id,
                experiment_id=policy.experiment_id,
                policy_id=policy.policy_id,
                agent_id=agent_id,
                timestamp=now_ts(),
                type=fleet_gateway_v2_pb2.RUN_FINISHED,
                message=status,
                metrics={
                    "run_state": "COMPLETED" if commands_failed == 0 else "FAILED",
                    "commands_total": str(commands_total),
                    "commands_failed": str(commands_failed),
                    "wifi_ap_total": str(wifi_ap_total),
                    "ble_adv_total": str(ble_adv_total),
                    "csi_frames_total": str(csi_frames_total),
                },
            )
        )

        try:
            artifacts.append(
                store.write_summary_artifact(
                    run_id,
                    {
                        "run_id": run_id,
                        "status": status,
                        "commands_total": commands_total,
                        "commands_failed": commands_failed,
                        "wifi_ap_total": wifi_ap_total,
                        "ble_adv_total": ble_adv_total,
                        "csi_frames_total": csi_frames_total,
                        "generated_at": now_utc().isoformat().replace("+00:00", "Z"),
                    },
                )
            )
        except Exception:
            log.exception("Failed to persist summary artifact for run_id=%s", run_id)

        report = fleet_gateway_v2_pb2.Report(
            run_id=run_id,
            experiment_id=policy.experiment_id,
            policy_id=policy.policy_id,
            agent_id=agent_id,
            started_at=events[0].timestamp,
            finished_at=events[-1].timestamp,
            status=status,
            events=events,
            artifacts=artifacts,
            summary_metrics={
                "commands_total": str(commands_total),
                "commands_failed": str(commands_failed),
                "artifacts_total": str(len(artifacts)),
                "wifi_ap_total": str(wifi_ap_total),
                "ble_adv_total": str(ble_adv_total),
                "csi_frames_total": str(csi_frames_total),
                "spool_state": "pending_upload",
                "fleet_interface_version": "v3",
            },
        )

        store.persist_report(report)
        last_policy_id = policy.policy_id

        sent_ids = flush_pending_reports(stub, agent_id, store)
        if run_id in sent_ids:
            log.info("Run %s stored and reported", run_id)
        else:
            log.warning("Run %s stored locally and queued for later upload", run_id)

        if reached_max_cycles(cycles, max_cycles):
            break
        time.sleep(poll)


if __name__ == "__main__":
    main()
