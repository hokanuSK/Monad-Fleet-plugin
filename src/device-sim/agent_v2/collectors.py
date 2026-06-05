from .core import *  # noqa: F401,F403
from . import core as core_mod

_collect_device_status_metrics = core_mod._collect_device_status_metrics
_collect_iface_counters = core_mod._collect_iface_counters
_collect_wifi_observability_metrics = core_mod._collect_wifi_observability_metrics
_extract_output_paths_from_command = core_mod._extract_output_paths_from_command
_find_alternate_wifi_iface = core_mod._find_alternate_wifi_iface
_is_trivial_cmdline = core_mod._is_trivial_cmdline
_parse_bluetoothctl_scan = core_mod._parse_bluetoothctl_scan
_parse_iw_link = core_mod._parse_iw_link
_parse_iw_link_extended = core_mod._parse_iw_link_extended
_parse_iw_scan = core_mod._parse_iw_scan
_parse_proc_net_wireless = core_mod._parse_proc_net_wireless
_resolve_wifi_scan_iface = core_mod._resolve_wifi_scan_iface
_run_capture_args = core_mod._run_capture_args
_run_local_cmd = core_mod._run_local_cmd
_safe_float = core_mod._safe_float
_safe_int = core_mod._safe_int
from .sinks import _ingest_metrics_http, opportunistic_upload_artifacts_to_elab
from . import wifi5g_capture as _wifi5g_capture
from . import ble_advertise as _ble_advertise


def _artifact_stem(base: str, detail: str = "") -> str:
    clean_base = sanitize_name(base, "artifact")
    clean_detail = sanitize_name(detail, "")
    if clean_detail:
        return f"{clean_base}-{clean_detail}"
    return clean_base


def _wifi_sample_label(index: int, total: int) -> str:
    index = max(1, int(index))
    total = max(1, int(total))
    if total <= 1:
        return "single-sample"
    if index == 1:
        return "first-sample"
    if index == total:
        return "last-sample"
    width = max(3, len(str(total)))
    return f"sample-{index:0{width}d}-of-{total:0{width}d}"


def collect_wifi_monitor_capture(
    *,
    iface: str,
    timeout_ms: int,
    execute_policy: bool,
    store: RunStore,
    run_id: str,
    cmd_id: str,
    override_env: dict[str, str],
    persist_artifacts: bool,
) -> tuple[int, int, str, dict[str, str], list[fleet_gateway_v2_pb2.ArtifactRef]]:
    """Phase 2 WIFI_SCAN branch: passive monitor + channel hopping capture.

    Reads ``WIFI_SCAN_CHANNELS`` (CSV), ``WIFI_SCAN_CHANNEL_DWELL_S``, and
    ``WIFI_SCAN_CHANNEL_WIDTH_MHZ`` from ``override_env`` and runs a
    monitor-mode capture for ~``timeout_ms`` minus a short teardown buffer.

    On success returns the standard collector tuple with a pcap +
    channel-schedule artifact pair. On any failure (missing tools, iface
    cannot enter monitor mode, tcpdump dies) returns rc=1 with an empty
    artifact list and a descriptive message; callers should not treat
    that as fatal for the rest of the run.
    """
    env = override_env or {}
    metrics: dict[str, str] = {"wifi_capture_mode": "passive_monitor"}
    artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []

    iface = normalize(iface)
    if not iface:
        metrics["wifi_capture_status"] = "no_iface"
        return 1, 0, "passive_monitor: no interface configured", metrics, []

    if not execute_policy:
        metrics["wifi_capture_status"] = "dry_run"
        return 0, 100, "passive_monitor dry run (execute_policy=false)", metrics, []

    channels = _wifi5g_capture.parse_channel_list(env.get("WIFI_SCAN_CHANNELS", ""))
    try:
        dwell_s = float(env.get("WIFI_SCAN_CHANNEL_DWELL_S") or _wifi5g_capture.DEFAULT_DWELL_S)
    except (TypeError, ValueError):
        dwell_s = _wifi5g_capture.DEFAULT_DWELL_S
    try:
        width_mhz = int(env.get("WIFI_SCAN_CHANNEL_WIDTH_MHZ") or _wifi5g_capture.DEFAULT_CHANNEL_WIDTH_MHZ)
    except (TypeError, ValueError):
        width_mhz = _wifi5g_capture.DEFAULT_CHANNEL_WIDTH_MHZ
    try:
        rotate_mb = int(env.get("WIFI_SCAN_PCAP_ROTATE_MB") or "0")
    except (TypeError, ValueError):
        rotate_mb = 0

    # Stage capture outputs in /tmp; we copy them into the run-artifact dir
    # after stop so the partial pcap won't pollute the spool if we crash.
    stamp = int(time.time())
    pcap_scratch = f"/tmp/wifi-5g-monitor-{run_id}-{stamp}.pcap"
    schedule_scratch = f"/tmp/wifi-5g-schedule-{run_id}-{stamp}.log"

    try:
        from . import prom_exposition as _prom
    except Exception:
        _prom = None  # type: ignore[assignment]

    def _prom_set(name: str, value: float) -> None:
        if _prom is None:
            return
        gauge = getattr(_prom, name, None)
        if gauge is None:
            return
        try:
            gauge.set(value)
        except Exception:
            pass

    _prom_set("wifi5g_capture_active", 1.0)

    handles = _wifi5g_capture.start_capture(
        iface=iface,
        pcap_path=pcap_scratch,
        schedule_log_path=schedule_scratch,
        channels=channels,
        dwell_s=dwell_s,
        width_mhz=width_mhz,
        rotate_mb=rotate_mb,
    )
    if handles is None:
        _prom_set("wifi5g_capture_active", 0.0)
        metrics["wifi_capture_status"] = "start_failed"
        return 1, 0, "passive_monitor: could not start monitor-mode capture", metrics, []

    # Block for the configured capture duration, leaving a small teardown
    # buffer so the pcap gets a clean flush before the run finalizer fires.
    capture_duration_s = max(1, int(timeout_ms / 1000) - 2)
    deadline_ns = time.monotonic_ns() + capture_duration_s * 1_000_000_000
    while time.monotonic_ns() < deadline_ns:
        time.sleep(0.5)
        if handles.tcpdump_proc.poll() is not None:
            log.warning(
                "wifi5g_capture: tcpdump exited early rc=%s",
                handles.tcpdump_proc.returncode,
            )
            break

    stats = _wifi5g_capture.stop_capture(handles)
    _prom_set("wifi5g_capture_active", 0.0)
    _prom_set("wifi5g_current_channel", 0)

    if persist_artifacts:
        try:
            pcap_paths = [Path(path) for path in stats.get("pcap_segment_paths") or []]
            if not pcap_paths and Path(pcap_scratch).exists():
                pcap_paths = [Path(pcap_scratch)]
            if len(pcap_paths) == 1:
                artifacts.append(
                    store.import_file_artifact(
                        run_id,
                        pcap_paths[0],
                        artifact_name="wifi-5g-monitor.pcap",
                    )
                )
            else:
                for idx, pcap_path in enumerate(pcap_paths, start=1):
                    artifacts.append(
                        store.import_file_artifact(
                            run_id,
                            pcap_path,
                            artifact_name=f"wifi-5g-monitor-part-{idx:03d}.pcap",
                        )
                    )
        except Exception:
            log.exception("Failed to import wifi-5g-monitor.pcap as run artifact")
        try:
            if Path(schedule_scratch).exists():
                sched_text = Path(schedule_scratch).read_text(encoding="utf-8")
                artifacts.append(
                    store.write_text_artifact(
                        run_id,
                        "wifi-5g-channel-schedule",
                        sched_text,
                        suffix=".log",
                        include_timestamp=False,
                    )
                )
        except Exception:
            log.exception("Failed to persist wifi-5g-channel-schedule.log")

    scratch_paths = {pcap_scratch, schedule_scratch, *[str(path) for path in stats.get("pcap_segment_paths") or []]}
    for scratch in scratch_paths:
        try:
            Path(scratch).unlink()
        except FileNotFoundError:
            pass
        except Exception:
            log.debug("Failed to unlink scratch file %s", scratch, exc_info=True)

    metrics["wifi_capture_status"] = "ok"
    metrics["wifi_capture_pcap_bytes"] = str(stats["pcap_bytes"])
    metrics["wifi_capture_pcap_segments"] = str(stats.get("pcap_segment_count") or 0)
    metrics["wifi_capture_dwell_changes"] = str(stats["dwell_changes"])
    metrics["wifi_capture_elapsed_s"] = f"{stats['elapsed_s']:.2f}"
    metrics["wifi_capture_channels"] = str(len(channels))
    metrics["wifi_capture_dwell_s"] = f"{dwell_s:.2f}"
    duration_ms = int(stats["elapsed_s"] * 1000)
    msg = (
        f"passive_monitor: {stats['pcap_bytes']}B pcap, "
        f"{stats['dwell_changes']} dwells over {len(channels)} channels"
    )
    if int(stats.get("pcap_segment_count") or 0) > 1:
        msg += f" (segments={int(stats['pcap_segment_count'])})"
    return 0, duration_ms, msg, metrics, artifacts


