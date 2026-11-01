"""Registering rm-mcp with the Claude desktop app, and leaving a trace when it dies.

The desktop app launches an MCP server from `claude_desktop_config.json` and
shows only "Connection closed" when the server exits at start-up; stderr is
not displayed. Two failures follow from that and both happened (2026-10-11):
the entry named an interpreter without mcp 2.x, and nothing said so. This
module holds the pieces that make that visible: the config path per OS, a
merge-write of the entry that never clobbers other servers, a read-only
check that the registered interpreter is the running one, and a fatal log
the launcher appends to.

Stdlib only, and nothing here prints: under stdio transport stdout is the
protocol channel.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

_CONFIG_NAME = "claude_desktop_config.json"
_FATAL_NAME = "rm-mcp-fatal.log"


def desktop_config_path(platform: str = sys.platform,
                        env: Mapping[str, str] = os.environ,
                        home: Path | None = None) -> Path:
    """Where the Claude desktop app keeps its MCP config on this OS.

    Windows: %APPDATA%\\Claude. macOS: ~/Library/Application Support/Claude.
    Linux: ~/.config/Claude, a guess only; Anthropic ships no official Linux
    desktop build, so this path is the conventional one, not a documented one.
    """
    home = Path.home() if home is None else home
    if platform.startswith("win"):
        appdata = env.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "Claude" / _CONFIG_NAME
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / _CONFIG_NAME
    return home / ".config" / "Claude" / _CONFIG_NAME


def fatal_log_path(platform: str = sys.platform,
                   env: Mapping[str, str] = os.environ,
                   home: Path | None = None) -> Path:
    """The file run_server.py appends its FATAL messages to."""
    home = Path.home() if home is None else home
    if platform.startswith("win"):
        local = env.get("LOCALAPPDATA")
        base = Path(local) if local else home / "AppData" / "Local"
        return base / "rm-mcp" / _FATAL_NAME
    if platform == "darwin":
        return home / "Library" / "Logs" / "rm-mcp" / _FATAL_NAME
    xdg = env.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else home / ".local" / "state"
    return base / "rm-mcp" / _FATAL_NAME


def desktop_entry(python: str, launcher: Path) -> dict[str, Any]:
    """The mcpServers.rm entry in the desktop format (no "type" key)."""
    return {"command": python, "args": [str(launcher)]}


def _result(state: str, path: Path, backup: Path | None = None,
            previous_command: str | None = None,
            reason: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"state": state, "path": str(path),
                           "backup": str(backup) if backup else None,
                           "previous_command": previous_command}
    if reason is not None:
        out["reason"] = reason
    return out


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_desktop_entry(path: Path, python: str, launcher: Path) -> dict[str, Any]:
    """Merge the rm entry into the desktop config; never clobber anything else.

    States: "created" (file or entry was absent), "kept" (the rm entry already
    has this command and args; nothing written), "updated" (its command and
    args were replaced after a timestamped backup; any other key on the entry,
    such as env, is kept), "refused" (unparseable JSON or a non-object
    mcpServers; nothing written, `reason` says why).
    """
    path = Path(path)
    entry = desktop_entry(python, launcher)
    data: dict[str, Any] = {}
    raw: str | None = None
    if path.is_file():
        try:
            # utf-8-sig: a config saved by an editor that adds a BOM is still
            # the user's config, not an unparseable one.
            raw = path.read_text(encoding="utf-8-sig")
            parsed = json.loads(raw)
        except (OSError, ValueError) as exc:
            return _result("refused", path, reason=f"cannot read config: {exc}")
        if not isinstance(parsed, dict):
            return _result("refused", path,
                           reason="top level of the config is not a JSON object")
        data = parsed
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        return _result("refused", path, reason="mcpServers is not a JSON object")

    existing = servers.get("rm")
    # Only command and args are ours. Anything else on the rm entry (an env
    # block, say) is the user's and survives both "kept" and "updated".
    if isinstance(existing, dict) and all(existing.get(k) == v for k, v in entry.items()):
        return _result("kept", path)

    backup: Path | None = None
    previous: str | None = None
    state = "created"
    if existing is not None:
        state = "updated"
        if isinstance(existing, dict):
            cmd = existing.get("command")
            previous = cmd if isinstance(cmd, str) else None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        try:
            backup.write_text(raw or "", encoding="utf-8")
        except OSError as exc:
            return _result("refused", path, reason=f"cannot write backup: {exc}")

    new_rm = {**existing, **entry} if isinstance(existing, dict) else entry
    new_data = {**data, "mcpServers": {**servers, "rm": new_rm}}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_dump(new_data), encoding="utf-8")
    except OSError as exc:
        return _result("refused", path, backup, previous, f"cannot write config: {exc}")
    return _result(state, path, backup, previous)


def _same_interpreter(a: str, b: str) -> bool:
    def norm(p: str) -> str:
        return os.path.normcase(os.path.realpath(p))
    return norm(a) == norm(b)


def registration_check(python: str = sys.executable,
                       path: Path | None = None) -> dict[str, Any]:
    """Does the desktop config's rm entry launch `python`? Never raises.

    state: absent (no file), not_registered (no rm entry), match, mismatch,
    unreadable (bad JSON, wrong shape, or no command string).
    """
    try:
        cfg = Path(path) if path is not None else desktop_config_path()
        out: dict[str, Any] = {"path": str(cfg), "state": "absent",
                               "command": None, "expected": python}
        if not cfg.is_file():
            return out
        try:
            data = json.loads(cfg.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            out["state"] = "unreadable"
            return out
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            out["state"] = "unreadable"
            return out
        entry = servers.get("rm")
        if entry is None:
            out["state"] = "not_registered"
            return out
        command = entry.get("command") if isinstance(entry, dict) else None
        if not isinstance(command, str) or not command:
            out["state"] = "unreadable"
            return out
        out["command"] = command
        out["state"] = "match" if _same_interpreter(command, python) else "mismatch"
        return out
    except Exception as exc:  # a health check must not be the thing that fails
        return {"path": str(path) if path else None, "state": "unreadable",
                "command": None, "expected": python, "detail": str(exc)}


NOT_A_VENV_WARNING = (
    "[!] this interpreter is not in a virtual environment. Registering a "
    "system Python can be stranded by the next system Python change (the "
    "desktop app then shows only 'Connection closed'). Create a venv, install "
    "into it, and register from there.")

RESTART_HINT = ("Then fully quit Claude desktop (on Windows from the system tray, "
                "not just by closing the window) and reopen it, then call rm_health.")


def describe(result: dict[str, Any]) -> str:
    """One line for a write_desktop_entry result, shared by --init and the CLI."""
    state, path = result["state"], result["path"]
    if state == "refused":
        return f"[!] desktop config not written: {result.get('reason')}"
    if state == "kept":
        return f"[ok] desktop config already has this entry: {path} kept"
    if state == "updated":
        return (f"[ok] desktop config updated: {path} (was "
                f"{result['previous_command']}; backup {result['backup']})")
    return f"[ok] desktop config written: {path}"


def register_cli(argv: list[str], python: str, launcher: Path,
                 out: Any = None) -> int:
    """`run_server.py --register-desktop [--desktop-config PATH]`.

    Writes the desktop entry for `python` and touches nothing else: no .env,
    no routing or creation note. It is the repair after an upgrade moves the
    server to another interpreter, which must not re-run the init walk (its
    --yes defaults would rewrite tools/.env). Exit 0 written or kept, 1
    refused, 2 bad arguments.
    """
    import argparse
    stream = sys.stderr if out is None else out
    ap = argparse.ArgumentParser(
        prog="run_server.py --register-desktop",
        description="Write (merge) the rm entry into the Claude desktop config, "
                    "for the interpreter running this command. Nothing else is touched.")
    ap.add_argument("--desktop-config", default=None, metavar="PATH",
                    help="config file (default: the standard path for this OS)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:  # argparse exits 2 on a bad option, 0 on --help
        return int(exc.code or 0)
    cfg = Path(args.desktop_config) if args.desktop_config else desktop_config_path()
    if sys.prefix == sys.base_prefix:
        print(NOT_A_VENV_WARNING, file=stream)
    result = write_desktop_entry(cfg, python, launcher)
    print(describe(result), file=stream)
    if result["state"] == "refused":
        return 1
    print(RESTART_HINT, file=stream)
    return 0


def append_fatal(message: str, path: Path | None = None) -> Path | None:
    """Append one line to the fatal log. Returns its path, or None on OSError."""
    try:
        target = Path(path) if path is not None else fatal_log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        line = f"{stamp} {sys.executable} {message}".replace("\n", " ")
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return target
    except OSError:
        return None
