#!/usr/bin/env python3
"""
rm_render_page.py -- Render annotated reMarkable pages to PNG.

Takes an extracted .rmdoc directory (the unzipped form, with <uuid>.pdf,
<uuid>.content, and <uuid>/<page-uuid>.rm files) and produces one PNG per page
with strokes overlaid on the source PDF page (or on a blank background for
native notebooks with no underlying PDF).

This is the rendering primitive that feeds rm_interpret.py (Claude vision).

Usage:
    python tools/rm_render_page.py <extracted-rmdoc-dir>
    python tools/rm_render_page.py <extracted-dir> --out <png-dir>
    python tools/rm_render_page.py <extracted-dir> --pages 1,3,5-7
    python tools/rm_render_page.py <extracted-dir> --no-source        # strokes only, blank bg

See ForClaude/REMARKABLE.md (sketch + handwriting interpretation workflow).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable

import rmscene
from PIL import Image, ImageChops, ImageDraw
from rmscene.scene_items import Pen, PenColor


# reMarkable 2 native device resolution (portrait)
RM_WIDTH = 1404
RM_HEIGHT = 1872

# RGBA colour map per PenColor enum value
COLOR_RGBA: dict[int, tuple[int, int, int, int]] = {
    PenColor.BLACK.value:   (20, 20, 20, 255),
    PenColor.GRAY.value:    (130, 130, 130, 255),
    PenColor.WHITE.value:   (255, 255, 255, 255),
    PenColor.YELLOW.value:  (250, 230, 100, 130),   # highlighter, translucent
    PenColor.GREEN.value:   (90, 180, 90, 130),
    PenColor.PINK.value:    (230, 130, 200, 200),
    PenColor.BLUE.value:    (50, 90, 200, 255),
    PenColor.RED.value:     (220, 60, 60, 255),
    PenColor.GRAY_OVERLAP.value: (140, 140, 140, 180),
    PenColor.HIGHLIGHT.value:   (250, 230, 100, 130),  # default highlighter
    PenColor.GREEN_2.value:  (60, 200, 100, 200),
    PenColor.CYAN.value:     (90, 200, 220, 200),
    PenColor.MAGENTA.value:  (220, 90, 200, 200),
    PenColor.YELLOW_2.value: (240, 220, 80, 200),
}
DEFAULT_RGBA = (40, 40, 40, 255)

# Stroke-width hints per pen tool (rough — reMarkable has variable per-point
# pressure-based widths but a uniform fallback is fine for v0).
# Ink-writing tools (fineliner/ballpoint/pencil) kept thin (matching the 0.8pt
# convention used by rm_flatten.py's PDF-points renderer) -- thick strokes were
# found to hurt Gemini's transcription legibility on dense handwriting pages.
PEN_WIDTH_PX: dict[int, float] = {
    Pen.HIGHLIGHTER_1.value: 28,
    Pen.HIGHLIGHTER_2.value: 28,
    Pen.FINELINER_1.value:    0.8,
    Pen.FINELINER_2.value:    0.8,
    Pen.BALLPOINT_1.value:    0.8,
    Pen.BALLPOINT_2.value:    0.8,
    Pen.MARKER_1.value:       6,
    Pen.MARKER_2.value:       6,
    Pen.PENCIL_1.value:       0.8,
    Pen.PENCIL_2.value:       0.8,
    Pen.MECHANICAL_PENCIL_1.value: 0.6,
    Pen.MECHANICAL_PENCIL_2.value: 0.6,
    Pen.PAINTBRUSH_1.value:   5,
    Pen.PAINTBRUSH_2.value:   5,
    Pen.CALIGRAPHY.value:     5,
    Pen.SHADER.value:        12,
}
DEFAULT_PEN_WIDTH = 0.8


def find_doc_uuid(extracted_dir: Path) -> str:
    contents = list(extracted_dir.glob("*.content"))
    if not contents:
        raise FileNotFoundError(f"no *.content file in {extracted_dir}")
    return contents[0].stem


def build_page_map(extracted_dir: Path, doc_uuid: str) -> list[tuple[int, Path | None]]:
    """Return [(pdf_page_idx, rm_path_or_None), ...] in page order."""
    content = json.loads((extracted_dir / f"{doc_uuid}.content").read_text(encoding="utf-8"))
    pages = content.get("cPages", {}).get("pages") or content.get("pages") or []
    rm_dir = extracted_dir / doc_uuid
    out: list[tuple[int, Path | None]] = []
    for idx, p in enumerate(pages):
        # newer format: dict with 'id'; older format: raw UUID string
        if isinstance(p, dict):
            pid = p.get("id")
        elif isinstance(p, str):
            pid = p
        else:
            pid = None
        if not pid:
            out.append((idx, None))
            continue
        rm = rm_dir / f"{pid}.rm"
        out.append((idx, rm if rm.exists() else None))
    return out


def build_template_map(extracted_dir: Path, doc_uuid: str) -> dict[int, str | None]:
    """Return {0-indexed-page: template_name_or_None}."""
    content = json.loads((extracted_dir / f"{doc_uuid}.content").read_text(encoding="utf-8"))
    pages = content.get("cPages", {}).get("pages") or content.get("pages") or []
    out: dict[int, str | None] = {}
    for idx, p in enumerate(pages):
        if not isinstance(p, dict):
            out[idx] = None
            continue
        tmpl = p.get("template")
        if isinstance(tmpl, dict):
            out[idx] = tmpl.get("value")
        elif isinstance(tmpl, str):
            out[idx] = tmpl
        else:
            out[idx] = None
    return out


CANVAS_PADDING = 40  # pixels of margin around extended-canvas content


def parse_rm_blocks(rm_path_or_bytes) -> list:
    """Read an .rm file (path or bytes) into a list of rmscene blocks."""
    import io
    if isinstance(rm_path_or_bytes, (bytes, bytearray)):
        f = io.BytesIO(rm_path_or_bytes)
        return list(rmscene.read_blocks(f))
    with open(rm_path_or_bytes, "rb") as f:
        return list(rmscene.read_blocks(f))


# Thresholds for the rmscene 0.8.0 stroke-merge artifact detector.
# A "severe" connector is a large inter-point jump at full pen pressure inside
# what rmscene treats as a single stroke — the signature of a pen-up lift that
# the parser failed to split into two strokes. The defaults live in rm_config
# (env-overridable, tunable by tools/rm_calibrate_merge.py) so the detector's
# default args and the CLI flags share one source of truth.
from rm_config import (  # noqa: E402
    MERGE_JUMP_THRESHOLD as _MERGE_JUMP_THRESHOLD,
    MERGE_PRESSURE_MIN as _MERGE_PRESSURE_MIN,
)


def detect_stroke_merge(
    blocks: list,
    jump_threshold: float = _MERGE_JUMP_THRESHOLD,
    pressure_min: float = _MERGE_PRESSURE_MIN,
) -> dict:
    """Return connector-artifact metrics for one page's blocks.

    Returns {"has_artifact": bool, "severe_count": int, "max_jump": float}.
    "severe" = inter-point distance > jump_threshold at pressure > pressure_min
    inside a single ink stroke — the rmscene 0.8.0 stroke-merge signature. See
    ForClaude/KNOWN_ISSUE_rmscene_stroke_merge.md.

    jump_threshold / pressure_min default to rm_config.MERGE_JUMP_THRESHOLD /
    MERGE_PRESSURE_MIN (60.0 / 189); callers (the CLI, the calibration sweep)
    override them to retune without touching the frozen defaults. max_jump is
    threshold-independent — it is the raw largest jump seen — so the sweep can
    read it once per page and re-threshold in pure Python.
    """
    import math
    severe = 0
    max_jump = 0.0
    for b in blocks:
        if type(b).__name__ != "SceneLineItemBlock":
            continue
        val = getattr(getattr(b, "item", None), "value", None)
        if val is None:
            continue
        pts = getattr(val, "points", []) or []
        if len(pts) < 2:
            continue
        tool = getattr(val, "tool", None)
        if tool in (Pen.HIGHLIGHTER_1, Pen.HIGHLIGHTER_2):
            continue
        for j in range(1, len(pts)):
            dx = pts[j].x - pts[j - 1].x
            dy = pts[j].y - pts[j - 1].y
            dist = math.sqrt(dx * dx + dy * dy)
            if dist > max_jump:
                max_jump = dist
            if dist > jump_threshold and getattr(pts[j], "pressure", 0) > pressure_min:
                severe += 1
    return {"has_artifact": severe > 0, "severe_count": severe, "max_jump": max_jump}


def stroke_bbox(blocks) -> tuple[float, float, float, float] | None:
    """Return (xmin, xmax, ymin, ymax) in rmscene coords; None if no strokes."""
    xs: list[float] = []
    ys: list[float] = []
    for b in blocks:
        line = getattr(b, "item", None)
        if not (line and getattr(line, "value", None)):
            continue
        for p in getattr(line.value, "points", []) or []:
            xs.append(p.x)
            ys.append(p.y)
    if not xs:
        return None
    return min(xs), max(xs), min(ys), max(ys)


# PDF-backed-document transform constant (rmscene units per PDF point).
# Single source of truth is rm_config.PDF_RM_SCALE, which carries the full
# 2026-05-22 screen-calibration provenance comment. Frozen at 3.16.
from rm_config import PDF_RM_SCALE  # noqa: E402


def detect_scene_scale(
    sb: tuple[float, float, float, float] | None,
    has_pdf: bool = False,
    pdf_w: float | None = None,
    pdf_h: float | None = None,
) -> tuple[float, float, float]:
    """Return (scale, tx, ty) such that rmscene = scale * PDF + (tx, ty).

    For PDF-backed documents the device records strokes in a coord space where
    1 PDF point = ~3.16 rmscene units, the page is centred around rmscene x=0
    (so PDF x=0 maps to rmscene x = -PDF_W * 3.16 / 2), and the page top edge
    is at rmscene y=0 (so PDF y=0 maps to rmscene y=0).

    For native notebooks (no PDF) we keep the original behaviour: rmscene
    coords are device pixels, extended canvas accommodates strokes that go
    beyond the standard 1404x1872 viewport.

    Derived from the Screen Calibration anchored-affine fit; see
    tools/rm_solve_calibration.py.
    """
    if not has_pdf or pdf_w is None:
        return 1.0, 0.0, 0.0
    scale = PDF_RM_SCALE
    tx = -(pdf_w * scale) / 2.0
    ty = 0.0
    return scale, tx, ty


def rm_to_canvas(
    sx: float, sy: float,
    rm_per_canvas_x: float, rm_per_canvas_y: float,
    canvas_x_for_rm_zero: float, canvas_y_for_rm_zero: float,
) -> tuple[float, float]:
    """Map rmscene (sx, sy) -> canvas pixel coords."""
    return (
        sx / rm_per_canvas_x + canvas_x_for_rm_zero,
        sy / rm_per_canvas_y + canvas_y_for_rm_zero,
    )


def compute_canvas_layout(
    src_w: int, src_h: int, sb: tuple[float, float, float, float] | None,
    rm_per_canvas_x: float = 1.0, rm_per_canvas_y: float = 1.0,
    canvas_x_for_rm_zero: float = RM_WIDTH / 2,
    canvas_y_for_rm_zero: float = 0,
) -> tuple[int, int, int, int]:
    """Given source-page dims and a stroke bbox in rmscene coords, return
    (canvas_w, canvas_h, page_x_offset, page_y_offset).

    rm_per_canvas_{x,y} is the rmscene-units-per-canvas-pixel ratio (1.0 = same).
    canvas_{x,y}_for_rm_zero is where rmscene (0,0) maps to in canvas coords."""
    if sb is None:
        return src_w, src_h, 0, 0
    sx_min, sx_max, sy_min, sy_max = sb
    cx_min, cy_min = rm_to_canvas(sx_min, sy_min, rm_per_canvas_x, rm_per_canvas_y,
                                   canvas_x_for_rm_zero, canvas_y_for_rm_zero)
    cx_max, cy_max = rm_to_canvas(sx_max, sy_max, rm_per_canvas_x, rm_per_canvas_y,
                                   canvas_x_for_rm_zero, canvas_y_for_rm_zero)
    left_overflow  = max(0, -cx_min)
    top_overflow   = max(0, -cy_min)
    right_extent   = max(src_w, cx_max)
    bottom_extent  = max(src_h, cy_max)
    canvas_w = int(right_extent  + left_overflow + 2 * CANVAS_PADDING)
    canvas_h = int(bottom_extent + top_overflow  + 2 * CANVAS_PADDING)
    page_x = int(left_overflow + CANVAS_PADDING)
    page_y = int(top_overflow  + CANVAS_PADDING)
    return canvas_w, canvas_h, page_x, page_y


# PDF rasterizer backend. pypdfium2 (permissive-licensed) is the default since
# 2026-07-23, adopted on the eval verdict in
# rm-mcp/tests/fixtures/render/PYPDFIUM2_EVAL.md (differences are text-edge
# antialiasing only); goldens are minted under pdfium.
#
# Since 2026-08-27 it is the ONLY backend here, and this module names no AGPL
# code at all -- which is what lets rm_render and rm_page_image ship on the
# public surface. Page geometry comes from PdfSource (pypdfium2) rather than an
# open fitz.Document, and the AGPL rasterizer that used to live here as
# render_pdf_page_pymupdf now lives in tools/rm_eval_pdfium.py, which is
# deliberately NOT shipped and is the only caller that still needs a
# backend-vs-backend diff. flatten / highlight extraction / push_image are
# separate files and remain PyMuPDF for now.
_PDF_BACKEND = os.environ.get("RM_PDF_BACKEND", "pdfium").strip().lower()


class PdfSource:
    """Minimal page-geometry handle over pypdfium2.

    render_pdf_page and make_canvas previously required an open fitz.Document
    purely for the page count and page dimensions. That single dependency is
    what kept this module -- and so rm_render and rm_page_image -- off the
    public surface, long after pdfium had taken over the actual rasterizing.
    This exposes only what those call sites used: len(), page size, and the
    source path."""

    __slots__ = ("_doc", "name")

    def __init__(self, path) -> None:
        import pypdfium2 as pdfium
        self.name = str(path)
        self._doc = pdfium.PdfDocument(self.name)

    def __len__(self) -> int:
        return len(self._doc)

    def page_size(self, idx: int) -> tuple[float, float]:
        w, h = self._doc[idx].get_size()
        return float(w), float(h)

    def close(self) -> None:
        self._doc.close()

    def __enter__(self) -> "PdfSource":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


def open_pdf(path) -> PdfSource:
    """Open a PDF for the render helpers. pypdfium2-backed, permissively licensed."""
    return PdfSource(path)


def _page_size(pdf, idx: int) -> tuple[float, float]:
    """Page dimensions in points, from a PdfSource or a legacy fitz.Document.

    The fitz branch serves callers that still hold a PyMuPDF handle (rm_browse,
    the backend eval); reading .rect off a page they opened imports nothing."""
    if hasattr(pdf, "page_size"):
        return pdf.page_size(idx)
    rect = pdf[idx].rect
    return float(rect.width), float(rect.height)


def render_pdf_page_pdfium(pdf_path: str, idx: int,
                           target_size: tuple[int, int] | None = None) -> Image.Image:
    """Rasterize a PDF page with pypdfium2 (permissive-licensed), fit-to-width
    (RM_WIDTH px). The eval-only alternative to the PyMuPDF render_pdf_page.

    Strokes are PIL-drawn on top downstream and are rasterizer-independent, so
    substituting this changes only background PDF pixels -- the calibration-
    critical stroke geometry is untouched. target_size, when given, LANCZOS-
    resizes to the PyMuPDF pixmap dimensions so any sub-pixel rounding
    difference between the two rasterizers doesn't shift the canvas layout.
    """
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf_path)
    try:
        page = doc[idx]
        pw = page.get_size()[0]
        scale = RM_WIDTH / pw
        img = page.render(scale=scale).to_pil().convert("RGB")
    finally:
        doc.close()
    if target_size is not None and img.size != target_size:
        img = img.resize(target_size, Image.Resampling.LANCZOS)
    return img.convert("RGBA")


def render_pdf_page(pdf, idx: int) -> Image.Image:
    """Render PDF page at fit-to-width (1404 px). Height = page_aspect * 1404.

    Rasterizer is pypdfium2, and since 2026-08-27 it is the only one here.
    Accepts a PdfSource or a legacy fitz.Document. The target size keeps the
    PyMuPDF pixmap rounding so canvas layout and stroke placement stay identical
    to goldens minted before the swap."""
    if _PDF_BACKEND not in ("", "pdfium"):
        raise RuntimeError(
            f"RM_PDF_BACKEND={_PDF_BACKEND!r} is not available here. This module "
            "ships on the permissive public surface and carries only the pypdfium2 "
            "backend; the AGPL PyMuPDF rasterizer moved to tools/rm_eval_pdfium.py "
            "(not shipped) for the backend diff. Unset RM_PDF_BACKEND or set it "
            "to 'pdfium'.")
    pw, ph = _page_size(pdf, idx)
    scale = RM_WIDTH / pw
    tgt = (round(pw * scale), round(ph * scale))
    return render_pdf_page_pdfium(pdf.name, idx, target_size=tgt)


def blank_canvas(w: int = RM_WIDTH, h: int = RM_HEIGHT,
                 transparent: bool = False) -> Image.Image:
    fill = (255, 255, 255, 0) if transparent else (255, 255, 255, 255)
    return Image.new("RGBA", (w, h), fill)


# Page-template patterns at native device resolution (1404x1872 reference).
# Spacings are device-pixel approximations of what reMarkable's built-in
# templates draw — close enough for visual fidelity when rendering native
# notebooks. Calendar / checklist templates are skipped (too complex; render
# as Blank). For supersampled renders, pass `scale=ss` so the pattern stays
# crisp at the larger resolution.
TEMPLATE_COLOR_RGBA = (180, 180, 180, 255)  # faint grey — matches device template aesthetic


def apply_page_template(canvas: Image.Image, template_name: str | None, scale: int = 1) -> None:
    """Draw a faint background pattern on `canvas` in-place based on the
    reMarkable template name (e.g. 'P Dots S'). No-op for Blank, unrecognised
    templates, calendar templates ('P Week*'), and checklist templates."""
    if not template_name:
        return
    name = template_name.lower()
    if "blank" in name or "week" in name or "checklist" in name:
        return
    draw = ImageDraw.Draw(canvas)
    w, h = canvas.size

    if "dots" in name:
        # P Dots S = small/dense; P Dots large = sparse
        spacing = (28 if "small" in name or " s" in name.replace(" l", "") else 56) * scale
        radius = max(1, scale)
        for x in range(spacing, w, spacing):
            for y in range(spacing, h, spacing):
                draw.ellipse([(x - radius, y - radius), (x + radius, y + radius)],
                             fill=TEMPLATE_COLOR_RGBA)
        return

    if "grid" in name:
        spacing = (28 if "small" in name else 56) * scale
        line_w = max(1, scale)
        for x in range(spacing, w, spacing):
            draw.line([(x, 0), (x, h)], fill=TEMPLATE_COLOR_RGBA, width=line_w)
        for y in range(spacing, h, spacing):
            draw.line([(0, y), (w, y)], fill=TEMPLATE_COLOR_RGBA, width=line_w)
        return

    if "lines" in name or "lined" in name:
        spacing = 56 * scale
        line_w = max(1, scale)
        for y in range(spacing, h, spacing):
            draw.line([(0, y), (w, y)], fill=TEMPLATE_COLOR_RGBA, width=line_w)
        return
    # Unknown template → leave canvas blank.


def make_canvas(
    pdf: "PdfSource | None", idx: int, sb: tuple[float, float, float, float] | None,
    rm_per_canvas_x: float = 1.0, rm_per_canvas_y: float = 1.0,
    canvas_x_for_rm_zero: float = RM_WIDTH / 2,
    canvas_y_for_rm_zero: float = 0,
    transparent: bool = False,
) -> tuple[Image.Image, int, int]:
    """Build canvas sized to contain both the source page and any extended-canvas
    strokes. Returns (canvas, page_x_offset, page_y_offset).

    transparent=True gives a fully-transparent background (for native notebooks
    with no PDF source); a composited PDF page is opaque regardless."""
    if pdf is not None and idx < len(pdf):
        page_img = render_pdf_page(pdf, idx)
        src_w, src_h = page_img.size
    else:
        page_img = None
        src_w, src_h = RM_WIDTH, RM_HEIGHT
    canvas_w, canvas_h, page_x, page_y = compute_canvas_layout(
        src_w, src_h, sb,
        rm_per_canvas_x=rm_per_canvas_x, rm_per_canvas_y=rm_per_canvas_y,
        canvas_x_for_rm_zero=canvas_x_for_rm_zero,
        canvas_y_for_rm_zero=canvas_y_for_rm_zero,
    )
    canvas = blank_canvas(canvas_w, canvas_h, transparent=transparent)
    if page_img is not None:
        canvas.alpha_composite(page_img, dest=(page_x, page_y))
    return canvas, page_x, page_y


# Per-layer visualisation colours used when --color-by-layer is set. Cycles
# if there are more layers than colours. Layer-1 stays close to original ink.
LAYER_COLORS_RGBA: list[tuple[int, int, int, int]] = [
    (20, 20, 20, 255),    # Layer 1 — near-black
    (50, 90, 200, 255),   # Layer 2 — blue
    (200, 60, 60, 255),   # Layer 3 — red
    (60, 160, 80, 255),   # Layer 4 — green
    (200, 130, 30, 255),  # Layer 5 — orange
    (140, 80, 200, 255),  # Layer 6 — purple
]
ROOT_LAYER_LABEL = "(root)"


def build_layer_map(blocks: list) -> dict:
    """Map node_id -> layer label (TreeNodeBlocks). Skip the root '' node."""
    layers = {}
    for b in blocks:
        if type(b).__name__ != "TreeNodeBlock":
            continue
        grp = getattr(b, "group", None)
        if not grp:
            continue
        node_id = getattr(grp, "node_id", None)
        label_obj = getattr(grp, "label", None)
        label = getattr(label_obj, "value", None) if label_obj else None
        if node_id is not None:
            layers[node_id] = label or ROOT_LAYER_LABEL
    return layers


def stroke_layer_label(block, layer_map: dict) -> str:
    """Return the layer label for one SceneLineItemBlock; ROOT_LAYER_LABEL if unknown."""
    parent_id = getattr(block, "parent_id", None)
    return layer_map.get(parent_id, ROOT_LAYER_LABEL)


def layer_summary(blocks: list, layer_map: dict) -> dict[str, int]:
    """Return {layer_label: stroke_count} across all SceneLineItemBlocks with points."""
    counts: dict[str, int] = {}
    for b in blocks:
        if type(b).__name__ != "SceneLineItemBlock":
            continue
        line = getattr(b, "item", None)
        if not (line and getattr(line, "value", None)):
            continue
        if not getattr(line.value, "points", []):
            continue
        label = stroke_layer_label(b, layer_map)
        counts[label] = counts.get(label, 0) + 1
    return counts


def overlay_strokes(
    canvas: Image.Image, blocks: list, page_x: int = 0, page_y: int = 0,
    width_scale: float = 1.0,
    rm_per_canvas_x: float = 1.0, rm_per_canvas_y: float = 1.0,
    canvas_x_for_rm_zero: float = RM_WIDTH / 2,
    canvas_y_for_rm_zero: float = 0,
    layer_filter: set[str] | None = None,
    color_by_layer: bool = False,
    pressure_width: bool = False,
) -> int:
    """Draw rmscene strokes onto canvas. Stroke (sx, sy) maps to canvas:
        cx = sx / rm_per_canvas_x + canvas_x_for_rm_zero + page_x
        cy = sy / rm_per_canvas_y + canvas_y_for_rm_zero + page_y

    Handles three mark classes (in compositing order, bottom up):
    - SceneGlyphItemBlock (snap-to-text highlights) — translucent yellow rects
    - SceneLineItemBlock with HIGHLIGHTER_* tool — translucent polylines
    - SceneLineItemBlock with ink tools — opaque polylines on top

    layer_filter (set of layer labels) restricts which layers get drawn —
      None = all layers. Use ROOT_LAYER_LABEL for strokes parented to root.
    color_by_layer overrides stroke colour with a per-layer palette so we
      can visually distinguish layers in a flat render.
    pressure_width draws each ink segment individually with width scaled by the
      source point's pressure (width * pressure/255, min 1px). This attenuates
      the firmware-level stroke-merge connectors — a pen-lift reposition appears
      as a large spatial jump whose pressure is lower, so per-segment widths
      thin the spurious connector away. Mirrors rm_flatten.draw_strokes_on_page.
      Use for MV/model-input renders; leave False for publication output."""
    layer_map = build_layer_map(blocks) if (layer_filter is not None or color_by_layer) else {}
    layer_color_idx: dict[str, int] = {}  # stable label -> palette index
    glyph_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    highlighter_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ink_layer        = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(glyph_layer)
    h_draw = ImageDraw.Draw(highlighter_layer)
    i_draw = ImageDraw.Draw(ink_layer)

    def _rm_to_canvas(x_rm: float, y_rm: float) -> tuple[float, float]:
        return (x_rm / rm_per_canvas_x + canvas_x_for_rm_zero + page_x,
                y_rm / rm_per_canvas_y + canvas_y_for_rm_zero + page_y)

    # Pass 0: snap-to-text GlyphItems → translucent yellow rectangles.
    # These are placed UNDER all other marks so freehand ink draws cleanly
    # over the highlight blocks (same compositing order as the device shows).
    n_glyph = 0
    for b in blocks:
        if type(b).__name__ != "SceneGlyphItemBlock":
            continue
        item = getattr(b, "item", None)
        gr = getattr(item, "value", None) if item else None
        if gr is None:
            continue
        rects = getattr(gr, "rectangles", None) or []
        if not rects:
            continue
        # Colour from GlyphRange.color_rgba (0..255), default yellow highlighter
        c_rgba = getattr(gr, "color_rgba", None)
        if c_rgba:
            # Force translucent regardless of stored alpha so text shows through
            rgba_fill = (c_rgba[0], c_rgba[1], c_rgba[2], 130)
        else:
            rgba_fill = (250, 230, 100, 130)
        for r in rects:
            x0, y0 = _rm_to_canvas(r.x, r.y)
            x1, y1 = _rm_to_canvas(r.x + r.w, r.y + r.h)
            g_draw.rectangle([(min(x0, x1), min(y0, y1)),
                              (max(x0, x1), max(y0, y1))],
                             fill=rgba_fill)
            n_glyph += 1

    n_strokes = 0
    for b in blocks:
        if type(b).__name__ != "SceneLineItemBlock":
            continue
        line = getattr(b, "item", None)
        if not (line and getattr(line, "value", None)):
            continue
        l = line.value
        tool  = getattr(l, "tool", None)
        color = getattr(l, "color", None)
        pts   = getattr(l, "points", []) or []
        # rmscene 0.8 doesn't decode colour for some newer pen types
        # (PAINTBRUSH_2, CALIGRAPHY, SHADER) — those still have valid tool
        # and points, so we render them with the default ink colour rather
        # than silently dropping them. Same for tool=None edge cases.
        if not pts:
            continue
        if tool is None:
            continue

        # Layer gating + per-layer colour override
        layer_label = stroke_layer_label(b, layer_map) if layer_map else None
        if layer_filter is not None and layer_label not in layer_filter:
            continue

        coords = [
            (p.x / rm_per_canvas_x + canvas_x_for_rm_zero + page_x,
             p.y / rm_per_canvas_y + canvas_y_for_rm_zero + page_y)
            for p in pts
        ]
        if len(coords) < 2:
            cx, cy = coords[0]
            coords = [(cx - 1, cy - 1), (cx + 1, cy + 1)]

        if color_by_layer and layer_label is not None:
            if layer_label not in layer_color_idx:
                layer_color_idx[layer_label] = len(layer_color_idx)
            rgba = LAYER_COLORS_RGBA[layer_color_idx[layer_label] % len(LAYER_COLORS_RGBA)]
        elif color is None:
            # rmscene couldn't decode colour (newer pen types) — default to ink black
            rgba = DEFAULT_RGBA
        else:
            rgba = COLOR_RGBA.get(color.value, DEFAULT_RGBA)
        base_w = PEN_WIDTH_PX.get(tool.value, DEFAULT_PEN_WIDTH)
        # pen widths scale with the rm-to-canvas ratio so strokes look the
        # right thickness relative to text on the rendered page
        avg_rpc = (rm_per_canvas_x + rm_per_canvas_y) / 2.0
        width = max(1, int(round(base_w * width_scale / avg_rpc)))

        # Treat highlighter strokes as overlay even when --color-by-layer
        # overrides the colour, so they composite under ink instead of over.
        is_highlighter = tool in (Pen.HIGHLIGHTER_1, Pen.HIGHLIGHTER_2)
        draw = h_draw if is_highlighter else i_draw
        if pressure_width and not is_highlighter and len(coords) >= 2:
            # Per-segment render with pressure-scaled width. Attenuates the
            # rmscene stroke-merge connector without splitting strokes: the
            # low-pressure pen-lift point thins its outgoing segment away.
            for i in range(len(coords) - 1):
                press = getattr(pts[i], "pressure", 255)
                seg_w = max(1, int(round(width * (press / 255.0))))
                draw.line([coords[i], coords[i + 1]], fill=rgba, width=seg_w, joint="curve")
        else:
            draw.line(coords, fill=rgba, width=width, joint="curve")
        n_strokes += 1

    canvas.alpha_composite(glyph_layer)
    canvas.alpha_composite(highlighter_layer)
    canvas.alpha_composite(ink_layer)
    return n_strokes + n_glyph


def crop_canvas_to_ink(canvas: Image.Image, transparent: bool,
                       margin: int) -> Image.Image:
    """Crop a rendered canvas to its ink bounding box plus a margin.

    The substrate renderer sizes the canvas to the whole device page, which
    leaves large dead margins that are wrong for publication output. This crops
    to the actual marks. Alpha-aware: a transparent render is cropped from its
    alpha channel (so it stays see-through); an opaque render is cropped from
    its non-white bounding box. A blank page is returned unchanged.
    """
    if transparent:
        bbox = canvas.getchannel("A").getbbox()
    else:
        rgb = canvas.convert("RGB")
        bg = Image.new("RGB", rgb.size, (255, 255, 255))
        bbox = ImageChops.difference(rgb, bg).getbbox()
    if bbox is None:
        return canvas
    box = (
        max(0, bbox[0] - margin),
        max(0, bbox[1] - margin),
        min(canvas.width, bbox[2] + margin),
        min(canvas.height, bbox[3] + margin),
    )
    return canvas.crop(box)


def parse_page_spec(spec: str | None, total: int) -> set[int] | None:
    """Parse '1,3,5-7' style page specs → 0-indexed set. None means all."""
    if not spec:
        return None
    out: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            out.update(range(int(a) - 1, int(b)))
        else:
            out.add(int(chunk) - 1)
    return {i for i in out if 0 <= i < total}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("extracted_dir", help="Path to an unzipped .rmdoc directory")
    ap.add_argument("--out", default=None,
                    help="Output PNG dir (default: <extracted_dir>/_pngs)")
    ap.add_argument("--pages", default=None,
                    help="Page selection: '1,3,5-7' (1-indexed). Default: all annotated pages.")
    ap.add_argument("--all-pages", action="store_true",
                    help="Render every page (annotated or not); default is annotated-only")
    ap.add_argument("--no-source", action="store_true",
                    help="Skip PDF source rendering; strokes on blank background")
    ap.add_argument("--width-scale", type=float, default=1.0,
                    help="Multiplier on stroke widths (default 1.0). >1 thickens; <1 thins.")
    ap.add_argument("--supersample", type=int, default=1, choices=[1, 2, 3],
                    help="Render at NxN resolution then downsample for anti-aliasing (1=off; 2=4x slower but smoother)")
    ap.add_argument("--layer", action="append", default=None,
                    help="Render only strokes on this layer (e.g. --layer 'Layer 2'). Repeatable. Omit to render all layers.")
    ap.add_argument("--color-by-layer", action="store_true",
                    help="Override stroke colours with a per-layer palette so layers are visually distinguishable.")
    ap.add_argument("--list-layers", action="store_true",
                    help="Print per-page layer breakdown and exit without rendering.")
    ap.add_argument("--template", action="store_true",
                    help="Render the reMarkable page template (Dots / Grid / Lines) as a faint background. "
                         "Off by default so AI vision interpretation sees a clean page; turn on for human browsing.")
    ap.add_argument("--check-only", action="store_true",
                    help="Scan pages for the rmscene stroke-merge connector artifact and report clean/affected "
                         "status per page — no PNGs written. Exit code 1 if any page is affected.")
    ap.add_argument("--merge-jump-threshold", type=float, default=_MERGE_JUMP_THRESHOLD,
                    help=f"Stroke-merge detector: min inter-point jump (rmscene units) to flag as a "
                         f"connector (default {_MERGE_JUMP_THRESHOLD}, from rm_config/RM_MERGE_JUMP_THRESHOLD). "
                         f"Governs --check-only and the inline [ARTIFACT] render tag.")
    ap.add_argument("--merge-pressure-min", type=float, default=_MERGE_PRESSURE_MIN,
                    help=f"Stroke-merge detector: min pen pressure (0-255) for a jump to count as severe "
                         f"(default {_MERGE_PRESSURE_MIN}, from rm_config/RM_MERGE_PRESSURE_MIN).")
    ap.add_argument("--pressure-width", action="store_true",
                    help="Render each ink segment individually with pressure-scaled width to attenuate the "
                         "rmscene stroke-merge connector artifact. Use for MV/model-input renders; "
                         "off by default for publication-quality output.")
    ap.add_argument("--transparent", action="store_true",
                    help="Transparent background instead of white (native notebooks only; a composited "
                         "PDF source stays opaque). Output is saved RGBA so strokes sit on transparency "
                         "for compositing. Use for publication artwork.")
    ap.add_argument("--crop", action="store_true",
                    help="Crop each rendered page to its ink bounding box (plus --crop-margin). "
                         "For publication output; off by default (renders the full device page).")
    ap.add_argument("--crop-margin", type=int, default=0,
                    help="Pixels of margin left around the ink when --crop is set (default 0).")
    args = ap.parse_args()

    extracted = Path(args.extracted_dir)
    out_dir = Path(args.out) if args.out else extracted / "_pngs"
    out_dir.mkdir(parents=True, exist_ok=True)

    doc_uuid = find_doc_uuid(extracted)
    pdf_path = extracted / f"{doc_uuid}.pdf"
    has_pdf  = pdf_path.exists() and not args.no_source
    pdf      = open_pdf(pdf_path) if has_pdf else None

    page_map = build_page_map(extracted, doc_uuid)
    template_map = build_template_map(extracted, doc_uuid)
    total = len(page_map)

    selected = parse_page_spec(args.pages, total)
    if selected is None and not args.all_pages:
        # default: annotated pages only
        selected = {idx for idx, rm in page_map if rm is not None}
    elif selected is None:
        selected = set(range(total))

    layer_filter = set(args.layer) if args.layer else None
    color_by_layer = bool(args.color_by_layer)
    print(f"doc: {doc_uuid}  pages: {total}  has_pdf: {has_pdf}  rendering: {len(selected)}  "
          f"width_scale={args.width_scale}  supersample={args.supersample}x"
          + (f"  layer_filter={sorted(layer_filter)}" if layer_filter else "")
          + ("  color_by_layer" if color_by_layer else ""))

    # --list-layers: dump per-page layer breakdown, no rendering
    if args.list_layers:
        for idx, rm_path in page_map:
            if idx not in selected or rm_path is None:
                continue
            blocks = parse_rm_blocks(rm_path)
            lmap = build_layer_map(blocks)
            counts = layer_summary(blocks, lmap)
            if not counts:
                continue
            summary = ", ".join(f"{label!r}: {n}" for label, n in sorted(counts.items()))
            print(f"  page {idx + 1:>3}: layers={sorted(set(lmap.values()))}  strokes by layer: {summary}")
        return 0

    # --check-only: scan for stroke-merge connector artifact, no rendering.
    # Exit code 1 if any page is affected (useful for CI / pre-publish checks).
    if args.check_only:
        affected = 0
        clean = 0
        if (args.merge_jump_threshold != _MERGE_JUMP_THRESHOLD
                or args.merge_pressure_min != _MERGE_PRESSURE_MIN):
            print(f"  (non-default thresholds: jump>{args.merge_jump_threshold:g} "
                  f"pressure>{args.merge_pressure_min:g})")
        for idx, rm_path in page_map:
            if idx not in selected:
                continue
            if rm_path is None:
                print(f"  page {idx + 1:>3}: [BLANK] no .rm file")
                clean += 1
                continue
            blocks = parse_rm_blocks(rm_path)
            m = detect_stroke_merge(blocks, args.merge_jump_threshold, args.merge_pressure_min)
            if m["has_artifact"]:
                print(f"  page {idx + 1:>3}: [ARTIFACT] severe={m['severe_count']} max_jump={m['max_jump']:.0f} units"
                      " -- do NOT publish (rmscene stroke-merge bug)")
                affected += 1
            else:
                print(f"  page {idx + 1:>3}: [CLEAN]    max_jump={m['max_jump']:.0f} units")
                clean += 1
        total_checked = affected + clean
        print(f"\n{total_checked} page(s) checked: {clean} clean, {affected} affected")
        if affected:
            print(f"Affected pages have the rmscene 0.8.0 stroke-merge artifact.")
            print(f"See ForClaude/KNOWN_ISSUE_rmscene_stroke_merge.md for context.")
        return 1 if affected else 0

    rendered = 0
    for idx, rm_path in page_map:
        if idx not in selected:
            continue
        blocks = parse_rm_blocks(rm_path) if rm_path else []
        merge = (detect_stroke_merge(blocks, args.merge_jump_threshold, args.merge_pressure_min)
                 if blocks else {"has_artifact": False, "severe_count": 0, "max_jump": 0.0})
        sb = stroke_bbox(blocks) if blocks else None

        # Compute rmscene -> canvas transform per page.
        if has_pdf and idx < len(pdf):
            pdf_pt_w, pdf_pt_h = _page_size(pdf, idx)
            # The rendered page is RM_WIDTH px wide, page_aspect * RM_WIDTH tall.
            canvas_per_pdf = RM_WIDTH / pdf_pt_w           # canvas px per PDF point
            rm_per_canvas_x = PDF_RM_SCALE / canvas_per_pdf  # rmscene units per canvas px
            rm_per_canvas_y = rm_per_canvas_x              # uniform per calibration
            canvas_x_for_rm_zero = RM_WIDTH / 2            # rmscene x=0 -> page centre
            canvas_y_for_rm_zero = 0                       # rmscene y=0 -> page top
        else:
            rm_per_canvas_x = 1.0
            rm_per_canvas_y = 1.0
            canvas_x_for_rm_zero = RM_WIDTH / 2
            canvas_y_for_rm_zero = 0

        canvas, page_x, page_y = make_canvas(
            pdf if has_pdf else None, idx, sb,
            rm_per_canvas_x=rm_per_canvas_x, rm_per_canvas_y=rm_per_canvas_y,
            canvas_x_for_rm_zero=canvas_x_for_rm_zero,
            canvas_y_for_rm_zero=canvas_y_for_rm_zero,
            transparent=args.transparent,
        )

        # Page template (dots / grid / lines) — native notebooks only, opt-in.
        # Off by default so AI vision (rm_interpret.py) sees a clean canvas
        # without faint background patterns confusing transcription.
        page_template = (
            template_map.get(idx) if (args.template and not has_pdf) else None
        )

        # Page template (dots / grid / lines) gets drawn at native canvas
        # resolution before any supersample/downsample so the pattern stays
        # crisp. Supersample is for stroke anti-aliasing, not background
        # patterns — those benefit nothing from being smudged through LANCZOS.
        if page_template:
            apply_page_template(canvas, page_template, scale=1)

        if args.supersample > 1:
            ss = args.supersample
            # Strokes go on a separate transparent layer at supersample, then
            # downsample, then composite onto the (already-template-drawn) canvas.
            strokes_big = Image.new(
                "RGBA", (canvas.width * ss, canvas.height * ss), (0, 0, 0, 0)
            )
            n_strokes = overlay_strokes(
                strokes_big, blocks, page_x=page_x * ss, page_y=page_y * ss,
                width_scale=args.width_scale * ss,
                rm_per_canvas_x=rm_per_canvas_x / ss,
                rm_per_canvas_y=rm_per_canvas_y / ss,
                canvas_x_for_rm_zero=canvas_x_for_rm_zero * ss,
                canvas_y_for_rm_zero=canvas_y_for_rm_zero * ss,
                layer_filter=layer_filter,
                color_by_layer=color_by_layer,
                pressure_width=args.pressure_width,
            )
            strokes_small = strokes_big.resize(
                (canvas.width, canvas.height), Image.Resampling.LANCZOS
            )
            canvas.alpha_composite(strokes_small)
        else:
            n_strokes = overlay_strokes(
                canvas, blocks, page_x=page_x, page_y=page_y,
                width_scale=args.width_scale,
                rm_per_canvas_x=rm_per_canvas_x, rm_per_canvas_y=rm_per_canvas_y,
                canvas_x_for_rm_zero=canvas_x_for_rm_zero,
                canvas_y_for_rm_zero=canvas_y_for_rm_zero,
                layer_filter=layer_filter,
                color_by_layer=color_by_layer,
                pressure_width=args.pressure_width,
            )

        if args.crop:
            canvas = crop_canvas_to_ink(canvas, args.transparent, args.crop_margin)

        out_path = out_dir / f"page_{idx + 1:03d}.png"
        if args.transparent:
            canvas.save(out_path, "PNG", optimize=True)  # keep alpha
        else:
            canvas.convert("RGB").save(out_path, "PNG", optimize=True)
        size_kb = out_path.stat().st_size // 1024
        artifact_tag = (
            f"  [ARTIFACT severe={merge['severe_count']} max_jump={merge['max_jump']:.0f}]"
            if merge["has_artifact"] else ""
        )
        print(f"  page {idx + 1:>3} -> {out_path.name}  "
              f"({canvas.width}x{canvas.height}, "
              f"rm/canvas={rm_per_canvas_x:.3f}, "
              f"{n_strokes} strokes, {size_kb} kB){artifact_tag}")
        rendered += 1

    print(f"\nrendered {rendered} page(s) into {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
