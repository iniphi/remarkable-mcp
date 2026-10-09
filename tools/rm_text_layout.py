#!/usr/bin/env python3
"""
rm_text_layout.py -- approximate vertical layout of a page's typed text.

Handwriting written on a page that also carries Type Folio text is anchored to
that text: its group stores an anchor (a character id, or the end-of-text
marker) and its stroke coordinates are relative to the anchor's y. To draw the
ink where it sits, the renderer needs the y of the anchor. The device computes
that from real font metrics, which are not in the .rm file; this module
estimates it.

HOW APPROXIMATE THIS IS (read before trusting a pixel):

  * The per-style line heights and the characters-per-line estimate below are
    UNCALIBRATED guesses. They are not measured from a device. They are to be
    calibrated against one real device-written page (the FFT notebook), after
    which the numbers here are replaced and this note is rewritten. Until
    then ink is placed in the right ORDER and roughly the right place, and can
    be a line or more out.
  * Anchoring resolves at PARAGRAPH granularity. Ink anchored to a character in
    the middle of a paragraph is placed at the paragraph's START, because the
    wrapped line the character falls on needs the device font to find
    (decided 2026-10-08).

Pure geometry over rmscene's Text item; imports rmscene only (no fitz), so the
module ships in the public tree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequence
from rmscene.scene_items import CrdtId
from rmscene.text import expand_text_items

# End-of-text anchor marker: Group.anchor_id == CrdtId(0, 0xffffffffffff).
# rmscene's docstring hedges ("bottom of the page?"); that this is "end of the
# typed text" is EMPIRICAL, read off one device file (the FFT notebook), and is
# why anchor resolution is gated on this exact value and treats the rest as
# unresolved.
END_OF_TEXT_ID = CrdtId(0, 0xFFFFFFFFFFFF)
TOP_MARKER_ID = CrdtId(0, 0xFFFFFFFFFFFE)  # meaning unknown: never resolved

# UNCALIBRATED line heights, rm units per rendered line, by paragraph style.
LINE_HEIGHT: dict[str, float] = {
    "heading": 90.0,
    "bold": 60.0,
    "bullet": 50.0,
    "bullet2": 50.0,
    "checkbox": 50.0,
    "checkbox_checked": 50.0,
    "plain": 50.0,
}
DEFAULT_LINE_HEIGHT = 50.0  # UNCALIBRATED

# UNCALIBRATED average glyph advance, rm units per character, by style. Drives
# only the wrapped-line count: lines = ceil(chars * advance / block width).
CHAR_ADVANCE: dict[str, float] = {
    "heading": 40.0,
    "bold": 24.0,
    "plain": 22.0,
}
DEFAULT_CHAR_ADVANCE = 22.0  # UNCALIBRATED


@dataclass(frozen=True)
class ParagraphBox:
    index: int
    style: str
    chars: int
    lines: int
    start_y: float
    end_y: float


@dataclass(frozen=True)
class TextLayout:
    paragraphs: tuple[ParagraphBox, ...]
    end_y: float
    char_paragraph: dict

    def paragraph_of(self, char_id: CrdtId) -> int | None:
        """Index of the paragraph a character id belongs to; None if unknown.

        A paragraph's terminating newline belongs to THAT paragraph, not to the
        next one (pinned in tests)."""
        return self.char_paragraph.get(char_id)


def _style_name(style_lww) -> str:
    value = getattr(style_lww, "value", style_lww)
    name = getattr(value, "name", None)
    return str(name).lower() if name else "plain"


def _split_paragraphs(text: si.Text) -> list[tuple[CrdtId, list[CrdtId], int]]:
    """(style key, char ids, printable char count) per paragraph.

    Built from the character sequence itself (not TextDocument's Paragraph
    objects, whose start_id is the PRECEDING newline), and empty paragraphs are
    kept: each is a line. A final newline closes the last line; it does not
    open a phantom empty paragraph."""
    chars = CrdtSequence(expand_text_items(text.items.sequence_items()))
    out: list[tuple[CrdtId, list[CrdtId], int]] = []
    key = si.END_MARKER
    ids: list[CrdtId] = []
    printable = 0
    for cid in chars:
        ch = chars[cid]
        ids.append(cid)
        if ch == "\n":
            out.append((key, ids, printable))
            key, ids, printable = cid, [], 0
        elif isinstance(ch, str):
            printable += 1
    if ids:  # text after the last newline
        out.append((key, ids, printable))
    return out


def _line_count(style: str, printable: int, width: float) -> int:
    if width <= 0 or printable <= 0:
        return 1
    advance = CHAR_ADVANCE.get(style, DEFAULT_CHAR_ADVANCE)
    return max(1, math.ceil(printable * advance / width))


def layout_text(text: si.Text) -> TextLayout:
    """Lay out a Text item: paragraph boxes, end-of-text y, char -> paragraph."""
    y = float(text.pos_y)
    boxes: list[ParagraphBox] = []
    char_paragraph: dict = {}
    for i, (key, ids, printable) in enumerate(_split_paragraphs(text)):
        style = _style_name(text.styles.get(key))
        lines = _line_count(style, printable, float(text.width))
        height = LINE_HEIGHT.get(style, DEFAULT_LINE_HEIGHT) * lines
        boxes.append(ParagraphBox(i, style, printable, lines, y, y + height))
        for cid in ids:
            char_paragraph[cid] = i
        y += height
    return TextLayout(tuple(boxes), y, char_paragraph)


def anchor_y(layout: TextLayout, anchor_id: CrdtId) -> float | None:
    """y (rm units) an anchor id resolves to, or None when it cannot be placed.

    End-of-text marker -> the bottom of the typed block. A character id -> the
    START of its paragraph (mid-paragraph precision needs device font metrics).
    The top marker and unknown ids -> None, so the caller can count them."""
    if anchor_id == END_OF_TEXT_ID:
        return layout.end_y
    index = layout.paragraph_of(anchor_id)
    if index is None:
        return None
    return layout.paragraphs[index].start_y
