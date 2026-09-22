"""Offline tests for CREATION.md and rm_create (creation.py + roundtrip).

Two things are being guarded, and only one of them is "does it work".

The first is the round trip: the file `--init` writes must parse back to the
template the code falls back to. If those two ever disagree, the documented
page and the rendered page are different pages, and nothing says so.

The second is the refusal. A malformed CREATION.md must be an ERROR, never a
silent fall back to the default -- because the failure it would otherwise
produce is the expensive one: a user edits the CSS, the JSON beside it has a
trailing comma, the render comes back cheerfully with the OLD template, and
the only symptom is a document that looks like it always did.

The render leg runs for real (a child rm_render_content.py process); the push
leg is monkeypatched, so nothing here touches rmapi or the cloud.

Run: python -m pytest tests/test_creation.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))
from rm_mcp import config as _config  # noqa: E402
TOOLS_DIR = _config.TOOLS_DIR
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import rm_render_content  # noqa: E402
from rm_mcp import creation, roundtrip  # noqa: E402

# A clean public install has NO PDF engine: pymupdf is AGPL and deliberately
# absent from this package's dependencies. Tests that render for real must
# skip there rather than fail, or the published repo ships a suite that goes
# red on first run and looks broken when it is doing exactly what it says.
# The template and parsing tests below need no engine and always run.
needs_pdf_engine = unittest.skipUnless(
    roundtrip._pdf_engine_available(),
    "no PDF engine (pip install pymupdf) -- expected on a clean install")


class _TempCreation:
    """Point creation.creation_path() at a temp file for one test."""

    def __init__(self, text: str | None) -> None:
        self.text = text

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory(prefix="rm_creation_")
        self.path = Path(self._tmp.name) / creation.CREATION_NAME
        if self.text is not None:
            self.path.write_text(self.text, encoding="utf-8")
        self._patch = mock.patch.object(creation, "creation_path", lambda: self.path)
        self._patch.start()
        return self.path

    def __exit__(self, *exc) -> None:
        self._patch.stop()
        self._tmp.cleanup()


class _PushSpy:
    """Stand-in for push_local_file. Records the PDF it was handed."""

    def __init__(self) -> None:
        self.pushed: Path | None = None

    def __call__(self, local: Path, project: str | None, title: str | None) -> dict:
        self.pushed = local
        return {"ok": True, "data": {"device_path": f"/{project}/{local.stem}"},
                "warnings": [], "error": None}


# -- the template itself ------------------------------------------------------

class TestDefaultTemplate(unittest.TestCase):

    def test_default_page_is_the_device_panel_not_a4(self):
        """468 x 624 pt is 1404 x 1872 px / 3. A4 would letterbox on a 3:4 screen."""
        tpl = rm_render_content.resolve_template(None)
        self.assertEqual((tpl["page_w_pt"], tpl["page_h_pt"]), (468.0, 624.0))
        self.assertAlmostEqual(tpl["page_w_pt"] / tpl["page_h_pt"], 3 / 4, places=6)
        self.assertNotEqual((tpl["page_w_pt"], tpl["page_h_pt"]), (595.0, 842.0))

    def test_partial_template_inherits_the_rest(self):
        tpl = rm_render_content.resolve_template({"margin_left": 120})
        self.assertEqual(tpl["margin_left"], 120.0)
        self.assertEqual(tpl["margin_right"],
                         rm_render_content.DEFAULT_TEMPLATE["margin_right"])
        self.assertEqual(tpl["css"], rm_render_content.DEFAULT_TEMPLATE["css"])

    def test_margin_x_shorthand_sets_both_sides(self):
        tpl = rm_render_content.resolve_template({"margin_x": 70})
        self.assertEqual((tpl["margin_left"], tpl["margin_right"]), (70.0, 70.0))

    def test_explicit_side_wins_over_the_shorthand(self):
        tpl = rm_render_content.resolve_template({"margin_x": 70, "margin_right": 130})
        self.assertEqual((tpl["margin_left"], tpl["margin_right"]), (70.0, 130.0))

    def test_unknown_key_is_refused_by_name(self):
        with self.assertRaises(rm_render_content.TemplateError) as cm:
            rm_render_content.resolve_template({"margin_bottom": 40})
        self.assertIn("margin_bottom", str(cm.exception))

    def test_margins_that_leave_no_text_frame_are_refused(self):
        """A zero-width frame otherwise surfaces as the max-page guard, which
        reads like runaway content rather than a bad margin."""
        with self.assertRaises(rm_render_content.TemplateError) as cm:
            rm_render_content.resolve_template({"margin_left": 240, "margin_right": 240})
        self.assertIn("text frame", str(cm.exception))

    def test_non_numeric_geometry_is_refused(self):
        with self.assertRaises(rm_render_content.TemplateError):
            rm_render_content.resolve_template({"margin_top": "wide"})


# -- CREATION.md round trip ---------------------------------------------------

class TestCreationRoundTrip(unittest.TestCase):

    def test_written_file_parses_back_to_the_built_in_default(self):
        """The doc and the code must describe the same page. This is the whole
        reason creation_text() is generated from DEFAULT_TEMPLATE rather than
        hand-written."""
        with _TempCreation(None) as path:
            creation.write_creation()
            self.assertTrue(path.is_file())
            loaded, provenance = creation.load_template()

        built_in = rm_render_content.resolve_template(None)
        for key in ("page_w_pt", "page_h_pt", "margin_left", "margin_right",
                    "margin_top", "margin_bot", "name"):
            self.assertEqual(loaded[key], built_in[key], f"{key} drifted")
        self.assertEqual(loaded["css"].strip(), built_in["css"].strip())
        self.assertIn(str(creation.CREATION_NAME), provenance)

    def test_absent_file_is_not_an_error(self):
        """A fresh clone that never ran --init gets the default, quietly."""
        with _TempCreation(None):
            tpl, provenance = creation.load_template()
            self.assertEqual(tpl["name"], "default")
            self.assertIn("built-in", provenance)

    def test_write_creation_never_clobbers_an_edited_file(self):
        with _TempCreation("# mine\n\n## Template: default\n") as path:
            dest, written = creation.write_creation()
            self.assertFalse(written)
            self.assertEqual(path.read_text(encoding="utf-8"), "# mine\n\n## Template: default\n")
            _, written_forced = creation.write_creation(overwrite=True)
            self.assertTrue(written_forced)
            self.assertIn("reMarkable", path.read_text(encoding="utf-8"))


# -- parsing ------------------------------------------------------------------

_CSS_ONLY = """# notes

