"""Thin rmapi transport for rm-mcp's net-new tools.

Used by the tools that have no substrate CLI to wrap (rm_health, rm_list,
rm_ensure_project_folder, rm_push_pdf/file/image, and the get step of
rm_pull_project). Delegates to the shared rm_config.run_rmapi wrapper (the
same invocation path every substrate CLI uses since v2: utf-8 decode, stdin
closed, MSYS_NO_PATHCONV set in the child env), keeping the server's
auth-raise semantics on top.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from .config import RMAPI_BIN, rm_config
from .envelope import (RmapiAuthError, RmapiNotFoundError,
                       RmapiThrottledError, looks_unauthenticated,
                       make_warning)


def _run(args: list[str], timeout: int = 60,
         cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run one rmapi command. Raises RmapiAuthError on a lost pairing.

    check=False: callers branch on returncode; only a lost pairing raises
    (the server surfaces it as the not_authenticated remedy, never re-auths).
    """
    proc = rm_config.run_rmapi(*args, check=False, cwd=cwd,
                               timeout=float(timeout))
    if proc.returncode != 0 and looks_unauthenticated(
            f"{proc.stdout}\n{proc.stderr}"):
        raise RmapiAuthError(proc.stderr.strip() or proc.stdout.strip()
                             or "rmapi not authenticated")
    return proc


def ls(device_path: str, timeout: int = 60) -> list[dict[str, str]]:
    """List a device folder. Entries: {"name", "type": "folder"|"doc"}.

    Raises RuntimeError when the path does not exist / cannot be listed.
    """
    proc = _run(["ls", device_path], timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"rmapi ls {device_path} failed: "
                           f"{proc.stderr.strip() or proc.stdout.strip()}")
    entries = []
    for line in proc.stdout.splitlines():
        line = line.rstrip()
        if not line or "\t" not in line:
            continue
        kind, name = line.split("\t", 1)
        entries.append({"name": name.strip(),
                        "type": "folder" if kind.strip("[]") == "d" else "doc"})
    return entries


def canonical_child(parent: str, name: str,
                    timeout: int = 60,
                    want_type: str | None = None) -> tuple[str, list[dict]]:
    """Device-casing canonicalization for one child segment (V2-8).

    Lists `parent` and returns (canonical_name, warnings). An exact match
    wins; a unique case-insensitive match returns the DEVICE's casing plus a
    project_case_matched warning (prevents 110_notes / 110_Notes
    duplicate folders). No match, or an unlistable parent, returns the name
    unchanged -- creating a genuinely new folder stays legal. Auth errors
    propagate; other transport failures fall through.

    When `want_type` ("folder" or "doc") is given, matching is TYPE-AWARE:
    a same-type case-insensitive match still reuses the device casing, but a
    DIFFERENT-type match of the same name is a genuine collision -- a folder
    and a notebook whose names differ only by case resolve to the same path
    under rmapi and become mutually unfetchable (and rmapi cannot rename
    either, since the path is ambiguous). That case returns the REQUESTED name
    plus a project_notebook_collision warning naming the convention remedy
    (folders use '_' as the code/slug separator, notebooks use '-'). Passing
    want_type=None keeps the original type-agnostic behavior for existing
    callers.
    """
    try:
        entries = ls(parent, timeout=timeout)
    except (RmapiAuthError, RmapiThrottledError):
        raise
    except (RuntimeError, subprocess.TimeoutExpired):
        return name, []
    if any(e["name"] == name for e in entries):
        return name, []
    ci = [e for e in entries if e["name"].lower() == name.lower()]
    if not ci:
        return name, []

    def _case_matched(device_name: str) -> tuple[str, list[dict]]:
        return device_name, [make_warning(
            "project_case_matched",
            f"{parent}/{name}: an entry with different casing already exists "
            f"on the device; using {device_name!r} to avoid a duplicate",
            data={"requested": name, "device": device_name})]

    if want_type is None:
        # Legacy type-agnostic path (unchanged behavior).
        if len(ci) == 1:
            return _case_matched(ci[0]["name"])
        return name, []

    other = next((e for e in ci if e["type"] != want_type), None)
    if other is not None:
        want_sep = "'_' (NNN_slug)" if want_type == "folder" else "'-' (NNN-slug)"
        other_sep = "'-' (NNN-slug)" if other["type"] == "doc" else "'_' (NNN_slug)"
        return name, [make_warning(
            "project_notebook_collision",
            f"{parent}/{name}: a {other['type']} named {other['name']!r} already "
            f"exists; a folder and a notebook whose names differ only by case "
            f"collide under rmapi's case-insensitive path resolution and both "
            f"become unfetchable. Convention: folders use {want_sep}, notebooks "
            f"use {other_sep}. Rename the {other['type']} to that form on the "
            f"device (tablet or desktop app -- rmapi cannot rename a collided "
            f"node).",
            data={"requested": name, "collides_with": other["name"],
                  "collides_type": other["type"]})]
    same_type = [e for e in ci if e["type"] == want_type]
    if len(same_type) == 1:
        return _case_matched(same_type[0]["name"])
    return name, []


def mkdir_p(device_path: str, timeout: int = 30) -> None:
    """Ensure each segment of device_path exists. Idempotent."""
    parts = [p for p in device_path.strip("/").split("/") if p]
    cur = ""
    for part in parts:
        cur = f"{cur}/{part}"
        proc = _run(["mkdir", cur], timeout=timeout)
        if proc.returncode != 0:
            stderr = proc.stderr.lower()
            if "exists" in stderr or "already" in stderr:
                continue
            raise RuntimeError(f"rmapi mkdir {cur} failed: {proc.stderr.strip()}")


