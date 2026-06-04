#!/usr/bin/env python3
"""Generate the full exp145-style WiFi figure pack from a normalized dataset."""
from __future__ import annotations

import argparse
from collections import Counter
import math
import os
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="matplotlib-"))
os.environ.setdefault("XDG_CACHE_HOME", tempfile.mkdtemp(prefix="fontconfig-"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.lines as mlines
import matplotlib.patches as patches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

try:
    from PIL import Image
except ImportError:  # pragma: no cover - optional dependency
    Image = None


SENSOR_COLORS = {
    "monad-02": "#d35454",
    "monad-03": "#4e8fe0",
    "monad-04": "#e07c4e",
    "monad-05": "#3aaa5c",
    "monad-06": "#9b59b6",
}
SENSOR_ORDER = ["monad-02", "monad-03", "monad-04", "monad-05", "monad-06"]
VENDOR_COLORS = {
    "Compal": "#3498db",
    "CommScope": "#e67e22",
    "Sagemcom": "#e74c3c",
    "TP-Link": "#16a085",
    "Virtual BSSID": "#95a5a6",
    "Unknown": "#bdc3c7",
    "Other": "#7f8c8d",
}
OUI_VENDOR = {
    "34:2C:C4": "Compal",
    "90:5C:44": "Compal",
    "68:02:B8": "Compal",
    "38:43:7D": "Compal",
    "10:FE:ED": "TP-Link",
    "AC:F8:CC": "CommScope",
    "64:FD:96": "Sagemcom",
}
IDEAL_BEACON_MS = 102.4
WEAK_SIGNAL_DBM = -70.0


def configure_plotting() -> None:
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.size": 11,
            "axes.titlesize": 13,
            "axes.titleweight": "bold",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.alpha": 0.25,
            "figure.dpi": 130,
            "savefig.dpi": 130,
            "savefig.bbox": "tight",
        }
    )


def vendor(mac: object) -> str:
    if not isinstance(mac, str) or len(mac) < 2:
        return "Unknown"
    known_vendor = OUI_VENDOR.get(mac[:8].upper())
    if known_vendor:
        return known_vendor
    try:
        is_locally_administered = int(mac[:2], 16) & 0x02
    except ValueError:
        return "Unknown"
    return "Virtual BSSID" if is_locally_administered else "Unknown"


def label_vendor(name: str) -> str:
    return "Virtual" if name == "Virtual BSSID" else ("Other" if name == "Unknown" else name)


def short_bssid(bssid: str) -> str:
    if not isinstance(bssid, str):
        return str(bssid)
    if len(bssid) < 8:
        return bssid
    return f"{bssid[:2]}..{bssid[-8:]}"


def short_label(text: str, max_len: int = 22) -> str:
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 1]}…"


def mode_or_empty(values: pd.Series) -> str:
    non_empty = values.dropna().astype(str)
    non_empty = non_empty[non_empty.str.len() > 0]
    if len(non_empty) == 0:
        return ""
    return str(non_empty.mode().iloc[0])


def ordered_sensors(df: pd.DataFrame) -> list[str]:
    present = [sensor for sensor in SENSOR_ORDER if sensor in set(df["sensor"].dropna().unique())]
    extras = sorted(set(df["sensor"].dropna().unique()) - set(present))
    return present + extras


def parse_timestamp_col(values: pd.Series) -> pd.Series:
    ts = pd.to_datetime(values, utc=True, errors="coerce")
    if ts.notna().any():
        return ts
    numeric = pd.to_numeric(values, errors="coerce")
    return pd.to_datetime(numeric, unit="s", utc=True, errors="coerce")


def load_alias_ssids(repo_root: Path) -> dict[str, str]:
    observations: list[pd.DataFrame] = []
    for frames_csv in sorted((repo_root / "artifacts" / "analysis").glob("exp*/wifi_5g_frames.csv")):
        try:
            sample = pd.read_csv(frames_csv, usecols=lambda name: name in {"bssid", "ssid"})
        except Exception:
            continue
        if "bssid" not in sample.columns or "ssid" not in sample.columns:
            continue
        sample["bssid"] = sample["bssid"].fillna("").str.upper()
        sample["ssid"] = sample["ssid"].fillna("").astype(str)
        sample = sample[(sample["bssid"].str.len() > 0) & (sample["ssid"].str.len() > 0)]
        if not sample.empty:
            observations.append(sample[["bssid", "ssid"]])
    if not observations:
        return {}
    aliases = pd.concat(observations, ignore_index=True)
    return aliases.groupby("bssid")["ssid"].agg(mode_or_empty).to_dict()


def preferred_ssid(bssid: str, ssid: str, alias_ssids: dict[str, str]) -> str:
    if isinstance(ssid, str) and ssid:
        return ssid
    return alias_ssids.get(bssid, "")


