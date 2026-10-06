#!/usr/bin/env python3
"""
rm_diff.py -- whole-device activity diff for the reMarkable.

Answers one question: "where have I worked since last time?" -- across the
ENTIRE `/My Files` tree, not just the pushed Reading papers that rm_pull.py
tracks. It walks every document on the device, records modification times, and
reports what is new / changed / gone since the previous run.

Mtimes come from `rmapi ls -l <dir>`, one call per DIRECTORY, not from a `stat`
call per document. A live probe over the whole device (2026-09-07) found 65
directories holding 298 documents, so this is a ~4.6x cut in rmapi calls --
each call does its own device-token -> user-token exchange, which is the 429
multiplier (see STAT_WORKERS below). `rmapi find` is still the one call that
decides existence (unchanged); only the mtime source moved. `ls -l` exposes no
document id and no CurrentPage/Version, so a snapshot entry now carries only
"mtime" -- see save_snapshot for the resulting schema bump, and main.report's
closing note for what that costs the MODIFIED signal.

Rename detection (2026-09-09, restored same day it went dormant): pair_moves()
still pairs a vanished path with an appeared path by document id, but `ls -l`
never supplies one. resolve_move_ids() closes that gap with a HANDFUL of
targeted `rmapi stat <path>` calls -- one each, only for the paths that show
up as NEW or DELETED in a given diff (never the whole device), and only for a
path that doesn't already carry an id. Cost is O(changes), not O(docs): a scan
with no deletions never calls stat at all (see the early return in
resolve_move_ids), and an ordinary session touching a handful of documents
costs a handful of calls, not 188. Verified live 2026-09-09: `rmapi stat
<path>` still returns the document's "ID" field the old per-file stat_doc()
used to read.

Unlike `.rm_state.json` (push/pull baseline, papers only), this keeps its own
rolling snapshot in `tools/.rm_device_snapshot.json`. Each run diffs against the
stored snapshot, prints the delta, then (by default) re-baselines so the next
run reports only what changed after this one.

The Drawings folder (`/Draw`) and `/trash` are ignored by default -- doodles and
deleted items are noise for "what did I actually work on". Override with
--ignore / --no-default-ignore.

Usage:
    python tools/rm_diff.py                      # diff whole device, then re-baseline
    python tools/rm_diff.py --no-update          # preview delta, do NOT move the baseline
    python tools/rm_diff.py --root /104_Stacks   # scope to one subtree
    python tools/rm_diff.py --ignore Draw --ignore trash --ignore "Business Time"
    python tools/rm_diff.py --all                # also list unchanged docs
    python tools/rm_diff.py --out diff.json      # write the change manifest as JSON
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rm_config import (RM_ROOT, RMAPI_BIN, cli_main, load_env, make_warning,
                       run_rmapi)

load_env()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

RMAPI = RMAPI_BIN
# From rm_config, not Path(__file__).parent: the tools/ split (S112) put this
# module in a bucket, and a bare .parent moved the snapshot to
# tools/rm/.rm_device_snapshot.json -- a fresh empty baseline, so the next
# /rm-diff would have reported the whole device as changed. rm_config resolves
# the tools root in both the split tree and the flat public one.
from rm_config import TOOLS_DIR  # noqa: E402
SNAPSHOT_FILE = TOOLS_DIR / ".rm_device_snapshot.json"

# Folders skipped by default: freehand doodles + deleted items (top-level
# name match), plus the auto-generated Calendar month/year pages (path-prefix
# match -- see is_ignored). "Daily Life/Calendar" is the ONE subtree excluded
# by prefix rather than top-level name, because the rest of "Daily Life"
# (Business Time/Locarno notebooks etc.) is real content.
DEFAULT_IGNORE = ("Draw", "trash", "Daily Life/Calendar")

# Every rmapi call is a separate process doing its own device-token -> user-token
# exchange, so listing concurrency is a direct multiplier on requests to the
# token endpoint. At 8 workers the reMarkable cloud answered 253 of 279 (then
# per-document) stats with HTTP 429 (2026-08-24). Keep this low; the walk is
# one call, only the per-directory `ls -l` calls fan out. Renamed in spirit,
# not in name, by the 2026-09-09 ls-l rewrite -- still governs the one knob
# that controls rmapi call concurrency, just for list_dir() instead of the
# stat_doc() this constant used to size.
STAT_WORKERS = 2
STAT_RETRIES = 3

# Targeted stat calls for move-pairing, sized independently of STAT_WORKERS
# because it fires far less often (only on a diff with candidates) but hits
# the same rate-limited token endpoint -- keep it just as conservative.
MOVE_STAT_WORKERS = 2

# Refuse to targeted-stat a diff with more move candidates than this. A
# reorganisation this large is rare and reads more like a bad scan than a
# rename spree; skipping keeps the O(changes) promise from becoming O(docs)
# on the one pathological day it isn't small.
MAX_MOVE_STAT_CANDIDATES = 150

# Refuse to move the baseline when this fraction of the walk could not be read,
# or this fraction of the previous baseline appears to have vanished. Both are
# far more likely to mean "the cloud stopped answering" than "the device
# changed that much", and writing such a scan back destroys the baseline.
MAX_UNREADABLE_FRACTION = 0.10
MAX_DISAPPEARED_FRACTION = 0.25


# ── rmapi wrappers ───────────────────────────────────────────────────────────

def _rmapi(*args: str, check: bool = True, timeout: int = 180) -> subprocess.CompletedProcess:
    """Run rmapi via the shared rm_config wrapper (utf-8, stdin closed,
    MSYS_NO_PATHCONV set). RmapiError is a RuntimeError subclass, so existing
    except-RuntimeError sites are unaffected."""
    return run_rmapi(*args, check=check, timeout=float(timeout))


_FIND_LINE = re.compile(r"^\[([fd])\]\s+(.*)$")


def _norm_device_path(raw: str) -> str:
    """Normalise an rmapi-find path fragment to a clean `/a/b/c` form.

    rmapi find emits backslash paths with inconsistent leading slashes
    (`\\Quick sheets`, `\\\\Travel\\London`). Collapse any run of separators to
    a single `/` and force exactly one leading slash.
    """
    collapsed = re.sub(r"[\\/]+", "/", raw).strip("/")
    return "/" + collapsed if collapsed else "/"


def walk_device(root: str) -> list[str]:
    """Return every document (file) path under `root`, normalised. Dirs dropped."""
    proc = _rmapi("find", root)
    files: list[str] = []
    for line in proc.stdout.splitlines():
        m = _FIND_LINE.match(line.strip())
        if not m:
            continue
        kind, raw = m.group(1), m.group(2)
        if kind != "f":
            continue
        files.append(_norm_device_path(raw))
    return files


def parent_dir(device_path: str) -> str:
    """The directory containing a device file path, for grouping paths into
    one `ls -l` call per directory instead of one `stat` per file."""
    idx = device_path.rfind("/")
    return device_path[:idx] if idx > 0 else "/"


_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}

# `rmapi ls -l` prints a fixed-width date prefix then the name to end of line:
#   "Jul  9 2026  10:17  200_poetics/"        (single-digit day: extra space)
#   "Jun 17 2026  15:51  Merton 1968 - ..."    (a file: no trailing slash)
_LS_LINE = re.compile(
    r"^(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<year>\d{1,4})\s+"
    r"(?P<hour>\d{2}):(?P<min>\d{2})\s+(?P<name>.*)$"
)


def parse_ls_line(line: str) -> tuple[str, str, bool] | None:
    """One `rmapi ls -l` line -> (name, iso_mtime, is_dir), or None if the
    line doesn't match the fixed date-prefix shape (blank lines, banner text).

    Defensive by construction: an unrecognised month, or a day/hour/minute
    out of datetime's valid range (seen live -- `trash/` reports "Jan 1 0001
    01:00"), is treated as unparsable rather than raising. One bad line must
    not crash the whole directory's listing. Titles may contain further
    spaces, en-dashes, non-ASCII, even embedded backslashes (seen live under
    /trash); the name is everything after the fixed prefix, unstripped, so a
    trailing space in a real device filename round-trips exactly.
    """
    m = _LS_LINE.match(line)
    if not m:
        return None
    month = _MONTHS.get(m.group("mon")[:3].title())
    name = m.group("name")
    is_dir = name.endswith("/")
    name = name[:-1] if is_dir else name
    if month is None or not name:
        return None
    try:
        dt = datetime(int(m.group("year")), month, int(m.group("day")),
                      int(m.group("hour")), int(m.group("min")))
    except ValueError:
        return None
    return name, dt.isoformat(), is_dir


def list_dir(device_path: str) -> dict[str, str] | None:
    """`rmapi ls -l <device_path>`, parsed into {filename: iso_mtime} for the
    files directly inside it (subdirectories are dropped -- `find` already
    supplies the directory tree; this only needs to add mtimes).

    Returns None if the listing itself failed. Same UNKNOWN-not-absent
    contract the old per-file `stat_doc` carried, just at directory
    granularity: every file that used to need its own rmapi call now costs
    zero extra ones; only a directory that fails to list is unknown, and
    build_current folds that back into the existing per-file `unreadable`
    bucket unchanged -- every downstream consumer (merge_scan, diff_snapshots,
    baseline_guard) is untouched by this rewrite.
    """
    try:
        proc = run_rmapi("ls", "-l", device_path, check=False,
                         retries=STAT_RETRIES, retry_delay=1.0)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    files: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parsed = parse_ls_line(line)
        if parsed is None or parsed[2]:  # unparsable, or a subdirectory entry
            continue
        name, mtime, _ = parsed
        files[name] = mtime
    return files


def stat_id(device_path: str) -> str | None:
    """Targeted `rmapi stat <path>` for ONE ambiguous move candidate.

    Returns the document's stable ID (rmapi stat's "ID" field), or None if the
    stat failed, timed out, or returned no ID. This is the per-document call
    the 2026-09-09 ls-l rewrite removed from the main walk; resolve_move_ids
    reintroduces it, but only for the small set of paths a diff cannot
    otherwise explain -- never for the whole device.
    """
    try:
        proc = run_rmapi("stat", device_path, check=False,
                         retries=STAT_RETRIES, retry_delay=1.0)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    doc_id = data.get("ID")
    return doc_id or None


# ── snapshot state ───────────────────────────────────────────────────────────

def load_snapshot() -> dict[str, Any]:
    if SNAPSHOT_FILE.exists():
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    return {}


def save_snapshot(root: str, files: dict[str, dict[str, Any]]) -> None:
    """Write the snapshot. SCHEMA BUMPED 1 -> 2 by the 2026-09-09 ls-l rewrite:
    each file entry now carries only "mtime" -- "id"/"current_page"/"version"
    came from `stat` and `ls -l` exposes none of them, so they are simply
    absent going forward rather than written as null. No migration of the old
    schema-1 file is needed: the brief for this rewrite is an explicit one-time
    re-baseline (running the tool for real overwrites every entry in one pass,
    old and new paths alike), not a field-by-field upgrade in place.
    """
    blob = {
        "schema": 2,
        "root": root,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    SNAPSHOT_FILE.write_text(json.dumps(blob, indent=2, ensure_ascii=False),
                             encoding="utf-8")


# ── ignore + grouping ────────────────────────────────────────────────────────

def is_ignored(device_path: str, ignore: tuple[str, ...]) -> bool:
    """True if the path matches an ignore entry.

    A bare name (no "/") matches only the path's first segment, case
    insensitive -- the original behaviour, for excluding a whole top-level
    folder ("Draw", "trash"). A name containing "/" instead matches as a
    path PREFIX, for excluding one nested subtree without dragging in its
    whole top-level parent -- e.g. "Daily Life/Calendar" without ignoring
    the rest of "Daily Life".

    Why "Daily Life/Calendar" needs this: confirmed live 2026-09-10 that
    `rmapi find`'s own path separator is backslash (even `find /00_Projects`
    prints `00_Projects\\203_lightroom\\...`), but find ALSO escapes a literal
    "/" inside one filename as "\\" -- the same character. The Calendar
    MM/YY entries are single flat files whose real name literally contains a
    "/" (`ls -l "/Daily Life/Calendar"` shows one file named "04/25", not a
    directory "04" containing a file "25"). find's output for one of these,
    `Calendar\\04\\25`, is therefore indistinguishable by string parsing alone
    from a genuine two-level path, so _norm_device_path split it into a
    nonexistent "/Daily Life/Calendar/04" directory, and every run's `ls -l`
    on that fake directory failed with "no matches for '04'" -- deterministically,
    not as cloud flakiness. That was ~12 of every run's "unreadable" count.
    Excluding the subtree sidesteps the ambiguity rather than teaching every
    path-handling function here (parent_dir, top_area, the by-name lookup in
    build_current) to tell a real separator from an escaped literal one.
    """
    stripped = device_path.strip("/")
    first = stripped.split("/", 1)[0].lower()
    low = stripped.lower()
    for name in ignore:
        name_low = name.strip("/").lower()
        if "/" in name_low:
            if low == name_low or low.startswith(name_low + "/"):
                return True
        elif first == name_low:
            return True
    return False


def top_area(device_path: str) -> str:
    """A human grouping label: the leading segment(s) under RM_ROOT, else top dir."""
    parts = device_path.strip("/").split("/")
    rm_top = RM_ROOT.strip("/")
    if parts and parts[0] == rm_top and len(parts) >= 2:
        return f"{rm_top}/{parts[1]}"          # e.g. 104_Stacks/Reading
    return parts[0] if parts else "/"


# ── diff ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DeviceScan:
    """One walk of the device, keeping readability separate from existence.

    `walked` is the authoritative answer to "what is on the device" -- it comes
    from a single `rmapi find`. `readable` is only the subset whose metadata we
    also managed to fetch. `unreadable` is walked-but-unstattable: those
    documents exist, we just do not know their mtime this run.
    """
    walked: tuple[str, ...]
    readable: dict[str, dict[str, Any]]
    unreadable: tuple[str, ...]


def build_current(root: str, ignore: tuple[str, ...],
                  verbose: bool = False,
                  workers: int = STAT_WORKERS) -> DeviceScan:
    """Walk the device (via `find`, unchanged -- this is still the sole
    authority on existence), then fetch mtimes with one `ls -l` call PER
    DIRECTORY instead of one `stat` call per document.

    Historically this returned only the stat-able documents, which silently
    conflated "we could not read it" with "it is gone" and let a run of cloud
    429s be reported as mass deletion. It still returns all three
    DeviceScan sets so callers keep that distinction -- only the source of
    the mtime data changed; a directory whose listing fails makes every file
    grouped under it unreadable, exactly as an individual stat failure used
    to.

    Some find entries (e.g. auto-generated `/Daily Life/Calendar/MM/YY` pages)
    are genuinely not syncable documents; they simply won't appear in their
    parent directory's `ls -l` output and land in `unreadable` too, which is
    harmless -- unreadable entries are carried forward, not diffed.
    """
    paths = [p for p in walk_device(root) if not is_ignored(p, ignore)]
    by_dir: dict[str, list[str]] = {}
    for p in paths:
        by_dir.setdefault(parent_dir(p), []).append(p)
    dirs = sorted(by_dir)
    print(f"[walk] {len(paths)} document(s) under {root} across {len(dirs)} "
          f"director{'y' if len(dirs) == 1 else 'ies'} "
          f"(ignoring {', '.join(ignore) or 'nothing'})")

    listings: dict[str, dict[str, str] | None] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for dir_path, listing in zip(dirs, pool.map(list_dir, dirs)):
            listings[dir_path] = listing

    readable: dict[str, dict[str, Any]] = {}
    unreadable: list[str] = []
    for dir_path, dir_paths in by_dir.items():
        listing = listings[dir_path]
        # `find` and `ls -l` disagree about surrounding whitespace in a name:
        # find trims it, ls -l preserves it. A document literally called
        # "Wasgenring " (trailing space, made on the tablet) therefore arrived
        # as "Wasgenring" from the walk and never matched its own listing
        # entry -- so it was marked unreadable on EVERY run, and unreadable
        # entries are carried forward unchanged rather than diffed. That is
        # permanent invisibility, not a transient miss: work in that notebook
        # could never surface, and the count stayed at a reassuring "1
        # unreadable". Found 2026-09-20 by checking what the 1 actually was.
        #
        # Exact match still wins, so two names differing only by whitespace
        # cannot be confused with each other; the stripped index is only
        # consulted when the exact name is absent, and only when it resolves
        # to exactly one candidate.
        stripped_index: dict[str, list[str]] = {}
        for key in (listing or {}):
            stripped_index.setdefault(key.strip(), []).append(key)
        for path in dir_paths:
            name = path.rsplit("/", 1)[-1]
            mtime = listing.get(name) if listing is not None else None
            if mtime is None and listing is not None:
                candidates = stripped_index.get(name.strip(), [])
                if len(candidates) == 1:
                    mtime = listing[candidates[0]]
                    if verbose:
                        print(f"  [~] whitespace-matched: {path} -> "
                              f"{candidates[0]!r}", file=sys.stderr)
            if mtime is None:
                unreadable.append(path)
                if verbose:
                    print(f"  [?] could not read: {path}", file=sys.stderr)
            else:
                readable[path] = {"mtime": mtime}
    print(f"[list] {len(dirs)} director{'y' if len(dirs) == 1 else 'ies'} "
          f"read -- {len(readable)} readable document(s), "
          f"{len(unreadable)} unreadable")
    if unreadable and not verbose:
        print("  [?] unreadable entries are carried forward unchanged, never "
              "reported as deleted. Use --verbose to list.")
    return DeviceScan(walked=tuple(paths), readable=readable,
                      unreadable=tuple(unreadable))


def merge_scan(scan: DeviceScan,
               prev: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The snapshot to diff and persist: readable metadata, plus each
    unreadable document's PRIOR record carried forward verbatim.

    Carrying the old record forward is what makes an unreadable document a
    no-op: it matches its own baseline, so it cannot surface as modified, and
    it is present, so it cannot surface as deleted. An unreadable document with
    no prior record is omitted entirely -- we know nothing about it, so it stays
    out of the baseline and gets picked up as new on a healthier run.
    """
    merged = dict(scan.readable)
    for path in scan.unreadable:
        if path in prev:
            merged[path] = dict(prev[path])
    return merged


