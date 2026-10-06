"""Subprocess runner for the wrapped substrate CLIs.

Every heavy tool shells out to `C:\\Python313\\python.exe tools/rm_X.py ...`
(same interpreter as the server, which carries all heavy deps) instead of
importing the script. This is forced, not stylistic: rm_push.py and rm_pull.py
call sys.exit(2) at module level when Zotero env keys are missing, and every
script is print-heavy while the server's own stdout is the MCP stdio channel.

State-writer note (v2): the CLIs are no longer the ONLY writers of
.rm_state.json -- the server records its direct /Projects pushes via
rm_config.record_project_push. Safe since rm_config gained atomic writes and
the cross-process tools/.rm_state.lock.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import config
from .config import TOOLS_DIR
from .envelope import (
    REMEDIES,
    classify,
    err_result,
    log_tail,
    ok_result,
    synthesize_warnings_from_tail,
)

TIMEOUTS = {
    "rm_diff": 900,
    "rm_push_reading": 900,
    "rm_pull": 1800,
    "rm_pull_notebook": 1800,
    "rm_pull_project": 1800,
    "rm_capture_todos": 900,
    "rm_get_highlights": 300,
    "rm_interpret_page": 1800,
    "rm_flatten": 300,
    "rm_write_note1": 300,
}

_PUSH_TOTALS_RE = re.compile(
    r"=== Total: (\d+) pushed, (\d+) renamed, (\d+) unchanged, "
    r"(\d+) failed, (\d+) without a PDF ===")
_PULL_DONE_RE = re.compile(r"Done: (\d+) pulled, (\d+) failed")


def run_script(script: str, args: list[str], tool: str,
               timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run one substrate CLI. Blocking -- call via asyncio.to_thread.

    stdin is closed so nothing (unauthenticated rmapi, stray input()) can
    hang the call; MSYS_NO_PATHCONV/PYTHONUTF8 are inherited from the server
    environment (set in config.py).

    A build that does not ship `script` gets an actionable rc=127 result rather
    than the interpreter's bare "can't open file". Builds differ on purpose:
    the public surface ships only the permissive script manifest, so the
    Zotero/Notion/AGPL-linked CLIs are genuinely absent there. Returning a
    CompletedProcess (rather than raising) keeps every existing call site --
    which already branches on returncode and surfaces stderr -- unchanged.
    """
    # Bucket-aware since the tools/ split (2026-09-20): callers still name a bare
    # filename ("rm_render_page.py") and config resolves it in whichever layout
    # this tree is -- flat in the public build, split here.
    script_path = config.resolve_script(script)
    if not script_path.is_file():
        return subprocess.CompletedProcess(
            args=[sys.executable, str(script_path), *args],
            returncode=127, stdout="",
            stderr=(f"{script} is not available in this build -- the tool "
                    f"{tool!r} depends on it. This build ships the permissive "
                    f"script manifest only; {script} is excluded (Zotero/Notion "
                    f"coupling, or an AGPL PyMuPDF dependency). Use the full "
                    f"build if you need it."),
        )
    return subprocess.run(
        [sys.executable, str(script_path), *args],
        capture_output=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, cwd=str(TOOLS_DIR),
        timeout=timeout or TIMEOUTS.get(tool, 600),
    )


def run_wrapped(script: str, args: list[str], tool: str,
                data_builder=None,
                summary_path: Path | None = None) -> dict[str, Any]:
    """Run a CLI and map the outcome into an RmResult.

    data_builder(proc) -> dict runs on rc 0 (and on rc 1 to salvage partial
    results); exceptions from it are surfaced as config errors.

    summary_path: a --json-summary file the CLI was asked to write. When
    present, its structured counts/warnings take precedence over the legacy
    stdout-regex parsers and log-tail synthesis.
    """
    try:
        proc = run_script(script, args, tool)
    except subprocess.TimeoutExpired as exc:
        return err_result("rmapi", f"{tool} timed out after {exc.timeout}s",
                          REMEDIES["timeout"])

    tail = log_tail(proc)
    error = classify(proc, tool)
    data: dict[str, Any] = {}
    if data_builder is not None:
        try:
            data = data_builder(proc) or {}
        except Exception as exc:  # data salvage must never mask the real error
            if error is None:
                return err_result("config",
                                  f"{tool} output could not be parsed: {exc}",
                                  "inspect log_tail", tail=tail)

    warnings: list[dict[str, Any]] = []
    summary = None
    if summary_path is not None:
        try:
            summary = load_manifest(summary_path)
        except (OSError, json.JSONDecodeError):
            summary = None
    if isinstance(summary, dict):
        if summary.get("counts") is not None:
            data["counts"] = summary["counts"]
        if summary.get("items"):
            data["items"] = summary["items"]
        warnings = list(summary.get("warnings") or [])
    manifest = data.get("manifest")
    if isinstance(manifest, dict) and manifest.get("warnings"):
        seen_codes = {w.get("code") for w in warnings}
        warnings += [w for w in manifest["warnings"]
                     if w.get("code") not in seen_codes]
    if not warnings:
        warnings = synthesize_warnings_from_tail(tail)

    if error is not None:
        return {"ok": False, "data": data, "warnings": warnings,
                "error": error, "log_tail": tail}
    return ok_result(data, tail, warnings)


# -- output parsers -----------------------------------------------------------

def load_manifest(path: Path) -> dict[str, Any] | list | None:
    """Load a --out JSON manifest if the script produced one."""
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def parse_push_totals(stdout: str) -> dict[str, int] | None:
    m = _PUSH_TOTALS_RE.search(stdout or "")
    if not m:
        return None
    keys = ("pushed", "renamed", "unchanged", "failed", "without_pdf")
    return dict(zip(keys, (int(g) for g in m.groups())))


def parse_pull_done(stdout: str) -> dict[str, int] | None:
    m = _PULL_DONE_RE.search(stdout or "")
    if not m:
        return None
    return {"pulled": int(m.group(1)), "failed": int(m.group(2))}


def glob_artifacts(root: Path) -> list[str]:
    """Absolute paths of every file under root (for --keep-downloads etc.)."""
    if not root.is_dir():
        return []
    return sorted(str(p) for p in root.rglob("*") if p.is_file())
