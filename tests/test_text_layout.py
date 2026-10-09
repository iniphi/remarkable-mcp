"""Typed-text layout helper (rm_text_layout): pure geometry, offline.

The line heights in rm_text_layout are UNCALIBRATED, so nothing here asserts an
exact pixel value or reads the module's own constants back. Expectations are
stated independently: ordering, strictly-greater-than, and a hard-coded floor
of 20 rm units per line (no real typed line is shorter than that).
"""

from __future__ import annotations

import sys
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

import io  # noqa: E402

import rmscene  # noqa: E402
from rmscene import RootTextBlock  # noqa: E402
from rmscene.crdt_sequence import CrdtSequence, CrdtSequenceItem  # noqa: E402
from rmscene.scene_items import CrdtId, LwwValue, ParagraphStyle  # noqa: E402
from rmscene import scene_items as si  # noqa: E402

import rm_make_text_notebook as mtn  # noqa: E402
import rm_text_layout as lay  # noqa: E402

FLOOR = 20.0  # hard-coded: one line is never shorter than this
END_ID = CrdtId(0, 0xFFFFFFFFFFFF)


def _text_for(paragraphs: list[dict]) -> si.Text:
    raw = mtn.build_rm_page(paragraphs, uuid.uuid4())
    for b in rmscene.read_blocks(io.BytesIO(raw)):
        if isinstance(b, RootTextBlock):
            return b.value
    raise AssertionError("no RootTextBlock")


def _text_literal(s: str, styles: dict | None = None, width: float = 936.0) -> si.Text:
    item = CrdtSequenceItem(item_id=CrdtId(1, 100), left_id=CrdtId(0, 0),
                            right_id=CrdtId(0, 0), deleted_length=0, value=s)
    return si.Text(items=CrdtSequence([item]), styles=styles or {},
                   pos_x=-468.0, pos_y=234.0, width=width)


class TestLayout(unittest.TestCase):
    def setUp(self):
        self.text = _text_for([{"text": "Title", "style": "heading"},
                               {"text": "Body", "style": "plain"}])
        self.layout = lay.layout_text(self.text)

    def test_boundaries_monotonic_from_pos_y_and_end_marker(self):
        ps = self.layout.paragraphs
        self.assertEqual(len(ps), 2)
        self.assertEqual(ps[0].start_y, 234.0)
        self.assertGreaterEqual(ps[1].start_y - ps[0].start_y, FLOOR)
        self.assertGreaterEqual(self.layout.end_y - ps[1].start_y, FLOOR)
        self.assertEqual(self.layout.end_y, ps[1].end_y)

    def test_heading_is_taller_than_plain(self):
        ps = self.layout.paragraphs
        self.assertGreater(ps[0].end_y - ps[0].start_y,
                           ps[1].end_y - ps[1].start_y)

    def test_long_paragraph_wraps_to_more_lines(self):
        short = lay.layout_text(_text_literal("word"))
        long_ = lay.layout_text(_text_literal("word " * 400))
        self.assertGreater(long_.end_y - 234.0, short.end_y - 234.0)
        self.assertGreater(long_.paragraphs[0].lines, 1)

    def test_empty_paragraph_counts_as_a_line(self):
        gap = lay.layout_text(_text_literal("a\n\nb"))
        tight = lay.layout_text(_text_literal("a\nb"))
        self.assertEqual(len(gap.paragraphs), 3)
        self.assertGreaterEqual(gap.end_y - tight.end_y, FLOOR)

    def test_trailing_newline_does_not_add_a_phantom_paragraph(self):
        # Pinned choice: the final newline terminates the last line; it does
        # not open an extra empty one.
        a = lay.layout_text(_text_literal("a\nb\n"))
        b = lay.layout_text(_text_literal("a\nb"))
        self.assertEqual(len(a.paragraphs), 2)
        self.assertEqual(a.end_y, b.end_y)

    def test_char_ids_map_to_paragraphs_and_newline_stays_with_its_paragraph(self):
        seq = mtn.TEXT_START_SEQ
        # "Title\n" occupies seq..seq+5, "Body\n" seq+6..seq+10
        self.assertEqual(self.layout.paragraph_of(CrdtId(1, seq)), 0)
        self.assertEqual(self.layout.paragraph_of(CrdtId(1, seq + 5)), 0)  # its \n
        self.assertEqual(self.layout.paragraph_of(CrdtId(1, seq + 6)), 1)
        self.assertIsNone(self.layout.paragraph_of(CrdtId(9, 9999)))

    def test_anchor_y_resolution(self):
        seq = mtn.TEXT_START_SEQ
        ps = self.layout.paragraphs
        self.assertEqual(lay.anchor_y(self.layout, END_ID), self.layout.end_y)
        self.assertEqual(lay.anchor_y(self.layout, CrdtId(1, seq + 6)), ps[1].start_y)
        # Mid-paragraph snaps to the paragraph START (decided 2026-10-08).
        self.assertEqual(lay.anchor_y(self.layout, CrdtId(1, seq + 8)), ps[1].start_y)
        self.assertIsNone(lay.anchor_y(self.layout, CrdtId(0, 0xFFFFFFFFFFFE)))
        self.assertIsNone(lay.anchor_y(self.layout, CrdtId(9, 9999)))


if __name__ == "__main__":
    unittest.main()
