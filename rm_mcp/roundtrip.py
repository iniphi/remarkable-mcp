"""The Zotero-free project round-trip lane.

Any agent pushes an arbitrary artifact (PDF / generated writing / image) to
the project's device folder with the push tools; the user annotates it on the
device; rm_pull_project brings back the annotated flat PDF, highlight records,
and an optional vision interpretation -- as local files, no Zotero anywhere.
The calling agent routes the artifacts (Notion, session context, re-push).

The shared push helpers live here (not in server.py) so manage.py's
rm_push_dir can reuse them without a server<->manage import cycle. Pushes
case-canonicalize the /Projects/<code> segment against the device (V2-8) and
record themselves in state['projects_pushed'] via rm_config (V2-11) -- a
separate namespace from 'pushed', which rm_pull's dedupe planner owns.

All heavy steps are child processes of the proven substrate scripts
(rm_extract_text / rm_extract_highlights / rm_render_page, plus rm_flatten
and rm_interpret* where a build ships them); the only in-server work is
the rmapi get/put, the stdlib unzip and the Pillow image-to-PDF fit.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, device
from .config import new_out_dir, rm_config, safe_filename_stem
from .envelope import (err_from_exception, err_result, make_warning,
                       ok_result, RmapiAuthError, RmapiThrottledError,
                       REMEDIES, throttled_result)
from .runner import run_script

# Mirrors rm_pull.py's backend dispatch (VISION_BACKENDS / model ids).
#
# DEFAULT IS "claude" ON PURPOSE (ruled 2026-09-06). The calling agent already
# has the page image in hand and reads the handwriting itself, so the default
# path costs the user nothing. The Gemini backends are the opt-in tier for
# anyone who wants a different model or a different destination -- they are
# metered against the caller's own key.
#
# The model ids are pinned ONCE, in rm_config (GEMINI_FLASH_MODEL /
# GEMINI_PRO_MODEL), rather than aliased to *-latest, so a given release
# behaves the same for everyone and a bug report is reproducible. The cost is
# that a human must bump them, and the history says why they live in one
# place: gemini-2.5 was pinned here until 2026-09-06 (the old pin returned
# HTTP 404 for every new user of this repo -- CORRECTED 2026-09-17: that was
# the NEW-PROJECT LOCKOUT, which is real, and NOT a retirement. The undated
# base ids read "No shutdown date announced" on Google's live deprecations
# page; the 2026-10-16 date this comment used to assert is not on it, and
# came from reading the dated preview snapshots' real dates onto the undated
# base ids), and the
# 2026-09-06 bump then pinned two ids that did not exist (gemini-3-flash,
# gemini-3.1-pro), found 2026-09-09 by listing the key's models. If a Gemini
# backend 404s for you, bump the two constants in rm_config or use "claude".
VISION_BACKENDS = ("claude", "gemini-pro", "gemini-flash")
DEFAULT_BACKEND = "claude"

# Returned on data.guidance whenever the ink is left for the calling agent to
# read. This is the loop's last mile: the pages come back as images, the
# agent reads them, tells the user what it found, and routes it.
_AGENT_GUIDANCE = (
    "Read every image in data.pngs yourself (typed text, if any, is already in "
    "data.typed_text and highlights in data.highlights). Separate what you find "
    "into NOTES (content to keep) and DIRECTIVES (instructions addressed to you "
    "or naming a destination), report both to the user, then route them where "
    "this install's CLAUDE.md says they go.")
_GEMINI_MODEL = {"gemini-pro": rm_config.GEMINI_PRO_MODEL,
                 "gemini-flash": rm_config.GEMINI_FLASH_MODEL}

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")



# -- shared push helpers (used by server.py's push tools and manage.rm_push_dir)

def validated_local(path: str, allow: tuple[str, ...]) -> tuple[Path | None, dict | None]:
    """Existence + extension gate for a local file argument."""
    local = Path(path)
    if not local.is_file():
        return None, err_result("config", f"file not found: {path}",
                                "pass an absolute path to an existing file")
    if local.suffix.lower() not in allow:
        return None, err_result("config",
                                f"{local.suffix or '(no extension)'} not allowed",
                                f"allowed: {', '.join(allow)}")
    return local, None


# A base64 payload inflates the bytes by ~4/3, and the wire refuses a body over
# RM_MCP_MAX_BODY_BYTES (16 MiB on the live deployment). Refuse a little under
# that, here, with a remedy -- an HTTP 413 from the transport tells the caller
# nothing about which argument was too big.
MAX_INLINE_PUSH_BYTES = 11 * 1024 * 1024


def materialised_local(content_b64: str, filename: str,
                       allow: tuple[str, ...]) -> tuple[Path | None, dict | None]:
    """Write an inline base64 document to disk and gate it like a local file.

    The counterpart to validated_local, and the reason the push tools are usable
    from a remote caller at all. rm_push_pdf / rm_push_file / rm_push_image /
    rm_push_dir all take a `path`, which resolves on the SERVICE's filesystem --
    so a sandbox has no way to supply one, and four registered tools were dead
    on the wire by construction (the project notes said as much and left
    it there). rm_push_content already proved the fix for generated markup:
    send the content inline. This does the same for bytes.

    The extension gate runs on `filename`, not on sniffed content, because that
    is what the allowlist means and what the device reads.
    """
    import base64
    import binascii

    name = Path(filename).name  # never let a caller write outside the out dir
    if not name or name in (".", ".."):
        return None, err_result("config", f"invalid filename {filename!r}",
                                "pass a plain filename such as 'paper.pdf'")
    if Path(name).suffix.lower() not in allow:
        return None, err_result(
            "config", f"{Path(name).suffix or '(no extension)'} not allowed",
            f"allowed: {', '.join(allow)} -- the extension comes from filename=")
    try:
        raw = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        return None, err_result("config", f"content_b64 is not valid base64: {exc}",
                                "send standard base64 of the file's bytes, "
                                "unwrapped and with no data: URI prefix")
    if not raw:
        return None, err_result("config", "content_b64 decoded to zero bytes",
                                "check the payload was not truncated in transit")
    if len(raw) > MAX_INLINE_PUSH_BYTES:
        return None, err_result(
            "config",
            f"{len(raw) // (1024*1024)}MB exceeds the "
            f"{MAX_INLINE_PUSH_BYTES // (1024*1024)}MB inline limit",
            "split the document, or push it from a machine with the rmapi "
            "binary where path= works")
    local = new_out_dir("inline_push") / name
    local.write_bytes(raw)
    return local, None


def canonical_project_dir(project: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Resolve + device-case-canonicalize /Projects/<code> (V2-8).

    Raises ValueError (unresolvable project) and RmapiAuthError; other
    transport failures fall through with the requested casing.
    """
    code = config.resolve_project(project)
    canon, warnings = device.canonical_child(
        config.PROJECTS_DEVICE_ROOT, code, want_type="folder")
    return config.join_root(config.PROJECTS_DEVICE_ROOT, canon), warnings


