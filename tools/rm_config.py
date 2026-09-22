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
/Stacks to /104_Stacks and silently zeroed every pull until .rm_state.json was
hand-migrated AND a stale `DEVICE_READING_ROOT = "/Stacks/Reading"` literal was
found in rm_push.py. Deriving every device path from a single RM_ROOT means the
next renumber is a one-line change here (or in .env), not a hunt-and-migrate.

A second reorg (device-side, deliberate, confirmed live via /rm-diff on
2026-07-03) nested every top-level project folder under /00_Projects/ --
/104_Stacks became /00_Projects/104_Stacks.

A third reorg (found 2026-08-27 by a live rmapi walk, ruled deliberate) lifted
Reading OUT of the Stacks root to /00_Projects/Reading, making it a shared
top-level lane. This one broke the "one root, one line" assumption above: the
reading desk no longer derives from RM_ROOT at all. It cost 95 documents'
worth of silently undrained annotations before anyone looked, because rm_pull
skips a missing path per-document rather than failing. When a lane moves, the
tell is a drain that reports plenty of candidates and pulls none of them.
RM_ROOT was repointed here on 2026-07-03; rm-mcp (Overseer-owned, a separate
codebase) needs the same fix independently -- flagged via crosstalk.
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
from typing import Any, Callable, Iterator

try:
    import rm_state_remote
except ImportError:  # pragma: no cover -- the public tree has no cloud lane
    # rm_state_remote is the shared-ledger (GCS) lane, and tools/
    # rm_build_public.py deliberately does NOT ship the cloud lane, so the
    # public tree has no such module. Before this guard landed the import was
    # hard, which made rm_config -- the module 5 of the 9 shipped scripts
    # import -- fail on import in the public tree: the package was broken from
    # the first line. Caught 2026-09-20 (S112) by the first clean rebuild since
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
# litgather/ and common/ on 2026-09-20 (S112) while the public tree stayed
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
    104_stacks/.env > workspace/.env. Keys already present in os.environ when
    this runs (i.e. set by the real shell) are never overwritten; among the
    .env files, the more specific (closer to the tools dir) wins.

    Idempotent: a second call is a no-op because every key loaded by the first
    call is then already present in os.environ.
    """
    preset = set(os.environ)  # real / shell-provided keys: never override these
    candidates = [
        _WORKSPACE_DIR / ".env",   # most general
        _PROJECT_DIR / ".env",     # 104_stacks/.env
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
# "/00_Projects/104_Stacks" -- Bradley's own folder -- which also disagreed
# with the documented default in the same build; until 2026-09-10 it was
# "/00_Projects/Notes", still his layout with the name filed off. The root is
# the one folder every owner has ("My files"). Set RM_ROOT (tools/.env wins)
# to point a machine at its real tree; the desk does.
RM_ROOT = "/" + os.environ.get("RM_ROOT", "/").strip().strip("/")


def device_root(name: str) -> str:
    """Normalise an arbitrary top-level device root.

    The reMarkable tree has several top-level project roots, all siblings:
    /104_Stacks (the Stacks substrate), /203_lightroom, etc. RM_ROOT is the
    Stacks default; this lets any tool address a different per-project root
    without that root being baked into the substrate.

    >>> device_root("203_lightroom")
    '/203_lightroom'
    >>> device_root("/104_Stacks/")
    '/104_Stacks'
    """
    return "/" + name.strip("/")


def under(root: str, *parts: str) -> str:
    """Join device-path segments under an arbitrary device root.

    Generalises rm_path() (which is hardwired to RM_ROOT) to any root, so a
    push tool can target /203_lightroom/<session>/ the same way Stacks tools
    target /104_Stacks/Reading/<collection>/.

    >>> under("203_lightroom", "session-1", "contact sheets")
    '/203_lightroom/session-1/contact sheets'
    """
    base = device_root(root)
    cleaned = [p.strip("/") for p in parts if p]
    return "/".join([base, *cleaned]) if cleaned else base


def rm_path(*parts: str) -> str:
    """Join device-path segments under RM_ROOT (the Stacks root).

    Thin convenience over under() for the common Stacks case.

    >>> rm_path("Reading", "Some Paper")    # with RM_ROOT=/Notes
    '/Notes/Reading/Some Paper'
    """
    return under(RM_ROOT, *parts)


# Reading is NOT under RM_ROOT. A third device-side reorg lifted the reading desk
# out of /00_Projects/104_Stacks to sit at /00_Projects/Reading as a shared
# top-level lane (found 2026-08-27, ruled deliberate by Bradley). It therefore
# carries its own overridable root instead of being derived from RM_ROOT.
#
# Deriving it from RM_ROOT is exactly what broke: rm_pull passes the full stored
# path to `rmapi stat`, so after the move all 95 tracked documents stat'd as
# missing, and the drain printed "[?] not found on device, skip" per document and
# reported nothing to do -- indistinguishable from a clean run. Every annotation
# made since the move sat undrained. That is the same failure the /Stacks ->
# /104_Stacks renumber caused above; the lesson held, but the assumption that
# every lane stays under one root did not.
#
# 2026-09-07: moved again, deliberately and this time WITH the tooling -- the
# desk is now a first-class top-level lane at /Reading, on Bradley's instruction
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
# reliable pool. The literature tools each hardcoded ONE address -- Bradley's
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
# inside Bradley's home directory -- which no other machine has, so the
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
# CORRECTED 2026-09-20 (crosstalk 0b439074, homelab). This comment asserted a
# 2.5 retirement on 2026-10-16. THERE IS NO SUCH DATE: Google's deprecations
# page lists the undated base ids gemini-2.5-flash / gemini-2.5-pro as "no
# shutdown date announced", and the 2026-10-16 figure was manufactured by
# reading the DATED preview snapshots' real shutdown dates onto the undated
# base ids. A lockout is not a retirement. roundtrip.py and SHIP-FLAVOUR-2.md
# were corrected when homelab found this; THIS file, which those corrections
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
    msvcrt.locking on Windows, fcntl.flock elsewhere. Raises StateLockTimeout
    when another process holds the lock past `timeout` seconds.
    """
    STATE_LOCK_FILE.touch(exist_ok=True)
    fh = open(STATE_LOCK_FILE, "r+b")
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
                    raise StateLockTimeout(
                        f"another rm tool holds {STATE_LOCK_FILE.name} "
                        f"(likely an rm_pull/rm_push in another window); "
                        f"wait for it to finish and retry"
                    ) from None
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
)

