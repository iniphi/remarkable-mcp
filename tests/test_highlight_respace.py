r"""Re-spacing wrapped glyph highlights from the PDF's own words (S7b).

A snap-to-text GlyphRange loses the space at a wrapped line break. The text is
rebuilt from PDF words inside the highlight rects, adopted only on an exact
whitespace-insensitive character match; otherwise the device text is kept.

Run: python -m pytest tests/test_highlight_respace.py
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

from rm_extract_highlights import cluster_highlights, respace_glyph_text  # noqa: E402


def word(x0, y0, x1, y1, text, order):
    return (x0, y0, x1, y1, text, order)


# Two text lines: line 1 y 100-112, line 2 y 113-125.
WRAPPED_WORDS = [
    word(72, 100, 100, 112, "Before", 0),
    word(102, 100, 130, 112, "signs", 7),
    word(132, 100, 150, 112, "or", 13),
    word(72, 113, 120, 125, "numbers", 16),
    word(122, 113, 160, 125, "after", 24),
]
WRAPPED_RECTS = [[100, 100, 152, 112], [70, 113, 122, 125]]


class Respace(unittest.TestCase):
    def test_a_wrapped_passage_missing_its_break_space_is_spaced_from_pdf_words(self):
        rects = [[100, 100, 152, 112], [70, 113, 122, 125]]
        text, src = respace_glyph_text("signsornumbers", rects, WRAPPED_WORDS)
        self.assertEqual(text, "signs or numbers")
        self.assertEqual(src, "pdf_words")

    def test_b_letters_differ_so_device_text_is_kept(self):
        text, src = respace_glyph_text("signsornurnbers", WRAPPED_RECTS, WRAPPED_WORDS)
        self.assertEqual(text, "signsornurnbers")
        self.assertEqual(src, "device")

    def test_b2_case_difference_is_a_mismatch(self):
        text, src = respace_glyph_text("SignsOrNumbers", WRAPPED_RECTS, WRAPPED_WORDS)
        self.assertEqual((text, src), ("SignsOrNumbers", "device"))

    def test_c_no_text_layer_leaves_text_unchanged(self):
        text, src = respace_glyph_text("signsornumbers", WRAPPED_RECTS, [])
        self.assertEqual((text, src), ("signsornumbers", "device"))

    def test_d_hyphenated_line_end_keeps_hyphen_and_gains_a_space(self):
        # Exact-match rule: the hyphen is a real PDF character present in the
        # device text, so "transforma-tion" re-spaces to "transforma- tion"
        # (matches the merge layer's own non-dehyphenating behaviour).
        words = [word(72, 100, 140, 112, "transforma-", 0),
                 word(72, 113, 100, 125, "tion", 12)]
        rects = [[70, 100, 142, 112], [70, 113, 102, 125]]
        text, src = respace_glyph_text("transforma-tion", rects, words)
        self.assertEqual((text, src), ("transforma- tion", "pdf_words"))

    def test_e_same_line_highlight_already_correct_is_unchanged(self):
        words = [word(72, 100, 100, 112, "the", 0), word(102, 100, 140, 112, "same", 4)]
        text, src = respace_glyph_text("the same", [[70, 100, 142, 112]], words)
        self.assertEqual(text, "the same")
        self.assertEqual(src, "pdf_words")

    def test_ligature_in_pdf_words_matches_plain_device_text(self):
        words = [word(72, 100, 100, 112, "ﬁrst", 0), word(102, 100, 140, 112, "line", 6)]
        text, src = respace_glyph_text("firstline", [[70, 100, 142, 112]], words)
        self.assertEqual(src, "pdf_words")
        self.assertEqual(text.replace("ﬁ", "fi"), "first line")

    def test_rects_selecting_no_words_keeps_device_text(self):
        text, src = respace_glyph_text("abc", [[500, 500, 510, 510]], WRAPPED_WORDS)
        self.assertEqual((text, src), ("abc", "device"))


class MergeCarriesProvenance(unittest.TestCase):
    def rec(self, text, device, src):
        return {"pdf_page": 1, "source": "glyph", "text": text, "text_device": device,
                "text_source": src, "stroke_color": "YELLOW",
                "bbox_pdf": [72, 100, 300, 112], "rects_pdf": [[72, 100, 300, 112]],
                "n_points": None}

    def test_single_record_keeps_additive_keys(self):
        out = cluster_highlights([self.rec("signs or", "signsor", "pdf_words")])
        self.assertEqual(out[0]["text"], "signs or")
        self.assertEqual(out[0]["text_device"], "signsor")
        self.assertEqual(out[0]["text_source"], "pdf_words")

    def test_records_without_text_source_gain_no_new_keys(self):
        r = self.rec("x", "x", "device")
        del r["text_source"], r["text_device"]
        out = cluster_highlights([r])
        self.assertNotIn("text_source", out[0])
        self.assertNotIn("text_device", out[0])


if __name__ == "__main__":
    unittest.main()
