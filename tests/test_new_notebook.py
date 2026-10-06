"""rm_new_notebook: a template-built Type Folio notebook, pushed like any push.

Added 2026-09-10 with the templates in tools/rm_make_text_notebook.py.
Everything is offline: the build runs the real script as a child process, the
push is replaced by a fake that records what it was handed. The template
values themselves are what three real notebooks carried; whether the device
honours them on a fresh upload is checked live, not here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))

from rm_mcp import config, roundtrip  # noqa: E402

TOOLS_DIR = config.TOOLS_DIR

from _toolpath import tool_script  # noqa: E402
# The builder CLI, wherever the layout puts it (2026-09-20 split).
BUILDER_CLI = tool_script(TOOLS_DIR, "rm_make_text_notebook.py")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import rm_make_text_notebook as mtn  # noqa: E402
from rm_extract_text import extract_typed_text  # noqa: E402


def _content_of(rmdoc: Path) -> tuple[dict, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="rm_nb_"))
    with zipfile.ZipFile(rmdoc) as zf:
        zf.extractall(tmp)
    content = next(tmp.glob("*.content"))
    return json.loads(content.read_text(encoding="utf-8")), tmp


class TestTemplates(unittest.TestCase):
    def test_shipped_templates_are_well_formed(self):
        templates = mtn.load_templates()
        self.assertIn("default", templates)
        self.assertIn("plain", templates)
        self.assertEqual(templates["lined"]["page_template"], "P Lines small")
        default = templates["default"]
        self.assertEqual(default["extra_metadata"]["LastPen"], "Finelinerv2")
        self.assertEqual(default["extra_metadata"]["LastFinelinerv2Size"], "2",
                         "the device stores the size as a string")
        self.assertEqual(default["extra_metadata"]["LastFinelinerv2Color"], "Black")
        self.assertEqual(default["margins"], 125)

    def test_local_override_merges_by_name(self):
        tmp = Path(tempfile.mkdtemp(prefix="rm_tpl_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        local = tmp / "rm_templates.local.json"
        local.write_text(json.dumps({
            "default": {"margins": 200},
            "sketch": {"extra_metadata": {"LastPen": "Pencilv2"}},
        }), encoding="utf-8")
        with mock.patch.object(mtn, "TEMPLATES_LOCAL", local):
            templates = mtn.load_templates()
        self.assertEqual(templates["default"]["margins"], 200)
        self.assertEqual(templates["default"]["extra_metadata"]["LastPen"], "Finelinerv2",
                         "an override is field-by-field, not a replacement")
        self.assertEqual(templates["sketch"]["extra_metadata"]["LastPen"], "Pencilv2")
        self.assertIsNone(templates["sketch"]["margins"], "unknown names start from plain")

    def test_malformed_local_file_is_an_error_not_a_silent_default(self):
        tmp = Path(tempfile.mkdtemp(prefix="rm_tpl_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        local = tmp / "rm_templates.local.json"
        local.write_text("{not json", encoding="utf-8")
        with mock.patch.object(mtn, "TEMPLATES_LOCAL", local):
            with self.assertRaises(ValueError):
                mtn.load_templates()


class TestBuilder(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rm_nb_build_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _build(self, *extra: str) -> Path:
        out = self.tmp / "nb.rmdoc"
        proc = subprocess.run(
            [sys.executable, str(BUILDER_CLI),
             "--title", "Field notes", "--text", "# Field notes\\n- first",
             "--out", str(out), *extra],
            capture_output=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=120, cwd=str(TOOLS_DIR))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(out.is_file())
        return out

    def test_default_template_lands_in_content(self):
        content, extracted = self._content_of(self._build("--template", "default"))
        self.addCleanup(shutil.rmtree, extracted, True)
        self.assertEqual(content["margins"], 125)
        self.assertEqual(content["extraMetadata"]["LastPen"], "Finelinerv2")
        self.assertEqual(content["extraMetadata"]["LastFinelinerv2Size"], "2")
        self.assertNotIn("template", content["cPages"]["pages"][0],
                         "page background is unverified and off by default")

    def test_lined_template_records_the_page_background(self):
        content, extracted = self._content_of(self._build("--template", "lined"))
        self.addCleanup(shutil.rmtree, extracted, True)
        page = content["cPages"]["pages"][0]
        self.assertEqual(page["template"], {"timestamp": "1:1", "value": "P Lines small"},
                         "the shape the device itself writes, read off three real notebooks")
        self.assertEqual(content["margins"], 125)

    def test_plain_template_omits_the_margin_key(self):
        content, extracted = self._content_of(self._build("--template", "plain"))
        self.addCleanup(shutil.rmtree, extracted, True)
        self.assertNotIn("margins", content)
        self.assertEqual(content["extraMetadata"], {})

    def test_no_template_keeps_the_legacy_build(self):
        content, extracted = self._content_of(self._build())
        self.addCleanup(shutil.rmtree, extracted, True)
        self.assertEqual(content["margins"], 180)
        self.assertEqual(content["extraMetadata"], {})

    def test_heading_round_trips_as_typed_text(self):
        _, extracted = self._content_of(self._build("--template", "default"))
        self.addCleanup(shutil.rmtree, extracted, True)
        pages = extract_typed_text(extracted)
        self.assertEqual(len(pages), 1)
        paragraphs = pages[0]["paragraphs"]
        self.assertEqual(paragraphs[0]["style"], "heading")
        self.assertEqual(paragraphs[0]["text"].rstrip("\n"), "Field notes")

    def test_unknown_template_exits_2(self):
        proc = subprocess.run(
            [sys.executable, str(BUILDER_CLI),
             "--title", "x", "--text", "x", "--out", str(self.tmp / "x.rmdoc"),
             "--template", "nonesuch"],
            capture_output=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=120, cwd=str(TOOLS_DIR))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("nonesuch", proc.stderr)

    _content_of = staticmethod(_content_of)


class TestTool(unittest.TestCase):
    """roundtrip.new_notebook: validation, build, push hand-off."""

    def _fake_push(self, local: Path, project, title):
        self.pushed = (local, project, title)
        return {"ok": True, "data": {"device_path": f"/{project}/{title}"},
                "warnings": [], "error": None, "log_tail": []}

    def test_builds_then_pushes_with_template_fields(self):
        with mock.patch.object(roundtrip, "push_local_file", self._fake_push):
            result = roundtrip.new_notebook("Lab book", "Thesis", "default", "- first line")
        self.assertTrue(result["ok"], result)
        data = result["data"]
        self.assertEqual(data["template"], "default")
        self.assertEqual(data["template_fields"]["margins"], 125)
        self.assertTrue(data["template_fields"]["heading"])
        local, project, title = self.pushed
        self.assertEqual((project, title), ("Thesis", "Lab book"))
        self.assertTrue(local.name.endswith(".rmdoc"))
        content, extracted = _content_of(local)
        self.addCleanup(shutil.rmtree, extracted, True)
        self.assertEqual(content["extraMetadata"]["LastFinelinerv2Color"], "Black")
        pages = extract_typed_text(extracted)
        texts = [p["text"].rstrip("\n") for p in pages[0]["paragraphs"]]
        self.assertEqual(texts[0], "Lab book")
        self.assertEqual(pages[0]["paragraphs"][1]["style"], "bullet")

    def test_unknown_template_is_a_config_error(self):
        with mock.patch.object(roundtrip, "push_local_file", self._fake_push):
            result = roundtrip.new_notebook("Lab book", "Thesis", "nonesuch", None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["system"], "config")
        self.assertIn("plain", result["error"]["remedy"])

    def test_empty_title_is_refused_before_any_build(self):
        with mock.patch.object(roundtrip, "push_local_file", self._fake_push):
            result = roundtrip.new_notebook("   ", "Thesis", "default", None)
        self.assertFalse(result["ok"])
        self.assertFalse(hasattr(self, "pushed"))


if __name__ == "__main__":
    unittest.main()