def primary_ap_label(bssid: str, ssid: str, vendor_name: str, alias_ssids: dict[str, str]) -> str:
    resolved_ssid = preferred_ssid(bssid, ssid, alias_ssids)
    if resolved_ssid:
        return resolved_ssid
    return f"{label_vendor(vendor_name)} {short_bssid(bssid)}"


def display_ap_label(bssid: str, ssid: str, vendor_name: str, alias_ssids: dict[str, str]) -> str:
    resolved_ssid = preferred_ssid(bssid, ssid, alias_ssids)
    if resolved_ssid:
        return f"{resolved_ssid} · {label_vendor(vendor_name)}"
    return primary_ap_label(bssid, ssid, vendor_name, alias_ssids)


def load_frames(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "sensor" not in df.columns and "pi_id" in df.columns:
        df["sensor"] = df["pi_id"]
    if "frame_subtype" in df.columns:
        df = df[df["frame_subtype"].isin(["Beacon", "Probe Response"])].copy()
    else:
        df = df[df["bssid"].notna()].copy()
    df["ts"] = parse_timestamp_col(df["timestamp"])
    df["rssi_dbm"] = pd.to_numeric(df["rssi_dbm"], errors="coerce")
    df["channel"] = pd.to_numeric(df["channel"], errors="coerce")
    df["bssid"] = df["bssid"].fillna("").str.upper()
    df["ssid"] = df["ssid"].fillna("").astype(str)
    df["vendor"] = df["bssid"].apply(vendor)
    return df[df["bssid"].str.len() > 0].copy()


def build_inventory(df: pd.DataFrame, sensors: list[str], alias_ssids: dict[str, str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    per_sensor = (
        df.groupby(["bssid", "sensor"])
        .agg(
            median_rssi=("rssi_dbm", "median"),
            mean_rssi=("rssi_dbm", "mean"),
            frame_count=("bssid", "size"),
            min_rssi=("rssi_dbm", "min"),
            max_rssi=("rssi_dbm", "max"),
        )
        .reset_index()
    )
    pivot = per_sensor.pivot(index="bssid", columns="sensor", values="median_rssi").reindex(columns=sensors)
    frame_pivot = per_sensor.pivot(index="bssid", columns="sensor", values="frame_count").reindex(columns=sensors, fill_value=0)

    grouped = df.groupby("bssid")
    inventory = grouped.agg(
        total_frames=("bssid", "size"),
        ssid=("ssid", mode_or_empty),
        vendor=("vendor", mode_or_empty),
        channel=("channel", lambda s: int(s.dropna().mode().iloc[0]) if s.dropna().any() else np.nan),
        mean_rssi=("rssi_dbm", "mean"),
        median_rssi=("rssi_dbm", "median"),
        std_rssi=("rssi_dbm", "std"),
        min_rssi=("rssi_dbm", "min"),
        max_rssi=("rssi_dbm", "max"),
    )
    inventory["std_rssi"] = inventory["std_rssi"].fillna(0.0)
    inventory["sensor_count"] = pivot.notna().sum(axis=1)
    inventory["shared_with_others"] = inventory["sensor_count"] > 1
    inventory["delta"] = pivot.max(axis=1, skipna=True) - pivot.min(axis=1, skipna=True)
    inventory["closest_sensor"] = pivot.idxmax(axis=1, skipna=True)
    inventory["worst_sensor"] = pivot.idxmin(axis=1, skipna=True)
    inventory["detected_sensors"] = frame_pivot.gt(0).sum(axis=1)
    inventory["exclusive_sensor"] = ""
    for bssid, row in frame_pivot.iterrows():
        active = [sensor for sensor, value in row.items() if pd.notna(value) and value > 0]
        inventory.at[bssid, "exclusive_sensor"] = active[0] if len(active) == 1 else ""
    inventory["best_ssid"] = [
        preferred_ssid(bssid, row["ssid"], alias_ssids)
        for bssid, row in inventory.iterrows()
    ]
    inventory["primary_label"] = [
        primary_ap_label(bssid, row["ssid"], row["vendor"], alias_ssids)
        for bssid, row in inventory.iterrows()
    ]
    inventory["display_name"] = [
        display_ap_label(bssid, row["ssid"], row["vendor"], alias_ssids)
        for bssid, row in inventory.iterrows()
    ]
    duplicate_ssids = inventory.loc[inventory["best_ssid"] != "", "best_ssid"].value_counts()
    duplicate_ssids = set(duplicate_ssids[duplicate_ssids > 1].index)
    for bssid, row in inventory.iterrows():
        if row["best_ssid"] not in duplicate_ssids:
            continue
        primary = f"{row['best_ssid']} [{short_bssid(bssid)}]"
        inventory.at[bssid, "primary_label"] = primary
        inventory.at[bssid, "display_name"] = f"{primary} · {label_vendor(row['vendor'])}"
    inventory = inventory.sort_values(["total_frames", "max_rssi"], ascending=[False, False])
    return inventory, pivot


def capture_label(run_id: str, duration_s: float) -> str:
    if run_id == "exp145":
        return "8 h overnight"
    if run_id == "exp153":
        return "1 h BLE proof"
    if run_id == "exp156":
        return "15 min apartment preflight"
    if run_id == "exp158":
        return "2 h apartment rectangle"
    if duration_s >= 3600:
        return f"{duration_s / 3600:.1f} h capture"
    return f"{duration_s / 60:.1f} min capture"


def save_01_rf_environment(inventory: pd.DataFrame, output_dir: Path, run_id: str, capture_text: str) -> None:
    vendor_order = ["Compal", "CommScope", "Sagemcom", "TP-Link", "Virtual BSSID", "Unknown"]
    env = (
        inventory.groupby(["channel", "vendor"])
        .size()
        .unstack(fill_value=0)
        .reindex(columns=[v for v in vendor_order if v in inventory["vendor"].unique()], fill_value=0)
        .sort_index()
    )
    totals = env.sum(axis=1)
    fig, ax = plt.subplots(figsize=(12, 5))
    bottom = np.zeros(len(env))
    for vendor_name in env.columns:
        ax.bar(
            env.index.astype(int).astype(str),
            env[vendor_name].values,
            bottom=bottom,
            color=VENDOR_COLORS.get(vendor_name, "#7f8c8d"),
            edgecolor="white",
            label=vendor_name,
        )
        bottom += env[vendor_name].values
    for idx, total in enumerate(totals.values):
        ax.text(idx, total + 0.3, f"{int(total)}", ha="center", va="bottom", fontsize=12, fontweight="bold")
    ax.set_ylabel("Unique APs")
    ax.set_xlabel("5 GHz Channel")
    ax.set_ylim(0, max(22, int(totals.max()) + 3))
    ax.set_title(f"5 GHz RF Environment — APs per Channel by Vendor  ({run_id}, {capture_text})")
    ax.legend(loc="upper right", ncol=2, fontsize=10)
    fig.savefig(output_dir / "01_rf_environment.png")
    plt.close(fig)


def save_02_ap_covisibility(df: pd.DataFrame, inventory: pd.DataFrame, output_dir: Path, run_id: str, sensors: list[str]) -> None:
    ap_sensors = inventory["sensor_count"]
    counts = ap_sensors.value_counts().sort_index()
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))

    colors = ["#bdc3c7", "#85c1e9", "#2e86c1", "#1a5276", "#154360"][: len(counts)]
    wedges, _ = ax1.pie(
        counts.values,
        colors=colors,
        startangle=90,
        wedgeprops={"width": 0.42, "edgecolor": "white"},
    )
    for wedge, count in zip(wedges, counts.values):
        angle = (wedge.theta2 + wedge.theta1) / 2.0
        x = 0.62 * math.cos(math.radians(angle))
        y = 0.62 * math.sin(math.radians(angle))
        ax1.text(x, y, f"{int(count)}", ha="center", va="center", fontsize=12, fontweight="bold")
    legend_labels = [
        f"{sensor_count} sensor{'s' if sensor_count > 1 else ''}: {int(ap_count)} APs ({ap_count / len(ap_sensors):.0%})"
        for sensor_count, ap_count in counts.items()
    ]
    ax1.legend(
        wedges,
        legend_labels,
        title="Visibility tier",
        loc="center",
        bbox_to_anchor=(0.5, -0.1),
        fontsize=9,
        title_fontsize=10,
        frameon=False,
        ncol=2,
    )
    ax1.set_title(f"AP Co-visibility\n({len(ap_sensors)} total APs)")
    ax1.text(0, 0, "AP\ncount", ha="center", va="center", fontsize=12, color="#5f6a6a", fontweight="bold")

    shared = inventory[inventory["sensor_count"] >= 2].index
    pivot = (
        df[df["bssid"].isin(shared)]
        .groupby(["bssid", "sensor"])
        .size()
        .unstack(fill_value=0)
        .reindex(columns=sensors, fill_value=0)
    )
    pivot["total"] = pivot.sum(axis=1)
    pivot = pivot.sort_values("total", ascending=True).tail(15)
    left = np.zeros(len(pivot))
    for sensor in sensors:
        values = pivot[sensor].values
        ax2.barh(range(len(pivot)), values, left=left, color=SENSOR_COLORS.get(sensor, "#7f8c8d"), label=sensor, alpha=0.85)
        left += values
    ylabels = [short_label(inventory.loc[bssid, "display_name"], max_len=24) for bssid in pivot.index]
    ax2.set_yticks(range(len(pivot)))
    ax2.set_yticklabels(ylabels, fontsize=9)
    ax2.set_xlabel("Beacon / probe-response frames")
    ax2.set_title("APs seen by >=2 sensors\n(top 15 by frame count)")
    ax2.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    ax2.legend(loc="lower right", fontsize=9)

    fig.suptitle(f"AP Co-visibility Across Sensors - {run_id}", fontsize=14, fontweight="bold")
    fig.tight_layout(rect=(0, 0.05, 1, 0.95))
    fig.savefig(output_dir / "02_ap_covisibility.png")
    plt.close(fig)


def save_heatmap(path: Path, matrix: pd.DataFrame, title: str, subtitle: str, highlight_rows: list[str] | None = None) -> None:
    data = matrix.values.astype(float)
    masked = np.ma.masked_invalid(data)
    fig, ax = plt.subplots(figsize=(12, max(6, 0.7 * len(matrix) + 1.5)))
    cmap = matplotlib.colormaps["RdYlGn"].copy()
    cmap.set_bad(color="#f3f3f3")
    im = ax.imshow(masked, aspect="auto", cmap=cmap, vmin=-95, vmax=-25)

    for row in range(data.shape[0]):
        for col in range(data.shape[1]):
            value = data[row, col]
            if math.isnan(value):
                ax.text(col, row, "—", ha="center", va="center", fontsize=15, color="#bbbbbb")
            else:
                text_color = "white" if value <= -84 else "black"
                ax.text(col, row, f"{int(round(value))}", ha="center", va="center", fontsize=12, fontweight="bold", color=text_color)

    ax.set_xticks(range(len(matrix.columns)))
    ax.set_xticklabels([sensor.replace("monad-", "M") for sensor in matrix.columns], fontsize=15)
    ax.set_yticks(range(len(matrix.index)))
    ax.set_yticklabels(matrix.index, fontsize=11)
    ax.set_xlabel("Sensor", fontsize=16)
    ax.set_ylabel("Access Point", fontsize=16)
    ax.set_title(f"{title}\n{subtitle}", fontsize=22, pad=12)
    if highlight_rows:
        index_map = {name: idx for idx, name in enumerate(matrix.index)}
        for row_name in highlight_rows:
            row_idx = index_map.get(row_name)
            if row_idx is None:
                continue
            rect = patches.Rectangle(
                (-0.5, row_idx - 0.5),
                len(matrix.columns),
                1,
                linewidth=2.5,
                edgecolor="#27ae60",
                facecolor="none",
            )
            ax.add_patch(rect)
    cbar = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cbar.set_label("Median RSSI (dBm)", fontsize=16)
    fig.savefig(path)
    plt.close(fig)


def save_03_rssi_fingerprint(inventory: pd.DataFrame, pivot: pd.DataFrame, output_dir: Path) -> None:
    top = inventory[inventory["sensor_count"] >= 2].head(9)
    matrix = pivot.loc[top.index].copy()
    matrix.index = top["display_name"].tolist()
    save_heatmap(
        output_dir / "03_rssi_fingerprint_heatmap.png",
        matrix,
        "RSSI Fingerprint — Median Signal per AP per Sensor",
        "(green = strong · red = weak · \"—\" = not detected)",
    )


def stability_bin_size(duration_s: float) -> tuple[str, str]:
    if duration_s >= 4 * 3600:
        return "30min", "30-min"
    if duration_s >= 90 * 60:
        return "15min", "15-min"
    return "5min", "5-min"


def save_04_rssi_stability(df: pd.DataFrame, inventory: pd.DataFrame, output_dir: Path, sensors: list[str], duration_s: float) -> None:
    top = inventory[inventory["sensor_count"] >= 2].head(6)
    if top.empty:
        return
    freq, label = stability_bin_size(duration_s)
    fig, axes = plt.subplots(2, 3, figsize=(15, 8), sharey=True)
    axes = axes.flatten()
    for ax, (bssid, row) in zip(axes, top.iterrows()):
        subset = df[df["bssid"] == bssid].copy()
        subset["bucket"] = subset["ts"].dt.floor(freq)
        series = (
            subset.groupby(["bucket", "sensor"])["rssi_dbm"]
            .median()
            .unstack()
            .reindex(columns=sensors)
        )
        for sensor in sensors:
            if sensor not in series:
                continue
            s = series[sensor].dropna()
            if s.empty:
                continue
            ax.plot(s.index, s.values, linewidth=1.5, color=SENSOR_COLORS.get(sensor, "#7f8c8d"), label=sensor)
        ax.axhline(WEAK_SIGNAL_DBM, color="#e74c3c", linestyle="--", linewidth=1, alpha=0.6)
        title = short_label(row["primary_label"], max_len=22)
        subtitle = label_vendor(row["vendor"]) if row["best_ssid"] else f"BSSID {short_bssid(bssid)}"
        ax.set_title(f"{title}\n{subtitle}", fontsize=10)
        ax.tick_params(axis="x", labelrotation=35, labelsize=7)
        ax.set_ylim(-100, -20)
    for ax in axes[len(top) :]:
        ax.axis("off")
    axes[0].set_ylabel("RSSI (dBm)")
    axes[3].set_ylabel("RSSI (dBm)")
    handles = [mlines.Line2D([], [], color=SENSOR_COLORS.get(sensor, "#7f8c8d"), label=sensor) for sensor in sensors]
    fig.legend(handles=handles, loc="lower center", ncol=len(sensors), bbox_to_anchor=(0.5, 0.01), fontsize=9)
    fig.suptitle(f"RSSI Stability Over Time — Top 6 APs ({label} median per sensor)", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0.04, 1, 0.95))
    fig.savefig(output_dir / "04_rssi_stability.png")
    plt.close(fig)