def write_probe(parent: str, *, cleanup: bool, timeout: int = 30) -> dict:
    """Attempt one real write against the cloud and report what happened.

    Every other check in rm_health is a read, which is why it reported
    "cloud authenticated, ok" throughout the TOTAL write outage of
    2026-08-17/18: the cloud had begun rejecting any root index whose entries
    were not sorted by document ID, so reads kept working while every write
    returned a bare 400. Only an actual write detects that class of outage.

    Creates a uniquely-named folder (the cheapest write that still rewrites
    the root index) and removes it again when `cleanup` is set. When cleanup
    is refused -- the hosted lane disables destructive ops -- the leftover
    path is reported rather than silently left behind.
    """
    name = f".rm_health_probe_{uuid.uuid4().hex[:8]}"
    path = f"{parent.rstrip('/')}/{name}"
    proc = _run(["mkdir", path], timeout=timeout)
    detail = (proc.stderr.strip() or proc.stdout.strip())[:400]

    if proc.returncode != 0:
        result = {"writable": False, "probe_path": path, "detail": detail}
        # The outage signature: a bare 400 with no further explanation. Both
        # lanes now build rmapi from a pinned source commit for exactly this.
        if "400" in detail:
            result["likely_cause"] = (
                "bare 400 on a write means rmapi is behind upstream -- the cloud "
                "rejects a root index that is not sorted by document ID. Rebuild "
                "rmapi from source at a commit containing ddvk/rmapi f295d54.")
        return result

    result: dict = {"writable": True, "probe_path": path, "detail": detail or None}
    if not cleanup:
        result["cleaned_up"] = False
        result["note"] = ("destructive ops are disabled on this transport, so the "
                          "probe folder was left in place -- remove it by hand or "
                          "set RM_MCP_ALLOW_DESTRUCTIVE=1")
        return result
    try:
        rm(path, timeout=timeout)
        result["cleaned_up"] = True
    except (RuntimeError, RmapiAuthError) as exc:
        result["cleaned_up"] = False
        result["note"] = f"probe folder left behind: {exc}"
    return result


def put(local: Path, device_dir: str, timeout: int = 120) -> None:
    """Push a local file into a device folder.

    Runs from the file's parent so rmapi sees the basename only (device
    filename = local stem).
    """
    proc = _run(["put", local.name, device_dir],
                timeout=timeout, cwd=str(local.parent))
    if proc.returncode != 0:
        raise RuntimeError(f"rmapi put {local.name} -> {device_dir} failed: "
                           f"{proc.stderr.strip()}")


def get(device_path: str, dest_dir: Path, timeout: int = 300) -> Path:
    """Download a document bundle (.rmdoc zip) into dest_dir.

    rmapi get writes `<name>.rmdoc` into its cwd; returns that path.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in dest_dir.iterdir()}
    proc = _run(["get", device_path], timeout=timeout, cwd=str(dest_dir))
    if proc.returncode != 0:
        raise RuntimeError(f"rmapi get {device_path} failed: "
                           f"{proc.stderr.strip() or proc.stdout.strip()}")
    new_files = [p for p in dest_dir.iterdir() if p.name not in before]
    bundles = [p for p in new_files
               if p.suffix.lower() in (".rmdoc", ".zip")] or new_files
    if not bundles:
        raise RuntimeError(f"rmapi get {device_path} produced no file in "
                           f"{dest_dir}")
    return bundles[0]


def mv(src: str, dest: str, timeout: int = 60) -> None:
    """Move/rename a device entry (V2-3 transport)."""
    proc = _run(["mv", src, dest], timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"rmapi mv {src} -> {dest} failed: "
                           f"{proc.stderr.strip() or proc.stdout.strip()}")


def rm(device_path: str, timeout: int = 60) -> None:
    """Delete a device entry (V2-3 transport). No recursion -- the tool
    layer refuses non-empty folders before calling this."""
    proc = _run(["rm", device_path], timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"rmapi rm {device_path} failed: "
                           f"{proc.stderr.strip() or proc.stdout.strip()}")


def auth_probe(timeout: int = 30) -> dict[str, object]:
    """Non-raising reachability probe for rm_health.

    Returns {"authenticated": bool | None, "detail": str}; authenticated is
    None when rmapi could not be executed at all, or when the cloud is
    throttling -- then "throttled_until" says when to ask again, and during an
    active cooldown the probe never reaches the cloud.
    """
    try:
        proc = rm_config.run_rmapi("ls", "/", check=False,
                                   timeout=float(timeout))
    except RmapiThrottledError as exc:
        return {"authenticated": None, "detail": str(exc),
                "throttled_until": exc.until_iso}
    except RmapiNotFoundError as exc:
        # run_rmapi types the missing binary since 2026-09-20; the bare
        # FileNotFoundError this used to catch no longer reaches here. The
        # detail now carries the remedy rather than restating the path the
        # caller can already see in rmapi_binary.
        return {"authenticated": None, "detail": str(exc)}
    except rm_config.RmapiBusyError as exc:
        return {"authenticated": None, "detail": str(exc)}
    except subprocess.TimeoutExpired:
        return {"authenticated": None, "detail": "rmapi ls / timed out"}
    if proc.returncode == 0:
        return {"authenticated": True, "detail": "ok"}
    combined = f"{proc.stdout}\n{proc.stderr}"
    if looks_unauthenticated(combined):
        return {"authenticated": False,
                "detail": (proc.stderr.strip() or proc.stdout.strip())[:300]}
    return {"authenticated": None,
            "detail": (proc.stderr.strip() or proc.stdout.strip())[:300]}
