"""Offline unit tests for rm_push_content's render+push wrapper
(roundtrip.render_and_push_content).

The render leg runs for real (a child rm_render_content.py process, no device);
the push leg (push_local_file) is monkeypatched so nothing touches rmapi or the
cloud. Validation guards return before any render at all.

Run: python -m pytest tests/test_push_content.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))
from rm_mcp import config as _config  # noqa: E402
TOOLS_DIR = _config.TOOLS_DIR
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from rm_mcp import roundtrip  # noqa: E402

# A clean public install has NO PDF engine: pymupdf is AGPL and deliberately
# absent from this package's dependencies. A test that renders a PDF for real
# must SKIP there rather than fail, or the published repo ships a suite that
# goes red on first run and looks broken when it is doing exactly what it says.
#
# Same decorator and same reason as tests/test_creation.py, which got this
# right when the creation lane was written. This older file was never brought
# up to it, and nothing caught that because every run happened on a desk where
# pymupdf is installed. Found 2026-09-22 by installing the PUBLISHED repo into
# a clean venv per its own README, where this was the only failure.
needs_pdf_engine = unittest.skipUnless(
    roundtrip._pdf_engine_available(),
    "no PDF engine (pip install pymupdf) -- expected on a clean install")


class _PushSpy:
    """Stand-in for push_local_file: records the file it was handed and
    returns an ok envelope so the wrapper's post-push stamping runs."""

    def __init__(self) -> None:
        self.pushed: Path | None = None
        self.project: str | None = None
        self.title: str | None = None

    def __call__(self, local: Path, project: str | None, title: str | None) -> dict:
        self.pushed = local
        self.project = project
        self.title = title
        return {"ok": True, "data": {"device_path": f"/00_Projects/{project}/{local.stem}"},
                "warnings": [], "error": None}


class TestValidationGuards(unittest.TestCase):
    """These return before any render, so no monkeypatch / device is involved."""

    def test_unknown_mode_errors(self):
        r = roundtrip.render_and_push_content("pdf", "x", "104_Stacks", None)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["system"], "config")

    def test_empty_content_errors(self):
        r = roundtrip.render_and_push_content("markdown", "   \n ", "104_Stacks", None)
        self.assertFalse(r["ok"])

    def test_native_requires_title(self):
        r = roundtrip.render_and_push_content("native", "# Notes\n- a", "104_Stacks", None)
        self.assertFalse(r["ok"])
        self.assertIn("title", r["error"]["message"].lower())


class TestRenderThenPush(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = roundtrip.push_local_file
        self.spy = _PushSpy()
        roundtrip.push_local_file = self.spy

    def tearDown(self) -> None:
        roundtrip.push_local_file = self._orig

    @needs_pdf_engine
    def test_markdown_renders_pdf_then_pushes(self):
        r = roundtrip.render_and_push_content(
            "markdown", "# Title\n\nBody paragraph.", "104_Stacks", "Brief")
        self.assertTrue(r["ok"], r)
        self.assertIsNotNone(self.spy.pushed)
        self.assertEqual(self.spy.pushed.suffix, ".pdf")
        self.assertTrue(self.spy.pushed.is_file())
        self.assertGreater(self.spy.pushed.stat().st_size, 0)
        self.assertEqual(self.spy.project, "104_Stacks")
        # wrapper stamps mode + rendered_from onto the pushed envelope
        self.assertEqual(r["data"]["mode"], "markdown")
        self.assertTrue(r["data"]["rendered_from"].endswith(".src"))

    def test_native_renders_rmdoc_then_pushes(self):
        r = roundtrip.render_and_push_content(
            "native", "# Notes\n- one\n- two", "104_Stacks", "Notes")
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.spy.pushed.suffix, ".rmdoc")
        self.assertTrue(self.spy.pushed.is_file())
        self.assertEqual(r["data"]["mode"], "native")


if __name__ == "__main__":
    unittest.main()
