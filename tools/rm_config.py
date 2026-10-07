#!/usr/bin/env python3
"""
rm_config.py -- Shared configuration for the rm_*.py reMarkable tool family.

Single source of truth for two things every reMarkable tool needs:

  1. The .env loader. Workspace -> project -> tools precedence, and a real
     shell environment variable always wins over any .env file. Replaces the
     three slightly-divergent `_load_env()` copies that used to live in
     rm_push.py, rm_pull.py and rm_interpret.py.

  2. RM_ROOT -- the device-side root folder under which most content lives
     (Sketches / Notebooks / Inbox / Projects). Defaults to "/00_Projects/Notes"
     -- deliberately generic, because this file ships in the public rm-mcp
     manifest -- and is overridable via the RM_ROOT environment variable or an
     RM_ROOT line in any .env. This machine sets it in tools/.env. NOTE: Reading
     is no longer one of these -- see READING_ROOT / RM_READING_ROOT below.

Why this exists: the 2026-06-01 workspace renumber moved the device tree from
/Name to /NNN_Name and silently zeroed every pull until .rm_state.json was
hand-migrated AND a stale hardcoded reading-root literal was found in
rm_push.py. Deriving every device path from a single RM_ROOT means the
next renumber is a one-line change here (or in .env), not a hunt-and-migrate.

A second reorg (device-side, deliberate, confirmed live via /rm-diff on
2026-07-03) nested every top-level project folder under /00_Projects/ --
/NNN_Name became /00_Projects/NNN_Name.

A third reorg (found 2026-08-27 by a live rmapi walk, ruled deliberate) lifted
Reading OUT of the project root to /00_Projects/Reading, making it a shared
top-level lane. This one broke the "one root, one line" assumption above: the
reading desk no longer derives from RM_ROOT at all. It cost 95 documents'
worth of silently undrained annotations before anyone looked, because rm_pull
skips a missing path per-document rather than failing. When a lane moves, the
tell is a drain that reports plenty of candidates and pulls none of them.
RM_ROOT was repointed here on 2026-07-03; rm-mcp (a separate codebase) needs
the same fix independently.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, NoReturn

try:
    import rm_state_remote
except ImportError:  # pragma: no cover -- the public tree has no cloud lane
    # rm_state_remote is the shared-ledger (GCS) lane, and tools/
    # rm_build_public.py deliberately does NOT ship the cloud lane, so the
    # public tree has no such module. Before this guard landed the import was
    # hard, which made rm_config -- the module 5 of the 9 shipped scripts
    # import -- fail on import in the public tree: the package was broken from
    # the first line. Caught 2026-09-20 by the first clean rebuild since
    # the import arrived in 56f84bb; the currently PUBLISHED tree predates it
    # and is unaffected.
    #
    # None, not a no-op stub: every use in this file is already gated on
    # rm_state_remote.configured(), so the absent case has exactly one
    # behaviour -- local-only -- and a stub would let a real broken install on
    # the desk masquerade as "not configured" instead of failing loudly.
    rm_state_remote = None


def _remote_ledger_configured() -> bool:
    """Whether the shared GCS ledger is in play for this process.

    False when the cloud lane is not shipped (public tree) and when it is
    shipped but RM_STATE_BUCKET is unset (the ordinary desk default). Both
    mean the same thing to every caller -- plan against the local ledger --
    which is why one predicate covers them.
    """
    return rm_state_remote is not None and rm_state_remote.configured()


# The tools/ ROOT, in either layout. tools/ was split into rm/, zotero/,
# litgather/ and common/ on 2026-09-20 while the public tree stayed
# FLAT, so this module ships at tools/rm_config.py there and lives at
# tools/rm/rm_config.py here. Both have to resolve to tools/.
#
# This is the most load-bearing path in the project and the split briefly broke
# it: a bare Path(__file__).parent made STATE_FILE resolve to
# tools/rm/.rm_state.json, which does not exist. .rm_state.json is not a cache
# -- every "pushed" entry carries the zotero_item_key that lets a drained
# annotation find its Zotero item. An absent ledger does not raise; it returns
# the empty baseline, so every drain would have reported a clean run over 112
# invisible entries. That is the "reported plenty of candidates and pulled none
# of them" failure this project has already paid for twice, and it was live for
# about an hour before a path audit caught it.
_TOOLS_BUCKETS = ("rm", "zotero", "litgather", "common")
_HERE = Path(__file__).resolve().parent
_TOOLS_DIR = _HERE.parent if _HERE.name in _TOOLS_BUCKETS else _HERE
_PROJECT_DIR = _TOOLS_DIR.parent
_WORKSPACE_DIR = _PROJECT_DIR.parent

# Public alias. Every sibling that keeps its own state or cache beside the
# ledger reads this rather than recomputing the root, so the layout question is
# answered in exactly one place. rm_diff, rm_index and rm_lightroom each had
# their own Path(__file__).parent and each silently moved its state file into a
# bucket when the split landed -- three separate copies of one bug, which is
# the argument for one alias.
TOOLS_DIR = _TOOLS_DIR


def load_env() -> None:
    """Load .env files into os.environ.

    Precedence, highest first: real shell environment > tools/.env >
    <project>/.env > workspace/.env. Keys already present in os.environ when
    this runs (i.e. set by the real shell) are never overwritten; among the
    .env files, the more specific (closer to the tools dir) wins.

    Idempotent: a second call is a no-op because every key loaded by the first
    call is then already present in os.environ.
    """
    preset = set(os.environ)  # real / shell-provided keys: never override these
    candidates = [
        _WORKSPACE_DIR / ".env",   # most general
        _PROJECT_DIR / ".env",     # <project>/.env
        _TOOLS_DIR / ".env",       # most specific
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in preset:
                continue  # a real shell value wins over any file
            os.environ[key] = value.strip().strip('"').strip("'")


load_env()

# Console-encoding guard: on a cp1252 Windows console a device filename with
# e.g. U+2010 crashes any CLI print (seen live 2026-07-03, rm_pull dry-run).
# Keep the console's encoding but degrade unencodable chars to '?' instead of
# dying -- the same errors="replace" stance run_rmapi takes on capture.
for _stream in (sys.stdout, sys.stderr):
    try:
        if (_stream.encoding or "").lower() not in ("utf-8", "utf8"):
            _stream.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass


# ── device-side layout ─────────────────────────────────────────────────────

# Default is the DEVICE ROOT, matching the RM_ROOT that rm_build_public.py
# writes into the public .env.example. This file ships in the public manifest,
# so the default is a stranger's first experience: until 2026-09-06 it was
# "/00_Projects/<project>" -- the author's own folder -- which also disagreed
# with the documented default in the same build; until 2026-09-10 it was
# "/00_Projects/Notes", still his layout with the name filed off. The root is
# the one folder every owner has ("My files"). Set RM_ROOT (tools/.env wins)
# to point a machine at its real tree; the desk does.
RM_ROOT = "/" + os.environ.get("RM_ROOT", "/").strip().strip("/")


def device_root(name: str) -> str:
    """Normalise an arbitrary top-level device root.

    The reMarkable tree has several top-level project roots, all siblings:
    /100_thesis, /110_notes, etc. RM_ROOT is the default notes
    tree; this lets any tool address a different per-project root
    without that root being baked into the substrate.

    >>> device_root("110_notes")
    '/110_notes'
    >>> device_root("/100_thesis/")
    '/100_thesis'
    """
    return "/" + name.strip("/")


def under(root: str, *parts: str) -> str:
    """Join device-path segments under an arbitrary device root.

    Generalises rm_path() (which is hardwired to RM_ROOT) to any root, so a
    push tool can target /110_notes/<session>/ the same way the notes tools
    target /100_thesis/Reading/<collection>/.

    >>> under("110_notes", "session-1", "contact sheets")
    '/110_notes/session-1/contact sheets'
    """
    base = device_root(root)
    cleaned = [p.strip("/") for p in parts if p]
    # At the device root itself base is "/", and joining onto it as-is gave
    # "//Sketches" (2026-10-06): RM_ROOT defaults to "/" in the public build.
    return "/".join([base.rstrip("/"), *cleaned]) if cleaned else base


def rm_path(*parts: str) -> str:
    """Join device-path segments under RM_ROOT (the notes tree).

    Thin convenience over under() for the common case.

    >>> rm_path("Reading", "Some Paper")    # with RM_ROOT=/Notes
    '/Notes/Reading/Some Paper'
    """
    return under(RM_ROOT, *parts)


# Reading is NOT under RM_ROOT. A third device-side reorg lifted the reading desk
# out of /00_Projects/<project> to sit at /00_Projects/Reading as a shared
# top-level lane (found 2026-08-27, ruled deliberate). It therefore
# carries its own overridable root instead of being derived from RM_ROOT.
#
# Deriving it from RM_ROOT is exactly what broke: rm_pull passes the full stored
# path to `rmapi stat`, so after the move all 95 tracked documents stat'd as
# missing, and the drain printed "[?] not found on device, skip" per document and
# reported nothing to do -- indistinguishable from a clean run. Every annotation
# made since the move sat undrained. That is the same failure the /Name ->
# /NNN_Name renumber caused above; the lesson held, but the assumption that
# every lane stays under one root did not.
#
# 2026-09-07: moved again, deliberately and this time WITH the tooling -- the
# desk is now a first-class top-level lane at /Reading, on the author's instruction
# ("move the reading folder to myfiles/reading level"). The `rmapi mv` and the
# 111 .rm_state.json key rewrites were done in the same operation as this edit,
# which is the point: the constant, the device and the ledger move together or
# the drain reports a clean run over an empty result. Verified after the move by
# a live stat resolving under the new root.
READING_ROOT = (os.environ.get("RM_READING_ROOT", "/Reading").rstrip("/")
                or "/Reading")

# Where rm_retire parks a finished paper. ARCHIVE is retire's DEFAULT action,
# so this subtree only ever grows -- which is why the desk-capacity gate must
# exclude it. Counting the archive against the cap made a 12-paper desk report
# 22/5 and refuse every push (measured 2026-09-17); the cap would then be
# satisfiable only by --allow-over-cap, which is the same as having no cap.
# Shared here rather than owned by rm_retire so the gate and the archiver can
# never disagree about which folder is the archive.
READING_ARCHIVE_FOLDER = "_Read"
READING_ARCHIVE_ROOT = f"{READING_ROOT.rstrip('/')}/{READING_ARCHIVE_FOLDER}"

# How many documents the reading desk may hold before a push is refused.
# The desk should be what you are reading NOW, not everything you might read.
# Measured 2026-08-03: one session pushed 76 papers in a single operation and
# 66 of them had still never been opened twelve days later. Bulk push is the
# anti-pattern that produced that bloat, so the cap is enforced at the push
# boundary rather than left to intention.
READING_DESK_CAP = 5

SKETCHES_ROOT = rm_path("Sketches")
# The Notebooks/ level was flattened away on the device (deliberate, confirmed
# 2026-08-25): the writing, capture and talks notebooks now sit directly under
# RM_ROOT. rm_talks already pointed at rm_path("Talks")
# after the 2026-07-03 reorg; this finishes the same migration for the rest.
# Kept as a named constant rather than deleted so callers keep reading as
# "the notebooks live here", and so a future re-nesting is one line again.
NOTEBOOKS_ROOT = RM_ROOT
INBOX_ROOT = rm_path("Inbox")
PROJECTS_ROOT = rm_path("Projects")


# ── outbound identity (CrossRef polite pool) ────────────────────────────────

# CrossRef serves requests carrying a contact address from a faster, more
# reliable pool. The literature tools each hardcoded ONE address -- the author's
# personal email, in six files -- which made the address impossible to change,
# and made a documented setting a lie: README.public.md has advertised
# CROSSREF_CONTACT_EMAIL as a supported variable since the first public build,
# and until 2026-09-06 nothing anywhere read it.
#
# Same class of defect as the RM_ROOT and RMAPI_BIN defaults corrected in the
# same file: a personal value frozen into a shipped default. This file is in
# the public manifest, so leave the fallback EMPTY -- CrossRef treats a
# missing mailto as the anonymous pool, which is slower but correct, whereas
# a stranger's tooling identifying itself as someone else is neither.
CROSSREF_CONTACT_EMAIL = os.environ.get("CROSSREF_CONTACT_EMAIL", "").strip()


def polite_user_agent(product: str, version: str = "1.0",
                      url: str | None = None) -> str:
    """A CrossRef-friendly User-Agent, with a mailto only if one is configured.

    Shape is the conventional `product/version (comment)`, so callers pass the
    product and version separately rather than pre-formatting -- a rate-limit
    complaint then still names the tool that caused it. `url` adds a project
    link to the comment, which CrossRef also accepts as contact information.
    """
    base = f"{product}/{version}"
    parts = [p for p in (url, f"mailto:{CROSSREF_CONTACT_EMAIL}"
                         if CROSSREF_CONTACT_EMAIL else None) if p]
    return f"{base} ({'; '.join(parts)})" if parts else base


# ── rmapi binary ────────────────────────────────────────────────────────────

# The ddvk rmapi fork built against the v4 sync schema. Historically hardcoded
# as a literal in 10+ rm_*.py tools; centralised here so every CLI and the
# forthcoming rm-mcp read one env-overridable path. Override with RMAPI_BIN.
# Defaults to the bare name so it resolves on PATH. This file SHIPS in the
# public manifest, and until 2026-09-06 the default was an absolute path
# inside the author's home directory -- which no other machine has, so the
# first thing a stranger met was a missing-binary error naming a
# stranger's user account. Set RMAPI_BIN to an absolute path if your
# rmapi is not on PATH.
RMAPI_BIN_CONFIGURED = os.environ.get("RMAPI_BIN", "rmapi")

# Typical install locations, searched only after PATH. Deliberately generic --
# the whole point of the 2026-09-06 change above was that this file must not
# name one machine's home directory, so these are $HOME-relative or system
# prefixes that exist on any install rather than anybody's actual desk.
_RMAPI_FALLBACK_DIRS = (
    Path.home() / "bin",
    Path.home() / ".local" / "bin",
    Path("/usr/local/bin"),
    Path("/usr/bin"),
)


def _resolve_rmapi(configured: str) -> str | None:
    """Absolute path to the rmapi binary, or None if it cannot be found.

    A BARE NAME IS NOT A FILESYSTEM PATH, and conflating the two is what
    made the 2026-09-20 outage undiagnosable. The default "rmapi" resolves
    through PATH when subprocess runs it, but `Path("rmapi").is_file()`
    tests the current directory -- so rm-mcp's health check reported the
    binary missing on every machine, including ones where it worked fine,
    and reported it missing for the wrong reason on ones where it did not.
    Resolving once, here, gives every caller a real path to test and to run.

    Order: an explicit path is honoured as given; otherwise PATH (via which,
    which also applies PATHEXT on Windows, so "rmapi" finds "rmapi.exe");
    otherwise the typical install dirs above.

    Returns None rather than raising: import must never fail on a desk with
    no rmapi, because rm_health's whole job is to REPORT that state.
    """
    if not configured:
        return None
    # An explicit path (anything with a separator) is the caller's assertion
    # about where the binary is -- never second-guess it with a PATH lookup.
    if os.sep in configured or (os.altsep and os.altsep in configured):
        given = Path(configured).expanduser()
        for candidate in (given, given.with_suffix(".exe")):
            if candidate.is_file():
                return str(candidate.resolve())
        return None
    found = shutil.which(configured)
    if found:
        return str(Path(found).resolve())
    for directory in _RMAPI_FALLBACK_DIRS:
        for candidate in (directory / configured,
                          directory / f"{configured}.exe"):
            if candidate.is_file():
                return str(candidate.resolve())
    return None


RMAPI_RESOLVED: str | None = _resolve_rmapi(RMAPI_BIN_CONFIGURED)

# What subprocess actually invokes. Falls back to the configured value when
# resolution fails, so the failure surfaces as run_rmapi's FileNotFoundError
# (now typed as RmapiNotFoundError, which names RMAPI_BIN) rather than as a
# None leaking into a command line.
RMAPI_BIN = RMAPI_RESOLVED or RMAPI_BIN_CONFIGURED


# -- Gemini model ids ---------------------------------------------------------

# The ONE place a Gemini model id is pinned. Every Gemini lane -- the drain
# vision pass, the triage glance, capture, talks, paper interpret, the
# lightroom analyser, both key probes and rm-mcp's roundtrip dispatch --
# imports these rather than carrying its own literal. Two lessons sit behind
# that: the 2.5 generation is locked out for NEW projects (a fresh project
# gets HTTP 404 today), and two plausible-but-fake ids (gemini-3-flash,
# gemini-3.1-pro) shipped across ten files at once because each file pinned
# its own.
#
# CORRECTED 2026-09-20. This comment asserted a
# 2.5 retirement on 2026-10-16. THERE IS NO SUCH DATE: Google's deprecations
# page lists the undated base ids gemini-2.5-flash / gemini-2.5-pro as "no
# shutdown date announced", and the 2026-10-16 figure was manufactured by
# reading the DATED preview snapshots' real shutdown dates onto the undated
# base ids. A lockout is not a retirement. roundtrip.py and the ship notes
# were corrected when this was found; THIS file, which those corrections
# cite as their source, was missed -- so the false date outlived its own
# correction by three days in the one place most likely to be copied from.
#
# Verified live against the key on 2026-09-09: both defaults below serve
# generateContent. A 200 is not verification and neither is models.list() --
# gemini-3.5-transcribe passes both and returns an EMPTY transcript while
# billing for the input. Check that something CORRECT comes back. Pinned ids rather
# than *-latest aliases, so a run is reproducible; bump them HERE, once.
# Override per machine with GEMINI_FLASH_MODEL / GEMINI_PRO_MODEL in the
# environment or any .env (rm_interpret_gemini's preflight names them).
GEMINI_FLASH_MODEL = os.environ.get("GEMINI_FLASH_MODEL", "").strip() or "gemini-3.8-flash"
GEMINI_PRO_MODEL = os.environ.get("GEMINI_PRO_MODEL", "").strip() or "gemini-3.1-pro-preview"


def gemini_model_for(backend: str) -> str:
    """Map a vision backend name to its pinned Gemini model id.

    Only the two Gemini backends resolve; "claude" and anything else raise,
    so a caller cannot silently hand a non-Gemini backend to the Gemini tool.
    """
    if backend == "gemini-pro":
        return GEMINI_PRO_MODEL
    if backend == "gemini-flash":
        return GEMINI_FLASH_MODEL
    raise ValueError(f"not a Gemini vision backend: {backend!r}")


# ── push/pull state (.rm_state.json) ────────────────────────────────────────

# Single owner of the push/pull baseline shared by rm_push / rm_pull / rm_triage
# / rm_capture / rm_push_project. "pushed" is keyed by device_path; "captures"
# is rm_capture's idempotency ledger. Previously each tool carried its own
# STATE_FILE + load_state/save_state with divergent serialisation; consolidated
# here so there is one writer and one schema. NOTE: rm_diff's
# .rm_device_snapshot.json and rm_lightroom's .rm_lightroom_state.json are
# SEPARATE state files and are intentionally not managed here.
STATE_FILE = _TOOLS_DIR / ".rm_state.json"
STATE_BAK = _TOOLS_DIR / ".rm_state.json.bak"
STATE_TMP = _TOOLS_DIR / ".rm_state.json.tmp"
STATE_LOCK_FILE = _TOOLS_DIR / ".rm_state.lock"

# Current vision prompt, resolving to tools/prompts/rm_interpret_<v>.md.
# It lives HERE rather than in rm_interpret because the pull tools need to stamp
# it into the reading ledger, and they deliberately invoke rm_interpret as a
# subprocess rather than importing it -- rm_interpret pulls in `anthropic` at
# module level, which the Gemini-only drain path must not require.
# rm_interpret re-exports it as DEFAULT_PROMPT_VERSION.
INTERPRET_PROMPT_VERSION = "v3"


class StateError(RuntimeError):
    """The shared .rm_state.json is unreadable and unrecoverable."""


class StateLockTimeout(StateError):
    """Another process holds tools/.rm_state.lock past the timeout."""


# Generation of the shared ledger this process last read or wrote, used as the
# ifGenerationMatch precondition on the next upload. Process-scoped on purpose:
# a stale generation from a previous run would either spuriously conflict or,
# worse, be forced past. A run that never read the remote never writes it.
_REMOTE_GENERATION: str | None = None


def remote_state_generation() -> str | None:
    """The generation this process is holding, for diagnostics and tests."""
    return _REMOTE_GENERATION


def load_state() -> dict:
    """Load the shared .rm_state.json, or the empty baseline if absent.

    When RM_STATE_BUCKET is set the ledger is fetched from that GCS object and
    mirrored to the local file, so the desk and the Cloud Run deployment plan
    against one ledger rather than two. See tools/rm_state_remote.py for why
    that had to stop being optional. A configured-but-unreachable bucket
    RAISES; it never falls back to the local copy, because a deployment
    quietly running on its own empty ledger is the failure being fixed.

    A corrupt main file falls back to the .bak written by the last successful
    save_state (with a loud stderr warning). If both are corrupt this raises
    StateError rather than returning the empty baseline: an empty baseline
    would silently zero the pull-dedupe ledger, which is exactly the failure
    the 2026-06-01 renumber taught us to fear.
    """
    global _REMOTE_GENERATION
    if _remote_ledger_configured():
        text, generation = rm_state_remote.fetch()
        _REMOTE_GENERATION = generation
        if text is None:
            # Fresh bucket. Seed it from whatever this machine already holds,
            # rather than declaring the ledger empty.
            return _load_local_state()
        try:
            state = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateError(
                f"the shared ledger at {rm_state_remote.describe()} is not "
                f"valid JSON ({exc}); repair it by hand before running any "
                f"state-mutating rm tool"
            ) from exc
        # Mirror locally so the .bak recovery path and offline inspection keep
        # working, and so a later local-only run starts from the shared truth.
        _write_state_file(json.dumps(state, indent=2, ensure_ascii=False,
                                     sort_keys=True))
        return state
    return _load_local_state()


def _load_local_state() -> dict:
    """The on-disk half of load_state: main file, then .bak, then raise."""
    if not STATE_FILE.exists():
        return {"schema": 1, "pushed": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        main_err = exc
    if STATE_BAK.exists():
        try:
            state = json.loads(STATE_BAK.read_text(encoding="utf-8"))
            print(
                f"[rm_config] WARNING: {STATE_FILE.name} is corrupt "
                f"({main_err}); recovered from {STATE_BAK.name}. "
                f"The corrupt file is left in place for inspection.",
                file=sys.stderr,
            )
            return state
        except (json.JSONDecodeError, OSError) as bak_err:
            raise StateError(
                f"both {STATE_FILE.name} ({main_err}) and "
                f"{STATE_BAK.name} ({bak_err}) are unreadable; "
                f"repair one by hand before running any state-mutating rm tool"
            ) from bak_err
    raise StateError(
        f"{STATE_FILE.name} is corrupt ({main_err}) and no {STATE_BAK.name} "
        f"exists; repair it by hand before running any state-mutating rm tool"
    ) from main_err


def save_state(state: dict) -> None:
    """Write the shared .rm_state.json atomically (utf-8, stable key order).

    Serialise first so a TypeError can never truncate the file, write to a
    temp sibling with fsync, snapshot the current file to .bak, then
    os.replace into place. A crash at any point leaves either the old file
    or the new file intact, never a torn write.
    """
    global _REMOTE_GENERATION
    payload = json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True)
    _write_state_file(payload)
    if _remote_ledger_configured():
        # The local write happened first so a failed upload still leaves this
        # machine's own copy intact and inspectable. The precondition makes a
        # concurrent write from the other machine a refusal, not a silent
        # overwrite; the new generation is kept for the next save in this run,
        # because push saves state incrementally per item.
        _REMOTE_GENERATION = rm_state_remote.upload(
            payload,
            _REMOTE_GENERATION
            if _REMOTE_GENERATION is not None
            else rm_state_remote.GENERATION_ABSENT)


def _write_state_file(payload: str) -> None:
    """Atomically replace .rm_state.json with payload, keeping a .bak.

    Serialised text in, so a TypeError can never truncate the file: write to a
    temp sibling with fsync, snapshot the current file to .bak, then
    os.replace into place. A crash at any point leaves either the old file or
    the new file intact, never a torn write.
    """
    with open(STATE_TMP, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    if STATE_FILE.exists():
        shutil.copy2(STATE_FILE, STATE_BAK)
    os.replace(STATE_TMP, STATE_FILE)


@contextmanager
def state_lock(timeout: float = 60.0, poll: float = 0.25) -> Iterator[None]:
    """Cross-process exclusive lock guarding .rm_state.json mutations.

    Locks one byte of the sidecar STATE_LOCK_FILE (never the state file
    itself -- save_state's os.replace would invalidate a handle on it).
    msvcrt.locking on Windows, fcntl.flock elsewhere (via _exclusive_file_lock,
    which rmapi_lock shares). Raises StateLockTimeout when another process
    holds the lock past `timeout` seconds.
    """
    lock_file = STATE_LOCK_FILE

    def _timed_out() -> Exception:
        return StateLockTimeout(
            f"another rm tool holds {lock_file.name} "
            f"(likely an rm_pull/rm_push in another window); "
            f"wait for it to finish and retry")

    with _exclusive_file_lock(lock_file, timeout, poll, _timed_out):
        yield


def update_state(mutator: Callable[[dict], None],
                 timeout: float = 60.0) -> dict:
    """Scoped state write: state_lock -> load_state -> mutator -> save_state.

    For short writes (MCP push records, triage mark-routed). Long-running
    CLIs that hold state in memory across a whole run (rm_pull) take
    state_lock() around the run instead.
    """
    with state_lock(timeout=timeout):
        state = load_state()
        mutator(state)
        save_state(state)
    return state


def record_project_push(device_path: str, entry: dict[str, Any]) -> None:
    """Record a direct (non-Zotero) push in state['projects_pushed'].

    Deliberately a separate namespace from 'pushed': rm_pull's dedupe planner
    walks 'pushed' and requires zotero_item_key on every entry, so direct
    MCP pushes must never land there.
    """
    def _mutate(state: dict) -> None:
        state.setdefault("projects_pushed", {})[device_path] = entry

    update_state(_mutate)


# ── shared rmapi subprocess wrapper ─────────────────────────────────────────

# Substrings in rmapi stderr/stdout that indicate a lost pairing. Wording
# pinned during the F4 live test; keep hints lowercase. Single source of
# truth -- rm_mcp/envelope.py re-exports this list.
AUTH_HINTS: tuple[str, ...] = (
    "one-time code",
    "unauthorized",
    "authentication failed",
    "not logged in",
    "please login",
    "code from https://my.remarkable.com",
    "401",
    # What rmapi prints under -ni (which run_rmapi always passes) when it has
    # no device token: it aborts instead of asking for a one-time code.
    "missing token, not asking",
)

# Signatures of transient cloud/network failures that are safe to retry.
# Auth failures, not-found and THROTTLING are never retried.
TRANSIENT_HINTS: tuple[str, ...] = (
    "connection reset",
    "connection refused",
    "timeout",
    "timed out",
    "temporar",
    "503",
    "502",
    "tls handshake",
)

# Signatures of the reMarkable cloud rate-limiting this account. A throttle is
# a HARD STOP, not a retry (to-do 3ec3d581, 2026-10-01), for a reason read off
# the rmapi source at the pinned commit 74a8e2e rather than inferred:
#
#   - An rmapi process does NOT re-sync the whole tree on start. It keeps
#     tree.cache and returns after one root-index request when the root hash is
#     unchanged (api/sync15/tree.go, Mirror). It also reuses the cached user
#     token (a 3-hour JWT); it does not exchange the device token every time.
#     The comment that used to stand here said it did, and was wrong for this
#     build.
#   - But ANY failure while building that context -- a 429, a network blip, an
#     expired token -- makes it re-mint the user token with no pause, up to
#     three times (main.go, AUTH_RETRIES), and a throttled login endpoint then
#     Fatal-exits with "failed to create user token from device token ...
#     status 429" (api/auth.go). That is the error seen live.
#
# So retrying a 429 -- which this list did from 2026-08-24 -- multiplied the
# token mints: up to 3 x (retries+1) per logical call. Now run_rmapi records a
# cooldown (rmapi_throttle_file) and refuses every rmapi call, in every
# process, until it expires.
#
# The 2026-08-24 lesson that put "status 429" in the retry list still holds,
# and is kept by making a throttle RAISE rather than return: an unretried 429
# that came back as a plain failure is what let rm_diff mistake 163 live
# documents for deletions. Kept specific ("status 429", not a bare "429"), and
# matched with the call's own path arguments removed, so a document whose NAME
# carries the words cannot block the lane.
THROTTLE_HINTS: tuple[str, ...] = (
    "status 429",
    "too many requests",
    "rate limit",
)

# CLI exit codes cli_main maps the two lane-wide conditions to. 3 predates
# cli_main (the four CLIs that take state_lock); rm-mcp's envelope.classify
# reads both.
EXIT_STATE_LOCKED = 3
EXIT_THROTTLED = 4

# Ruled 2026-10-01: five minutes. Env RM_THROTTLE_COOLDOWN_S.
DEFAULT_THROTTLE_COOLDOWN_S = 300.0
# How long a call waits for another rm tool's rmapi call to finish. A single
# call is bounded by its own timeout (300s for a get), so this covers a short
# queue. Env RM_RMAPI_LOCK_TIMEOUT_S.
DEFAULT_RMAPI_LOCK_TIMEOUT_S = 600.0
# rmapi fans out up to 20 requests when the root hash has moved
# (api/sync15/apictx.go). An explicit RMAPI_CONCURRENT in the environment wins.
RMAPI_CONCURRENT_DEFAULT = "4"
# Minimum gap, in seconds, between the end of one rmapi process and the start
# of the next, across every rm tool. Serialising stops overlap; it does not
# stop a burst. Measured live 2026-10-01: a whole-ledger sweep at ~0.5s per
# call drew a 429 on the SYNC endpoint at the 36th call (~19s), while a
# 12-call run at ~1.1s per call was clean. Two seconds is ~30 calls a minute.
# Env RM_RMAPI_MIN_INTERVAL_S; 0 disables it.
DEFAULT_RMAPI_MIN_INTERVAL_S = 2.0


class RmapiError(RuntimeError):
    """rmapi exited non-zero. Carries the CompletedProcess as .proc."""

    def __init__(self, message: str,
                 proc: subprocess.CompletedProcess | None = None) -> None:
        super().__init__(message)
        self.proc = proc


class RmapiAuthError(RmapiError):
    """rmapi surfaced an authentication problem. Pairing is a manual
    terminal step -- no tool ever re-auths programmatically."""


class RmapiNotFoundError(RmapiError):
    """The rmapi binary could not be executed at all.

    Subclasses RmapiError (and so RuntimeError) deliberately: every caller in
    this repo and in rm-mcp already handles RuntimeError, so a missing binary
    now arrives as a named, remediable failure everywhere instead of escaping
    as a bare OSError. That escape is what produced "[WinError 2] The system
    cannot find the file specified" from rm_ensure_project_folder on
    2026-09-20 -- an error that never mentions rmapi and points the caller at
    their own arguments (reported 2026-09-20).

    FileNotFoundError is an OSError, NOT a RuntimeError, which is exactly why
    it slipped past handlers that looked complete.
    """


class RmapiThrottledError(RmapiError):
    """The reMarkable cloud is rate-limiting this account (HTTP 429).

    Raised whatever `check` says, like RmapiNotFoundError: a throttle is a
    condition of the whole lane, never an answer about one document, so it must
    not come back as a CompletedProcess a caller could read as "absent".
    `until` is the epoch second the cooldown ends.
    """

    def __init__(self, message: str, until: float,
                 proc: subprocess.CompletedProcess | None = None) -> None:
        super().__init__(message, proc)
        self.until = until

    @property
    def until_iso(self) -> str:
        return _iso_utc(self.until)

    @property
    def retry_after_s(self) -> int:
        return max(0, int(self.until - time.time()))


class RmapiBusyError(RmapiError):
    """Another rm tool held the rmapi lock for longer than this call would
    wait. Nothing was sent to the cloud."""


RMAPI_NOT_FOUND_REMEDY = (
    "the rmapi binary could not be found. Set RMAPI_BIN to its absolute path "
    "(in the environment or any .env this loader reads), or put it on PATH. "
    "NOTE for Windows + Git Bash: ~/bin is on Git Bash's PATH but NOT on the "
    "Windows PATH, so a binary you can call from a shell is invisible to a "
    "server process started outside that shell -- which is the 2026-09-20 "
    "failure exactly. An absolute RMAPI_BIN is immune to which shell started "
    "the process."
)


def looks_unauthenticated(text: str) -> bool:
    low = (text or "").lower()
    return any(hint in low for hint in AUTH_HINTS)


def _looks_transient(text: str) -> bool:
    low = (text or "").lower()
    return any(hint in low for hint in TRANSIENT_HINTS)


def looks_throttled(text: str) -> bool:
    low = (text or "").lower()
    return any(hint in low for hint in THROTTLE_HINTS)


def _verb(args: tuple[str, ...]) -> str:
    """The rmapi command: the first argument that is not a global flag
    (`-json ls /x` has the verb "ls")."""
    return next((a for a in args if not a.startswith("-")), "")


def _without_path_args(text: str, args: tuple[str, ...]) -> str:
    """`text` with the call's own path arguments blanked out.

    rmapi echoes the path it was given in its errors, so a document named
    "Rate Limit Theory" would otherwise arm a five-minute lane-wide cooldown on
    a plain not-found. The verb and flags are kept: blanking "stat" would turn
    "status 429" into "us 429" and hide a real throttle.
    """
    verb_seen = False
    for arg in args:
        if arg.startswith("-"):
            continue
        if not verb_seen:
            verb_seen = True
            continue
        if len(arg) > 1:
            text = text.replace(arg, " ")
    return text


# -- rmapi cooldown + serialisation (to-do 3ec3d581, 2026-10-01) -------------

def _env_seconds(name: str, default: float) -> float:
    """A non-negative seconds value from the environment, read at call time so
    a .env loaded after import still applies. Garbage falls back to default."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _iso_utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def rmapi_throttle_file() -> Path:
    """Where the cooldown lives. Shared by every rm tool on this machine (the
    CLIs, rm_watch's drains and the rm-mcp server). Env RM_RMAPI_THROTTLE_FILE
    overrides it; the test suite points it at a temp dir."""
    override = os.environ.get("RM_RMAPI_THROTTLE_FILE")
    return Path(override) if override else _TOOLS_DIR / ".rmapi_throttle.json"


def rmapi_lock_file() -> Path:
    """The file lock that serialises rmapi calls. Env RM_RMAPI_LOCK_FILE."""
    override = os.environ.get("RM_RMAPI_LOCK_FILE")
    return Path(override) if override else _TOOLS_DIR / ".rmapi.lock"


def rmapi_last_call_file() -> Path:
    """When the last rmapi process ended (epoch seconds, as text). Lives beside
    the lock (`.rmapi.lock` -> `.rmapi.last`) and is only touched under it.
    Not the lock file's own mtime: acquiring the lock touches that."""
    return rmapi_lock_file().with_suffix(".last")


def _pace() -> None:
    """Sleep until RM_RMAPI_MIN_INTERVAL_S has passed since the last rmapi call
    ended. Caller holds rmapi_lock. A missing or garbled stamp means no wait."""
    interval = _env_seconds("RM_RMAPI_MIN_INTERVAL_S",
                            DEFAULT_RMAPI_MIN_INTERVAL_S)
    if interval <= 0:
        return
    try:
        last = float(rmapi_last_call_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    wait = interval - (time.time() - last)
    if 0 < wait <= interval:  # a stamp from the future is ignored, not obeyed
        time.sleep(wait)


def _stamp_last_call() -> None:
    try:
        rmapi_last_call_file().write_text(repr(time.time()), encoding="utf-8")
    except OSError as exc:
        print(f"[warn] could not stamp {rmapi_last_call_file()}: {exc}",
              file=sys.stderr)


def _read_throttle() -> dict[str, Any] | None:
    """The recorded cooldown, or None.

    Fails OPEN on a corrupt or unreadable file: a bad cooldown record must not
    brick the lane, and the next real 429 rewrites it anyway.
    """
    try:
        data = json.loads(rmapi_throttle_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("until"),
                                                    (int, float)):
        return None
    return data


def throttle_status(now: float | None = None) -> dict[str, Any]:
    """Read-only view of the cooldown, for rm_health and humans. No cloud call."""
    now = time.time() if now is None else now
    data = _read_throttle()
    if data is None or data["until"] <= now:
        return {"throttled": False, "until": None, "remaining_s": 0,
                "observed_at": None, "detail": None}
    return {"throttled": True, "until": _iso_utc(data["until"]),
            "remaining_s": int(data["until"] - now),
            "observed_at": data.get("observed_at"),
            "detail": data.get("detail")}


def clear_throttle() -> None:
    """Drop the cooldown (a human who knows the throttle has lifted)."""
    rmapi_throttle_file().unlink(missing_ok=True)


def _throttle_line(combined: str) -> str:
    """The line of rmapi's output that names the throttle, else the last one."""
    lines = [ln.strip() for ln in combined.splitlines() if ln.strip()]
    hit = next((ln for ln in reversed(lines) if looks_throttled(ln)), None)
    return (hit or (lines[-1] if lines else ""))[:300]


def _arm_throttle(args: tuple[str, ...], combined: str) -> float:
    """Record a cooldown and return the epoch second it ends."""
    now = time.time()
    until = now + _env_seconds("RM_THROTTLE_COOLDOWN_S",
                               DEFAULT_THROTTLE_COOLDOWN_S)
    record = {"until": until, "until_iso": _iso_utc(until),
              "observed_at": _iso_utc(now),
              "command": _verb(args),
              "detail": _throttle_line(combined)}
    path = rmapi_throttle_file()
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        # This caller still gets the raise; only later calls lose the memory.
        print(f"[warn] could not record the rmapi cooldown at {path}: {exc}",
              file=sys.stderr)
    return until


def _throttled_error(until: float, detail: str | None,
                     proc: subprocess.CompletedProcess | None = None
                     ) -> RmapiThrottledError:
    remaining = max(0, int(until - time.time()))
    local = time.strftime("%H:%M", time.localtime(until))
    message = (f"rmapi throttled: the reMarkable cloud answered 429, so no rm "
               f"tool will call it until {_iso_utc(until)} (local {local}, "
               f"{remaining}s). Wait: a retry before then re-mints the login "
               f"token and extends the throttle.")
    if detail:
        message += f" Last error: {detail}"
    return RmapiThrottledError(message, until, proc)


def _refuse_if_throttled() -> None:
    data = _read_throttle()
    if data is not None and data["until"] > time.time():
        raise _throttled_error(data["until"], data.get("detail"))


@contextmanager
def _exclusive_file_lock(lock_file: Path, timeout: float, poll: float,
                         on_timeout: Callable[[], Exception]
                         ) -> Iterator[None]:
    """Cross-process exclusive lock on one byte of `lock_file`.

    msvcrt.locking on Windows, fcntl.flock elsewhere. Both conflict across
    separate handles in the SAME process too, so this also serialises threads.
    Raises on_timeout() when the lock is still held after `timeout` seconds.
    """
    lock_file.touch(exist_ok=True)
    fh = open(lock_file, "r+b")
    locked = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise on_timeout() from None
                time.sleep(poll)
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass  # handle close releases the lock regardless
        fh.close()


@contextmanager
def rmapi_lock(timeout: float | None = None,
               poll: float = 0.1) -> Iterator[None]:
    """One rmapi process at a time, across threads and processes.

    Two reasons, both from the rmapi source. Every process reads and rewrites
    the same tree.cache with no locking of its own, so concurrent processes
    race on it. And concurrent processes multiply the token mints a single
    failure triggers. rm_diff's two-worker pool, rm_watch's drains and the
    rm-mcp server all reach the cloud through here.
    """
    wait = (_env_seconds("RM_RMAPI_LOCK_TIMEOUT_S",
                         DEFAULT_RMAPI_LOCK_TIMEOUT_S)
            if timeout is None else timeout)
    lock_file = rmapi_lock_file()

    def _busy() -> Exception:
        return RmapiBusyError(
            f"another rm tool held {lock_file.name} for over {wait:.0f}s "
            f"(likely a long drain in another window); nothing was sent to "
            f"the cloud. Wait for it to finish and retry.")

    with _exclusive_file_lock(lock_file, wait, poll, _busy):
        yield


def _rmapi_child_env() -> dict[str, str]:
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    env.setdefault("RMAPI_CONCURRENT", RMAPI_CONCURRENT_DEFAULT)
    return env


def _spawn_rmapi(args: tuple[str, ...], cwd: str | Path | None,
                 timeout: float,
                 lock_timeout: float | None = None) -> subprocess.CompletedProcess:
    """One rmapi process, under the lock, behind the cooldown, paced.

    The throttle is detected and recorded INSIDE the lock, so a call queued
    behind the one that drew the 429 sees the cooldown and never spawns. The
    cooldown is re-checked after pacing, since a pause is long enough for
    another process to have recorded one.
    """
    _refuse_if_throttled()  # before queueing: no wait to be refused anyway
    with rmapi_lock(timeout=lock_timeout):
        _refuse_if_throttled()
        _pace()
        _refuse_if_throttled()
        try:
            proc = subprocess.run(
                [RMAPI_BIN, "-ni", *args], capture_output=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                cwd=str(cwd) if cwd is not None else None,
                stdin=subprocess.DEVNULL, env=_rmapi_child_env(),
            )
        except FileNotFoundError as exc:
            # Never retried: a binary that is absent on attempt 1 is absent on
            # attempt 3, and the hint lists cannot classify an exception that
            # carries no stdout or stderr to match against. Nothing reached
            # the cloud, so there is no call to stamp.
            raise RmapiNotFoundError(
                f"rmapi could not be executed as {RMAPI_BIN!r} "
                f"(configured: {RMAPI_BIN_CONFIGURED!r}). "
                f"{RMAPI_NOT_FOUND_REMEDY}"
            ) from exc
        except subprocess.TimeoutExpired:
            _stamp_last_call()  # it was talking to the cloud until killed
            raise
        _stamp_last_call()
        if proc.returncode != 0:
            combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
            if looks_throttled(_without_path_args(combined, args)):
                until = _arm_throttle(args, combined)
                raise _throttled_error(until, _throttle_line(combined), proc)
        return proc


def run_rmapi(*args: str, check: bool = True,
              cwd: str | Path | None = None,
              timeout: float = 180.0,
              retries: int = 0,
              retry_delay: float = 2.0,
              lock_timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run rmapi with arguments; capture stdout/stderr.

    lock_timeout: seconds to wait for another rm tool's rmapi call. None (the
    default) keeps RM_RMAPI_LOCK_TIMEOUT_S / DEFAULT_RMAPI_LOCK_TIMEOUT_S, so
    drains and CLIs behave as before; interactive callers (rm-mcp push tools)
    pass a shorter bound so they return before their client gives up.

    The one shared invocation path for every rm_*.py CLI and the rm-mcp
    server (replaces six divergent per-file _rmapi copies). Guarantees:

      - encoding='utf-8', errors='replace' -- device names with en-dashes
        survive a cp1252 console.
      - stdin=DEVNULL and `-ni` -- an unpaired rmapi aborts ("missing token,
        not asking", an AUTH_HINT) instead of waiting on a one-time code.
      - MSYS_NO_PATHCONV=1 in the child env -- Git Bash must not rewrite
        /device/paths into C:/ paths (previously only ambient via rm-mcp).
      - RMAPI_CONCURRENT=4 in the child env unless already set.
      - One rmapi process at a time (rmapi_lock); RmapiBusyError if another
        rm tool holds it past RM_RMAPI_LOCK_TIMEOUT_S.
      - At least RM_RMAPI_MIN_INTERVAL_S (default 2s) between the end of one
        rmapi process and the start of the next, across every rm tool.
      - Global flags go first: run_rmapi("-json", "ls", path).
      - A 429 is a hard stop: RmapiThrottledError, whatever `check` says, and
        every later call refuses without spawning until the cooldown ends.
      - Bounded retry (opt-in via retries=) ONLY on transient signatures or
        TimeoutExpired; never on throttle, auth hints or not-found. Backoff
        is exponential in retry_delay (delay, 2*delay, 4*delay, ...).
      - check + rc!=0 raises RmapiAuthError on auth hints, else RmapiError.
    """
    attempts = max(0, retries) + 1
    proc: subprocess.CompletedProcess | None = None
    for attempt in range(1, attempts + 1):
        try:
            proc = _spawn_rmapi(args, cwd, timeout, lock_timeout)
        except subprocess.TimeoutExpired:
            if attempt < attempts:
                time.sleep(retry_delay * (2 ** (attempt - 1)))
                continue
            raise
        if proc.returncode == 0:
            return proc
        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        if looks_unauthenticated(combined):
            break  # never retry auth failures
        if attempt < attempts and _looks_transient(combined):
            time.sleep(retry_delay * (2 ** (attempt - 1)))
            continue
        break
    assert proc is not None
    if check and proc.returncode != 0:
        message = (
            f"rmapi {' '.join(args)} failed (exit {proc.returncode}):\n"
            f"  stdout: {(proc.stdout or '').strip()}\n"
            f"  stderr: {(proc.stderr or '').strip()}"
        )
        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        if looks_unauthenticated(combined):
            raise RmapiAuthError(message, proc)
        raise RmapiError(message, proc)
    return proc


def cli_main(main: Callable[[], int | None]) -> NoReturn:
    """Run an rm CLI's main() and exit, mapping the two lane-wide conditions
    to their own exit codes so rm-mcp (envelope.classify) and rm_watch can
    tell them from an ordinary failure: a held state lock (EXIT_STATE_LOCKED)
    and a reMarkable cloud throttle (EXIT_THROTTLED). Each prints one line,
    not a traceback.
    """
    try:
        code = main()
    except StateLockTimeout as exc:
        print(f"[x] {exc}", file=sys.stderr)
        sys.exit(EXIT_STATE_LOCKED)
    except RmapiThrottledError as exc:
        print(f"[x] {exc}", file=sys.stderr)
        sys.exit(EXIT_THROTTLED)
    sys.exit(code)


# ── calibration + filename constants (single source of truth) ───────────────

# PDF-backed-document transform constant: rmscene units per PDF point.
# Derived empirically from the Screen Calibration test (2026-05-22): across
# 3 test pages with different scroll/zoom state the rmscene<->PDF affine had
# scale ~3.16 on both axes, tx = -PDF_W * scale / 2 (page x-centred around
# rmscene x=0), ty ~ 0 (page top edge at rmscene y=0), RMS residual ~1-2
# rmscene units (sub-millimetre on the device). Scroll and zoom do NOT change
# the transform -- coords are page-absolute. FROZEN: consumed by
# rm_render_page / rm_flatten / rm_extract_highlights and echoed read-only by
# rm-mcp; never override at runtime.
PDF_RM_SCALE = 3.16

# Authoring page size for PDFs we GENERATE for the device, in points.
# The RM2 screen is 1404 x 1872 px (3:4). A4 (595 x 842 pt, 1:1.415) is a
# taller ratio, so an A4 page letterboxes on the device and gives away screen
# area on every side -- which is what every pusher in this repo did until
# 2026-08-18. 468 x 624 is exactly device pixels / 3, so it fills the screen
# with no bars.
#
# This governs pages we author. It does NOT apply to:
#   - PDFs that arrive from outside (papers, books) -- their size is theirs
#   - the calibration sheets (rm_make_screen_calibration.py,
#     rm_make_pen_calibration.py), which stay A4 because PDF_RM_SCALE above
#     was derived from that exact geometry and prior runs must stay comparable
#   - rm_make_dossier.py, whose glyphs are hand-positioned against A4
#     Helvetica metrics. Since 2026-09-26 a dossier is printed or published to
#     Notion, never pushed, so A4 is its right page, not a device compromise
RM_PAGE_W_PT: float = 468.0
RM_PAGE_H_PT: float = 624.0

# Stroke-merge detector thresholds -- the two knobs of
# rm_render_page.detect_stroke_merge (the rmscene 0.8.0 connector-artifact
# scanner behind --check-only). Unlike PDF_RM_SCALE these are deliberately NOT
# frozen: they are hand-calibrated against the labeled fixture corpus
# (rm-mcp/tests/fixtures/render/labels.json) and the calibration sweep
# (tools/rm_calibrate_merge.py) can propose new values. Env-overridable so a
# sweep, a CI gate, or a one-off check can retune without a code edit; the
# rm_render_page CLI also exposes them as --merge-jump-threshold /
# --merge-pressure-min (which fall back to these).
#   MERGE_JUMP_THRESHOLD -- inter-point distance (rmscene units) a jump must
#     exceed to be a candidate connector; genuine in-letter sampling is ~3-11
#     units and real cursive pen-movement rarely exceeds ~30.
#   MERGE_PRESSURE_MIN -- 0-255 pen pressure a jump must exceed to count as
#     "severe" (a full-pressure pen-down reposition, not a low-pressure skim).
MERGE_JUMP_THRESHOLD: float = float(os.environ.get("RM_MERGE_JUMP_THRESHOLD", "60.0"))
MERGE_PRESSURE_MIN: int = int(os.environ.get("RM_MERGE_PRESSURE_MIN", "189"))

# Max length of the device-side filename stem (i.e. without extension).
# reMarkable's UI truncates long names visually, so we keep stems short
# enough to read at-a-glance in the library view.
FILENAME_STEM_MAX = 80


def safe_folder_name(name: str) -> str:
    """Sanitise a collection/folder name for use as a reMarkable folder."""
    for bad in ["/", "\\", ":", "*", "?", '"', "<", ">", "|"]:
        name = name.replace(bad, "-")
    return name.strip() or "untitled"


def safe_filename_stem(name: str) -> str:
    """Sanitise a title for use as a reMarkable filename stem.

    Replaces filename-illegal characters, collapses whitespace, strips
    leading/trailing punctuation, and truncates to FILENAME_STEM_MAX chars.
    Returns 'untitled' if the input is empty after cleaning.
    """
    if not name:
        return "untitled"
    for bad in ["/", "\\", ":", "*", "?", '"', "<", ">", "|", "\n", "\r", "\t"]:
        name = name.replace(bad, " ")
    name = " ".join(name.split())
    name = name.strip(" .,-")
    if len(name) > FILENAME_STEM_MAX:
        name = name[:FILENAME_STEM_MAX].rstrip(" .,-")
    return name or "untitled"


# ── structured warnings + JSON run summaries ────────────────────────────────

def make_warning(code: str, message: str, *,
                 possible_data_loss: bool = False,
                 data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build one structured warning for a CLI summary / RmResult envelope.

    Known codes (documented in rm_mcp/envelope.py): rmscene_newer_format,
    mtime_only_touch, zero_ink, collection_name_resolved,
    project_case_matched, epub_untested_roundtrip, state_record_failed,
    geta_placeholder, rmapi_not_found.
    """
    return {"code": code, "message": message,
            "possible_data_loss": possible_data_loss, "data": data or {}}


def write_json_summary(path: str | Path | None, summary: dict) -> None:
    """Write a machine-readable run summary (--json-summary flag target).

    No-op when path is None so CLIs can call this unconditionally. Per-run
    scratch, not shared state -- a plain write is fine here.
    """
    if path is None:
        return
    Path(path).write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