def save_05_sensor_comparison(df: pd.DataFrame, inventory: pd.DataFrame, output_dir: Path, sensors: list[str], run_id: str, capture_text: str) -> None:
    per_sensor_bssid = {
        sensor: set(df.loc[df["sensor"] == sensor, "bssid"].dropna())
        for sensor in sensors
    }
    total_aps = pd.Series({sensor: len(items) for sensor, items in per_sensor_bssid.items()})
    exclusive_aps = pd.Series(
        {
            sensor: sum(1 for bssid in items if inventory.loc[bssid, "sensor_count"] == 1)
            for sensor, items in per_sensor_bssid.items()
        }
    )
    shared_aps = total_aps - exclusive_aps
    frame_counts = df.groupby("sensor").size().reindex(sensors).fillna(0)
    sensor_median = df.groupby("sensor")["rssi_dbm"].median().reindex(sensors)
    q1 = df.groupby("sensor")["rssi_dbm"].quantile(0.25).reindex(sensors)
    q3 = df.groupby("sensor")["rssi_dbm"].quantile(0.75).reindex(sensors)

    fig, axes = plt.subplots(1, 3, figsize=(16, 5))

    x = np.arange(len(sensors))
    axes[0].bar(x, shared_aps.values, color="#d0dff3", edgecolor="white", label="Shared with others")
    axes[0].bar(x, exclusive_aps.values, bottom=shared_aps.values, color="#6c95d3", edgecolor="white", label="Exclusive to this sensor")
    axes[0].set_title("AP Coverage per Sensor")
    axes[0].set_ylabel("Unique APs detected")
    axes[0].set_xticks(x)
    axes[0].set_xticklabels([sensor.replace("monad-", "M") for sensor in sensors])
    for idx, sensor in enumerate(sensors):
        axes[0].text(idx, total_aps[sensor] + 0.5, f"{int(total_aps[sensor])}\n({int(exclusive_aps[sensor])} excl.)", ha="center", va="bottom", fontsize=10, fontweight="bold")
    axes[0].legend(fontsize=8, loc="upper right")

    colors = [SENSOR_COLORS.get(sensor, "#7f8c8d") for sensor in sensors]
    axes[1].bar(x, frame_counts.values / 1000.0, color=colors, edgecolor="white", alpha=0.85)
    axes[1].set_title("Total Frames Captured")
    axes[1].set_ylabel("Beacon frames (thousands)")
    axes[1].set_xticks(x)
    axes[1].set_xticklabels([sensor.replace("monad-", "M") for sensor in sensors])
    for idx, value in enumerate(frame_counts.values):
        axes[1].text(idx, value / 1000.0 + 0.8, f"{int(round(value / 1000.0))}K", ha="center", va="bottom", fontsize=10, fontweight="bold")

    axes[2].bar(x, sensor_median.values, color=colors, alpha=0.8, edgecolor="white")
    lower = sensor_median.values - q1.values
    upper = q3.values - sensor_median.values
    axes[2].errorbar(x, sensor_median.values, yerr=[lower, upper], fmt="none", ecolor="black", capsize=4, linewidth=1.2)
    axes[2].axhline(WEAK_SIGNAL_DBM, color="#e74c3c", linestyle="--", linewidth=1, label="-70 dBm (weak)")
    axes[2].set_title("Median RSSI (bar ± IQR)")
    axes[2].set_ylabel("RSSI (dBm)")
    axes[2].set_xticks(x)
    axes[2].set_xticklabels([sensor.replace("monad-", "M") for sensor in sensors])
    axes[2].legend(fontsize=8, loc="upper right")

    fig.suptitle(f"Sensor Comparison — {run_id} ({capture_text})", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(output_dir / "05_sensor_comparison.png")
    plt.close(fig)


def save_06_landmark_quality(inventory: pd.DataFrame, output_dir: Path) -> None:
    plot_df = inventory[inventory["sensor_count"] >= 1].copy()
    fig, ax = plt.subplots(figsize=(12, 8))
    ax.axvspan(-70, -20, color="#27ae60", alpha=0.08)
    ax.axhspan(0, 4, color="#27ae60", alpha=0.08)
    sizes = 30 + (plot_df["total_frames"] / plot_df["total_frames"].max()) * 2500
    scatter = ax.scatter(
        plot_df["mean_rssi"],
        plot_df["std_rssi"],
        s=sizes,
        c=plot_df["sensor_count"],
        cmap="viridis",
        alpha=0.7,
        edgecolors="#2c3e50",
        linewidths=1,
    )
    labels = plot_df.sort_values("mean_rssi", ascending=False).head(8)
    for idx, (_, row) in enumerate(labels.iterrows()):
        offset_y = 0.15 + (idx % 2) * 0.45
        ax.text(
            row["mean_rssi"] + 0.8,
            row["std_rssi"] + offset_y,
            short_label(row["primary_label"], max_len=20),
            fontsize=10,
            bbox=dict(facecolor="white", alpha=0.78, edgecolor="none", pad=1.5),
        )
    ax.set_xlim(-100, -20)
    ax.set_ylim(0, max(15, float(plot_df["std_rssi"].max()) + 1.5))
    ax.set_xlabel("Mean RSSI (dBm) → stronger →", fontsize=14)
    ax.set_ylabel("RSSI std-dev (dB) → more variable →", fontsize=14)
    ax.set_title(
        "AP Landmark Quality Map\nbubble size = frame count · color = # sensors detecting · top-8 strongest labelled",
        fontsize=16,
        fontweight="bold",
    )
    ax.text(-36.5, 0.5, "← strong & stable\n(good landmark)", color="#1e8449", fontsize=12, fontweight="bold")
    cbar = fig.colorbar(scatter, ax=ax, fraction=0.03, pad=0.03)
    cbar.set_label("# sensors that detect this AP", fontsize=14)
    fig.savefig(output_dir / "06_landmark_quality.png")
    plt.close(fig)


def save_07_positioning_signal(inventory: pd.DataFrame, output_dir: Path, sensors: list[str]) -> None:
    plot_df = inventory[inventory["sensor_count"] >= 2].copy().sort_values("delta", ascending=False).head(10)
    if plot_df.empty:
        return
    colors = [SENSOR_COLORS.get(sensor, "#7f8c8d") for sensor in plot_df["closest_sensor"]]
    labels = [short_label(value, max_len=34) for value in plot_df["display_name"].tolist()]
    values = plot_df["delta"].values
    fig, ax = plt.subplots(figsize=(13.5, 6))
    y = np.arange(len(plot_df))
    ax.barh(y, values, color=colors, edgecolor="white", alpha=0.85)
    ax.invert_yaxis()
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=12)
    ax.set_xlabel("RSSI delta (dB)  =  best sensor  —  worst sensor", fontsize=14)
    ax.set_title(
        "Positioning Signal Strength per AP\nbar colour = sensor that sees the AP strongest (i.e. closest)",
        fontsize=18,
        fontweight="bold",
        pad=12,
    )
    for threshold in (10, 20):
        ax.axvline(threshold, color="#888888", linestyle="--", linewidth=1)
        ax.text(threshold + 0.2, -0.6, f"{threshold} dB", color="#888888", fontsize=10)
    for idx, value in enumerate(values):
        ax.text(value + 0.3, idx, f"{int(round(value))} dB", va="center", ha="left", fontsize=12, fontweight="bold")
    closest_counts = Counter(plot_df["closest_sensor"].dropna())
    handles = []
    missing = []
    for sensor in sensors:
        count = int(closest_counts.get(sensor, 0))
        if count == 0:
            missing.append(sensor.replace("monad-", "M"))
        handles.append(
            patches.Patch(
                color=SENSOR_COLORS.get(sensor, "#7f8c8d"),
                alpha=0.9 if count > 0 else 0.25,
                label=f"{sensor.replace('monad-', 'M')}: {count} AP{'s' if count != 1 else ''} closest",
            )
        )
    ax.legend(handles=handles, title="Closest sensor", loc="lower right", fontsize=10, title_fontsize=12)
    if missing:
        ax.text(
            0.995,
            0.02,
            f"No shared-AP wins for {', '.join(missing)}",
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=9,
            color="#5f6a6a",
        )
    fig.savefig(output_dir / "07_positioning_signal.png")
    plt.close(fig)