## Template: default

```css
body { font-family: Georgia, serif; font-size: 13pt; }
```
"""

_TWO = """## Template: default

```json
{ "margin_left": 40, "margin_right": 40 }
```

## Template: wide

```json
{ "margin_left": 30, "margin_right": 150 }
```

```css
body { font-size: 12pt; }
```
"""


class TestParsing(unittest.TestCase):

    def test_css_only_section_keeps_default_geometry(self):
        with _TempCreation(_CSS_ONLY):
            tpl, _ = creation.load_template()
        self.assertIn("Georgia", tpl["css"])
        self.assertEqual(tpl["page_w_pt"], 468.0)
        self.assertEqual(tpl["margin_left"],
                         rm_render_content.DEFAULT_TEMPLATE["margin_left"])

    def test_json_only_section_keeps_default_css(self):
        with _TempCreation("## Template: default\n\n```json\n{\"margin_top\": 30}\n```\n"):
            tpl, _ = creation.load_template()
        self.assertEqual(tpl["margin_top"], 30.0)
        self.assertEqual(tpl["css"], rm_render_content.DEFAULT_TEMPLATE["css"])

    def test_named_template_is_selectable(self):
        with _TempCreation(_TWO):
            wide, _ = creation.load_template("wide")
            default, _ = creation.load_template()
        self.assertEqual(wide["margin_right"], 150.0)
        self.assertEqual(wide["name"], "wide")
        self.assertEqual(default["margin_right"], 40.0)

    def test_available_templates_lists_every_section(self):
        with _TempCreation(_TWO):
            names, source = creation.available_templates()
        self.assertEqual(names, ["default", "wide"])
        self.assertIn(creation.CREATION_NAME, source)

    def test_prose_outside_the_fences_is_ignored(self):
        text = ("## Template: default\n\nSome prose mentioning {braces} and\n"
                "`margin_left: 999` inline, which must not be read as config.\n\n"
                "```json\n{\"margin_left\": 55}\n```\n")
        with _TempCreation(text):
            tpl, _ = creation.load_template()
        self.assertEqual(tpl["margin_left"], 55.0)


# -- the refusals -------------------------------------------------------------

class TestMalformedIsAnErrorNotAFallback(unittest.TestCase):
    """Each of these must RAISE. A silent DEFAULT_TEMPLATE here is the bug."""

    def test_invalid_json_raises_and_says_the_render_did_not_run(self):
        with _TempCreation('## Template: default\n\n```json\n{"margin_left": 40,}\n```\n'):
            with self.assertRaises(creation.CreationError) as cm:
                creation.load_template()
        self.assertIn("not valid JSON", str(cm.exception))
        self.assertIn("NOT run", str(cm.exception))

    def test_no_heading_raises(self):
        with _TempCreation("# just prose\n\nno template here\n"):
            with self.assertRaises(creation.CreationError):
                creation.load_template()

    def test_unknown_template_name_lists_what_is_there(self):
        with _TempCreation(_TWO):
            with self.assertRaises(creation.CreationError) as cm:
                creation.load_template("narrow")
        msg = str(cm.exception)
        self.assertIn("narrow", msg)
        self.assertIn("default", msg)
        self.assertIn("wide", msg)

    def test_named_template_with_no_file_does_not_silently_use_the_default(self):
        with _TempCreation(None):
            with self.assertRaises(creation.CreationError):
                creation.load_template("wide")

    def test_duplicate_section_names_raise(self):
        with _TempCreation("## Template: default\n\n## Template: default\n"):
            with self.assertRaises(creation.CreationError):
                creation.load_template()

    def test_bad_geometry_in_the_file_names_the_file(self):
        with _TempCreation('## Template: default\n\n```json\n{"margin_left": 300, '
                           '"margin_right": 300}\n```\n'):
            with self.assertRaises(creation.CreationError) as cm:
                creation.load_template()
        self.assertIn(creation.CREATION_NAME, str(cm.exception))

    def test_available_templates_survives_a_file_it_cannot_parse(self):
        """It is called BY the error path, so it must not raise on the same file."""
        with _TempCreation("# broken\n"):
            names, source = creation.available_templates()
        self.assertEqual(names, ["default"])
        self.assertIn("unreadable", source)


# -- rm_create end to end (render real, push mocked) --------------------------

class TestCreateAndPush(unittest.TestCase):

    def test_dry_run_reports_the_template_and_pushes_nothing(self):
        spy = _PushSpy()
        with _TempCreation(None), mock.patch.object(roundtrip, "push_local_file", spy):
            r = roundtrip.create_and_push("", None, None, dry_run=True)
        self.assertTrue(r["ok"], r)
        self.assertIsNone(spy.pushed)
        self.assertEqual(r["data"]["page_pt"], [468.0, 624.0])
        self.assertEqual(r["data"]["text_frame_pt"], [372.0, 504.0])
        self.assertIn("css", r["data"])

    def test_dry_run_needs_no_content(self):
        """Inspecting the house style must not require inventing a document."""
        with _TempCreation(None):
            r = roundtrip.create_and_push("", None, None, dry_run=True)
        self.assertTrue(r["ok"], r)

    def test_native_mode_is_refused_with_a_pointer(self):
        r = roundtrip.create_and_push("# x", None, None, mode="native")
        self.assertFalse(r["ok"])
        self.assertIn("rm_new_notebook", r["error"]["remedy"])

    def test_empty_content_errors_when_not_a_dry_run(self):
        with _TempCreation(None):
            r = roundtrip.create_and_push("   \n ", None, None)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["system"], "config")

    def test_malformed_file_fails_the_call_rather_than_rendering(self):
        spy = _PushSpy()
        with _TempCreation('## Template: default\n\n```json\n{oops}\n```\n'), \
                mock.patch.object(roundtrip, "push_local_file", spy):
            r = roundtrip.create_and_push("# hello", None, "Doc")
        self.assertFalse(r["ok"])
        self.assertIsNone(spy.pushed)

    def test_missing_pdf_engine_is_reported_before_the_render(self):
        """The expected state of a fresh public install -- pymupdf is AGPL and
        deliberately absent. The caller must be told what that costs, not
        handed a subprocess traceback."""
        spy = _PushSpy()
        with _TempCreation(None), \
                mock.patch.object(roundtrip, "_pdf_engine_available", lambda: False), \
                mock.patch.object(roundtrip, "push_local_file", spy):
            r = roundtrip.create_and_push("# hello", "Demo", "Doc")
        self.assertFalse(r["ok"])
        self.assertIsNone(spy.pushed)
        self.assertIn("pip install pymupdf", r["error"]["remedy"])
        self.assertIn("AGPL", r["error"]["remedy"])
        self.assertIn("rm_new_notebook", r["error"]["remedy"])

    def test_dry_run_reports_engine_availability(self):
        """So a caller can find out whether a create is possible at all
        without pushing anything to find out."""
        with _TempCreation(None), \
                mock.patch.object(roundtrip, "_pdf_engine_available", lambda: False):
            r = roundtrip.create_and_push("", None, None, dry_run=True)
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["data"]["pdf_engine"])

    @needs_pdf_engine
    def test_renders_on_the_template_page(self):
        import pypdfium2 as pdfium

        spy = _PushSpy()
        with _TempCreation(None), mock.patch.object(roundtrip, "push_local_file", spy):
            r = roundtrip.create_and_push("# Title\n\nBody text.\n", "Demo", "Doc")
        self.assertTrue(r["ok"], r)
        self.assertIsNotNone(spy.pushed)
        doc = pdfium.PdfDocument(str(spy.pushed))
        self.assertEqual(tuple(round(v) for v in doc[0].get_size()), (468, 624))
        self.assertEqual(r["data"]["template"], "default")

    @needs_pdf_engine
    def test_an_edited_template_changes_the_page(self):
        """The point of the whole design: edit the file, the output differs.
        A6 is used because it is unmistakably not the default."""
        import pypdfium2 as pdfium

        spy = _PushSpy()
        edited = ('## Template: default\n\n```json\n'
                  '{"page_w_pt": 297, "page_h_pt": 420, "margin_left": 30, '
                  '"margin_right": 30, "margin_top": 30, "margin_bot": 30}\n```\n')
        with _TempCreation(edited), mock.patch.object(roundtrip, "push_local_file", spy):
            r = roundtrip.create_and_push("# Title\n\nBody text.\n", "Demo", "Doc")
        self.assertTrue(r["ok"], r)
        doc = pdfium.PdfDocument(str(spy.pushed))
        self.assertEqual(tuple(round(v) for v in doc[0].get_size()), (297, 420))

    @needs_pdf_engine
    def test_asymmetric_margins_reach_the_renderer(self):
        """margin_left/right are split so a template can leave a writing gutter.
        Proved by the text starting further left than a symmetric page would."""
        import pypdfium2 as pdfium

        def first_text_x(pdf: Path) -> float:
            page = pdfium.PdfDocument(str(pdf))[0]
            textpage = page.get_textpage()
            return min(textpage.get_charbox(i)[0]
                       for i in range(textpage.count_chars()))

        spy = _PushSpy()
        wide = ('## Template: default\n\n```json\n'
                '{"margin_left": 30, "margin_right": 160}\n```\n')
        body = "# Title\n\n" + ("Body text that must wrap over several lines. " * 8)
        with _TempCreation(wide), mock.patch.object(roundtrip, "push_local_file", spy):
            self.assertTrue(roundtrip.create_and_push(body, "Demo", "Wide")["ok"])
            narrow_gutter = first_text_x(spy.pushed)
        with _TempCreation(None), mock.patch.object(roundtrip, "push_local_file", spy):
            self.assertTrue(roundtrip.create_and_push(body, "Demo", "Std")["ok"])
            standard = first_text_x(spy.pushed)

        self.assertLess(narrow_gutter, standard - 10,
                        "a 30pt left margin should start well left of the 48pt default")


if __name__ == "__main__":
    unittest.main()
