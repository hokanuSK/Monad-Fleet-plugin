#!/usr/bin/env python3
"""Sequence SVG post-processing for label alignment.

This post-process patches rendered sequence SVG files to:
- center labels for non-self message arrows
- keep self-loop arrow labels unchanged
- keep loop/alt fragment frames from drifting too far left
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


MESSAGE_BLOCK_RE = re.compile(
    r'(?P<msg><text [^>]*class="messageText"[^>]*>.*?</text>)'
    r'(?P<ws1>\s*)'
    r'(?P<line><(?:line|path) [^>]*class="messageLine[01]"[^>]*>)'
    r'(?P<ws2>\s*<line [^>]*marker-start="url\(#sequencenumber\)"[^>]*/>\s*)'
    r'(?P<num><text [^>]*class="sequenceNumber"[^>]*>\d+</text>)',
    re.S,
)

X_RE = re.compile(r'x="([\-0-9.]+)"')
TEXT_ANCHOR_RE = re.compile(r'text-anchor="[^"]+"')
ACTOR_X_RE = re.compile(
    r'<line id="actor\d+" x1="([\-0-9.]+)" y1="[0-9.]+" x2="\1" y2="[\-0-9.]+" class="actor-line'
)
VIEWBOX_RE = re.compile(
    r'viewBox="([\-0-9.]+) ([\-0-9.]+) ([\-0-9.]+) ([\-0-9.]+)"'
)
ACTOR_TOP_RECT_RE = re.compile(
    r'<rect x="[\-0-9.]+" y="([\-0-9.]+)" fill="#eaeaea" stroke="#666" '
    r'width="150" height="65" name="[^"]+" rx="3" ry="3" class="actor actor-top"/>'
)
ACTOR_MAN_TOP_TEXT_RE = re.compile(
    r'<g class="actor-man actor-top"[^>]*>.*?'
    r'<text [^>]*\by="([\-0-9.]+)"[^>]*class="actor actor-man"[^>]*>',
    re.S,
)
ACTOR_MAN_TOP_CIRCLE_RE = re.compile(
    r'<g class="actor-man actor-top"[^>]*>.*?'
    r'<circle [^>]*\bcy="([\-0-9.]+)"[^>]*\br="([\-0-9.]+)"[^>]*>',
    re.S,
)
ACTOR_MAN_GROUP_RE = re.compile(
    r'<g class="actor-man actor-(?:top|bottom)"[^>]*>.*?</g>',
    re.S,
)
ACTOR_MAN_TEXT_TAG_RE = re.compile(r'<text [^>]*class="actor actor-man"[^>]*>')
ACTOR_MAN_CIRCLE_TAG_RE = re.compile(r"<circle [^>]*>")
ACTOR_TOP_BOX_RE = re.compile(
    r'<rect x="([\-0-9.]+)" y="([\-0-9.]+)" fill="#eaeaea" stroke="#666" '
    r'width="([\-0-9.]+)" height="([\-0-9.]+)" name="[^"]+" rx="3" ry="3" class="actor actor-top"/>'
)
ACTOR_BOX_TEXT_RE = re.compile(
    r'<text [^>]*\by="([\-0-9.]+)"[^>]*class="actor actor-box"[^>]*>'
)
NOTE_GROUP_RE = re.compile(
    r'<g>(?P<rect><rect [^>]*class="note"[^>]*/>)(?P<body>.*?)(?P<end></g>)',
    re.S,
)
NOTE_TEXT_TAG_RE = re.compile(r"<text [^>]*class=\"noteText\"[^>]*>")
TOP_BUNDLE_GROUP_RE = re.compile(
    r'<g>(?P<rect><rect x="[\-0-9.]+" y="[\-0-9.]+" fill="transparent" '
    r'stroke="rgb\(0,0,0, 0.5\)" width="[\-0-9.]+" height="[\-0-9.]+" class="rect"/>)'
    r'(?P<text><text [^>]*class="text"[^>]*>.*?</text>)</g>',
    re.S,
)
LOOP_LINE_TAG_RE = re.compile(r'<line [^>]*class="loopLine"[^>]*/>')
FRAGMENT_GROUP_RE = re.compile(
    r"<g>(?P<body>(?:(?!</g>).)*class=\"labelBox\"(?:(?!</g>).)*)</g>", re.S
)
LABEL_BOX_RE = re.compile(r'<polygon points="([^"]+)" class="labelBox"/>')
LABEL_TEXT_RE = re.compile(r'(<text [^>]*class="labelText"[^>]*>)(.*?)(</text>)', re.S)
LOOP_TEXT_RE = re.compile(r'(<text [^>]*class="loopText"[^>]*>)(.*?)(</text>)', re.S)
TITLE_TEXT_RE = re.compile(r'(<text [^>]*\by="-25"[^>]*>)([^<]*)(</text>)')
BUNDLE_TEXT_RE = re.compile(r'(<text [^>]*class="text"[^>]*>)(.*?)(</text>)', re.S)
TSPAN_X_RE = re.compile(r'(<tspan [^>]*\bx=")([\-0-9.]+)(")')
MEASURE_SPLIT_RE = re.compile(
    r'(?P<open><text [^>]*class="loopText"[^>]*>)\s*'
    r'(?:(?:<tspan [^>]*>)?\[measure\.type =\s*(?:</tspan>)?)\s*</text>\s*'
    r'<text [^>]*class="loopText"[^>]*>\s*'
    r'(?:(?:<tspan [^>]*>)?(?P<kind>WIFI|BLE|CSI)\](?:</tspan>)?)\s*</text>',
    re.S,
)


def _float_attr(tag: str, name: str) -> float | None:
    m = re.search(rf'{name}="([\-0-9.]+)"', tag)
    if not m:
        return None
    return float(m.group(1))


def _set_text_anchor_end(msg_tag: str) -> str:
    if TEXT_ANCHOR_RE.search(msg_tag):
        return TEXT_ANCHOR_RE.sub('text-anchor="end"', msg_tag, count=1)
    return msg_tag.replace("<text ", '<text text-anchor="end" ', 1)


def _set_x(msg_tag: str, value: float) -> str:
    x_text = f'{value:.3f}'.rstrip("0").rstrip(".")
    if X_RE.search(msg_tag):
        return X_RE.sub(f'x="{x_text}"', msg_tag, count=1)
    return msg_tag.replace("<text ", f'<text x="{x_text}" ', 1)


def _fmt(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _set_attr(tag: str, name: str, value: float) -> str:
    value_text = f'{value:.3f}'.rstrip("0").rstrip(".")
    return re.sub(rf'{name}="([\-0-9.]+)"', f'{name}="{value_text}"', tag, count=1)


def _set_or_add_numeric_attr(tag: str, name: str, value: float) -> str:
    value_text = _fmt(value)
    if re.search(rf'{name}="([\-0-9.]+)"', tag):
        return re.sub(rf'{name}="([\-0-9.]+)"', f'{name}="{value_text}"', tag, count=1)
    return tag.replace(">", f' {name}="{value_text}">', 1)


def _set_text_anchor_start(tag: str) -> str:
    if TEXT_ANCHOR_RE.search(tag):
        return TEXT_ANCHOR_RE.sub('text-anchor="start"', tag, count=1)
    return tag.replace("<text ", '<text text-anchor="start" ', 1)


def _set_text_anchor_middle(tag: str) -> str:
    if TEXT_ANCHOR_RE.search(tag):
        return TEXT_ANCHOR_RE.sub('text-anchor="middle"', tag, count=1)
    return tag.replace("<text ", '<text text-anchor="middle" ', 1)


def _text_anchor(tag: str) -> str | None:
    m = TEXT_ANCHOR_RE.search(tag)
    if not m:
        return None
    return m.group(0).split('"')[1]


def _patch_fragment_frames(svg_text: str, fragment_side_margin: float) -> tuple[str, int]:
    actor_x = [float(x) for x in ACTOR_X_RE.findall(svg_text)]
    if not actor_x:
        return svg_text, 0

    left_edge = min(actor_x) - fragment_side_margin
    label_box_inset = 8.0
    label_box_min_x = left_edge + label_box_inset
    label_center_min = label_box_min_x + 25.0
    min_left_fragment_width = 560.0
    patched = 0

    def _replace_loop_line(m: re.Match[str]) -> str:
        nonlocal patched
        tag = m.group(0)
        x1 = _float_attr(tag, "x1")
        x2 = _float_attr(tag, "x2")
        if x1 is None or x2 is None:
            return tag
        nx1 = max(x1, left_edge)
        nx2 = max(x2, left_edge)
        # Expand very narrow left-anchored fragment frames so condition/body
        # text stays inside (common for self-loop nested blocks).
        if abs(nx1 - left_edge) < 0.2 and nx2 > nx1 and (nx2 - nx1) < min_left_fragment_width:
            nx2 = nx1 + min_left_fragment_width
        if nx1 == x1 and nx2 == x2:
            return tag
        patched += 1
        tag = _set_attr(tag, "x1", nx1)
        tag = _set_attr(tag, "x2", nx2)
        return tag

    def _replace_label_box(m: re.Match[str]) -> str:
        nonlocal patched
        points_text = m.group(1)
        parts = points_text.split()
        if not parts:
            return m.group(0)

        coords: list[tuple[float, float]] = []
        for part in parts:
            if "," not in part:
                return m.group(0)
            xs, ys = part.split(",", 1)
            try:
                coords.append((float(xs), float(ys)))
            except ValueError:
                return m.group(0)

        min_x = min(x for x, _ in coords)
        if min_x >= label_box_min_x:
            return m.group(0)

        dx = label_box_min_x - min_x
        new_points = " ".join(
            f'{(x + dx):.3f}'.rstrip("0").rstrip(".")
            + ","
            + f'{y:.3f}'.rstrip("0").rstrip(".")
            for x, y in coords
        )
        patched += 1
        return f'<polygon points="{new_points}" class="labelBox"/>'

    def _replace_label_text(m: re.Match[str]) -> str:
        nonlocal patched
        start_tag, body, end_tag = m.groups()
        x = _float_attr(start_tag, "x")
        if x is None or x >= label_center_min:
            return m.group(0)
        patched += 1
        start_tag = _set_attr(start_tag, "x", label_center_min)
        return f"{start_tag}{body}{end_tag}"

    def _replace_loop_text(m: re.Match[str]) -> str:
        nonlocal patched
        start_tag, body, end_tag = m.groups()
        x = _float_attr(start_tag, "x")
        if x is None:
            return m.group(0)
        # Keep non-nested fragment condition labels center-aligned.
        # This includes top-level alt command conditions like
        # "[command = hello]" while leaving nested-left fragments to
        # the dedicated placement logic below.
        if x > left_edge + 42.0:
            new_tag = _set_text_anchor_middle(start_tag)
            new_body = TSPAN_X_RE.sub(lambda mm: f'{mm.group(1)}{_fmt(x)}{mm.group(3)}', body)
            if new_tag != start_tag or new_body != body or _text_anchor(start_tag) != "middle":
                patched += 1
                return f"{new_tag}{new_body}{end_tag}"
            return m.group(0)
        # Very-left branch-condition labels (e.g., [type=BLE]) should read
        # from left-to-right inside narrow fragment blocks.
        plain_body = re.sub(r"<[^>]+>", "", body)
        if "async " in plain_body:
            center_x = left_edge + (min_left_fragment_width / 2.0)
            patched += 1
            y = _float_attr(start_tag, "y")
            start_tag = _set_attr(start_tag, "x", center_x)
            if y is not None:
                start_tag = _set_attr(start_tag, "y", y - 6.0)
            start_tag = _set_text_anchor_middle(start_tag)
            body = TSPAN_X_RE.sub(lambda mm: f'{mm.group(1)}{_fmt(center_x)}{mm.group(3)}', body)
            return f"{start_tag}{body}{end_tag}"

        target_x = label_box_min_x + 10.0
        patched += 1
        y = _float_attr(start_tag, "y")
        start_tag = _set_attr(start_tag, "x", target_x)
        if y is not None:
            start_tag = _set_attr(start_tag, "y", y + 10.0)
        start_tag = _set_text_anchor_start(start_tag)
        body = TSPAN_X_RE.sub(lambda mm: f'{mm.group(1)}{_fmt(target_x)}{mm.group(3)}', body)
        return f"{start_tag}{body}{end_tag}"

    out = LOOP_LINE_TAG_RE.sub(_replace_loop_line, svg_text)
    out = LABEL_BOX_RE.sub(_replace_label_box, out)
    out = LABEL_TEXT_RE.sub(_replace_label_text, out)
    out = LOOP_TEXT_RE.sub(_replace_loop_text, out)

    def _merge_measure(m: re.Match[str]) -> str:
        nonlocal patched
        open_tag = m.group("open")
        y = _float_attr(open_tag, "y")
        if y is not None:
            # Keep measure-branch condition on the same row as the fragment tab label.
            open_tag = _set_attr(open_tag, "y", y - 16.0)
        # Center measure.type labels in the branch box while keeping row alignment.
        open_tag = _set_attr(open_tag, "x", left_edge + (min_left_fragment_width / 2.0))
        open_tag = _set_text_anchor_middle(open_tag)
        patched += 1
        return f"{open_tag}[measure.type = {m.group('kind')}]</text>"

    out = MEASURE_SPLIT_RE.sub(_merge_measure, out)
    return out, patched


def _fragment_bounds(fragment_body: str) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for line_tag in LOOP_LINE_TAG_RE.findall(fragment_body):
        x1 = _float_attr(line_tag, "x1")
        x2 = _float_attr(line_tag, "x2")
        y1 = _float_attr(line_tag, "y1")
        y2 = _float_attr(line_tag, "y2")
        if x1 is None or x2 is None or y1 is None or y2 is None:
            continue
        xs.extend([x1, x2])
        ys.extend([y1, y2])
    if not xs or not ys:
        return None
    return min(xs), max(xs), min(ys), max(ys)


def _center_shallow_fragment_condition_labels(
    svg_text: str, max_nesting_depth: int = 999
) -> tuple[str, int]:
    groups: list[dict[str, float | int | str]] = []
    for m in FRAGMENT_GROUP_RE.finditer(svg_text):
        body = m.group("body")
        bounds = _fragment_bounds(body)
        if bounds is None:
            continue
        left, right, top, bottom = bounds
        groups.append(
            {
                "start": m.start("body"),
                "end": m.end("body"),
                "body": body,
                "left": left,
                "right": right,
                "top": top,
                "bottom": bottom,
                "depth": 0,
            }
        )

    if not groups:
        return svg_text, 0

    eps = 0.1
    for i, g in enumerate(groups):
        depth = 0
        for j, h in enumerate(groups):
            if i == j:
                continue
            if (
                float(h["left"]) <= float(g["left"]) + eps
                and float(h["right"]) >= float(g["right"]) - eps
                and float(h["top"]) <= float(g["top"]) + eps
                and float(h["bottom"]) >= float(g["bottom"]) - eps
                and (
                    float(h["right"]) - float(h["left"])
                    > (float(g["right"]) - float(g["left"])) + eps
                    or float(h["bottom"]) - float(h["top"])
                    > (float(g["bottom"]) - float(g["top"])) + eps
                )
            ):
                depth += 1
        g["depth"] = depth

    patched = 0
    out = svg_text
    for g in reversed(groups):
        if int(g["depth"]) > max_nesting_depth:
            continue

        center_x = (float(g["left"]) + float(g["right"])) / 2.0
        body = str(g["body"])

        def _replace_loop_text(m: re.Match[str]) -> str:
            nonlocal patched
            start_tag, body_text, end_tag = m.groups()
            new_tag = _set_or_add_numeric_attr(start_tag, "x", center_x)
            new_tag = _set_text_anchor_middle(new_tag)
            new_body = TSPAN_X_RE.sub(
                lambda mm: f'{mm.group(1)}{_fmt(center_x)}{mm.group(3)}', body_text
            )
            if new_tag != start_tag or new_body != body_text:
                patched += 1
            return f"{new_tag}{new_body}{end_tag}"

        new_body = LOOP_TEXT_RE.sub(_replace_loop_text, body)
        if new_body == body:
            continue

        start = int(g["start"])
        end = int(g["end"])
        out = out[:start] + new_body + out[end:]

    return out, patched


def _center_fragment_header_tags(svg_text: str) -> tuple[str, int]:
    """Center sequence fragment header tabs (alt/loop) within each fragment frame."""
    patched = 0
    out = svg_text

    groups: list[dict[str, float | int | str]] = []
    for m in FRAGMENT_GROUP_RE.finditer(svg_text):
        body = m.group("body")
        bounds = _fragment_bounds(body)
        if bounds is None:
            continue
        left, right, _, _ = bounds
        groups.append(
            {
                "start": m.start("body"),
                "end": m.end("body"),
                "body": body,
                "center_x": (left + right) / 2.0,
            }
        )

    if not groups:
        return out, 0

    for g in reversed(groups):
        body = str(g["body"])
        center_x = float(g["center_x"])

        def _replace_label_text(m: re.Match[str]) -> str:
            nonlocal patched
            start_tag, body_text, end_tag = m.groups()
            new_tag = _set_or_add_numeric_attr(start_tag, "x", center_x)
            new_tag = _set_text_anchor_middle(new_tag)
            new_body = TSPAN_X_RE.sub(
                lambda mm: f'{mm.group(1)}{_fmt(center_x)}{mm.group(3)}', body_text
            )
            if new_tag != start_tag or new_body != body_text:
                patched += 1
            return f"{new_tag}{new_body}{end_tag}"

        def _replace_label_box(m: re.Match[str]) -> str:
            nonlocal patched
            points_text = m.group(1)
            parts = points_text.split()
            if not parts:
                return m.group(0)

            coords: list[tuple[float, float]] = []
            for part in parts:
                if "," not in part:
                    return m.group(0)
                xs, ys = part.split(",", 1)
                try:
                    coords.append((float(xs), float(ys)))
                except ValueError:
                    return m.group(0)

            min_x = min(x for x, _ in coords)
            max_x = max(x for x, _ in coords)
            current_center = (min_x + max_x) / 2.0
            dx = center_x - current_center
            if abs(dx) < 0.1:
                return m.group(0)

            new_points = " ".join(
                f'{_fmt(x + dx)},{_fmt(y)}' for x, y in coords
            )
            patched += 1
            return f'<polygon points="{new_points}" class="labelBox"/>'

        new_body = LABEL_BOX_RE.sub(_replace_label_box, body)
        new_body = LABEL_TEXT_RE.sub(_replace_label_text, new_body)
        if new_body == body:
            continue

        start = int(g["start"])
        end = int(g["end"])
        out = out[:start] + new_body + out[end:]

    return out, patched


def _center_titles(svg_text: str) -> tuple[str, int]:
    viewbox = VIEWBOX_RE.search(svg_text)
    if not viewbox:
        return svg_text, 0

    min_x = float(viewbox.group(1))
    width = float(viewbox.group(3))
    center_x = min_x + (width / 2.0)
    patched = 0

    def _replace(m: re.Match[str]) -> str:
        nonlocal patched
        open_tag, body, close_tag = m.groups()
        new_tag = _set_or_add_numeric_attr(open_tag, "x", center_x)
        new_tag = _set_text_anchor_middle(new_tag)
        if new_tag != open_tag:
            patched += 1
        return f"{new_tag}{body}{close_tag}"

    out = TITLE_TEXT_RE.sub(_replace, svg_text)
    return out, patched


def _normalize_bundle_title_vertical_gap(
    svg_text: str, bundle_title_gap: float
) -> tuple[str, int]:
    actor_top_y = [float(y) for y in ACTOR_TOP_RECT_RE.findall(svg_text)]
    if not actor_top_y:
        return svg_text, 0

    target_y = min(actor_top_y) - bundle_title_gap
    patched = 0

    def _replace(m: re.Match[str]) -> str:
        nonlocal patched
        start_tag, body, end_tag = m.groups()
        current_y = _float_attr(start_tag, "y")
        new_tag = _set_or_add_numeric_attr(start_tag, "y", target_y)
        if current_y is None or abs(current_y - target_y) > 0.05:
            patched += 1
        return f"{new_tag}{body}{end_tag}"

    out = BUNDLE_TEXT_RE.sub(_replace, svg_text)
    return out, patched


def _normalize_top_bundle_widths(
    svg_text: str, bundle_side_margin: float, bundle_gap_min: float
) -> tuple[str, int]:
    actors: list[tuple[float, float]] = []
    for m in ACTOR_TOP_BOX_RE.finditer(svg_text):
        x = float(m.group(1))
        y = float(m.group(2))
        w = float(m.group(3))
        # Stick to participant header row only.
        if y > 80:
            continue
        actors.append((x, x + w))
    if not actors:
        return svg_text, 0

    bundles: list[dict[str, float | int | str]] = []
    for m in TOP_BUNDLE_GROUP_RE.finditer(svg_text):
        rect_tag = m.group("rect")
        text_tag = m.group("text")
        left = _float_attr(rect_tag, "x")
        width = _float_attr(rect_tag, "width")
        y = _float_attr(rect_tag, "y")
        if left is None or width is None or y is None:
            continue
        # Only top participant bundles (not inner rects).
        if y > 10.0:
            continue
        right = left + width

        contained = []
        for ax1, ax2 in actors:
            center_x = (ax1 + ax2) / 2.0
            if center_x >= left - 0.1 and center_x <= right + 0.1:
                contained.append((ax1, ax2))
        if not contained:
            continue

        span_left = min(a[0] for a in contained)
        span_right = max(a[1] for a in contained)
        bundles.append(
            {
                "start": m.start(),
                "end": m.end(),
                "rect": rect_tag,
                "text": text_tag,
                "span_left": span_left,
                "span_right": span_right,
                "left_margin": bundle_side_margin,
                "right_margin": bundle_side_margin,
            }
        )

    if not bundles:
        return svg_text, 0

    bundles.sort(key=lambda b: float(b["span_left"]))
    for i in range(len(bundles) - 1):
        cur = bundles[i]
        nxt = bundles[i + 1]
        actor_gap = float(nxt["span_left"]) - float(cur["span_right"])
        shared_margin = min(bundle_side_margin, max((actor_gap - bundle_gap_min) / 2.0, 0.0))
        cur["right_margin"] = min(float(cur["right_margin"]), shared_margin)
        nxt["left_margin"] = min(float(nxt["left_margin"]), shared_margin)

    patched = 0
    out = svg_text
    for b in sorted(bundles, key=lambda item: int(item["start"]), reverse=True):
        rect_tag = str(b["rect"])
        text_tag = str(b["text"])
        new_left = float(b["span_left"]) - float(b["left_margin"])
        new_right = float(b["span_right"]) + float(b["right_margin"])
        new_width = new_right - new_left
        center_x = (new_left + new_right) / 2.0

        new_rect = _set_attr(rect_tag, "x", new_left)
        new_rect = _set_attr(new_rect, "width", new_width)

        new_text = _set_or_add_numeric_attr(text_tag, "x", center_x)
        new_text = _set_text_anchor_middle(new_text)
        new_text = TSPAN_X_RE.sub(
            lambda mm: f'{mm.group(1)}{_fmt(center_x)}{mm.group(3)}', new_text
        )

        new_group = f"<g>{new_rect}{new_text}</g>"
        old_group = out[int(b["start"]) : int(b["end"])]
        if new_group != old_group:
            patched += 1
            out = out[: int(b["start"])] + new_group + out[int(b["end"]) :]

    return out, patched


def _ensure_actor_man_top_note_clearance(
    svg_text: str, actor_man_note_gap: float
) -> tuple[str, int]:
    actor_man_text_y = [float(y) for y in ACTOR_MAN_TOP_TEXT_RE.findall(svg_text)]
    if not actor_man_text_y:
        return svg_text, 0

    # Approximate bottom of actor-man label text with a half-line height.
    actor_label_bottom = max(actor_man_text_y) + 8.0
    circle_bottoms = [
        float(cy) + float(r) for cy, r in ACTOR_MAN_TOP_CIRCLE_RE.findall(svg_text)
    ]
    actor_visual_bottom = max([actor_label_bottom, *circle_bottoms], default=actor_label_bottom)

    first_note_match = None
    first_note_y = None
    for m in NOTE_GROUP_RE.finditer(svg_text):
        rect_tag = m.group("rect")
        y = _float_attr(rect_tag, "y")
        if y is None:
            continue
        if first_note_y is None or y < first_note_y:
            first_note_y = y
            first_note_match = m

    if first_note_match is None or first_note_y is None:
        return svg_text, 0

    min_note_y = actor_visual_bottom + actor_man_note_gap
    if first_note_y >= min_note_y - 0.05:
        return svg_text, 0

    delta = min_note_y - first_note_y
    rect_tag = first_note_match.group("rect")
    body = first_note_match.group("body")

    new_rect = _set_attr(rect_tag, "y", first_note_y + delta)

    def _shift_note_text_y(m: re.Match[str]) -> str:
        tag = m.group(0)
        y = _float_attr(tag, "y")
        if y is None:
            return tag
        return _set_attr(tag, "y", y + delta)

    new_body = NOTE_TEXT_TAG_RE.sub(_shift_note_text_y, body)
    new_group = f"<g>{new_rect}{new_body}</g>"
    out = (
        svg_text[: first_note_match.start()]
        + new_group
        + svg_text[first_note_match.end() :]
    )
    return out, 1


def _align_top_actor_man_labels_to_actor_box_row(svg_text: str) -> tuple[str, int]:
    top_actor_box_text_y: float | None = None
    for y_text in ACTOR_BOX_TEXT_RE.findall(svg_text):
        y = float(y_text)
        # Use top participant box-label row (ignore bottom mirrored actor row).
        if y > 200.0:
            continue
        if top_actor_box_text_y is None or y < top_actor_box_text_y:
            top_actor_box_text_y = y

    # Prefer aligning top stick-figure labels to top bundle-title row when present
    # (e.g., "Researcher" aligned with "Device Agent").
    top_bundle_text_y: float | None = None
    for m in BUNDLE_TEXT_RE.finditer(svg_text):
        start_tag = m.group(1)
        y = _float_attr(start_tag, "y")
        if y is None:
            continue
        if y > 80.0:
            continue
        if top_bundle_text_y is None or y < top_bundle_text_y:
            top_bundle_text_y = y

    patched = 0

    def _replace_group(m: re.Match[str]) -> str:
        nonlocal patched
        group = m.group(0)
        group_head = group.split(">", 1)[0]
        is_top_actor = 'actor-man actor-top' in group_head

        circle_match = ACTOR_MAN_CIRCLE_TAG_RE.search(group)
        text_match = ACTOR_MAN_TEXT_TAG_RE.search(group)
        if not circle_match or not text_match:
            return group

        circle_tag = circle_match.group(0)
        text_tag = text_match.group(0)
        cy = _float_attr(circle_tag, "cy")
        r = _float_attr(circle_tag, "r")
        if cy is None or r is None:
            return group

        if not is_top_actor:
            return group

        # Align top actor-man label to top bundle-title row when available.
        target_y = top_bundle_text_y
        if target_y is None:
            target_y = top_actor_box_text_y
        if target_y is None:
            return group

        new_text_tag = _set_attr(text_tag, "y", target_y)
        if new_text_tag == text_tag:
            return group

        patched += 1
        return (
            group[: text_match.start()]
            + new_text_tag
            + group[text_match.end() :]
        )

    out = ACTOR_MAN_GROUP_RE.sub(_replace_group, svg_text)
    return out, patched


def patch_svg_text(svg_text: str, gap_px: float) -> tuple[str, int]:
    out: list[str] = []
    last = 0
    patched = 0

    for m in MESSAGE_BLOCK_RE.finditer(svg_text):
        out.append(svg_text[last : m.start()])
        last = m.end()

        msg_tag = m.group("msg")
        line_tag = m.group("line")
        num_tag = m.group("num")

        x1 = _float_attr(line_tag, "x1")
        x2 = _float_attr(line_tag, "x2")
        # Keep self-loop arrows as-is (typically rendered as <path> without x2,
        # or with x1 ~= x2). Center only cross-actor arrows.
        if x1 is None or x2 is None or abs(x1 - x2) < 0.2:
            out.append(m.group(0))
            continue

        target_x = (x1 + x2) / 2.0
        msg_tag = _set_text_anchor_middle(msg_tag)
        msg_tag = _set_x(msg_tag, target_x)

        out.append(msg_tag)
        out.append(m.group("ws1"))
        out.append(line_tag)
        out.append(m.group("ws2"))
        out.append(num_tag)
        patched += 1

    out.append(svg_text[last:])
    return "".join(out), patched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("svg_files", nargs="+", help="Rendered Mermaid sequence SVG files")
    parser.add_argument(
        "--gap-px",
        type=float,
        default=11.0,
        help="Horizontal gap between reverse-label end and sequence number center",
    )
    parser.add_argument(
        "--fragment-side-margin",
        type=float,
        default=9.0,
        help="Left inset from first actor lane used for loop/alt frame alignment",
    )
    parser.add_argument(
        "--bundle-title-gap",
        type=float,
        default=16.5,
        help="Vertical gap between bundle title baseline and top actor box row.",
    )
    parser.add_argument(
        "--bundle-side-margin",
        type=float,
        default=22.0,
        help="Horizontal inset from first/last participant header to bundle border.",
    )
    parser.add_argument(
        "--bundle-gap-min",
        type=float,
        default=24.0,
        help="Minimum gap between adjacent top-level participant bundles.",
    )
    parser.add_argument(
        "--actor-man-note-gap",
        type=float,
        default=20.0,
        help="Minimum vertical gap between actor-man label baseline and first top note.",
    )
    args = parser.parse_args()

    total = 0
    total_fragments = 0
    total_fragment_centers = 0
    total_fragment_headers = 0
    total_titles = 0
    total_bundle_gaps = 0
    total_bundle_widths = 0
    total_actor_man_note_gap = 0
    total_actor_man_labels = 0
    for path_text in args.svg_files:
        path = Path(path_text)
        original = path.read_text(encoding="utf-8", errors="ignore")
        patched_text, count = patch_svg_text(original, args.gap_px)
        patched_text, fragment_count = _patch_fragment_frames(
            patched_text, args.fragment_side_margin
        )
        patched_text, centered_count = _center_shallow_fragment_condition_labels(
            patched_text
        )
        # Keep Mermaid's native corner-tab placement ("alt"/"loop") and only
        # center condition names inside fragment bodies.
        centered_headers_count = 0
        patched_text, title_count = _center_titles(patched_text)
        patched_text, bundle_gap_count = _normalize_bundle_title_vertical_gap(
            patched_text, args.bundle_title_gap
        )
        patched_text, bundle_width_count = _normalize_top_bundle_widths(
            patched_text, args.bundle_side_margin, args.bundle_gap_min
        )
        patched_text, actor_man_label_count = _align_top_actor_man_labels_to_actor_box_row(
            patched_text
        )
        patched_text, actor_man_note_gap_count = _ensure_actor_man_top_note_clearance(
            patched_text, args.actor_man_note_gap
        )
        if patched_text != original:
            path.write_text(patched_text, encoding="utf-8")
        total += count
        total_fragments += fragment_count
        total_fragment_centers += centered_count
        total_fragment_headers += centered_headers_count
        total_titles += title_count
        total_bundle_gaps += bundle_gap_count
        total_bundle_widths += bundle_width_count
        total_actor_man_labels += actor_man_label_count
        total_actor_man_note_gap += actor_man_note_gap_count
        print(
            f"{path}: patched_reverse_labels={count}, "
            f"patched_fragment_frames={fragment_count}, "
            f"patched_fragment_centers={centered_count}, "
            f"patched_fragment_headers={centered_headers_count}, "
            f"patched_titles={title_count}, "
            f"patched_bundle_title_gap={bundle_gap_count}, "
            f"patched_bundle_widths={bundle_width_count}, "
            f"patched_actor_man_labels={actor_man_label_count}, "
            f"patched_actor_man_note_gap={actor_man_note_gap_count}"
        )

    print(f"total_patched_reverse_labels={total}")
    print(f"total_patched_fragment_frames={total_fragments}")
    print(f"total_patched_fragment_centers={total_fragment_centers}")
    print(f"total_patched_fragment_headers={total_fragment_headers}")
    print(f"total_patched_titles={total_titles}")
    print(f"total_patched_bundle_title_gap={total_bundle_gaps}")
    print(f"total_patched_bundle_widths={total_bundle_widths}")
    print(f"total_patched_actor_man_labels={total_actor_man_labels}")
    print(f"total_patched_actor_man_note_gap={total_actor_man_note_gap}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