def push_local_file(local: Path, project: str | None,
                    title: str | None) -> dict[str, Any]:
    """Shared ensure-then-put for the push tools.

    Case-canonicalizes the project segment (V2-8) and records the push in
    state['projects_pushed'] (V2-11). A push that lands on the device but
    fails to record returns ok:true + a state_record_failed warning -- the
    device action happened, so the result must never read as a failure.
    """
    staged = local
    if title:
        stem = config.safe_filename_stem(title)
        staged = config.new_out_dir("push_staging") / f"{stem}{local.suffix.lower()}"
        shutil.copy2(local, staged)
    try:
        device_dir, warnings = canonical_project_dir(project)
    except ValueError as exc:
        return err_result("config", str(exc), "pass project=<NNN_name>")
    except RmapiAuthError as exc:
        return err_result("rmapi", str(exc), REMEDIES["not_authenticated"])
    try:
        device.mkdir_p(device_dir)
        device.put(staged, device_dir)
    except RmapiAuthError as exc:
        return err_result("rmapi", str(exc), REMEDIES["not_authenticated"])
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return err_from_exception(exc, "cloud",
                                 "check device/cloud reachability with "
                                 "rm_health, then retry")
    sha256 = hashlib.sha256(local.read_bytes()).hexdigest()
    device_path = f"{device_dir}/{staged.stem}"
    if staged.suffix.lower() == ".epub":
        # V2-5 verdict (live pass 2026-07-03): epub pushes fine but the
        # pull-side flatten/highlights lane is PDF-backed by design --
        # rm_pull_project returns the raw bundle only.
        warnings = warnings + [make_warning(
            "epub_untested_roundtrip",
            f"{device_path}: .epub pushed, but the pull-side round-trip "
            f"(flatten/highlights) only supports PDF-backed documents; "
            f"pulls return the raw bundle without an annotated PDF")]
    entry = {
        "pushed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sha256": sha256,
        "size": local.stat().st_size,
        "source": str(local),
        "project": device_dir.rsplit("/", 1)[1],
        "via": "rm-mcp",
    }
    try:
        rm_config.record_project_push(device_path, entry)
    except Exception as exc:
        warnings = warnings + [make_warning(
            "state_record_failed",
            f"pushed to {device_path} but could not record it in "
            f".rm_state.json: {exc}",
            data={"device_path": device_path})]
    return ok_result({"device_path": device_path,
                      "device_dir": device_dir,
                      "source": str(local),
                      "size": local.stat().st_size,
                      "sha256": sha256},
                     warnings=warnings)


