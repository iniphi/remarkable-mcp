r"""Line-break spacing in merged glyph highlights.

Wrapped snap-to-text highlights came back with the space at a line break lost
("signsor", "samelanguage"). Diagnosis: cluster_highlights joins DEVICE RECORDS with a space, so a record
boundary at a line break never loses one. The lost space lives inside a single
GlyphRange's own text (one record, several rects), where no boundary is
detectable. These tests pin both halves so a refactor of the join cannot
quietly move the loss into the merge.

Run: python -m pytest tests/test_highlight_linebreak_space.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

RM_MCP_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = RM_MCP_DIR.parent / "tools"
for _p in (RM_MCP_DIR, TOOLS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from rm_extract_highlights import cluster_highlights  # noqa: E402


def rec(y0, y1, text, x0=72.0, x1=300.0):
    return {
        "pdf_page": 12,
        "source": "glyph",
        "text": text,
        "stroke_color": "YELLOW",
        "bbox_pdf": [x0, y0, x1, y1],
        "rects_pdf": [[x0, y0, x1, y1]],
        "n_points": None,
    }


class LineBreakSpace(unittest.TestCase):
    def test_record_boundary_at_line_break_keeps_a_space(self):
        out = cluster_highlights([rec(100, 112, "the same"), rec(113, 125, "language")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["n_merged"], 2)
        self.assertEqual(out[0]["text"], "the same language")

    def test_trailing_hyphen_is_kept_not_dehyphenated(self):
        out = cluster_highlights([rec(100, 112, "struc-"), rec(113, 125, "tural")])
        self.assertEqual(out[0]["text"], "struc- tural")

    def test_single_record_text_is_passed_through_untouched(self):
        # The device's own glyph text, even where it lacks a space, is not
        # altered: with one record there is no boundary to detect.
        out = cluster_highlights([rec(100, 125, "signsor")])
        self.assertEqual(out[0]["text"], "signsor")
        self.assertEqual(out[0]["n_merged"], 1)


if __name__ == "__main__":
    unittest.main()
