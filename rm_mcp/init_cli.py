"""`python run_server.py --init` -- walk a fresh clone to a working install.

The server's checks already exist (rm_health reports every prerequisite),
but nothing turned them into the artefacts an install consists of: a
paired rmapi, a .env naming the device roots the user actually has, an MCP
registration that points at the right interpreter, a routing note that
tells the agent where pulled notes and directives go, and a creation note
that says what a document pushed the other way should look like. This
module writes all of them. It is the mechanical half of the setup; SETUP
steps an agent cannot do (pairing needs a browser and a one-time code) are
printed as instructions rather than pretended.

ROUTING.md and CREATION.md are a pair and differ in one way that matters:
routing has no sensible default (only the user knows where their notes go),
so it is written only when an answer is given. A page template DOES have a
sensible default -- the device-native one -- so CREATION.md is always
written, and is never overwritten once it exists.

Two ways to run it:

    python run_server.py --init            interactive, asks each question
    python run_server.py --init --yes ...  non-interactive; every answer is a
                                           flag, defaults for the rest

The second form is what an agent installing on someone's behalf uses. Every
flag is idempotent: re-running merges into the existing .env rather than
clobbering it, and rewrites ROUTING.md only when a routing answer is given.

Nothing here touches the device. Listing the top-level folders is a read;
the one write an install needs (a project folder) happens on first push.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from . import config, creation, device

# Keys --init may write. Anything else in an existing .env is left alone.
_MANAGED_KEYS = ("RMAPI_BIN", "RM_MCP_PROJECTS_ROOT", "RM_ROOT",
                 "RM_MCP_MANAGED_ROOTS", "GEMINI_API_KEY")

_ROUTING_NAME = "ROUTING.md"

_RMAPI_BUILD_HINT = (
    "rmapi is not on PATH. Build it from source (the newest release cannot\n"
    "write to the cloud since 2026-08-17; the fix is in commit f295d54):\n"
    "    go install github.com/ddvk/rmapi@f295d54\n"
    "then run `rmapi` once to pair (it prints a URL and asks for the\n"
    "one-time code from my.remarkable.com/device/desktop/connect).")


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _ask(prompt: str, default: str, yes: bool) -> str:
    if yes:
        return default
    shown = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{shown}: ").strip()
    except EOFError:
        return default
    return answer or default


# -- .env ---------------------------------------------------------------------

def env_path() -> Path:
    """tools/.env: read first in both layouts (see rm_config.load_env)."""
    return config.TOOLS_DIR / ".env"


def merge_env(path: Path, updates: dict[str, str]) -> list[str]:
    """Write `updates` into the .env at `path`, keeping every other line.

    A key already present is replaced in place; a new key is appended under
    a dated marker. Returns the keys written. Never touches a key that is
    not in `updates`, so a hand-edited file survives a re-run.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    if remaining:
        out.append("")
        out.append(f"# written by run_server.py --init on {date.today().isoformat()}")
        out.extend(f"{k}={v}" for k, v in remaining.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    return list(updates)


# -- ROUTING.md ---------------------------------------------------------------

def routing_path() -> Path:
    return config.RM_MCP_DIR / _ROUTING_NAME


def routing_text(notes_to: str, directives_to: str) -> str:
    return f"""# Routing for this reMarkable install

Written by `python run_server.py --init` on {date.today().isoformat()}. Edit it
freely. The agent reads this file after every pull, so it is the one place
that says where things go.

- notes_to: {notes_to}
- directives_to: {directives_to}

## What the agent does after `rm_pull_project`

1. Read `data.typed_text` (keyboard text), `data.highlights` (marked passages)
   and every image in `data.pngs` (handwriting, read directly).
2. Separate NOTES -- content to keep -- from DIRECTIVES -- instructions
   addressed to the agent, or naming a destination ("send this to X",
   "add to the reading list", "draft a reply").
3. Tell the user both, briefly, before filing anything.
4. File notes at `notes_to`. Act on directives, or file them at
   `directives_to` when they are for later. A directive that would delete,
   send or publish something is confirmed with the user first.
"""


# -- the walk -----------------------------------------------------------------

def _rmapi_status(rmapi: str) -> tuple[str, bool | None, list[str]]:
    """(resolved binary or '', authenticated, top-level folder names)."""
    from shutil import which
    resolved = rmapi if Path(rmapi).is_file() else (which(rmapi) or "")
    if not resolved:
        return "", None, []
    os.environ["RMAPI_BIN"] = resolved
    config.rm_config.RMAPI_BIN = resolved  # type: ignore[attr-defined]
    device.RMAPI_BIN = resolved            # type: ignore[attr-defined]
    probe = device.auth_probe()
    if not probe.get("authenticated"):
        return resolved, probe.get("authenticated"), []
    try:
        entries = device.ls("/", timeout=60)
    except Exception:  # listing is informational; pairing is what matters
        return resolved, True, []
    return resolved, True, sorted(e["name"] for e in entries if e.get("type") == "folder")


def _pair_interactively(rmapi: str, yes: bool) -> None:
    """Let rmapi ask for its one-time code on the user's own terminal."""
    if yes:
        _say("  Not paired. Run `rmapi` once in a terminal to pair, then re-run --init.")
        return
    _say("  Not paired. Handing the terminal to rmapi so it can ask for the code...")
    try:
        subprocess.run([rmapi, "ls", "/"], check=False,
                       env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    except OSError as exc:
        _say(f"  could not start rmapi: {exc}")


def registration(python: str, launcher: Path) -> tuple[str, str]:
    """The two spellings of the MCP registration, with absolute paths."""
    block = json.dumps({"rm": {"type": "stdio", "command": python,
                               "args": [str(launcher)], "env": {}}}, indent=2)
    cli = f'claude mcp add rm -- "{python}" "{launcher}"'
    return cli, block


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="run_server.py --init",
        description="Walk this clone to a working rm-mcp install.")
    ap.add_argument("--yes", action="store_true",
                    help="non-interactive: take flags and defaults, ask nothing")
    ap.add_argument("--rmapi", default=os.environ.get("RMAPI_BIN", "rmapi"),
                    help="path to the rmapi binary (default: RMAPI_BIN or PATH)")
    ap.add_argument("--projects-root", default=None,
                    help="device folder projects live under (default: the root, /)")
    ap.add_argument("--managed-roots", default=None,
                    help="comma-separated folders rm_move/rm_delete may touch "
                         "(default: the whole device)")
    ap.add_argument("--notes-to", default=None,
                    help="where pulled NOTES go, in words (a folder, a Notion "
                         "database, an Obsidian vault...)")
    ap.add_argument("--directives-to", default=None,
                    help="where pulled DIRECTIVES go, in words")
    ap.add_argument("--no-pair", action="store_true",
                    help="never hand the terminal to rmapi for pairing")
    ap.add_argument("--reset-creation", action="store_true",
                    help=f"overwrite {creation.CREATION_NAME} with the shipped "
                         f"default page template (it is never overwritten otherwise)")
    args = ap.parse_args(argv)
    yes = args.yes

    launcher = config.RM_MCP_DIR / "run_server.py"
    _say("rm-mcp init")
    _say("=" * 60)

    # 1. Python side.
    missing = []
    for mod in ("mcp", "rmscene", "pypdfium2", "PIL", "markdown"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        _say(f"[!] missing Python packages: {', '.join(missing)}")
        _say(f"    {sys.executable} -m pip install -e .")
    else:
        _say(f"[ok] Python {sys.version.split()[0]} at {sys.executable}, dependencies present")

    # 2. rmapi + pairing.
    rmapi, authed, folders = _rmapi_status(args.rmapi)
    if not rmapi:
        _say("[!] " + _RMAPI_BUILD_HINT.replace("\n", "\n    "))
    elif authed:
        _say(f"[ok] rmapi at {rmapi}, paired; {len(folders)} top-level folder(s) on the device")
    else:
        _say(f"[!] rmapi at {rmapi} but the cloud did not answer as paired")
        if not args.no_pair:
            _pair_interactively(rmapi, yes)
            rmapi, authed, folders = _rmapi_status(rmapi)
            _say("[ok] paired" if authed else "[!] still not paired; pair and re-run")

    # 3. Roots.
    _say("")
    if folders:
        _say("Top-level folders on the device (\"My files\"):")
        for name in folders:
            _say(f"    /{name}")
    projects_root = args.projects_root or _ask(
        "Folder your projects live under ('/' = the device root, a project is "
        "any top-level folder you name)", "/", yes)
    projects_root = "/" + projects_root.strip().strip("/")
    managed = args.managed_roots
    if managed is None:
        managed = _ask("Folders rm_move and rm_delete may touch, comma-separated "
                       "(blank = the whole device)", "", yes)
    managed = ",".join(p.strip() for p in managed.split(",") if p.strip()) if managed else ""

    # 4. .env
    updates = {"RMAPI_BIN": rmapi or args.rmapi,
               "RM_MCP_PROJECTS_ROOT": projects_root,
               "RM_ROOT": projects_root}
    if managed:
        updates["RM_MCP_MANAGED_ROOTS"] = managed
    written = merge_env(env_path(), updates)
    _say("")
    _say(f"[ok] wrote {', '.join(written)} to {env_path()}")
    if not managed:
        _say("     guard scope: WHOLE DEVICE. rm_delete never recurses and defaults to a "
             "dry run, but set RM_MCP_MANAGED_ROOTS to fence it in.")

    # 5. Routing.
    notes_to = args.notes_to
    directives_to = args.directives_to
    if notes_to is None and not yes:
        notes_to = _ask("Where should pulled NOTES go? (a folder, a Notion database, "
                        "an Obsidian vault -- in words)", "", yes)
    if directives_to is None and not yes:
        directives_to = _ask("Where should pulled DIRECTIVES go?", "", yes)
    if notes_to or directives_to:
        routing_path().write_text(
            routing_text(notes_to or "(not set)", directives_to or "(not set)"),
            encoding="utf-8")
        _say(f"[ok] wrote {routing_path()}")
    elif routing_path().is_file():
        _say(f"[ok] {routing_path()} kept")
    else:
        _say(f"[ ] no routing yet. Re-run with --notes-to/--directives-to, or write "
             f"{_ROUTING_NAME} by hand; the agent reads it after every pull.")

    # 6. Creation -- the outbound counterpart of routing.
    try:
        created_at, written_now = creation.write_creation(
            overwrite=args.reset_creation)
    except OSError as exc:
        _say(f"[!] could not write {creation.CREATION_NAME}: {exc}")
    else:
        if written_now:
            _say(f"[ok] wrote {created_at}")
            _say("     the page rm_create lays documents out on. Edit the css or "
                 "json block in it to change how they look.")
        else:
            _say(f"[ok] {created_at} kept (yours to edit; --reset-creation restores "
                 f"the shipped default)")

    # 7. Registration.
    cli, block = registration(sys.executable, launcher)
    _say("")
    _say("Register the server with Claude Code:")
    _say(f"    {cli}")
    _say("or add to mcpServers in ~/.claude.json:")
    _say("    " + block.replace("\n", "\n    "))
    _say("")
    _say("Then, in a new session: call rm_health, make something with rm_create, "
         "write on it, pull it back with rm_pull_project.")
    return 0