# Signatures of transient cloud/network failures that are safe to retry.
# Auth failures and not-found are never retried.
#
# The rate-limit hints matter more than they look. Every rmapi invocation is a
# fresh process that exchanges the device token for a user token, so a sweep
# that shells out per document (rm_diff stat-ing 279 files) hammers the token
# endpoint and earns "failed to create user token from device token request
# failed with status 429". That text matches no auth hint, so before these
# entries existed a 429 was classified as a hard failure and never retried --
# which is what let rm_diff mistake 163 live documents for deletions on
# 2026-08-24. Kept specific ("status 429", not a bare "429") so a document
# whose NAME contains the digits cannot trigger a spurious retry.
TRANSIENT_HINTS: tuple[str, ...] = (
    "connection reset",
    "connection refused",
    "timeout",
    "timed out",
    "temporar",
    "503",
    "502",
    "tls handshake",
    "status 429",
    "too many requests",
    "rate limit",
)


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
    their own arguments (crosstalk 7981b341, 305_krisis).

    FileNotFoundError is an OSError, NOT a RuntimeError, which is exactly why
    it slipped past handlers that looked complete.
    """


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


def run_rmapi(*args: str, check: bool = True,
              cwd: str | Path | None = None,
              timeout: float = 180.0,
              retries: int = 0,
              retry_delay: float = 2.0) -> subprocess.CompletedProcess:
    """Run rmapi with arguments; capture stdout/stderr.

    The one shared invocation path for every rm_*.py CLI and the rm-mcp
    server (replaces six divergent per-file _rmapi copies). Guarantees:

      - encoding='utf-8', errors='replace' -- device names with en-dashes
        survive a cp1252 console.
      - stdin=DEVNULL -- an unpaired rmapi asking for a one-time code can
        never hang the caller.
      - MSYS_NO_PATHCONV=1 in the child env -- Git Bash must not rewrite
        /device/paths into C:/ paths (previously only ambient via rm-mcp).
      - Bounded retry (opt-in via retries=) ONLY on transient signatures or
        TimeoutExpired; never on auth hints or not-found. Backoff is
        exponential in retry_delay (delay, 2*delay, 4*delay, ...) so a
        rate-limited call actually gets a chance to recover.
      - check + rc!=0 raises RmapiAuthError on auth hints, else RmapiError.
    """
    cmd = [RMAPI_BIN, *args]
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    attempts = max(0, retries) + 1
    proc: subprocess.CompletedProcess | None = None
    for attempt in range(1, attempts + 1):
        try:
            proc = subprocess.run(
                cmd, capture_output=True, encoding="utf-8", errors="replace",
                timeout=timeout, cwd=str(cwd) if cwd is not None else None,
                stdin=subprocess.DEVNULL, env=env,
            )
        except FileNotFoundError as exc:
            # Never retried: a binary that is absent on attempt 1 is absent on
            # attempt 3, and TRANSIENT_HINTS/AUTH_HINTS cannot classify an
            # exception that carries no stdout or stderr to match against.
            raise RmapiNotFoundError(
                f"rmapi could not be executed as {RMAPI_BIN!r} "
                f"(configured: {RMAPI_BIN_CONFIGURED!r}). "
                f"{RMAPI_NOT_FOUND_REMEDY}"
            ) from exc
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
            # Exponential, not fixed: a rate limiter answers a prompt retry
            # with another 429, so each attempt must wait longer than the last.
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
#     Helvetica metrics -- a re-layout, not a constant swap
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
