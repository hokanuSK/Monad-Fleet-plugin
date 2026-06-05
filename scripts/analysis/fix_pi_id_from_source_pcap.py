#!/usr/bin/env python3
"""Rewrite the pi_id column of a normalized wifi_5g_frames.csv in place.

When prepare_measurement_dataset.py is fed pcaps whose filenames lack the
canonical `monad-XX` prefix (e.g. when the raw layout uses MAC-address
directories), every row gets `pi_id="unknown"` and all five sensors collapse
into a single bucket. This script repairs that by deriving pi_id from the
source_pcap column using `metadata.json`'s device_map (MAC → pi).

The original CSV is moved to <name>.pi_id_unknown.bak before being rewritten.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

MAC_RE = re.compile(r"([0-9a-f]{2}(?:-[0-9a-f]{2}){5})")


def load_mac_to_pi(metadata_path: Path) -> dict[str, str]:
    meta = json.loads(metadata_path.read_text())
    return {
        mac.lower().replace(":", "-"): pi
        for pi, mac in (meta.get("device_map") or {}).items()
        if isinstance(mac, str)
    }


def derive_pi(source_pcap: str, mac_to_pi: dict[str, str]) -> str:
    if not source_pcap:
        return "unknown"
    p = source_pcap.lower()
    m = re.search(r"monad[-_]?(\d+)", p)
    if m:
        return f"monad-{int(m.group(1)):02d}"
    mac_m = MAC_RE.search(p)
    if mac_m:
        return mac_to_pi.get(mac_m.group(1), "unknown")
    return "unknown"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip writing a .pi_id_unknown.bak copy of the original.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.csv.exists():
        sys.exit(f"CSV not found: {args.csv}")
    if not args.metadata.exists():
        sys.exit(f"metadata.json not found: {args.metadata}")

    mac_to_pi = load_mac_to_pi(args.metadata)
    if not mac_to_pi:
        sys.exit("metadata.json has no device_map; nothing to recover from")
    print(f"Loaded {len(mac_to_pi)} MAC→pi mappings: {mac_to_pi}", flush=True)

    tmp = args.csv.with_suffix(args.csv.suffix + ".rewriting")
    bak = args.csv.with_suffix(args.csv.suffix + ".pi_id_unknown.bak")

    rewritten = 0
    untouched = 0
    pi_cache: dict[str, str] = {}
    with args.csv.open("r", newline="") as src, tmp.open("w", newline="") as dst:
        reader = csv.DictReader(src)
        fieldnames = reader.fieldnames or []
        if "pi_id" not in fieldnames or "source_pcap" not in fieldnames:
            tmp.unlink(missing_ok=True)
            sys.exit("CSV is missing required pi_id or source_pcap columns")
        writer = csv.DictWriter(dst, fieldnames=fieldnames)
        writer.writeheader()
        for row in reader:
            pi = row.get("pi_id") or ""
            if pi in ("", "unknown"):
                src_pcap = row.get("source_pcap") or ""
                resolved = pi_cache.get(src_pcap)
                if resolved is None:
                    resolved = derive_pi(src_pcap, mac_to_pi)
                    pi_cache[src_pcap] = resolved
                if resolved != "unknown":
                    row["pi_id"] = resolved
                    rewritten += 1
                else:
                    untouched += 1
            writer.writerow(row)

    if not args.no_backup and not bak.exists():
        args.csv.rename(bak)
        print(f"Backed up original to {bak}", flush=True)
    else:
        args.csv.unlink()
    tmp.rename(args.csv)

    print(f"Done. Rewrote pi_id on {rewritten:,} rows; left {untouched:,} as unknown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
