"""Configuration for rm-mcp.

Bridges to the substrate config (tools/rm_config.py) via a
sys.path insert, and adds the rm-mcp-only pieces: the /Projects/<code> device
lane, project resolution, session output directories, and filename sanitising.

The server process sets MSYS_NO_PATHCONV / PYTHONUTF8 / PYTHONIOENCODING in
its own os.environ at import time so every child process -- including the
rmapi subprocesses spawned *inside* the wrapped CLIs -- inherits them. This
compensates for the substrate scripts that do not set MSYS_NO_PATHCONV
themselves (rm_pull.py, rm_diff.py) without editing any substrate file.
"""

from __future__ import annotations

import itertools
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

RM_MCP_DIR = Path(__file__).resolve().parent.parent
STACKS_DIR = RM_MCP_DIR.parent


def _find_tools_dir() -> Path:
    """Locate the substrate scripts, in either layout.

    This file must work UNCHANGED in two trees, because the public repo is a
    file copy of this one -- no fork. The previous extraction hardcoded the
    embedded layout, drifted two days out of date and silently lost a security
    fix; one config that detects its own layout is what stops that recurring.

        embedded (monorepo):      <stacks>/rm-mcp/rm_mcp/  + <stacks>/tools/
        standalone (public):     <repo>/rm_mcp/           + <repo>/tools/

    RM_MCP_TOOLS_DIR overrides both for an unusual deployment.
    """
    override = os.environ.get("RM_MCP_TOOLS_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    standalone = RM_MCP_DIR / "tools"
    if _holds_substrate(standalone):
        return standalone
    return STACKS_DIR / "tools"


# The subpackages tools/ was split into on 2026-09-20. The PUBLIC tree
# is still flat -- the build copies the manifest with its basename -- so both
# layouts are live at once and this file has to read either. Named here rather
# than imported from tools/_split_plan.py, which does not ship; the standing
# constraint on this module is that it works UNCHANGED in both trees.
TOOL_BUCKETS = ("rm", "zotero", "litgather", "common")


def _holds_substrate(root: Path) -> bool:
    """Whether `root` looks like a tools/ dir, flat or split."""
    return ((root / "rm_config.py").is_file()
            or any((root / b / "rm_config.py").is_file() for b in TOOL_BUCKETS))


def _import_roots(tools_dir: Path) -> list[Path]:
    """Directories that must be on sys.path for a bare `import rm_config`.

    Flat tree: tools/ itself. Split tree: tools/ plus each bucket that exists,
    because the scripts still import each other by bare name.
    """
    roots = [tools_dir]
    roots += [tools_dir / b for b in TOOL_BUCKETS if (tools_dir / b).is_dir()]
    return roots


def resolve_script(name: str) -> Path:
    """Locate one substrate CLI by bare filename, in either layout.

    Returns the flat path when nothing matches, so the caller's
    "is not available in this build" branch still reports a sensible path.
    """
    direct = TOOLS_DIR / name
    if direct.is_file():
        return direct
    for bucket in TOOL_BUCKETS:
        candidate = TOOLS_DIR / bucket / name
        if candidate.is_file():
            return candidate
    return direct


TOOLS_DIR = _find_tools_dir()

for _root in _import_roots(TOOLS_DIR):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

import rm_config  # noqa: E402  (the substrate config; loads .env at import)

RMAPI_BIN = rm_config.RMAPI_BIN
# What the operator ASKED for vs what was actually found on disk. rm_health
# reports both, because the gap between them is the whole diagnosis: a bare
# name that resolves nowhere looks identical to an absolute path that is
# simply absent, and only the configured value tells you which to fix.
RMAPI_BIN_CONFIGURED = rm_config.RMAPI_BIN_CONFIGURED
RMAPI_RESOLVED = rm_config.RMAPI_RESOLVED
RM_ROOT = rm_config.RM_ROOT

# Inherited by every subprocess, including rmapi calls made inside the
# wrapped CLIs. PYTHONUTF8 guards against UnicodeEncodeError from em-dash
# prints on a cp1252 pipe; the interpreter running this server is unaffected
# (the var is only read at interpreter startup).
os.environ["MSYS_NO_PATHCONV"] = "1"
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"


# -- /Projects device lane ---------------------------------------------------

def _norm_root(raw: str) -> str:
    """Normalise a device root to a leading slash and no trailing slash."""
    return "/" + (raw or "").strip().strip("/")


# Where projects live on the tablet. DEFAULT IS THE DEVICE ROOT ("My files"),
# ruled 2026-09-10: a project is any top-level folder the user names,
# because the root is the one place every reMarkable owner already has. The
# earlier default, /00_Projects, was the author's own convention (renamed
# on-device 2026-07-03 so the 00_ prefix sorts it to the top) and the desk
# keeps it by setting RM_MCP_PROJECTS_ROOT in tools/.env. Deliberately NOT
# rm_config.PROJECTS_ROOT (which is <RM_ROOT>/Projects, the owning project's
# planning desk).
PROJECTS_DEVICE_ROOT = _norm_root(
    os.environ.get("RM_MCP_PROJECTS_ROOT", "/"))


def join_root(root: str, name: str) -> str:
    """Join one segment under a device root without producing "//name"."""
    return f"{root.rstrip('/')}/{name.strip('/')}"


def _managed_roots() -> tuple[str, ...]:
    """The device subtrees this server is allowed to touch.

    Everything else on the paired account (personal notebooks, unrelated
    trees) needs the explicit allow_anywhere=True escape, and on the
    network-exposed lane cannot be read at all -- see read_scope_enforced.

    Defaults to the project lane plus RM_ROOT, which is what a single-owner
    deployment wants. RM_MCP_MANAGED_ROOTS overrides the pair entirely, as a
    comma-separated list, for a deployment whose device layout is neither.

    "/" IS THE WHOLE DEVICE, AND IT IS LEGAL. Until 2026-09-10 a bare slash was
    dropped on the grounds that it silently disabled the guard. Now that the
    default project lane IS the device root, refusing it would leave a fresh
    install with no lane at all. Instead it is made loud: "/" subsumes every
    other entry, the tuple collapses to ("/",), and rm_health reports
    guard_scope as "whole device" rather than pretending a list of roots is
    in force. Narrowing is one .env line (RM_MCP_MANAGED_ROOTS), and
    `run_server.py --init` offers it.
    """
    override = os.environ.get("RM_MCP_MANAGED_ROOTS", "").strip()
    if override:
        roots = [_norm_root(r) for r in override.split(",") if r.strip()]
    else:
        # RM_ROOT comes from the substrate config, which only strips a trailing
        # slash -- it does not guarantee a leading one. An entry without it can
        # never match _guard_path (which always normalises the candidate to a
        # leading slash), so the root would silently stop being managed.
        # RM_ROOT joins the pair only when it was SET (shell or .env). Its
        # unset default is the device root, and letting that default widen a
        # deliberately narrowed project lane back to the whole device would
        # undo the one .env line the user wrote.
        roots = [PROJECTS_DEVICE_ROOT]
        if os.environ.get("RM_ROOT", "").strip():
            roots.append(_norm_root(RM_ROOT))
    if "/" in roots:
        return ("/",)
    seen: dict[str, None] = {}
    for r in roots:
        seen.setdefault(r, None)
    return tuple(seen) or (PROJECTS_DEVICE_ROOT,)


# Single source of truth -- manage.MANAGED_ROOTS aliases this.
MANAGED_ROOTS = _managed_roots()


def guard_scope() -> str:
    """What the destructive-tool guard actually covers, in words, for rm_health."""
    return "whole device" if MANAGED_ROOTS == ("/",) else "managed roots only"


def destructive_allowed() -> bool:
    """Whether rm_move / rm_delete may actually execute (dry runs always may).

    Transport is the policy. On stdio the client is a local, trusted desktop
    registration and deletes behave as before. On streamable-http the server
    is an internet-reachable endpoint whose only gate is a shared secret, so
    a leaked token must not be able to wipe the device library: execution is
    refused unless the deploy explicitly opts back in.

    SINCE TOKEN SCOPING, THE OPT-IN IS A CREDENTIAL, NOT A FLAG. Minting
    RM_MCP_ADMIN_TOKEN is the deliberate act that RM_MCP_ALLOW_DESTRUCTIVE
    used to be, and it is strictly better: the flag said "somebody may
    delete", the token says WHICH caller may, and leaves the read and write
    tokens unable to. The flag is still honoured so existing deployments keep
    working, but it is deprecated -- see authz.py.

    This is the SECOND layer, not the first. authz/wire refuse a destructive
    call at the HTTP boundary before the tool is ever entered; this guard is
    what still holds if that chain is misconfigured or bypassed, which is why
    a deployment with no admin credential refuses here regardless.

    Read at call time, not import time, so a deploy can flip it without a
    code change and tests can patch the environment.
    """
    optin = os.environ.get("RM_MCP_ALLOW_DESTRUCTIVE", "").strip().lower()
    if optin in ("1", "true", "yes"):
        return True
    if os.environ.get("RM_MCP_TRANSPORT", "stdio") != "streamable-http":
        return True
    from . import authz  # local: keeps config importable from bare scripts
    return authz.admin_token_configured()

def read_scope_enforced() -> bool:
    """Whether READ tools are confined to the managed roots.

    Same principle as destructive_allowed: transport is the policy. On stdio
    the client is a local, trusted desktop registration and whole-device
    browsing is legitimate (rm_diff walks every tree). On streamable-http the
    only gate is a shared secret, and an unscoped read tool lets a leaked
    token enumerate and rasterise the entire paired account -- Business Time,
    Science, personal notebooks -- not just the collaboration folders. So the
    network-exposed lane is scoped by default.

    Read at call time so a deploy can flip it without a code change.
    """
    optin = os.environ.get("RM_MCP_ALLOW_READ_ANYWHERE", "").strip().lower()
    if optin in ("1", "true", "yes"):
        return False
    return os.environ.get("RM_MCP_TRANSPORT", "stdio") == "streamable-http"


# Folder-name shape for a project under PROJECTS_DEVICE_ROOT. DEFAULT: any
# single folder name (ruled 2026-09-10 with the root move -- a stranger's
# project is whatever they called the folder). The NNN_name shape is the
# author's convention; the desk sets RM_MCP_PROJECT_PATTERN to it in tools/.env.
#
# The pattern also decides how much auto-detection is allowed when project= is
# not passed. With the NNN shape a parent-directory walk is safe: only a
# deliberately named directory can match. With the permissive default it is
# not -- ".*" would make the walk match the first directory it meets and push
# into "/Documents" -- so detection then trusts only CLAUDE_PROJECT_DIR, which
# the client sets to the project it actually opened, and otherwise asks for
# project= explicitly.
NNN_PATTERN = r"^\d{3}_[A-Za-z0-9][A-Za-z0-9_\-]*$"
ANY_FOLDER_PATTERN = r"^[^/\\]+$"
_PROJECT_PATTERN = os.environ.get("RM_MCP_PROJECT_PATTERN", ANY_FOLDER_PATTERN)
_PROJECT_CODE_RE = re.compile(_PROJECT_PATTERN)

# Only used to make the error messages concrete; a custom pattern gets the
# pattern itself quoted back instead of a misleading NNN_name example.
_IS_NNN_PATTERN = _PROJECT_PATTERN.startswith(r"^\d{3}_")
_PROJECT_SHAPE = ("NNN_name" if _IS_NNN_PATTERN
                  else "a folder name" if _PROJECT_PATTERN == ANY_FOLDER_PATTERN
                  else _PROJECT_PATTERN)
# Neutral on purpose: this string reaches a user's error message in the public
# build, so it must not name one of the author's own projects.
NNN_EXAMPLE = "100_thesis"
_PROJECT_EXAMPLE = f" (e.g. {NNN_EXAMPLE})" if _IS_NNN_PATTERN else ""


def resolve_project(project: str | None = None) -> str:
    """Resolve the project code (the full project directory segment).

    Precedence: explicit argument > CLAUDE_PROJECT_DIR env > (NNN pattern
    only) nearest matching ancestor of the cwd. Raises ValueError with an
    actionable message when nothing resolves -- for a globally registered
    stdio server the env and cwd are host-dependent, so callers should pass
    project= explicitly.
    """
    if project:
        code = project.strip().strip("/")
        if not code or "/" in code or "\\" in code or ".." in code:
            raise ValueError(f"invalid project code {project!r}: "
                             "must be a single path segment")
        if not _PROJECT_CODE_RE.match(code):
            raise ValueError(f"invalid project code {project!r}: "
                             f"expected {_PROJECT_SHAPE}{_PROJECT_EXAMPLE}")
        return code

    env_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if env_dir:
        name = Path(env_dir).name
        if _PROJECT_CODE_RE.match(name):
            return name

    if _IS_NNN_PATTERN:
        for parent in [Path.cwd(), *Path.cwd().parents]:
            if _PROJECT_CODE_RE.match(parent.name):
                return parent.name

    raise ValueError(
        f"could not resolve a project code: pass project=<{_PROJECT_SHAPE}>"
        f"{_PROJECT_EXAMPLE}"
    )


def project_device_dir(project: str | None = None) -> str:
    """Device folder for a project under the shared project tree."""
    return join_root(PROJECTS_DEVICE_ROOT, resolve_project(project))


# -- filename sanitising -----------------------------------------------------

# Imported from rm_config (import-safe, unlike rm_push which exits at module
# level when Zotero env keys are missing). Previously copied verbatim here;
# the copies were retired in v2 when the definitions moved to rm_config.
FILENAME_STEM_MAX = rm_config.FILENAME_STEM_MAX
safe_filename_stem = rm_config.safe_filename_stem


# -- session output directories ----------------------------------------------

# All tool outputs land under a per-server-process session dir, never inside
# the repository tree (rm_capture/rm_triage/rm_notebook_pull default their
# workspaces INSIDE the repo -- we always override with explicit paths).
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
SESSION_DIR = (Path(_LOCALAPPDATA) / "rm-mcp" / "sessions"
               / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}")

_seq = itertools.count(1)

# Per-call output dirs accumulate under SESSION_DIR for the whole life of the
# process. On Cloud Run the container filesystem is RAM-backed, so every
# rendered PNG / flattened PDF / .rmdoc bundle a pull writes counts against the
# instance memory limit -- and prune_sessions only runs ONCE at startup and
# never touches the current session. Left unbounded this is the "per-session
# memory leak": the 2nd (or multi-page) pull OOMs because the first pull's
# artifacts are still resident. We bound it by evicting all but the newest
# KEEP_OUTPUTS per-call dirs whenever a new one is created.
#
# Remote (streamable-http) is BOTH the OOM environment and the one where the
# returned artifact paths are useless to the client anyway -- they name the
# container's own filesystem, not the caller's -- so evict aggressively there.
# Local stdio keeps a generous window because the co-located agent reads those
# files across later turns to route them (e.g. rm_pull_project -> rm_interpret_page
# on the same png_dir). Override the window with RM_MCP_KEEP_OUTPUTS.
_DEFAULT_KEEP_OUTPUTS = (
    2 if os.environ.get("RM_MCP_TRANSPORT") == "streamable-http" else 40)
try:
    KEEP_OUTPUTS = max(1, int(
        os.environ.get("RM_MCP_KEEP_OUTPUTS", _DEFAULT_KEEP_OUTPUTS)))
except ValueError:
    KEEP_OUTPUTS = _DEFAULT_KEEP_OUTPUTS

# A per-call output dir is handed BACK to the caller, which reads it on a LATER
# request (rm_pull_project -> rm_interpret_page on the same png_dir). Global FIFO
# eviction has no notion of which caller owns a dir, so with two concurrent
# callers on one warm instance, caller B's request could evict caller A's
# just-returned dir before A read it -- silent data loss on a path we told the
# caller to use. There is no client identity to key retention on: streamable-http
# carries none of its own, and the 2026-07-28 stateless spec removes
# Mcp-Session-Id, so keying on that would build on something being deleted.
#
# Retention is therefore bounded by AGE as well as count: a dir younger than
# OUTPUT_GRACE_SECONDS is never evicted, whatever the queue depth. If that holds
# the queue above KEEP_OUTPUTS the excess is RETAINED and counted, not deleted --
# a loud OOM is a better failure than quietly removing data a caller still holds
# a path to, and capture-path data loss is the worst defect class this project
# has. Grace defaults to 0 on stdio, where a single co-located caller means there
# is no race and space should be reclaimed promptly.
_DEFAULT_OUTPUT_GRACE = (
    300 if os.environ.get("RM_MCP_TRANSPORT") == "streamable-http" else 0)
try:
    OUTPUT_GRACE_SECONDS = max(0, int(
        os.environ.get("RM_MCP_OUTPUT_GRACE_SECONDS", _DEFAULT_OUTPUT_GRACE)))
except ValueError:
    OUTPUT_GRACE_SECONDS = _DEFAULT_OUTPUT_GRACE

# (path, creation epoch seconds), oldest first.
_out_dirs: list[tuple[Path, float]] = []
_out_lock = threading.Lock()
# How many dirs are currently held ABOVE KEEP_OUTPUTS because they are in grace.
# Non-zero means real memory pressure that eviction is deliberately not relieving.
_grace_retained = 0


def new_out_dir(tool: str) -> Path:
    """Fresh absolute output directory for one tool call.

    Registers the dir for bounded retention: creating a new one evicts the
    oldest per-call dirs beyond KEEP_OUTPUTS (see the note above), so a
    long-lived server does not accumulate rendered artifacts in RAM-backed
    storage across pulls. The newest KEEP_OUTPUTS dirs always survive, so the
    artifacts a caller just received stay readable for a healthy window.
    """
    out = SESSION_DIR / tool / f"{next(_seq):04d}"
    out.mkdir(parents=True, exist_ok=True)
    _register_and_evict(out)
    return out


def _register_and_evict(out: Path, now: float | None = None) -> list[str]:
    """Track a new per-call out dir; rmtree those beyond KEEP_OUTPUTS.

    Eviction is by creation order (oldest first), but a dir younger than
    OUTPUT_GRACE_SECONDS is never evicted -- see the note above on the
    concurrent-caller race. The queue is in creation order, so the first
    in-grace dir ends the scan: everything behind it is younger still.

    Best-effort: an undeletable dir is dropped from tracking rather than retried
    forever. rmtree runs outside the lock so a slow delete never blocks a
    concurrent tool call. Returns the deleted paths.
    """
    global _grace_retained
    now = time.time() if now is None else now
    victims: list[Path] = []
    with _out_lock:
        _out_dirs.append((out, now))
        while len(_out_dirs) > KEEP_OUTPUTS:
            path, created = _out_dirs[0]
            if now - created < OUTPUT_GRACE_SECONDS:
                # Oldest is still in grace, so every newer one is too. Hold the
                # excess rather than delete a dir a caller may still be reading.
                _grace_retained = len(_out_dirs) - KEEP_OUTPUTS
                break
            _out_dirs.pop(0)
            victims.append(path)
        else:
            _grace_retained = 0
    deleted: list[str] = []
    for victim in victims:
        if victim.exists():
            shutil.rmtree(victim, ignore_errors=True)
        deleted.append(str(victim))
    return deleted


def output_retention_status() -> dict:
    """Snapshot of per-call output retention, for rm_health.

    retained_in_grace > 0 means dirs are being held above KEEP_OUTPUTS because
    they are too young to evict safely -- the deliberate trade against silent
    deletion, and the number to watch if an instance is running hot on memory.
    """
    with _out_lock:
        return {
            "tracked": len(_out_dirs),
            "keep_outputs": KEEP_OUTPUTS,
            "grace_seconds": OUTPUT_GRACE_SECONDS,
            "retained_in_grace": _grace_retained,
        }


def _session_dir_age(path: Path) -> float:
    """Epoch seconds of a session dir's creation, from its timestamped name
    (YYYYmmdd-HHMMSS-pid) with an mtime fallback."""
    try:
        stamp = "-".join(path.name.split("-")[:2])
        return time.mktime(time.strptime(stamp, "%Y%m%d-%H%M%S"))
    except (ValueError, IndexError):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0


def prune_sessions(root: Path | None = None, keep_days: int = 14,
                   keep_last: int = 20) -> list[str]:
    """Delete old per-process session output dirs (retention policy).

    Keeps the newest keep_last dirs regardless of age plus anything younger
    than keep_days; never touches the CURRENT session dir. Opt out entirely
    with RM_MCP_KEEP_SESSIONS=all. Returns the deleted paths (best-effort:
    a dir that will not delete is skipped, never fatal). Run from a daemon
    thread at server start.
    """
    if os.environ.get("RM_MCP_KEEP_SESSIONS", "").lower() == "all":
        return []
    base = Path(root) if root is not None else SESSION_DIR.parent
    if not base.is_dir():
        return []
    current = SESSION_DIR.resolve()
    candidates = sorted(
        (p for p in base.iterdir() if p.is_dir() and p.resolve() != current),
        key=_session_dir_age, reverse=True)
    cutoff = time.time() - keep_days * 86400
    deleted: list[str] = []
    for path in candidates[keep_last:]:
        if _session_dir_age(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                deleted.append(str(path))
    return deleted


# Frozen calibration constant, surfaced read-only via rm_health. The live
# value is owned by rm_config.PDF_RM_SCALE (single source of truth for the
# render/flatten/highlights substrate); this echo exists so callers can see
# it without any path to overriding it.
PDF_RM_SCALE_FROZEN = rm_config.PDF_RM_SCALE

# The authoring page contract, surfaced alongside the scale constant it is
# calibrated against. Exposing the scale WITHOUT the geometry was a real
# contract gap: on 2026-09-20 a caller authoring a PDF for the device saw
# pdf_rm_scale_frozen 3.16 in rm_health, correctly inferred that it must be
# frozen against some assumed page size, could not find that size anywhere
# on the tool surface, and picked A5 (420 x 595 pt) -- close enough to look
# deliberate, and wrong, so the document letterboxes rather than sitting 1:1
# (reported 2026-09-20). The answer already existed one file away
# in rm_config, set on 2026-08-18 for precisely this reason. A tool that
# accepts a PDF has to state the page it expects.
RM_AUTHORING_PAGE = {
    "width_pt": rm_config.RM_PAGE_W_PT,
    "height_pt": rm_config.RM_PAGE_H_PT,
    "device_px": [1404, 1872],
    "device_dpi": 226,
    "note": ("author generated PDFs at this size -- exactly device pixels / 3, "
             "so the page fills the screen with no letterboxing. A4 "
             "(595 x 842 pt) is a taller ratio and gives away screen area on "
             "every side."),
    "does_not_apply_to": [
        "PDFs that arrive from outside (papers, books) -- their size is theirs",
        "the calibration sheets, which stay A4 because PDF_RM_SCALE was "
        "derived from that exact geometry and prior runs must stay comparable",
        "rm_make_dossier.py, whose glyphs are hand-positioned against A4 "
        "Helvetica metrics -- a re-layout, not a constant swap",
    ],
}