def resolve_move_ids(new: list[dict[str, Any]],
                     deleted: list[dict[str, Any]],
                     prev: dict[str, dict[str, Any]],
                     current: dict[str, dict[str, Any]],
                     workers: int = MOVE_STAT_WORKERS,
                     ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Fetch document ids for move-pairing, via targeted `rmapi stat`, ONLY for
    the paths a diff cannot otherwise explain.

    A move needs a vanished path AND an appeared path, so this costs zero
    rmapi calls whenever either side of `new` / `deleted` is empty -- the
    common case, since most sessions only add or only touch documents. Among
    the candidates that remain, a path that already carries an id (an old
    schema-1 baseline, or a caller that supplies one directly -- every pinned
    test in test_rm_diff_moves.py does this) is never stat-ed again.

    Returns new (prev, current) dicts with "id" merged in for the resolved
    candidates -- the originals are never mutated, per this repo's
    immutability convention. Returns the ORIGINAL prev/current objects
    unchanged, and makes no rmapi call at all, when there is nothing to
    resolve -- so a caller can pass resolve_moves=True unconditionally without
    it ever being observable on a no-op diff.
    """
    if not new or not deleted:
        return prev, current

    def _needs_id(path: str, source: dict[str, dict[str, Any]]) -> bool:
        return not (source.get(path) or {}).get("id")

    new_candidates = sorted({r["path"] for r in new if _needs_id(r["path"], current)})
    deleted_candidates = sorted({r["path"] for r in deleted if _needs_id(r["path"], prev)})
    all_candidates = new_candidates + deleted_candidates
    if not all_candidates:
        return prev, current
    if len(all_candidates) > MAX_MOVE_STAT_CANDIDATES:
        print(f"[moves] {len(all_candidates)} move candidate(s) exceeds the "
              f"targeted-stat cap ({MAX_MOVE_STAT_CANDIDATES}) -- skipping "
              f"rename resolution this run; renames will show as delete+new.",
              file=sys.stderr)
        return prev, current

    ids: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for path, doc_id in zip(all_candidates, pool.map(stat_id, all_candidates)):
            ids[path] = doc_id

    def _merge(source: dict[str, dict[str, Any]],
              paths: list[str]) -> dict[str, dict[str, Any]]:
        merged = dict(source)
        for path in paths:
            doc_id = ids.get(path)
            if doc_id:
                merged[path] = {**merged.get(path, {}), "id": doc_id}
        return merged

    return _merge(prev, deleted_candidates), _merge(current, new_candidates)


def pair_moves(new: list[dict[str, Any]],
               deleted: list[dict[str, Any]],
               prev: dict[str, dict[str, Any]],
               current: dict[str, dict[str, Any]],
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Re-pair delete-at-old + new-at-new into a single MOVE, matched by doc id.

    The snapshot is keyed by path, so any device-side rename or reorganisation
    reads as mass deletion plus mass creation -- the exact shape that made the
    2026-08-24 run look like data loss. rmapi's stat carries a stable document
    ID, so when a vanished path and an appeared path carry the same one, the
    document did not go anywhere; only its name did.

    Conservative by construction: pairing requires an id on BOTH sides and an
    unambiguous 1:1 match. Entries with no id fall through to the old
    new/deleted behaviour rather than being guessed at -- which matters because
    the 163 records restored during the 2026-08-24 recovery kept only path +
    mtime, so they have no id until the next clean baseline.

    RESTORED (2026-09-09, same day it went dormant): `rmapi ls -l` still
    exposes no document id on its own -- build_current() supplies none, same
    as the moment this went dormant -- but diff_snapshots() now calls
    resolve_move_ids() first, which fetches an id via a handful of targeted
    `rmapi stat` calls for exactly the paths this function's by-id index would
    otherwise miss. This function itself is unchanged: it still only pairs an
    id present on BOTH sides, never guesses. See resolve_move_ids() and the
    module docstring for the O(changes)-not-O(docs) targeting.

    Returns (moved, remaining_new, remaining_deleted).
    """
    by_id: dict[str, list[dict[str, Any]]] = {}
    for rec in new:
        doc_id = (current.get(rec["path"]) or {}).get("id")
        if doc_id:
            by_id.setdefault(doc_id, []).append(rec)

    moved: list[dict[str, Any]] = []
    still_deleted: list[dict[str, Any]] = []
    claimed: set[int] = set()

    for rec in deleted:
        prev_meta = prev.get(rec["path"]) or {}
        doc_id = prev_meta.get("id")
        candidates = by_id.get(doc_id) if doc_id else None
        # Two live paths sharing one id is stranger than a move; leave it alone.
        if not candidates or len(candidates) != 1 or id(candidates[0]) in claimed:
            still_deleted.append(rec)
            continue
        dest = candidates[0]
        claimed.add(id(dest))
        dest_mtime = (current.get(dest["path"]) or {}).get("mtime")
        moved.append({
            "from": rec["path"],
            "to": dest["path"],
            "mtime": dest_mtime,
            "prev_mtime": prev_meta.get("mtime"),
            # A move that also changed mtime was renamed AND written in; a move
            # that did not is purely a reorganisation and is not "worked on".
            "edited": bool(dest_mtime and dest_mtime != prev_meta.get("mtime")),
            "area": top_area(dest["path"]),
        })

    still_new = [r for r in new if id(r) not in claimed]
    return moved, still_new, still_deleted


def diff_snapshots(prev: dict[str, dict[str, Any]],
                   current: dict[str, dict[str, Any]],
                   walked: set[str] | None = None,
                   *,
                   resolve_moves: bool = False,
                   move_workers: int = MOVE_STAT_WORKERS,
                   ) -> dict[str, list[dict[str, Any]]]:
    """Diff a merged scan against the prior baseline.

    `walked` is the set of paths `rmapi find` actually returned. When given, it
    -- and only it -- decides deletion: a document is gone because the device
    stopped listing it, never because a stat call failed. Omit it only for
    callers that have no walk (tests comparing two snapshots directly).

    `resolve_moves` gates the live `rmapi stat` fallback in resolve_move_ids():
    OFF by default, so calling this function never makes a real rmapi call
    just because a caller (a synthetic test fixture, an old schema-1 baseline)
    already carries ids. main() is the one caller that opts in, since it is
    the only one running against a real device.
    """
    new, modified, deleted, unchanged = [], [], [], []
    for path, meta in current.items():
        if path not in prev:
            new.append({"path": path, "mtime": meta["mtime"], "area": top_area(path)})
        elif prev[path].get("mtime") != meta["mtime"]:
            prev_meta = prev[path]
            # Heuristic: mtime moved but the open page did not -> likely a
            # sync/push container touch, not ink. rmapi stat exposes no
            # annotation-layer signal, so CurrentPage is the best cheap
            # discriminator; the rm_pull drain stays authoritative. Only fires
            # once the prior snapshot carries current_page (post-upgrade runs).
            page_known = (prev_meta.get("current_page") is not None
                          and meta.get("current_page") is not None)
            mtime_only = (page_known
                          and prev_meta.get("current_page") == meta.get("current_page"))
            modified.append({"path": path, "mtime": meta["mtime"],
                             "prev_mtime": prev_meta.get("mtime"),
                             "area": top_area(path),
                             "mtime_only": mtime_only})
        else:
            unchanged.append({"path": path, "mtime": meta["mtime"], "area": top_area(path)})
    present = walked if walked is not None else set(current)
    for path, meta in prev.items():
        if path not in present:
            deleted.append({"path": path, "mtime": meta.get("mtime"), "area": top_area(path)})

    if resolve_moves:
        prev, current = resolve_move_ids(new, deleted, prev, current,
                                         workers=move_workers)
    moved, new, deleted = pair_moves(new, deleted, prev, current)

    def _key(rec: dict[str, Any]) -> str:
        return rec.get("mtime") or ""

    return {
        "moved": sorted(moved, key=lambda r: r["to"]),
        "new": sorted(new, key=_key, reverse=True),
        "modified": sorted(modified, key=_key, reverse=True),
        "deleted": sorted(deleted, key=lambda r: r["path"]),
        "unchanged": sorted(unchanged, key=_key, reverse=True),
    }


# ── reporting ────────────────────────────────────────────────────────────────

def print_section(title: str, rows: list[dict[str, Any]], show_prev: bool = False) -> None:
    if not rows:
        return
    print(f"\n{title} ({len(rows)})")
    for r in rows:
        when = (r.get("mtime") or "")[:16].replace("T", " ")
        line = f"  {when}  {r['path']}"
        if show_prev and r.get("prev_mtime"):
            line += f"   (was {r['prev_mtime'][:16].replace('T', ' ')})"
        print(line)


def print_moves(rows: list[dict[str, Any]]) -> None:
    """Moves are reported before anything else: a rename that reads as loss is
    the single most alarming way this tool can be wrong."""
    if not rows:
        return
    print()
    print(f"MOVED / RENAMED (same document, new path) ({len(rows)})")
    for r in rows:
        print(f"  {r['from']}")
        suffix = "   [also edited]" if r.get("edited") else ""
        print(f"    -> {r['to']}{suffix}")


def report(diff: dict[str, list[dict[str, Any]]], first_run: bool, show_all: bool) -> None:
    if first_run:
        total = len(diff["new"])
        print(f"\n[baseline] No prior snapshot -- recorded {total} document(s) as the "
              f"baseline. Re-run after working on the device to see the delta.")
        # Still group the baseline by area so the user sees the lay of the land.
        by_area: dict[str, int] = {}
        for r in diff["new"]:
            by_area[r["area"]] = by_area.get(r["area"], 0) + 1
        for area, n in sorted(by_area.items(), key=lambda kv: kv[1], reverse=True):
            print(f"    {area:<28} {n}")
        return

    print_moves(diff.get("moved", []))
    print_section("NEW (created since last diff)", diff["new"])
    print_section("MODIFIED (worked on since last diff)", diff["modified"], show_prev=True)
    print_section("DELETED / moved away", diff["deleted"])
    if show_all:
        print_section("UNCHANGED", diff["unchanged"])

    edited_moves = [r for r in diff.get("moved", []) if r.get("edited")]
    worked = len(diff["new"]) + len(diff["modified"]) + len(edited_moves)
    if worked == 0 and not diff["deleted"] and not diff.get("moved"):
        print("\nNo changes since last diff -- nothing worked on.")
    else:
        areas = {}
        for r in diff["new"] + diff["modified"] + edited_moves:
            areas[r["area"]] = areas.get(r["area"], 0) + 1
        summary = ", ".join(f"{a} ({n})" for a, n in
                            sorted(areas.items(), key=lambda kv: kv[1], reverse=True))
        print(f"\nWorked on {worked} document(s): {summary}")
        reorganised = len(diff.get("moved", [])) - len(edited_moves)
        if reorganised:
            print(f"  ({reorganised} document(s) only moved -- renamed or "
                  f"refiled, not written in. No drain needed.)")
        if diff["modified"]:
            # `ls -l` exposes a timestamp and nothing else -- no CurrentPage,
            # no stroke count, no annotation-layer signal at all (unlike the
            # old per-document `stat`, which at least hinted via CurrentPage).
            # MODIFIED here means "this device path's timestamp changed since
            # last diff", which a sync/push touch or a view-only open can
            # trigger with zero ink written -- see fix-list.md item 14 for a
            # live example. Treat it as "worth checking", never as "written in".
            print("  (mtime alone does not prove ink was added -- a sync/push "
                  "touch or a view-only open can move it too. Confirm via the "
                  "drain before treating MODIFIED as worked-on.)")


# ── baseline safety ──────────────────────────────────────────────────────────

def baseline_guard(scan: DeviceScan, prev: dict[str, dict[str, Any]],
                   diff: dict[str, list[dict[str, Any]]]) -> str | None:
    """Reason to refuse advancing the baseline, or None if the scan looks sound.

    A default run overwrites the baseline, so a bad scan is not merely a bad
    report -- it erases the record of everything it failed to see. Two shapes
    say "the cloud stopped answering" far more often than they say "the device
    changed": most of the walk being unreadable, and most of the baseline
    missing from the walk.
    """
    walked = len(scan.walked)
    if walked:
        unreadable_fraction = len(scan.unreadable) / walked
        if unreadable_fraction > MAX_UNREADABLE_FRACTION:
            return (f"{len(scan.unreadable)}/{walked} document(s) "
                    f"({unreadable_fraction:.0%}) could not be read")
    if prev:
        disappeared_fraction = len(diff["deleted"]) / len(prev)
        if disappeared_fraction > MAX_DISAPPEARED_FRACTION:
            return (f"{len(diff['deleted'])}/{len(prev)} baseline document(s) "
                    f"({disappeared_fraction:.0%}) are missing from the walk")
    return None


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="/",
                    help="Device subtree to diff (default '/', the whole library).")
    ap.add_argument("--ignore", action="append", default=None, metavar="NAME",
                    help="Top-level folder name to skip (repeatable). Defaults to "
                         f"{', '.join(DEFAULT_IGNORE)} unless --no-default-ignore.")
    ap.add_argument("--no-default-ignore", action="store_true",
                    help="Do not apply the default ignore list (Draw, trash).")
    ap.add_argument("--no-update", action="store_true",
                    help="Report the delta but do NOT move the baseline snapshot.")
    ap.add_argument("--all", action="store_true",
                    help="Also list unchanged documents.")
    ap.add_argument("--verbose", action="store_true",
                    help="List every unreadable entry instead of summarising the count.")
    ap.add_argument("--workers", type=int, default=STAT_WORKERS, metavar="N",
                    help=f"Parallel `ls -l` directory-listing calls (default "
                         f"{STAT_WORKERS}). Each is a separate rmapi process doing "
                         f"its own token exchange, so raising this invites HTTP "
                         f"429 from the cloud.")
    ap.add_argument("--move-stat-workers", type=int, default=MOVE_STAT_WORKERS,
                    metavar="N",
                    help=f"Parallel targeted `rmapi stat` calls used only to "
                         f"resolve ambiguous move candidates (default "
                         f"{MOVE_STAT_WORKERS}). Fires far less often than "
                         f"--workers -- only when a diff has both new and "
                         f"deleted paths -- but hits the same rate-limited "
                         f"token endpoint.")
    ap.add_argument("--force-baseline", action="store_true",
                    help="Advance the baseline even when the scan looks incomplete "
                         "(most of the walk unreadable, or most of the baseline "
                         "missing). Only use when the change is genuinely real.")
    ap.add_argument("--out", help="Write the change manifest as JSON to this path.")
    args = ap.parse_args()

    ignore = tuple(args.ignore or ())
    if not args.no_default_ignore:
        ignore = tuple(dict.fromkeys((*DEFAULT_IGNORE, *ignore)))  # dedupe, keep order

    snapshot = load_snapshot()
    prev_files = snapshot.get("files", {})
    first_run = not prev_files

    try:
        scan = build_current(args.root, ignore, verbose=args.verbose,
                             workers=args.workers)
    except Exception as e:
        print(f"[x] device walk failed: {e}", file=sys.stderr)
        return 2

    current = merge_scan(scan, prev_files)
    diff = diff_snapshots(prev_files, current, walked=set(scan.walked),
                          resolve_moves=True, move_workers=args.move_stat_workers)
    report(diff, first_run=first_run, show_all=args.all)

    if scan.unreadable:
        print(f"\nUNREADABLE ({len(scan.unreadable)}) -- state unknown this run, "
              f"carried forward, NOT deleted")
        shown = sorted(scan.unreadable)[:15]
        for path in shown:
            print(f"  {path}")
        if len(scan.unreadable) > len(shown):
            print(f"  ... and {len(scan.unreadable) - len(shown)} more")

    refusal = baseline_guard(scan, prev_files, diff)

    if args.out:
        warnings = []
        if diff["modified"]:
            warnings.append(make_warning(
                "mtime_no_annotation_signal",
                f"{len(diff['modified'])} modified entry(ies) are flagged on "
                f"timestamp alone -- `ls -l` carries no annotation-layer field, "
                f"so a sync/push touch or a view-only open reads identically to "
                f"real ink; confirm via the drain before treating as worked-on",
                data={"paths": [r["path"] for r in diff["modified"]]},
            ))
        if scan.unreadable:
            warnings.append(make_warning(
                "unreadable_entries",
                f"{len(scan.unreadable)} of {len(scan.walked)} walked document(s) "
                f"could not be read (their directory's `ls -l` failed -- cloud "
                f"rate limiting shows up as HTTP 429); their state is unknown this "
                f"run and they are carried forward, NOT reported as deleted",
                data={"paths": sorted(scan.unreadable)},
            ))
        if refusal:
            warnings.append(make_warning(
                "baseline_not_advanced" if not args.force_baseline
                else "baseline_forced",
                f"scan looks incomplete: {refusal}",
            ))
        manifest = {
            "root": args.root,
            "ignore": list(ignore),
            "diffed_at": datetime.now(timezone.utc).isoformat(),
            "prev_snapshot_at": snapshot.get("updated_at"),
            "first_run": first_run,
            "walked": len(scan.walked),
            "readable": len(scan.readable),
            "unreadable": sorted(scan.unreadable),
            "baseline_advanced": not args.no_update and not (
                refusal and not args.force_baseline),
            "changes": {k: v for k, v in diff.items() if k != "unchanged" or args.all},
            "warnings": warnings,
        }
        Path(args.out).write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"\nWrote manifest -> {args.out}")

    if args.no_update:
        print("\n[snapshot] --no-update: baseline left untouched.")
    elif refusal and not args.force_baseline:
        print(f"\n[snapshot] REFUSING to advance the baseline: {refusal}.",
              file=sys.stderr)
        print("[snapshot] This scan is almost certainly incomplete rather than "
              "the device having changed that much; writing it back would "
              "destroy the baseline. Re-run when the cloud is answering, or "
              "pass --force-baseline if the change is genuinely real.",
              file=sys.stderr)
        return 3
    else:
        if refusal and args.force_baseline:
            print(f"\n[snapshot] --force-baseline: advancing despite {refusal}.",
                  file=sys.stderr)
        save_snapshot(args.root, current)
        print(f"\n[snapshot] baseline updated -> {SNAPSHOT_FILE.name} "
              f"({len(current)} docs)")
    return 0


if __name__ == "__main__":
    cli_main(main)
