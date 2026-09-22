#!/usr/bin/env python3
"""
rm_bundle.py -- the permissive, fitz-free half of the .rmdoc reading stack:
locating a document inside an extracted bundle, mapping pages to their .rm
stroke files, and parsing those .rm files into blocks with format-integrity
signals. rmscene only. No PDF engine, no device I/O, no credentials.

WHY THIS FILE EXISTS
--------------------
These primitives were carved out of rm_extract_highlights.py on 2026-08-21 for
the public rm-mcp ship. That module needs PyMuPDF (fitz) for the one thing that
genuinely requires a PDF engine -- intersecting stroke bounding boxes with a
source PDF's text layer -- and PyMuPDF is AGPL-3.0. But three of its functions
(find_doc_uuid, build_page_map, read_rm_blocks) touch no PDF at all and are
needed by tools that ship under a permissive licence:

    rm_reading_ledger.page_ink_shas   -> rm-mcp's rm_page_ink
    rm_extract_text                   -> rm_make_text_notebook -> native-mode
                                         rm_push_content

Leaving them in an AGPL-linked module would have dragged that licence across
the whole shipped surface. Splitting on the fitz line keeps the shipped closure
provably permissive; rm_extract_highlights.py re-exports these names, so every
existing caller keeps working unchanged.

Keep this module fitz-free. tests/test_agpl_boundary.py enforces it.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import warnings
from pathlib import Path
from typing import Any

import rmscene

# Block types that carry actual annotation content. Anything rmscene cannot
# parse comes back as an UnreadableBlock, whose strokes/text are silently lost.
_SCENE_ITEM_BLOCKS = {
    "SceneLineItemBlock", "SceneGlyphItemBlock", "SceneTextItemBlock",
    "SceneGroupItemBlock",
}

# SceneItem block-type bytes (0x03-0x08): strokes, highlights, scene text,
# groups, tombstones -- the actual annotation content. A "newer format" trailing
# tail on ONE of these (or any UnreadableBlock) is a real ink-loss risk. The
# same tail on SceneInfo (0x0D page geometry), RootText (0x07) or other
# metadata/structural blocks is just newer fields we ignore -- no ink is lost.
# Established empirically 2026-07-03 (V2-1): 31 native notebooks / 6433 blocks,
# 0 unreadable, all "newer format" excess on SceneInfo(0x0D)/RootText(0x07).
_INK_BLOCK_TYPES = frozenset({0x03, 0x04, 0x05, 0x06, 0x08})

# rmscene logs `In <block_info ... block_type=N ...> only read M bytes` at INFO
# for every block whose declared length exceeded what the parser consumed.
_ONLY_READ_RE = re.compile(r"only read \d+ bytes")
_BLOCK_TYPE_RE = re.compile(r"block_type=(\d+)")


def find_doc_uuid(extracted_dir: Path) -> str:
    """Identify the document UUID from the extracted .rmdoc layout."""
    contents = list(extracted_dir.glob("*.content"))
    if not contents:
        raise FileNotFoundError(f"no *.content file in {extracted_dir}")
    return contents[0].stem


def build_page_map(extracted_dir: Path, doc_uuid: str) -> dict[int, Path]:
    """Map 0-indexed PDF page -> path to its .rm stroke file (if annotated)."""
    content = json.loads((extracted_dir / f"{doc_uuid}.content").read_text())
    pages = content.get("cPages", {}).get("pages") or content.get("pages") or []
    rm_dir = extracted_dir / doc_uuid
    out: dict[int, Path] = {}
    for idx, p in enumerate(pages):
        # newer format: dict with 'id'; older format: raw UUID string
        if isinstance(p, dict):
            pid = p.get("id")
        elif isinstance(p, str):
            pid = p
        else:
            pid = None
        if not pid:
            continue
        rm = rm_dir / f"{pid}.rm"
        if rm.exists():
            out[idx] = rm
    return out


def classify_excess(excess_types: list, unreadable_types: list) -> dict[str, Any]:
    """Classify "newer format" trailing-byte excess by block type (V2-1).

    Pure helper (no I/O) so the ink-vs-metadata decision is unit-testable.

    Args:
        excess_types: one entry per block that had unread trailing bytes -- the
            block_type int, or None when it could not be attributed (a subblock,
            whose repr carries no block_type).
        unreadable_types: block_type int (or None) for each UnreadableBlock -- a
            whole block that failed to parse.

    Returns a dict: ink_excess, metadata_excess, unattributed_excess,
    excess_block_types (sorted, Nones dropped), ink_loss (bool). ink_loss is
    True when any content (SceneItem) block lost data OR any block was wholly
    unreadable (an unknown/newer whole block is treated as ink-risk); trailing
    metadata-field excess alone is NOT flagged as data loss.
    """
    ink = meta = unattributed = 0
    seen: set[int] = set()
    for bt in excess_types:
        if bt is None:
            unattributed += 1
            continue
        seen.add(bt)
        if bt in _INK_BLOCK_TYPES:
            ink += 1
        else:
            meta += 1
    unreadable_ink = 0
    for bt in unreadable_types:
        if bt is not None:
            seen.add(bt)
        # An unreadable block of unknown or content type is an ink-loss risk.
        if bt is None or bt in _INK_BLOCK_TYPES:
            unreadable_ink += 1
    return {
        "ink_excess": ink,
        "metadata_excess": meta,
        "unattributed_excess": unattributed,
        "excess_block_types": sorted(seen),
        "ink_loss": bool(unreadable_ink) or ink > 0,
    }


def read_rm_blocks(rm_path: Path, diag: dict[str, Any] | None = None) -> list:
    """Parse a .rm file into blocks while capturing format-integrity signals.

    rmscene emits a logging warning (and yields UnreadableBlock entries) when it
    meets a .rm block version newer than it fully models — exactly the case
    behind the "newer .rm format" warning seen after a device firmware bump. If
    that goes unnoticed, strokes/highlights are dropped silently. This wrapper:

    - captures rmscene's WARNING-level log records and any Python warnings,
    - counts UnreadableBlock entries (dropped content) vs real scene items,
    - prints a clear stderr warning for any file with issues (never fails),
    - accumulates per-run totals into `diag` when provided.

    Returns the same block list as rmscene.read_blocks.
    """
    rmscene_logger = logging.getLogger("rmscene")
    captured: list[str] = []     # WARNING-level parser warnings
    info_lines: list[str] = []   # INFO-level "only read" excess-attribution lines

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            msg = record.getMessage()
            if record.levelno >= logging.WARNING:
                captured.append(msg)
            elif "only read" in msg:
                info_lines.append(msg)

    # Raise the logger to INFO so the per-block "only read" attribution reaches
    # us; suppress propagation so those INFO lines don't leak to the console.
    handler = _Capture(level=logging.INFO)
    prev_level, prev_propagate = rmscene_logger.level, rmscene_logger.propagate
    rmscene_logger.addHandler(handler)
    rmscene_logger.setLevel(logging.INFO)
    rmscene_logger.propagate = False
    try:
        with warnings.catch_warnings(record=True) as wlist:
            warnings.simplefilter("always")
            with open(rm_path, "rb") as f:
                blocks = list(rmscene.read_blocks(f))
            captured.extend(str(w.message) for w in wlist)
    finally:
        rmscene_logger.removeHandler(handler)
        rmscene_logger.setLevel(prev_level)
        rmscene_logger.propagate = prev_propagate

    unreadable_blocks = [b for b in blocks if type(b).__name__ == "UnreadableBlock"]
    unreadable = len(unreadable_blocks)
    scene_items = sum(1 for b in blocks if type(b).__name__ in _SCENE_ITEM_BLOCKS)

    # Attribute the "newer format" excess to block types (V2-1): a tail on a
    # content block is an ink-loss risk; a tail on SceneInfo/metadata is not.
    excess_types = [
        (int(m.group(1)) if (m := _BLOCK_TYPE_RE.search(line)) else None)
        for line in info_lines
    ]
    unreadable_types = [
        getattr(getattr(b, "block_info", None), "block_type", None)
        for b in unreadable_blocks
    ]
    cls = classify_excess(excess_types, unreadable_types)

    if unreadable or captured:
        kind = "INK-LOSS RISK" if cls["ink_loss"] else "newer metadata ignored"
        detail = ""
        if captured:
            detail = (f"; {len(captured)} parser warning(s): "
                      f"{'; '.join(sorted(set(captured)))[:200]}")
        print(f"  [warn] rmscene integrity ({rm_path.name}) [{kind}]: "
              f"{unreadable} unreadable, {cls['ink_excess']} ink / "
              f"{cls['metadata_excess']} metadata excess of {len(blocks)} "
              f"block(s){detail}", file=sys.stderr)

    if diag is not None:
        diag["total_blocks"] = diag.get("total_blocks", 0) + len(blocks)
        diag["unreadable_blocks"] = diag.get("unreadable_blocks", 0) + unreadable
        diag["scene_items"] = diag.get("scene_items", 0) + scene_items
        diag["ink_excess"] = diag.get("ink_excess", 0) + cls["ink_excess"]
        diag["metadata_excess"] = (diag.get("metadata_excess", 0)
                                   + cls["metadata_excess"])
        diag["unattributed_excess"] = (diag.get("unattributed_excess", 0)
                                       + cls["unattributed_excess"])
        if cls["excess_block_types"]:
            merged = (set(diag.get("excess_block_types", []))
                      | set(cls["excess_block_types"]))
            diag["excess_block_types"] = sorted(merged)
        if cls["ink_loss"]:
            diag["ink_loss"] = True
        if captured:
            diag.setdefault("warnings", []).extend(captured)
        if unreadable or captured:
            diag.setdefault("files_with_issues", []).append(rm_path.name)

    return blocks