def collect_wifi_scan(
    iface: str,
    timeout_ms: int,
    execute_policy: bool,
    store: RunStore,
    run_id: str,
    cmd_id: str,
    *,
    persist_artifacts: bool = True,
    override_cmdline: str = "",
    override_argv: list[str] | None = None,
    override_env: dict[str, str] | None = None,
    artifact_label: str = "",
) -> tuple[int, int, str, dict[str, str], list[fleet_gateway_v2_pb2.ArtifactRef]]:
    # Phase 2: branch on WIFI_SCAN_MODE before any default-path work so a
    # passive_monitor policy gets routed to the new pcap-based capture without
    # the legacy `iw scan` running first.
    scan_mode = normalize((override_env or {}).get("WIFI_SCAN_MODE")).lower()
    if scan_mode == "passive_monitor":
        return collect_wifi_monitor_capture(
            iface=iface,
            timeout_ms=timeout_ms,
            execute_policy=execute_policy,
            store=store,
            run_id=run_id,
            cmd_id=cmd_id,
            override_env=override_env or {},
            persist_artifacts=persist_artifacts,
        )

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
        if persist_artifacts:
            try:
                artifacts.append(
                    store.write_text_artifact(
                        run_id,
                        _artifact_stem("wifi-command-override-output", artifact_label),
                        full,
                        include_timestamp=False,
                    )
                )
            except Exception:
                log.exception("Failed to persist wifi override output artifact")

        # If the override output looks like iw scan output, parse it. Otherwise, fall back to default scan.
        ap_count, avg_rssi = _parse_iw_scan(full)
        if ap_count > 0 or avg_rssi is not None:
            metrics["wifi_ap_count"] = str(max(0, int(ap_count)))
            if avg_rssi is not None:
                metrics["wifi_avg_rssi_dbm"] = f"{avg_rssi:.2f}"
            metrics["wifi_connected"] = "1"
            metrics.update(_collect_wifi_observability_metrics(iface, timeout_ms))
            return exit_code, duration_ms, short or "wifi scan override", metrics, artifacts

    if not execute_policy:
        # Simulation mode for local containers (no RF access). Keep deterministic non-zero values to validate pipeline.
        duration_ms = min(timeout_ms, 1000)
        time.sleep(min(1.0, max(0.1, duration_ms / 1000.0)))
        wifi_ap_count = max(1, int(duration_ms / 250) + 3)
        metrics["wifi_ap_count"] = str(wifi_ap_count)
        metrics["wifi_avg_rssi_dbm"] = "-55.0"
        metrics["wifi_connected"] = "1"
        metrics["wifi_scan_ok"] = "1"
        metrics["wifi_scan_iface_requested"] = iface
        metrics["wifi_scan_iface_used"] = iface
        metrics.update(_collect_device_status_metrics())
        return 0, duration_ms, f"simulated wifi scan on {iface}", metrics, []

    iw_bin = resolve_executable("iw", ["/usr/sbin/iw", "/sbin/iw"])
    resolved_iface, iface_meta, iface_note = _resolve_wifi_scan_iface(iface, iw_bin)
    metrics.update(iface_meta)
    if not resolved_iface:
        metrics.setdefault("wifi_scan_ok", "0")
        metrics.setdefault("wifi_connected", "0")
        metrics.setdefault("wifi_ap_count", "0")
        metrics.update(_collect_device_status_metrics())
        return 65, 0, iface_note, metrics, artifacts
    iface = resolved_iface

    # Primary: active scan (may require CAP_NET_ADMIN); fall back to link status (should work unprivileged).
    if iw_bin:
        exit_code, duration_ms, out = _run_capture_args([iw_bin, "dev", iface, "scan"], timeout_ms)
    else:
        exit_code, duration_ms, out = 127, 0, "iw not found"
    if exit_code != 0 and "No such device" in (out or ""):
        retry_iface = _find_alternate_wifi_iface(iface, iw_bin)
        if retry_iface and retry_iface != iface:
            original_iface = iface
            iface = retry_iface
            metrics["wifi_scan_iface_used"] = iface
            metrics["wifi_scan_iface_fallback"] = "1"
            retry_note = f"scan iface '{original_iface}' reported no such device; retried on '{iface}'"
            if iface_note:
                iface_note = f"{iface_note}; {retry_note}"
            else:
                iface_note = retry_note
            if iw_bin:
                exit_code, duration_ms, out = _run_capture_args([iw_bin, "dev", iface, "scan"], timeout_ms)
        else:
            up_ok = False
            for up_cmd in (["sudo", "-n", "ip", "link", "set", iface, "up"], ["ip", "link", "set", iface, "up"]):
                rc, _ = _run_local_cmd(up_cmd, timeout_s=8)
                if rc == 0:
                    up_ok = True
                    break
            if up_ok and iw_bin:
                exit_code, duration_ms, out = _run_capture_args([iw_bin, "dev", iface, "scan"], timeout_ms)
                if exit_code == 0:
                    recover_note = f"scan iface '{iface}' was brought up and recovered after no such device"
                    if iface_note:
                        iface_note = f"{iface_note}; {recover_note}"
                    else:
                        iface_note = recover_note

    if exit_code == 0:
        ap_count, avg_rssi = _parse_iw_scan(out)
        metrics["wifi_ap_count"] = str(max(0, int(ap_count)))
        if avg_rssi is not None:
            metrics["wifi_avg_rssi_dbm"] = f"{avg_rssi:.2f}"
        metrics["wifi_connected"] = "1"
        metrics["wifi_scan_ok"] = "1"
        metrics.update(_collect_wifi_observability_metrics(iface, timeout_ms, iw_bin))
        msg = f"iw scan ok: ap_count={ap_count}"
        if iface_note:
            msg = f"{msg}; {iface_note}"
        if persist_artifacts:
            try:
                artifacts.append(
                    store.write_text_artifact(
                        run_id,
                        _artifact_stem("wifi-access-point-scan", artifact_label or "current"),
                        out,
                        include_timestamp=False,
                    )
                )
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
    metrics.update(_parse_iw_link_extended(out2))
    metrics.update(_collect_wifi_observability_metrics(iface, timeout_ms, iw_bin))
    msg = "iw scan unavailable; used iw link"
    if iface_note:
        msg = f"{msg}; {iface_note}"
    if persist_artifacts:
        try:
            artifacts.append(
                store.write_text_artifact(
                    run_id,
                    _artifact_stem("wifi-link-status", artifact_label or "current"),
                    out2,
                    include_timestamp=False,
                )
            )
        except Exception:
            log.exception("Failed to persist wifi link artifact")

    has_channel_context = metrics.get("wifi_channel") is not None or metrics.get("wifi_freq_mhz") is not None
    if (
        metrics.get("wifi_avg_rssi_dbm") is not None
        or metrics.get("wifi_connected") == "1"
        or has_channel_context
    ):
        # Treat inability to scan as non-fatal for smoke testing on restricted environments.
        # Monitor-mode captures may be intentionally not associated but still provide channel/frequency evidence.
        metrics["wifi_scan_ok"] = "1"
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
        metrics.update(_collect_iface_counters(iface))
        metrics.update(_collect_device_status_metrics())
        msg = "iw unavailable; used /proc/net/wireless"
        if iface_note:
            msg = f"{msg}; {iface_note}"
        if persist_artifacts:
            try:
                artifacts.append(
                    store.write_text_artifact(
                        run_id,
                        _artifact_stem("wifi-proc-wireless-status", artifact_label or "current"),
                        proc_text,
                        include_timestamp=False,
                    )
                )
            except Exception:
                log.exception("Failed to persist proc wireless artifact")
        has_channel_context = metrics.get("wifi_channel") is not None or metrics.get("wifi_freq_mhz") is not None
        if (
            metrics.get("wifi_connected") == "1"
            or metrics.get("wifi_avg_rssi_dbm") is not None
            or has_channel_context
        ):
            metrics["wifi_scan_ok"] = "1"
            return 0, duration_ms2, msg, metrics, artifacts
    except Exception:
        pass

    metrics["wifi_scan_ok"] = "0"
    metrics.setdefault("wifi_connected", "0")
    metrics.setdefault("wifi_ap_count", "0")
    metrics.update(_collect_device_status_metrics())
    failure_msg = (
        "wifi sensing failed: no usable scan/link/channel evidence "
        "(scan unavailable and interface not connected)"
    )
    if iface_note:
        failure_msg = f"{failure_msg}; {iface_note}"
    if persist_artifacts:
        try:
            debug_payload = (
                f"iw_scan_output:\n{out or '<empty>'}\n\n"
                f"iw_link_output:\n{out2 if 'out2' in locals() else '<empty>'}\n"
            )
            artifacts.append(
                store.write_text_artifact(
                    run_id,
                    _artifact_stem("wifi-diagnostics-debug", artifact_label or "current"),
                    debug_payload,
                    include_timestamp=False,
                )
            )
        except Exception:
            log.exception("Failed to persist wifi debug artifact")
    return 65, duration_ms2 if "duration_ms2" in locals() else duration_ms, failure_msg, metrics, artifacts


