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


def main() -> None:
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