def save_08_beacon_timing(df: pd.DataFrame, inventory: pd.DataFrame, output_dir: Path, sensors: list[str]) -> None:
    top_bssids = inventory.head(5).index.tolist()
    subset = df[df["bssid"].isin(top_bssids)].copy().sort_values(["sensor", "bssid", "ts"])
    subset["delta_ms"] = subset.groupby(["sensor", "bssid"])["ts"].diff().dt.total_seconds() * 1000.0
    timing = subset[(subset["delta_ms"] > 0) & (subset["delta_ms"] <= 500)].copy()
    if timing.empty:
        return
    sensor_medians = timing.groupby("sensor")["delta_ms"].median().reindex(sensors)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    axes[0].hist(timing["delta_ms"], bins=np.arange(0, 505, 5), color="#5dade2", edgecolor="white")
    axes[0].axvline(100, color="#e74c3c", linestyle="--", linewidth=2, label="100 ms (typical)")
    axes[0].axvline(IDEAL_BEACON_MS, color="#58d68d", linestyle="--", linewidth=2, label="102.4 ms (TU·100)")
    axes[0].set_xlim(0, 500)
    axes[0].set_xlabel("Inter-arrival time between consecutive beacons (ms)")
    axes[0].set_ylabel("Count")
    axes[0].set_title(f"Beacon Inter-arrival Distribution\n(top 5 APs across all sensors, n={len(timing):,})")
    axes[0].legend(fontsize=10)

    x = np.arange(len(sensors))
    colors = [SENSOR_COLORS.get(sensor, "#7f8c8d") for sensor in sensors]
    axes[1].bar(x, sensor_medians.values, color=colors, edgecolor="white", alpha=0.85)
    axes[1].axhline(IDEAL_BEACON_MS, color="#58d68d", linestyle="--", linewidth=2, label="102.4 ms (ideal)")
    for idx, value in enumerate(sensor_medians.values):
        if pd.isna(value):
            continue
        axes[1].text(idx, value + 1.0, f"{int(round(value))} ms", ha="center", va="bottom", fontsize=12, fontweight="bold")
    axes[1].set_xticks(x)
    axes[1].set_xticklabels([sensor.replace("monad-", "M") for sensor in sensors])
    axes[1].set_ylabel("Median inter-arrival (ms)")
    axes[1].set_ylim(0, max(150, float(np.nanmax(sensor_medians.values)) + 20))
    axes[1].set_title("Median Beacon Spacing per Sensor\n(closer to 102.4 = fewer dropped beacons)")
    axes[1].legend(fontsize=10)

    fig.suptitle("Capture Quality Validation — Beacon Inter-arrival Times", fontsize=18, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(output_dir / "08_beacon_timing.png")
    plt.close(fig)


def save_09_channel_coverage(df: pd.DataFrame, output_dir: Path, sensors: list[str], note: str | None = None) -> None:
    ch_per_sensor = df.groupby(["sensor", "channel"]).size().unstack(fill_value=0)
    ch_per_sensor = ch_per_sensor.reindex(sensors)
    ch_per_sensor = ch_per_sensor.loc[:, ch_per_sensor.max(axis=0) > 50]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.5), gridspec_kw={"width_ratios": [1.4, 1]})
    data = ch_per_sensor.values.astype(float)
    data_log = np.log10(data + 1)
    ax1.imshow(data_log, aspect="auto", cmap="YlOrRd", vmin=0, vmax=np.log10(data.max() + 1))
    ax1.set_xticks(range(len(ch_per_sensor.columns)))
    ax1.set_xticklabels([f"Ch {int(ch)}" for ch in ch_per_sensor.columns], fontsize=10)
    ax1.set_yticks(range(len(sensors)))
    ax1.set_yticklabels([sensor.replace("monad-", "M") for sensor in sensors], fontsize=12)
    for row in range(data.shape[0]):
        for col in range(data.shape[1]):
            value = int(data[row, col])
            if value >= 1000:
                text = f"{value // 1000}K"
            elif value > 0:
                text = str(value)
            else:
                text = "."
            color = "white" if data_log[row, col] > 3.5 else "black"
            ax1.text(col, row, text, ha="center", va="center", fontsize=10, color=color, fontweight="bold")
    ax1.set_title("Beacon Frames per Channel per Sensor\n(log-scale color, raw count in cell)")
    ax1.grid(False)

    covered = (ch_per_sensor > 100).sum(axis=1)
    all_aps_per_sensor = df.groupby("sensor")["bssid"].nunique().reindex(sensors)
    x = np.arange(len(sensors))
    width = 0.35
    bars_channels = ax2.bar(x - width / 2, covered.values, width, color=[SENSOR_COLORS.get(sensor, "#7f8c8d") for sensor in sensors], alpha=0.6, label="Channels w/ >100 frames")
    bars_aps = ax2.bar(x + width / 2, all_aps_per_sensor.values, width, color=[SENSOR_COLORS.get(sensor, "#7f8c8d") for sensor in sensors], alpha=1.0, label="Unique APs detected")
    for bar, value in zip(bars_channels, covered.values):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3, str(int(value)), ha="center", va="bottom", fontsize=11, fontweight="bold")
    for bar, value in zip(bars_aps, all_aps_per_sensor.values):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3, str(int(value)), ha="center", va="bottom", fontsize=11, fontweight="bold")
    ax2.set_xticks(x)
    ax2.set_xticklabels([sensor.replace("monad-", "M") for sensor in sensors])
    ax2.set_ylabel("Count")
    ax2.set_title("Channel Coverage & AP Detection per Sensor")
    ax2.legend(fontsize=9)
    ax2.set_ylim(0, max(all_aps_per_sensor.values) * 1.2)
    if note:
        ax2.annotate(
            note,
            xy=(0, covered.values[0] + 0.5),
            xytext=(0.4, max(all_aps_per_sensor.values) * 0.75),
            fontsize=9,
            color="#c0392b",
            ha="center",
            fontweight="bold",
            arrowprops={"arrowstyle": "->", "color": "#c0392b", "lw": 1.5},
        )
    title = "Channel Coverage Analysis"
    if note:
        title += " - Reveals monad-03 Hop Failure"
    fig.suptitle(title, fontsize=14, fontweight="bold")
    plt.tight_layout()
    fig.savefig(output_dir / "09_channel_coverage.png")
    plt.close(fig)


