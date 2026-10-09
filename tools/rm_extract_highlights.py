#!/usr/bin/env python3
"""
rm_extract_highlights.py -- Extract highlighted text from a reMarkable .rmdoc bundle.

Pipeline:
    rmapi get /Science/Papers/OoL/1   ->   1.rmdoc
    unzip 1.rmdoc                      ->   <uuid>/, <uuid>.pdf, <uuid>.content
    rm_extract_highlights.py <dir>     ->   JSON of {page, bbox, text} per highlight

Background:
    reMarkable's Share -> OneDrive export flattens annotations into PDF page graphics
    (no /Highlight subtype). But the raw .rm stroke files inside .rmdoc bundles preserve
    every highlighter swipe with x,y coordinates. By intersecting those stroke bboxes
    against the source PDF's text layer (via pypdfium2), we recover the highlighted
    text -- with zero OCR -- as long as the PDF has a clean text layer underneath.

Requires:
    rmscene >= 0.8.0    (parses .rm v6 stroke files)
    pypdfium2           (permissive-licensed; PDF text positions, geometry
                         intersection -- ships in the public manifest, unlike
                         AGPL PyMuPDF/fitz)

Status: v0 proof of concept (2026-05-21). Confirmed on Waechtershaeuser 1988
(/Science/Papers/OoL/1 on the device). Future work:
- Robust handling of degenerate (single-point) strokes
- Cluster overlapping/sub-strokes into single semantic highlights
- Map back to per-page Zotero annotation objects
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import warnings
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import rmscene
from rmscene.scene_items import Pen


# reMarkable 2 native device resolution (portrait)
RM_WIDTH = 1404
RM_HEIGHT = 1872

# PDF-backed-document transform: 1 PDF point ~ 3.16 rmscene units, page centred
# on rmscene x=0, page top edge at rmscene y=0. Calibrated 2026-05-22; full
# provenance comment lives on rm_config.PDF_RM_SCALE (single source of truth).
from rm_config import PDF_RM_SCALE  # noqa: E402

# Bundle/rmscene primitives live in the fitz-free rm_bundle module so the
# permissive public surface (rm_page_ink, native-mode rm_push_content) can use
# them without linking AGPL PyMuPDF -- see rm_bundle.py's docstring. Re-exported
# here so the seven existing callers keep importing them from this module.
from rm_bundle import (  # noqa: E402,F401
    _BLOCK_TYPE_RE,
    _INK_BLOCK_TYPES,
    _ONLY_READ_RE,
    _SCENE_ITEM_BLOCKS,
    build_page_map,
    classify_excess,
    find_doc_uuid,
    read_rm_blocks,
)

# Padding around stroke bbox before text intersection (PDF points)
BBOX_PAD = 4

# --- Semantic highlight clustering ---------------------------------------
# One user-intended highlight is emitted by the device as SEVERAL records: a
# passage spanning a line break splits per line, and a dense two-column target
# splits per column run. Measured 2026-07-25 on the first remote highlighter
# test: 11 records for 5 highlights. Nothing is lost -- they concatenate in
# reading order -- but every consumer (rm_write_notes Note 1, Findings Archive)
# then presents one highlight as several bullets. These tolerances merge them
# back. All values are PDF points.
CLUSTER_WORD_GAP = 18.0     # max horizontal gap joining two runs on one line
CLUSTER_LINE_GAP = 8.0      # max vertical gap joining a wrapped line
CLUSTER_COLUMN_TOL = 24.0   # max left-edge drift for a wrapped line to count
CLUSTER_VOVERLAP = 0.5      # frac of the shorter height that means "same line"

# PDF text extraction returns typographic ligatures as single codepoints, so
# "effects" comes back as "eﬀects". Naive substring matching downstream
# then fails to find a passage that is visibly present. Normalised once here,
# at the boundary where records become user-facing text.
LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
    "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}


def rm_to_pdf(x_rm: float, y_rm: float, pdf_w: float, pdf_h: float) -> tuple[float, float]:
    """rmscene coords (x centred at 0, y top-down) -> PDF coords (origin top-left).

    Uses the calibrated PDF_RM_SCALE: rmscene records strokes such that
    PDF (0,0) maps to rmscene (-pdf_w * PDF_RM_SCALE / 2, 0). pdf_h is unused
    but kept in the signature so callers don't break.
    """
    del pdf_h  # noqa: F841 — kept for signature stability
    return (x_rm / PDF_RM_SCALE + pdf_w / 2.0,
            y_rm / PDF_RM_SCALE)


def page_words(page, page_h: float) -> list[tuple[float, float, float, float, str, int]]:
    """Reconstruct words from a pypdfium2 page's character-level text boxes.

    pypdfium2 has no word-level API (unlike PyMuPDF's page.get_text("words")),
    so words are built here by grouping consecutive non-whitespace characters
    -- `not ch.strip()` is the separator test, which also covers the `\\r\\n`
    line-end characters pdfium generates between text lines.

    Returns (x0, y0, x1, y1, text, order) tuples in PDF points with a
    TOP-LEFT origin: pdfium's get_charbox reports (left, bottom, right, top)
    in the PDF's native bottom-left-up space, flipped here via page_h so
    every box in this module shares one origin. `order` is the index of the
    word's first (geometry-bearing) character, used to restore reading order
    after the stroke-box filter picks a subset of words.
    """
    textpage = page.get_textpage()
    try:
        n = textpage.count_chars()
        text = textpage.get_text_range(0, n)
        if len(text) != n:
            text = "".join(textpage.get_text_range(i, 1) for i in range(n))

        words: list[tuple[float, float, float, float, str, int]] = []
        chars: list[str] = []
        boxes: list[tuple[float, float, float, float]] = []
        start_idx: int | None = None

        def flush() -> None:
            nonlocal chars, boxes, start_idx
            if chars and boxes:
                wx0 = min(b[0] for b in boxes)
                wy0 = min(b[1] for b in boxes)
                wx1 = max(b[2] for b in boxes)
                wy1 = max(b[3] for b in boxes)
                words.append((wx0, wy0, wx1, wy1, "".join(chars), start_idx))
            chars = []
            boxes = []
            start_idx = None

        for i, ch in enumerate(text):
            if not ch.strip():
                flush()
                continue
            try:
                left, bottom, right, top = textpage.get_charbox(i)
            except Exception:
                continue  # no geometry for this char -- skip it, keep the word
            box = (min(left, right), page_h - top, max(left, right), page_h - bottom)
            if start_idx is None:
                start_idx = i
            chars.append(ch)
            boxes.append(box)
        flush()
        return words
    finally:
        textpage.close()


def _strip_ws(text: str) -> str:
    return "".join(text.split())


def respace_glyph_text(device_text: str,
                       rects_pdf: list[list[float]],
                       words: list[tuple[float, float, float, float, str, int]],
                       ) -> tuple[str, str]:
    """Rebuild a glyph highlight's spacing from the PDF's own words.

    A snap-to-text GlyphRange's `text` loses the space at a wrapped line break
    ("signsor"). The PDF words whose centres fall inside the highlight's rects,
    joined in reading order with single spaces, restore it. The candidate is
    ADOPTED only when it equals the device text once all whitespace is removed
    (exact, case-sensitive, ligature-normalised on both sides); otherwise the
    device text is returned unchanged. Characters are never invented or dropped.

    Returns (text, text_source) with text_source "pdf_words" or "device".
    """
    if not device_text or not rects_pdf or not words:
        return device_text, "device"
    picked = sorted(
        (order, w)
        for wx0, wy0, wx1, wy1, w, order in words
        if any(r[0] <= (wx0 + wx1) / 2 <= r[2] and r[1] <= (wy0 + wy1) / 2 <= r[3]
               for r in rects_pdf)
    )
    if not picked:
        return device_text, "device"
    candidate = " ".join(w for _, w in picked)
    if _strip_ws(normalise_ligatures(candidate)) != _strip_ws(normalise_ligatures(device_text)):
        return device_text, "device"
    return candidate, "pdf_words"


def _extract_glyph_items_for_page(blocks) -> list[dict[str, Any]]:
    """Pull snap-to-text highlights (SceneGlyphItemBlock -> GlyphRange) from a
    .rm block list. Each GlyphRange already carries the literal highlighted
    text — no PDF text intersection needed.

    Returns a list of {text, color, rectangles_rm, source}. `rectangles_rm` is
    the list of rmscene-coord Rectangle(x,y,w,h) — caller transforms to PDF.
    """
    out: list[dict[str, Any]] = []
    for b in blocks:
        if type(b).__name__ != "SceneGlyphItemBlock":
            continue
        item = getattr(b, "item", None)
        gr = getattr(item, "value", None) if item else None
        if gr is None:
            continue  # deleted / placeholder
        rects = getattr(gr, "rectangles", None) or []
        out.append({
            "text": getattr(gr, "text", "") or "",
            "color": (gr.color.name if hasattr(getattr(gr, "color", None), "name")
                      else str(getattr(gr, "color", ""))),
            "rectangles_rm": [(r.x, r.y, r.w, r.h) for r in rects],
            "source": "glyph",
        })
    return out


def integrity_warning(diag: dict[str, Any], device_path: str) -> dict[str, Any] | None:
    """Build the structured integrity warning for a parsed .rmdoc, or None when
    the parse was clean.

    Distinguishes real ink loss (content-block excess or an UnreadableBlock ->
    `rmscene_newer_format`, possible_data_loss=True) from benign newer metadata
    fields being ignored (`newer_metadata_ignored`, possible_data_loss=False).
    Shared by rm_pull and rm_capture so the two stay in lockstep (V2-1).
    """
    if not (diag.get("unreadable_blocks") or diag.get("warnings")):
        return None
    from rm_config import make_warning  # lazy -- avoids a top-level import cycle
    if diag.get("ink_loss"):
        return make_warning(
            "rmscene_newer_format",
            f"{device_path}: .rm content blocks newer than the installed rmscene "
            f"parser -- newest ink may be silently dropped",
            possible_data_loss=True,
            data={"device_path": device_path,
                  "ink_excess": diag.get("ink_excess"),
                  "unreadable_blocks": diag.get("unreadable_blocks"),
                  "excess_block_types": diag.get("excess_block_types"),
                  "warnings": diag.get("warnings", [])[:5]},
        )
    return make_warning(
        "newer_metadata_ignored",
        f"{device_path}: newer .rm metadata fields (page geometry / SceneInfo) "
        f"ignored by the installed rmscene parser -- no ink content affected",
        possible_data_loss=False,
        data={"device_path": device_path,
              "metadata_excess": diag.get("metadata_excess"),
              "excess_block_types": diag.get("excess_block_types")},
    )


def check_rm_integrity(extracted_dir: Path) -> dict[str, Any]:
    """Scan every annotated .rm page of an extracted .rmdoc for parse-integrity
    issues. Returns a diag dict: {total_blocks, unreadable_blocks, scene_items,
    warnings[], files_with_issues[], ok}.
    """
    doc_uuid = find_doc_uuid(extracted_dir)
    page_map = build_page_map(extracted_dir, doc_uuid)
    diag: dict[str, Any] = {}
    for _, rm_path in sorted(page_map.items()):
        read_rm_blocks(rm_path, diag)
    diag["ok"] = not diag.get("unreadable_blocks") and not diag.get("warnings")
    # possible_data_loss is the honest signal: only ink-content loss counts.
    # A clean parse whose only "newer format" excess is metadata is ok=False but
    # possible_data_loss=False (V2-1).
    diag["possible_data_loss"] = bool(diag.get("ink_loss"))
    diag["metadata_only"] = (bool(diag.get("warnings"))
                             and not diag.get("ink_loss"))
    return diag


def extract_highlights(extracted_dir: Path,
                       *, diag: dict[str, Any] | None = None,
                       cluster: bool = True) -> list[dict[str, Any]]:
    """Return a list of {page, bbox, text, source} dicts.

    `cluster` (default True) folds the several device records that make up one
    user-intended highlight back into a single record -- see cluster_highlights.
    Pass cluster=False for the raw per-stroke/per-glyph-run records.

    Pass `diag` (a dict) to accumulate rmscene parse-integrity signals across
    pages (see read_rm_blocks); leave it None to ignore them.

    Two highlight sources are merged:
    - `glyph`: snap-to-text highlights (SceneGlyphItemBlock -> GlyphRange) —
      reMarkable's modern highlighter when the underlying PDF has a text layer
      that it can align to. Text is recorded literally on the device.
    - `stroke`: free-drawn highlighter polylines (SceneLineItemBlock with
      tool=HIGHLIGHTER_*) — used when snap fails (no/misaligned text layer) or
      when the user free-draws on a margin / figure. Text recovered via
      pypdfium2 text-bbox intersection.
    """
    doc_uuid = find_doc_uuid(extracted_dir)
    pdf_path = extracted_dir / f"{doc_uuid}.pdf"
    if not pdf_path.exists():
        raise FileNotFoundError(f"source PDF missing: {pdf_path}")

    pdf = pdfium.PdfDocument(str(pdf_path))
    page_map = build_page_map(extracted_dir, doc_uuid)

    results: list[dict[str, Any]] = []
    try:
        pdf_page_count = len(pdf)
        for page_idx, rm_path in sorted(page_map.items()):
            if page_idx >= pdf_page_count:
                # Appended native page (no backing PDF page / text layer): no
                # text-snapped or geometry-recovered highlights to extract here. Its
                # handwritten content is captured by the flat PDF and the vision pass.
                continue
            page = pdf[page_idx]
            pdf_w, pdf_h = page.get_width(), page.get_height()
            words = page_words(page, pdf_h)

            blocks = read_rm_blocks(rm_path, diag)

            # Pass 1: snap-to-text GlyphRange items (literal text, no inference)
            for gh in _extract_glyph_items_for_page(blocks):
                pdf_rects = []
                for x_rm, y_rm, w_rm, h_rm in gh["rectangles_rm"]:
                    px0, py0 = rm_to_pdf(x_rm, y_rm, pdf_w, pdf_h)
                    px1, py1 = rm_to_pdf(x_rm + w_rm, y_rm + h_rm, pdf_w, pdf_h)
                    pdf_rects.append([round(px0, 1), round(py0, 1),
                                      round(px1, 1), round(py1, 1)])
                rec = {
                    "pdf_page": page_idx + 1,
                    "source": "glyph",
                    "text": gh["text"],
                    "stroke_color": gh["color"],
                    "bbox_pdf": pdf_rects[0] if pdf_rects else None,
                    "rects_pdf": pdf_rects,
                    "n_points": None,
                }
                if words:  # only pages with a text layer; others stay unchanged
                    text, src = respace_glyph_text(gh["text"], pdf_rects, words)
                    rec["text"] = text
                    rec["text_device"] = gh["text"]
                    rec["text_source"] = src
                results.append(rec)

            # Pass 2: free-drawn highlighter polylines (text recovered via geometry)
            for b in blocks:
                line = getattr(b, "item", None)
                if not (line and getattr(line, "value", None)):
                    continue
                l = line.value
                tool = getattr(l, "tool", None)
                if tool not in (Pen.HIGHLIGHTER_1, Pen.HIGHLIGHTER_2):
                    continue
                pts = getattr(l, "points", []) or []
                if not pts:
                    continue

                xs = [p.x for p in pts]
                ys = [p.y for p in pts]
                x0, y0 = rm_to_pdf(min(xs), min(ys), pdf_w, pdf_h)
                x1, y1 = rm_to_pdf(max(xs), max(ys), pdf_w, pdf_h)
                # widen degenerate (single-point / tap) strokes so the bbox isn't zero-area
                if x1 - x0 < 2:
                    x0, x1 = x0 - 2, x1 + 2
                if y1 - y0 < 2:
                    y0, y1 = y0 - 2, y1 + 2
                rx0, ry0 = x0 - BBOX_PAD, y0 - BBOX_PAD
                rx1, ry1 = x1 + BBOX_PAD, y1 + BBOX_PAD

                picked = sorted(
                    (order, w)
                    for wx0, wy0, wx1, wy1, w, order in words
                    if rx0 <= (wx0 + wx1) / 2 <= rx1
                    and ry0 <= (wy0 + wy1) / 2 <= ry1
                )
                text = " ".join(w for _, w in picked)
                results.append({
                    "pdf_page": page_idx + 1,
                    "source": "stroke",
                    "bbox_pdf": [round(rx0, 1), round(ry0, 1),
                                 round(rx1, 1), round(ry1, 1)],
                    "text": text,
                    "stroke_color": getattr(l, "color", None).name if getattr(l, "color", None) else None,
                    "n_points": len(pts),
                })
    finally:
        pdf.close()
    return cluster_highlights(results) if cluster else results


def normalise_ligatures(text: str) -> str:
    """Expand typographic ligature codepoints to their ASCII letter pairs."""
    if not text:
        return text
    for lig, plain in LIGATURES.items():
        text = text.replace(lig, plain)
    return text


def _record_rects(rec: dict[str, Any]) -> list[list[float]]:
    """Every constituent rect of a record, preferring rects_pdf over bbox_pdf."""
    rects = rec.get("rects_pdf")
    if rects:
        return [list(r) for r in rects if r]
    bbox = rec.get("bbox_pdf")
    return [list(bbox)] if bbox else []


def _union(rects: list[list[float]]) -> list[float] | None:
    """Axis-aligned union of rects, or None when there are none."""
    if not rects:
        return None
    return [min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects)]


def _continues(prev: list[float], nxt: list[float], column_x0: float) -> bool:
    """True when `nxt` continues the same semantic highlight as `prev`.

    Two shapes count as continuation: a further run on the SAME line (vertical
    ranges overlap, small horizontal gap), or the START OF THE NEXT LINE (small
    vertical gap, and the new run begins near the cluster's own left edge --
    which is what keeps a second text column from being swallowed).
    """
    prev_h, nxt_h = prev[3] - prev[1], nxt[3] - nxt[1]
    shorter = min(prev_h, nxt_h)
    overlap = min(prev[3], nxt[3]) - max(prev[1], nxt[1])
    if shorter > 0 and overlap >= CLUSTER_VOVERLAP * shorter:
        return (nxt[0] - prev[2]) <= CLUSTER_WORD_GAP
    if nxt[1] >= prev[1] and (nxt[1] - prev[3]) <= CLUSTER_LINE_GAP:
        return nxt[0] <= column_x0 + CLUSTER_COLUMN_TOL
    return False


def cluster_highlights(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge device records into one record per semantic highlight.

    Returns a NEW list; `records` is never mutated. Records only ever merge
    within the same page, source and stroke colour, so a yellow and a green
    highlight on the same line stay separate. Constituent rects are preserved
    on `rects_pdf` for bbox fidelity, and `n_merged` records how many device
    records the result came from.
    """
    groups: dict[tuple, list[dict[str, Any]]] = {}
    order: list[tuple] = []
    for rec in records:
        key = (rec.get("pdf_page"), rec.get("source"), rec.get("stroke_color"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(rec)

    out: list[dict[str, Any]] = []
    for key in order:
        members = sorted(
            groups[key],
            key=lambda r: (
                (_union(_record_rects(r)) or [0, 0, 0, 0])[1],
                (_union(_record_rects(r)) or [0, 0, 0, 0])[0],
            ),
        )
        cluster: list[dict[str, Any]] = []
        for rec in members:
            box = _union(_record_rects(rec))
            if cluster and box is not None:
                prev_box = _union([r for m in cluster for r in _record_rects(m)])
                column_x0 = prev_box[0] if prev_box else box[0]
                if prev_box is not None and _continues(prev_box, box, column_x0):
                    cluster.append(rec)
                    continue
            if cluster:
                out.append(_merge_cluster(cluster))
            cluster = [rec]
        if cluster:
            out.append(_merge_cluster(cluster))
    return out


def _merge_cluster(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold one run of continuous records into a single highlight record."""
    head = cluster[0]
    rects = [r for m in cluster for r in _record_rects(m)]
    parts = [(m.get("text") or "").strip() for m in cluster]
    n_points = [m.get("n_points") for m in cluster if m.get("n_points") is not None]
    merged = {
        "pdf_page": head.get("pdf_page"),
        "source": head.get("source"),
        "text": normalise_ligatures(" ".join(p for p in parts if p)),
        "stroke_color": head.get("stroke_color"),
        "bbox_pdf": _union(rects),
        "rects_pdf": rects,
        "n_points": sum(n_points) if n_points else None,
        "n_merged": len(cluster),
    }
    sources = {m.get("text_source") for m in cluster}
    if sources != {None}:  # additive: only glyph records that went through respacing
        merged["text_source"] = (sources.pop() if len(sources) == 1 else "mixed")
        merged["text_device"] = normalise_ligatures(" ".join(
            p for p in ((m.get("text_device", m.get("text")) or "").strip()
                        for m in cluster) if p))
    return merged


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("extracted_dir", help="Path to an unzipped .rmdoc directory")
    ap.add_argument("--json", action="store_true",
                    help="Output as JSON (default: human-readable text)")
    ap.add_argument("--raw", action="store_true",
                    help="Emit raw per-stroke records without semantic clustering")
    args = ap.parse_args()

    results = extract_highlights(Path(args.extracted_dir), cluster=not args.raw)
    if args.json:
        json.dump(results, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        for r in results:
            t = r["text"] or "(empty)"
            print(f"page {r['pdf_page']}: {t[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
