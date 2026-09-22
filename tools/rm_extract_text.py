#!/usr/bin/env python3
"""
rm_extract_text.py -- pull the typed (Type-Folio keyboard) text layer out of a
reMarkable .rmdoc.

reMarkable stores keyboard-typed text natively in the .rm scene as a
`RootTextBlock` holding a CRDT `Text` item — NOT as strokes. So it can be read
losslessly with rmscene (no OCR / no vision), exactly like snap-to-text
highlights are. This is the counterpart to:
  - rm_extract_highlights.py  (highlighter glyph + stroke recovery)
  - rm_interpret.py           (handwriting / sketches via vision)

Usage:
    python tools/rm_extract_text.py <unzipped-.rmdoc-dir>
    python tools/rm_extract_text.py <dir> --out typed.json

Output JSON:
    {
      "doc_uuid": "...",
      "pages": [
        {"pdf_page": 1, "text": "full page text",
         "paragraphs": [{"text": "...", "style": "heading"}, ...]}
      ],
      "integrity": { rmscene parse diagnostics, see read_rm_blocks }
    }

Pages with no typed text (pure handwriting / PDF) simply don't appear.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from rmscene import RootTextBlock
from rmscene.text import TextDocument

from rm_bundle import build_page_map, find_doc_uuid, read_rm_blocks

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _paragraph_style_name(para: Any) -> str:
    """ParagraphStyle enum name (plain / heading / bullet / ...), lower-cased."""
    try:
        return para.style.value.name.lower()
    except Exception:
        return "plain"


def extract_typed_text(extracted_dir: Path,
                       *, diag: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return per-page typed text from an unzipped .rmdoc.

    [{pdf_page, text, paragraphs: [{text, style}]}] for each page that carries a
    RootTextBlock. Handwriting is NOT included here (use rm_extract_highlights /
    rm_interpret). Pass `diag` to accumulate rmscene parse-integrity signals.
    """
    doc_uuid = find_doc_uuid(extracted_dir)
    page_map = build_page_map(extracted_dir, doc_uuid)

    out: list[dict[str, Any]] = []
    for page_idx, rm_path in sorted(page_map.items()):
        blocks = read_rm_blocks(rm_path, diag)
        paragraphs: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, RootTextBlock):
                continue
            try:
                doc = TextDocument.from_scene_item(block.value)
            except Exception as e:
                print(f"  [warn] typed-text parse failed on page {page_idx + 1}: {e}",
                      file=sys.stderr)
                continue
            for para in doc.contents:
                text = str(para).rstrip("\n")
                if not text.strip():
                    continue
                paragraphs.append({"text": text, "style": _paragraph_style_name(para)})

        if paragraphs:
            out.append({
                "pdf_page": page_idx + 1,
                "text": "\n".join(p["text"] for p in paragraphs),
                "paragraphs": paragraphs,
            })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("extracted_dir", help="Path to an unzipped .rmdoc directory")
    ap.add_argument("--out", help="Write JSON here (default: stdout)")
    args = ap.parse_args()

    extracted = Path(args.extracted_dir)
    diag: dict[str, Any] = {}
    pages = extract_typed_text(extracted, diag=diag)
    result = {
        "doc_uuid": find_doc_uuid(extracted),
        "pages": pages,
        "integrity": diag,
    }
    blob = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(blob, encoding="utf-8")
        print(f"Wrote {args.out} ({len(pages)} page(s) with typed text)")
    else:
        print(blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