def save_10_usable_aps(inventory: pd.DataFrame, pivot: pd.DataFrame, output_dir: Path) -> None:
    usable = inventory[(inventory["sensor_count"] >= 2) & (inventory["max_rssi"] > -85)].copy()
    if usable.empty:
        return
    highlight = usable[usable["delta"] >= 10].index.tolist()
    matrix = pivot.loc[usable.index].copy()
    matrix.index = usable["display_name"].tolist()
    highlight_names = [usable.loc[bssid, "display_name"] for bssid in highlight]
    save_heatmap(
        output_dir / "10_usable_aps_only.png",
        matrix,
        "Usable APs Only — RSSI Fingerprint",
        f"(≥ 2 sensors AND max RSSI > -85 dBm · {len(usable)} APs · green box = Δ ≥ 10 dB = positioning-grade)",
        highlight_rows=highlight_names,
    )


def resize_png(path: Path, max_width: int) -> None:
    if Image is None or max_width <= 0:
        return
    with Image.open(path) as img:
        if img.width <= max_width:
            return
        height = int(img.height * max_width / img.width)
        resized = img.resize((max_width, height), Image.Resampling.LANCZOS)
        resized.save(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="exp145")
    parser.add_argument(
        "--frames-csv",
        type=Path,
        default=None,
        help="Path to normalized wifi_5g_frames.csv. Defaults to artifacts/analysis/<run-id>/wifi_5g_frames.csv.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for generated PNGs. Defaults to artifacts/figures/<run-id>/.",
    )
    parser.add_argument("--resize-max-width", type=int, default=1200)
    parser.add_argument(
        "--channel-coverage-note",
        default=None,
        help="Optional annotation for the per-sensor coverage comparison chart.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[2]
    frames_csv = args.frames_csv or repo_root / "artifacts" / "analysis" / args.run_id / "wifi_5g_frames.csv"
    output_dir = args.output_dir or repo_root / "artifacts" / "figures" / args.run_id
    if not frames_csv.exists():
        raise FileNotFoundError(f"Missing input CSV: {frames_csv}")

    output_dir.mkdir(parents=True, exist_ok=True)
    configure_plotting()
    df = load_frames(frames_csv)
    sensors = ordered_sensors(df)
    alias_ssids = load_alias_ssids(repo_root)
    inventory, pivot = build_inventory(df, sensors, alias_ssids)
    duration_s = (df["ts"].max() - df["ts"].min()).total_seconds() if df["ts"].notna().any() else 0.0
    capture_text = capture_label(args.run_id, duration_s)

    save_01_rf_environment(inventory, output_dir, args.run_id, capture_text)
    save_02_ap_covisibility(df, inventory, output_dir, args.run_id, sensors)
    save_03_rssi_fingerprint(inventory, pivot, output_dir)
    save_04_rssi_stability(df, inventory, output_dir, sensors, duration_s)
    save_05_sensor_comparison(df, inventory, output_dir, sensors, args.run_id, capture_text)
    save_06_landmark_quality(inventory, output_dir)
    save_07_positioning_signal(inventory, output_dir, sensors)
    save_08_beacon_timing(df, inventory, output_dir, sensors)

    note = args.channel_coverage_note
    if note is None and args.run_id == "exp145":
        note = "M03 effectively\nmissed ch 112\n-> saw fewer APs"
    save_09_channel_coverage(df, output_dir, sensors, note=note)
    save_10_usable_aps(inventory, pivot, output_dir)

    for filename in (
        "01_rf_environment.png",
        "02_ap_covisibility.png",
        "03_rssi_fingerprint_heatmap.png",
        "04_rssi_stability.png",
        "05_sensor_comparison.png",
        "06_landmark_quality.png",
        "07_positioning_signal.png",
        "08_beacon_timing.png",
        "09_channel_coverage.png",
        "10_usable_aps_only.png",
    ):
        path = output_dir / filename
        if path.exists():
            resize_png(path, args.resize_max_width)
            print(f"{filename}: {path.stat().st_size // 1024} KB")

    print(f"Unique APs: {inventory.shape[0]}")
    print("APs per sensor:")
    for sensor, count in df.groupby("sensor")["bssid"].nunique().reindex(sensors).items():
        print(f"  {sensor}: {count}")
    usable_count = int(((inventory["sensor_count"] >= 2) & (inventory["max_rssi"] > -85)).sum())
    print(f"Usable APs: {usable_count}")
    recovered_aliases = int(((inventory["ssid"] == "") & (inventory["best_ssid"] != "")).sum())
    print(f"Recovered SSID aliases from prior runs: {recovered_aliases}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
