"""rm_pull_project is the loop: get, typed text, highlights, render, hand-off.

Added 2026-09-10 after a newcomer test of the public tree found that the
pull returned ok while three of its four steps had silently failed against
scripts the build does not ship. The loop now reports one status line per
step, skips what a build cannot do (rather than failing it), skips what a
native notebook has no basis for (no PDF, no text layer), always reads typed
text, and hands the page images to the calling agent when no metered vision
backend is installed and keyed.

Everything here is offline: the device fetch and every substrate script are
replaced by fakes, so the test exercises the loop's decisions, not the
scripts. The synthetic bundle comes from gen_neutral_fixtures (no real
handwriting). Runs in both the embedded tree and the public one.
"""

from __future__ import annotations

import io
import json
import os
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
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
TOOLS_DIR = next((d for d in (RM_MCP_DIR / "tools", RM_MCP_DIR.parent / "tools")
                  if (d / "rm_render_page.py").is_file()), RM_MCP_DIR / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import gen_neutral_fixtures as gen  # noqa: E402
from rm_mcp import roundtrip  # noqa: E402

DOC_UUID = "40000000-0000-4000-8000-000000000001"
PAGE_UUID = "40000000-0000-4000-8000-0000000000a1"
PROJECT_DIR = "/Demo"


def _bundle_zip(tmp: Path, with_pdf: bool) -> Path:
    """A synthetic .rmdoc: one native stroke page, optionally PDF-backed."""
    src = tmp / "src"
    rm_bytes = gen.build_stroke_page([[(0.0, 100.0, 200), (50.0, 100.0, 200)]])
    pdf_bytes = gen.build_lorem_pdf(["Lorem ipsum dolor sit amet"]) if with_pdf else None
    gen._write_rmdoc_dir(src, DOC_UUID, PAGE_UUID, rm_bytes, pdf_bytes, "Brief")
    bundle = tmp / "Brief.rmdoc"
    with zipfile.ZipFile(bundle, "w") as zf:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(src).as_posix())
    return bundle


