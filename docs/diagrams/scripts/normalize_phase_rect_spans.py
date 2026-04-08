#!/usr/bin/env python3
"""Normalize target color rect spans in sequence SVGs.

For active sequence diagrams, make selected background rects align to the
actor lanes with consistent margins:
- phase blocks: full-span
- command-highlight blocks: inset inside phase blocks
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


# Top-level phase colors.
PHASE_COLORS = {
    "232,245,233",  # PREPARE
    "227,242,253",  # EXECUTION / event internals
    "255,243,224",  # MAINTENANCE / AUTHORING
}

# Command highlight colors.
COMMAND_COLORS = {
    "220,252,231",  # command hello
    "255,228,230",  # command upload
    "254,249,195",  # command LogMetrics
    "237,233,254",  # command LogLogs
    "255,237,213",  # command measure
    "226,232,240",  # command ExecuteShell
}
HELLO_COMMAND_COLOR = "220,252,231"

ACTOR_X_RE = re.compile(
    r'<line id="actor\d+" x1="([\-0-9.]+)" y1="[0-9.]+" x2="\1" y2="[\-0-9.]+" class="actor-line'
)

RECT_RE = re.compile(
    r'<rect x="([\-0-9.]+)" y="([\-0-9.]+)" fill="rgb\(([^)]*)\)" width="([\-0-9.]+)" height="([\-0-9.]+)" class="rect"/>'
)

def _fmt(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


def patch_svg(
    svg_text: str,
    phase_side_margin: float,
    command_side_margin: float,
    command_top_overlap: float,
    hello_top_overlap: float,
) -> tuple[str, int]:
    actor_x = [float(x) for x in ACTOR_X_RE.findall(svg_text)]
    if not actor_x:
        return svg_text, 0

    phase_left = min(actor_x) - phase_side_margin
    phase_right = max(actor_x) + phase_side_margin
    phase_width = phase_right - phase_left

    command_left = min(actor_x) - command_side_margin
    command_right = max(actor_x) + command_side_margin
    command_width = command_right - command_left

    patched = 0

    def _replace(m: re.Match[str]) -> str:
        nonlocal patched
        _, y, color, _, height = m.groups()
        if color in PHASE_COLORS:
            patched += 1
            return (
                f'<rect x="{_fmt(phase_left)}" y="{y}" fill="rgb({color})" '
                f'width="{_fmt(phase_width)}" height="{height}" class="rect"/>'
            )
        if color in COMMAND_COLORS:
            top_overlap = hello_top_overlap if color == HELLO_COMMAND_COLOR else command_top_overlap
            y_value = float(y)
            h_value = float(height)
            y_value -= top_overlap
            h_value += top_overlap
            patched += 1
            return (
                f'<rect x="{_fmt(command_left)}" y="{_fmt(y_value)}" fill="rgb({color})" '
                f'width="{_fmt(command_width)}" height="{_fmt(h_value)}" class="rect"/>'
            )
        return m.group(0)

    out = RECT_RE.sub(_replace, svg_text)
    return out, patched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("svg_files", nargs="+")
    parser.add_argument(
        "--phase-side-margin",
        type=float,
        default=32.0,
        help="Desired side margin from first/last actor lane to phase rect edge.",
    )
    parser.add_argument(
        "--command-side-margin",
        type=float,
        default=9.0,
        help="Desired side margin from first/last actor lane to command rect edge.",
    )
    parser.add_argument(
        "--command-top-overlap",
        type=float,
        default=36.0,
        help="Extra top extension for command rects so command alt labels sit inside color blocks.",
    )
    parser.add_argument(
        "--hello-top-overlap",
        type=float,
        default=54.0,
        help="Extra top extension for hello command rect (first command branch needs larger overlap).",
    )
    args = parser.parse_args()

    total = 0
    for file_path in args.svg_files:
        path = Path(file_path)
        original = path.read_text(encoding="utf-8", errors="ignore")
        patched_text, count = patch_svg(
            original,
            args.phase_side_margin,
            args.command_side_margin,
            args.command_top_overlap,
            args.hello_top_overlap,
        )
        if patched_text != original:
            path.write_text(patched_text, encoding="utf-8")
        total += count
        print(f"{path}: patched_phase_rects={count}")

    print(f"total_patched_phase_rects={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