def collect_wifi_scan_series(
    iface: str,
    timeout_ms: int,
    execute_policy: bool,
    store: RunStore,
    run_id: str,
    cmd_id: str,
    *,
    sample_interval_s: int,
    sample_duration_s: int,
    device_id: str,
    emit_live_samples: bool,
    experiment_id: str = "",
    agent_id: str = "",
    upload_during_measure: bool = False,
    artifact_upload_target: str = "elabftw",
    override_cmdline: str = "",
    override_argv: list[str] | None = None,
    override_env: dict[str, str] | None = None,
) -> tuple[int, int, str, dict[str, str], list[fleet_gateway_v2_pb2.ArtifactRef]]:
    cfg_env = override_env or {}
    min_interval_s = max(1, parse_int(os.environ.get("WIFI_SAMPLE_MIN_INTERVAL_S"), 1))
    max_duration_s = max(1, parse_int(os.environ.get("WIFI_SAMPLE_MAX_DURATION_S"), 300))
    max_points = max(1, parse_int(os.environ.get("WIFI_SAMPLE_MAX_POINTS"), 600))

    requested_interval_s = max(1, sample_interval_s)
    requested_duration_s = max(1, sample_duration_s)
    effective_interval_s = max(min_interval_s, requested_interval_s)
    effective_duration_s = min(requested_duration_s, max_duration_s)
    effective_duration_s = min(effective_duration_s, max(1, int(timeout_ms / 1000)))
    requested_start_delay_s = max(
        0,
        parse_int(cfg_env.get("WIFI_SAMPLE_START_DELAY_S") or os.environ.get("WIFI_SAMPLE_START_DELAY_S"), 0),
    )
    requested_start_jitter_s = max(
        0,
        parse_int(cfg_env.get("WIFI_SAMPLE_START_JITTER_S") or os.environ.get("WIFI_SAMPLE_START_JITTER_S"), 0),
    )
    requested_start_at_epoch_s = max(
        0,
        parse_int(cfg_env.get("WIFI_SAMPLE_START_AT_EPOCH_S") or os.environ.get("WIFI_SAMPLE_START_AT_EPOCH_S"), 0),
    )
    start_jitter_draw_s = random.randint(0, requested_start_jitter_s) if requested_start_jitter_s > 0 else 0
    start_wait_epoch_needed_s = 0
    if requested_start_at_epoch_s > 0:
        now_epoch_s = int(time.time())
        if requested_start_at_epoch_s > now_epoch_s:
            start_wait_epoch_needed_s = requested_start_at_epoch_s - now_epoch_s

    # Keep preface wait bounded so a misconfigured start time does not stall a run indefinitely.
    max_preface_sleep_s = max(0, int(timeout_ms / 1000) - 1)
    start_wait_epoch_applied_s = min(start_wait_epoch_needed_s, max_preface_sleep_s)
    remaining_preface_s = max_preface_sleep_s - start_wait_epoch_applied_s
    start_delay_applied_s = min(requested_start_delay_s, remaining_preface_s)
    remaining_preface_s -= start_delay_applied_s
    start_jitter_applied_s = min(start_jitter_draw_s, remaining_preface_s)
    preface_sleep_applied_s = start_wait_epoch_applied_s + start_delay_applied_s + start_jitter_applied_s

    effective_duration_budget_s = max(1, effective_duration_s - preface_sleep_applied_s)
    effective_points = max(1, min(max_points, int(effective_duration_budget_s / max(1, effective_interval_s))))
    requested_artifact_stride = parse_int(
        cfg_env.get("WIFI_SAMPLE_ARTIFACT_STRIDE") or os.environ.get("WIFI_SAMPLE_ARTIFACT_STRIDE"),
        0,
    )
    artifact_stride = max(0, requested_artifact_stride)

    all_artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []
    rssi_samples: list[float] = []
    link_samples: list[float] = []
    ap_samples: list[int] = []
    connected_samples = 0
    scan_ok_samples = 0
    artifact_samples = 0
    artifacts_uploaded_during_measure = 0
    artifacts_upload_failed_during_measure = 0
    total_duration_ms = 0
    last_metrics: dict[str, str] = {}
    start_monotonic = time.monotonic()

    if preface_sleep_applied_s > 0:
        time.sleep(preface_sleep_applied_s)

    for idx in range(1, effective_points + 1):
        sample_start = time.monotonic()
        keep_sample_artifacts = False
        if artifact_stride <= 0:
            keep_sample_artifacts = idx == 1 or idx == effective_points
        elif artifact_stride == 1:
            keep_sample_artifacts = True
        else:
            keep_sample_artifacts = (idx == 1) or (idx == effective_points) or (idx % artifact_stride == 0)
        exit_code, duration_ms, _, metrics, artifacts = collect_wifi_scan(
            iface,
            min(timeout_ms, max(1000, effective_interval_s * 1000)),
            execute_policy,
            store,
            run_id,
            f"{cmd_id}-s{idx}",
            persist_artifacts=keep_sample_artifacts,
            override_cmdline=override_cmdline,
            override_argv=override_argv,
            override_env=override_env,
            artifact_label=_wifi_sample_label(idx, effective_points),
        )
        total_duration_ms += max(0, int(duration_ms))
        all_artifacts.extend(artifacts)
        if keep_sample_artifacts:
            artifact_samples += 1
        if upload_during_measure and artifacts:
            uploaded_now, failed_now = opportunistic_upload_artifacts_to_elab(
                run_id,
                experiment_id,
                agent_id,
                artifacts,
                store,
                enabled=True,
                target=artifact_upload_target,
            )
            artifacts_uploaded_during_measure += uploaded_now
            artifacts_upload_failed_during_measure += failed_now
        last_metrics = metrics or {}

        ap_count = parse_int(last_metrics.get("wifi_ap_count"), 0)
        ap_samples.append(max(0, ap_count))
        if normalize(last_metrics.get("wifi_connected")) == "1":
            connected_samples += 1
        sample_has_rssi = normalize(last_metrics.get("wifi_avg_rssi_dbm")) != ""
        sample_has_channel_context = (
            normalize(last_metrics.get("wifi_channel")) != ""
            or normalize(last_metrics.get("wifi_freq_mhz")) != ""
        )
        if (
            exit_code == 0
            or normalize(last_metrics.get("wifi_connected")) == "1"
            or sample_has_rssi
            or sample_has_channel_context
        ):
            scan_ok_samples += 1

        rssi = None
        try:
            rssi = float(normalize(last_metrics.get("wifi_avg_rssi_dbm")))
        except Exception:
            rssi = None
        if rssi is not None:
            rssi_samples.append(rssi)

        link_quality = None
        try:
            link_quality = float(normalize(last_metrics.get("wifi_link_quality")))
        except Exception:
            link_quality = None
        if link_quality is not None:
            link_samples.append(link_quality)

        if emit_live_samples:
            values: dict[str, float] = {"wifi_rssi_sample_index": float(idx)}
            if rssi is not None:
                values["wifi_avg_rssi_dbm"] = float(rssi)
            if link_quality is not None:
                values["wifi_link_quality"] = float(link_quality)
            values["wifi_connected"] = 1.0 if normalize(last_metrics.get("wifi_connected")) == "1" else 0.0
            for metric_key in (
                "wifi_tx_bitrate_mbps",
                "wifi_rx_bitrate_mbps",
                "wifi_signal_dbm",
                "wifi_channel",
                "wifi_freq_mhz",
                "device_cpu_temp_c",
                "device_load1",
            ):
                numeric = _safe_float(last_metrics.get(metric_key))
                if numeric is None:
                    continue
                values[metric_key] = float(numeric)
            _ingest_metrics_http(
                device_id,
                values,
                source=normalize(last_metrics.get("wifi_sample_source")) or "agent-wifi-sampler",
            )

        elapsed = time.monotonic() - sample_start
        sleep_s = max(0.0, float(effective_interval_s) - elapsed)
        if idx < effective_points and sleep_s > 0:
            time.sleep(sleep_s)

    aggregate: dict[str, str] = {
        "wifi_ap_count": str(int(round(sum(ap_samples) / len(ap_samples)))) if ap_samples else "0",
        "wifi_connected": "1" if connected_samples > 0 else "0",
        "wifi_scan_ok": "1" if scan_ok_samples > 0 else "0",
        "wifi_rssi_samples": str(len(rssi_samples)),
        "wifi_sampling_interval_s_requested": str(requested_interval_s),
        "wifi_sampling_duration_s_requested": str(requested_duration_s),
        "wifi_sampling_interval_s_applied": str(effective_interval_s),
        "wifi_sampling_duration_s_applied": str(effective_duration_budget_s),
        "wifi_sampling_points_applied": str(effective_points),
        "wifi_sampling_artifact_stride_applied": str(artifact_stride),
        "wifi_sampling_artifact_samples": str(artifact_samples),
        "artifacts_uploaded_during_measure": str(artifacts_uploaded_during_measure),
        "artifacts_upload_failed_during_measure": str(artifacts_upload_failed_during_measure),
        "wifi_sampling_start_delay_s_requested": str(requested_start_delay_s),
        "wifi_sampling_start_delay_s_applied": str(start_delay_applied_s),
        "wifi_sampling_start_jitter_s_requested": str(requested_start_jitter_s),
        "wifi_sampling_start_jitter_s_applied": str(start_jitter_applied_s),
        "wifi_sampling_start_wait_epoch_s_requested": str(start_wait_epoch_needed_s),
        "wifi_sampling_start_wait_epoch_s_applied": str(start_wait_epoch_applied_s),
        "wifi_sampling_preface_sleep_s_applied": str(preface_sleep_applied_s),
    }
    if rssi_samples:
        aggregate["wifi_avg_rssi_dbm"] = f"{(sum(rssi_samples) / len(rssi_samples)):.2f}"
        aggregate["wifi_rssi_min_dbm"] = f"{min(rssi_samples):.2f}"
        aggregate["wifi_rssi_max_dbm"] = f"{max(rssi_samples):.2f}"
    if link_samples:
        aggregate["wifi_link_quality"] = f"{(sum(link_samples) / len(link_samples)):.2f}"

    wall_ms = int((time.monotonic() - start_monotonic) * 1000.0)
    message = (
        f"wifi sampled {effective_points}x in {effective_duration_budget_s}s "
        f"(interval={effective_interval_s}s, preface={preface_sleep_applied_s}s, rssi_samples={len(rssi_samples)})"
    )
    return 0, max(total_duration_ms, wall_ms), message, aggregate, all_artifacts


