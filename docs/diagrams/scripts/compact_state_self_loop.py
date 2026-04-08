#!/usr/bin/env python3
"""Compact Mermaid state self-loop geometry for readability.

Mermaid renders state self-loops (A --> A) as a wide 3-segment path that can
drop far below the diagram. This post-process compacts the UPLOADING loop in
the state-run-lifecycle SVG to a tight right-side loop.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


UPLOADING_NODE_RE = re.compile(
    r'<g class="node [^"]*statediagram-state" id="state-UPLOADING-[^"]+" '
    r'transform="translate\(([-0-9.]+),\s*([-0-9.]+)\)">'
)

PATH_TAG_RE_TEMPLATE = r'(<path (?=[^>]*\bid="{id}")[^>]*>)'
D_ATTR_RE = re.compile(r'(\bd=")([^"]*)(")')
EDGE_LABEL_RE = re.compile(
    r'(<g class="edgeLabel" transform="translate\()[-0-9.]+,\s*[-0-9.]+(\)">\s*'
    r'<g class="label" data-id="UPLOADING-cyclic-special-mid")'
)
NODE_LABEL_RE_TEMPLATE = (
    r'(<g [^>]*\bid="{id}"[^>]*\btransform="translate\()'
    r'[-0-9.]+,\s*[-0-9.]+(\))(")'
)
STATE_X_RE_TEMPLATE = (
    r'(<g class="node [^"]*" id="state-{state}-[^"]+" transform="translate\()'
    r'[-0-9.]+,\s*([-0-9.]+)(\)")'
)
STATE_Y_RE_TEMPLATE = (
    r'(<g class="node [^"]*" id="state-{state}-[^"]+" transform="translate\([\-0-9.]+,\s*)'
    r'[-0-9.]+(\)")'
)
VIEWBOX_RE = re.compile(r'(viewBox=")([-0-9.]+) ([-0-9.]+) ([-0-9.]+) ([-0-9.]+)(")')
MAX_WIDTH_RE = re.compile(r'(max-width:\s*)([-0-9.]+)(px;)')
SLEEPING_NODE_RE = re.compile(
    r'<g class="node [^"]*statediagram-state" id="state-SLEEPING-[^"]+" '
    r'transform="translate\(([-0-9.]+),\s*([-0-9.]+)\)">'
)
STATE_CENTER_RE_TEMPLATE = (
    r'id="state-{state}-[^"]+" transform="translate\(([-0-9.]+),\s*([-0-9.]+)\)"'
)
STATE_BOX_RE_TEMPLATE = (
    r'<g class="node [^"]*statediagram-state" id="state-{state}-[^"]+" '
    r'transform="translate\(([-0-9.]+),\s*([-0-9.]+)\)">'
    r'.*?<rect class="basic label-container"[^>]*\bx="([-0-9.]+)"[^>]*'
    r'\by="([-0-9.]+)"[^>]*\bwidth="([-0-9.]+)"[^>]*\bheight="([-0-9.]+)"'
)


def _fmt(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _set_path_d(svg_text: str, path_id: str, d_value: str) -> tuple[str, int]:
    pattern = re.compile(PATH_TAG_RE_TEMPLATE.format(id=re.escape(path_id)))
    count = 0

    def _replace(m: re.Match[str]) -> str:
        nonlocal count
        tag = m.group(1)
        if not D_ATTR_RE.search(tag):
            return tag
        count += 1
        return D_ATTR_RE.sub(lambda mm: f'{mm.group(1)}{d_value}{mm.group(3)}', tag, count=1)

    return pattern.sub(_replace, svg_text), count


def _state_center(svg_text: str, state_key: str) -> tuple[float, float] | None:
    m = re.search(STATE_CENTER_RE_TEMPLATE.format(state=state_key), svg_text)
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def _set_state_x(svg_text: str, state_key: str, target_x: float) -> tuple[str, int]:
    pat = re.compile(STATE_X_RE_TEMPLATE.format(state=re.escape(state_key)))
    replaced = 0

    def _repl(m: re.Match[str]) -> str:
        nonlocal replaced
        replaced += 1
        return f"{m.group(1)}{_fmt(target_x)}, {m.group(2)}{m.group(3)}"

    return pat.sub(_repl, svg_text, count=1), replaced


def _set_state_y(svg_text: str, state_key: str, target_y: float) -> tuple[str, int]:
    pat = re.compile(STATE_Y_RE_TEMPLATE.format(state=re.escape(state_key)))
    replaced = 0

    def _repl(m: re.Match[str]) -> str:
        nonlocal replaced
        replaced += 1
        return f"{m.group(1)}{_fmt(target_y)}{m.group(2)}"

    return pat.sub(_repl, svg_text, count=1), replaced


def _set_edge_label_xy(svg_text: str, edge_id: str, x: float, y: float) -> tuple[str, int]:
    pat = re.compile(
        rf'(<g class="edgeLabel" transform="translate\()[-0-9.]+,\s*[-0-9.]+(\)">\s*<g class="label" data-id="{re.escape(edge_id)}")'
    )
    return pat.subn(lambda m: f"{m.group(1)}{_fmt(x)}, {_fmt(y)}{m.group(2)}", svg_text, count=1)


def _state_box(svg_text: str, state_key: str) -> dict[str, float] | None:
    m = re.search(STATE_BOX_RE_TEMPLATE.format(state=re.escape(state_key)), svg_text, flags=re.S)
    if not m:
        return None
    cx = float(m.group(1))
    cy = float(m.group(2))
    rx = float(m.group(3))
    ry = float(m.group(4))
    rw = float(m.group(5))
    rh = float(m.group(6))
    return {
        "x": cx,
        "y": cy,
        "left": cx + rx,
        "right": cx + rx + rw,
        "top": cy + ry,
        "bottom": cy + ry + rh,
    }


def patch_svg(svg_text: str) -> tuple[str, int]:
    mw = MAX_WIDTH_RE.search(svg_text)
    if mw:
        current = float(mw.group(2))
        target = max(current, 1200.0)
        if abs(target - current) > 1e-6:
            svg_text = MAX_WIDTH_RE.sub(lambda mm: f"{mm.group(1)}{_fmt(target)}{mm.group(3)}", svg_text, count=1)
            patched = 1
        else:
            patched = 0
    else:
        patched = 0

    vm = VIEWBOX_RE.search(svg_text)
    if vm:
        vb_x = float(vm.group(2))
        vb_y = float(vm.group(3))
        vb_w = float(vm.group(4))
        vb_h = float(vm.group(5))
        target_w = max(vb_w, 1560.0)
        if abs(target_w - vb_w) > 1e-6:
            svg_text = VIEWBOX_RE.sub(
                lambda mm: f'{mm.group(1)}{_fmt(vb_x)} {_fmt(vb_y)} {_fmt(target_w)} {_fmt(vb_h)}{mm.group(6)}',
                svg_text,
                count=1,
            )
            patched += 1
        center_x = target_w / 2.0
    else:
        preparing0 = _state_center(svg_text, "PREPARING")
        if not preparing0:
            return svg_text, 0
        center_x = preparing0[0]

    # Center all main boxes on the canvas centerline.
    for state_key in ("title", "root_start", "PREPARING", "EXECUTING", "MAINTAINING", "UPLOADING", "SLEEPING"):
        svg_text, c = _set_state_x(svg_text, state_key, center_x)
        patched += c
    # Keep start dot below title box so it does not overlap title text.
    title_center = _state_center(svg_text, "title")
    if title_center:
        svg_text, c = _set_state_y(svg_text, "root_start", title_center[1] + 52.0)
        patched += c
    # Keep note on the left side with extra room after widening.
    svg_text, c = _set_state_x(svg_text, "MAINTAINING----note", center_x - 620.0)
    patched += c

    preparing = _state_center(svg_text, "PREPARING")
    executing = _state_center(svg_text, "EXECUTING")
    maintaining = _state_center(svg_text, "MAINTAINING")
    uploading = _state_center(svg_text, "UPLOADING")
    sleeping = _state_center(svg_text, "SLEEPING")
    root_start = _state_center(svg_text, "root_start")
    if not (preparing and executing and maintaining and uploading and sleeping and root_start):
        return svg_text, patched

    px, py = preparing
    ex, ey = executing
    mx, my = maintaining
    ux, uy = uploading
    sx, sy = sleeping
    rx, ry = root_start

    pbox = _state_box(svg_text, "PREPARING")
    ebox = _state_box(svg_text, "EXECUTING")
    mbox = _state_box(svg_text, "MAINTAINING")
    ubox = _state_box(svg_text, "UPLOADING")
    sbox = _state_box(svg_text, "SLEEPING")
    if not (pbox and ebox and mbox and ubox and sbox):
        return svg_text, patched

    # Keep transitions attached after centering.
    # Prefer straight vertical arrows when source/target are on one column.
    d_edge0 = f"M {_fmt(rx)},{_fmt(ry + 7.0)} L {_fmt(px)},{_fmt(pbox['top'])}"
    d_edge1 = f"M {_fmt(px)},{_fmt(pbox['bottom'])} L {_fmt(ex)},{_fmt(ebox['top'])}"
    d_edge2 = f"M {_fmt(ex)},{_fmt(ebox['bottom'])} L {_fmt(mx)},{_fmt(mbox['top'])}"
    d_edge3 = f"M {_fmt(mx)},{_fmt(mbox['bottom'])} L {_fmt(ux)},{_fmt(ubox['top'])}"
    d_edge4 = f"M {_fmt(ux)},{_fmt(ubox['bottom'])} L {_fmt(sx)},{_fmt(sbox['top'])}"
    rail_left = px - 280.0
    rail_right = px + 300.0
    rail_upload_return = px - 240.0
    d_edge5 = (
        f"M {_fmt(pbox['left'])},{_fmt(py)} "
        f"L {_fmt(rail_left)},{_fmt(py)} "
        f"L {_fmt(rail_left)},{_fmt(sy)} "
        f"L {_fmt(sbox['left'])},{_fmt(sy)}"
    )
    d_edge6 = (
        f"M {_fmt(sbox['right'])},{_fmt(sy)} "
        f"L {_fmt(rail_right)},{_fmt(sy)} "
        f"L {_fmt(rail_right)},{_fmt(py)} "
        f"L {_fmt(pbox['right'])},{_fmt(py)}"
    )
    d_edge7 = (
        f"M {_fmt(ubox['left'])},{_fmt(uy)} "
        f"L {_fmt(rail_upload_return)},{_fmt(uy)} "
        f"L {_fmt(rail_upload_return)},{_fmt(my)} "
        f"L {_fmt(mbox['left'])},{_fmt(my)}"
    )

    for pid, dval in (
        ("edge0", d_edge0),
        ("edge1", d_edge1),
        ("edge2", d_edge2),
        ("edge3", d_edge3),
        ("edge4", d_edge4),
        ("edge5", d_edge5),
        ("edge6", d_edge6),
        ("edge7", d_edge7),
    ):
        svg_text, c = _set_path_d(svg_text, pid, dval)
        patched += c

    # Re-position edge labels after centering.
    for eid, lx, ly in (
        ("edge1", px + 120.0, (py + ey) / 2.0),
        ("edge2", px + 120.0, (ey + my) / 2.0),
        ("edge3", (ubox["left"] + rail_upload_return) / 2.0 + 38.0, (my + uy) / 2.0),
        ("edge4", px, (uy + sy) / 2.0 + 8.0),
        ("edge5", rail_left, (ey + my) / 2.0),
        ("edge6", rail_right, (ey + my) / 2.0),
        ("edge7", (ubox["left"] + rail_upload_return) / 2.0, uy + 64.0),
    ):
        svg_text, c = _set_edge_label_xy(svg_text, eid, lx, ly)
        patched += c

    # Square-like queue-drained loop:
    # start at UPLOADING right-side center and end at top-side center.
    start_x = ubox["right"]
    start_y = uy
    right_x = ubox["right"] + 120.0
    top_y = ubox["top"] - 56.0
    mid_x = ux
    return_x = ux
    return_y = ubox["top"]
    d1 = (
        f"M { _fmt(start_x) },{ _fmt(start_y) } "
        f"L { _fmt(right_x) },{ _fmt(start_y) } "
        f"L { _fmt(right_x) },{ _fmt(top_y) }"
    )
    dmid = (
        f"M { _fmt(right_x) },{ _fmt(top_y) } "
        f"L { _fmt(mid_x) },{ _fmt(top_y) }"
    )
    d2 = (
        f"M { _fmt(mid_x) },{ _fmt(top_y) } "
        f"L { _fmt(return_x) },{ _fmt(return_y) }"
    )

    for pid, dval in (
        ("UPLOADING-cyclic-special-1", d1),
        ("UPLOADING-cyclic-special-mid", dmid),
        ("UPLOADING-cyclic-special-2", d2),
    ):
        svg_text, c = _set_path_d(svg_text, pid, dval)
        patched += c

    # Place queue-drained text below the loop and slightly left so no loop
    # segment overlaps the text.
    label_x = mid_x + 156.0
    label_y = return_y + 56.0
    svg_text, c = EDGE_LABEL_RE.subn(
        lambda mm: f'{mm.group(1)}{_fmt(label_x)}, {_fmt(label_y)}{mm.group(2)}',
        svg_text,
        count=1,
    )
    patched += c

    # Move hidden helper nodes used by Mermaid's self-loop splitting.
    for helper_id, hx, hy in (
        ("UPLOADING---UPLOADING---1", right_x, top_y),
        ("UPLOADING---UPLOADING---2", mid_x, top_y),
    ):
        helper_re = re.compile(NODE_LABEL_RE_TEMPLATE.format(id=re.escape(helper_id)))
        svg_text, c = helper_re.subn(
            lambda mm: f'{mm.group(1)}{_fmt(hx)}, {_fmt(hy)}{mm.group(2)}{mm.group(3)}',
            svg_text,
            count=1,
        )
        patched += c

    # Reduce excess whitespace introduced by Mermaid's original self-loop bbox.
    # Keep enough bottom padding for the SLEEPING state and labels.
    vm = VIEWBOX_RE.search(svg_text)
    if vm:
        current_h = float(vm.group(5))
        sleeping_m = SLEEPING_NODE_RE.search(svg_text)
        if sleeping_m:
            sleeping_y = float(sleeping_m.group(2))
            target_h = sleeping_y + 70.0
        else:
            target_h = uy + 260.0
        if current_h - target_h > 20.0:
            new_h = max(target_h, 760.0)
            svg_text = VIEWBOX_RE.sub(
                lambda mm: (
                    f'{mm.group(1)}{mm.group(2)} {mm.group(3)} '
                    f'{mm.group(4)} {_fmt(new_h)}{mm.group(6)}'
                ),
                svg_text,
                count=1,
            )
            patched += 1

    return svg_text, patched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("svg_files", nargs="+")
    args = parser.parse_args()

    total = 0
    for file_path in args.svg_files:
        path = Path(file_path)
        original = path.read_text(encoding="utf-8", errors="ignore")
        patched_text, count = patch_svg(original)
        if patched_text != original:
            path.write_text(patched_text, encoding="utf-8")
        total += count
        print(f"{path}: patched_compact_self_loop={count}")

    print(f"total_patched_compact_self_loop={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
