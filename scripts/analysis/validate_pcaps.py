#!/usr/bin/env python3
"""Quick pre-normalization sanity check for WiFi monitor pcaps.

Run this BEFORE prepare_measurement_dataset.py to avoid spending an hour parsing
a corrupt, truncated, or empty capture. For each pcap, we sample the first N
packets and the last N packets via tcpdump (cheap because tcpdump streams),
then report:

  - file size + readable pcap magic
  - first-packet and last-packet timestamps  → capture duration
  - frame count from the sample window
  - count of unique BSSID-like MAC tokens in the sample
  - count of frames carrying a 5 GHz channel frequency

A pcap is flagged GREEN if it parses, has a duration of at least a few minutes,
and shows non-trivial BSSID / 5 GHz coverage. Anything else is RED with a
specific reason so the operator can decide whether to re-collect.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Match BSSID-like tokens in tcpdump output (lowercase hex with colons).
MAC_RE = re.compile(r"\b([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b")
# tcpdump radiotap line: "... 5180 MHz 11a ..."
FREQ_RE = re.compile(r"\b(5\d{3})\s*MHz\b")
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)")

SAMPLE_PACKETS = 200_000  # how many packets to scan from the head of the pcap


@dataclass
class PcapReport:
    path: Path
    ok: bool = True
    issues: list[str] = field(default_factory=list)
    file_size_bytes: int = 0
    sampled_packets: int = 0
    first_ts: str = ""
    last_ts: str = ""
    duration_seconds: float = 0.0
    unique_macs_in_sample: int = 0
    frames_with_5ghz_freq: int = 0

    def fail(self, reason: str) -> None:
        self.ok = False
        self.issues.append(reason)


def parse_timestamp(line: str) -> str | None:
    m = TS_RE.match(line)
    if m:
        return m.group(1)
    return None


def check_pcap(path: Path) -> PcapReport:
    report = PcapReport(path=path)
    try:
        report.file_size_bytes = path.stat().st_size
    except FileNotFoundError:
        report.fail("file does not exist")
        return report
    if report.file_size_bytes == 0:
        report.fail("file is empty")
        return report

    # Validate pcap magic bytes (pcap classic / pcapng).
    with path.open("rb") as fh:
        magic = fh.read(4)
    if magic not in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x0a\x0d\x0d\x0a"):
        report.fail(f"unknown pcap magic bytes: {magic.hex()}")
        # keep going — tcpdump may still parse it

    # Sample first N packets via tcpdump for fast inspection (-c limits count).
    cmd = ["tcpdump", "-r", str(path), "-tttt", "-e", "-n", "-c", str(SAMPLE_PACKETS)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        report.fail("tcpdump binary not found on PATH")
        return report
    if result.returncode != 0 and not result.stdout:
        report.fail(f"tcpdump failed: {result.stderr.strip().splitlines()[-1] if result.stderr else 'no output'}")
        return report

    macs: set[str] = set()
    freq_hits = 0
    first_ts: str | None = None
    last_ts_seen: str | None = None
    line_count = 0
    for line in result.stdout.splitlines():
        ts = parse_timestamp(line)
        if ts:
            line_count += 1
            if first_ts is None:
                first_ts = ts
            last_ts_seen = ts
        # MAC harvesting — pull all addresses, BSSID is among them.
        for mac in MAC_RE.findall(line.lower()):
            if mac == "ff:ff:ff:ff:ff:ff":
                continue
            macs.add(mac)
        if FREQ_RE.search(line):
            freq_hits += 1

    report.sampled_packets = line_count
    report.first_ts = first_ts or ""
    report.last_ts = last_ts_seen or ""
    report.unique_macs_in_sample = len(macs)
    report.frames_with_5ghz_freq = freq_hits

    if report.sampled_packets == 0:
        report.fail("no packets parsed from sample window")
        return report

    # Compute duration spanned by the sample.
    if first_ts and last_ts_seen and first_ts != last_ts_seen:
        from datetime import datetime
        try:
            t0 = datetime.fromisoformat(first_ts.replace(" ", "T"))
            t1 = datetime.fromisoformat(last_ts_seen.replace(" ", "T"))
            report.duration_seconds = max(0.0, (t1 - t0).total_seconds())
        except ValueError:
            pass

    # Heuristics: these come from real exp174 captures.
    if report.unique_macs_in_sample < 5:
        report.fail(
            f"sample has only {report.unique_macs_in_sample} unique MAC(s) — "
            "capture may be looking at the wrong interface"
        )
    if report.frames_with_5ghz_freq == 0:
        report.fail(
            "no frames with 5 GHz channel frequency in sample — capture may not "
            "have been on a monitor-mode 5 GHz interface"
        )
    if report.duration_seconds and report.duration_seconds < 60:
        report.fail(
            f"sample only spans {report.duration_seconds:.1f}s — far below the "
            "expected multi-hour window"
        )

    return report


def render(reports: list[PcapReport]) -> str:
    lines = []
    width = max((len(r.path.name) for r in reports), default=20)
    header = f"{'pcap'.ljust(width)}  {'size':>10}  {'packets':>10}  {'span (s)':>10}  {'MACs':>6}  {'5GHz':>6}  status"
    lines.append(header)
    lines.append("-" * len(header))
    for r in reports:
        status = "OK   " if r.ok else "FAIL "
        size_mb = r.file_size_bytes / (1024 * 1024)
        lines.append(
            f"{r.path.name.ljust(width)}  "
            f"{size_mb:>8.1f}MB  "
            f"{r.sampled_packets:>10,}  "
            f"{r.duration_seconds:>10.1f}  "
            f"{r.unique_macs_in_sample:>6,}  "
            f"{r.frames_with_5ghz_freq:>6,}  "
            f"{status}"
        )
        for issue in r.issues:
            lines.append(f"  └─ {issue}")
        if r.ok and r.first_ts:
            lines.append(f"  └─ first {r.first_ts} → last {r.last_ts} (sample of {SAMPLE_PACKETS:,} pkts)")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap", nargs="+", type=Path, help="pcap files to validate")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reports = [check_pcap(p) for p in args.pcap]
    print(render(reports))
    all_ok = all(r.ok for r in reports)
    print()
    print("RESULT:", "all pcaps look valid" if all_ok else "one or more pcaps failed validation")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
