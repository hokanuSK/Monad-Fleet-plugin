#!/usr/bin/env python3
"""Center upper/Execution axis and add a curved loopback edge for selected flowcharts."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def _fmt(v: float) -> str:
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _node_transform(svg_text: str, node_id: str) -> tuple[float, float] | None:
    m = re.search(
        rf'id="flowchart-{re.escape(node_id)}-\d+" transform="translate\(([\-0-9.]+), ([\-0-9.]+)\)"',
        svg_text,
    )
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def _rect_box(svg_text: str, node_id: str) -> dict[str, float] | None:
    m = re.search(
        rf'<g class="node[^"]*" id="flowchart-{re.escape(node_id)}-\d+" transform="translate\(([\-0-9.]+), ([\-0-9.]+)\)"><rect[^>]*x="([\-0-9.]+)" y="([\-0-9.]+)" width="([\-0-9.]+)" height="([\-0-9.]+)"',
        svg_text,
    )
    if not m:
        return None
    cx = float(m.group(1))
    cy = float(m.group(2))
    rx = float(m.group(3))
    ry = float(m.group(4))
    w = float(m.group(5))
    h = float(m.group(6))
    return {
        "x": cx,
        "y": cy,
        "left": cx + rx,
        "right": cx + rx + w,
        "top": cy + ry,
        "bottom": cy + ry + h,
    }


def _node_box(svg_text: str, node_id: str) -> dict[str, float] | None:
    rect = _rect_box(svg_text, node_id)
    if rect:
        return rect

    m = re.search(
        rf'<g class="node[^"]*" id="flowchart-{re.escape(node_id)}-\d+" transform="translate\(([\-0-9.]+), ([\-0-9.]+)\)"><polygon points="([^"]+)"[^>]*transform="translate\(([\-0-9.]+), ([\-0-9.]+)\)"',
        svg_text,
    )
    if not m:
        c = _node_transform(svg_text, node_id)
        if not c:
            return None
        cx, cy = c
        return {
            "x": cx,
            "y": cy,
            "left": cx - 60.0,
            "right": cx + 60.0,
            "top": cy - 60.0,
            "bottom": cy + 60.0,
        }

    cx = float(m.group(1))
    cy = float(m.group(2))
    pts = m.group(3)
    tx = float(m.group(4))
    ty = float(m.group(5))
    xs: list[float] = []
    ys: list[float] = []
    for pt in pts.split():
        px, py = pt.split(",")
        xs.append(float(px) + tx)
        ys.append(float(py) + ty)
    left = cx + min(xs)
    right = cx + max(xs)
    top = cy + min(ys)
    bottom = cy + max(ys)
    return {
        "x": cx,
        "y": cy,
        "left": left,
        "right": right,
        "top": top,
        "bottom": bottom,
    }


def _replace_node_x(svg_text: str, node_id: str, new_x: float) -> tuple[str, int]:
    patched = 0
    pat = re.compile(
        rf'(<g class="node[^"]*" id="flowchart-{re.escape(node_id)}-\d+" transform="translate\()([\-0-9.]+)(,\s*[\-0-9.]+\)")'
    )

    def _repl(m: re.Match[str]) -> str:
        nonlocal patched
        cur = float(m.group(2))
        if abs(cur - new_x) < 1e-6:
            return m.group(0)
        patched += 1
        return f"{m.group(1)}{_fmt(new_x)}{m.group(3)}"

    return pat.sub(_repl, svg_text, count=1), patched


def _replace_node_xy(
    svg_text: str, node_id: str, new_x: float | None = None, new_y: float | None = None
) -> tuple[str, int]:
    patched = 0
    pat = re.compile(
        rf'(<g class="node[^"]*" id="flowchart-{re.escape(node_id)}-\d+" transform="translate\()([\-0-9.]+), ([\-0-9.]+)(\)")'
    )

    def _repl(m: re.Match[str]) -> str:
        nonlocal patched
        cx = float(m.group(2))
        cy = float(m.group(3))
        nx = cx if new_x is None else new_x
        ny = cy if new_y is None else new_y
        if abs(cx - nx) < 1e-6 and abs(cy - ny) < 1e-6:
            return m.group(0)
        patched += 1
        return f"{m.group(1)}{_fmt(nx)}, {_fmt(ny)}{m.group(4)}"

    return pat.sub(_repl, svg_text, count=1), patched


def _set_rect_size(
    svg_text: str, node_id: str, new_w: float, new_h: float
) -> tuple[str, int]:
    patched = 0
    pat = re.compile(
        rf'(<g class="node[^"]*" id="flowchart-{re.escape(node_id)}-\d+" transform="translate\([^)]+\)"><rect[^>]*x=")([\-0-9.]+)(" y=")([\-0-9.]+)(" width=")([\-0-9.]+)(" height=")([\-0-9.]+)(")'
    )

    def _repl(m: re.Match[str]) -> str:
        nonlocal patched
        cur_w = float(m.group(6))
        cur_h = float(m.group(8))
        nx = -new_w / 2.0
        ny = -new_h / 2.0
        if abs(cur_w - new_w) < 1e-6 and abs(cur_h - new_h) < 1e-6:
            return m.group(0)
        patched += 1
        return (
            f'{m.group(1)}{_fmt(nx)}{m.group(3)}{_fmt(ny)}'
            f'{m.group(5)}{_fmt(new_w)}{m.group(7)}{_fmt(new_h)}{m.group(9)}'
        )

    return pat.sub(_repl, svg_text, count=1), patched


def _replace_edge_path(svg_text: str, edge_id: str, new_d: str) -> tuple[str, int]:
    patched = 0
    pat = re.compile(rf'(<path d=")([^"]+)(" id="{re.escape(edge_id)}"[^>]*?/>)')

    def _repl(m: re.Match[str]) -> str:
        nonlocal patched
        if m.group(2) == new_d:
            return m.group(0)
        patched += 1
        return f'{m.group(1)}{new_d}{m.group(3)}'

    return pat.sub(_repl, svg_text, count=1), patched


def _make_edge_labels_transparent(svg_text: str) -> tuple[str, int]:
    patched = 0
    replacements = (
        (".edgeLabel p{background-color:#ffffff;}", ".edgeLabel p{background-color:transparent;}"),
        (
            ".edgeLabel rect{opacity:0.5;background-color:#ffffff;fill:#ffffff;}",
            ".edgeLabel rect{opacity:0;background-color:transparent;fill:transparent;}",
        ),
        (".labelBkg{background-color:rgba(255, 255, 255, 0.5);}", ".labelBkg{background-color:transparent;}"),
    )
    out = svg_text
    for old, new in replacements:
        if old in out:
            out = out.replace(old, new)
            patched += 1
    return out, patched


def _edge_end_xy(svg_text: str, edge_id: str) -> tuple[float, float] | None:
    m = re.search(rf'<path d="([^"]+)" id="{re.escape(edge_id)}"', svg_text)
    if not m:
        return None
    pts = re.findall(r'([\-0-9.]+),([\-0-9.]+)', m.group(1))
    if not pts:
        return None
    x, y = pts[-1]
    return float(x), float(y)


def _edge_start_end_xy(
    svg_text: str, edge_id: str
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    m = re.search(rf'<path d="([^"]+)" id="{re.escape(edge_id)}"', svg_text)
    if not m:
        return None
    pts = re.findall(r'([\-0-9.]+),([\-0-9.]+)', m.group(1))
    if len(pts) < 2:
        return None
    sx, sy = pts[0]
    ex, ey = pts[-1]
    return (float(sx), float(sy)), (float(ex), float(ey))


def _set_edge_label_xy(svg_text: str, edge_id: str, x: float, y: float) -> tuple[str, int]:
    patched = 0
    pat = re.compile(
        rf'(<g class="edgeLabel" transform="translate\()([\-0-9.]+), ([\-0-9.]+)(\)"><g class="label" data-id="{re.escape(edge_id)}")'
    )

    def _repl(m: re.Match[str]) -> str:
        nonlocal patched
        cx = float(m.group(2))
        cy = float(m.group(3))
        if abs(cx - x) < 1e-6 and abs(cy - y) < 1e-6:
            return m.group(0)
        patched += 1
        return f"{m.group(1)}{_fmt(x)}, {_fmt(y)}{m.group(4)}"

    return pat.sub(_repl, svg_text, count=1), patched


def _curve_to_pm(sx: float, sy: float, ex: float, ey: float, from_right: bool) -> str:
    # Short down-curve with a mild side bend into PM.
    c1x = sx
    c1y = sy + 26
    c2x = ex + (42 if from_right else -42)
    c2y = (sy + ey) / 2.0
    return (
        f"M{_fmt(sx)},{_fmt(sy)}"
        f"L{_fmt(sx)},{_fmt(sy + 8)}"
        f"C{_fmt(c1x)},{_fmt(c1y)},{_fmt(c2x)},{_fmt(c2y)},{_fmt(ex)},{_fmt(ey)}"
    )


def _curve_vertical(sx: float, sy: float, ex: float, ey: float) -> str:
    return (
        f"M{_fmt(sx)},{_fmt(sy)}"
        f"L{_fmt(sx)},{_fmt(sy + 8)}"
        f"C{_fmt(sx)},{_fmt(sy + 24)},{_fmt(ex)},{_fmt(ey - 24)},{_fmt(ex)},{_fmt(ey)}"
    )


def _curve_to_result(sx: float, sy: float, ex: float, ey: float) -> str:
    return (
        f"M{_fmt(sx)},{_fmt(sy)}"
        f"L{_fmt(sx)},{_fmt(sy + 10)}"
        f"C{_fmt(sx)},{_fmt(sy + 36)},{_fmt(ex)},{_fmt(ey - 36)},{_fmt(ex)},{_fmt(ey)}"
    )


def _loopback_path(sx: float, sy: float, ex: float, ey: float) -> str:
    span = max(1.0, sy - ey)
    c1x = sx - 70.0
    c2x = ex - 70.0
    c1y = sy - span * 0.32
    c2y = ey + span * 0.32
    return (
        f"M{_fmt(sx)},{_fmt(sy)}"
        f"C{_fmt(c1x)},{_fmt(c1y)},{_fmt(c2x)},{_fmt(c2y)},{_fmt(ex)},{_fmt(ey)}"
    )


def _right_rail_loop(
    sx: float, sy: float, ex: float, ey: float, rail_x: float
) -> str:
    mid = (sy + ey) / 2.0
    if ey >= sy:
        # Downward arc
        return (
            f"M{_fmt(sx)},{_fmt(sy)}"
            f"C{_fmt(sx + 26)},{_fmt(sy + 8)},{_fmt(rail_x)},{_fmt(sy + 44)},{_fmt(rail_x)},{_fmt(mid)}"
            f"C{_fmt(rail_x)},{_fmt(ey - 44)},{_fmt(ex + 26)},{_fmt(ey - 8)},{_fmt(ex)},{_fmt(ey)}"
        )
    # Upward arc
    return (
        f"M{_fmt(sx)},{_fmt(sy)}"
        f"C{_fmt(sx + 26)},{_fmt(sy - 8)},{_fmt(rail_x)},{_fmt(sy - 44)},{_fmt(rail_x)},{_fmt(mid)}"
        f"C{_fmt(rail_x)},{_fmt(ey + 44)},{_fmt(ex + 26)},{_fmt(ey + 8)},{_fmt(ex)},{_fmt(ey)}"
    )


def _curve_general(sx: float, sy: float, ex: float, ey: float) -> str:
    return (
        f"M{_fmt(sx)},{_fmt(sy)}"
        f"C{_fmt(sx)},{_fmt(sy + 36)},{_fmt(ex)},{_fmt(ey - 36)},{_fmt(ex)},{_fmt(ey)}"
    )


def _curve_horizontal(sx: float, sy: float, ex: float, ey: float) -> str:
    mid = (sx + ex) / 2.0
    return (
        f"M{_fmt(sx)},{_fmt(sy)}"
        f"C{_fmt(mid)},{_fmt(sy)},{_fmt(mid)},{_fmt(ey)},{_fmt(ex)},{_fmt(ey)}"
    )


def _curve_left_rail(sx: float, sy: float, ex: float, ey: float, rail_x: float) -> str:
    mid = (sy + ey) / 2.0
    return (
        f"M{_fmt(sx)},{_fmt(sy)}"
        f"C{_fmt(sx - 26)},{_fmt(sy + 8)},{_fmt(rail_x)},{_fmt(sy + 44)},{_fmt(rail_x)},{_fmt(mid)}"
        f"C{_fmt(rail_x)},{_fmt(ey - 44)},{_fmt(ex - 26)},{_fmt(ey - 8)},{_fmt(ex)},{_fmt(ey)}"
    )


def _curve_bent(
    sx: float,
    sy: float,
    ex: float,
    ey: float,
    *,
    bias: float = 0.0,
    span: float | None = None,
) -> str:
    dy = ey - sy
    sign = 1.0 if dy >= 0 else -1.0
    bend = max(32.0, abs(dy) * 0.33) if span is None else max(20.0, span)
    c1x = sx + bias
    c1y = sy + sign * bend
    c2x = ex + bias
    c2y = ey - sign * bend
    return (
        f"M{_fmt(sx)},{_fmt(sy)}"
        f"C{_fmt(c1x)},{_fmt(c1y)},{_fmt(c2x)},{_fmt(c2y)},{_fmt(ex)},{_fmt(ey)}"
    )


def _curve_via_rail(sx: float, sy: float, ex: float, ey: float, rail_x: float) -> str:
    dy = ey - sy
    sign = 1.0 if dy >= 0 else -1.0
    bend = max(48.0, abs(dy) * 0.28)
    return (
        f"M{_fmt(sx)},{_fmt(sy)}"
        f"C{_fmt(rail_x)},{_fmt(sy + sign * bend)},"
        f"{_fmt(rail_x)},{_fmt(ey - sign * bend)},"
        f"{_fmt(ex)},{_fmt(ey)}"
    )


def _line(sx: float, sy: float, ex: float, ey: float) -> str:
    return f"M{_fmt(sx)},{_fmt(sy)}L{_fmt(ex)},{_fmt(ey)}"


def _polyline(points: list[tuple[float, float]]) -> str:
    if not points:
        return ""
    start = points[0]
    segs = [f"M{_fmt(start[0])},{_fmt(start[1])}"]
    for x, y in points[1:]:
        segs.append(f"L{_fmt(x)},{_fmt(y)}")
    return "".join(segs)


def _is_diamond_node(svg_text: str, node_id: str) -> bool:
    return (
        re.search(
            rf'<g class="node[^"]*" id="flowchart-{re.escape(node_id)}-\d+" transform="translate\([^)]+\)"><polygon ',
            svg_text,
        )
        is not None
    )


def _border_point(
    box: dict[str, float], toward_x: float, toward_y: float, *, diamond: bool
) -> tuple[float, float]:
    cx = box["x"]
    cy = box["y"]
    dx = toward_x - cx
    dy = toward_y - cy
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return cx, cy

    hx = max(1e-6, (box["right"] - box["left"]) / 2.0)
    hy = max(1e-6, (box["bottom"] - box["top"]) / 2.0)

    if diamond:
        # |x|/hx + |y|/hy = 1 (diamond aligned to axes)
        t = 1.0 / ((abs(dx) / hx) + (abs(dy) / hy))
    else:
        sx = float("inf") if abs(dx) < 1e-6 else hx / abs(dx)
        sy = float("inf") if abs(dy) < 1e-6 else hy / abs(dy)
        t = min(sx, sy)
    return cx + dx * t, cy + dy * t


def _line_border(
    src: dict[str, float],
    dst: dict[str, float],
    *,
    src_diamond: bool,
    dst_diamond: bool,
) -> tuple[str, float, float]:
    sx, sy = _border_point(src, dst["x"], dst["y"], diamond=src_diamond)
    ex, ey = _border_point(dst, src["x"], src["y"], diamond=dst_diamond)
    return _line(sx, sy, ex, ey), (sx + ex) / 2.0, (sy + ey) / 2.0


def _line_cardinal(src: dict[str, float], dst: dict[str, float]) -> tuple[str, float, float]:
    dx = dst["x"] - src["x"]
    dy = dst["y"] - src["y"]
    if abs(dy) > 1e-6:
        # Cross-row flow: always connect from end-side to start-side (bottom->top / top->bottom).
        if dy >= 0:
            sx, sy = src["x"], src["bottom"]
            ex, ey = dst["x"], dst["top"]
        else:
            sx, sy = src["x"], src["top"]
            ex, ey = dst["x"], dst["bottom"]
    else:
        # Same-row flow: connect side centers.
        if dx >= 0:
            sx, sy = src["right"], src["y"]
            ex, ey = dst["left"], dst["y"]
        else:
            sx, sy = src["left"], src["y"]
            ex, ey = dst["right"], dst["y"]
    return _line(sx, sy, ex, ey), (sx + ex) / 2.0, (sy + ey) / 2.0


def _line_src_border_to_point(
    src: dict[str, float],
    px: float,
    py: float,
    *,
    src_diamond: bool,
) -> tuple[str, float, float]:
    sx, sy = _border_point(src, px, py, diamond=src_diamond)
    return _line(sx, sy, px, py), (sx + px) / 2.0, (sy + py) / 2.0


def _insert_loop_edge(svg_text: str, d: str) -> tuple[str, int]:
    if re.search(r'id="L_F_P0_0"', svg_text):
        return _replace_edge_path(svg_text, "L_F_P0_0", d)
    tag = (
        f'<path d="{d}" id="L_F_P0_0" '
        'class="edge-thickness-normal edge-pattern-solid edge-thickness-normal edge-pattern-solid flowchart-link" '
        'style=";" marker-end="url(#my-svg_flowchart-v2-pointEnd)"/>'
    )
    marker = "</g><g class=\"edgeLabels\">"
    if marker not in svg_text:
        return svg_text, 0
    return svg_text.replace(marker, f"{tag}{marker}", 1), 1


def patch_svg(svg_text: str) -> tuple[str, int]:
    # Guard: only patch the intended flowchart.
    if "id=\"flowchart-P0-" not in svg_text or "id=\"flowchart-EX0-" not in svg_text:
        return svg_text, 0

    p0 = _node_transform(svg_text, "P0")
    p1 = _node_transform(svg_text, "P1")
    p2 = _node_transform(svg_text, "P2")
    if not (p0 and p1 and p2):
        return svg_text, 0
    # Keep the main prepare chain on one shared centerline.
    target_x = p1[0]

    out = svg_text
    patched = 0
    out, c = _make_edge_labels_transparent(out)
    patched += c

    # Center-based deterministic rows.
    p3_cur = _node_transform(out, "P3")
    pm_cur = _node_transform(out, "PM")
    ex0_cur = _node_transform(out, "EX0")
    exe_cur = _node_transform(out, "EXE")
    e_cur = _node_transform(out, "E")
    if not (p3_cur and pm_cur and ex0_cur and exe_cur and e_cur and p0 and p1 and p2):
        return out, patched

    prep_y = p3_cur[1]
    pm_y = pm_cur[1]
    ex0_y = ex0_cur[1]
    exe_y = exe_cur[1]
    e_y = e_cur[1]
    gap = 220.0
    s_y = e_y + gap
    u_y = s_y + gap
    ul_y = u_y + gap
    r_y = ul_y + gap
    x_y = r_y + gap
    yz_y = x_y + gap
    open_y = yz_y + gap
    cron_y = open_y + gap
    f_y = cron_y + gap

    offset = 300.0
    branch = 220.0

    targets: dict[str, tuple[float, float]] = {
        "P0": (target_x, p0[1]),
        "P1": (target_x, p1[1]),
        "P2": (target_x, p2[1]),
        "P5": (target_x - offset, prep_y),
        "P3": (target_x, prep_y),
        "P4": (target_x + offset, prep_y),
        "PM": (target_x, pm_y),
        "EX0": (target_x, ex0_y),
        "EXE": (target_x, exe_y),
        "E": (target_x, e_y),
        "S": (target_x, s_y),
        "U": (target_x, u_y),
        "UL": (target_x, ul_y),
        "R": (target_x, r_y),
        "X": (target_x, x_y),
        "Y": (target_x - branch, yz_y),
        "Z": (target_x + branch, yz_y),
        "OPEN": (target_x, open_y),
        "CRON": (target_x, cron_y),
        "F": (target_x, f_y),
    }

    for nid, (nx, ny) in targets.items():
        out, c = _replace_node_xy(out, nid, nx, ny)
        patched += c

    # Keep rectangular nodes in the same visual row at identical size.
    row_rect_groups: list[list[str]] = [
        ["P5", "P3", "P4"],
        ["UL", "R"],
        ["Y", "Z"],
        ["CRON", "F"],
    ]
    for group in row_rect_groups:
        dims: list[tuple[str, float, float]] = []
        for nid in group:
            rect = _rect_box(out, nid)
            if rect:
                dims.append((nid, rect["right"] - rect["left"], rect["bottom"] - rect["top"]))
        if len(dims) < 2:
            continue
        max_w = max(w for _, w, _ in dims)
        max_h = max(h for _, _, h in dims)
        for nid, _, _ in dims:
            out, c = _set_rect_size(out, nid, max_w, max_h)
            patched += c

    boxes: dict[str, dict[str, float]] = {}
    node_is_diamond: dict[str, bool] = {}
    for nid in (
        "P0",
        "P1",
        "P2",
        "P3",
        "P4",
        "P5",
        "PM",
        "EX0",
        "EXE",
        "E",
        "S",
        "U",
        "UL",
        "R",
        "X",
        "Y",
        "Z",
        "OPEN",
        "CRON",
        "F",
    ):
        box = _node_box(out, nid)
        if box:
            boxes[nid] = box
            node_is_diamond[nid] = _is_diamond_node(out, nid)

    def b(n: str) -> dict[str, float] | None:
        return boxes.get(n)

    edges: list[tuple[str, str, str]] = [
        ("L_P0_P1_0", "P0", "P1"),
        ("L_P1_P2_0", "P1", "P2"),
        ("L_P2_P3_0", "P2", "P3"),
        ("L_P2_P4_0", "P2", "P4"),
        ("L_P2_P5_0", "P2", "P5"),
        ("L_P3_PM_0", "P3", "PM"),
        ("L_P4_PM_0", "P4", "PM"),
        ("L_PM_EX0_0", "PM", "EX0"),
        ("L_EX0_EXE_0", "EX0", "EXE"),
        ("L_EXE_E_0", "EXE", "E"),
        ("L_P5_E_0", "P5", "E"),
        ("L_E_S_0", "E", "S"),
        ("L_S_U_0", "S", "U"),
        ("L_U_UL_0", "U", "UL"),
        ("L_UL_R_0", "UL", "R"),
        ("L_R_X_0", "R", "X"),
        ("L_X_Y_0", "X", "Y"),
        ("L_X_Y_2", "X", "Y"),
        ("L_X_Z_0", "X", "Z"),
        ("L_Y_OPEN_0", "Y", "OPEN"),
        ("L_Z_OPEN_0", "Z", "OPEN"),
        ("L_U_OPEN_0", "U", "OPEN"),
        ("L_OPEN_U_0", "OPEN", "U"),
        ("L_E_CRON_0", "E", "CRON"),
        ("L_OPEN_CRON_0", "OPEN", "CRON"),
        ("L_CRON_F_0", "CRON", "F"),
    ]
    for edge_id, src, dst in edges:
        if b(src) and b(dst) and node_is_diamond.get(src, False) and not node_is_diamond.get(dst, False):
            sx0 = b(src)["x"]
            sy0 = b(src)["y"]
            ex0 = b(dst)["x"]
            ey0 = b(dst)["y"]
            if abs(ex0 - sx0) > 1e-6:
                # Side branch from a decision diamond into a side box:
                # exit from left/right side, one bend, enter destination from top/bottom.
                going_down = ey0 >= sy0
                sx = b(src)["right"] if ex0 > sx0 else b(src)["left"]
                sy = sy0
                ex = ex0
                ey = b(dst)["top"] if going_down else b(dst)["bottom"]
                label_anchor_x = ex
                if edge_id.endswith("_2"):
                    # Secondary parallel edge uses a different source side:
                    # go down/up first from diamond bottom/top, then enter dst from side.
                    sx = sx0
                    sy = b(src)["bottom"] if going_down else b(src)["top"]
                    # Enter destination from the side facing the source.
                    enter_right = sx0 > ex0
                    ex = b(dst)["right"] if enter_right else b(dst)["left"]
                    ey = b(dst)["y"]
                    d = _polyline([(sx, sy), (sx, ey), (ex, ey)])
                else:
                    d = _polyline([(sx, sy), (ex, sy), (ex, ey)])
                out, c = _replace_edge_path(out, edge_id, d)
                patched += c
                ly = sy - 18.0 if going_down else sy + 18.0
                if edge_id == "L_X_Y_2":
                    # Keep this long label close to the destination box to avoid
                    # colliding with the Result diamond and neighboring labels.
                    ly = b(dst)["top"] - 14.0
                elif edge_id.endswith("_2"):
                    ly += 22.0
                out, c = _set_edge_label_xy(out, edge_id, (sx + label_anchor_x) / 2.0, ly)
                patched += c
                continue
        if b(src) and b(dst) and not node_is_diamond.get(src, False) and node_is_diamond.get(dst, False):
            sx0 = b(src)["x"]
            sy0 = b(src)["y"]
            ex0 = b(dst)["x"]
            ey0 = b(dst)["y"]
            if abs(ex0 - sx0) > 1e-6:
                # Side-origin edge into a decision diamond:
                # one bend, then enter left/right side according to source side.
                going_down = ey0 >= sy0
                sx = sx0
                sy = b(src)["bottom"] if going_down else b(src)["top"]
                ex = b(dst)["left"] if sx0 < ex0 else b(dst)["right"]
                ey = b(dst)["y"]
                d = _polyline([(sx, sy), (sx, ey), (ex, ey)])
                out, c = _replace_edge_path(out, edge_id, d)
                patched += c
                out, c = _set_edge_label_xy(out, edge_id, sx + (ex - sx) * 0.55, ey - 18.0)
                patched += c
                continue
        if edge_id in ("L_P3_PM_0", "L_P4_PM_0"):
            if b(src) and b("EX0"):
                ex0 = b("EX0")
                sx, sy = b(src)["x"], b(src)["bottom"]
                if edge_id == "L_P4_PM_0":
                    # Keep cached policy path should enter EX0 from the side
                    # so it does not share the same destination side as P3->EX0.
                    ex, ey = ex0["right"], ex0["y"]
                    # Go down first, then bend into the side so final segment
                    # is perpendicular to EX0's box side.
                    d = _polyline([(sx, sy), (sx, ey), (ex, ey)])
                    lx = (sx + ex) / 2.0
                else:
                    # Cache selected policy path enters EX0 from the top.
                    ex, ey = ex0["x"], ex0["top"]
                    d = _line(sx, sy, ex, ey)
                    lx = (sx + ex) / 2.0
                ly = (sy + ey) / 2.0
                out, c = _replace_edge_path(out, edge_id, d)
                patched += c
                out, c = _set_edge_label_xy(out, edge_id, lx, ly)
                patched += c
            continue
        if edge_id == "L_P5_E_0":
            if b("P5") and b("E"):
                e = b("E")
                sx, sy = b("P5")["x"], b("P5")["bottom"]
                ex, ey = e["left"], e["y"]
                d = _line(sx, sy, ex, ey)
                lx = (sx + ex) / 2.0
                ly = (sy + ey) / 2.0
                out, c = _replace_edge_path(out, edge_id, d)
                patched += c
                out, c = _set_edge_label_xy(out, edge_id, lx, ly)
                patched += c
            continue
        if b(src) and b(dst):
            d, lx, ly = _line_cardinal(b(src), b(dst))
            out, c = _replace_edge_path(out, edge_id, d)
            patched += c
            out, c = _set_edge_label_xy(out, edge_id, lx, ly)
            patched += c

    if b("F") and b("P0"):
        f = b("F")
        p0 = b("P0")
        # Keep loopback lane outside all boxes to avoid overlap.
        rightmost = max(v["right"] for v in boxes.values())
        rail_x = rightmost + 56.0
        vm = re.search(r'viewBox="[\-0-9.]+\s+[\-0-9.]+\s+([\-0-9.]+)\s+[\-0-9.]+"', out)
        if vm:
            view_w = float(vm.group(1))
            rail_x = min(rail_x, view_w - 24.0)
        rail_x = max(rail_x, max(f["right"], p0["right"]) + 24.0)
        loop_d = _polyline(
            [
                (f["right"], f["y"]),
                (rail_x, f["y"]),
                (rail_x, p0["y"]),
                (p0["right"], p0["y"]),
            ]
        )
        out, c = _insert_loop_edge(out, loop_d)
        patched += c

    return out, patched


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
        print(f"{path}: patched_flowchart_centerline={count}")

    print(f"total_patched_flowchart_centerline={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
