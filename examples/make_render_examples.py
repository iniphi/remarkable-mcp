#!/usr/bin/env python3
"""
make_render_examples.py -- generate NEUTRAL synthetic render examples for rm-mcp.

Why this exists: the private render test-suite goldens
(rm-mcp/tests/fixtures/render/*/_goldens/*.golden.png) are pixel-locked to
pages of the author's own handwriting and are correctly withheld from the public
repo. That leaves nothing publishable
that actually shows the render pipeline working. This script closes that gap
with two fully synthetic native-notebook pages -- every coordinate below is
computed by this script, not copied, traced, or derived from any real
notebook, PDF, or handwriting sample:

  1. colouring_book_shapes -- outline circle / square / star strokes plus a
     generated zigzag "colouring-in" fill inside the circle and a highlighter
     box behind the square. (This is the ink-only version: a page of outline shapes with
     one shape coloured in, built from a page of pen strokes rather than a
     printed PDF background, which keeps this generator dependency-light and
     licence-clean (no PDF-authoring library needed).)
  2. ruled_writing_pad -- ruled guide lines (like lined paper) each carrying
     a short generated triangle-wave squiggle standing in for "something was
     written here", plus a couple of checkmark ticks. Not a copy of anyone's
     handwriting -- a deterministic waveform.

Both pages are built directly with rmscene (the same library rm-mcp's own
render path parses with) and rendered through the repo's own render CLI,
tools/rm_render_page.py, at the same --supersample 2 "golden" profile the
private test suite mints its goldens under
(rm-mcp/tests/test_render_goldens.py GOLDEN_FLAGS) -- so what ships as an
example is a faithful sample of the real pipeline output, not a mockup.

Usage:
    C:\\Python313\\python.exe rm-mcp/examples/make_render_examples.py

Writes:
    rm-mcp/examples/sources/<name>/            the extracted-rmdoc-dir source
                                                (<uuid>.content, <uuid>.metadata,
                                                 <uuid>/<page>.rm)
    rm-mcp/examples/<name>.png                 the rendered example PNG
"""

from __future__ import annotations

