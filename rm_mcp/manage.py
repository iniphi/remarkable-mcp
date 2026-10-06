"""Device management tools: rm_move, rm_delete, rm_push_dir (V2-3, V2-4).

Registered onto the server's FastMCP instance via register(mcp) so server.py
stays under the file-size ceiling. The sync implementations are module-level
functions (testable offline with mocked device transports); the registered
tools are thin async wrappers.

Safety posture: rm_move and rm_delete default to dry_run=True (the plan is
the product; execution is the explicit second call), refuse paths outside
the managed roots (config.MANAGED_ROOTS) unless allow_anywhere=True, and
rm_delete never recurses -- a non-empty folder is refused with a remedy.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from . import config, device, roundtrip
from .envelope import (REMEDIES, RmapiAuthError, RmapiThrottledError,
                       err_from_exception, err_result, ok_result,
                       throttled_result)

# The roots rm-mcp manages. Everything else on the device (personal trees,
# unrelated folders) needs the explicit allow_anywhere=True escape. Defined in
# config so a deployment can repoint them with RM_MCP_PROJECTS_ROOT / RM_ROOT /
# RM_MCP_MANAGED_ROOTS; aliased here because this is where callers expect it.
MANAGED_ROOTS = config.MANAGED_ROOTS


def _destructive_guard(action: str) -> dict[str, Any] | None:
    """Refuse an execute-path destructive call on a network-exposed server."""
    if config.destructive_allowed():
        return None
    return err_result(
        "config",
        f"{action} is disabled on this deployment",
        "this server is running network-exposed (streamable-http), where the "
        "only gate is a shared secret -- destructive operations are off so a "
        "leaked token cannot delete or move device content. Run it from the "
        "local stdio registration, or redeploy with "
        "RM_MCP_ALLOW_DESTRUCTIVE=1 if remote execution is genuinely needed.")

_PUSH_DIR_SUFFIXES = (".pdf", ".epub")


def _guard_path(path: str, allow_anywhere: bool) -> dict[str, Any] | None:
    if allow_anywhere:
        return None
    norm = "/" + (path or "").strip("/")
    for root in MANAGED_ROOTS:
        # "/" is the whole device (config._managed_roots); a prefix test
        # against "//" would never match it.
        if root == "/" or norm == root or norm.startswith(root + "/"):
            return None
    return err_result(
        "config",
        f"{path!r} is outside the managed roots "
        f"({', '.join(MANAGED_ROOTS)})",
        "pass allow_anywhere=True to operate outside "
        f"{' and '.join(MANAGED_ROOTS)}")


def guard_read_path(path: str, allow_anywhere: bool = False) -> dict[str, Any] | None:
    """Confine a READ tool to the managed roots on a network-exposed server.

    A no-op on the local stdio lane, so whole-device browsing is unchanged for
    the desk. See config.read_scope_enforced for why the two lanes differ.
    """
    if not config.read_scope_enforced():
        return None
    return _guard_path(path, allow_anywhere)


def move_impl(src: str, dest: str, dry_run: bool,
              allow_anywhere: bool) -> dict[str, Any]:
    for path in (src, dest):
        guard = _guard_path(path, allow_anywhere)
        if guard is not None:
            return guard
    if not dry_run:
        denial = _destructive_guard("rm_move")
        if denial is not None:
            return denial
    plan = {"action": "move", "src": src, "dest": dest}
    if dry_run:
        return ok_result({"dry_run": True, "plan": plan})
    try:
        device.mv(src, dest)
    except RmapiAuthError as exc:
        return err_result("rmapi", str(exc), REMEDIES["not_authenticated"])
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return err_from_exception(exc, "cloud",
                                 "check src exists (rm_list on its parent) and "
                                 "the dest parent folder exists, then retry")
    return ok_result({"moved": True, **plan})


def delete_impl(device_path: str, dry_run: bool,
                allow_anywhere: bool) -> dict[str, Any]:
    guard = _guard_path(device_path, allow_anywhere)
    if guard is not None:
        return guard
    # Refuse before any device round-trip: a call that cannot execute should
    # not touch the device at all.
    if not dry_run:
        denial = _destructive_guard("rm_delete")
        if denial is not None:
            return denial
    # Classify via the PARENT listing: rmapi ls on a document path succeeds
    # and lists the doc itself (live-smoke finding, 2026-07-03), so listing
    # the target cannot distinguish doc from folder.
    norm = "/" + (device_path or "").strip("/")
    parent, _, name = norm.rpartition("/")
    try:
        siblings = device.ls(parent or "/", timeout=30)
    except RmapiAuthError as exc:
        return err_result("rmapi", str(exc), REMEDIES["not_authenticated"])
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return err_from_exception(exc, "cloud",
                                 "the parent folder could not be listed; check "
                                 "the path with rm_list, then retry")
    entry = next((e for e in siblings if e["name"] == name), None)
    if entry is None:
        return err_result("config", f"{device_path} does not exist",
                          "check the path with rm_list on its parent")
    if entry["type"] == "folder":
        try:
            children = device.ls(norm, timeout=30)
        except RmapiThrottledError as exc:
            return throttled_result(exc)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            # Never fall through to "empty": until 2026-10-01 this set
            # children = [] and planned -- and, for real, performed -- a
            # delete of a folder whose contents nobody had been able to read.
            return err_result(
                "cloud",
                f"{device_path}'s contents could not be listed, so it is not "
                f"known to be empty: {exc}",
                "rm_delete only removes a folder it has seen to be empty: "
                "check the cloud with rm_health, then retry")
        if children:
            return err_result(
                "config",
                f"{device_path} is a non-empty folder "
                f"({len(children)} entries)",
                "rm_delete never recurses: move or delete its children first "
                "(rm_list to see them), then delete the empty folder")
        kind = "empty_folder"
    else:
        kind = "doc"
    plan = {"action": "delete", "device_path": device_path, "kind": kind}
    if dry_run:
        return ok_result({"dry_run": True, "plan": plan})
    try:
        device.rm(device_path)
    except RmapiAuthError as exc:
        return err_result("rmapi", str(exc), REMEDIES["not_authenticated"])
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return err_from_exception(exc, "cloud",
                                 "check the path with rm_list on its parent, "
                                 "then retry")
    return ok_result({"deleted": True, **plan})


def push_dir_impl(dir_path: str, project: str | None, glob: str,
                  limit: int | None, dry_run: bool) -> dict[str, Any]:
    root = Path(dir_path)
    if not root.is_dir():
        return err_result("config", f"not a directory: {dir_path}",
                          "pass an absolute path to an existing directory")
    matched = sorted(p for p in root.glob(glob) if p.is_file())
    skipped = [str(p) for p in matched
               if p.suffix.lower() not in _PUSH_DIR_SUFFIXES]
    files = [p for p in matched if p.suffix.lower() in _PUSH_DIR_SUFFIXES]
    if limit is not None:
        files = files[:limit]
    if dry_run:
        return ok_result({
            "dry_run": True,
            "counts": {"planned": len(files), "skipped": len(skipped)},
            "items": [{"source": str(p), "status": "would_push"}
                      for p in files],
            "skipped": skipped})

    items: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen_warnings: set[tuple] = set()
    pushed = failed = 0
    for path in files:
        result = roundtrip.push_local_file(path, project, None)
        if result["ok"]:
            pushed += 1
            items.append({"source": str(path), "status": "pushed",
                          "device_path": result["data"].get("device_path")})
        else:
            failed += 1
            items.append({"source": str(path), "status": "failed",
                          "detail": result["error"]["message"]})
        for warning in result.get("warnings") or []:
            key = (warning.get("code"), warning.get("message"))
            if key not in seen_warnings:
                seen_warnings.add(key)
                warnings.append(warning)

    data = {"counts": {"pushed": pushed, "failed": failed,
                       "planned": len(files)},
            "items": items}
    if skipped:
        data["skipped"] = skipped
    if files and pushed == 0:
        first_error = next((i["detail"] for i in items
                            if i["status"] == "failed"), "all pushes failed")
        return err_result("cloud", f"all {failed} pushes failed: {first_error}",
                          "check rm_health, then retry; per-file details are "
                          "in data.items", data=data, warnings=warnings)
    return ok_result(data, warnings=warnings)


def register(mcp) -> None:
    """Attach the management tools to the server's FastMCP instance."""

    @mcp.tool()
    async def rm_move(src: str, dest: str, dry_run: bool = True,
                      allow_anywhere: bool = False) -> dict:
        """Move or rename a device document/folder (dry-run by DEFAULT).

        Replaces raw-rmapi cleanup: call once for the plan, then again with
        dry_run=False to execute. Refuses paths outside the managed roots
        (rm_health reports them) unless allow_anywhere=True.

        Args:
            src: Full device path of the document/folder to move.
            dest: Destination device path (rmapi mv semantics: an existing
                folder moves src into it; otherwise a rename).
            dry_run: True (default) returns the plan without touching the
                device; pass False to execute.
            allow_anywhere: Permit paths outside the managed roots.
        Returns:
            RmResult with data.plan (dry run) or data.moved.
        """
        return await asyncio.to_thread(move_impl, src, dest, dry_run,
                                       allow_anywhere)

    @mcp.tool()
    async def rm_delete(device_path: str, dry_run: bool = True,
                        allow_anywhere: bool = False) -> dict:
        """Delete a device document or EMPTY folder (dry-run by DEFAULT).

        Never recurses: a non-empty folder is refused with a remedy (delete
        or move its children first). Refuses paths outside the managed roots
        (rm_health reports them) unless allow_anywhere=True.

        Args:
            device_path: Full device path of the document/empty folder.
            dry_run: True (default) returns the plan (including whether the
                target is a doc or an empty folder); pass False to execute.
            allow_anywhere: Permit paths outside the managed roots.
        Returns:
            RmResult with data.plan (dry run) or data.deleted.
        """
        return await asyncio.to_thread(delete_impl, device_path, dry_run,
                                       allow_anywhere)

    @mcp.tool()
    async def rm_push_dir(dir_path: str, project: str | None = None,
                          glob: str = "*.pdf", limit: int | None = None,
                          dry_run: bool = False) -> dict:
        """Push every matching file in a local directory to /Projects/<code>/.

        Bulk form of rm_push_pdf/rm_push_file: same per-file pipeline
        (case-canonicalized project folder, state recorded per file in
        projects_pushed), per-file results + merged warnings. Only .pdf and
        .epub are pushed; other glob matches are listed in data.skipped.

        Args:
            dir_path: Absolute path to a local directory.
            project: Project code (NNN_name) -- pass explicitly.
            glob: Filename pattern within dir_path (default "*.pdf").
            limit: Push at most N files (after sorting by name).
            dry_run: Return the plan (counts.planned + would_push items).
        Returns:
            RmResult with data.counts {pushed, failed, planned} + data.items.
        """
        return await asyncio.to_thread(push_dir_impl, dir_path, project,
                                       glob, limit, dry_run)
