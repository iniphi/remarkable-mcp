"""Which tools a given deployment exposes.

One codebase, three surfaces. Before this existed, the "6-tool public ship"
to-do and the 24-tool repo contradicted each other for weeks -- because they
were answering two different questions and neither was written down as code:

    full    (25)  the desk. Everything, including the Zotero/Notion workflow
                  lane and the AGPL-linked render tools. The default.
    core    (18)  the public repo's device-infrastructure surface: push, pull,
                  diff, move, delete, create, inspect. No Zotero, no Notion, no
                  vision spend, and nothing that reaches an AGPL script.
    remote   (7)  the hosted Flavour-2 wire surface (SHIP-FLAVOUR-2.md): what a
                  remote caller over streamable-http gets. Chosen on blast
                  radius, and on the fact that tools returning container-local
                  paths are a functional dead end for a remote caller.

    (Counts as of 2026-09-20, when rm_create joined core and remote. The core
    figure read 15 from before the 2026-08-27 render port added two tools and
    was never corrected -- these numbers are prose, and test_surface.py is what
    actually holds the line.)

The two axes are orthogonal, which is the thing the old to-do collapsed: `core`
is a REPO-SCOPE cut (what code we publish), `remote` is a WIRE cut (what a
network-exposed deployment answers). remote is a subset of core is a subset of
full, so a public deployment can serve either without a second codebase.

Set RM_MCP_SURFACE=core|remote|full. Unset means full, so the desk is
unaffected. An unrecognised value is a hard startup error rather than a silent
fallback -- a typo must not quietly publish the whole surface.

RENDER TOOLS ARE IN `core` (blocked 2026-08-21; UNBLOCKED 2026-08-27)
---------------------------------------------------------------------
rm_render and rm_page_image both shell out to tools/rm_render_page.py.
rm_pull_project reaches it too, plus rm_flatten.py and rm_extract_highlights.py,
but only through its flatten/highlights/interpret flags -- so it ships in `core`
with those defaulted off, and asking for them in a core build returns runner.py's
actionable rc=127 rather than failing oddly.

DONE (2026-08-27). Step one was commit ed345ac (2026-08-26), which stopped
rm_render_page.py importing fitz at module scope: page geometry moved to
PdfSource (pypdfium2) from an open fitz.Document, and that handle -- needed only
for the page count and page size -- was the actual licence blocker.

That was not sufficient, for a reason easy to miss: tests/test_agpl_boundary.py
computes imports via AST at ANY scope, deliberately, so the surviving LAZY
`import fitz` inside render_pdf_page_pymupdf still counted. The lazy import
bought nothing.

Step two (2026-08-27) finished it. render_pdf_page_pymupdf moved out into
tools/rm_eval_pdfium.py, which is deliberately not shipped and is the only
caller that ever needed a backend-vs-backend diff. rm_render_page.py is now
free of fitz at every scope and sits in SHIPPED_TOOLS and in
rm_build_public.MANIFEST, which are kept in lockstep. RM_PDF_BACKEND now accepts
only 'pdfium' or unset; the legacy 'pymupdf' value raises rather than silently
rendering under a backend the caller did not ask for.

The eval verdict ruling GO on the swap (min SSIM 0.943, native fixtures
byte-identical) is at rm-mcp/tests/fixtures/render/PYPDFIUM2_EVAL.md. This
docstring previously cited ForClaude/PYPDFIUM2_EVAL.md, which does not exist.

`remote` is therefore the full seven tools of its documented surface (six
when SHIP-FLAVOUR-2.md was written, plus rm_create from 2026-09-20), and
BLOCKED_ON_RENDER_PORT is empty. It is kept as an empty frozenset rather than
deleted so that test_surface.py's guard against silently re-adding a blocked
tool still has something to assert against.
"""

from __future__ import annotations

import os

# Device infrastructure, provably free of Zotero, Notion, vision spend and any
# AGPL-linked script. Verified by tests/test_agpl_boundary.py against the
# shipped script manifest.
CORE_TOOLS = frozenset({
    "rm_health",
    "rm_list",
    "rm_ensure_project_folder",
    "rm_push_pdf",
    "rm_push_file",
    "rm_push_image",
    "rm_push_content",
    "rm_create",
    "rm_new_notebook",
    "rm_push_dir",
    "rm_pull_project",
    "rm_get_highlights",
    "rm_move",
    "rm_delete",
    "rm_diff",
    "rm_page_ink",
    "rm_render",
    "rm_page_image",
})

