#!/usr/bin/env python3
"""Launcher for the rm MCP server.

Registered in ~/.claude.json (mcpServers.rm) as:
    <python>  <path-to-clone>/run_server.py

Adds rm-mcp/ to sys.path so the rm_mcp package resolves without any pip
install or cwd assumption, hard-checks the imports (a missing dep must fail
loudly on stderr, never silently no-op the registration), then runs the
stdio server. `--check` performs the import smoke test and exits; `--init`
walks a fresh clone to a working install (see rm_mcp/init_cli.py).

IMPORTANT: nothing here may print to stdout -- under stdio transport stdout
is the MCP protocol channel. All diagnostics go to stderr.
"""

from __future__ import annotations

import sys
from pathlib import Path

RM_MCP_DIR = Path(__file__).resolve().parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))


def _fail(message: str) -> None:
    print(f"rm-mcp FATAL: {message}", file=sys.stderr)
    sys.exit(1)


def _load_server():
    try:
        import mcp.server.fastmcp  # noqa: F401  (the SDK FastMCP, not standalone fastmcp)
    except ImportError as exc:
        _fail(f"mcp SDK not importable under {sys.executable}: {exc}. "
              f"Install with: {sys.executable} -m pip install 'mcp[cli]>=1.6.0'")
    try:
        from rm_mcp import server
    except Exception as exc:
        _fail(f"rm_mcp package failed to import: {exc}")
    return server


def _tool_count(server) -> int:
    import asyncio
    return len(asyncio.run(server.mcp.list_tools()))


USAGE = """\
rm-mcp -- a reMarkable 2 as a working surface for an AI agent.

usage:
  run_server.py                 run the MCP server on stdio (how a client
                                launches it; not useful by hand)
  run_server.py --check         import smoke test, print the tool count, exit
  run_server.py --init [opts]   walk this clone to a working install
  run_server.py --init --help   the full list of --init options
  run_server.py --help          this message

Diagnostics go to stderr: under stdio transport stdout is the protocol channel.
"""


def _usage(stream, code: int) -> None:
    print(USAGE, file=stream, end="")
    sys.exit(code)


def main() -> None:
    argv = sys.argv[1:]

    # Anything not recognised used to fall THROUGH to server.main(), which
    # starts the stdio server -- so `--help` sat waiting on a stdin nobody was
    # writing to, and at EOF exited 0 having printed nothing. A newcomer typing
    # the most obvious command got silence and a success code, indistinguishable
    # from a broken install; a typo like `--chek` did the same. Found 2026-09-22
    # installing the published repo into a clean venv per its own README.
    # No args is still the server: that is how an MCP client launches it.
    if argv[:1] in (["--help"], ["-h"]):
        _usage(sys.stderr, 0)
    if argv and argv[0] not in ("--init", "--check"):
        print(f"rm-mcp: unknown option {argv[0]!r}\n", file=sys.stderr)
        _usage(sys.stderr, 2)

    if sys.argv[1:2] == ["--init"]:
        # The setup walk: writes .env and ROUTING.md, prints the registration.
        # Runs before the server import so a half-installed clone still gets
        # a diagnosis rather than an import error.
        try:
            from rm_mcp import init_cli
        except Exception as exc:
            _fail(f"rm_mcp package failed to import: {exc}. "
                  f"Install first: {sys.executable} -m pip install -e .")
        sys.exit(init_cli.run(sys.argv[2:]))
    server = _load_server()
    if "--check" in sys.argv[1:]:
        count = _tool_count(server)
        print(f"rm-mcp OK: {count} tools", file=sys.stderr)
        sys.exit(0)
    server.main()


if __name__ == "__main__":
    main()
