"""Snap-to-text highlight colour: an all-zero color_rgba must not paint black.

A real reMarkable 2 yellow snap-to-text highlight stores GlyphRange.color =
PenColor.YELLOW with color_rgba = (0, 0, 0, 255). The renderer used to trust
the tuple and painted a translucent black bar. Offline, no device.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))
# Ships in the public tree too, where tools/ is flat beside tests/; the private
# tree splits it into tools/rm/. Ask tool_script rather than build either path.
from _toolpath import tool_script  # noqa: E402

TOOLS_DIR = next((d for d in (RM_MCP_DIR / "tools", RM_MCP_DIR.parent / "tools")
                  if tool_script(d, "rm_render_page.py").is_file()),
                 RM_MCP_DIR / "tools")
for _d in [TOOLS_DIR, TOOLS_DIR / "rm"]:
    if _d.is_dir() and str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from PIL import Image  # noqa: E402
from rmscene.scene_items import PenColor  # noqa: E402

import rm_render_page  # noqa: E402


class SceneGlyphItemBlock:
    def __init__(self, color, color_rgba):
        rect = SimpleNamespace(x=0.0, y=100.0, w=100.0, h=50.0)
        gr = SimpleNamespace(rectangles=[rect], color=color, color_rgba=color_rgba)
        self.item = SimpleNamespace(value=gr)


def _render_pixel(block) -> tuple[int, int, int]:
    canvas = Image.new("RGBA", (1404, 400), (255, 255, 255, 255))
    rm_render_page.overlay_strokes(canvas, [block])
    # rect spans canvas x 702..802, y 100..150
    return canvas.getpixel((750, 125))[:3]


class TestGlyphHighlightColour(unittest.TestCase):
    def test_zero_rgba_falls_back_to_pen_colour_yellow(self):
        r, g, b = _render_pixel(SceneGlyphItemBlock(PenColor.YELLOW, (0, 0, 0, 255)))
        self.assertGreater(r, 200)
        self.assertGreater(g, 200)
        self.assertGreater(r - b, 40)

    def test_zero_rgba_and_no_colour_uses_default_yellow(self):
        r, g, b = _render_pixel(SceneGlyphItemBlock(None, (0, 0, 0, 255)))
        self.assertGreater(r - b, 40)
        self.assertGreater(g - b, 40)

    def test_real_rgba_is_honoured(self):
        r, g, b = _render_pixel(SceneGlyphItemBlock(PenColor.YELLOW, (0, 200, 0, 255)))
        self.assertGreater(g - r, 40)
        self.assertGreater(g - b, 40)


if __name__ == "__main__":
    unittest.main()
