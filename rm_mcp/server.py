"""rm -- FastMCP server for the reMarkable-2 integration.

24 tools: thin device ops + the cross-project push/pull lane (Zotero-free,
incl. one-step content render+push via rm_push_content) + device management
(move/delete/bulk push, in manage.py) + page-level tools (pages.py) +
subprocess wraps of the round-trip CLIs (Zotero reading pipeline, drains,
extract/interpret).

The device layout is configuration, not a constant: the project lane, the
managed roots and the project-folder pattern all come from config.py, which
reads them from the environment. Never hardcode a device path here -- see
config.PROJECTS_DEVICE_ROOT / config.MANAGED_ROOTS, both reported by rm_health.

House pattern: paper-search-mcp. Import path MUST be mcp.server.fastmcp --
the standalone `fastmcp` package (also installed) has an incompatible API.

Invariants: Notion never inside the MCP (tools return manifests; the calling
agent routes); calibration constants frozen; auth surfaced, never re-paired
in-process; every push is an explicit tool call. Direct /Projects pushes are
recorded in state['projects_pushed'] via rm_config (v2: safe now that the
substrate has atomic state IO + the cross-process tools/.rm_state.lock).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import (__version__, authz, config, device, manage, pages, roundtrip,
               runner, surface, wire)
from .envelope import (
    REMEDIES,
    RMAPI_NOT_FOUND_REMEDY,
    RmapiAuthError,
    err_from_exception,
    err_result,
    make_warning,
    ok_result,
)
from .locks import STATE_LOCK

# Private lanes: modules the public build drops by name
# (tools/rm_build_public.PRIVATE_LANE), so their tools exist only on the desk.
# The public tree has no such files, the import fails by NAME and nothing is
# registered -- which is the point: an unregistered tool still ships its
# source, an absent file does not. A missing dependency INSIDE a present lane
# is not swallowed: that is a broken desk, not a trimmed build.
_PRIVATE_LANES: list = []
for _lane_name in ("stacks_lane", "zotero_lane"):
    try:
        _PRIVATE_LANES.append(
            importlib.import_module(f".{_lane_name}", __package__))
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.{_lane_name}":
            raise
zotero_lane = next((m for m in _PRIVATE_LANES
                    if m.__name__.endswith(".zotero_lane")), None)

# Transport selection: local Claude Code always uses stdio (the default,
# unaffected by any of this). RM_MCP_TRANSPORT=streamable-http switches to a
# network-reachable server for a remote deployment (e.g. a claude.ai custom
# connector) -- host/port only matter in that mode. PORT is read for
# compatibility with platforms (Railway, Fly, Heroku-style) that inject it.
_TRANSPORT = os.environ.get("RM_MCP_TRANSPORT", "stdio")
_HOST = os.environ.get("RM_MCP_HOST", "127.0.0.1")
_PORT = int(os.environ.get("PORT", os.environ.get("RM_MCP_PORT", "8000")))

_server = FastMCP("rm", host=_HOST, port=_PORT)

# Report OUR version in the initialize handshake, not the SDK's.
#
# FastMCP takes no `version` argument (checked against mcp 1.30.0), and when
# the inner server's version is None the SDK substitutes its own -- so a client
# connecting to this server was told "rm v1.30.0", which is the mcp package's
# version, tells a user nothing about rm-mcp, and silently changes whenever the
# SDK is upgraded. The handshake is how a client identifies what it is talking
# to, so it should say 1.0.0.
#
# `_mcp_server` is private, and this is the only route in this SDK version;
# create_initialization_options() reads the attribute set here. If FastMCP ever
# accepts a version directly, pass it there and delete this.
_server._mcp_server.version = __version__

# Registration passes through the surface gate, so RM_MCP_SURFACE decides which
# of the registered tools exist on this deployment. Unset = full = the desk, unchanged.
# Everything except .tool() delegates, so main()'s run/streamable_http_app are
# untouched. See surface.py for what each surface is and why they differ.
mcp = surface.SurfaceGate(_server, surface.allowed_tools())


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok"})


# The network lane's door is no longer a single shared secret. Scoped tokens,
# an IP allowlist and two rate-limit buckets live in authz.py (the policy) and
# wire.py (the ASGI chain that applies it); main() wraps the app in
# wire.harden(). This is deliberately NOT the SDK's OAuth
# token_verifier/auth_server_provider machinery: claude.ai custom connectors
# support a simpler fixed-header auth mode (a stored API key sent on every
# request), and checking that directly avoids standing up an authorization
# server for a single-owner deployment.

_PUSH_ALLOWLISTS = {
    "rm_push_pdf": (".pdf",),
    "rm_push_file": (".pdf", ".epub"),
}


def _push_source(tool: str, path: str | None, content_b64: str | None,
                 filename: str | None):
    """Resolve a push tool's source to a local file: a server path, or inline bytes.

    Exactly one of the two. Refusing "both" matters as much as refusing
    "neither": silently preferring one would make a caller that sent both think
    it pushed the other.
    """
    allow = _PUSH_ALLOWLISTS[tool]
    if path and content_b64:
        return None, err_result(
            "config", "pass path= or content_b64=, not both",
            "path= reads a file on the server; content_b64= sends the bytes "
            "over the wire. A remote caller wants content_b64=")
    if content_b64:
        if not filename:
            return None, err_result(
                "config", "content_b64= requires filename=",
                f"filename supplies the extension the allowlist gates on "
                f"({', '.join(allow)})")
        return roundtrip.materialised_local(content_b64, filename, allow)
    if not path:
        return None, err_result(
            "config", "no source given: pass path= or content_b64=",
            "path= for a file on the server, content_b64= + filename= to send "
            "the bytes from a remote caller")
    return roundtrip.validated_local(path, allow)


def _resolve_or_error(project: str | None) -> tuple[str | None, dict | None]:
    try:
        return config.project_device_dir(project), None
    except ValueError as exc:
        return None, err_result("config", str(exc), "pass project=<NNN_name>")


def _creation_status() -> dict:
    """What rm_create would use right now, for rm_health's config block.

    Reports a broken CREATION.md rather than raising: health is the tool you
    call when something is wrong, so it must be able to describe the file it
    cannot parse. rm_create itself still refuses that file -- health says what
    is true, it does not launder it.
    """
    from . import creation

    path = creation.creation_path()
    status: dict[str, Any] = {
        "file": str(path),
        "exists": path.is_file(),
    }
    try:
        tpl, provenance = creation.load_template()
        status.update({
            "ok": True,
            "template": tpl["name"],
            "source": provenance,
            "page_pt": [tpl["page_w_pt"], tpl["page_h_pt"]],
            "text_frame_pt": [
                tpl["page_w_pt"] - tpl["margin_left"] - tpl["margin_right"],
                tpl["page_h_pt"] - tpl["margin_top"] - tpl["margin_bot"],
            ],
        })
    except creation.CreationError as exc:
        status.update({"ok": False, "error": str(exc)})
    status["templates"] = creation.available_templates()[0]
    return status


# -- thin device tools --------------------------------------------------------

@mcp.tool()
async def rm_health(write_probe: bool = False) -> dict:
    """Preflight for every other rm tool: reachability + config, read-only.

    Reports (never errors): rmapi binary presence, cloud auth state, the
    projects root's existence, vision key presence, the frozen config block,
    and -- on a desk with the Zotero lane installed -- its credentials. Call this first when
    any rm tool fails, and before diagnosing device problems.

    Set `write_probe=True` to additionally attempt ONE real write (creating
    and removing a throwaway folder). Every default check is a read, so a
    clean report does NOT mean the device is writable -- this tool reported
    "cloud authenticated, ok" throughout the total write outage of
    2026-08-17/18. Use the probe before trusting a push, and when a write
    fails while reads succeed.

    ONE condition returns ok=false: the rmapi binary cannot be resolved, which
    means there is no device lane at all and every other rm tool will fail.
    The data block is still fully populated -- this reports, it does not
    refuse. Everything else (unpaired cloud, missing root, absent vision key)
    is a state to READ off the report, not a failure of the report.
    """
    def probe() -> dict:
        # Resolution, not a filesystem test. `Path("rmapi").is_file()` asks
        # whether a file called "rmapi" sits in the CURRENT DIRECTORY, which
        # is false on essentially every machine -- including ones where rmapi
        # is on PATH and every other tool works. Until 2026-09-20 that test
        # short-circuited the auth probe, so this tool reported the device
        # unreachable on healthy installs and, on broken ones, reported
        # "authenticated: null" as though it had asked the cloud when it had
        # never run rmapi at all (crosstalk 7981b341, 305_krisis).
        resolved = config.RMAPI_RESOLVED
        auth = device.auth_probe() if resolved else {
            "authenticated": None, "detail": RMAPI_NOT_FOUND_REMEDY}

        projects_root_exists = None
        if auth.get("authenticated"):
            try:
                device.ls(config.PROJECTS_DEVICE_ROOT, timeout=30)
                projects_root_exists = True
            except (RmapiAuthError, config.rm_config.RmapiThrottledError,
                    config.rm_config.RmapiBusyError):
                projects_root_exists = None  # not asked, so not "missing"
            except RuntimeError:
                projects_root_exists = False

        # Opt-in, because it mutates the device. Only meaningful once the
        # cloud is actually reachable -- an unauthenticated rmapi fails every
        # write for a reason that has nothing to do with the outage class.
        write: dict | None = None
        if write_probe:
            if not auth.get("authenticated"):
                write = {"writable": None,
                         "detail": "skipped: cloud not authenticated"}
            else:
                try:
                    write = device.write_probe(
                        config.PROJECTS_DEVICE_ROOT,
                        cleanup=config.destructive_allowed())
                except RmapiAuthError as exc:
                    write = {"writable": False, "detail": str(exc)}
                except config.rm_config.RmapiThrottledError as exc:
                    # Unknown, not False: the write was never attempted.
                    write = {"writable": None, "detail": str(exc),
                             "throttled_until": exc.until_iso}

        return {
            "rmapi_binary": {
                # `path` kept for readers that already parse it; it is the
                # resolved path when there is one, so an existing caller sees
                # a strictly better value rather than a changed shape.
                "path": config.RMAPI_BIN,
                "configured": config.RMAPI_BIN_CONFIGURED,
                "resolved": resolved,
                "exists": bool(resolved),
            },
            "cloud": auth,
            # The rmapi cooldown, read from its file: never a cloud call, so
            # it answers even while the cloud is refusing us.
            "throttle": config.rm_config.throttle_status(),
            # Only where the Zotero lane is installed (the desk). The public
            # build has no Zotero coupling, so it has nothing to report here.
            **({"zotero": zotero_lane.health_status()} if zotero_lane else {}),
            "projects_root_exists": projects_root_exists,
            "write_probe": write,
            "vision_keys": {
                "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "gemini": bool(os.environ.get("GEMINI_API_REMARKABLE")
                               or os.environ.get("GEMINI_API_KEY")),
            },
            "config": {
                "rm_root": config.RM_ROOT,
                "projects_root": config.PROJECTS_DEVICE_ROOT,
                # What rm_move / rm_delete / rm_push_dir will accept without
                # allow_anywhere, and what the read tools are confined to when
                # read_scope_enforced() -- so a deployment can verify its own
                # scoping in one call instead of inferring it.
                "managed_roots": list(config.MANAGED_ROOTS),
                # "whole device" when the roots collapse to "/" -- the public
                # default since 2026-09-10 -- so an install can see at a
                # glance that rm_move / rm_delete are not fenced in.
                "guard_scope": config.guard_scope(),
                "read_scope_enforced": config.read_scope_enforced(),
                # Which scopes have a credential and whether the abuse
                # controls are armed -- never a token, a length or a prefix.
                # An operator should be able to confirm the door of a
                # deployed server without shelling into it.
                "wire": authz.status(),
                "session_dir": str(config.SESSION_DIR),
                "output_retention": config.output_retention_status(),
                "pdf_rm_scale_frozen": config.PDF_RM_SCALE_FROZEN,
                # The geometry that scale is frozen AGAINST. Surfacing one
                # without the other is what sent a caller guessing at A5.
                "authoring_page": config.RM_AUTHORING_PAGE,
                # Which page rm_create will actually lay a document on, and
                # where that came from. The question this answers is "did my
                # CREATION.md edit take effect", which is otherwise only
                # answerable by pushing something and looking at it.
                "creation": _creation_status(),
                "stale_revision_remedy": REMEDIES["stale_revision"],
            },
        }

    data = await asyncio.to_thread(probe)

    # An envelope that reports its own transport missing must not be green.
    # Ruled 2026-09-20 answering crosstalk 7981b341: "a report whose own body
    # says the tool is absent should not be green." The data block is returned
    # in full either way -- the caller loses nothing by the honesty.
    if not data["rmapi_binary"]["resolved"]:
        return err_result(
            "rmapi",
            f"rmapi could not be resolved as "
            f"{config.RMAPI_BIN_CONFIGURED!r} -- no device lane is available",
            REMEDIES["rmapi_not_found"],
            data=data,
            warnings=[make_warning(
                "rmapi_not_found",
                f"rmapi not found (configured: "
                f"{config.RMAPI_BIN_CONFIGURED!r}); every other rm tool will "
                f"fail until this is fixed",
                possible_data_loss=False)],
        )
    return ok_result(data)


@mcp.tool()
async def rm_list(device_path: str = "/", project: str | None = None) -> dict:
    """List a reMarkable device folder.

    Args:
        device_path: Folder to list ("/", "/SomeFolder/Reading", ...). On the
            network-exposed lane this is confined to the managed roots, which
            rm_health reports.
        project: If given, lists the project's folder under the configured
            project root instead of device_path.
    Returns:
        RmResult with data.entries = [{name, type: folder|doc}].
    """
    path = device_path
    if project is not None:
        resolved, error = _resolve_or_error(project)
        if error:
            return error
        path = resolved
    guard = manage.guard_read_path(path)
    if guard is not None:
        return guard
    try:
        entries = await asyncio.to_thread(device.ls, path)
    except RmapiAuthError as exc:
        return err_result("rmapi", str(exc), REMEDIES["not_authenticated"])
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return err_from_exception(exc, "cloud",
                                 "check the path exists (rm_list on the parent) "
                                 "and the cloud is reachable (rm_health)")
    return ok_result({"device_path": path, "entries": entries})


@mcp.tool()
async def rm_ensure_project_folder(project: str | None = None) -> dict:
    """Create /Projects/<code>/ on the device (idempotent).

    Case-canonicalized against the device (V2-8): if a folder with different
    casing already exists (200_poetics vs 200_Poetics), the existing folder
    is reused and a project_case_matched warning is attached.

    Args:
        project: Project code (NNN_name). Pass explicitly -- the fallback
            (CLAUDE_PROJECT_DIR, cwd walk) is host-dependent for a global server.
    Returns:
        RmResult with data.device_dir.
    """
    def ensure() -> dict:
        try:
            device_dir, warnings = roundtrip.canonical_project_dir(project)
            device.mkdir_p(device_dir)
        except ValueError as exc:
            return err_result("config", str(exc), "pass project=<NNN_name>")
        except RmapiAuthError as exc:
            return err_result("rmapi", str(exc), REMEDIES["not_authenticated"])
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            return err_from_exception(exc, "cloud",
                                     "retry after rm_health is green")
        return ok_result({"device_dir": device_dir}, warnings=warnings)

    return await asyncio.to_thread(ensure)


@mcp.tool()
async def rm_push_pdf(path: str | None = None, project: str | None = None,
                      title: str | None = None,
                      content_b64: str | None = None,
                      filename: str | None = None) -> dict:
    """Push a PDF to the project's device folder /Projects/<code>/.

    The cross-project lane: any agent can stage a PDF for pen annotation and
    later pull it back with rm_pull_project. The push is recorded in
    state['projects_pushed'] (V2-11; never in the Zotero lane's 'pushed').

    Pass `path` (a file on the SERVER) or `content_b64` + `filename` (the bytes
    over the wire), never both. A remote caller wants the second: `path`
    resolves on the service's own filesystem, which a sandbox cannot write to.

    IF YOU ARE AUTHORING THE PDF, author it at 468 x 624 pt -- exactly the
    device's 1404 x 1872 px (226 dpi) divided by 3, so the page fills the
    screen 1:1. A4 (595 x 842 pt) is a taller ratio and letterboxes; A5
    (420 x 595 pt) is close enough to look deliberate and is still wrong.
    rm_health reports this as config.authoring_page, with the three cases it
    does NOT govern: PDFs arriving from outside (a paper's size is its own),
    the calibration sheets, and rm_make_dossier.py. This tool does not resize
    anything -- the geometry is a contract on the caller, which is why it is
    stated here rather than left to be inferred from pdf_rm_scale_frozen.

    Args:
        path: Absolute path to a .pdf file ON THE SERVER.
        project: Project code (NNN_name, e.g. "101_Overseer") -- pass explicitly.
        title: Optional device filename (sanitised, max 80 chars).
        content_b64: The PDF's bytes as standard base64, for a remote caller.
            Capped at 11MB decoded.
        filename: Required with content_b64, e.g. "figure.pdf".
    Returns:
        RmResult with data.device_path.
    """
    local, error = _push_source("rm_push_pdf", path, content_b64, filename)
    if error:
        return error
    return await asyncio.to_thread(roundtrip.push_local_file, local, project, title)


@mcp.tool()
async def rm_push_file(path: str | None = None, project: str | None = None,
                       title: str | None = None,
                       content_b64: str | None = None,
                       filename: str | None = None) -> dict:
    """Push a .pdf or .epub to /Projects/<code>/ on the device.

    TWO WAYS IN, and which one you can use depends on where you are. `path`
    resolves on the SERVER's filesystem, so it works from the local stdio
    registration and is meaningless to a remote caller. `content_b64` +
    `filename` send the bytes over the wire instead, which is the only push a
    sandbox can actually perform -- the same move rm_push_content already makes
    for generated markup. Pass exactly one.

    .epub is a documented one-way limitation (V2-5): the push works and the
    device renders it, but rm_pull_project's flatten/highlights lane is
    PDF-backed -- an .epub pull returns the raw bundle only. Every .epub
    push carries an epub_untested_roundtrip warning.

    Authoring a PDF rather than forwarding one? Use 468 x 624 pt -- see
    rm_push_pdf, and rm_health's config.authoring_page.

    Args:
        path: Absolute path to a .pdf or .epub file ON THE SERVER.
        project: Project code (NNN_name) -- pass explicitly.
        title: Optional device filename (sanitised, max 80 chars).
        content_b64: The file's bytes as standard base64, for a remote caller.
            Capped at 11MB decoded; larger needs the path= lane.
        filename: Required with content_b64 -- supplies the extension the
            allowlist gates on (e.g. "Latour 1986.pdf").
    Returns:
        RmResult with data.device_path.
    """
    local, error = _push_source(
        "rm_push_file", path, content_b64, filename)
    if error:
        return error
    return await asyncio.to_thread(roundtrip.push_local_file, local, project, title)


@mcp.tool()
async def rm_push_image(path: str, project: str | None = None,
                        title: str | None = None) -> dict:
    """Convert a local image to a one-page PDF and push it to /Projects/<code>/.

    For staging diagrams, screenshots, or generated figures for pen
    annotation. Conversion runs pymupdf in a child process.

    Args:
        path: Absolute path to a .png/.jpg/.jpeg/.webp file.
        project: Project code (NNN_name) -- pass explicitly.
        title: Optional device filename (sanitised, max 80 chars).
    Returns:
        RmResult with data.device_path (the pushed PDF).
    """
    local, error = roundtrip.validated_local(path, roundtrip.IMAGE_SUFFIXES)
    if error:
        return error

    def convert_and_push() -> dict:
        stem = config.safe_filename_stem(title or local.stem)
        pdf_path = config.new_out_dir("push_staging") / f"{stem}.pdf"
        try:
            roundtrip.convert_image_to_pdf(local, pdf_path)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            return err_result("config", str(exc),
                              "confirm pymupdf is importable under the server "
                              "interpreter and the image is readable")
        result = roundtrip.push_local_file(pdf_path, project, None)
        if result["ok"]:
            result["data"]["converted_from"] = str(local)
        return result

    return await asyncio.to_thread(convert_and_push)


@mcp.tool()
async def rm_push_content(mode: str, content: str, project: str | None = None,
                          title: str | None = None) -> dict:
    """Render Claude-generated content and push it to /Projects/<code>/ in one
    step -- the render+push wrapper over rm_render_content.py, so no manual
    rmapi calls and no separate render-then-push dance.

    Modes: "markdown" | "html" | "svg" -> a paginated PDF at the device-native
    468 x 624 pt (rm_health's config.authoring_page; NOT A4, which this
    docstring claimed until 2026-09-20 -- the renderer itself moved off A4 on
    2026-08-18 because it letterboxed on the 1404 x 1872 screen); "native" ->
    a Type-Folio-editable .rmdoc (requires title). SVG is the vector escape
    hatch: pre-render mermaid/D2/RDKit/matplotlib output to SVG, then push it
    here. For an existing local file use rm_push_pdf/rm_push_file instead --
    this tool is for content generated in-conversation.

    Args:
        mode: "markdown" | "html" | "svg" | "native".
        content: the source markup/text (generated in this conversation).
        project: Project code (the project's folder name) -- pass explicitly.
        title: device filename (sanitised, max 80 chars); also the H1 fallback
            for markdown; REQUIRED for native mode.
    Returns:
        RmResult with data.device_path, data.mode, data.rendered_from.
    """
    return await asyncio.to_thread(
        roundtrip.render_and_push_content, mode, content, project, title)


@mcp.tool()
async def rm_create(content: str = "", project: str | None = None,
                    title: str | None = None, template: str | None = None,
                    mode: str = "markdown", dry_run: bool = False) -> dict:
    """Create a document that FITS a reMarkable, and push it -- start here.

    Use this, not rm_push_content, whenever you are making something for the
    device rather than moving a file that already exists. The difference is
    the template: this reads the page out of CREATION.md, so you do not have
    to know the geometry and the user can change how documents look by
    editing that one file.

    You do not need to reason about page size. The template is already the
    device-native 468 x 624 pt -- exactly the RM2's 1404 x 1872 px divided by
    three, so it fills the screen. Do NOT generate A4: it is 1:1.415 against a
    3:4 panel and letterboxes. Do not set CSS page sizes, @page rules or
    absolute widths in your markup either; the template owns layout, your job
    is the content.

    Call with dry_run=True first if you want to see the house style -- it
    returns the resolved template (page, margins, text frame, CSS) and
    renders nothing. A template named in `template` that is not in
    CREATION.md is an error, never a silent fall back to the default.

    Args:
        content: markdown (default), HTML or finished SVG markup, generated in
            this conversation. Required unless dry_run.
        project: Project folder (the folder's name) -- pass explicitly.
        title: device filename (sanitised, max 80 chars); also the H1 fallback
            for markdown.
        template: section name from CREATION.md. Omit for "default".
        mode: "markdown" | "html" | "svg". For a Type-Folio-editable notebook
            use rm_new_notebook instead -- a template cannot apply to one.
        dry_run: report the resolved template and push nothing.
    Returns:
        RmResult with data.device_path, data.template, data.template_source,
        data.page_pt, data.margins_pt, data.text_frame_pt.
    """
    return await asyncio.to_thread(
        roundtrip.create_and_push, content, project, title, template, mode, dry_run)


@mcp.tool()
async def rm_new_notebook(title: str, project: str | None = None,
                          template: str = "default",
                          body: str | None = None) -> dict:
    """Create a fresh handwriting notebook on the device, ready to write in.

    The notebook opens with the template's page margin and pen already set
    (default: small margin, fineliner, medium, black) and the title typed as
    a heading on page 1 with the Type Folio keyboard, so the first thing to
    do on the device is start writing. `body` (markdown-ish: "# ", "- ",
    "**bold**") seeds more typed text under the heading; leave it out for a
    blank page. Pull the notebook back with rm_pull_project: typed text comes
    back as text, handwriting as page images.

    Templates: "default", "lined" (P Lines small paper) and "plain" ship;
    tools/rm_templates.local.json adds
    or overrides by name. Pen and margin values are what the device itself
    writes, but whether a fresh upload honours them before the first stroke
    is unverified -- worst case, one tap on the toolbar.

    Args:
        title: Notebook name on the device and the heading on page 1.
        project: Project folder (the folder's name) -- pass explicitly.
        template: "default" | "lined" | "plain" | a name from rm_templates.local.json.
        body: Optional typed text under the heading.
    Returns:
        RmResult with data.device_path, data.template, data.template_fields,
        data.local_rmdoc.
    """
    return await asyncio.to_thread(
        roundtrip.new_notebook, title, project, template, body)


# -- project round-trip -------------------------------------------------------

@mcp.tool()
async def rm_pull_project(name: str, project: str | None = None,
                          flatten: bool = True, highlights: bool = True,
                          interpret: bool = True, backend: str | None = None,
                          pages: str | None = None, profile: str = "analysis",
                          dry_run: bool = False) -> dict:
    """Pull an annotated document back from /Projects/<code>/ -- no Zotero.

    Fetches the .rmdoc bundle and produces local artifacts: the flat annotated
    PDF (text layer preserved), highlight records, and optionally per-page
    PNGs + vision interpretations. The calling agent routes the artifacts
    (Notion, session context, re-push); this tool only returns paths.
    Interpretation is metered vision spend (~$0.01-0.03/page) -- use
    dry_run=True first on unfamiliar documents. Stateless: safe to call
    repeatedly; works on zero-annotation documents.

    Args:
        name: Device document name inside the project folder (as shown by
            rm_list), without extension.
        project: Project code (NNN_name) -- pass explicitly.
        flatten: Produce the flat annotated PDF.
        highlights: Extract highlight records (glyph + stroke).
        interpret: Render pages + run the vision pass.
        backend: claude | gemini-pro | gemini-flash (default claude, which
            costs nothing because the calling agent reads the page itself).
        pages: Page selection like "1,3,5-7" (default: all).
        profile: Render profile for the interpret pass -- "analysis" (default,
            the model-input render) | "publication". render_profiles is the
            single source of truth, shared with rm_pull_notebook and rm_render.
        dry_run: Return the plan without touching the device.
    Returns:
        RmResult with data: annotated_pdf, highlights, pngs, interpretations,
        extracted_dir, bundle, out_dir.
    """
    return await asyncio.to_thread(
        roundtrip.pull_project_doc, name, project, flatten, highlights,
        interpret, backend, pages, dry_run, profile)


@mcp.tool()
async def rm_render(device_path: str | None = None,
                    extracted: str | None = None,
                    profile: str = "publication",
                    pages: str | None = None,
                    transparent: bool | None = None,
                    dry_run: bool = False) -> dict:
    """Render device pages to PNG under a named profile -- NO vision, NO spend.

    The human-facing render lane, distinct from the analysis render baked into
    rm_pull_notebook / rm_pull_project (which thins strokes for the vision model
    and always interprets). Two profiles (the config in render_profiles.py):

      - publication (default): full stroke width, supersample 3, cropped to the
        ink bounding box, no interpret. For output / artwork.
      - analysis: supersample 2, pressure-width, width 0.85 -- a faithful
        preview of the render the vision model sees, minus the interpret step.

    Source: pass device_path (a full device path to fetch, e.g. "/Draw/Drawing")
    OR extracted (an already-unzipped .rmdoc dir, e.g. the extracted_dir from
    rm_pull_project). Default renders annotated pages only; pass pages= to force
    a selection.

    Args:
        device_path: Full device path to fetch + render.
        extracted: Path to an unzipped .rmdoc dir (skips the fetch).
        profile: "publication" (default) | "analysis".
        pages: Page selection like "1,3,5-7" (default: annotated pages).
        transparent: Transparent background instead of the profile default
            (True/False overrides; None keeps the profile's default white).
            Native notebooks only -- a PDF-backed page stays opaque. The PNG is
            saved RGBA so strokes sit on transparency for compositing.
        dry_run: Return the plan (profile + flags) without touching the device.
    Returns:
        RmResult with data.pngs (rendered PNGs) + data.cropped + out_dir.
    """
    if device_path is not None:
        guard = manage.guard_read_path(device_path)
        if guard is not None:
            return guard
    return await asyncio.to_thread(
        roundtrip.render_doc, device_path, extracted, profile, pages,
        dry_run, transparent)


# -- heavy wraps: device activity ----------------------------------------------

@mcp.tool()
async def rm_diff(root: str | None = None, update_baseline: bool = True,
                  include_all: bool = False) -> dict:
    """Whole-device activity diff vs the rolling snapshot ("what did I work on?").

    Args:
        root: Device subtree to diff (default "/", ignoring /Draw and /trash).
        update_baseline: Update the snapshot after diffing (False = peek only).
        include_all: Include unchanged entries in the manifest.
    Returns:
        RmResult with data.manifest (changed/new/removed entries).
    """
    out = config.new_out_dir("rm_diff")
    manifest_path = out / "diff.json"
    args = ["--out", str(manifest_path)]
    if root:
        args += ["--root", root]
    if not update_baseline:
        args.append("--no-update")
    if include_all:
        args.append("--all")

    async with STATE_LOCK:
        return await asyncio.to_thread(
            runner.run_wrapped, "rm_diff.py", args, "rm_diff",
            lambda proc: {"manifest": runner.load_manifest(manifest_path),
                          "out_dir": str(out)})


# -- extract (operate on local artifacts) --------------------------------------

@mcp.tool()
async def rm_get_highlights(extracted_dir: str) -> dict:
    """Extract highlight records from an unzipped .rmdoc directory.

    Both data models: source=glyph (snap-to-text, literal) and source=stroke
    (freehand, geometry intersection against the PDF text layer).

    Args:
        extracted_dir: Path to an unzipped .rmdoc directory (e.g. the
            extracted_dir returned by rm_pull_project).
    Returns:
        RmResult with data.highlights = [{page, bbox, text, color, source}].
    """
    def builder(proc: subprocess.CompletedProcess) -> dict:
        return {"highlights": json.loads(proc.stdout)}

    return await asyncio.to_thread(
        runner.run_wrapped, "rm_extract_highlights.py",
        [extracted_dir, "--json"], "rm_get_highlights", builder)


# Device management tools (rm_move / rm_delete / rm_push_dir) live in
# manage.py to keep this file under the size ceiling.
manage.register(mcp)
pages.register(mcp)

# Fail loudly now if the declared surface and the registered set disagree -- a
# misspelling in surface.py would otherwise silently drop (or publish) a tool.
surface.verify(mcp)


def main() -> None:
    # Session-output retention: prune old per-process output dirs in the
    # background (never the current session; opt out RM_MCP_KEEP_SESSIONS=all).
    threading.Thread(target=config.prune_sessions, daemon=True).start()

    if _TRANSPORT == "streamable-http":
        # Everything that can be wrong about the wire config -- no token, one
        # secret in two scopes, a malformed allowlist entry -- is fatal here,
        # before a single request is served. A public endpoint with the wrong
        # door on it is not a thing to discover from traffic.
        authz.validate_startup()
        header_name = os.environ.get("RM_MCP_AUTH_HEADER", "x-api-key")
        app = wire.harden(mcp.streamable_http_app(), header_name=header_name)
        import uvicorn
        uvicorn.run(app, host=_HOST, port=_PORT)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()


# Private lanes (see _PRIVATE_LANES at the top): absent from the public build
# by design, so on the public tree this loop registers nothing.
for _lane in _PRIVATE_LANES:
    _lane.register(mcp)

# Last, because the private lanes register after main() is defined and their
# tools need a scope too. Same argument as surface.verify: a tool with no row
# in authz.TOOL_SCOPES is gated at admin and quietly stops working on the
# network lane, and that must fail at deploy time rather than in a bug report.
authz.verify_tools(mcp.registered)
