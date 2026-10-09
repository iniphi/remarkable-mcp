"""The MCP render tools surface an unresolved-anchor count as a warning."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))

from rm_mcp import roundtrip  # noqa: E402


class TestAnchorWarnings(unittest.TestCase):
    def test_unresolved_line_becomes_a_warning_with_count(self):
        out = ("  page   1 -> page_001.png  (1404x1872, rm/canvas=1.000, 1 strokes, "
               "3 kB)  [anchors placed=0 unresolved=2]  [ANCHOR-UNRESOLVED 2]\n")
        warns = roundtrip.anchor_warnings(out)
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0]["code"], "anchor_unresolved")
        self.assertEqual(warns[0]["data"]["unresolved"], 2)
        self.assertTrue(warns[0]["possible_data_loss"])

    def test_counts_sum_across_pages(self):
        out = "x [ANCHOR-UNRESOLVED 1]\ny [ANCHOR-UNRESOLVED 3]\n"
        self.assertEqual(roundtrip.anchor_warnings(out)[0]["data"]["unresolved"], 4)

    def test_clean_output_gives_no_warning(self):
        self.assertEqual(roundtrip.anchor_warnings(
            "page 1 [anchors placed=1 unresolved=0]\n"), [])
        self.assertEqual(roundtrip.anchor_warnings(""), [])


if __name__ == "__main__":
    unittest.main()
