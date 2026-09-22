#!/usr/bin/env python3
"""
rm_render_content.py -- turn Claude-generated content into a local file ready
to push to the reMarkable. No device interaction here at all (no rmapi calls,
no RM_ROOT) -- purely content-in, local-file-out. The push step is a separate,
already-solved concern (rm-mcp's rm_push_file/rm_push_pdf/rm_push_dir, or
tools/rm_push*.py) that takes an existing local file.

Four modes, chosen to be a small, general primitive set rather than an
ever-growing list of "X-to-PDF" converters -- see ForClaude/DIAGRAM_DOC_GEN_TOOLS.md
for the reasoning: SVG is the universal escape hatch. Claude generates SVG
directly (or via RDKit/D2/Penrose/matplotlib/mermaid-render) and hands the
finished markup to --mode svg; this tool stays dumb about *how* the SVG was
produced.

    --mode markdown   Markdown text -> multi-page device-native PDF (468 x 624 pt;
                       python-markdown -> HTML
                       -> fitz.Story, auto-paginated). Supersedes the old
                       headings/bullets-only render_markdown_pdf() in
                       rm_push_project.py -- full markdown (tables, code blocks,
                       nested lists, bold/italic) for free via the HTML bridge.
    --mode html       Raw HTML -> multi-page device-native PDF (fitz.Story,
                       auto-paginated).
                       Covers anything markdown can't express cleanly (tables,
                       custom layout).

The page those two lay out on is DEFAULT_TEMPLATE, and --template-file
overrides any part of it. That indirection exists because the geometry used to
be five module constants: correct, but invisible to a caller and unreachable
by a user, so an agent asked for "a PDF for a reMarkable" still reached for A4.
rm-mcp's rm_create passes a template read from the user's CREATION.md; the
default here is what it falls back to, and the two must not drift -- which is
why rm_mcp/creation.py generates that file FROM this dict rather than
restating the numbers.
    --mode svg        Finished SVG markup -> single-page vector PDF (fitz native
                       SVG->PDF conversion -- true vector, not rasterised). This
                       is where mermaid/D2/Penrose/RDKit/matplotlib output lands
                       once Claude has rendered it to SVG.
    --mode native     Markdown-ish text -> Type-Folio-editable .rmdoc (thin
                       delegation to the existing, already-complete
                       rm_make_text_notebook.py -- not reimplemented here).

All PDF modes use PyMuPDF (fitz) -- no new external tools (no weasyprint, no
Node/Chromium, no cairosvg) needed; MuPDF's bundled Story/HTML engine and
native SVG reader cover both cases.

PyMuPDF isolation -- READ BEFORE EDITING
----------------------------------------
Nothing permissive replaces MuPDF here: pypdfium2 can rasterise but cannot DRAW
into a PDF at all (ForClaude/PYPDFIUM2_EVAL.md). PyMuPDF is AGPL-3.0, so it is
never imported into this module. Every fitz call happens inside
_FITZ_RENDER_SNIPPET, executed in a CHILD INTERPRETER -- exactly the isolation
rm-mcp/rm_mcp/roundtrip.py already uses for image->PDF conversion. The
dependency stays at arm's length and enters neither this process nor the MCP
server's. Keep it that way: a module-level `import fitz` here would make this
file a linked derivative work and break the permissive licence of the shipped
package.

fitz is therefore an OPTIONAL runtime dependency. --mode native needs no PDF
engine and works without it; the PDF modes fail with an actionable install
message.

Usage:
    python tools/rm_render_content.py --mode markdown --text "# Title\n\nBody" --out out.pdf
    python tools/rm_render_content.py --mode markdown --input-file brief.md --title "Brief" --out out.pdf
    python tools/rm_render_content.py --mode html --input-file page.html --out out.pdf
    python tools/rm_render_content.py --mode svg --input-file scheme.svg --out out.pdf
    python tools/rm_render_content.py --mode native --text "# Notes\n- one\n- two" --title "Notes" --out out.rmdoc
    python tools/rm_render_content.py --mode markdown --input-file brief.md --template-file wide.json --out out.pdf
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown as md_lib

sys.path.insert(0, str(Path(__file__).parent))

from rm_config import RM_PAGE_W_PT, RM_PAGE_H_PT  # noqa: E402

# Runaway-content guard, not a real document length.
_MAX_PAGES = 2000

# How long the child render may take before we give up on it.
_RENDER_TIMEOUT_S = 180

# Base readability CSS. Content's own <style> blocks still win where more
# specific -- this only sets sane defaults for an e-ink reading surface
# (larger body text, generous line-height; e-ink has no benefit from tight
# print-style leading).
_DEFAULT_CSS = """
body { font-family: Helvetica, Arial, sans-serif; font-size: 11pt; line-height: 1.45; }
h1 { font-size: 16pt; margin-bottom: 6pt; }
h2 { font-size: 13pt; margin-top: 12pt; margin-bottom: 4pt; }
h3 { font-size: 11.5pt; margin-top: 10pt; margin-bottom: 3pt; }
code, pre { font-family: monospace; font-size: 9.5pt; }
table { border-collapse: collapse; }
td, th { border: 0.5pt solid #999; padding: 3pt 6pt; }
"""

# The page an authored document is laid out on, as one editable object.
#
# It exists because these numbers used to be five module constants nobody
# outside this file could see, let alone change: an agent asked to "make a PDF
# for the reMarkable" had to guess the geometry, and guessing gives you A4,
# which letterboxes on a 3:4 screen and throws away margin on every side. The
# template is the answer to "what does a page that FITS look like", written
# down once, in a shape a caller can pass and a user can edit.
#
# Geometry, and why these numbers:
#   page_w_pt/page_h_pt  468 x 624 (rm_config.RM_PAGE_W_PT/H_PT) is exactly the
#       RM2's 1404 x 1872 px divided by three, so the page fills the screen with
#       no bars. Every pusher here used A4 until 2026-08-18.
#   margin_left/right    Split, not one margin_x, so a template can leave an
#       asymmetric gutter to write in -- the move a generic PDF generator never
#       makes, on a device whose whole point is the pen.
#   margin_top/bot       Top and bottom are separate for the same reason.
#
# Callers pass a partial dict; resolve_template() fills the rest. Nothing here
# is frozen -- unlike PDF_RM_SCALE, which is a measured property of the device,
# these are taste, and the whole design is that they get overridden.
DEFAULT_TEMPLATE: dict = {
    "name": "default",
    "page_w_pt": RM_PAGE_W_PT,
    "page_h_pt": RM_PAGE_H_PT,
    "margin_left": 48.0,
    "margin_right": 48.0,
    "margin_top": 60.0,
    "margin_bot": 60.0,
    "css": _DEFAULT_CSS,
}

_MARGIN_KEYS = ("margin_left", "margin_right", "margin_top", "margin_bot")
_SIZE_KEYS = ("page_w_pt", "page_h_pt")

# Smallest text frame worth rendering into, either axis. Roughly one line of
# 11pt type -- below this the render is certainly not what the caller meant.
_MIN_TEXT_PT = 36.0


class TemplateError(ValueError):
    """A template dict is malformed or would produce an unusable page."""


def resolve_template(template: dict | None) -> dict:
    """Fill a partial template from DEFAULT_TEMPLATE and validate the result.

    Validation is here rather than at the MCP boundary because this is the
    file that knows what the numbers mean, and because the CLI is a caller
    too. A template that leaves no room for text is rejected with the
    arithmetic in the message -- a zero-width text frame otherwise surfaces
    as fitz quietly emitting the max-page guard, which reads like runaway
    content rather than a bad margin.

    `margin_x` is accepted as a shorthand that sets both side margins, so a
    template written before the split still resolves.
    """
    merged = dict(DEFAULT_TEMPLATE)
    if template:
        if not isinstance(template, dict):
            raise TemplateError(f"template must be a dict, got {type(template).__name__}")
        extra = dict(template)
        if "margin_x" in extra:
            side = extra.pop("margin_x")
            extra.setdefault("margin_left", side)
            extra.setdefault("margin_right", side)
        unknown = sorted(set(extra) - set(DEFAULT_TEMPLATE))
        if unknown:
            raise TemplateError(
                f"unknown template key(s): {', '.join(unknown)}. "
                f"Known keys: {', '.join(sorted(DEFAULT_TEMPLATE))} (plus margin_x)")
        merged.update(extra)

    for key in _SIZE_KEYS + _MARGIN_KEYS:
        try:
            merged[key] = float(merged[key])
        except (TypeError, ValueError) as exc:
            raise TemplateError(f"{key} must be a number, got {merged[key]!r}") from exc
    if not isinstance(merged.get("css", ""), str):
        raise TemplateError("css must be a string")
    merged["name"] = str(merged.get("name") or "default")

    for key in _SIZE_KEYS:
        if merged[key] <= 0:
            raise TemplateError(f"{key} must be positive, got {merged[key]}")
    for key in _MARGIN_KEYS:
        if merged[key] < 0:
            raise TemplateError(f"{key} must not be negative, got {merged[key]}")

    text_w = merged["page_w_pt"] - merged["margin_left"] - merged["margin_right"]
    text_h = merged["page_h_pt"] - merged["margin_top"] - merged["margin_bot"]
    if text_w < _MIN_TEXT_PT:
        raise TemplateError(
            f"margins leave a {text_w:.0f}pt-wide text frame "
            f"({merged['page_w_pt']:.0f} - {merged['margin_left']:.0f} - "
            f"{merged['margin_right']:.0f}); at least {_MIN_TEXT_PT:.0f}pt is needed")
    if text_h < _MIN_TEXT_PT:
        raise TemplateError(
            f"margins leave a {text_h:.0f}pt-tall text frame "
            f"({merged['page_h_pt']:.0f} - {merged['margin_top']:.0f} - "
            f"{merged['margin_bot']:.0f}); at least {_MIN_TEXT_PT:.0f}pt is needed")
    return merged


# The ONLY place fitz is named. Runs in a child interpreter (see the module
# docstring). Takes one argv -- the path of a JSON job file -- so no content,
# markup or CSS ever has to survive shell/argv quoting. Always prints a single
# JSON line to stdout; a non-zero exit means the child could not start at all,
# most often because PyMuPDF is not installed.
_FITZ_RENDER_SNIPPET = r"""
import json
import sys

import fitz


def run(job):
    dest = job["dest"]
    src_text = open(job["src"], encoding="utf-8").read()

    if job["mode"] == "svg":
        try:
            svg_doc = fitz.open("svg", src_text.encode("utf-8"))
        except Exception as exc:
            return {"ok": False, "error": "invalid SVG: %s" % exc}
        open(dest, "wb").write(svg_doc.convert_to_pdf())
        return {"ok": True, "pages": 1}

    story = fitz.Story(html=src_text, user_css=job["user_css"])
    mediabox = fitz.Rect(0, 0, job["page_w"], job["page_h"])
    where = fitz.Rect(job["margin_left"], job["margin_top"],
                      job["page_w"] - job["margin_right"],
                      job["page_h"] - job["margin_bot"])
    writer = fitz.DocumentWriter(dest)
    pages = 0
    more = 1
    try:
        while more:
            device = writer.begin_page(mediabox)
            more, _filled = story.place(where)
            story.draw(device)
            writer.end_page()
            pages += 1
            if pages > job["max_pages"]:
                return {"ok": False,
                        "error": "exceeded %d pages -- input is probably malformed"
                                 % job["max_pages"]}
    finally:
        writer.close()
    return {"ok": True, "pages": pages}


try:
    result = run(json.load(open(sys.argv[1], encoding="utf-8")))
except Exception as exc:
    result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
print(json.dumps(result))
"""


class RenderError(RuntimeError):
    """Content could not be rendered (bad markup, empty input, etc.)."""


def _run_fitz_render(mode: str, src_text: str, dest: Path,
                     *, template: dict | None = None) -> int:
    """Render via fitz in a child interpreter. Returns the page count written.

    The AGPL boundary lives here: this spawns a process, it does not import
    fitz. Read the module docstring before changing it.
    """
    tpl = resolve_template(template)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rm_render_") as tmp:
        src_path = Path(tmp) / "content.src"
        src_path.write_text(src_text, encoding="utf-8")
        job_path = Path(tmp) / "job.json"
        job_path.write_text(json.dumps({
            "mode": mode,
            "src": str(src_path),
            "dest": str(dest),
            "user_css": tpl["css"],
            "page_w": tpl["page_w_pt"],
            "page_h": tpl["page_h_pt"],
            "margin_left": tpl["margin_left"],
            "margin_right": tpl["margin_right"],
            "margin_top": tpl["margin_top"],
            "margin_bot": tpl["margin_bot"],
            "max_pages": _MAX_PAGES,
        }), encoding="utf-8")

        try:
            proc = subprocess.run(
                [sys.executable, "-c", _FITZ_RENDER_SNIPPET, str(job_path)],
                capture_output=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=_RENDER_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise RenderError(
                f"render timed out after {exc.timeout}s -- shorten the content "
                f"or split it across pushes") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if "No module named" in stderr and "fitz" in stderr:
            raise RenderError(
                "PyMuPDF is required for markdown/html/svg rendering but is not "
                "installed: pip install pymupdf. It is kept out of this process "
                "deliberately (see the module docstring); --mode native needs no "
                "PDF engine and works without it.")
        raise RenderError(f"render subprocess failed: {stderr[:400]}")

    try:
        result = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RenderError(
            f"render subprocess returned no result: {(proc.stdout or '')[:200]}"
        ) from exc

    if not result.get("ok"):
        raise RenderError(result.get("error", "unknown render failure"))
    return int(result.get("pages", 0))


def html_to_pdf(html: str, dest: Path, *, template: dict | None = None) -> int:
    """Render HTML to a multi-page PDF via fitz.Story (auto-paginating).

    `template` is a partial DEFAULT_TEMPLATE; omit it for the device-native
    page. Returns the page count written. Raises RenderError on empty/invalid
    input, TemplateError on an unusable template.
    """
    if not html.strip():
        raise RenderError("empty HTML content")
    return _run_fitz_render("html", html, dest, template=template)


def markdown_to_pdf(md_text: str, dest: Path, *, title: str | None = None,
                    template: dict | None = None) -> int:
    """Render markdown to a multi-page PDF on the template's page.

    markdown -> HTML -> html_to_pdf. The default page is the device-native
    468 x 624 pt, NOT A4: see DEFAULT_TEMPLATE above for why.
    """
    if not md_text.strip():
        raise RenderError("empty markdown content")
    html_body = md_lib.markdown(
        md_text, extensions=["tables", "fenced_code", "sane_lists"]
    )
    if title and not md_text.lstrip().startswith("#"):
        html_body = f"<h1>{title}</h1>\n{html_body}"
    return html_to_pdf(html_body, dest, template=template)


def svg_to_pdf(svg_text: str, dest: Path) -> None:
    """Convert finished SVG markup to a single-page vector PDF (true vector,
    not rasterised -- fitz opens SVG as a native document type)."""
    if not svg_text.strip():
        raise RenderError("empty SVG content")
    _run_fitz_render("svg", svg_text, dest)


def native_to_rmdoc(md_text: str, dest: Path, *, title: str) -> int:
    """Delegate to the existing, already-complete native-notebook builder.

    Not reimplemented here -- rm_make_text_notebook.py already does exactly
    this (markdown-ish text -> CRDT .rm page -> .rmdoc), local-file-only
    unless its own --push flag is used (which this caller never sets).
    Needs no PDF engine, so this mode works with fitz absent entirely.
    Returns the paragraph count built.
    """
    import uuid

    from rm_make_text_notebook import build_rm_page, build_rmdoc, parse_paragraphs

    paragraphs = parse_paragraphs(md_text)
    if not paragraphs:
        raise RenderError("no paragraphs found in input")
    author_uuid = uuid.uuid4()
    rm_bytes = build_rm_page(paragraphs, author_uuid)
    dest.parent.mkdir(parents=True, exist_ok=True)
    build_rmdoc(title, rm_bytes, str(dest))
    return len(paragraphs)


def _read_content(args: argparse.Namespace) -> str:
    if args.input_file:
        return Path(args.input_file).read_text(encoding="utf-8")
    if args.text is not None:
        return args.text.replace("\\n", "\n")
    raise RenderError("specify --text or --input-file")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", required=True, choices=["markdown", "html", "svg", "native"])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="Inline content (use \\n for newlines)")
    src.add_argument("--input-file", help="Path to a file holding the content")
    ap.add_argument("--out", required=True, help="Output path (.pdf for markdown/html/svg, .rmdoc for native)")
    ap.add_argument("--title", default=None,
                     help="Title -- used as a fallback H1 for markdown mode, required for native mode")
    ap.add_argument("--template-file", default=None,
                     help="JSON file holding a partial template (page size, margins, css). "
                          "Omit for the device-native default. Ignored by --mode native.")
    args = ap.parse_args()

    dest = Path(args.out)
    try:
        template = None
        if args.template_file:
            try:
                template = json.loads(Path(args.template_file).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"[x] could not read --template-file: {exc}", file=sys.stderr)
                return 2
        content = _read_content(args)

        if args.mode == "markdown":
            pages = markdown_to_pdf(content, dest, title=args.title, template=template)
            print(f"wrote {dest} ({pages} page(s))")
        elif args.mode == "html":
            pages = html_to_pdf(content, dest, template=template)
            print(f"wrote {dest} ({pages} page(s))")
        elif args.mode == "svg":
            svg_to_pdf(content, dest)
            print(f"wrote {dest}")
        elif args.mode == "native":
            if not args.title:
                print("--mode native requires --title", file=sys.stderr)
                return 2
            n = native_to_rmdoc(content, dest, title=args.title)
            print(f"wrote {dest} ({n} paragraph(s))")
    except TemplateError as e:
        print(f"[x] bad template: {e}", file=sys.stderr)
        return 2
    except RenderError as e:
        print(f"[x] {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
