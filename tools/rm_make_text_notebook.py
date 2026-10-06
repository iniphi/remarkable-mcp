"""
rm_make_text_notebook.py — create a native reMarkable Type Folio notebook from text.

Markdown-style paragraph detection:
    # text        -> heading
    ## text       -> heading
    - text        -> bullet
      - text      -> bullet2 (2-space indent)
    **text**      -> bold (whole line wrapped in **)
    (anything else) -> plain

Usage:
    python tools/rm_make_text_notebook.py \\
        --title "Ch1 writing session" \\
        --input content.md \\
        --out tools/rm_workspace/ch1_write.rmdoc

    python tools/rm_make_text_notebook.py \\
        --title "Quick note" \\
        --text "# Heading\nSome text" \\
        --out out.rmdoc \\
        --push \\
        --device-dir "/100_thesis/Projects/Poetics/Ch1"

    python tools/rm_make_text_notebook.py \\
        --title "Test" \\
        --text "# Hello\nWorld" \\
        --out /tmp/test.rmdoc \\
        --roundtrip   # parse back and print extracted text
"""

import argparse
import io
import json
import os
import re
import sys
import time
import uuid
import zipfile
from pathlib import Path

from rmscene import (
    AuthorIdsBlock,
    CrdtId,
    CrdtSequence,
    CrdtSequenceItem,
    LwwValue,
    MigrationInfoBlock,
    PageInfoBlock,
    RootTextBlock,
    SceneGroupItemBlock,
    SceneInfo,
    SceneTreeBlock,
    TreeNodeBlock,
    read_blocks,
    write_blocks,
)
import rmscene.scene_items as si

from rm_config import cli_main, run_rmapi

# The tools/ ROOT in either layout. The .py files moved into buckets in the
# 2026-09-20 split; the DATA beside them -- prompts/, rm_workspace/,
# zotero_plans/, litgather_plans/, the .json manifests -- did not. So a bare
# Path(__file__).parent here points one level too deep and silently names a
# directory that does not exist.
_TOOLS_ROOT_DIR = (lambda _p: _p.parent
                     if _p.name in ("rm", "zotero", "litgather", "common")
                     else _p)(Path(__file__).resolve().parent)


STYLE_MAP: dict[str, si.ParagraphStyle] = {
    "heading": si.ParagraphStyle.HEADING,
    "bold": si.ParagraphStyle.BOLD,
    "bullet": si.ParagraphStyle.BULLET,
    "bullet2": si.ParagraphStyle.BULLET2,
    "plain": si.ParagraphStyle.PLAIN,
}

TEXT_START_SEQ = 16
STYLE_TIMESTAMP_SEQ = 15

# -- notebook templates -------------------------------------------------------
# What a freshly created notebook opens as: page margin, the pen the toolbar
# has selected with its size and colour, the page background. The values are
# the .content fields the device itself writes, read off three real notebooks
# on 2026-09-10: extraMetadata.LastPen "Finelinerv2", LastFinelinerv2Size "2"
# (a STRING, not an int), LastFinelinerv2Color "Black"; margins 125 on both
# notebooks whose margin had been changed by hand. Two things stay unverified
# until a pushed notebook is opened on a device: that 125 is the margin
# picker's "small", and that a fresh upload's extraMetadata is honoured as the
# initial pen rather than the device's last-used tool. Either way the worst
# case is one tap on the toolbar. A page background is recorded per page under
# cPages as {"template": {"timestamp": "1:N", "value": "<name>"}} -- read off
# the same three notebooks ("P Lines small", "P Dots large", "Blank"), so the
# shape is verified. Absent means the device default (blank). The names are
# the device's own: "P Lines small|medium|large", "P Dots small|large",
# "P Grid small|medium|large", "Blank"; "P" is portrait, "LS" landscape.
#
# tools/rm_templates.local.json (gitignored, never shipped) overlays or adds
# templates by name, so a personal preference never has to live in this file.
TEMPLATES: dict[str, dict] = {
    "default": {
        "description": "small margin, fineliner medium black, title typed on page 1",
        "margins": 125,
        "extra_metadata": {
            "LastPen": "Finelinerv2",
            "LastTool": "Finelinerv2",
            "LastActiveTool": "primary",
            "LastFinelinerv2Color": "Black",
            "LastFinelinerv2Size": "2",
        },
        "page_template": None,
        "heading": True,
    },
    "lined": {
        "description": "as default, on lined paper (P Lines small)",
        "margins": 125,
        "extra_metadata": {
            "LastPen": "Finelinerv2",
            "LastTool": "Finelinerv2",
            "LastActiveTool": "primary",
            "LastFinelinerv2Color": "Black",
            "LastFinelinerv2Size": "2",
        },
        "page_template": "P Lines small",
        "heading": True,
    },
    "plain": {
        "description": "device defaults, nothing pre-set, title typed on page 1",
        "margins": None,
        "extra_metadata": {},
        "page_template": None,
        "heading": True,
    },
}
TEMPLATES_LOCAL = _TOOLS_ROOT_DIR / "rm_templates.local.json"


