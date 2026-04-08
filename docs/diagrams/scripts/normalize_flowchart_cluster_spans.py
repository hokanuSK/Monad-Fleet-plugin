#!/usr/bin/env python3
"""Normalize flowchart cluster spans for selected SVG diagrams.

For flowchart-windowed-upload-scheduler.svg we keep PREP, START, and MW on a
shared horizontal span, without disturbing node layout.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

CLUSTER_RECT_RE = re.compile(
    r'<g class="cluster" id="([^"]+)"[^>]*><rect[^>]*x="([\-0-9.]+)" y="([\-0-9.]+)" width="([\-0-9.]+)" height="([\-0-9.]+)"/>'
)


def _fmt(v: float) -> str:
    return f"{v:.6f}".rstrip("0").rstrip(".")


def patch_svg(svg_text: str, target_ids: list[str], reference_ids: list[str]) -> tuple[str, int]:
    clusters: dict[str, tuple[float, float]] = {}
    for m in CLUSTER_RECT_RE.finditer(svg_text):
        cid = m.group(1)
        x = float(m.group(2))
        w = float(m.group(4))
        clusters[cid] = (x, w)

    refs = [cid for cid in reference_ids if cid in clusters]
    if not refs:
        return svg_text, 0

    left = min(clusters[cid][0] for cid in refs)
    right = max(clusters[cid][0] + clusters[cid][1] for cid in refs)
    width = right - left

    patched = 0

    out = svg_text
    for target_id in target_ids:
        if target_id not in clusters:
            continue
        rect_re = re.compile(
            rf'(<g class="cluster" id="{re.escape(target_id)}"[^>]*><rect[^>]*x=")([\-0-9.]+)(" y="[\-0-9.]+" width=")([\-0-9.]+)(" height="[\-0-9.]+"/>)'
        )

        def _replace_rect(m: re.Match[str]) -> str:
            nonlocal patched
            cur_x = float(m.group(2))
            cur_w = float(m.group(4))
            if abs(cur_x - left) < 1e-6 and abs(cur_w - width) < 1e-6:
                return m.group(0)
            patched += 1
            return f"{m.group(1)}{_fmt(left)}{m.group(3)}{_fmt(width)}{m.group(5)}"

        out = rect_re.sub(_replace_rect, out, count=1)

        # Re-center the target cluster label after width update.
        label_re = re.compile(
            rf'(<g class="cluster" id="{re.escape(target_id)}"[^>]*><rect[^>]*?/><g class="cluster-label" transform="translate\()([\-0-9.]+)(,\s*[\-0-9.]+\)"><foreignObject width=")([\-0-9.]+)(" height=")',
            re.S,
        )

        def _replace_label(m: re.Match[str]) -> str:
            nonlocal patched
            label_w = float(m.group(4))
            new_x = left + (width - label_w) / 2.0
            cur_x = float(m.group(2))
            if abs(cur_x - new_x) < 1e-6:
                return m.group(0)
            patched += 1
            return f"{m.group(1)}{_fmt(new_x)}{m.group(3)}{m.group(4)}{m.group(5)}"

        out = label_re.sub(_replace_label, out, count=1)
    return out, patched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("svg_files", nargs="+")
    parser.add_argument(
        "--targets",
        nargs="+",
        default=["PREP", "START", "MW"],
        help="Cluster ids to resize to the shared span",
    )
    parser.add_argument("--target", default=None, help="(Deprecated) single cluster id to resize")
    parser.add_argument(
        "--reference",
        nargs="+",
        default=["PREP", "MW"],
        help="Reference cluster ids used to compute shared span",
    )
    args = parser.parse_args()

    total = 0
    targets = [args.target] if args.target else args.targets
    for file_path in args.svg_files:
        path = Path(file_path)
        original = path.read_text(encoding="utf-8", errors="ignore")
        patched_text, count = patch_svg(original, targets, args.reference)
        if patched_text != original:
            path.write_text(patched_text, encoding="utf-8")
        total += count
        print(f"{path}: patched_flowchart_clusters={count}")

    print(f"total_patched_flowchart_clusters={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
