"""Text-anchored ink is placed at its anchor, not at the top of the page.

Offline: every page is built in-test from the Type Folio builder plus a
hand-added anchored Group and a stroke, written through rmscene's real writer
and read back through parse_rm_blocks (so the LwwValue wrappers are real).

Oracles are INDEPENDENT of rm_text_layout's constants: ink must sit at or below
the root text's pos_y (234) plus a hard-coded floor of 20 rm units per
paragraph, and strictly lower than the same ink unanchored. The line heights
are uncalibrated; nothing here pins an exact pixel.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))
from _toolpath import tool_script  # noqa: E402

TOOLS_DIR = next((d for d in (RM_MCP_DIR / "tools", RM_MCP_DIR.parent / "tools")
                  if tool_script(d, "rm_render_page.py").is_file()),
                 RM_MCP_DIR / "tools")
for _d in [TOOLS_DIR, TOOLS_DIR / "rm"]:
    if _d.is_dir() and str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import numpy as np  # noqa: E402
import rmscene  # noqa: E402
from PIL import Image  # noqa: E402
from rmscene import (SceneGroupItemBlock, SceneLineItemBlock,  # noqa: E402
                     SceneTreeBlock, TreeNodeBlock, write_blocks)
from rmscene import scene_items as si  # noqa: E402
from rmscene.crdt_sequence import CrdtSequenceItem  # noqa: E402
from rmscene.scene_items import CrdtId, LwwValue, Pen, PenColor  # noqa: E402

import rm_make_text_notebook as mtn  # noqa: E402
import rm_render_page as rrp  # noqa: E402

RENDER_CLI = tool_script(TOOLS_DIR, "rm_render_page.py")
END_ID = CrdtId(0, 0xFFFFFFFFFFFF)
TOP_ID = CrdtId(0, 0xFFFFFFFFFFFE)
ROOT = CrdtId(0, 1)
TS = CrdtId(0, 0)
TEXT_TOP = 234.0          # Text.pos_y written by the builder
FLOOR = 20.0              # hard-coded minimum per paragraph
STROKE_Y0, STROKE_Y1 = 77.0, 359.0
SEQ0 = mtn.TEXT_START_SEQ


def _seq_item(node_seq: int, value, author: int = 0) -> CrdtSequenceItem:
    return CrdtSequenceItem(item_id=CrdtId(author, node_seq), left_id=TS,
                            right_id=TS, deleted_length=0, value=value)


def _group_blocks(node: CrdtId, parent: CrdtId, anchor_id: CrdtId | None,
                  ox: float = 187.7, atype: int = 2) -> list:
    group = si.Group(node_id=node, label=LwwValue(TS, ""),
                     visible=LwwValue(TS, True))
    if anchor_id is not None:
        group.anchor_id = LwwValue(TS, anchor_id)
        group.anchor_type = LwwValue(TS, atype)
        group.anchor_threshold = LwwValue(TS, 35.7)
        group.anchor_origin_x = LwwValue(TS, ox)
    return [
        TreeNodeBlock(group=group),
        SceneTreeBlock(tree_id=node, node_id=TS, is_update=True, parent_id=parent),
        SceneGroupItemBlock(parent_id=parent,
                            item=_seq_item(100 + node.part2, node)),
    ]


def _stroke(parent: CrdtId, n: int = 0) -> SceneLineItemBlock:
    pts = [si.Point(x=100.0 + 5 * i, y=STROKE_Y0 + (STROKE_Y1 - STROKE_Y0) * i / 9,
                    speed=0, direction=0, width=4, pressure=120) for i in range(10)]
    line = si.Line(color=PenColor.BLACK, tool=Pen.BALLPOINT_1, points=pts,
                   thickness_scale=1.0, starting_length=0.0)
    return SceneLineItemBlock(parent_id=parent, item=_seq_item(500 + n, line, 1))


def build_page(groups: list[tuple[CrdtId, CrdtId, CrdtId | None]],
               paragraphs: list[dict] | None = None, textless: bool = False,
               ox: float = 187.7, atype: int = 2) -> bytes:
    """groups: (node, parent, anchor_id). Each gets one stroke."""
    paragraphs = paragraphs or [{"text": "Title", "style": "heading"},
                                {"text": "Body text here", "style": "plain"}]
    base = list(rmscene.read_blocks(io.BytesIO(
        mtn.build_rm_page(paragraphs, uuid.uuid4()))))
    if textless:
        base = [b for b in base if type(b).__name__ != "RootTextBlock"]
    for i, (node, parent, anchor) in enumerate(groups):
        base += _group_blocks(node, parent, anchor, ox=ox, atype=atype)
        base.append(_stroke(node, i))
    buf = io.BytesIO()
    write_blocks(buf, base)
    return buf.getvalue()


def parse(raw: bytes) -> list:
    return rrp.parse_rm_blocks(raw)


def _ink_rows_cols(canvas: Image.Image) -> tuple[int, int, int, int]:
    a = np.asarray(canvas.convert("L"))
    ys, xs = np.where(a < 128)
    return int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())


def render(blocks: list, offsets=None, ss: bool = False) -> Image.Image:
    """Mirror main(): bbox from translated points, canvas, overlay."""
    sb = rrp.stroke_bbox(blocks, offsets) if offsets is not None else rrp.stroke_bbox(blocks)
    w, h, px, py = rrp.compute_canvas_layout(rrp.RM_WIDTH, rrp.RM_HEIGHT, sb)
    canvas = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    kw = {"offsets": offsets} if offsets is not None else {}
    rrp.overlay_strokes(canvas, blocks, page_x=px, page_y=py, **kw)
    top, bot, left, right = _ink_rows_cols(canvas)
    render.pad = (px, py)
    return canvas


class TestResolveAndRoundTrip(unittest.TestCase):
    def test_anchor_fields_survive_writer_and_are_unwrapped(self):
        blocks = parse(build_page([(CrdtId(0, 21), ROOT, END_ID)]))
        plan = rrp.resolve_anchor_offsets(blocks)
        self.assertEqual(plan.placed, 1)
        self.assertEqual(plan.unresolved, 0)
        dx, dy = plan.offsets[CrdtId(0, 21)]
        self.assertAlmostEqual(dx, 187.7, places=1)
        self.assertGreaterEqual(dy, TEXT_TOP + 2 * FLOOR)

    def test_no_root_text_gives_no_offsets_but_counts_unresolved(self):
        blocks = parse(build_page([(CrdtId(0, 21), ROOT, END_ID)], textless=True))
        plan = rrp.resolve_anchor_offsets(blocks)
        self.assertEqual(plan.offsets, {})
        self.assertEqual(plan.unresolved, 1)

    def test_unanchored_groups_are_not_counted_at_all(self):
        blocks = parse(build_page([(CrdtId(0, 21), ROOT, None)]))
        plan = rrp.resolve_anchor_offsets(blocks)
        self.assertEqual((plan.offsets, plan.placed, plan.unresolved), ({}, 0, 0))

    def test_unknown_char_top_marker_and_other_type_are_unresolved_and_counted(self):
        for anchor, atype in ((CrdtId(9, 9999), 2), (TOP_ID, 2), (END_ID, 7)):
            with self.subTest(anchor=anchor, atype=atype):
                blocks = parse(build_page([(CrdtId(0, 21), ROOT, anchor)], atype=atype))
                plan = rrp.resolve_anchor_offsets(blocks)
                self.assertEqual(plan.offsets, {})
                self.assertEqual(plan.unresolved, 1)
                self.assertTrue(plan.notes)

    def test_nested_anchored_groups_compose(self):
        outer, inner = CrdtId(0, 21), CrdtId(0, 22)
        blocks = parse(build_page([(outer, ROOT, END_ID), (inner, outer, END_ID)]))
        plan = rrp.resolve_anchor_offsets(blocks)
        (ox, oy), (ix, iy) = plan.offsets[outer], plan.offsets[inner]
        self.assertAlmostEqual(iy, 2 * oy)
        self.assertAlmostEqual(ix, 2 * ox)
        self.assertTrue(any("nest" in n for n in plan.notes))

    def test_unanchored_child_of_anchored_group_inherits_offset(self):
        outer, inner = CrdtId(0, 21), CrdtId(0, 22)
        blocks = parse(build_page([(outer, ROOT, END_ID), (inner, outer, None)]))
        plan = rrp.resolve_anchor_offsets(blocks)
        self.assertEqual(plan.offsets[inner], plan.offsets[outer])


class TestPlacement(unittest.TestCase):
    def test_end_of_text_anchor_renders_below_typed_block(self):
        node = CrdtId(0, 21)
        raw_a = parse(build_page([(node, ROOT, END_ID)]))
        raw_u = parse(build_page([(node, ROOT, None)]))
        plan = rrp.resolve_anchor_offsets(raw_a)
        canvas_a = render(raw_a, plan.offsets)
        pad_a = render.pad
        top_a = _ink_rows_cols(canvas_a)[0] - pad_a[1]
        canvas_u = render(raw_u)
        top_u = _ink_rows_cols(canvas_u)[0] - render.pad[1]
        self.assertGreaterEqual(top_a, TEXT_TOP + 2 * FLOOR)
        self.assertGreater(top_a, top_u + FLOOR)
        self.assertLessEqual(top_u, STROKE_Y0)  # the defect: raised to ~77

    def test_anchor_origin_x_shifts_ink_horizontally(self):
        node = CrdtId(0, 21)
        a = parse(build_page([(node, ROOT, END_ID)], ox=187.7))
        u = parse(build_page([(node, ROOT, None)]))
        la = _ink_rows_cols(render(a, rrp.resolve_anchor_offsets(a).offsets))[2]
        pa = render.pad[0]
        lu = _ink_rows_cols(render(u))[2]
        pu = render.pad[0]
        shift = (la - pa) - (lu - pu)
        self.assertAlmostEqual(shift, 187.7, delta=2.0)

    def test_paragraph_anchors_order_and_do_not_overlap(self):
        n1, n2 = CrdtId(0, 21), CrdtId(0, 22)
        a1 = CrdtId(1, SEQ0)          # first char of "Title"
        a2 = CrdtId(1, SEQ0 + 6)      # first char of the body paragraph
        blocks = parse(build_page([(n1, ROOT, a1), (n2, ROOT, a2)]))
        plan = rrp.resolve_anchor_offsets(blocks)
        self.assertEqual(plan.placed, 2)
        dy1, dy2 = plan.offsets[n1][1], plan.offsets[n2][1]
        self.assertGreaterEqual(dy1, TEXT_TOP)
        self.assertGreaterEqual(dy2 - dy1, FLOOR)

    def test_trailing_newline_anchor_stays_with_its_paragraph(self):
        n1, n2 = CrdtId(0, 21), CrdtId(0, 22)
        nl = CrdtId(1, SEQ0 + 5)      # the "\n" ending "Title"
        first = CrdtId(1, SEQ0)
        blocks = parse(build_page([(n1, ROOT, nl), (n2, ROOT, first)]))
        plan = rrp.resolve_anchor_offsets(blocks)
        self.assertEqual(plan.offsets[n1], plan.offsets[n2])

    def test_unanchored_and_textless_pages_render_pixel_identically(self):
        node = CrdtId(0, 21)
        for textless in (False, True):
            with self.subTest(textless=textless):
                blocks = parse(build_page([(node, ROOT, None)], textless=textless))
                plan = rrp.resolve_anchor_offsets(blocks)
                self.assertEqual(plan.offsets, {})
                old = render(blocks)
                new = render(blocks, plan.offsets)
                self.assertEqual(old.size, new.size)
                self.assertEqual(old.tobytes(), new.tobytes())

    def test_stroke_bbox_default_unchanged_and_offsets_translate(self):
        blocks = parse(build_page([(CrdtId(0, 21), ROOT, END_ID)]))
        raw = rrp.stroke_bbox(blocks)
        self.assertEqual((raw[2], raw[3]), (STROKE_Y0, STROKE_Y1))
        moved = rrp.stroke_bbox(blocks, {CrdtId(0, 21): (10.0, 2000.0)})
        self.assertEqual((moved[0], moved[2]), (raw[0] + 10.0, raw[2] + 2000.0))
        self.assertEqual(rrp.stroke_bbox(blocks, {}), raw)

    def test_canvas_extends_for_ink_pushed_below_the_page(self):
        blocks = parse(build_page([(CrdtId(0, 21), ROOT, END_ID)]))
        offsets = {CrdtId(0, 21): (0.0, 2500.0)}
        canvas = render(blocks, offsets)
        self.assertGreater(canvas.height, 2500 + int(STROKE_Y1))
        top, bot, _, _ = _ink_rows_cols(canvas)
        self.assertGreater(bot - render.pad[1], 2500 + STROKE_Y1 - 5)

    def test_scaled_canvas_adds_offset_in_rm_units_before_mapping(self):
        # PDF-backed pages map rm units to canvas with rm_per_canvas != 1.0.
        node = CrdtId(0, 21)
        blocks = parse(build_page([(node, ROOT, END_ID)]))
        rpc = 2.0
        offsets = {node: (0.0, 400.0)}
        canvas = Image.new("RGBA", (1404, 1600), (255, 255, 255, 255))
        rrp.overlay_strokes(canvas, blocks, rm_per_canvas_x=rpc,
                            rm_per_canvas_y=rpc, offsets=offsets)
        top = _ink_rows_cols(canvas)[0]
        self.assertAlmostEqual(top, (STROKE_Y0 + 400.0) / rpc, delta=4)
        # and the bbox for sizing uses the same translated rm coordinates
        sb = rrp.stroke_bbox(blocks, offsets)
        self.assertEqual(sb[2], STROKE_Y0 + 400.0)


class TestCli(unittest.TestCase):
    def _bundle(self, root: Path, raw: bytes) -> Path:
        doc = str(uuid.uuid4())
        page = str(uuid.uuid4())
        (root / doc).mkdir(parents=True)
        (root / f"{doc}.content").write_text(json.dumps({
            "fileType": "notebook", "formatVersion": 2, "pageCount": 1,
            "cPages": {"pages": [{"id": page,
                                  "idx": {"timestamp": "0", "value": "aa"}}]},
        }), encoding="utf-8")
        (root / f"{doc}.metadata").write_text(json.dumps(
            {"visibleName": "anchor", "type": "DocumentType"}), encoding="utf-8")
        (root / doc / f"{page}.rm").write_bytes(raw)
        return root

    def _run(self, raw: bytes, *flags: str):
        with tempfile.TemporaryDirectory() as td:
            src = self._bundle(Path(td) / "src", raw)
            out = Path(td) / "out"
            proc = subprocess.run(
                [sys.executable, str(RENDER_CLI), str(src), "--out", str(out),
                 "--all-pages", *flags],
                capture_output=True, encoding="utf-8", errors="replace", timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            png = next(out.glob("*.png"))
            img = Image.open(png).convert("RGBA")
            return proc.stdout, _ink_rows_cols(img), img.size

    def test_cli_places_ink_on_plain_supersample_and_crop_paths(self):
        anchored = build_page([(CrdtId(0, 21), ROOT, END_ID)])
        plain_out, plain, _ = self._run(anchored)
        ss_out, ss, _ = self._run(anchored, "--supersample", "2")
        self.assertIn("anchors placed=1 unresolved=0", plain_out)
        self.assertAlmostEqual(plain[0], ss[0], delta=4)
        pad = rrp.CANVAS_PADDING
        self.assertGreaterEqual(plain[0] - pad, TEXT_TOP + 2 * FLOOR)
        # crop path: ink bounding box is unchanged in size, page is tight
        _, _, crop_size = self._run(anchored, "--crop")
        self.assertLess(crop_size[1], rrp.RM_HEIGHT)

    def test_cli_surfaces_unresolved_count(self):
        raw = build_page([(CrdtId(0, 21), ROOT, CrdtId(9, 9999))])
        out, _, _ = self._run(raw)
        self.assertIn("unresolved=1", out)
        self.assertIn("ANCHOR-UNRESOLVED 1", out)

    def test_cli_line_unchanged_for_unanchored_pages(self):
        raw = build_page([(CrdtId(0, 21), ROOT, None)])
        out, _, _ = self._run(raw)
        self.assertNotIn("anchors", out.lower())


if __name__ == "__main__":
    unittest.main()
