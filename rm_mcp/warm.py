"""Warm the rmapi tree cache once, before the server binds its port.

Cloud Run scales to zero, and a fresh instance needs ~113 s for rmapi's first
full tree sync. Every request-path call has a shorter timeout (auth_probe 30 s,
ls 60 s, mkdir 30 s), and the timeout kills rmapi before it persists the tree,
so short calls never warm the instance. This step runs from docker-entrypoint.sh
inside the startup-CPU-boost window, with a timeout long enough to finish.

Run as `python -m rm_mcp.warm` from rm-mcp/. Sequence: tree_cache.restore()
(pull rmapi's tree.cache from the state bucket), ONE `rmapi ls /` through
rm_config.run_rmapi (lock, pacing, 429 hard stop all apply), then, only if that
exited 0, tree_cache.save(). One stderr line per step, and ALWAYS exits 0: a failed warm must never stop the server starting.

Env: RM_WARM_TIMEOUT_S (default 150; the startup probe allows 240),
RM_WARM_DISABLE=1 (skip, never call rmapi).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from . import tree_cache
from .config import rm_config

# 150 s leaves 90 s of Cloud Run's 240 s startup probe for restore + server start.
DEFAULT_TIMEOUT_S = 150.0


def _timeout_s() -> float:
    raw = os.environ.get("RM_WARM_TIMEOUT_S", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT_S
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def _log(message: str) -> None:
    print(f"rm-mcp warm: {message}", file=sys.stderr, flush=True)


def _step(name: str, fn) -> None:
    """Run one cache step, log one line with elapsed seconds, never raise."""
    started = time.monotonic()
    try:
        outcome = fn()
    except Exception as exc:  # noqa: BLE001 - a cache step must never block startup
        outcome = f"unexpected {type(exc).__name__}: {exc}"
    _log(f"{name}: {outcome} ({time.monotonic() - started:.1f}s)")


def _attempt(timeout: float) -> str:
    """One warm call; returns the outcome sentence. May raise."""
    proc = rm_config.run_rmapi("ls", "/", check=False, timeout=timeout)
    if proc.returncode == 0:
        return "ok, tree cache warm"
    detail = ((proc.stderr or "").strip() or (proc.stdout or "").strip())[:200]
    return f"rmapi ls / failed (exit {proc.returncode}): {detail}"


def main() -> int:
    if os.environ.get("RM_WARM_DISABLE", "").strip() == "1":
        _log("disabled by RM_WARM_DISABLE=1, rmapi not called")
        return 0
    timeout = _timeout_s()
    _step("restore",
          lambda: f"{tree_cache.restore()} [{tree_cache.cache_path()}]")
    started = time.monotonic()
    ok = False
    try:
        outcome = _attempt(timeout)
        ok = outcome.startswith("ok")
    except subprocess.TimeoutExpired:
        outcome = f"timed out after {timeout:.0f}s limit"
    except rm_config.RmapiThrottledError as exc:
        outcome = f"throttled (429), cooldown until {exc.until_iso}"
    except rm_config.RmapiBusyError as exc:
        outcome = f"rmapi lock busy: {exc}"
    except rm_config.RmapiNotFoundError as exc:
        outcome = f"rmapi binary or config not found: {exc}"
    except Exception as exc:  # noqa: BLE001 - a warm must never block startup
        outcome = f"unexpected {type(exc).__name__}: {exc}"
    _log(f"ls: {outcome} ({time.monotonic() - started:.1f}s)")
    if ok:
        _step("save", tree_cache.save)
    return 0


if __name__ == "__main__":
    sys.exit(main())
