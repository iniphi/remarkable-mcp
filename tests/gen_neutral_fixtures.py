"""
gen_neutral_fixtures.py -- mint the NEUTRAL render-fixture corpus.

Regenerates tests/fixtures/render_neutral/ from scratch: every byte in that
tree is produced by this script, in code, right here. Nothing is copied from
a real reMarkable notebook, a real PDF, or any file under tools/rm_snapshots
or the device. Content is synthetic shapes (a benzene ring, a grid of lines,
engineered stroke-merge jumps) and generated lorem-ipsum text on a hand-built
PDF -- see NEUTRAL_PROVENANCE.md alongside this script for the full statement.

This exists because the private fixture corpus at tests/fixtures/render/ is
the author's own handwriting and cannot ship publicly. test_calibration_neutral.py and
test_render_goldens_neutral.py are the public-safe regression guard for
PDF_RM_SCALE (rm_config.py) and the stroke-merge detector calibration
(MERGE_JUMP_THRESHOLD / MERGE_PRESSURE_MIN) that this fixture set backs.

Run:
    python rm-mcp/tests/gen_neutral_fixtures.py

Rewrites every file under tests/fixtures/render_neutral/, including the
minted goldens. Review the diff before committing, same discipline as
RM_REGEN_GOLDENS=1 on the private suite.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import uuid
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
RM_MCP_DIR = TESTS_DIR.parent
REPO_ROOT = RM_MCP_DIR.parent
TOOLS_DIR = REPO_ROOT / "tools"

from _toolpath import tool_script  # noqa: E402
for _p in (TOOLS_DIR,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from rmscene import (  # noqa: E402
    AuthorIdsBlock,
    CrdtId,
    CrdtSequence,
    CrdtSequenceItem,
    LwwValue,
    MigrationInfoBlock,
    PageInfoBlock,
    SceneGroupItemBlock,
    SceneInfo,
    SceneLineItemBlock,
    SceneTreeBlock,
    TreeNodeBlock,
    write_blocks,
)
import rmscene.scene_items as si  # noqa: E402

import rm_render_page as rp  # noqa: E402
from rm_config import PDF_RM_SCALE  # noqa: E402

FIXTURES_ROOT = TESTS_DIR / "fixtures" / "render_neutral"
GOLDENS_SUBDIR = "_goldens"

# Fixed UUIDs (deterministic regeneration -- a re-run produces byte-identical
# .content/.metadata, so a diff after regen shows only real changes).
AUTHOR_UUID = uuid.UUID("00000000-0000-4000-8000-000000000001")


def _point(x: float, y: float, pressure: int, speed: int = 10, direction: int = 0,
           width: int = 2) -> "si.Point":
    return si.Point(x=x, y=y, speed=speed, direction=direction, width=width,
                     pressure=pressure)


def build_stroke_page(strokes: list[list[tuple[float, float, int]]]) -> bytes:
    """Build a native (no-PDF) .rm page containing the given strokes.

    strokes: list of strokes, each a list of (x, y, pressure) tuples forming
    one SceneLineItemBlock (one continuous ballpoint stroke).
    """
    author = 1
    root_group = TreeNodeBlock(
        group=si.Group(
            node_id=CrdtId(0, 1),
            children=CrdtSequence(),
            label=LwwValue(CrdtId(0, 0), ""),
            visible=LwwValue(CrdtId(0, 0), True),
        )
    )
    layer1_group = TreeNodeBlock(
        group=si.Group(
            node_id=CrdtId(0, 11),
            children=CrdtSequence(),
            label=LwwValue(CrdtId(0, 12), "Layer 1"),
            visible=LwwValue(CrdtId(0, 0), True),
        )
    )
    scene_group = SceneGroupItemBlock(
        parent_id=CrdtId(0, 1),
        item=CrdtSequenceItem(
            item_id=CrdtId(0, 13),
            left_id=CrdtId(0, 0),
            right_id=CrdtId(0, 0),
            deleted_length=0,
            value=CrdtId(0, 11),
        ),
    )

    scene_inf = SceneInfo(
        current_layer=LwwValue(CrdtId(0, 0), CrdtId(0, 0)),
        background_visible=LwwValue(CrdtId(0, 0), True),
        root_document_visible=LwwValue(CrdtId(0, 0), True),
        paper_size=(1404, 1872),
    )

    blocks: list = [
        AuthorIdsBlock(author_uuids={author: AUTHOR_UUID}),
        MigrationInfoBlock(migration_id=CrdtId(author, 1), is_device=True),
        PageInfoBlock(
            loads_count=1, merges_count=0,
            text_chars_count=0, text_lines_count=0,
            type_folio_use_count=0,
        ),
        scene_inf,
        SceneTreeBlock(
            tree_id=CrdtId(0, 11), node_id=CrdtId(0, 0),
            is_update=True, parent_id=CrdtId(0, 1),
        ),
        root_group,
        layer1_group,
        scene_group,
    ]

    seq = 20
    for stroke in strokes:
        points = [_point(x, y, p) for x, y, p in stroke]
        line = si.Line(
            color=si.PenColor.BLACK,
            tool=si.Pen.BALLPOINT_1,
            points=points,
            thickness_scale=1.0,
            starting_length=0.0,
        )
        item_id = CrdtId(author, seq)
        seq += 1
        blocks.append(SceneLineItemBlock(
            parent_id=CrdtId(0, 11),
            item=CrdtSequenceItem(
                item_id=item_id, left_id=CrdtId(0, 0), right_id=CrdtId(0, 0),
                deleted_length=0, value=line,
            ),
        ))

    buf = io.BytesIO()
    write_blocks(buf, blocks)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Minimal hand-built PDF (no PyMuPDF/reportlab dependency -- keeps this
# generator itself importable from the permissive public tree). Base-14
# Helvetica, one content stream of literal text lines. Good enough to give
# pypdfium2 a real text-bearing page to rasterize.
# ---------------------------------------------------------------------------

def _pdf_escape(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_lorem_pdf(lines: list[str], page_w: float = 612.0, page_h: float = 792.0,
                     font_size: int = 12, top_margin: float = 72.0,
                     left_margin: float = 72.0, leading: float = 18.0) -> bytes:
    """Build a minimal single-page PDF with literal text lines, top-down."""
    ops = [f"BT /F1 {font_size} Tf {leading:g} TL {left_margin:g} {page_h - top_margin:g} Td"]
    for i, line in enumerate(lines):
        prefix = "" if i == 0 else "T* "
        ops.append(f"{prefix}({_pdf_escape(line)}) Tj")
    ops.append("ET")
    content = "\n".join(ops).encode("latin-1")

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w:g} {page_h:g}] "
        f"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>".encode("latin-1")
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(
        f"<< /Length {len(content)} >>\nstream\n".encode("latin-1") + content
        + b"\nendstream"
    )

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]  # object 0 is the free-list head, never written as a body
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode("latin-1"))
        out.write(body)
        out.write(b"\nendobj\n")

    xref_offset = out.tell()
    n = len(objects) + 1
    out.write(f"xref\n0 {n}\n".encode("latin-1"))
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode("latin-1"))
    out.write(f"trailer\n<< /Size {n} /Root 1 0 R >>\n".encode("latin-1"))
    out.write(f"startxref\n{xref_offset}\n%%EOF".encode("latin-1"))
    return out.getvalue()


# ---------------------------------------------------------------------------
# Fixture assembly
# ---------------------------------------------------------------------------

def _write_rmdoc_dir(fixture_dir: Path, doc_uuid: str, page_uuid: str,
                      rm_bytes: bytes, pdf_bytes: bytes | None,
                      title: str) -> None:
    fixture_dir.mkdir(parents=True, exist_ok=True)
    content = {
        "coverPageNumber": 0,
        "documentMetadata": {},
        "extraMetadata": {},
        "fileType": "pdf" if pdf_bytes else "notebook",
        "fontName": "",
        "formatVersion": 2,
        "lineHeight": -1,
        "margins": 180,
        "orientation": "portrait",
        "pageCount": 1,
        "cPages": {
            "lastOpened": {"timestamp": "0", "value": page_uuid},
            "pages": [
                {"id": page_uuid, "idx": {"timestamp": "0", "value": {"rindex": -1}}}
            ],
            "uuids": [{"timestamp": "0", "value": page_uuid}],
        },
        "pageTags": [],
        "sizeInBytes": "0",
        "tags": [],
        "textAlignment": "justify",
        "textScale": 1,
        "zoomMode": "bestFit",
    }
    metadata = {
        "createdTime": "0",
        "lastModified": "0",
        "lastOpened": "0",
        "lastOpenedPage": 0,
        "pinned": False,
        "type": "DocumentType",
        "visibleName": title,
        "parent": "",
    }
    (fixture_dir / f"{doc_uuid}.content").write_text(
        json.dumps(content, indent=2), encoding="utf-8")
    (fixture_dir / f"{doc_uuid}.metadata").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8")
    if pdf_bytes is not None:
        (fixture_dir / f"{doc_uuid}.pdf").write_bytes(pdf_bytes)
    page_dir = fixture_dir / doc_uuid
    page_dir.mkdir(parents=True, exist_ok=True)
    (page_dir / f"{page_uuid}.rm").write_bytes(rm_bytes)


def _hexagon(cx: float, cy: float, r: float, pressure: int) -> list[tuple[float, float, int]]:
    import math
    pts = []
    for i in range(7):  # 7th point closes the loop back to vertex 0
        ang = math.radians(60 * i)
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang), pressure))
    return pts


def _hline(x0: float, y: float, length: float, n: int, pressure: int) -> list[tuple[float, float, int]]:
    step = length / (n - 1)
    return [(x0 + i * step, y, pressure) for i in range(n)]


def build_shapes_clean(fixture_dir: Path) -> dict:
    """Two clean pages: a hexagon (benzene ring) and a grid of short lines.
    All inter-point jumps stay well under MERGE_JUMP_THRESHOLD (60)."""
    doc_uuid = "10000000-0000-4000-8000-000000000001"

    page1_uuid = "10000000-0000-4000-8000-0000000000a1"
    strokes_p1 = [_hexagon(700, 700, 35, pressure=220)]
    rm1 = build_stroke_page(strokes_p1)

    page2_uuid = "10000000-0000-4000-8000-0000000000a2"
    strokes_p2 = [_hline(300, 400 + 50 * i, 150, 6, pressure=220) for i in range(4)]
    rm2 = build_stroke_page(strokes_p2)

    _write_rmdoc_dir(fixture_dir, doc_uuid, page1_uuid, rm1, None, "Neutral shapes (clean) p1")
    # Second page lives as a second .rm under the same doc dir; write directly.
    (fixture_dir / doc_uuid / f"{page2_uuid}.rm").write_bytes(rm2)
    # Extend content.json's page list to carry both pages, in order.
    content_path = fixture_dir / f"{doc_uuid}.content"
    content = json.loads(content_path.read_text(encoding="utf-8"))
    content["pageCount"] = 2
    content["cPages"]["pages"].append(
        {"id": page2_uuid, "idx": {"timestamp": "0", "value": {"rindex": -1}}})
    content["cPages"]["uuids"].append({"timestamp": "0", "value": page2_uuid})
    content_path.write_text(json.dumps(content, indent=2), encoding="utf-8")

    return {
        "doc_uuid": doc_uuid,
        "pages": {1: (page1_uuid, strokes_p1), 2: (page2_uuid, strokes_p2)},
    }


def build_shapes_artifact(fixture_dir: Path) -> dict:
    """Two pages, each with one stroke engineered to trip the stroke-merge
    detector: a short clean run, then a large jump at full pressure -- the
    signature of a pen-lift the parser mis-joined (see rm_render_page.py
    detect_stroke_merge)."""
    doc_uuid = "20000000-0000-4000-8000-000000000001"

    page1_uuid = "20000000-0000-4000-8000-0000000000a1"
    stroke1 = [
        (200, 300, 220), (218, 312, 220), (236, 300, 220),  # clean run
        (236 + 250, 300, 230),                              # 1 severe jump: dist=250
        (236 + 268, 312, 230),
    ]
    rm1 = build_stroke_page([stroke1])

    page2_uuid = "20000000-0000-4000-8000-0000000000a2"
    stroke2 = [
        (200, 900, 220), (215, 915, 220),                    # clean run
        (215 + 180, 915, 225),                               # severe jump #1: dist=180
        (215 + 180 + 15, 930, 225),
        (215 + 180 + 15 + 200, 930, 210),                    # severe jump #2: dist=200
        (215 + 180 + 15 + 200 + 12, 942, 210),
    ]
    rm2 = build_stroke_page([stroke2])

    _write_rmdoc_dir(fixture_dir, doc_uuid, page1_uuid, rm1, None, "Neutral shapes (artifact) p1")
    (fixture_dir / doc_uuid / f"{page2_uuid}.rm").write_bytes(rm2)
    content_path = fixture_dir / f"{doc_uuid}.content"
    content = json.loads(content_path.read_text(encoding="utf-8"))
    content["pageCount"] = 2
    content["cPages"]["pages"].append(
        {"id": page2_uuid, "idx": {"timestamp": "0", "value": {"rindex": -1}}})
    content["cPages"]["uuids"].append({"timestamp": "0", "value": page2_uuid})
    content_path.write_text(json.dumps(content, indent=2), encoding="utf-8")

    return {
        "doc_uuid": doc_uuid,
        "pages": {1: (page1_uuid, [stroke1]), 2: (page2_uuid, [stroke2])},
    }


LOREM = [
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
    "Sed do eiusmod tempor incididunt ut labore et dolore.",
    "Ut enim ad minim veniam, quis nostrud exercitation.",
    "Duis aute irure dolor in reprehenderit in voluptate.",
    "Excepteur sint occaecat cupidatat non proident, sunt.",
]


def build_lorem_pdf_fixture(fixture_dir: Path) -> dict:
    """One PDF-backed page: synthetic lorem-ipsum text plus one small clean
    annotation mark placed via the PDF_RM_SCALE affine relationship (rmscene
    = scale * PDF_point + (tx, ty), rm_render_page.detect_scene_scale) so the
    render path that constant feeds is exercised end to end."""
    doc_uuid = "30000000-0000-4000-8000-000000000001"
    page_uuid = "30000000-0000-4000-8000-0000000000a1"

    page_w, page_h = 612.0, 792.0
    pdf_bytes = build_lorem_pdf(LOREM, page_w=page_w, page_h=page_h)

    # Small tight "dot" mark near the middle of the page, in rmscene units,
    # derived from the documented PDF-backed transform: sx = scale*(px - w/2),
    # sy = scale*py, where (px, py) is measured in canvas/image space (origin
    # top-left, y down -- what render_pdf_page actually rasterizes).
    target_px, target_py = page_w / 2.0, 400.0
    sx = PDF_RM_SCALE * (target_px - page_w / 2.0)
    sy = PDF_RM_SCALE * target_py
    stroke = [
        (sx - 5, sy, 220), (sx, sy + 4, 220), (sx + 5, sy, 220),  # clean tight mark
    ]
    rm_bytes = build_stroke_page([stroke])

    _write_rmdoc_dir(fixture_dir, doc_uuid, page_uuid, rm_bytes, pdf_bytes,
                      "Neutral lorem PDF p1")

    return {
        "doc_uuid": doc_uuid,
        "pages": {1: (page_uuid, [stroke])},
        "pdf_page_size": (page_w, page_h),
        "mark_target_px_py": (target_px, target_py),
    }


def _labels_entry(rel_path: str, cls: str, built: dict, has_pdf: bool,
                   render_profile: str = "golden") -> dict:
    from rm_render_page import detect_stroke_merge

    pages_out = {}
    for page_no, (page_uuid, strokes) in built["pages"].items():
        blocks = []
        # Re-derive blocks by re-reading the just-written .rm so the recorded
        # expectations come from the exact bytes on disk, not the in-memory
        # stroke list (catches a serialize/parse round-trip bug too).
        fixture_dir = FIXTURES_ROOT / rel_path
        rm_path = fixture_dir / built["doc_uuid"] / f"{page_uuid}.rm"
        blocks = rp.parse_rm_blocks(rm_path)
        m = detect_stroke_merge(blocks)
        pages_out[str(page_no)] = {
            "rm": page_uuid,
            "expected_has_artifact": m["has_artifact"],
            "expected_severe_count": m["severe_count"],
            "expected_max_jump_approx": round(m["max_jump"], 1),
        }
    entry = {
        "path": rel_path,
        "class": cls,
        "doc_uuid": built["doc_uuid"],
        "has_pdf": has_pdf,
        "render_profile": render_profile,
        "pages": pages_out,
        "provenance": "synthetic -- generated by rm-mcp/tests/gen_neutral_fixtures.py, "
                       "no personal content. See NEUTRAL_PROVENANCE.md.",
    }
    return entry


def mint_goldens(fixture_dir: Path) -> None:
    out_dir = fixture_dir / GOLDENS_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    render_cli = tool_script(TOOLS_DIR, "rm_render_page.py")
    proc = subprocess.run(
        [sys.executable, str(render_cli), str(fixture_dir),
         "--out", str(out_dir), "--supersample", "2"],
        capture_output=True, encoding="utf-8", errors="replace", timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"render failed for {fixture_dir}:\n{proc.stdout}\n{proc.stderr}")
    # rm_render_page writes page_NNN.png; rename to the golden suffix the
    # test suite expects.
    for png in out_dir.glob("page_*.png"):
        if png.name.endswith(".golden.png"):
            continue
        target = out_dir / (png.stem + ".golden.png")
        png.replace(target)


def main() -> int:
    if FIXTURES_ROOT.exists():
        import shutil
        shutil.rmtree(FIXTURES_ROOT)
    FIXTURES_ROOT.mkdir(parents=True)

    shapes_clean_dir = FIXTURES_ROOT / "shapes_clean" / "hexagon_and_grid"
    shapes_artifact_dir = FIXTURES_ROOT / "shapes_artifact" / "engineered_jumps"
    lorem_pdf_dir = FIXTURES_ROOT / "pdf_paper" / "lorem"

    built_clean = build_shapes_clean(shapes_clean_dir)
    built_artifact = build_shapes_artifact(shapes_artifact_dir)
    built_pdf = build_lorem_pdf_fixture(lorem_pdf_dir)

    for d in (shapes_clean_dir, shapes_artifact_dir, lorem_pdf_dir):
        mint_goldens(d)

    labels = {
        "schema": 1,
        "minted_by": "rm-mcp/tests/gen_neutral_fixtures.py",
        "provenance": "Fully synthetic. No personal content -- see NEUTRAL_PROVENANCE.md.",
        "detector": "tools/rm_render_page.py detect_stroke_merge",
        "default_thresholds": {"jump": 60.0, "pressure": 189},
        "pdf_rm_scale": PDF_RM_SCALE,
        "fixtures": [
            _labels_entry("shapes_clean/hexagon_and_grid", "clean_native", built_clean, False),
            _labels_entry("shapes_artifact/engineered_jumps", "artifact_native", built_artifact, False),
            _labels_entry("pdf_paper/lorem", "pdf_paper", built_pdf, True),
        ],
    }
    (FIXTURES_ROOT / "labels.json").write_text(
        json.dumps(labels, indent=2) + "\n", encoding="utf-8")

    total_bytes = sum(f.stat().st_size for f in FIXTURES_ROOT.rglob("*") if f.is_file())
    print(f"Minted neutral fixtures under {FIXTURES_ROOT}")
    print(f"Total size: {total_bytes / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