def load_templates() -> dict[str, dict]:
    """The shipped templates, overlaid with tools/rm_templates.local.json.

    A local entry with a known name overrides field by field; an unknown name
    starts from "plain". A malformed file raises ValueError naming it, rather
    than silently falling back to defaults the user thought they had changed.
    """
    merged = {name: dict(spec) for name, spec in TEMPLATES.items()}
    if not TEMPLATES_LOCAL.is_file():
        return merged
    try:
        local = json.loads(TEMPLATES_LOCAL.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{TEMPLATES_LOCAL}: unreadable ({exc})") from exc
    if not isinstance(local, dict):
        raise ValueError(f"{TEMPLATES_LOCAL}: expected an object of named templates")
    for name, spec in local.items():
        if not isinstance(spec, dict):
            raise ValueError(f"{TEMPLATES_LOCAL}: template {name!r} must be an object")
        base = dict(merged.get(name, TEMPLATES["plain"]))
        base.update(spec)
        merged[name] = base
    return merged


def parse_paragraphs(md_text: str) -> list[dict]:
    paragraphs = []
    for line in md_text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("## "):
            paragraphs.append({"text": line[3:], "style": "heading"})
        elif line.startswith("# "):
            paragraphs.append({"text": line[2:], "style": "heading"})
        elif line.startswith("  - ") or line.startswith("  * "):
            paragraphs.append({"text": line[4:], "style": "bullet2"})
        elif line.startswith("- ") or line.startswith("* "):
            paragraphs.append({"text": line[2:], "style": "bullet"})
        elif re.match(r"^\*\*.+\*\*$", line):
            paragraphs.append({"text": line[2:-2], "style": "bold"})
        else:
            paragraphs.append({"text": line, "style": "plain"})
    return paragraphs


def build_rm_page(paragraphs: list[dict], author_uuid: uuid.UUID) -> bytes:
    author = 1
    seq = TEXT_START_SEQ
    items: list[CrdtSequenceItem] = []
    styles: dict[CrdtId, LwwValue] = {}
    prev_last_seq: int | None = None
    total_chars = 0
    total_lines = 0

    for i, para in enumerate(paragraphs):
        text = para["text"]
        style_val = STYLE_MAP.get(para.get("style", "plain"), si.ParagraphStyle.PLAIN)
        full_text = text + "\n"
        n = len(full_text)
        start_id = CrdtId(author, seq)

        if i == 0:
            style_key = CrdtId(0, 0)
            style_ts = CrdtId(author, STYLE_TIMESTAMP_SEQ)
        else:
            # Style key = the \n character that ended the previous paragraph.
            # TextDocument.from_scene_item pops that \n as the paragraph's start_id,
            # then looks it up in styles. seq - 1 is that \n's position.
            style_key = CrdtId(author, seq - 1)
            style_ts = CrdtId(author, seq - 1)

        styles[style_key] = LwwValue(style_ts, style_val)

        left_id = CrdtId(0, 0) if prev_last_seq is None else CrdtId(author, prev_last_seq)
        items.append(
            CrdtSequenceItem(
                item_id=start_id,
                left_id=left_id,
                right_id=CrdtId(0, 0),
                deleted_length=0,
                value=full_text,
            )
        )

        prev_last_seq = seq + n - 1
        seq += n
        total_chars += n
        total_lines += 1

    text_obj = si.Text(
        items=CrdtSequence(items),
        styles=styles,
        pos_x=-468.0,
        pos_y=234.0,
        width=936.0,
    )

    scene_inf = SceneInfo(
        current_layer=LwwValue(CrdtId(0, 0), CrdtId(0, 0)),
        background_visible=LwwValue(CrdtId(0, 0), True),
        root_document_visible=LwwValue(CrdtId(0, 0), True),
        paper_size=(1404, 1872),
    )

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

    blocks = [
        AuthorIdsBlock(author_uuids={author: author_uuid}),
        MigrationInfoBlock(migration_id=CrdtId(author, 1), is_device=True),
        PageInfoBlock(
            loads_count=1,
            merges_count=0,
            text_chars_count=total_chars,
            text_lines_count=total_lines,
            type_folio_use_count=1,
        ),
        scene_inf,
        SceneTreeBlock(
            tree_id=CrdtId(0, 11),
            node_id=CrdtId(0, 0),
            is_update=True,
            parent_id=CrdtId(0, 1),
        ),
        RootTextBlock(block_id=CrdtId(0, 0), value=text_obj),
        root_group,
        layer1_group,
        scene_group,
    ]

    buf = io.BytesIO()
    write_blocks(buf, blocks)
    return buf.getvalue()


def build_rmdoc(title: str, rm_bytes: bytes, dest_path: str, parent: str = "",
                *, margins: int | None = 180,
                extra_metadata: dict | None = None,
                page_template: str | None = None,
                quiet: bool = False) -> None:
    """Zip one .rm page into a device-ready .rmdoc.

    margins / extra_metadata / page_template come from a TEMPLATES entry (see
    load_templates). margins=None omits the key, which is how the device
    itself records an untouched margin. The 180 default predates the
    templates and is kept so existing callers build byte-for-byte what they
    always did.
    """
    doc_uuid = str(uuid.uuid4())
    page_uuid = str(uuid.uuid4())
    ts_ms = int(time.time() * 1000)

    page: dict = {
        "id": page_uuid,
        "idx": {"timestamp": "0", "value": {"rindex": -1}},
    }
    if page_template:
        page["template"] = {"timestamp": "1:1", "value": page_template}

    content = {
        "coverPageNumber": 0,
        "documentMetadata": {},
        "extraMetadata": dict(extra_metadata or {}),
        "fileType": "notebook",
        "fontName": "",
        "formatVersion": 2,
        "lineHeight": -1,
        "orientation": "portrait",
        "pageCount": 1,
        "cPages": {
            "lastOpened": {"timestamp": "0", "value": page_uuid},
            "pages": [page],
            "uuids": [{"timestamp": "0", "value": page_uuid}],
        },
        "pageTags": [],
        "sizeInBytes": "0",
        "tags": [],
        "textAlignment": "justify",
        "textScale": 1,
        "keyboardMetadata": {"count": 1, "timestamp": ts_ms},
        "zoomMode": "bestFit",
    }
    if margins is not None:
        content["margins"] = margins

    metadata = {
        "createdTime": str(ts_ms),
        "lastModified": str(ts_ms),
        "lastOpened": "0",
        "lastOpenedPage": 0,
        "pinned": False,
        "type": "DocumentType",
        "visibleName": title,
        "parent": parent,
    }

    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{doc_uuid}/{page_uuid}.rm", rm_bytes)
        z.writestr(f"{doc_uuid}.content", json.dumps(content, indent=2))
        z.writestr(f"{doc_uuid}.metadata", json.dumps(metadata, indent=2))

    if not quiet:
        print(f"Written: {dest_path} ({os.path.getsize(dest_path)} bytes)")


def roundtrip_check(rmdoc_path: str) -> None:
    """Parse the generated .rmdoc back and print extracted text."""
    import sys
    import tempfile

    sys.path.insert(0, str(Path(__file__).parent))
    from rm_extract_text import extract_typed_text

    # extract_typed_text reads an unzipped .rmdoc dir, not the .rmdoc archive,
    # so unpack into a temp dir first (the .rmdoc is a plain zip).
    with tempfile.TemporaryDirectory(prefix="rm_roundtrip_") as tmp:
        with zipfile.ZipFile(rmdoc_path) as zf:
            zf.extractall(tmp)
        pages = extract_typed_text(Path(tmp))
    print("\n--- Round-trip text extraction ---")
    for page in pages:
        print(f"Page {page['pdf_page']}:")
        for para in page["paragraphs"]:
            style = para["style"]
            text = para["text"].rstrip("\n")
            print(f"  [{style}] {text!r}")
    print("---")


def push_rmdoc(rmdoc_path: str, device_dir: str, dry_run: bool = False) -> None:
    if dry_run:
        print(f"[dry-run] mkdir {device_dir}")
        print(f"[dry-run] put {rmdoc_path} -> {device_dir}")
        return

    # Through run_rmapi, not a bare subprocess: the shared lock and the 429
    # cooldown only hold if every rmapi call takes the same path.
    print(f"mkdir {device_dir}")
    run_rmapi("mkdir", device_dir, check=False)
    print(f"put {rmdoc_path} -> {device_dir}")
    result = run_rmapi("put", rmdoc_path, device_dir, check=False)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
    else:
        print("  OK")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a native reMarkable Type Folio notebook")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", "-i", help="Markdown file to read paragraphs from")
    src.add_argument("--text", "-t", help="Inline text (use \\n for newlines)")

    parser.add_argument("--title", required=True, help="Notebook title shown on device")
    parser.add_argument("--out", required=True, help="Output .rmdoc path")
    parser.add_argument("--push", action="store_true", help="Push to device after writing")
    parser.add_argument("--device-dir", default="", help="Device directory for --push")
    parser.add_argument("--dry-run", action="store_true", help="Print push commands without running")
    parser.add_argument("--roundtrip", action="store_true", help="Parse back and print extracted text")
    parser.add_argument("--template", default=None,
                        help="Notebook template (margin, pen, background) -- see --list-templates")
    parser.add_argument("--list-templates", action="store_true",
                        help="Print the available templates and exit")
    if "--list-templates" in sys.argv[1:]:
        # Listing needs none of the required arguments, so answer before
        # argparse can demand them.
        for name, spec in sorted(load_templates().items()):
            print(f"{name}: {spec.get('description', '')}")
        return
    args = parser.parse_args()

    template = None
    if args.template:
        templates = load_templates()
        if args.template not in templates:
            print(f"unknown template {args.template!r}; one of: "
                  f"{', '.join(sorted(templates))}", file=sys.stderr)
            sys.exit(2)
        template = templates[args.template]

    if args.input:
        with open(args.input, encoding="utf-8") as f:
            md_text = f.read()
    else:
        md_text = args.text.replace("\\n", "\n")

    paragraphs = parse_paragraphs(md_text)
    if not paragraphs:
        print("No paragraphs found — aborting.")
        return

    print(f"Parsed {len(paragraphs)} paragraphs")
    author_uuid = uuid.uuid4()
    rm_bytes = build_rm_page(paragraphs, author_uuid)
    print(f"Built .rm page: {len(rm_bytes)} bytes")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    if template is None:
        build_rmdoc(args.title, rm_bytes, args.out)
    else:
        build_rmdoc(args.title, rm_bytes, args.out,
                    margins=template.get("margins"),
                    extra_metadata=template.get("extra_metadata") or {},
                    page_template=template.get("page_template"))

    if args.roundtrip:
        roundtrip_check(args.out)

    if args.push:
        if not args.device_dir:
            print("--push requires --device-dir")
            return
        push_rmdoc(args.out, args.device_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    cli_main(main)