def collect_ble_advertise(
    *,
    timeout_ms: int,
    execute_policy: bool,
    store: RunStore,
    run_id: str,
    cmd_id: str,
    override_env: dict[str, str],
) -> tuple[int, int, str, dict[str, str], list[fleet_gateway_v2_pb2.ArtifactRef]]:
    """Phase 3 BLE_SCAN branch: drive ``bluetoothctl`` to broadcast LE
    advertisements with rotating ``adv_id`` payloads for the duration of
    the measure window.

    Reads ``BLE_ADV_UPDATE_INTERVAL_S`` and ``BLE_ADV_PAYLOAD_PREFIX`` from
    ``override_env``. Stages the JSONL log in /tmp then imports it into the
    run-artifact dir as ``ble-tx-log.json`` so it lands in the bundled
    wireless-evidence tarball via the ``.json`` extension routing.
    """
    env = override_env or {}
    metrics: dict[str, str] = {"ble_mode": "advertise"}
    artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []

    if not execute_policy:
        metrics["ble_advertise_status"] = "dry_run"
        return 0, 100, "ble advertise dry run (execute_policy=false)", metrics, []

    try:
        update_interval_s = float(
            env.get("BLE_ADV_UPDATE_INTERVAL_S") or _ble_advertise.DEFAULT_UPDATE_INTERVAL_S
        )
    except (TypeError, ValueError):
        update_interval_s = _ble_advertise.DEFAULT_UPDATE_INTERVAL_S
    payload_prefix = (
        normalize(env.get("BLE_ADV_PAYLOAD_PREFIX")) or _ble_advertise.DEFAULT_PAYLOAD_PREFIX
    )

    duration_s = max(1.0, (timeout_ms / 1000.0) - 1.0)

    stamp = int(time.time())
    log_scratch = f"/tmp/ble-tx-log-{run_id}-{stamp}.json"

    handles = _ble_advertise.start_advertise(
        log_path=log_scratch,
        duration_s=duration_s,
        update_interval_s=update_interval_s,
        payload_prefix=payload_prefix,
    )
    if handles is None:
        metrics["ble_advertise_status"] = "start_failed"
        return 1, 0, "ble advertise: could not start (bluetoothctl missing or refused)", metrics, []

    # Block until the runner thread finishes (or measure window deadline expires).
    # The runner loop honors its own deadline; we just join the thread.
    handles.runner_thread.join()
    stats = _ble_advertise.stop_advertise(handles)

    if Path(log_scratch).exists():
        try:
            log_text = Path(log_scratch).read_text(encoding="utf-8")
            artifacts.append(
                store.write_text_artifact(
                    run_id,
                    "ble-tx-log",
                    log_text,
                    suffix=".json",
                    include_timestamp=False,
                )
            )
        except Exception:
            log.exception("Failed to persist ble-tx-log.json")
        try:
            Path(log_scratch).unlink()
        except Exception:
            pass

    metrics["ble_advertise_status"] = "ok"
    metrics["ble_tx_count"] = str(stats["tx_count"])
    metrics["ble_adv_count"] = str(stats["tx_count"])
    metrics["ble_advertise_elapsed_s"] = f"{stats['elapsed_s']:.2f}"
    metrics["ble_advertise_update_interval_s"] = f"{update_interval_s:.2f}"
    duration_ms = int(stats["elapsed_s"] * 1000)
    return 0, duration_ms, f"advertised {stats['tx_count']} adv_ids over {duration_ms}ms", metrics, artifacts


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
    # Phase 3: branch on BLE_SCAN_MODE before any default-path work so an
    # `advertise` policy gets routed to the bluetoothctl-driven advertise
    # branch without the legacy `bluetoothctl scan on` running first.
    ble_mode = normalize((override_env or {}).get("BLE_SCAN_MODE")).lower()
    if ble_mode == "advertise":
        return collect_ble_advertise(
            timeout_ms=timeout_ms,
            execute_policy=execute_policy,
            store=store,
            run_id=run_id,
            cmd_id=cmd_id,
            override_env=override_env or {},
        )

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
            artifacts.append(
                store.write_text_artifact(
                    run_id,
                    "ble-command-override-output",
                    full,
                    include_timestamp=False,
                )
            )
        except Exception:
            log.exception("Failed to persist ble override output artifact")

        adv_count, avg_rssi = _parse_bluetoothctl_scan(full)
        if adv_count > 0 or avg_rssi is not None:
            metrics["ble_adv_count"] = str(max(0, int(adv_count)))
            if avg_rssi is not None:
                metrics["ble_avg_rssi_dbm"] = f"{avg_rssi:.2f}"
            metrics["ble_scan_ok"] = "1"
            metrics.update(_collect_device_status_metrics())
            return exit_code, duration_ms, short or "ble scan override", metrics, artifacts

    if not execute_policy:
        duration_ms = min(timeout_ms, 1000)
        time.sleep(min(1.0, max(0.1, duration_ms / 1000.0)))
        ble_adv_count = max(1, int(duration_ms / 200) + 5)
        sim_metrics = _collect_device_status_metrics()
        sim_metrics.update({"ble_adv_count": str(ble_adv_count), "ble_avg_rssi_dbm": "-60.0", "ble_scan_ok": "1"})
        return (
            0,
            duration_ms,
            "simulated ble scan",
            sim_metrics,
            [],
        )

    # Run bluetoothctl keeping stdin open for the full scan window.
    # IMPORTANT: calling communicate() closes stdin immediately, which sends
    # EOF to bluetoothctl causing it to exit in ~14 ms instead of scanning.
    # Fix: keep stdin open, drain stdout via a background thread to prevent
    # pipe-buffer deadlock, then send "scan off\nquit\n" after the window.
    try:
        from . import prom_exposition as _prom_ble
    except Exception:
        _prom_ble = None
    if _prom_ble is not None and getattr(_prom_ble, "ble_scan_active", None) is not None:
        try:
            _prom_ble.ble_scan_active.set(1.0)
        except Exception:
            pass
    import subprocess as _subprocess
    import threading as _threading
    _scan_timeout_s = max(1, int(timeout_ms / 1000))
    _t0 = time.time()
    _live_adv_count = 0
    _live_rssi_sum = 0.0
    _live_rssi_count = 0
    _live_saw_name_event = False
    _live_seen_addrs: set[str] = set()
    try:
        _btproc = _subprocess.Popen(
            ["bluetoothctl"],
            stdin=_subprocess.PIPE,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        # Disable duplicate-advertisement filtering so BlueZ reports every
        # [CHG] Name: event when the advertiser MAC is static. Without this,
        # BlueZ suppresses repeated PDUs from the same address after first discovery.
        _btproc.stdin.write("power on\nmenu scan\nduplicate-data off\nback\nscan on\n")
        _btproc.stdin.flush()
        # Drain stdout continuously so the pipe buffer never fills and deadlocks,
        # and update Prometheus live as advertisements arrive.
        _lines: list[str] = []

        def _observe_ble_line(line: str) -> None:
            nonlocal _live_adv_count, _live_rssi_sum, _live_rssi_count
            nonlocal _live_saw_name_event, _live_seen_addrs
            _name_match = re.search(
                r"\[(?:NEW|CHG)\]\s+Device\s+[0-9A-F]{2}(?::[0-9A-F]{2}){5}\s+Name:",
                line,
                flags=re.IGNORECASE,
            )
            _addr_match = re.search(
                r"\[(?:NEW|CHG)\]\s+Device\s+([0-9A-F]{2}(?::[0-9A-F]{2}){5})\b",
                line,
                flags=re.IGNORECASE,
            )
            _should_increment = False
            if _name_match:
                _live_saw_name_event = True
                _should_increment = True
            elif _addr_match and not _live_saw_name_event:
                _addr = _addr_match.group(1).lower()
                if _addr not in _live_seen_addrs:
                    _live_seen_addrs.add(_addr)
                    _should_increment = True
            if _should_increment:
                _live_adv_count += 1
                if _prom_ble is not None and getattr(_prom_ble, "ble_rx_total", None) is not None:
                    try:
                        _prom_ble.ble_rx_total.inc()
                    except Exception:
                        pass
            _rssi_match = re.search(r"\bRSSI:\s*(-?\d+(?:\.\d+)?)\b", line, flags=re.IGNORECASE)
            if _rssi_match:
                try:
                    _live_rssi_sum += float(_rssi_match.group(1))
                    _live_rssi_count += 1
                except Exception:
                    pass

        def _drain() -> None:
            assert _btproc.stdout is not None
            for line in _btproc.stdout:
                _lines.append(line)
                _observe_ble_line(line)
        _drain_thread = _threading.Thread(target=_drain, daemon=True)
        _drain_thread.start()
        # Wait for the scan window, checking for early exit every 0.5 s.
        _waited = 0.0
        while _waited < _scan_timeout_s:
            if _btproc.poll() is not None:
                break
            time.sleep(min(0.5, _scan_timeout_s - _waited))
            _waited += 0.5
        # Graceful stop.
        try:
            _btproc.stdin.write("scan off\nquit\n")
            _btproc.stdin.flush()
            _btproc.stdin.close()
        except Exception:
            pass
        try:
            _btproc.wait(timeout=5)
        except _subprocess.TimeoutExpired:
            _btproc.kill()
            try:
                _btproc.wait(timeout=2)
            except Exception:
                pass
        _drain_thread.join(timeout=3)
        out = "".join(_lines)
        exit_code = _btproc.returncode if _btproc.returncode is not None else 0
        if "discovering: yes" not in out.lower() and "discovery started" not in out.lower():
            log.warning(
                "ble_scan: BLE discovery may not have started — 'Discovery started' missing from "
                "bluetoothctl output; check hci0 adapter state. output_head=%r",
                out[:300],
            )
    except FileNotFoundError:
        out = "missing: bluetoothctl"
        exit_code = 127
    except Exception as _ble_exc:
        out = str(_ble_exc)
        exit_code = 1
    duration_ms = int((time.time() - _t0) * 1000)
    # Mark scan as finished in Prometheus.
    if _prom_ble is not None and getattr(_prom_ble, "ble_scan_active", None) is not None:
        try:
            _prom_ble.ble_scan_active.set(0.0)
        except Exception:
            pass
    parsed_adv_count, parsed_avg_rssi = _parse_bluetoothctl_scan(out)
    adv_count = max(_live_adv_count, parsed_adv_count)
    if _live_adv_count == 0 and parsed_adv_count > 0:
        if _prom_ble is not None and getattr(_prom_ble, "ble_rx_total", None) is not None:
            try:
                _prom_ble.ble_rx_total.inc(parsed_adv_count)
            except Exception:
                pass
    avg_rssi = (_live_rssi_sum / _live_rssi_count) if _live_rssi_count > 0 else parsed_avg_rssi
    metrics["ble_adv_count"] = str(max(0, int(adv_count)))
    if avg_rssi is not None:
        metrics["ble_avg_rssi_dbm"] = f"{avg_rssi:.2f}"
    # bluetoothctl scan may be intentionally terminated by timeout; treat that as OK if output was captured.
    metrics["ble_scan_ok"] = "1" if exit_code in (0, 124) else "0"
    metrics.update(_collect_device_status_metrics())
    msg = f"bluetoothctl scan {'ok' if exit_code in (0,124) else 'failed'}: adv_count={adv_count}"
    try:
        artifacts.append(
            store.write_text_artifact(
                run_id,
                "ble-discovery-raw-bluetoothctl-scan",
                out,
                suffix=".log",
                include_timestamp=False,
            )
        )
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
    metrics: dict[str, str] = {
        "csi_supported": "0",
        "csi_frames_count": "0",
        "csi_collector_configured": "0",
        "csi_capture_ok": "0",
        "csi_capture_disabled": "0",
        "csi_capture_timeout": "0",
        "csi_capture_evidence_ok": "0",
        "csi_collector_sudo_noninteractive": "0",
        "csi_collector_sudo_prompt": "0",
        "csi_cleanup_kills": "0",
    }
    artifacts: list[fleet_gateway_v2_pb2.ArtifactRef] = []
    cfg = {normalize(k): normalize(v) for k, v in (env or {}).items() if normalize(k)}
    csi_hard_timeout_s = max(0, parse_int(cfg.get("CSI_HARD_TIMEOUT_S") or os.environ.get("CSI_HARD_TIMEOUT_S"), 0))
    csi_cleanup_before_capture = parse_bool(
        cfg.get("CSI_CLEANUP_BEFORE_CAPTURE") or os.environ.get("CSI_CLEANUP_BEFORE_CAPTURE"),
        True,
    )
    csi_cleanup_after_capture = parse_bool(
        cfg.get("CSI_CLEANUP_AFTER_CAPTURE") or os.environ.get("CSI_CLEANUP_AFTER_CAPTURE"),
        True,
    )
    csi_kill_patterns = normalize(cfg.get("CSI_KILL_PATTERNS") or os.environ.get("CSI_KILL_PATTERNS"))
    csi_traffic_enable = parse_bool(
        cfg.get("CSI_TRAFFIC_ENABLE") or os.environ.get("CSI_TRAFFIC_ENABLE"),
        False,
    )
    csi_traffic_iface = normalize(
        cfg.get("CSI_TRAFFIC_IFACE")
        or os.environ.get("CSI_TRAFFIC_IFACE")
        or os.environ.get("WIFI_SCAN_IFACE")
        or os.environ.get("CONTROL_PLANE_IFACE")
        or "wlan0"
    )
    csi_traffic_interval_ms = max(
        20,
        parse_int(
            cfg.get("CSI_TRAFFIC_INTERVAL_MS") or os.environ.get("CSI_TRAFFIC_INTERVAL_MS"),
            100,
        ),
    )

    if parse_bool(cfg.get("DISABLE_CSI_CAPTURE") or os.environ.get("DISABLE_CSI_CAPTURE"), False):
        metrics["csi_capture_disabled"] = "1"
        try:
            artifacts.append(
                store.write_text_artifact(
                    run_id,
                    "csi-status-disabled",
                    "CSI capture disabled by configuration (DISABLE_CSI_CAPTURE=true).\n",
                    include_timestamp=False,
                )
            )
        except Exception:
            log.exception("Failed to persist csi disabled note artifact")
        metrics.update(_collect_device_status_metrics())
        return 0, 0, "csi capture disabled", metrics, artifacts

    if not execute_policy:
        duration_ms = min(timeout_ms, 1000)
        time.sleep(min(1.0, max(0.1, duration_ms / 1000.0)))
        frames = max(1, int(duration_ms / 10) + 50)
        metrics["csi_supported"] = "1"
        metrics["csi_frames_count"] = str(frames)
        metrics["csi_collector_configured"] = "1"
        metrics["csi_capture_ok"] = "1"
        metrics["csi_capture_evidence_ok"] = "1"
        metrics.update(_collect_device_status_metrics())
        return 0, duration_ms, "simulated csi capture", metrics, []

    collector_cmdline = ""
    if cmdline and not _is_trivial_cmdline(cmdline):
        collector_cmdline = cmdline
    if not collector_cmdline:
        collector_cmdline = normalize(cfg.get("CSI_COLLECTOR_CMD") or os.environ.get("CSI_COLLECTOR_CMD"))

    collector_argv: list[str] = [normalize(a) for a in (argv or []) if normalize(a)]
    if not collector_argv:
        parsed = normalize(cfg.get("CSI_COLLECTOR_ARGV") or os.environ.get("CSI_COLLECTOR_ARGV"))
        if parsed:
            try:
                collector_argv = [normalize(a) for a in shlex.split(parsed) if normalize(a)]
            except Exception:
                collector_argv = []

    output_patterns = []
    for key in ("CSI_OUTPUT_FILE", "CSI_OUTPUT_PATH", "CSI_OUTPUT_GLOB", "CSI_OUTPUT_GLOBS"):
        raw = normalize(cfg.get(key) or os.environ.get(key))
        if not raw:
            continue
        output_patterns.extend([normalize(row) for row in raw.split(",") if normalize(row)])
    output_patterns.extend(_extract_output_paths_from_command(collector_cmdline, collector_argv))

    frame_pattern = normalize(cfg.get("CSI_FRAMES_REGEX") or os.environ.get("CSI_FRAMES_REGEX"))
    if not frame_pattern:
        frame_pattern = r"\b(?:frames|csi_frames|csi_frames_count)\s*[=:]\s*(\d+)\b"
    min_frames_required = max(0, parse_int(cfg.get("CSI_MIN_FRAMES") or os.environ.get("CSI_MIN_FRAMES"), 0))
    min_output_files_required = max(
        0,
        parse_int(cfg.get("CSI_MIN_OUTPUT_FILES") or os.environ.get("CSI_MIN_OUTPUT_FILES"), 0),
    )
    require_evidence = parse_bool(cfg.get("CSI_REQUIRE_EVIDENCE") or os.environ.get("CSI_REQUIRE_EVIDENCE"), False)
    force_noninteractive_sudo = parse_bool(
        cfg.get("CSI_SUDO_NONINTERACTIVE") or os.environ.get("CSI_SUDO_NONINTERACTIVE"),
        True,
    )

    def parse_frames_from_text(payload: str) -> int | None:
        if not payload:
            return None
        candidates: list[int] = []
        try:
            for match in re.finditer(frame_pattern, payload, flags=re.IGNORECASE):
                groups = match.groups()
                value = groups[-1] if groups else match.group(0)
                parsed = _safe_int(value)
                if parsed is not None:
                    candidates.append(parsed)
        except re.error:
            pass
        # Conservative fallback for common log forms.
        for fallback in (
            r"\bCSI\s+frames\s*:\s*(\d+)\b",
            r"\bcsi[_ -]?packets?\s*[=:]\s*(\d+)\b",
            r"\bpackets\s*captured\s*[=:]\s*(\d+)\b",
        ):
            for match in re.finditer(fallback, payload, flags=re.IGNORECASE):
                parsed = _safe_int(match.group(1))
                if parsed is not None:
                    candidates.append(parsed)
        return max(candidates) if candidates else None

    def parse_frames_from_feitcsi_binary(path: Path) -> int | None:
        # FeitCSI .dat stream is a sequence of [272-byte header][csiDataSize bytes].
        header_len = 272
        try:
            file_size = max(0, int(path.stat().st_size))
        except Exception:
            return None
        if file_size < (header_len + 4):
            return None

        frames = 0
        consumed = 0
        try:
            with path.open("rb") as handle:
                while consumed + 4 <= file_size:
                    raw_size = handle.read(4)
                    if len(raw_size) < 4:
                        break
                    consumed += 4

                    csi_data_size = int.from_bytes(raw_size, byteorder="little", signed=False)
                    record_size = header_len + csi_data_size
                    if csi_data_size <= 0 or record_size <= header_len:
                        break

                    remaining = file_size - consumed
                    skip = record_size - 4
                    if skip > remaining:
                        break

                    handle.seek(skip, os.SEEK_CUR)
                    consumed += skip
                    frames += 1
        except Exception:
            return None

        return frames if frames > 0 else None

    def expand_output_paths(patterns: list[str]) -> list[Path]:
        results: list[Path] = []
        seen: set[str] = set()
        for pattern in patterns:
            expanded = os.path.expandvars(os.path.expanduser(pattern))
            matches = glob.glob(expanded)
            if not matches and Path(expanded).exists():
                matches = [expanded]
            for match in matches:
                p = Path(match)
                if not p.exists() or not p.is_file():
                    continue
                rp = str(p.resolve())
                if rp in seen:
                    continue
                seen.add(rp)
                results.append(p)
        results.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        max_files = max(1, parse_int(cfg.get("CSI_OUTPUT_MAX_FILES") or os.environ.get("CSI_OUTPUT_MAX_FILES"), 5))
        return results[:max_files]

    def _collector_tokens(cmdline_text: str, argv_items: list[str]) -> list[str]:
        clean_argv = [normalize(a) for a in (argv_items or []) if normalize(a)]
        if clean_argv:
            return clean_argv
        try:
            return [normalize(a) for a in shlex.split(normalize(cmdline_text)) if normalize(a)]
        except Exception:
            token = normalize(cmdline_text)
            return [token] if token else []

    def _looks_like_feitcsi(tokens: list[str]) -> bool:
        for token in tokens:
            base = os.path.basename(token).lower()
            if base == "feitcsi":
                return True
        return False

    def _has_timeout_wrapper(tokens: list[str]) -> bool:
        for token in tokens:
            base = os.path.basename(token).lower()
            if base == "timeout":
                return True
        return False

    if collector_cmdline or collector_argv:
        metrics["csi_collector_configured"] = "1"
        if force_noninteractive_sudo:
            metrics["csi_collector_sudo_noninteractive"] = "1"
        traffic_proc: subprocess.Popen[str] | None = None
        traffic_target = normalize(cfg.get("CSI_TRAFFIC_TARGET") or os.environ.get("CSI_TRAFFIC_TARGET"))
        if csi_traffic_enable and not traffic_target:
            traffic_target = default_ipv4_gateway(iface=csi_traffic_iface)
        if csi_traffic_enable:
            metrics["csi_traffic_enabled"] = "1"
            metrics["csi_traffic_iface"] = csi_traffic_iface
            if traffic_target:
                metrics["csi_traffic_target"] = traffic_target
            else:
                metrics["csi_traffic_target"] = "unresolved"
        effective_timeout_ms = max(1, int(timeout_ms))
        if csi_hard_timeout_s > 0:
            effective_timeout_ms = min(effective_timeout_ms, csi_hard_timeout_s * 1000)
            metrics["csi_hard_timeout_s"] = str(csi_hard_timeout_s)
            metrics["csi_timeout_effective_ms"] = str(effective_timeout_ms)
        collector_timeout_ms = effective_timeout_ms
        collector_cmdline_effective = collector_cmdline
        collector_argv_effective = list(collector_argv)
        collector_tokens = _collector_tokens(collector_cmdline_effective, collector_argv_effective)
        if _looks_like_feitcsi(collector_tokens) and not _has_timeout_wrapper(collector_tokens):
            timeout_s = max(1, int((effective_timeout_ms + 999) / 1000))
            timeout_prefix = ["timeout", "-s", "INT", "-k", "2s", f"{timeout_s}s"]
            if collector_argv_effective:
                collector_argv_effective = [*timeout_prefix, *collector_argv_effective]
            else:
                collector_cmdline_effective = (
                    f"{' '.join(timeout_prefix)} {collector_cmdline_effective}".strip()
                )
            collector_timeout_ms = effective_timeout_ms + 5000
            metrics["csi_collector_wrapped_timeout"] = "1"
        if csi_cleanup_before_capture:
            killed_pre, _ = cleanup_csi_collectors("pre-capture", patterns_csv=csi_kill_patterns)
            if killed_pre > 0:
                metrics["csi_cleanup_kills"] = str(max(0, parse_int(metrics.get("csi_cleanup_kills"), 0)) + killed_pre)
        if csi_traffic_enable and traffic_target:
            try:
                interval_s = max(0.02, float(csi_traffic_interval_ms) / 1000.0)
                ping_cmd = [
                    "ping",
                    "-I",
                    csi_traffic_iface,
                    "-i",
                    f"{interval_s:.2f}",
                    traffic_target,
                ]
                traffic_proc = subprocess.Popen(
                    ping_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                metrics["csi_traffic_started"] = "1"
            except Exception:
                metrics["csi_traffic_started"] = "0"
                log.debug("Failed to start CSI traffic trigger", exc_info=True)
        exit_code, duration_ms, short, full = run_command_capture(
            collector_cmdline_effective,
            collector_argv_effective,
            cfg,
            collector_timeout_ms,
            True,
            force_noninteractive_sudo=force_noninteractive_sudo,
        )
        if traffic_proc is not None:
            try:
                traffic_proc.terminate()
                traffic_proc.wait(timeout=2)
            except Exception:
                try:
                    traffic_proc.kill()
                except Exception:
                    pass
            metrics["csi_traffic_started"] = "1"
        metrics["csi_collector_exit_code"] = str(int(exit_code))
        metrics["csi_capture_ok"] = "1" if int(exit_code) == 0 else "0"
        metrics["csi_capture_timeout"] = "1" if int(exit_code) == 124 else "0"
        metrics["csi_supported"] = "1" if int(exit_code) != 127 else "0"
        if re.search(r"(a terminal is required|password is required|sudo: .*password)", full or "", flags=re.IGNORECASE):
            metrics["csi_collector_sudo_prompt"] = "1"
        if csi_cleanup_after_capture:
            killed_post, _ = cleanup_csi_collectors("post-capture", patterns_csv=csi_kill_patterns)
            if killed_post > 0:
                metrics["csi_cleanup_kills"] = str(max(0, parse_int(metrics.get("csi_cleanup_kills"), 0)) + killed_post)

        frames_found = parse_frames_from_text(full)
        imported_files = 0
        imported_bytes = 0

        if frames_found is not None:
            metrics["csi_frames_count"] = str(max(0, int(frames_found)))

        output_files = expand_output_paths(output_patterns)
        max_parse_bytes = max(4096, parse_int(cfg.get("CSI_PARSE_MAX_BYTES") or os.environ.get("CSI_PARSE_MAX_BYTES"), 1024 * 1024))
        for idx, output_file in enumerate(output_files, start=1):
            try:
                artifacts.append(
                    store.import_file_artifact(
                        run_id,
                        output_file,
                        artifact_name=f"csi-capture-raw-data-file-{idx:03d}-{output_file.name}",
                    )
                )
                imported_files += 1
                imported_bytes += max(0, int(output_file.stat().st_size))
            except Exception:
                log.exception("Failed to import CSI output artifact path=%s", output_file)

            if frames_found is None:
                try:
                    preview = output_file.read_bytes()[:max_parse_bytes].decode("utf-8", errors="replace")
                    parsed = parse_frames_from_text(preview)
                    if parsed is not None:
                        frames_found = parsed
                    else:
                        parsed = parse_frames_from_feitcsi_binary(output_file)
                        if parsed is not None:
                            frames_found = parsed
                    if frames_found is None and output_file.suffix.lower() in {".csv", ".txt", ".log", ".ndjson", ".jsonl"}:
                        line_count = 0
                        with output_file.open("rb") as handle:
                            for line in handle:
                                if not line.strip():
                                    continue
                                line_count += 1
                                if line_count >= 2_000_000:
                                    break
                        if line_count > 1:
                            header_like = preview.splitlines()[0] if preview.splitlines() else ""
                            if header_like and re.search(r"[A-Za-z]", header_like):
                                line_count = max(0, line_count - 1)
                        if line_count > 0:
                            frames_found = line_count
                except Exception:
                    pass

        if imported_files > 0:
            metrics["csi_output_files_count"] = str(imported_files)
            metrics["csi_output_bytes_total"] = str(imported_bytes)

        if frames_found is not None:
            metrics["csi_frames_count"] = str(max(0, int(frames_found)))

        frames_value = max(0, int(metrics.get("csi_frames_count", "0") or 0))
        evidence_ok = False
        if min_frames_required > 0 or min_output_files_required > 0:
            evidence_ok = (frames_value >= min_frames_required) and (imported_files >= min_output_files_required)
        else:
            evidence_ok = frames_value > 0 or imported_files > 0
        metrics["csi_capture_evidence_ok"] = "1" if evidence_ok else "0"

        if int(exit_code) == 124 and evidence_ok:
            # Timeout is expected for long-running collectors; keep timeout marker
            # but accept capture when artifacts/frames prove data was produced.
            exit_code = 0
            metrics["csi_capture_ok"] = "1"

        if require_evidence and not evidence_ok and int(exit_code) == 0:
            exit_code = 65
            metrics["csi_capture_ok"] = "0"

        if int(metrics.get("csi_capture_ok", "0")) == 1 and (frames_found or imported_files > 0):
            metrics["csi_supported"] = "1"

        metrics.update(_collect_device_status_metrics())
        msg = short or "csi capture done"
        if metrics.get("csi_capture_timeout") == "1" and metrics.get("csi_capture_ok") == "1":
            msg = (
                f"csi collector timed out but capture evidence ok "
                f"(frames={frames_value}, files={imported_files})"
            )
        if require_evidence and not evidence_ok:
            msg = (
                f"csi evidence missing (frames={frames_value}, files={imported_files}, "
                f"min_frames={min_frames_required}, min_files={min_output_files_required})"
            )
        artifact_payload = full
        if metrics.get("csi_capture_timeout") == "1" and metrics.get("csi_capture_ok") == "1":
            summary = (
                "collector timeout tolerated because capture evidence was present "
                f"(frames={frames_value}, files={imported_files})."
            )
            body = (artifact_payload or "").strip()
            if not body or body == "command timed out":
                artifact_payload = summary + "\ncollector_output: command timed out\n"
            else:
                artifact_payload = summary + "\n\n" + body + "\n"
        try:
            artifacts.append(
                store.write_text_artifact(
                    run_id,
                    "csi-collector-output",
                    artifact_payload,
                    suffix=".log",
                    include_timestamp=False,
                )
            )
        except Exception:
            log.exception("Failed to persist csi output artifact")
        return int(exit_code), duration_ms, msg, metrics, artifacts

    # No capture tool configured.
    try:
        artifacts.append(
            store.write_text_artifact(
                run_id,
                "csi-status-not-configured",
                "CSI capture not configured on this agent.\n"
                "Set command/env CSI_COLLECTOR_CMD (and optional CSI_OUTPUT_PATH or CSI_OUTPUT_GLOB).\n",
                include_timestamp=False,
            )
        )
    except Exception:
        log.exception("Failed to persist csi note artifact")
    metrics.update(_collect_device_status_metrics())
    return 0, 0, "csi capture not configured", metrics, artifacts
