#!/usr/bin/env python3
"""Render BLE environment-scan figures from a ble_environment.csv dataset.

Produces, under artifacts/figures/<experiment>/:

  ble_env_devices_per_sensor.png       — unique devices per Pi, with named-device split
  ble_env_rssi_distribution.png        — RSSI distribution per sensor (boxplot)
  ble_env_multi_sensor_coverage.png    — how many sensors heard each device
  ble_env_manufacturer_mix.png         — BT-SIG company-id mix across the fleet

Pair with `parse_ble_bluetoothctl_logs.py --experiment <id>` to build the
input CSV from raw bluetoothctl scan logs.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="matplotlib-"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import seaborn as sns


REPO = Path("/Users/admin/FleetManager")
SENSOR_ORDER = ["monad-02", "monad-03", "monad-04", "monad-05", "monad-06"]
SENSOR_COLORS = {
    "monad-02": "#d35454",
    "monad-03": "#4e8fe0",
    "monad-04": "#e07c4e",
    "monad-05": "#3aaa5c",
    "monad-06": "#9b59b6",
}

# Small, conservative BT-SIG company-identifier map. Only entries I'm
# confident about — everything else stays as "Unknown (0xXXXX)" rather than
# guessing.
BT_COMPANY = {
    0x0006: "Microsoft",
    0x000F: "Broadcom",
    0x004C: "Apple",
    0x0075: "Samsung",
    0x00E0: "Google",
    0x00E1: "LG Electronics",
    0x010F: "Logitech",
    0x011B: "General Motors",
    0x0131: "Cypress Semiconductor",
    0x0157: "Samsung Electronics",
    0x0171: "Amazon Lab126",
    0x01D7: "Sonos",
    0x012D: "Sony",
    0xFFFF: "Reserved / unknown",
}


def configure_plotting() -> None:
    sns.set_theme(style="whitegrid")
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.size": 11,
            "axes.titlesize": 13,
            "axes.titleweight": "bold",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.dpi": 130,
            "savefig.dpi": 150,
            "savefig.bbox": "tight",
        }
    )


def fmt_int(x, _=None):
    return f"{int(x):,}"


def save_fig(fig, output_dir: Path, name: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.png"
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}", flush=True)


def fig_devices_per_sensor(df: pd.DataFrame, output_dir: Path, experiment: str) -> None:
    new_events = df[df["event_type"] == "new"].copy()
    has_name = new_events["device_name"].fillna("").astype(str).str.strip() != ""
    # A bluetoothctl name that simply restates the MAC (e.g. 12-34-56-78-90-AB)
    # is not a real advertised name.
    looks_like_mac = (
        new_events["device_name"]
        .fillna("")
        .astype(str)
        .str.fullmatch(r"[0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5}", na=False)
    )
    new_events["has_real_name"] = has_name & ~looks_like_mac
    total_per_sensor = new_events.groupby("pi_id")["device_mac"].nunique()
    named_per_sensor = (
        new_events[new_events["has_real_name"]]
        .groupby("pi_id")["device_mac"]
        .nunique()
    )
    sensors = [s for s in SENSOR_ORDER if s in total_per_sensor.index]
    total_vals = [int(total_per_sensor.get(s, 0)) for s in sensors]
    named_vals = [int(named_per_sensor.get(s, 0)) for s in sensors]
    anon_vals = [t - n for t, n in zip(total_vals, named_vals)]

    fig, ax = plt.subplots(figsize=(11, 5))
    x = np.arange(len(sensors))
    ax.bar(x, named_vals, color="#3498db", edgecolor="white", label="Named (real advertised name)")
    ax.bar(x, anon_vals, bottom=named_vals, color="#bdc3c7", edgecolor="white", label="Anonymous (MAC-only)")
    ax.set_xticks(x)
    ax.set_xticklabels(sensors)
    ax.set_xlabel("Sensor")
    ax.set_ylabel("Unique BLE devices observed")
    ax.set_title(f"Unique BLE devices per sensor — {experiment}")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_int))
    for i, total in enumerate(total_vals):
        ax.text(i, total, f"\nΣ {total:,}", ha="center", va="bottom", fontsize=10, fontweight="bold")
    ax.legend(loc="upper right")
    plt.tight_layout()
    save_fig(fig, output_dir, "ble_env_devices_per_sensor")


def fig_rssi_distribution(df: pd.DataFrame, output_dir: Path, experiment: str) -> None:
    rssi_df = df[(df["event_type"] == "rssi") & df["rssi_dbm"].notna()].copy()
    rssi_df["rssi_dbm"] = pd.to_numeric(rssi_df["rssi_dbm"], errors="coerce")
    rssi_df = rssi_df.dropna(subset=["rssi_dbm"])
    sensors = [s for s in SENSOR_ORDER if s in rssi_df["pi_id"].unique()]

    fig, ax = plt.subplots(figsize=(11, 5.5))
    sns.boxplot(
        data=rssi_df,
        x="pi_id",
        y="rssi_dbm",
        order=sensors,
        hue="pi_id",
        hue_order=sensors,
        palette=[SENSOR_COLORS.get(s, "#7f8c8d") for s in sensors],
        legend=False,
        linewidth=1.2,
        showmeans=True,
        meanprops={
            "marker": "D",
            "markerfacecolor": "white",
            "markeredgecolor": "black",
            "markersize": 6,
        },
        flierprops=dict(marker=".", markersize=3, alpha=0.3),
        ax=ax,
    )
    ax.axhline(-70, color="red", linestyle="--", linewidth=1, alpha=0.7)
    ax.text(len(sensors) - 0.5, -70, "  -70 dBm", color="red", fontsize=9, va="center")
    ax.set_xlabel("Sensor")
    ax.set_ylabel("BLE RSSI (dBm)")
    ax.set_title(f"BLE RSSI distribution per sensor — {experiment}")
    medians = rssi_df.groupby("pi_id")["rssi_dbm"].median().reindex(sensors)
    counts = rssi_df["pi_id"].value_counts().reindex(sensors, fill_value=0)
    ymin = float(rssi_df["rssi_dbm"].min()) - 5
    ax.set_ylim(ymin - 6, float(rssi_df["rssi_dbm"].max()) + 5)
    for i, s in enumerate(sensors):
        ax.text(
            i,
            ymin - 1,
            f"n={int(counts[s]):,}\nmed={medians[s]:.0f}",
            ha="center",
            va="top",
            fontsize=9,
            color="#333",
        )
    plt.tight_layout()
    save_fig(fig, output_dir, "ble_env_rssi_distribution")


def fig_multi_sensor_coverage(df: pd.DataFrame, output_dir: Path, experiment: str) -> None:
    devices = df[df["event_type"] == "new"].copy()
    coverage = (
        devices.drop_duplicates(subset=["pi_id", "device_mac"])
        .groupby("device_mac")["pi_id"]
        .nunique()
        .clip(upper=5)
    )
    counts = coverage.value_counts().sort_index()

    fig, ax = plt.subplots(figsize=(9, 5))
    bucket_colors = ["#e74c3c", "#e67e22", "#f1c40f", "#2ecc71", "#16a085"]
    labels = [f"{i} sensor{'s' if i > 1 else ''}" for i in range(1, 6)]
    values = [int(counts.get(i, 0)) for i in range(1, 6)]
    bars = ax.bar(labels, values, color=bucket_colors, edgecolor="white")
    ax.set_xlabel("Number of sensors that heard the device")
    ax.set_ylabel("Unique BLE devices")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_int))
    total = sum(values)
    for bar, v in zip(bars, values):
        if v == 0:
            continue
        pct = (100.0 * v / total) if total else 0.0
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            f"{v:,}\n({pct:.1f}%)",
            ha="center",
            va="bottom",
            fontsize=10,
        )
    multi = sum(values[2:])  # 3+ sensors
    ax.set_title(
        f"BLE multi-sensor coverage — {experiment}  ({multi:,} of {total:,} devices heard by ≥3 sensors)"
    )
    ax.set_ylim(0, max(values) * 1.18)
    plt.tight_layout()
    save_fig(fig, output_dir, "ble_env_multi_sensor_coverage")


def fig_manufacturer_mix(df: pd.DataFrame, output_dir: Path, experiment: str, top_n: int = 10) -> None:
    mfr_df = df[df["event_type"] == "manufacturer_key"].copy()
    if mfr_df.empty:
        print("  (no manufacturer-key events; skipping manufacturer mix figure)", flush=True)
        return
    mfr_df["manufacturer_key_int"] = pd.to_numeric(
        mfr_df["manufacturer_key_int"], errors="coerce"
    )
    mfr_df = mfr_df.dropna(subset=["manufacturer_key_int"])
    mfr_df["company_id"] = mfr_df["manufacturer_key_int"].astype(int)
    mfr_df["company"] = mfr_df["company_id"].map(
        lambda c: BT_COMPANY.get(c, f"Unknown (0x{c:04X})")
    )

    # Count unique devices per company (per observation, not per event-rate).
    by_company = (
        mfr_df.groupby("company")["device_mac"].nunique().sort_values(ascending=False)
    )
    top = by_company.head(top_n)
    other_count = int(by_company.iloc[top_n:].sum())
    if other_count > 0:
        merged_companies = len(by_company) - top_n
        top[f"Other ({merged_companies} other vendors)"] = other_count

    fig, ax = plt.subplots(figsize=(11, 6))
    bars = ax.barh(np.arange(len(top)), top.values, color="#3498db", edgecolor="white")
    ax.set_yticks(np.arange(len(top)))
    ax.set_yticklabels(top.index)
    ax.invert_yaxis()
    ax.set_xlabel("Unique BLE devices that broadcast this BT-SIG company id")
    ax.set_title(
        f"BLE manufacturer mix — {experiment}  ({len(by_company)} distinct BT-SIG company ids observed)"
    )
    for bar, v in zip(bars, top.values):
        ax.text(v, bar.get_y() + bar.get_height() / 2, f"  {int(v):,}", va="center", fontsize=9)
    ax.set_xlim(0, float(top.max()) * 1.18)
    plt.tight_layout()
    save_fig(fig, output_dir, "ble_env_manufacturer_mix")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    csv_path = REPO / "artifacts" / "analysis" / args.experiment / "ble_environment.csv"
    out_dir = REPO / "artifacts" / "figures" / args.experiment
    if not csv_path.exists():
        sys.exit(f"Missing input: {csv_path}. Run parse_ble_bluetoothctl_logs.py first.")
    configure_plotting()
    df = pd.read_csv(csv_path)
    print(f"Loaded {len(df):,} rows from {csv_path}", flush=True)

    fig_devices_per_sensor(df, out_dir, args.experiment)
    fig_rssi_distribution(df, out_dir, args.experiment)
    fig_multi_sensor_coverage(df, out_dir, args.experiment)
    fig_manufacturer_mix(df, out_dir, args.experiment)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