# SHIP-FLAVOUR-2.md's documented remote surface -- complete as of 2026-08-27,
# when the render port landed and rm_page_image became publishable. Keep this a
# strict subset of CORE_TOOLS.
REMOTE_TOOLS = frozenset({
    "rm_health",
    "rm_list",
    "rm_push_file",
    "rm_push_content",
    "rm_create",
    "rm_page_image",
    "rm_page_ink",
})

# Tools that reach tools/rm_render_page.py and could not be published while that
# file imported fitz. EMPTIED 2026-08-27 when the AGPL rasterizer moved out to
# tools/rm_eval_pdfium.py. Kept as an empty set rather than deleted: it is the
# named place for this class of block, and test_surface.py asserts against it.
BLOCKED_ON_RENDER_PORT: frozenset[str] = frozenset()

SURFACES: dict[str, frozenset[str] | None] = {
    "full": None,          # None means "no filter"
    "core": CORE_TOOLS,
    "remote": REMOTE_TOOLS,
}

# Presence of this script means the full workflow lane was installed. The
# public build ships only the permissive manifest, so it is absent there.
_FULL_BUILD_MARKER = "rm_pull.py"


def default_surface() -> str:
    """The surface to use when RM_MCP_SURFACE is unset.

    A build defines its own default. The public tree ships only the permissive
    script manifest, so its workflow tools could only ever return runner.py's
    rc=127 -- defaulting it to `full` would advertise 24 tools of which 11 are
    dead on arrival. Detect the trimmed build and default to `core` instead,
    while the desk (which has every script) still defaults to `full`.

    An explicit RM_MCP_SURFACE always wins over this.
    """
    # resolve_script, not a flat stat: the marker is PRESENT on the desk but
    # sits in tools/rm/ since the S112 split, so a flat check answered "absent"
    # and the desk server quietly dropped from 24 tools to 17 -- a real
    # capability loss reported as a normal startup.
    from .config import resolve_script
    return "full" if resolve_script(_FULL_BUILD_MARKER).is_file() else "core"


def active_surface() -> str:
    """The configured surface name. Raises on an unrecognised value."""
    name = os.environ.get("RM_MCP_SURFACE", "").strip().lower() or default_surface()
    if name not in SURFACES:
        raise SystemExit(
            f"RM_MCP_SURFACE={name!r} is not a known surface "
            f"(expected one of: {', '.join(sorted(SURFACES))}). "
            "Refusing to start rather than fall back to the full surface.")
    return name


def allowed_tools() -> frozenset[str] | None:
    """Tool names this deployment exposes, or None for all of them."""
    return SURFACES[active_surface()]


class SurfaceGate:
    """Registration-time filter wrapping a FastMCP instance.

    Wraps rather than edits the 24 decorator sites: `.tool()` returns either
    the real decorator or a no-op that leaves the function unregistered, and
    everything else delegates, so main()'s mcp.run / streamable_http_app are
    untouched.
    """

    def __init__(self, mcp, allowed: frozenset[str] | None) -> None:
        self._mcp = mcp
        self._allowed = allowed
        self.registered: set[str] = set()
        self.skipped: set[str] = set()

    def tool(self, *args, **kwargs):
        real = self._mcp.tool(*args, **kwargs)

        def deco(fn):
            name = getattr(fn, "__name__", "")
            if self._allowed is not None and name not in self._allowed:
                self.skipped.add(name)
                return fn          # defined, never registered
            self.registered.add(name)
            return real(fn)

        return deco

    def __getattr__(self, item):
        return getattr(self._mcp, item)


def verify(gate: SurfaceGate) -> None:
    """Fail startup if the registered set is not exactly the declared surface.

    A name misspelled in CORE_TOOLS would otherwise silently drop a tool from
    the public build (or, worse, silently keep one). Checked at startup because
    the cost of being wrong is a mis-scoped published server.
    """
    allowed = gate._allowed
    if allowed is None:
        return
    missing = sorted(allowed - gate.registered)
    if missing:
        raise SystemExit(
            f"surface {active_surface()!r} declares tool(s) that do not exist: "
            f"{', '.join(missing)}. Fix the name in surface.py.")
    extra = sorted(gate.registered - allowed)
    if extra:
        raise SystemExit(
            f"surface {active_surface()!r} registered undeclared tool(s): "
            f"{', '.join(extra)}.")