class FakeScripts:
    """Stand-in for roundtrip.run_script keyed by script name."""

    def __init__(self, *, flatten_rc: int = 127, text_rc: int = 0,
                 ink: bool = True) -> None:
        self.flatten_rc = flatten_rc
        self.text_rc = text_rc
        self.ink = ink
        self.calls: list[str] = []

    @staticmethod
    def _proc(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=["fake"], returncode=rc,
                                           stdout=stdout, stderr=stderr)

    def __call__(self, script: str, args: list[str], tool: str,
                 timeout: int | None = None) -> subprocess.CompletedProcess:
        self.calls.append(script)
        if script == "rm_extract_text.py":
            if self.text_rc != 0:
                return self._proc(self.text_rc, stderr="boom")
            out = Path(args[args.index("--out") + 1])
            out.write_text(json.dumps({"doc_uuid": DOC_UUID, "pages": [
                {"pdf_page": 1, "text": "# Title\nhello",
                 "paragraphs": [{"text": "Title", "style": "heading"},
                                {"text": "hello", "style": "plain"}]}],
                "integrity": {}}), encoding="utf-8")
            return self._proc(0)
        if script == "rm_flatten.py":
            if self.flatten_rc == 0:
                Path(args[args.index("--out") + 1]).write_bytes(b"%PDF-1.4\n")
            return self._proc(self.flatten_rc,
                              stderr="rm_flatten.py is not available in this build"
                              if self.flatten_rc == 127 else "")
        if script == "rm_extract_highlights.py":
            return self._proc(0, stdout="[]")
        if script == "rm_render_page.py":
            out = Path(args[args.index("--out") + 1])
            out.mkdir(parents=True, exist_ok=True)
            if self.ink:
                (out / "page_001.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            return self._proc(0)
        if script in ("rm_interpret.py", "rm_interpret_gemini.py"):
            return self._proc(127, stderr=f"{script} is not available in this build")
        raise AssertionError(f"unexpected script {script}")


class LoopCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rm_loop_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def pull(self, *, with_pdf: bool, fakes: FakeScripts, **kw) -> tuple[dict, FakeScripts]:
        bundle = _bundle_zip(self.tmp, with_pdf)

        def fake_get(device_path: str, dest: Path) -> Path:
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / bundle.name
            shutil.copy2(bundle, target)
            return target

        with mock.patch.object(roundtrip, "canonical_project_dir",
                               return_value=(PROJECT_DIR, [])), \
                mock.patch.object(roundtrip.device, "get", side_effect=fake_get), \
                mock.patch.object(roundtrip, "run_script", fakes):
            result = roundtrip.pull_project_doc(
                "Brief", "Demo", kw.get("flatten", True), kw.get("highlights", True),
                kw.get("interpret", True), kw.get("backend"), None, False, "analysis")
        return result, fakes


class TestNativeNotebook(LoopCase):
    def test_pdf_only_steps_are_skipped_with_a_reason(self):
        result, fakes = self.pull(with_pdf=False, fakes=FakeScripts())
        self.assertTrue(result["ok"], result)
        status = result["data"]["step_status"]
        self.assertEqual(result["data"]["document_kind"], "notebook")
        self.assertTrue(status["flatten"].startswith("skipped: native notebook"), status)
        self.assertTrue(status["highlights"].startswith("skipped: native notebook"), status)
        self.assertNotIn("rm_flatten.py", fakes.calls)
        self.assertNotIn("rm_extract_highlights.py", fakes.calls)

    def test_typed_text_is_always_read(self):
        result, _ = self.pull(with_pdf=False, fakes=FakeScripts())
        data = result["data"]
        self.assertEqual(data["typed_text"][0]["paragraphs"][0]["text"], "Title")
        self.assertTrue(data["step_status"]["typed_text"].startswith("ok"))

    def test_ink_is_handed_to_the_calling_agent_without_a_key(self):
        result, fakes = self.pull(with_pdf=False, fakes=FakeScripts())
        data = result["data"]
        self.assertEqual(data["step_status"]["interpret"], "by calling agent")
        self.assertTrue(data["agent_reads_pages"])
        self.assertIn("NOTES", data["guidance"])
        self.assertEqual(len(data["pngs"]), 1)
        self.assertNotIn("rm_interpret.py", fakes.calls)
        self.assertEqual([w for w in result["warnings"] if w["code"] == "step_failed"], [])


class TestPdfDocument(LoopCase):
    def test_unshipped_flatten_is_a_skip_not_a_failure(self):
        result, fakes = self.pull(with_pdf=True, fakes=FakeScripts(flatten_rc=127))
        data = result["data"]
        self.assertEqual(data["document_kind"], "pdf")
        self.assertEqual(data["step_status"]["flatten"], "skipped: not available in this build")
        self.assertNotIn("flatten_error", data)
        self.assertTrue(data["step_status"]["highlights"].startswith("ok (0 record"))
        self.assertEqual(data["highlights"], [])
        self.assertEqual([w for w in result["warnings"] if w["code"] == "step_failed"], [])

    def test_shipped_flatten_produces_the_annotated_pdf(self):
        result, _ = self.pull(with_pdf=True, fakes=FakeScripts(flatten_rc=0))
        data = result["data"]
        self.assertEqual(data["step_status"]["flatten"], "ok")
        self.assertTrue(data["annotated_pdf"].endswith("Brief.flat.pdf"))

    def test_a_failing_step_is_a_warning_and_a_legacy_key(self):
        result, _ = self.pull(with_pdf=True, fakes=FakeScripts(text_rc=1))
        data = result["data"]
        self.assertTrue(result["ok"])
        self.assertTrue(data["step_status"]["typed_text"].startswith("failed: boom"))
        self.assertEqual(data["typed_text_error"], "boom")
        codes = [w["code"] for w in result["warnings"]]
        self.assertIn("step_failed", codes)

    def test_no_ink_skips_interpretation_loudly(self):
        result, _ = self.pull(with_pdf=True, fakes=FakeScripts(ink=False))
        data = result["data"]
        self.assertTrue(data["no_ink"])
        self.assertEqual(data["step_status"]["interpret"], "skipped: no ink pages")
        self.assertIn("zero_ink", [w["code"] for w in result["warnings"]])

    def test_interpret_off_skips_the_render(self):
        result, fakes = self.pull(with_pdf=True, fakes=FakeScripts(), interpret=False)
        self.assertEqual(result["data"]["step_status"]["render"], "skipped: interpret=False")
        self.assertNotIn("rm_render_page.py", fakes.calls)


class TestImageToPdf(unittest.TestCase):
    """rm_push_image's converter: Pillow, no PyMuPDF, device-native page."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rm_img_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _page_size(self, pdf: Path) -> tuple[int, float, float]:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(pdf))
        try:
            page = doc[0]
            return len(doc), page.get_width(), page.get_height()
        finally:
            doc.close()

    def test_transparent_png_lands_on_one_device_page(self):
        from PIL import Image
        src = self.tmp / "sketch.png"
        img = Image.new("RGBA", (300, 100), (0, 0, 0, 0))
        img.putpixel((10, 10), (0, 0, 0, 255))
        img.save(src)
        out = self.tmp / "sketch.pdf"
        roundtrip.convert_image_to_pdf(src, out)
        n, w, h = self._page_size(out)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(w, roundtrip.rm_config.RM_PAGE_W_PT, delta=1.0)
        self.assertAlmostEqual(h, roundtrip.rm_config.RM_PAGE_H_PT, delta=1.0)

    def test_huge_image_is_capped_not_exploded(self):
        from PIL import Image
        src = self.tmp / "scan.jpg"
        Image.new("RGB", (5000, 5000), "white").save(src, quality=50)
        out = self.tmp / "scan.pdf"
        roundtrip.convert_image_to_pdf(src, out)
        n, w, h = self._page_size(out)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(w, roundtrip.rm_config.RM_PAGE_W_PT, delta=1.0)
        self.assertLess(out.stat().st_size, 5 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
