#!/usr/bin/env python3
"""
rm_reading_ledger.py -- per-page ink fingerprints + delta selection for the drain.

Why this exists
---------------
A long book (Vico's *New Science*) is read over weeks. Every drain re-interpreted
EVERY ink-heavy page, so a second session paid the vision bill again for the pages
read in the first -- and, worse, rebuilt Note 1 from that run's interpretations
alone, so nothing accreted.

The economic fact this exploits: the whole `.rmdoc` (ink vectors only) is fetched
cheaply on every pull; only the VISION pass is metered (~$0.008-0.03/page). So
"pull pages 9-20" really means "interpret only pages 9-20" -- the fetch stays
whole-doc and nothing about the download changes.

Two structures make that work:

  1. **Ledger** (`.rm_state.json` -> `reading_pages[device_path][page_no]`):
     the sha256 of a page's `.rm` bytes at the time it was last interpreted. A
     page whose sha is unchanged has not been written on since; skip it.

  2. **Interpretation cache** (`tools/rm_workspace/reading_cache/<slug>/`): the
     per-page interpretation JSON itself, kept across pulls so the note can be
     rebuilt COMPLETE from cache + this run's new pages. This is what makes the
     analysis accrete rather than be overwritten by the latest delta.

The cache is the source of truth for CONTENT, the ledger for FRESHNESS. A page is
skipped only when both agree (sha matches AND a cached interpretation exists), so
deleting the cache degrades to a full re-interpret rather than to a silently
truncated note. That asymmetry is deliberate: over-spending on vision is an
annoyance, losing prior analysis is not recoverable.

Heavy imports (rmscene via rm_bundle) are deliberately made
inside functions so importing this module stays cheap for callers that only need
the page-spec parser or the ledger accessors.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_TOOLS_DIR = (lambda _p: _p.parent if _p.name in ("rm", "zotero", "litgather", "common") else _p)(Path(__file__).resolve().parent)

# Ledger namespace inside the shared .rm_state.json. Keyed by device_path, then
# by str(page_no) -- JSON object keys are always strings, so every read goes
# through _page_key/_as_page_no rather than assuming int keys survive a round-trip.
LEDGER_KEY = "reading_pages"

# Where per-page interpretation JSONs are kept between pulls. Sits under the
# existing rm_workspace scratch tree (untracked); gitignored explicitly so a
# stray `git add tools/` can never commit a book's worth of interpretations.
READING_CACHE_ROOT = _TOOLS_DIR / "rm_workspace" / "reading_cache"

# Canonical per-page interpretation filename, matching what rm_interpret.py and
# rm_interpret_gemini.py (via --output-suffix) write. rm_write_notes'
# load_interpretations() reads the same pattern, so a cache dir is a drop-in
# substitute for a fresh interpret dir.
INTERPRET_SUFFIX = ".interpret.json"

# The two-stage pipeline (tools/rm_interpret_stages.py, opt-in via --two-stage)
# writes a DIFFERENT suffix by design, precisely so a later reader can tell
# which pipeline produced a page from the filename alone. Duplicated here as a
# literal rather than imported from rm_interpret_stages.DEFAULT_OUTPUT_SUFFIX
# for the same reason _unwrap() below duplicates rm_write_notes'
# _unwrap_interpretation: that module chain pulls in anthropic + pydantic at
# import time, and this module is deliberately importable without them.
TWOSTAGE_INTERPRET_SUFFIX = ".interpret.twostage.json"
_PAGE_FILE_RE = re.compile(
    r"page_0*(\d+)\.interpret(?:\.twostage)?\.json$", re.IGNORECASE
)


def _interpret_files(directory: Path) -> list[Path]:
    """Every per-page interpretation file in `directory`, from EITHER pipeline.

    The cache/ledger accretion contract (module docstring) must not go blind
    to two-stage output just because it lands under a different suffix -- a
    page interpreted via --two-stage still needs to be carried forward on the
    next drain, not silently re-selected (wasteful) or silently dropped from
    the accreted note (the one failure mode this module exists to prevent).
    """
    return list(directory.glob(f"page_*{INTERPRET_SUFFIX}")) + list(
        directory.glob(f"page_*{TWOSTAGE_INTERPRET_SUFFIX}")
    )


# ── page specs ──────────────────────────────────────────────────────────────

def parse_pages(spec: str | None) -> set[int] | None:
    """Parse a '1,3,5-7' page spec into a 1-INDEXED set. None means unset.

    rm_render_page.parse_page_spec() does the same job but returns 0-indexed
    values bounded by a page count; the ledger works in the 1-indexed page
    numbers the drain reports and the user types, and bounds against the pages
    actually present rather than a total. Kept separate (and dependency-free)
    rather than adapted, so importing this module never pulls in PIL/fitz.

    >>> sorted(parse_pages("1,3,5-7"))
    [1, 3, 5, 6, 7]
    >>> parse_pages(None) is None
    True
    """
    if spec is None:
        return None
    out: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk.lstrip("-"):
            start, _, end = chunk.partition("-")
            try:
                lo, hi = int(start), int(end)
            except ValueError as exc:
                raise ValueError(f"bad page range {chunk!r} in spec {spec!r}") from exc
            if lo > hi:
                lo, hi = hi, lo
            out.update(range(lo, hi + 1))
        else:
            try:
                out.add(int(chunk))
            except ValueError as exc:
                raise ValueError(f"bad page number {chunk!r} in spec {spec!r}") from exc
    return {n for n in out if n >= 1}


def format_pages(pages: Iterable[int]) -> str:
    """Render a page set as a compact '1-3, 7, 9-12' string for logs and headings."""
    ordered = sorted(set(pages))
    if not ordered:
        return "none"
    runs: list[tuple[int, int]] = []
    start = prev = ordered[0]
    for n in ordered[1:]:
        if n == prev + 1:
            prev = n
            continue
        runs.append((start, prev))
        start = prev = n
    runs.append((start, prev))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


# ── ink fingerprints ────────────────────────────────────────────────────────

def page_ink_shas(extracted_dir: Path) -> dict[int, str]:
    """Map 1-indexed PDF page -> sha256 of that page's raw `.rm` stroke bytes.

    Pages with no `.rm` file (never touched) are simply absent -- build_page_map
    only yields pages whose stroke file exists, which is exactly the "no ink, no
    fingerprint" semantics the delta selector wants.

    Hashing the raw bytes rather than a parsed stroke summary is deliberate: it
    is cheap, total, and cannot disagree with itself across rmscene versions.
    Its only cost is sensitivity to byte-level rewrites that change no ink, which
    would over-select (re-interpret a page needlessly) -- the safe direction.
    """
    from rm_bundle import build_page_map, find_doc_uuid

    doc_uuid = find_doc_uuid(extracted_dir)
    page_map = build_page_map(extracted_dir, doc_uuid)  # 0-indexed
    out: dict[int, str] = {}
    for page_idx, rm_path in page_map.items():
        try:
            out[page_idx + 1] = hashlib.sha256(rm_path.read_bytes()).hexdigest()
        except OSError:
            continue  # unreadable stroke file: treat as no fingerprint, re-interpret
    return out


# ── ledger accessors (.rm_state.json) ───────────────────────────────────────

def _page_key(page_no: int) -> str:
    return str(int(page_no))


def ledger_for(state: dict[str, Any], device_path: str) -> dict[str, dict[str, Any]]:
    """Read-only view of the ledger for one device path ({} when absent)."""
    return (state.get(LEDGER_KEY) or {}).get(device_path) or {}


def record_pages(
    state: dict[str, Any],
    device_path: str,
    pages: Iterable[int],
    ink_shas: dict[int, str],
    *,
    backend: str,
    interpreted_at: datetime | None = None,
    prompt_version: str | None = None,
    pipeline: str | None = None,
) -> None:
    """Stamp `pages` into the ledger for `device_path`. Mutates `state` in place.

    Mutation (rather than returning a new state) matches the surrounding
    rm_config contract: rm_pull holds one coarse state_lock for a whole run and
    rewrites incrementally via save_state, so a copy-on-write ledger here would
    be discarded by the next save. Callers outside that lock should route through
    rm_config.update_state instead.

    A page with no fingerprint (unreadable / absent `.rm`) is recorded with
    ink_sha None, which never equals a future sha and so always re-selects.

    `prompt_version` is recorded but deliberately NOT part of freshness. Ink is
    what the delta selector reasons about; making a prompt bump re-select would
    silently re-spend the vision bill across the whole corpus the first time
    anyone changed a word of the prompt. Recording it means
    `stale_prompt_pages()` can report the gap and a human can decide.

    `pipeline` ('single-call' or 'two-stage', see tools/rm_interpret_stages.py)
    is likewise recorded but not part of freshness -- purely provenance, so a
    later reader of `.rm_state.json` can tell which path produced a given
    page's interpretation without opening the cached JSON. None (unset) is
    read by callers as 'single-call' for pages recorded before this field
    existed.
    """
    stamp = (interpreted_at or datetime.now(timezone.utc)).isoformat()
    doc_ledger = state.setdefault(LEDGER_KEY, {}).setdefault(device_path, {})
    for page_no in pages:
        doc_ledger[_page_key(page_no)] = {
            "ink_sha": ink_shas.get(page_no),
            "interpreted_at": stamp,
            "backend": backend,
            "prompt_version": prompt_version,
            "pipeline": pipeline,
        }


def stale_prompt_pages(ledger: dict[str, dict[str, Any]],
                       current_version: str) -> list[int]:
    """Pages whose recorded interpretation predates `current_version`.

    Read-only and advisory. A page interpreted under an older prompt is not
    wrong, just older -- but after a prompt change that fixes a systematic miss
    (v2 -> v3 extended directive detection to prose), "older" can mean content
    was never extracted at all. Pages recorded before this field existed have no
    prompt_version and are reported too, since unknown is not current.

    Returns sorted 1-indexed page numbers.
    """
    out = []
    for key, entry in (ledger or {}).items():
        if (entry or {}).get("prompt_version") != current_version:
            try:
                out.append(int(key))
            except (TypeError, ValueError):
                continue
    return sorted(out)


# ── interpretation cache ────────────────────────────────────────────────────

def cache_dir_for(device_path: str, root: Path | None = None) -> Path:
    """Deterministic per-document cache directory.

    Named `<safe-basename>-<sha256[:10] of the full device path>` so two papers
    with the same filename in different collections can never share a cache, and
    a device path Git Bash mangled into a drive-prefixed string still yields a
    directory inside the workspace (the _safe_stem footgun fixed in
    rm_notebook_pull on 2026-06-30).
    """
    base = re.split(r"[\\/]", device_path.strip())[-1]
    base = base.rsplit(":", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")[:60] or "doc"
    digest = hashlib.sha256(device_path.encode("utf-8")).hexdigest()[:10]
    return (root or READING_CACHE_ROOT) / f"{base}-{digest}"


def cached_pages(cache_dir: Path) -> set[int]:
    """1-indexed page numbers that already have a cached interpretation."""
    if not cache_dir.is_dir():
        return set()
    out: set[int] = set()
    for path in _interpret_files(cache_dir):
        match = _PAGE_FILE_RE.search(path.name)
        if match:
            out.add(int(match.group(1)))
    return out


def load_cached(cache_dir: Path) -> dict[int, dict[str, Any]]:
    """Load every cached per-page interpretation. Corrupt files are skipped.

    A corrupt cache entry is dropped rather than raised on: the page is then
    absent from `cached_pages` on the next run too, so the selector re-interprets
    it. Failing the whole drain because one cached JSON went bad would be a worse
    trade.
    """
    out: dict[int, dict[str, Any]] = {}
    if not cache_dir.is_dir():
        return out
    for path in sorted(_interpret_files(cache_dir)):
        match = _PAGE_FILE_RE.search(path.name)
        if not match:
            continue
        try:
            out[int(match.group(1))] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def copy_interpretations(dest_dir: Path, source_dir: Path,
                         pages: Iterable[int]) -> list[int]:
    """Copy the named pages' interpretation JSONs from `source_dir` to `dest_dir`.

    Used in both directions: run scratch -> cache (banking what was just paid
    for) and cache -> run scratch (restoring earlier pages so a manifest built by
    globbing the scratch dir sees the whole document). Returns the pages actually
    copied. Files are copied, never moved, so the source stays intact for the
    rest of the drain chain.
    """
    wanted = set(pages)
    if not wanted or not source_dir.is_dir():
        return []
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: list[int] = []
    for path in _interpret_files(source_dir):
        match = _PAGE_FILE_RE.search(path.name)
        if not match:
            continue
        page_no = int(match.group(1))
        if page_no not in wanted:
            continue
        try:
            shutil.copy2(path, dest_dir / path.name)
        except OSError:
            continue
        copied.append(page_no)
    return sorted(copied)


# ── delta selection ─────────────────────────────────────────────────────────

def select_pages(
    *,
    ink_shas: dict[int, str],
    ink_counts: dict[int, int],
    ledger: dict[str, dict[str, Any]],
    have_cached: set[int],
    ink_threshold: int,
    explicit_pages: set[int] | None = None,
    all_pages: bool = False,
) -> dict[str, Any]:
    """Decide which pages this drain should send through the vision pass.

    Returns a plan dict: `selected`, `skipped_unchanged`, `skipped_thin`,
    `carried` (pages served from cache instead of re-interpreted), and `mode`.

    Three modes, in precedence order:

      explicit (`--pages 9-20`) -- interpret exactly these, ignoring BOTH the
        ledger and the ink threshold. An explicit request is a human overriding
        the heuristics; bounded only to pages that carry ink at all, since a page
        with no `.rm` file has nothing to interpret.
      all (`--all-pages`)       -- ignore the ledger, re-interpret every ink-heavy
        page. Overrides the delta, NOT the ink threshold (a blank page is still
        not worth $0.008); pass --ink-threshold 0 alongside for a true everything.
      delta (default)           -- ink-heavy pages whose fingerprint is new or
        changed since they were last interpreted.

    A page counts as already-done only when the ledger sha matches AND a cached
    interpretation exists, so a wiped cache re-interprets rather than yielding a
    note that silently lost its earlier pages.
    """
    inked = set(ink_shas)
    heavy = {p for p, n in ink_counts.items() if n > ink_threshold}
    thin = inked - heavy

    if explicit_pages is not None:
        selected = explicit_pages & inked
        mode = "explicit"
        skipped_unchanged: set[int] = set()
        skipped_thin: set[int] = set()
    elif all_pages:
        selected = set(heavy)
        mode = "all"
        skipped_unchanged = set()
        skipped_thin = thin
    else:
        done = {
            p for p in heavy
            if ledger.get(_page_key(p), {}).get("ink_sha") == ink_shas.get(p)
            and p in have_cached
        }
        selected = heavy - done
        mode = "delta"
        skipped_unchanged = done
        skipped_thin = thin

    return {
        "mode": mode,
        "selected": sorted(selected),
        "skipped_unchanged": sorted(skipped_unchanged),
        "skipped_thin": sorted(skipped_thin),
        "carried": sorted(have_cached - selected),
    }


def describe_plan(plan: dict[str, Any]) -> str:
    """One-line human summary of a selection plan, for drain stdout."""
    parts = [f"mode={plan['mode']}",
             f"interpret {format_pages(plan['selected'])}"]
    if plan["skipped_unchanged"]:
        parts.append(f"unchanged {format_pages(plan['skipped_unchanged'])}")
    if plan["carried"]:
        parts.append(f"carried from cache {format_pages(plan['carried'])}")
    return "; ".join(parts)


# ── accretion payload (Notion reading page) ─────────────────────────────────

def _unwrap(record: dict[str, Any]) -> dict[str, Any]:
    """rm_interpret writes {schema_version, ..., interpretation: {...}}.

    Duplicated from rm_write_notes._unwrap_interpretation rather than imported:
    that module pulls fitz + rmscene at import time, and this module is
    deliberately importable without them.
    """
    inner = record.get("interpretation")
    return inner if isinstance(inner, dict) else record


def build_reading_section(
    *,
    title: str,
    pages: Iterable[int],
    interpretations: dict[int, dict[str, Any]],
    drained_on: datetime,
    device_path: str | None = None,
) -> str:
    """Render THIS pull's new pages as one dated Markdown section.

    The unit of accretion for the per-book Notion reading page: each drain
    appends one of these, so the page grows section-by-section in reading order
    and no earlier section is ever rewritten. Deliberately covers only the new
    pages -- the full accreted record lives in the Zotero note, which is rebuilt
    from the cache each pull.
    """
    ordered = [p for p in sorted(set(pages)) if p in interpretations]
    lines = [f"### Pages {format_pages(ordered)} — drained {drained_on:%Y-%m-%d}"]
    if not ordered:
        lines.append("")
        lines.append("_No new interpreted pages in this drain._")
        return "\n".join(lines)
    lines.append("")
    lines.append(f"Source: {title}" + (f" (`{device_path}`)" if device_path else ""))
    for page_no in ordered:
        interp = _unwrap(interpretations[page_no])
        lines.append("")
        lines.append(f"**p{page_no}** — {(interp.get('page_summary') or '').strip()}")
        transcription = [
            (entry.get("text") or "").strip()
            for entry in (interp.get("transcription") or [])
            if isinstance(entry, dict) and (entry.get("text") or "").strip()
        ]
        for text in transcription:
            lines.append(f"> {text}")
        for passage in (interp.get("highlighted_passages") or []):
            text = (passage.get("text") or "").strip() if isinstance(passage, dict) else ""
            if text:
                lines.append(f"- Highlighted: {text}")
        for sketch in (interp.get("sketches") or []):
            if not isinstance(sketch, dict):
                continue
            desc = (sketch.get("description") or "").strip()
            extraction = (sketch.get("structured_extraction") or "").strip()
            if desc or extraction:
                lines.append(f"- Sketch ({sketch.get('type') or 'unlabelled'}): "
                             f"{desc}" + (f" — `{extraction}`" if extraction else ""))
        for directive in (interp.get("directives") or []):
            raw = (directive.get("raw_text") or "").strip() if isinstance(directive, dict) else ""
            if raw:
                lines.append(f"- Directive: {raw}")
    return "\n".join(lines)


def reading_page_payload(
    *,
    device_path: str,
    title: str,
    zotero_item_key: str | None,
    plan: dict[str, Any],
    interpretations: dict[int, dict[str, Any]],
    drained_on: datetime,
    backend: str,
    pipeline: str = "single-call",
) -> dict[str, Any]:
    """Everything Claude needs to accrete the per-book Notion reading page.

    The rm_* tools have no Notion access by design (see /rm-pull SKILL.md), so
    the drain emits this into its --json-summary and the skill performs the
    dual-write: append `section_markdown` to the book's reading page, then upsert
    the `workspace.page_registry` row per ForClaude/HUB_WRITEBACK.md.

    `pipeline` ('single-call' default, or 'two-stage') is carried into the
    summary purely as provenance -- see record_pages().
    """
    new_pages = [p for p in plan["selected"] if p in interpretations]
    return {
        "device_path": device_path,
        "title": title,
        "zotero_item_key": zotero_item_key,
        "backend": backend,
        "pipeline": pipeline,
        "drained_on": drained_on.strftime("%Y-%m-%d"),
        "new_pages": new_pages,
        "carried_pages": plan["carried"],
        "total_pages_analysed": sorted(set(interpretations)),
        "section_heading": f"Pages {format_pages(new_pages)} — drained {drained_on:%Y-%m-%d}",
        "section_markdown": build_reading_section(
            title=title, pages=new_pages, interpretations=interpretations,
            drained_on=drained_on, device_path=device_path,
        ),
    }