# Largest raster the page is written at. The device screen is 226 dpi, so
# 300 keeps every pixel a reader could see while capping the canvas a 6000 px
# scan would otherwise demand (a 468 x 624 pt page at 1000+ dpi is a 180 MB
# bitmap).
_IMAGE_PDF_MAX_DPI = 300.0


def convert_image_to_pdf(image_path: Path, out_pdf: Path) -> None:
    """Fit an image onto one device-native 3:4 portrait page and save as PDF.

    Pillow writes the PDF directly. It is a core dependency (MIT-CMU), which is
    what replaced the PyMuPDF child interpreter on 2026-09-10: that snippet
    made rm_push_image a core tool that failed on every public install, since
    PyMuPDF is deliberately not installed there (AGPL).

    The page is RM_PAGE_W_PT x RM_PAGE_H_PT points with a small margin, the
    image scaled to fit and centred. The raster is written at whatever DPI
    makes the image's own pixels fill their scaled box, so nothing is
    resampled below _IMAGE_PDF_MAX_DPI. Transparency is flattened onto white,
    which is what the e-paper would show anyway.
    """
    from PIL import Image

    page_w, page_h = rm_config.RM_PAGE_W_PT, rm_config.RM_PAGE_H_PT
    margin = 24.0
    with Image.open(image_path) as src_img:
        transparent = (src_img.mode in ("RGBA", "LA")
                       or (src_img.mode == "P" and "transparency" in src_img.info))
        if transparent:
            rgba = src_img.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = src_img.convert("RGB")
    iw, ih = image.size
    if iw == 0 or ih == 0:
        raise RuntimeError(f"image->PDF conversion failed: {image_path} has no pixels")

    pt_per_px = min((page_w - 2 * margin) / iw, (page_h - 2 * margin) / ih)
    dpi = 72.0 / pt_per_px
    if dpi > _IMAGE_PDF_MAX_DPI:
        shrink = _IMAGE_PDF_MAX_DPI / dpi
        image = image.resize((max(1, round(iw * shrink)), max(1, round(ih * shrink))),
                             Image.LANCZOS)
        iw, ih = image.size
        dpi = _IMAGE_PDF_MAX_DPI
    canvas = Image.new("RGB", (round(page_w / 72.0 * dpi), round(page_h / 72.0 * dpi)),
                       "white")
    canvas.paste(image, ((canvas.width - iw) // 2, (canvas.height - ih) // 2))
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_pdf, "PDF", resolution=dpi)
    if not out_pdf.is_file():
        raise RuntimeError(f"image->PDF conversion failed: {out_pdf} was not written")


_CONTENT_MODES = ("markdown", "html", "svg", "native")


def new_notebook(title: str, project: str | None, template: str,
                 body: str | None) -> dict[str, Any]:
    """Create a fresh Type Folio notebook from a template and push it.

    Builds the page through tools/rm_make_text_notebook.py as a child
    process (it prints on success, and stdout is the MCP channel), then
    pushes with push_local_file like every other push. The heading is the
    title typed on page 1; `body` seeds more typed text under it.
    """
    if not title or not title.strip():
        return err_result("config", "a title is required",
                          "pass title=<notebook title>")
    import rm_make_text_notebook as mtn  # shipped in every build; loads no device state
    try:
        templates = mtn.load_templates()
    except ValueError as exc:
        return err_result("config", str(exc),
                          "fix or remove tools/rm_templates.local.json")
    spec = templates.get(template)
    if spec is None:
        return err_result("config", f"unknown template {template!r}",
                          f"use one of {', '.join(sorted(templates))}")

    lines: list[str] = []
    if spec.get("heading", True):
        lines.append(f"# {title.strip()}")
    if body and body.strip():
        lines.append(body.strip())
    if not lines:
        lines.append(title.strip())
    out = new_out_dir("new_notebook")
    stem = safe_filename_stem(title)
    src = out / f"{stem}.md"
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")
    dest = out / f"{stem}.rmdoc"
    args = ["--input", str(src), "--title", title.strip(), "--out", str(dest),
            "--template", template]
    try:
        proc = run_script("rm_make_text_notebook.py", args, "rm_new_notebook")
    except subprocess.TimeoutExpired as exc:
        return err_result("config", f"notebook build timed out after {exc.timeout}s",
                          "retry; the build is local and should take under a second")
    if proc.returncode != 0 or not dest.is_file():
        detail = (proc.stderr.strip() or proc.stdout.strip() or "")[:400]
        return err_result("config", f"notebook build failed: {detail}",
                          "check the title and body are plain text")

    result = push_local_file(dest, project, title)
    if result.get("ok"):
        result["data"]["template"] = template
        result["data"]["template_fields"] = {
            "margins": spec.get("margins"),
            "extra_metadata": dict(spec.get("extra_metadata") or {}),
            "page_template": spec.get("page_template"),
            "heading": bool(spec.get("heading", True)),
        }
        result["data"]["local_rmdoc"] = str(dest)
    return result


def render_and_push_content(mode: str, content: str, project: str | None,
                            title: str | None) -> dict[str, Any]:
    """Render Claude-generated content to a device-ready file, then push it --
    the one-step wrapper over tools/rm_render_content.py + push_local_file, so
    the caller never touches rmapi. markdown|html|svg -> A4 PDF; native ->
    Type-Folio-editable .rmdoc (requires title). Content is written to a file
    and passed via --input-file (never a giant --text arg) so arbitrary markup
    and newlines survive intact.
    """
    if mode not in _CONTENT_MODES:
        return err_result("config", f"unknown mode {mode!r}",
                          f"use one of {', '.join(_CONTENT_MODES)}")
    if not content.strip():
        return err_result("config", "empty content", "pass non-empty content")
    if mode == "native" and not title:
        return err_result("config", "native mode requires a title",
                          "pass title=<notebook title>")

    out = new_out_dir("push_content")
    stem = safe_filename_stem(title) if title else "content"
    src = out / f"{stem}.src"
    src.write_text(content, encoding="utf-8")
    dest = out / (f"{stem}.rmdoc" if mode == "native" else f"{stem}.pdf")

    args = ["--mode", mode, "--input-file", str(src), "--out", str(dest)]
    if title:
        args += ["--title", title]
    try:
        proc = run_script("rm_render_content.py", args, "rm_push_content")
    except subprocess.TimeoutExpired as exc:
        return err_result("config", f"content render timed out after {exc.timeout}s",
                          "shorten the content or split it across pushes")
    if proc.returncode != 0 or not dest.is_file():
        detail = (proc.stderr.strip() or proc.stdout.strip() or "")[:400]
        return err_result("config", f"content render failed: {detail}",
                          "check the content is valid for the chosen mode "
                          "(svg needs finished SVG markup; native needs a title)",
                          tail=proc.stdout.splitlines()[-10:])

    result = push_local_file(dest, project, title)
    if result.get("ok"):
        result["data"]["mode"] = mode
        result["data"]["rendered_from"] = str(src)
    return result


# Modes rm_create accepts. "native" is deliberately absent: a template is a
# page geometry and a stylesheet, and a Type Folio notebook has neither -- it
# is CRDT text the device lays out itself. Offering a template argument that
# silently did nothing for one of four modes would be worse than not offering
# the mode. rm_push_content still takes native.
_CREATE_MODES = ("markdown", "html", "svg")

# What to say when the PDF engine is absent. This is the expected state of a
# fresh public install, not a broken one: pymupdf is AGPL-3.0 and is left out
# of the dependency list on purpose, which is what lets this package be MIT.
# It is never imported here either -- the render happens in a child
# interpreter. Installing it is the user's call to make knowingly, so the
# message says what it costs rather than just naming a pip line.
_NO_PDF_ENGINE_REMEDY = (
    "install the PDF engine: pip install pymupdf. It is left out of this "
    "package's dependencies deliberately -- it is AGPL-3.0, and bundling it "
    "would relicense the install -- so this is a choice to make knowingly. "
    "Without it, rm_create cannot lay out a page; rm_new_notebook (a "
    "handwriting notebook) and rm_push_pdf (a PDF you rendered elsewhere) "
    "both work with no PDF engine at all.")


def _pdf_engine_available() -> bool:
    """Whether the child interpreter could import fitz, without importing it.

    find_spec locates a module on the path and does NOT execute it, so this
    stays on the permissive side of the boundary the whole package is built
    around. Same interpreter as the child render (sys.executable), so the
    answer is the one that matters.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("fitz") is not None
    except (ImportError, ValueError):
        return False


def create_and_push(content: str, project: str | None, title: str | None,
                    template: str | None = None, mode: str = "markdown",
                    dry_run: bool = False) -> dict[str, Any]:
    """Lay content out on the CREATION.md template and push it to the device.

    The difference from render_and_push_content, which this otherwise
    resembles: that one renders on hardcoded geometry no caller can see or
    change, and it is the right primitive when you have already decided
    everything. This one reads the page out of CREATION.md -- so the agent
    does not have to know that a reMarkable page is 468 x 624 pt, and the user
    can change what "a document" looks like by editing one markdown file.

    dry_run returns the resolved template and renders nothing, which is how a
    caller inspects the house style before committing a push.
    """
    from . import creation

    if mode not in _CREATE_MODES:
        return err_result("config", f"unknown mode {mode!r}",
                          f"use one of {', '.join(_CREATE_MODES)} "
                          f"(for a Type Folio notebook use rm_new_notebook, "
                          f"or rm_push_content with mode='native')")

    try:
        resolved, provenance = creation.load_template(template)
    except creation.CreationError as exc:
        names, source = creation.available_templates()
        return err_result("config", str(exc),
                          f"fix {creation.creation_path()}, or delete it to fall "
                          f"back to the built-in default. Available now: "
                          f"{', '.join(names)} (from {source})")

    summary = {
        "template": resolved["name"],
        "template_source": provenance,
        "page_pt": [resolved["page_w_pt"], resolved["page_h_pt"]],
        "margins_pt": {k: resolved[k] for k in
                       ("margin_left", "margin_right", "margin_top", "margin_bot")},
        "text_frame_pt": [
            resolved["page_w_pt"] - resolved["margin_left"] - resolved["margin_right"],
            resolved["page_h_pt"] - resolved["margin_top"] - resolved["margin_bot"],
        ],
        "creation_md": str(creation.creation_path()),
        "creation_md_exists": creation.creation_path().is_file(),
        "pdf_engine": _pdf_engine_available(),
    }

    if dry_run:
        return ok_result({**summary, "dry_run": True, "css": resolved["css"]})

    if not content.strip():
        return err_result("config", "empty content", "pass non-empty content")

    # Checked before the render rather than after, so the answer is "this
    # install cannot do it, here is what that costs" instead of a subprocess
    # traceback the caller has to interpret.
    if not summary["pdf_engine"]:
        return err_result("config", "no PDF engine: PyMuPDF is not installed",
                          _NO_PDF_ENGINE_REMEDY)

    out = new_out_dir("create")
    stem = safe_filename_stem(title) if title else "document"
    src = out / f"{stem}.src"
    src.write_text(content, encoding="utf-8")
    dest = out / f"{stem}.pdf"

    # The template goes to the renderer as a FILE, for the same reason the
    # content does: CSS is full of braces, quotes and newlines, and none of it
    # should have to survive argv quoting on two platforms.
    tpl_file = out / "template.json"
    tpl_file.write_text(json.dumps(resolved, indent=2), encoding="utf-8")

    args = ["--mode", mode, "--input-file", str(src), "--out", str(dest),
            "--template-file", str(tpl_file)]
    if title:
        args += ["--title", title]
    try:
        proc = run_script("rm_render_content.py", args, "rm_create")
    except subprocess.TimeoutExpired as exc:
        return err_result("config", f"render timed out after {exc.timeout}s",
                          "shorten the content or split it across pushes")
    if proc.returncode != 0 or not dest.is_file():
        detail = (proc.stderr.strip() or proc.stdout.strip() or "")[:400]
        remedy = ("check the content is valid for the chosen mode, and "
                  f"that the template in {creation.creation_path()} is sane")
        if "PyMuPDF" in detail or ("No module named" in detail and "fitz" in detail):
            remedy = _NO_PDF_ENGINE_REMEDY
        return err_result("config", f"render failed: {detail}", remedy,
                          tail=proc.stdout.splitlines()[-10:])

    result = push_local_file(dest, project, title)
    if result.get("ok"):
        result["data"].update(summary)
        result["data"]["mode"] = mode
        result["data"]["rendered_from"] = str(src)
    return result


def _extract_bundle(bundle: Path, extracted_dir: Path) -> Path:
    """Unzip an .rmdoc bundle (a zip) into extracted_dir."""
    extracted_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle) as zf:
        zf.extractall(extracted_dir)
    return extracted_dir


def _interpret_cmd(png_dir: Path, backend: str,
                   pages: str | None) -> tuple[str, list[str]]:
    """Script + args for the vision pass, mirroring rm_pull.py:402-418."""
    if backend == "claude":
        args = [str(png_dir)]
        if pages:
            args += ["--pages", pages]
        return "rm_interpret.py", args
    args = [str(png_dir), "--model", _GEMINI_MODEL[backend],
            "--output-suffix", ".interpret.json"]
    if pages:
        args += ["--pages", pages]
    return "rm_interpret_gemini.py", args


def render_doc(device_path: str | None, extracted: str | None,
               profile_name: str | None, pages: str | None,
               dry_run: bool, transparent: bool | None = None) -> dict[str, Any]:
    """Render device pages to PNG under a named profile -- no vision, no spend.

    The human-facing render lane (rm_render), distinct from the analysis render
    baked into pull_project_doc / rm_pull_notebook. Source is either a full
    device path to fetch (e.g. "/Draw/Drawing") or an already-unzipped .rmdoc
    dir. Renders via the proven rm_render_page.py primitive with the profile's
    flag-set, then applies the profile's MCP-side post-processing (crop-to-ink
    for publication). transparent overrides the profile's background (None =
    profile default). Stateless: never touches .rm_state.json.
    """
    import dataclasses

    from . import render_profiles

    try:
        profile = render_profiles.get_profile(profile_name)
    except ValueError as exc:
        return err_result("config", str(exc),
                          "use profile=publication or profile=analysis")
    if transparent is not None:
        profile = dataclasses.replace(
            profile, background="transparent" if transparent else "white")

    if not device_path and not extracted:
        return err_result("config", "no source given",
                          "pass device_path=<full device path> or "
                          "extracted=<unzipped .rmdoc dir>")

    out = new_out_dir("rm_render")
    png_dir = out / "pngs"
    plan = {"profile": profile.name,
            "render_flags": profile.render_flags(),
            "crop_to_ink": profile.crop_to_ink,
            "background": profile.background,
            "source": extracted or device_path,
            "out_dir": str(out)}
    if dry_run:
        return ok_result({"dry_run": True, **plan})

    warnings: list[dict[str, Any]] = []
    if extracted:
        extracted_dir = Path(extracted)
        if not extracted_dir.is_dir():
            return err_result("config", f"not a directory: {extracted}",
                              "pass an unzipped .rmdoc dir (e.g. the "
                              "extracted_dir from rm_pull_project)")
    else:
        try:
            bundle = device.get(device_path, out / "download")
        except RmapiAuthError as exc:
            return err_result("rmapi", str(exc), REMEDIES["not_authenticated"])
        except RmapiThrottledError as exc:
            return throttled_result(exc)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            return err_result(
                "cloud", f"could not fetch {device_path}: {exc}",
                "check the path with rm_list; if it exists but the get "
                "persistently fails: " + REMEDIES["stale_revision"])
        extracted_dir = _extract_bundle(bundle, out / "extracted")

    render_args = [str(extracted_dir), "--out", str(png_dir),
                   *profile.render_flags()]
    if pages:
        render_args += ["--pages", pages]
    proc = run_script("rm_render_page.py", render_args, "rm_pull_project")
    log = proc.stdout.splitlines()[-15:] + proc.stderr.splitlines()[-10:]
    if proc.returncode != 0:
        return err_result("config",
                          "render failed: " + (proc.stderr.strip() or "")[:400],
                          "inspect log_tail", tail=log)

    pngs = sorted(png_dir.glob("*.png"))
    if not pngs:
        # Annotated-only render with nothing annotated (V2-7 zero-ink).
        return ok_result(
            {"no_ink": True, "pngs": [], **plan}, log,
            [make_warning("zero_ink",
                          f"{extracted or device_path}: no ink pages rendered "
                          f"-- nothing annotated on the selected pages")])

    # crop-to-ink is now native to the renderer (--crop in render_flags), so the
    # produced PNGs are already cropped for a crop_to_ink profile.
    cropped = [str(p) for p in pngs] if profile.crop_to_ink else []

    return ok_result({**plan,
                      "pngs": [str(p) for p in pngs],
                      "cropped": cropped,
                      "extracted_dir": str(extracted_dir)},
                     log, warnings)


def check_pull_options(backend: str | None, profile: str | None
                       ) -> tuple[str, Any, dict[str, Any] | None]:
    """Validate the backend and render-profile arguments of a project pull.

    Shared by the synchronous pull_project_doc and the asynchronous job start
    (pull_worker), so the two can never disagree about what a valid request is.
    Returns (backend, render profile, error); error is None when both are valid.
    """
    from . import render_profiles

    backend = backend or DEFAULT_BACKEND
    if backend not in VISION_BACKENDS:
        return backend, None, err_result(
            "config", f"unknown backend {backend!r}",
            f"use one of {', '.join(VISION_BACKENDS)}")
    try:
        rprofile = render_profiles.get_profile(profile or "analysis")
    except ValueError as exc:
        return backend, None, err_result(
            "config", str(exc), "use profile=analysis or profile=publication")
    return backend, rprofile, None


def pull_project_doc(name: str, project: str | None,
                     flatten: bool, highlights: bool, interpret: bool,
                     backend: str | None, pages: str | None,
                     dry_run: bool, profile: str | None = None) -> dict[str, Any]:
    """Fetch /Projects/<code>/<name> and produce local artifacts.

    Stateless and explicit: no .rm_state.json involvement, works on
    zero-annotation documents (the flat PDF then equals the original).

    profile selects the render flags for the interpret pass (default "analysis"
    -- the model-input render); render_profiles is the single source of truth,
    so this and rm_pull_notebook render identically.
    """
    backend, rprofile, option_error = check_pull_options(backend, profile)
    if option_error is not None:
        return option_error
    warnings: list[dict[str, Any]] = []
    try:
        if dry_run:
            # Dry runs never touch the device -- skip the canonical ls.
            code = config.resolve_project(project)
            device_dir = config.join_root(config.PROJECTS_DEVICE_ROOT, code)
        else:
            device_dir, warnings = canonical_project_dir(project)
    except ValueError as exc:
        return err_result("config", str(exc), "pass project=<NNN_name>")
    except RmapiAuthError as exc:
        return err_result("rmapi", str(exc), REMEDIES["not_authenticated"])

    device_path = f"{device_dir}/{name}"
    out = new_out_dir("rm_pull_project")
    plan = {"device_path": device_path,
            "steps": [s for s, on in
                      (("get", True), ("flatten", flatten),
                       ("highlights", highlights),
                       (f"interpret[{backend}]", interpret)) if on],
            "render_profile": rprofile.name,
            "out_dir": str(out)}
    if dry_run:
        return ok_result({"dry_run": True, **plan}, warnings=warnings)

    log: list[str] = []
    try:
        bundle = device.get(device_path, out / "download")
    except RmapiAuthError as exc:
        return err_result("rmapi", str(exc), REMEDIES["not_authenticated"])
    except RmapiThrottledError as exc:
        return throttled_result(exc)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return err_result(
            "cloud", f"could not fetch {device_path}: {exc}",
            "check the name with rm_list; if the document exists but the get "
            "persistently fails: " + REMEDIES["stale_revision"])

    extracted = _extract_bundle(bundle, out / "extracted")
    has_pdf = any(extracted.glob("*.pdf"))
    data: dict[str, Any] = {**plan, "bundle": str(bundle),
                            "extracted_dir": str(extracted),
                            "document_kind": "pdf" if has_pdf else "notebook"}
    # One line per step, so the caller can see what ran, what was skipped and
    # why, and what failed -- instead of inferring it from which keys exist.
    # The legacy *_error keys are kept for callers that already read them.
    status: dict[str, str] = {"get": "ok"}

    def step_failed(step: str, detail: str, legacy_key: str) -> None:
        status[step] = f"failed: {detail}"
        data[legacy_key] = detail
        warnings.append(make_warning(
            "step_failed", f"{device_path}: {step} failed -- {detail}",
            data={"step": step}))

    def step_skipped(step: str, why: str) -> None:
        status[step] = f"skipped: {why}"

    # Typed text (the Type Folio keyboard) is stored as text in the .rm scene,
    # so it comes back losslessly with no vision and no PDF. Always on: it is
    # the cheapest step, and the one a native notebook's content depends on.
    typed_path = out / "typed_text.json"
    proc = run_script("rm_extract_text.py",
                      [str(extracted), "--out", str(typed_path)], "rm_pull_project")
    log += proc.stderr.splitlines()[-5:]
    if proc.returncode == 0 and typed_path.is_file():
        try:
            typed = json.loads(typed_path.read_text(encoding="utf-8"))
            data["typed_text"] = typed.get("pages", [])
            data["typed_text_path"] = str(typed_path)
            status["typed_text"] = f"ok ({len(data['typed_text'])} page(s) with text)"
        except (OSError, json.JSONDecodeError) as exc:
            step_failed("typed_text", f"unreadable output: {exc}", "typed_text_error")
    else:
        step_failed("typed_text", (proc.stderr.strip() or "failed")[:400],
                    "typed_text_error")

    if flatten and not has_pdf:
        step_skipped("flatten", "native notebook, no PDF layer to flatten onto")
    elif flatten:
        flat_pdf = out / f"{safe_filename_stem(Path(name).stem)}.flat.pdf"
        proc = run_script("rm_flatten.py",
                          [str(extracted), "--out", str(flat_pdf), "--quiet"],
                          "rm_flatten")
        log += proc.stdout.splitlines()[-10:] + proc.stderr.splitlines()[-10:]
        if proc.returncode == 0 and flat_pdf.is_file():
            data["annotated_pdf"] = str(flat_pdf)
            status["flatten"] = "ok"
        elif proc.returncode == 127:
            # Not shipped in this build (PyMuPDF, AGPL). The bundle and the
            # page renders carry the same ink; only the composited PDF is
            # missing, so this is a skip, not a failure.
            step_skipped("flatten", "not available in this build")
        else:
            step_failed("flatten", (proc.stderr.strip() or "no output")[:400],
                        "flatten_error")

    if highlights and not has_pdf:
        step_skipped("highlights", "native notebook, no text layer to intersect")
    elif highlights:
        proc = run_script("rm_extract_highlights.py",
                          [str(extracted), "--json"], "rm_get_highlights")
        if proc.returncode == 0:
            try:
                data["highlights"] = json.loads(proc.stdout)
                status["highlights"] = f"ok ({len(data['highlights'])} record(s))"
            except json.JSONDecodeError:
                step_failed("highlights", "non-JSON output", "highlights_error")
        else:
            step_failed("highlights", (proc.stderr.strip() or "failed")[:400],
                        "highlights_error")

    if not interpret:
        step_skipped("render", "interpret=False")
    else:
        png_dir = out / "pngs"
        # render_profiles is the SSOT for the model-input render (analysis by
        # default) -- previously this hard-coded a thinner --supersample 2 only,
        # drifting from rm_pull_notebook's pressure-width render.
        render_args = [str(extracted), "--out", str(png_dir),
                       *rprofile.render_flags()]
        render_args += ["--pages", pages] if pages else ["--all-pages"]
        proc = run_script("rm_render_page.py", render_args, "rm_pull_project")
        log += proc.stdout.splitlines()[-10:] + proc.stderr.splitlines()[-10:]
        pngs = sorted(png_dir.glob("*.png")) if png_dir.is_dir() else []
        if proc.returncode != 0:
            step_failed("render", "render failed: " + (proc.stderr.strip() or "")[:400],
                        "interpret_error")
        elif not pngs:
            # Zero ink pages is not an error (V2-7): a pushed-but-unannotated
            # document simply has nothing to interpret yet.
            data["no_ink"] = True
            data["pngs"] = []
            data["interpretations"] = []
            status["render"] = "ok (no ink)"
            step_skipped("interpret", "no ink pages")
            warnings.append(make_warning(
                "zero_ink",
                f"{device_path}: no ink pages rendered -- the document has "
                f"no annotations yet, so the vision pass was skipped"))
        else:
            status["render"] = f"ok ({len(pngs)} page(s))"
            data["pngs"] = [str(p) for p in pngs]
            data["interpretations"] = []
            script, args = _interpret_cmd(png_dir, backend, pages)
            metered_claude = (backend == "claude"
                              and (config.TOOLS_DIR / script).is_file()
                              and bool(os.environ.get("ANTHROPIC_API_KEY")))
            if backend == "claude" and not metered_claude:
                # The ruled default (2026-09-06): the calling agent has the
                # page images and reads the ink itself, so this costs nothing
                # and needs no key. The metered rm_interpret.py path runs only
                # where it is installed AND keyed, i.e. the desk.
                status["interpret"] = "by calling agent"
                data["agent_reads_pages"] = True
                data["guidance"] = _AGENT_GUIDANCE
            else:
                proc = run_script(script, args, "rm_interpret_page")
                log += proc.stdout.splitlines()[-10:] + proc.stderr.splitlines()[-10:]
                interp_files = sorted(png_dir.glob("*.interpret*.json"))
                data["interpretations"] = [str(p) for p in interp_files]
                if proc.returncode == 127:
                    step_skipped("interpret",
                                 f"{backend} backend not available in this build")
                    data["agent_reads_pages"] = True
                    data["guidance"] = _AGENT_GUIDANCE
                elif proc.returncode != 0 and not interp_files:
                    step_failed("interpret", (proc.stderr.strip() or "failed")[:400],
                                "interpret_error")
                else:
                    status["interpret"] = f"ok ({len(interp_files)} file(s), {backend})"

    data["step_status"] = status
    # The raw .rmdoc bundle stays in out/download -- it is the only lossless
    # record of the annotation state (mirrors the Zotero lane's raw-sibling
    # rule) and the caller may want to re-extract with different options.
    return ok_result(data, log[-40:], warnings)