import io
import json
import math
import sys
import time
import uuid
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent
RM_MCP_DIR = EXAMPLES_DIR.parent
REPO = RM_MCP_DIR.parent
TOOLS_DIR = REPO / "tools"
for p in (str(TOOLS_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)

from rmscene import (  # noqa: E402
    AuthorIdsBlock,
    CrdtId,
    CrdtSequence,
    CrdtSequenceItem,
    LwwValue,
    MigrationInfoBlock,
    PageInfoBlock,
    SceneGroupItemBlock,
    SceneInfo,
    SceneLineItemBlock,
    SceneTreeBlock,
    TreeNodeBlock,
    write_blocks,
)
import rmscene.scene_items as si

RM_WIDTH = 1404
RM_HEIGHT = 1872

AUTHOR = 1


# --------------------------------------------------------------------------
# Synthetic geometry -- every point below is computed, not sampled from ink.
# --------------------------------------------------------------------------

def circle_pts(cx: float, cy: float, r: float, n: int = 80) -> list[tuple[float, float]]:
    return [
        (cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
        for i in range(n + 1)
    ]


def square_pts(cx: float, cy: float, side: float) -> list[tuple[float, float]]:
    h = side / 2.0
    return [
        (cx - h, cy - h), (cx + h, cy - h),
        (cx + h, cy + h), (cx - h, cy + h), (cx - h, cy - h),
    ]


def star_pts(cx: float, cy: float, r_out: float, r_in: float, points: int = 5) -> list[tuple[float, float]]:
    pts = []
    for i in range(points * 2 + 1):
        r = r_out if i % 2 == 0 else r_in
        angle = math.pi / 2 + 2 * math.pi * i / (points * 2) - math.pi
        pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return pts


def zigzag_fill(cx: float, cy: float, r: float, rows: int = 14) -> list[list[tuple[float, float]]]:
    """Horizontal zigzag rows spanning the interior of a circle -- a
    generated "colouring-in" scribble, not traced ink."""
    strokes: list[list[tuple[float, float]]] = []
    for row in range(rows):
        y = cy - r + (2 * r) * (row + 0.5) / rows
        dy = math.sqrt(max(r * r - (y - cy) ** 2, 0.0))
        if dy < 4:
            continue
        x0, x1 = cx - dy, cx + dy
        pts = []
        n_wiggle = 6
        for i in range(n_wiggle + 1):
            x = x0 + (x1 - x0) * i / n_wiggle
            yy = y + (6 if i % 2 == 0 else -6)
            pts.append((x, yy))
        strokes.append(pts)
    return strokes


def triangle_wave(x0: float, y: float, length: float, amp: float = 10.0, period: float = 24.0) -> list[tuple[float, float]]:
    pts = []
    x = x0
    n = int(length / (period / 2))
    for i in range(n + 1):
        yy = y + (amp if i % 2 == 0 else -amp)
        pts.append((x0 + i * (period / 2), yy))
    return pts


def checkmark(cx: float, cy: float, size: float = 24.0) -> list[tuple[float, float]]:
    return [
        (cx - size, cy),
        (cx - size / 3, cy + size * 0.7),
        (cx + size, cy - size * 0.9),
    ]


# --------------------------------------------------------------------------
# rmscene block construction
# --------------------------------------------------------------------------

def make_point(x: float, y: float, width: int = 6, pressure: int = 100) -> si.Point:
    return si.Point(x=x, y=y, speed=0, direction=0, width=width, pressure=pressure)


def make_line_block(
    parent: CrdtId,
    item_id: CrdtId,
    points: list[tuple[float, float]],
    tool: si.Pen,
    color: si.PenColor,
    width: int = 6,
    pressure: int = 100,
) -> SceneLineItemBlock:
    line = si.Line(
        color=color,
        tool=tool,
        points=[make_point(x, y, width=width, pressure=pressure) for x, y in points],
        thickness_scale=1.0,
        starting_length=0.0,
    )
    return SceneLineItemBlock(
        parent_id=parent,
        item=CrdtSequenceItem(
            item_id=item_id,
            left_id=CrdtId(0, 0),
            right_id=CrdtId(0, 0),
            deleted_length=0,
            value=line,
        ),
    )


def build_rm_page(strokes: list[dict]) -> bytes:
    """strokes: [{"points": [(x,y),...], "tool": si.Pen, "color": si.PenColor,
    "width": int, "pressure": int}, ...]"""
    author_uuid = uuid.uuid4()  # fresh, unrelated to any real device pairing

    scene_inf = SceneInfo(
        current_layer=LwwValue(CrdtId(0, 0), CrdtId(0, 0)),
        background_visible=LwwValue(CrdtId(0, 0), True),
        root_document_visible=LwwValue(CrdtId(0, 0), True),
        paper_size=(RM_WIDTH, RM_HEIGHT),
    )
    root_group = TreeNodeBlock(
        group=si.Group(
            node_id=CrdtId(0, 1),
            children=CrdtSequence(),
            label=LwwValue(CrdtId(0, 0), ""),
            visible=LwwValue(CrdtId(0, 0), True),
        )
    )
    layer1_group = TreeNodeBlock(
        group=si.Group(
            node_id=CrdtId(0, 11),
            children=CrdtSequence(),
            label=LwwValue(CrdtId(0, 12), "Layer 1"),
            visible=LwwValue(CrdtId(0, 0), True),
        )
    )
    scene_group = SceneGroupItemBlock(
        parent_id=CrdtId(0, 1),
        item=CrdtSequenceItem(
            item_id=CrdtId(0, 13),
            left_id=CrdtId(0, 0),
            right_id=CrdtId(0, 0),
            deleted_length=0,
            value=CrdtId(0, 11),
        ),
    )

    blocks: list = [
        AuthorIdsBlock(author_uuids={AUTHOR: author_uuid}),
        MigrationInfoBlock(migration_id=CrdtId(AUTHOR, 1), is_device=True),
        PageInfoBlock(
            loads_count=1, merges_count=0,
            text_chars_count=0, text_lines_count=0,
            type_folio_use_count=0,
        ),
        scene_inf,
        SceneTreeBlock(tree_id=CrdtId(0, 11), node_id=CrdtId(0, 0), is_update=True, parent_id=CrdtId(0, 1)),
        root_group,
        layer1_group,
        scene_group,
    ]

    seq = 16
    for s in strokes:
        blocks.append(make_line_block(
            parent=CrdtId(0, 11),
            item_id=CrdtId(AUTHOR, seq),
            points=s["points"],
            tool=s["tool"],
            color=s["color"],
            width=s.get("width", 6),
            pressure=s.get("pressure", 100),
        ))
        seq += 1

    buf = io.BytesIO()
    write_blocks(buf, blocks)
    return buf.getvalue()


def write_extracted_dir(dest: Path, title: str, rm_bytes: bytes) -> tuple[str, str]:
    """Write an extracted-rmdoc-dir (unzipped form) that tools/rm_render_page.py
    can read directly: <dest>/<doc_uuid>.content, <dest>/<doc_uuid>.metadata,
    <dest>/<doc_uuid>/<page_uuid>.rm -- no PDF, so it renders on a blank
    native-notebook background."""
    doc_uuid = str(uuid.uuid4())
    page_uuid = str(uuid.uuid4())
    ts_ms = int(time.time() * 1000)

    content = {
        "coverPageNumber": 0,
        "documentMetadata": {},
        "extraMetadata": {},
        "fileType": "notebook",
        "fontName": "",
        "formatVersion": 2,
        "lineHeight": -1,
        "margins": 125,
        "orientation": "portrait",
        "pageCount": 1,
        "cPages": {
            "lastOpened": {"timestamp": "0", "value": page_uuid},
            "pages": [{"id": page_uuid, "idx": {"timestamp": "0", "value": {"rindex": -1}}}],
            "uuids": [{"timestamp": "0", "value": page_uuid}],
        },
        "pageTags": [],
        "sizeInBytes": str(len(rm_bytes)),
        "tags": [],
        "textAlignment": "justify",
        "textScale": 1,
        "zoomMode": "bestFit",
    }
    metadata = {
        "createdTime": str(ts_ms),
        "lastModified": str(ts_ms),
        "lastOpened": "0",
        "lastOpenedPage": 0,
        "pinned": False,
        "type": "DocumentType",
        "visibleName": title,
        "parent": "",
    }

    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{doc_uuid}.content").write_text(json.dumps(content, indent=2), encoding="utf-8")
    (dest / f"{doc_uuid}.metadata").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    rm_dir = dest / doc_uuid
    rm_dir.mkdir(parents=True, exist_ok=True)
    (rm_dir / f"{page_uuid}.rm").write_bytes(rm_bytes)
    return doc_uuid, page_uuid


# --------------------------------------------------------------------------
# The two example pages
# --------------------------------------------------------------------------

def build_colouring_book_shapes() -> list[dict]:
    strokes: list[dict] = []
    circle_c = (620, 560)
    strokes.append({"points": circle_pts(*circle_c, 260), "tool": si.Pen.FINELINER_1, "color": si.PenColor.BLACK, "width": 8})
    strokes.append({"points": square_pts(300, 1160, 300), "tool": si.Pen.FINELINER_1, "color": si.PenColor.BLACK, "width": 8})
    strokes.append({"points": star_pts(1050, 1120, 190, 78), "tool": si.Pen.FINELINER_1, "color": si.PenColor.BLACK, "width": 8})

    # generated "colouring-in" fill, inside the circle only
    for pts in zigzag_fill(*circle_c, 220, rows=16):
        strokes.append({"points": pts, "tool": si.Pen.FINELINER_2, "color": si.PenColor.BLACK, "width": 4, "pressure": 60})

    # a highlighter box behind/around the square, to exercise the
    # translucent-highlighter compositing path
    hl = square_pts(300, 1160, 340)
    strokes.append({"points": hl, "tool": si.Pen.HIGHLIGHTER_1, "color": si.PenColor.YELLOW, "width": 28})

    return strokes


def build_ruled_writing_pad() -> list[dict]:
    strokes: list[dict] = []
    left, right = 160, 1244
    top, spacing = 340, 220
    for row in range(6):
        y = top + row * spacing
        strokes.append({"points": [(left, y), (right, y)], "tool": si.Pen.FINELINER_2, "color": si.PenColor.GRAY, "width": 3})
        wave = triangle_wave(left + 20, y - 14, length=620, amp=11, period=26)
        strokes.append({"points": wave, "tool": si.Pen.FINELINER_1, "color": si.PenColor.BLACK, "width": 6})
        if row % 2 == 0:
            strokes.append({"points": checkmark(right - 60, y - 14, size=20), "tool": si.Pen.FINELINER_1, "color": si.PenColor.BLACK, "width": 8})
    return strokes


EXAMPLES = {
    "colouring_book_shapes": build_colouring_book_shapes,
    "ruled_writing_pad": build_ruled_writing_pad,
}


def render(extracted_dir: Path, out_dir: Path) -> Path:
    import subprocess
    render_cli = TOOLS_DIR / "rm_render_page.py"
    proc = subprocess.run(
        [sys.executable, str(render_cli), str(extracted_dir),
         "--out", str(out_dir), "--supersample", "2", "--all-pages"],
        capture_output=True, encoding="utf-8", errors="replace", timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"render failed for {extracted_dir}:\n{proc.stdout}\n{proc.stderr}")
    pngs = sorted(out_dir.glob("page_*.png"))
    if not pngs:
        raise FileNotFoundError(f"no PNG produced in {out_dir}\n{proc.stdout}")
    return pngs[0]


def main() -> int:
    for name, builder in EXAMPLES.items():
        strokes = builder()
        rm_bytes = build_rm_page(strokes)
        src_dir = EXAMPLES_DIR / "sources" / name
        doc_uuid, page_uuid = write_extracted_dir(src_dir, title=name, rm_bytes=rm_bytes)
        png = render(src_dir, src_dir / "_pngs")
        dest_png = EXAMPLES_DIR / f"{name}.png"
        dest_png.write_bytes(png.read_bytes())
        print(f"{name}: {len(strokes)} strokes -> {dest_png} ({dest_png.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
