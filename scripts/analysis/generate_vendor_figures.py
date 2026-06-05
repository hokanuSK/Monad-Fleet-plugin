#!/usr/bin/env python3
"""Regenerate the vendor-analysis figure set with tighter precision.

Improvements over the original notebook cells:

- Resolve OUIs through scapy's IEEE manuf DB (Wireshark-style), not a short hand-
  rolled lookup. That turns large "Unknown" buckets (F4:4C:7F Huawei, AC:22:05 /
  DC:53:7C Compal, 84:3E:92 Huawei, ...) into real vendor attribution.
- Roll up locally-administered virtual BSSIDs onto their parent vendor by
  clearing the 0x02 bit on the first octet, instead of collapsing every virtual
  BSSID into a single opaque "Virtual BSSID" bucket.
- Order categorical axes by frame count, mark DFS channels, annotate N values
  and row/column totals, and use color consistently across all five figures.
- Adds a vendor-landmark-quality panel (multi-sensor visibility + RSSI std).

Run against the normalized wifi_5g_frames.csv produced by
prepare_measurement_dataset.py. Pass --experiment expNNN to point at a
different dataset (default: exp174).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pickle
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="matplotlib-"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import matplotlib.transforms as mtransforms
import numpy as np
import pandas as pd
import seaborn as sns


REPO_ROOT = Path("/Users/admin/FleetManager")
DEFAULT_EXPERIMENT = "exp174"
DEFAULT_MANUF_PICKLE = Path("/Users/admin/.cache/scapy/manufdb.pickle")


def dataset_paths_for(experiment: str) -> tuple[Path, Path]:
    csv = REPO_ROOT / "artifacts" / "analysis" / experiment / "wifi_5g_frames.csv"
    out = REPO_ROOT / "artifacts" / "figures" / experiment
    return csv, out


def tables_dir_for(experiment: str) -> Path:
    return REPO_ROOT / "artifacts" / "tables" / experiment

SENSOR_ORDER = ["monad-02", "monad-03", "monad-04", "monad-05", "monad-06"]

# 5 GHz DFS channels (must hop on radar) — used to shade channel axes.
DFS_CHANNELS = {52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144}

VENDOR_PALETTE = {
    "Compal": "#3498db",
    "TP-Link": "#16a085",
    "CommScope": "#e67e22",
    "Sagemcom": "#e74c3c",
    "Huawei": "#9b59b6",
    "Murata": "#6d28d9",
    "Apple": "#1abc9c",
    "Xiaomi": "#f1c40f",
    "Asus": "#34495e",
    "Netgear": "#7f8c8d",
    "Ubiquiti": "#2c3e50",
    "MikroTik": "#c0392b",
    "Technicolor": "#d35400",
    "Sercomm": "#2980b9",
    "Cisco": "#27ae60",
    "Linksys": "#8e44ad",
    "ZyXEL": "#f39c12",
    "Aruba": "#16537e",
    "Zhuolian": "#0d3b66",
    "Trolink": "#1d4ed8",
    "Bilian": "#0f766e",
    "Feasycom": "#7c3aed",
    "iComm": "#0369a1",
    "Chengqian": "#0f766e",
    "Vantiva": "#9d174d",
    "Qisda": "#a16207",
    "Wistron": "#64748b",
    "AMPAK": "#0f172a",
    "Realtek": "#9333ea",
    "Panasonic": "#475569",
    "nFore": "#be123c",
    "Virtual BSSID": "#95a5a6",
    "Unknown": "#bdc3c7",
    "Other": "#475569",
}

# Cap visible vendor categories on plots; everything outside the top-N (by frame
# count) is collapsed into a single OTHER_LABEL bucket with the count of merged
# vendors annotated in the legend.
TOP_N_DEFAULT = 10
OTHER_LABEL = "Other"

# Map scapy's long manufacturer strings to short canonical labels we want to
# show on plots. The rule list is order-dependent: first match wins.
CANONICAL_RULES: list[tuple[str, str]] = [
    ("compal", "Compal"),
    ("tp-link", "TP-Link"),
    ("tplink", "TP-Link"),
    ("arris", "CommScope"),
    ("commscope", "CommScope"),
    ("sagemcom", "Sagemcom"),
    ("huawei", "Huawei"),
    ("murata", "Murata"),
    ("apple", "Apple"),
    ("xiaomi", "Xiaomi"),
    ("asustek", "Asus"),
    ("asus", "Asus"),
    ("ubiquiti", "Ubiquiti"),
    ("netgear", "Netgear"),
    ("mikrotik", "MikroTik"),
    ("technicolor", "Technicolor"),
    ("vantiva", "Vantiva"),
    ("sercomm", "Sercomm"),
    ("cisco", "Cisco"),
    ("linksys", "Linksys"),
    ("zyxel", "ZyXEL"),
    ("zte", "ZTE"),
    ("aruba", "Aruba"),
    ("d-link", "D-Link"),
    ("dlink", "D-Link"),
    ("amazon", "Amazon"),
    ("google", "Google"),
    ("samsung", "Samsung"),
    ("intel", "Intel"),
    ("mercusys", "Mercusys"),
    ("qisda", "Qisda"),
    ("zhuolian", "Zhuolian"),
    ("routerboard.com", "MikroTik"),
    ("mikrotik", "MikroTik"),
    ("ampak", "AMPAK"),
    ("wistron", "Wistron"),
    ("realtek", "Realtek"),
    ("panasonic", "Panasonic"),
    ("nfore", "nFore"),
    ("trolink", "Trolink"),
    ("bilian", "Bilian"),
    ("feasycom", "Feasycom"),
    ("icomm", "iComm"),
    ("chengqian", "Chengqian"),
    ("lite-on", "Lite-On"),
    ("liteon", "Lite-On"),
]


def load_manuf(pickle_path: Path):
    if not pickle_path.exists():
        sys.exit(
            f"Scapy manuf pickle not found at {pickle_path}. "
            "Install scapy and run any scapy command once to populate it."
        )
    with open(pickle_path, "rb") as fh:
        blob = pickle.load(fh)
    return blob["content"]


def canonical_vendor(long_name: str | None, oui_str: str) -> str | None:
    if not long_name:
        return None
    if oui_str.upper() == "00:00:00":
        return None
    # Scapy returns the original OUI string when it cannot resolve it.
    if long_name.upper().startswith(oui_str.upper()):
        return None
    n = long_name.lower()
    if "officially xerox" in n:
        return None
    for tag, label in CANONICAL_RULES:
        if tag in n:
            return label
    # Fallback: first token, stripped of punctuation. Often this is a usable
    # short name (e.g. "Belkin" from "Belkin International Inc").
    first = re.sub(r"[^0-9A-Za-z./+-]", "", long_name.split()[0].rstrip(",.;:"))
    if not first:
        return None
    if first.lower() == "officially":
        return None
    if first.lower() == "routerboard.com":
        return "MikroTik"
    if first.islower():
        first = first.capitalize()
    return first or None


def build_vendor_resolver(manuf):
    cache: dict[str, tuple[str, bool]] = {}

    def resolve(bssid: str) -> tuple[str, bool]:
        """Return (vendor_label, is_virtual_bssid)."""
        if not bssid or len(bssid) < 8:
            return ("Unknown", False)
        key = bssid[:8].upper()
        if key in cache:
            return cache[key]
        first_byte = int(key[:2], 16)
        is_la = bool(first_byte & 0x02)

        long_name = manuf.lookup(bssid)[1] if manuf else None
        canon = canonical_vendor(long_name, key)

        if canon is None and is_la:
            # Try clearing the locally-administered bit and look up the implied
            # physical OUI. This rolls multi-SSID virtual BSSIDs onto their
            # parent vendor.
            phys_first = first_byte ^ 0x02
            phys_oui = f"{phys_first:02X}{key[2:]}"
            long2 = manuf.lookup(phys_oui + ":00:00:00")[1] if manuf else None
            canon = canonical_vendor(long2, phys_oui.replace("", ""))

        if canon is None:
            canon = "Virtual BSSID" if is_la else "Unknown"

        cache[key] = (canon, is_la)
        return cache[key]

    return resolve


def load_mac_to_pi_map(csv_path: Path) -> dict[str, str]:
    """Read device_map from sibling metadata.json so we can resolve MAC → pi_id."""
    meta_path = csv_path.parent / "metadata.json"
    if not meta_path.exists():
        return {}
    import json
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return {}
    device_map = meta.get("device_map") or {}
    # Normalize MAC keys to the dashed lowercase form that appears in pcap paths.
    return {
        mac.lower().replace(":", "-"): pi_id
        for pi_id, mac in device_map.items()
        if isinstance(mac, str)
    }


def recover_pi_from_pcap_path(path: object, mac_to_pi: dict[str, str]) -> str:
    """Pull a pi_id out of a pcap path using either monad-XX or a MAC parent dir."""
    if not isinstance(path, str) or not path:
        return "unknown"
    p_lower = path.lower()
    m = re.search(r"monad[-_]?(\d+)", p_lower)
    if m:
        return f"monad-{int(m.group(1)):02d}"
    # MAC-as-directory pattern, e.g. "/2c-cf-67-80-f5-86/" or "/24-eb-16-e3-6a-07__".
    mac_match = re.search(r"([0-9a-f]{2}(?:-[0-9a-f]{2}){5})", p_lower)
    if mac_match:
        return mac_to_pi.get(mac_match.group(1), "unknown")
    return "unknown"


def load_frames(csv_path: Path, resolve) -> pd.DataFrame:
    print(f"Loading {csv_path} (this is a multi-GB file)...", flush=True)
    # Include source_pcap so we can recover pi_id when the prep script's
    # filename heuristic missed it (e.g. symlinked pcaps under MAC-address
    # parent dirs).
    df = pd.read_csv(
        csv_path,
        usecols=["pi_id", "channel", "bssid", "frame_subtype", "rssi_dbm", "ssid", "source_pcap"],
        dtype={
            "pi_id": "string",
            "bssid": "string",
            "frame_subtype": "string",
            "ssid": "string",
            "source_pcap": "string",
        },
    )
    print(f"  raw rows: {len(df):,}", flush=True)
    df = df[df["frame_subtype"].isin(["Beacon", "Probe Response"])]
    df = df.dropna(subset=["bssid"])
    df["bssid"] = df["bssid"].str.upper()
    df = df[df["channel"].notna() & (df["channel"] > 0)].copy()

    # Recover pi_id from source_pcap when prep produced "unknown"/empty. The
    # pcap path is one of:
    #   .../monad-XX__wifi-5g-monitor.pcap            (monad-XX in basename)
    #   .../<mac-with-dashes>/wifi-5g-monitor.pcap    (parent dir is the MAC)
    #   .../<mac-with-dashes>__wifi-5g-monitor.pcap   (MAC prefix when symlinks were resolved)
    df["pi_id"] = df["pi_id"].fillna("").astype(str)
    mac_to_pi = load_mac_to_pi_map(csv_path)
    needs_fix = df["pi_id"].isin(["", "unknown"])
    if needs_fix.any() and not df.loc[needs_fix, "source_pcap"].dropna().empty:
        df.loc[needs_fix, "pi_id"] = df.loc[needs_fix, "source_pcap"].map(
            lambda p: recover_pi_from_pcap_path(p, mac_to_pi)
        )
        recovered = (df["pi_id"] != "unknown") & needs_fix
        if recovered.any():
            print(
                f"  recovered pi_id for {int(recovered.sum()):,} rows from source_pcap paths",
                flush=True,
            )

    df["sensor"] = df["pi_id"]
    print(f"  beacon/probe rows after filter: {len(df):,}", flush=True)

    # Resolve vendor once per unique BSSID, then map back.
    unique_bssids = df["bssid"].dropna().unique()
    print(f"  unique BSSIDs: {len(unique_bssids):,}", flush=True)
    bssid_to_vendor = {}
    bssid_to_virtual = {}
    for b in unique_bssids:
        v, is_v = resolve(b)
        bssid_to_vendor[b] = v
        bssid_to_virtual[b] = is_v
    df["vendor"] = df["bssid"].map(bssid_to_vendor)
    df["is_virtual_bssid"] = df["bssid"].map(bssid_to_virtual)
    return df


def build_inventory(df: pd.DataFrame) -> pd.DataFrame:
    inv = (
        df.groupby("bssid")
        .agg(
            ssid=("ssid", lambda s: s.dropna().astype(str).replace("", pd.NA).dropna().mode().iloc[0] if s.dropna().astype(str).replace("", pd.NA).dropna().size else ""),
            channel=("channel", lambda s: int(s.mode().iloc[0])),
            vendor=("vendor", lambda s: s.mode().iloc[0]),
            is_virtual=("is_virtual_bssid", "first"),
            rssi_median=("rssi_dbm", "median"),
            rssi_max=("rssi_dbm", "max"),
            rssi_std=("rssi_dbm", "std"),
            beacon_count=("rssi_dbm", "size"),
            sensors_seeing=("sensor", lambda s: s.nunique()),
        )
        .reset_index()
    )
    return inv


def vendor_order_by_frames(df: pd.DataFrame) -> list[str]:
    return df["vendor"].value_counts().index.tolist()


def collapse_to_top_n(
    df: pd.DataFrame, inv: pd.DataFrame, top_n: int = TOP_N_DEFAULT
) -> tuple[pd.DataFrame, pd.DataFrame, list[str], int]:
    """Collapse vendors outside the top-N (by frame count) into a single bucket.

    Returns (df_capped, inv_capped, top_order, merged_count). top_order is the
    final list of vendor labels in display order, with OTHER_LABEL last when
    any vendors were merged.
    """
    ranked = df["vendor"].value_counts().index.tolist()
    keep = set(ranked[:top_n])
    merged_count = max(0, len(ranked) - len(keep))

    def relabel(v: object) -> str:
        return v if v in keep else OTHER_LABEL

    df_capped = df.copy()
    inv_capped = inv.copy()
    df_capped["vendor"] = df_capped["vendor"].map(relabel)
    inv_capped["vendor"] = inv_capped["vendor"].map(relabel)

    top_order = [v for v in ranked if v in keep]
    if merged_count > 0:
        top_order.append(OTHER_LABEL)
    return df_capped, inv_capped, top_order, merged_count


def color_for(vendor_name: str) -> str:
    if vendor_name in VENDOR_PALETTE:
        return VENDOR_PALETTE[vendor_name]
    if vendor_name == OTHER_LABEL:
        return VENDOR_PALETTE[OTHER_LABEL]
    if vendor_name == "Unknown":
        return VENDOR_PALETTE["Unknown"]
    if vendor_name == "Virtual BSSID":
        return VENDOR_PALETTE["Virtual BSSID"]

    digest = hashlib.sha256(vendor_name.encode("utf-8")).digest()
    cmap = plt.get_cmap("tab20")
    idx = digest[0] % cmap.N
    rgba = cmap(idx)
    return matplotlib.colors.to_hex(rgba, keep_alpha=False)


def vendor_palette_for(order: list[str]) -> list[str]:
    return [color_for(v) for v in order]


def other_legend_label(name: str, merged_count: int) -> str:
    if name == OTHER_LABEL and merged_count > 0:
        return f"Other ({merged_count} small vendors)"
    return name


def add_vendor_row_swatches(ax, labels: list[str], vendor_names: list[str]) -> None:
    """Add colored vendor swatches aligned with y tick labels."""
    trans = mtransforms.blended_transform_factory(ax.transAxes, ax.transData)
    for i, vendor_name in enumerate(vendor_names):
        ax.add_patch(
            plt.Rectangle(
                (-0.055, i + 0.12),
                0.03,
                0.76,
                transform=trans,
                clip_on=False,
                facecolor=color_for(vendor_name),
                edgecolor="white",
                linewidth=0.6,
            )
        )
    ax.tick_params(axis="y", pad=14)


def configure_plotting() -> None:
    sns.set_theme(style="whitegrid", palette="tab10")
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


def fmt_int(x, _pos=None):
    return f"{int(x):,}"


def save_fig(fig, output_dir: Path, name: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.png"
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}", flush=True)


def save_summary_tables(df: pd.DataFrame, inv: pd.DataFrame, tables_dir: Path) -> None:
    tables_dir.mkdir(parents=True, exist_ok=True)

    vendor_summary = (
        inv.groupby("vendor")
        .agg(
            unique_aps=("bssid", "nunique"),
            frames=("beacon_count", "sum"),
            median_rssi_dbm=("rssi_median", "median"),
            best_rssi_dbm=("rssi_max", "max"),
            median_rssi_std_db=("rssi_std", "median"),
            aps_seen_by_2plus_sensors=("sensors_seeing", lambda s: int((s >= 2).sum())),
            aps_seen_by_3plus_sensors=("sensors_seeing", lambda s: int((s >= 3).sum())),
            usable_aps=("sensors_seeing", "size"),
            virtual_bssid_count=("is_virtual", "sum"),
        )
        .reset_index()
    )
    vendor_summary["usable_aps"] = (
        inv.groupby("vendor")
        .apply(lambda g: int(((g["sensors_seeing"] >= 2) & (g["rssi_max"] > -85)).sum()))
        .values
    )
    vendor_summary["virtual_share_pct"] = (
        100.0 * vendor_summary["virtual_bssid_count"] / vendor_summary["unique_aps"].clip(lower=1)
    ).round(1)
    vendor_summary = vendor_summary.sort_values(["frames", "unique_aps"], ascending=[False, False])
    vendor_summary.to_csv(tables_dir / "ap_vendor_summary.csv", index=False)

    sensor_vendor = (
        df.groupby(["sensor", "vendor"]).size().rename("frames").reset_index()
        .sort_values(["sensor", "frames"], ascending=[True, False])
    )
    sensor_vendor.to_csv(tables_dir / "ap_vendor_sensor_breakdown.csv", index=False)


# --------------------------------------------------------------------------- #
# Figure 1: Vendor overview (frames, AP count, median RSSI)
# --------------------------------------------------------------------------- #
def fig_vendor_overview(df: pd.DataFrame, inv: pd.DataFrame, out: Path, top_n: int, experiment: str) -> None:
    df_c, inv_c, order, merged = collapse_to_top_n(df, inv, top_n)
    vendor_frames = df_c["vendor"].value_counts().reindex(order)
    vendor_aps = inv_c["vendor"].value_counts().reindex(order, fill_value=0)
    vendor_rssi = (
        df_c.dropna(subset=["rssi_dbm"])
        .groupby("vendor")["rssi_dbm"]
        .median()
        .reindex(order)
    )
    virtual_share = (
        inv_c.groupby("vendor")["is_virtual"]
        .apply(lambda s: 100.0 * s.sum() / max(len(s), 1))
        .reindex(order, fill_value=0.0)
    )

    display_labels = [other_legend_label(v, merged) for v in order]
    colors = vendor_palette_for(order)

    fig, axes = plt.subplots(1, 3, figsize=(18, 6.5))
    n_sensors = df["sensor"].nunique()
    fig.suptitle(
        f"Vendor analysis — {experiment}  ({n_sensors} WiFi PCAP sensors)",
        fontsize=14,
        fontweight="bold",
    )

    # Panel 1: frames per vendor (log scale)
    ax = axes[0]
    bars = ax.bar(display_labels, vendor_frames.values, color=colors, edgecolor="white")
    ax.set_yscale("log")
    ax.set_title("Beacon / probe-response frames per vendor")
    ax.set_ylabel("Frame count (log scale)")
    ax.set_xlabel("Vendor")
    ax.tick_params(axis="x", rotation=30)
    for label in ax.get_xticklabels():
        label.set_horizontalalignment("right")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_int))
    # Extend log y-axis so frame-count labels above the tallest bar aren't clipped.
    ymax = float(vendor_frames.max())
    ax.set_ylim(top=ymax * 4)
    for bar, value in zip(bars, vendor_frames.values):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() * 1.15,
            f"{int(value):,}",
            ha="center",
            va="bottom",
            fontsize=9,
        )

    # Panel 2: AP count per vendor (horizontal, with virtual-BSSID share annotation)
    ax = axes[1]
    y = np.arange(len(order))
    ax.barh(y, vendor_aps.values, color=colors, edgecolor="white")
    ax.set_yticks(y)
    ax.set_yticklabels(display_labels)
    ax.invert_yaxis()
    ax.set_xlabel("Unique BSSIDs")
    ax.set_title("Unique APs per vendor (virtual-BSSID share annotated)")
    xmax = float(vendor_aps.max() or 0) * 1.25 + 5
    ax.set_xlim(0, xmax)
    for i, (n_aps, vshare) in enumerate(zip(vendor_aps.values, virtual_share.values)):
        if n_aps == 0:
            continue
        suffix = f"  ({vshare:.0f}% virt.)" if vshare > 0 else ""
        ax.text(n_aps, i, f" {int(n_aps):,}{suffix}", va="center", fontsize=9)

    # Panel 3: median RSSI per vendor
    ax = axes[2]
    rssi_sorted_idx = vendor_rssi.sort_values(ascending=False).index
    rssi_vals = vendor_rssi.reindex(rssi_sorted_idx).values
    rssi_labels = [other_legend_label(v, merged) for v in rssi_sorted_idx]
    rssi_colors = [color_for(v) for v in rssi_sorted_idx]
    bars = ax.barh(np.arange(len(rssi_sorted_idx)), rssi_vals, color=rssi_colors, edgecolor="white")
    ax.set_yticks(np.arange(len(rssi_sorted_idx)))
    ax.set_yticklabels(rssi_labels)
    ax.invert_yaxis()
    ax.axvline(-70, color="red", linestyle="--", linewidth=1, alpha=0.7)
    ax.text(-70, -0.6, "  -70 dBm (weak)", color="red", fontsize=8, va="bottom")
    ax.set_xlabel("Median RSSI (dBm)")
    ax.set_title("Median frame RSSI per vendor")
    for bar, value in zip(bars, rssi_vals):
        if pd.isna(value):
            continue
        ax.text(
            value + 0.5,
            bar.get_y() + bar.get_height() / 2,
            f"{value:.0f}",
            va="center",
            ha="left",
            fontsize=9,
        )
    ax.set_xlim(min(-100, float(np.nanmin(rssi_vals)) - 5), -20)

    plt.tight_layout(rect=(0, 0, 1, 0.95))
    save_fig(fig, out, "ap_vendor_overview")


# --------------------------------------------------------------------------- #
# Figure 2: RSSI distribution per vendor
# --------------------------------------------------------------------------- #
def fig_vendor_rssi_boxplot(df: pd.DataFrame, inv: pd.DataFrame, out: Path, top_n: int, experiment: str) -> None:
    df_c, _, _, merged = collapse_to_top_n(df, inv, top_n)
    df_v = df_c.dropna(subset=["rssi_dbm", "vendor"]).copy()
    # Order by median descending so the strongest-signal vendor is leftmost.
    medians = df_v.groupby("vendor")["rssi_dbm"].median().sort_values(ascending=False)
    order = medians.index.tolist()
    counts = df_v["vendor"].value_counts().reindex(order, fill_value=0)
    colors = vendor_palette_for(order)
    display_labels = [other_legend_label(v, merged) for v in order]

    fig, ax = plt.subplots(figsize=(14, 6))
    sns.boxplot(
        data=df_v,
        x="vendor",
        y="rssi_dbm",
        order=order,
        hue="vendor",
        hue_order=order,
        palette=colors,
        legend=False,
        linewidth=1.2,
        showmeans=True,
        meanprops={
            "marker": "D",
            "markerfacecolor": "white",
            "markeredgecolor": "black",
            "markersize": 6,
        },
        flierprops=dict(marker=".", markersize=2, alpha=0.25),
        ax=ax,
    )
    ax.set_xticks(np.arange(len(order)))
    ax.set_xticklabels(display_labels)
    ax.axhline(-70, color="red", linestyle="--", linewidth=1, alpha=0.7, label="-70 dBm (weak signal)")
    n_sensors = df["sensor"].nunique()
    ax.set_title(f"RSSI distribution per vendor — {experiment} ({n_sensors} WiFi PCAP sensors, beacon/probe-response frames)")
    ax.set_xlabel("Vendor (sorted by median RSSI, strongest first)")
    ax.set_ylabel("RSSI (dBm)")
    ax.tick_params(axis="x", rotation=25)
    for tick in ax.get_xticklabels():
        tick.set_horizontalalignment("right")

    # Annotate N frames + median below each box.
    ymin, ymax = ax.get_ylim()
    band = ymin - 4
    ax.set_ylim(band - 4, ymax)
    for i, v in enumerate(order):
        ax.text(
            i,
            band,
            f"n={int(counts[v]):,}\nmed={medians[v]:.0f}",
            ha="center",
            va="top",
            fontsize=8,
            color="#333",
        )

    legend_handles = [
        plt.Line2D([0], [0], color="red", linestyle="--", label="-70 dBm (weak signal)"),
        plt.Line2D(
            [0],
            [0],
            marker="D",
            markerfacecolor="white",
            markeredgecolor="black",
            color="white",
            label="Mean",
            linewidth=0,
        ),
    ]
    ax.legend(handles=legend_handles, loc="upper right")
    plt.tight_layout()
    save_fig(fig, out, "ap_vendor_rssi_boxplot")


# --------------------------------------------------------------------------- #
# Figure 3: Vendor × Channel
# --------------------------------------------------------------------------- #
def fig_vendor_channel(df: pd.DataFrame, inv: pd.DataFrame, out: Path, top_n: int, experiment: str) -> None:
    df_c, inv_c, order, merged = collapse_to_top_n(df, inv, top_n)
    channels = sorted(int(c) for c in inv_c["channel"].unique())
    display_labels = [other_legend_label(v, merged) for v in order]

    ap_matrix = (
        inv_c.groupby(["vendor", "channel"])
        .size()
        .unstack(fill_value=0)
        .reindex(index=order, columns=channels, fill_value=0)
    )
    ap_matrix.index = display_labels
    ap_matrix["Σ"] = ap_matrix.sum(axis=1)
    totals_row = ap_matrix.sum(axis=0)
    ap_matrix.loc["Σ"] = totals_row

    frame_matrix = (
        df_c.groupby(["channel", "vendor"])
        .size()
        .unstack(fill_value=0)
        .reindex(columns=order, fill_value=0)
        .reindex(index=channels, fill_value=0)
    )

    fig, axes = plt.subplots(1, 2, figsize=(18, 8), gridspec_kw={"width_ratios": [1.05, 1]})
    fig.suptitle(f"Vendor × 5 GHz channel — {experiment}", fontsize=14, fontweight="bold")

    # Heatmap with totals (mask totals row/column from the colormap so the
    # interior cells get the full color range, but still annotate the totals)
    ax = axes[0]
    annot = ap_matrix.astype(int).astype(str).copy()
    annot.loc["Σ", "Σ"] = ""
    sns.heatmap(
        ap_matrix.iloc[:-1, :-1].astype(int),
        annot=annot.iloc[:-1, :-1].values,
        fmt="",
        cmap="Blues",
        cbar_kws={"label": "AP count"},
        linewidths=0.4,
        linecolor="white",
        ax=ax,
    )
    # Add totals column/row as text outside the colored area
    ax.set_xticks(np.arange(len(channels)) + 0.5)
    ax.set_xticklabels([str(c) for c in channels])
    ax.set_yticks(np.arange(len(order)) + 0.5)
    ax.set_yticklabels(display_labels, rotation=0)
    add_vendor_row_swatches(ax, display_labels, order)
    for i, total in enumerate(ap_matrix.iloc[:-1, -1].values):
        ax.text(len(channels) + 0.3, i + 0.5, f"Σ {int(total)}", va="center", fontsize=9, fontweight="bold")
    for j, total in enumerate(ap_matrix.iloc[-1, :-1].values):
        ax.text(j + 0.5, len(order) + 0.4, f"Σ {int(total)}", ha="center", fontsize=8, fontweight="bold", color="#444")
    ax.text(len(channels) + 0.3, len(order) + 0.4, f"Σ {int(ap_matrix.iloc[-1, -1])}", ha="left", va="center", fontsize=9, fontweight="bold", color="#222")
    ax.set_title("Unique APs per vendor per channel (Σ = row / column totals)")
    ax.set_xlabel("5 GHz channel")
    ax.set_ylabel("Vendor")
    # Highlight DFS channel columns
    for i, ch in enumerate(channels):
        if ch in DFS_CHANNELS:
            ax.add_patch(
                plt.Rectangle(
                    (i, 0),
                    1,
                    len(ap_matrix),
                    fill=False,
                    edgecolor="#e74c3c",
                    lw=1.2,
                    alpha=0.7,
                )
            )
    ax.tick_params(axis="x", rotation=0)

    # Stacked bar of frames per channel by vendor, with DFS shading
    ax = axes[1]
    bottom = np.zeros(len(channels))
    x = np.arange(len(channels))
    for vendor_name, label in zip(order, display_labels):
        values = frame_matrix[vendor_name].values
        ax.bar(
            x,
            values,
            bottom=bottom,
            color=color_for(vendor_name),
            edgecolor="white",
            linewidth=0.4,
            label=label,
        )
        bottom += values
    # Mark DFS channels with a thin top tick band rather than full-height shade —
    # too many DFS channels in this dataset would otherwise wash out the plot.
    ymax_top = float(bottom.max()) if bottom.size else 0.0
    if ymax_top > 0:
        marker_y = ymax_top * 1.04
        for i, ch in enumerate(channels):
            if ch in DFS_CHANNELS:
                ax.plot([i], [marker_y], marker="v", color="#e74c3c", markersize=6)
        ax.set_ylim(top=ymax_top * 1.12)
    ax.set_xticks(x)
    ax.set_xticklabels([str(c) for c in channels])
    ax.set_xlabel("5 GHz channel (red ▼ marker = DFS)")
    ax.set_ylabel("Beacon / probe-response frames")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_int))
    ax.set_title("Frames per channel, stacked by vendor")
    ax.legend(title="Vendor", bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=8)

    plt.tight_layout(rect=(0, 0, 1, 0.95))
    save_fig(fig, out, "ap_vendor_channel_heatmap")


# --------------------------------------------------------------------------- #
# Figure 4: Per-sensor vendor breakdown
# --------------------------------------------------------------------------- #
def fig_vendor_per_sensor(df: pd.DataFrame, inv: pd.DataFrame, out: Path, top_n: int, experiment: str) -> None:
    df_c, _, order, merged = collapse_to_top_n(df, inv, top_n)
    sensors = [s for s in SENSOR_ORDER if s in df_c["sensor"].unique()]
    display_labels = [other_legend_label(v, merged) for v in order]

    sensor_frames = (
        df_c.groupby(["sensor", "vendor"]).size().unstack(fill_value=0)
        .reindex(index=sensors, columns=order, fill_value=0)
    )
    sensor_pct = sensor_frames.div(sensor_frames.sum(axis=1), axis=0) * 100

    # AP count per sensor per vendor (a BSSID counts once per sensor that saw it)
    bssid_sensor = (
        df_c.groupby(["sensor", "bssid"])["vendor"].first().reset_index()
    )
    ap_matrix = (
        bssid_sensor.groupby(["sensor", "vendor"]).size().unstack(fill_value=0)
        .reindex(index=sensors, columns=order, fill_value=0)
    )

    fig, axes = plt.subplots(1, 3, figsize=(20, 6))
    fig.suptitle(f"Per-sensor vendor breakdown — {experiment}", fontsize=14, fontweight="bold")
    colors = vendor_palette_for(order)

    # Panel A: stacked frames per sensor with total label
    ax = axes[0]
    bottom = np.zeros(len(sensors))
    x = np.arange(len(sensors))
    for v, c, label in zip(order, colors, display_labels):
        values = sensor_frames[v].values
        ax.bar(x, values, bottom=bottom, color=c, edgecolor="white", linewidth=0.4, label=label)
        bottom += values
    for i, total in enumerate(sensor_frames.sum(axis=1).values):
        ax.text(
            i,
            total,
            f"\nΣ {int(total):,}",
            ha="center",
            va="bottom",
            fontsize=9,
            fontweight="bold",
        )
    ax.set_xticks(x)
    ax.set_xticklabels(sensors)
    ax.set_title("Beacon frames per sensor")
    ax.set_ylabel("Frames")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_int))

    # Panel B: % share per sensor
    ax = axes[1]
    bottom = np.zeros(len(sensors))
    for v, c, label in zip(order, colors, display_labels):
        values = sensor_pct[v].values
        ax.bar(x, values, bottom=bottom, color=c, edgecolor="white", linewidth=0.4, label=label)
        bottom += values
    ax.set_xticks(x)
    ax.set_xticklabels(sensors)
    ax.set_title("Vendor share per sensor (% of frames)")
    ax.set_ylabel("% of beacon frames")
    ax.yaxis.set_major_formatter(mticker.PercentFormatter())
    ax.set_ylim(0, 100)

    # Panel C: unique APs per sensor per vendor
    ax = axes[2]
    bottom = np.zeros(len(sensors))
    for v, c, label in zip(order, colors, display_labels):
        values = ap_matrix[v].values
        ax.bar(x, values, bottom=bottom, color=c, edgecolor="white", linewidth=0.4, label=label)
        bottom += values
    for i, total in enumerate(ap_matrix.sum(axis=1).values):
        ax.text(i, total, f"\nΣ {int(total):,}", ha="center", va="bottom", fontsize=9, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(sensors)
    ax.set_title("Unique BSSIDs heard per sensor (stacked by vendor)")
    ax.set_ylabel("Unique BSSIDs")

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        title="Vendor",
        loc="center right",
        bbox_to_anchor=(1.0, 0.5),
        fontsize=9,
    )
    plt.tight_layout(rect=(0, 0, 0.93, 0.95))
    save_fig(fig, out, "ap_vendor_per_sensor")


# --------------------------------------------------------------------------- #
# Figure 5: Vendor landmark quality (multi-sensor coverage + RSSI stability)
# --------------------------------------------------------------------------- #
def fig_vendor_landmark_quality(df: pd.DataFrame, inv: pd.DataFrame, out: Path, top_n: int, experiment: str) -> None:
    """Answer: which vendor's APs are positioning-grade landmarks?

    Two cuts:
      1. Multi-sensor coverage: stacked share of APs heard by 1..5 sensors.
         Positioning needs at least 3 simultaneous fixes, so 3-of-5 is the
         meaningful threshold.
      2. RSSI stability: per-AP RSSI standard deviation distribution, vendor
         by vendor. Lower std = more stable landmark.
    """
    _, inv_c, order, merged = collapse_to_top_n(df, inv, top_n)
    display_labels = [other_legend_label(v, merged) for v in order]

    sensor_buckets = list(range(1, 6))  # 1..5 sensors
    coverage = (
        inv_c.assign(bucket=inv_c["sensors_seeing"].clip(upper=5))
        .groupby(["vendor", "bucket"])
        .size()
        .unstack(fill_value=0)
        .reindex(index=order, columns=sensor_buckets, fill_value=0)
    )
    coverage_pct = coverage.div(coverage.sum(axis=1).replace(0, np.nan), axis=0) * 100

    fig, axes = plt.subplots(1, 2, figsize=(18, 6.5), gridspec_kw={"width_ratios": [1.0, 1.2]})
    fig.suptitle(
        f"Vendor landmark quality — {experiment}  (positioning suitability of each vendor's APs)",
        fontsize=14,
        fontweight="bold",
    )

    # Panel A: multi-sensor visibility share
    ax = axes[0]
    bucket_colors = ["#e74c3c", "#e67e22", "#f1c40f", "#2ecc71", "#16a085"]
    bottom = np.zeros(len(order))
    x = np.arange(len(order))
    for bucket, color in zip(sensor_buckets, bucket_colors):
        values = coverage_pct[bucket].fillna(0).values
        label = f"{bucket} sensor{'s' if bucket > 1 else ''}"
        ax.bar(x, values, bottom=bottom, color=color, edgecolor="white", linewidth=0.4, label=label)
        bottom += values
    ax.set_xticks(x)
    ax.set_xticklabels(display_labels, rotation=30, ha="right")
    ax.set_ylabel("% of APs in vendor")
    ax.set_title("Multi-sensor visibility per vendor (≥3 sensors = positioning-grade)")
    ax.yaxis.set_major_formatter(mticker.PercentFormatter())
    # Reserve headroom above 100% bars for the AP-count annotations so they
    # don't collide with the subplot title.
    ax.set_ylim(0, 118)
    ax.set_yticks([0, 20, 40, 60, 80, 100])
    for i, total in enumerate(coverage.sum(axis=1).values):
        ax.text(i, 101, f"{int(total)} APs", ha="center", va="bottom", fontsize=8, color="#333")
    ax.legend(title="Heard by", loc="lower right", fontsize=8)

    # Panel B: RSSI stability box per vendor (APs with enough beacons to be
    # meaningful — at least 30 frames and at least 1 sensor)
    ax = axes[1]
    stable_inv = inv_c[inv_c["beacon_count"] >= 30].copy()
    # Use vendor-order intersect with what's present.
    present = [v for v in order if v in stable_inv["vendor"].unique()]
    stable_labels = [other_legend_label(v, merged) for v in present]
    box_data = [stable_inv.loc[stable_inv["vendor"] == v, "rssi_std"].dropna().values for v in present]
    colors = vendor_palette_for(present)
    bp = ax.boxplot(
        box_data,
        patch_artist=True,
        widths=0.6,
        flierprops=dict(marker=".", markersize=3, alpha=0.4),
        medianprops=dict(color="black", linewidth=1.2),
    )
    for patch, c in zip(bp["boxes"], colors):
        patch.set_facecolor(c)
        patch.set_edgecolor("white")
    ax.set_xticks(range(1, len(present) + 1))
    ax.set_xticklabels(stable_labels, rotation=30, ha="right")
    ax.set_ylabel("Per-AP RSSI std dev (dB)")
    ax.set_title("RSSI stability per vendor (APs with ≥30 frames; lower = more stable landmark)")
    # Annotate n per vendor
    ymax = max([float(d.max()) for d in box_data if len(d)], default=10) + 1
    ax.set_ylim(0, ymax * 1.15)
    for i, (v, vals) in enumerate(zip(present, box_data), start=1):
        ax.text(i, ymax * 1.05, f"n={len(vals)}", ha="center", va="bottom", fontsize=8, color="#333")

    plt.tight_layout(rect=(0, 0, 1, 0.94))
    save_fig(fig, out, "ap_vendor_landmark_quality")


def print_summary(df: pd.DataFrame, inv: pd.DataFrame) -> None:
    order = vendor_order_by_frames(df)
    print("\nVendor summary (frames / unique APs / virtual share):")
    vendor_frames = df["vendor"].value_counts()
    vendor_aps = inv["vendor"].value_counts()
    virtual_share = (
        inv.groupby("vendor")["is_virtual"]
        .apply(lambda s: 100.0 * s.sum() / max(len(s), 1))
        .round(1)
    )
    summary = pd.DataFrame(
        {
            "frames": vendor_frames,
            "unique_aps": vendor_aps,
            "virtual_share_pct": virtual_share,
        }
    ).reindex(order).fillna(0)
    summary["frames"] = summary["frames"].astype(int)
    summary["unique_aps"] = summary["unique_aps"].astype(int)
    print(summary.to_string())
    print(f"\nTotal beacon/probe frames: {len(df):,}")
    print(f"Total unique BSSIDs:       {inv['bssid'].nunique():,}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment",
        default=DEFAULT_EXPERIMENT,
        help="Experiment id under artifacts/analysis/<id>/ (default: exp174).",
    )
    parser.add_argument("--csv", type=Path, help="Override the input CSV path.")
    parser.add_argument("--out", type=Path, help="Override the output figure directory.")
    parser.add_argument("--manuf", type=Path, default=DEFAULT_MANUF_PICKLE)
    parser.add_argument(
        "--top-n",
        type=int,
        default=TOP_N_DEFAULT,
        help="Show the top-N vendors by frame count; collapse the rest into 'Other'.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    csv_default, out_default = dataset_paths_for(args.experiment)
    csv = args.csv or csv_default
    out = args.out or out_default
    experiment = args.experiment

    configure_plotting()
    manuf = load_manuf(args.manuf)
    resolve = build_vendor_resolver(manuf)
    df = load_frames(csv, resolve)
    if df.empty:
        print(f"No beacon/probe-response rows in {csv}. Aborting.", file=sys.stderr)
        return 2
    inv = build_inventory(df)
    print_summary(df, inv)
    save_summary_tables(df, inv, tables_dir_for(experiment))

    fig_vendor_overview(df, inv, out, args.top_n, experiment)
    fig_vendor_rssi_boxplot(df, inv, out, args.top_n, experiment)
    fig_vendor_channel(df, inv, out, args.top_n, experiment)
    fig_vendor_per_sensor(df, inv, out, args.top_n, experiment)
    fig_vendor_landmark_quality(df, inv, out, args.top_n, experiment)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
