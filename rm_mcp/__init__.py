"""rm-mcp -- FastMCP server exposing the 104_stacks reMarkable substrate.

Overseer-owned wrapper (see BUILD-CONTEXT.md). Stacks owns tools/rm_*.py and
rm_config.py; this package imports rm_config only and subprocesses the CLIs.
"""

__version__ = "1.0.0"
