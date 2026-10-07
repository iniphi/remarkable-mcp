"""rm_new_notebook must not sit on the rmapi lock for the default 600s per call.

Reported 2026-10-06: the tool never returned for 1800s although the upload
landed. Every rmapi call in the push path waited up to 600s for the shared
lock, one call after another, which can exceed Claude Code's 1800s MCP idle
timeout. The push path now takes a short, separate lock bound
(RM_MCP_PUSH_LOCK_TIMEOUT_S) and returns an error envelope.

Another rm tool is simulated by a thread holding the rmapi lock. RMAPI_BIN is
pointed at a missing path, so nothing is ever spawned.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

BOUND_S = 8.0


def _run_new_notebook(monkeypatch, tmp_path: Path):
    from rm_mcp import config, roundtrip
    import rm_config

    monkeypatch.setenv("RM_MCP_PUSH_LOCK_TIMEOUT_S", "1")
    monkeypatch.delenv("RM_RMAPI_LOCK_TIMEOUT_S", raising=False)  # real default
    monkeypatch.setattr(rm_config, "RMAPI_BIN", str(tmp_path / "no_such_rmapi.exe"))
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(roundtrip, "new_out_dir", lambda *_a, **_k: out)
    monkeypatch.setattr(config, "resolve_project", lambda p: "my_project")

    release = threading.Event()
    held = threading.Event()

    def holder():
        with rm_config.rmapi_lock(timeout=5):
            held.set()
            release.wait(60)

    threading.Thread(target=holder, daemon=True).start()
    assert held.wait(5), "could not take the simulated long-running lock"

    box: dict = {}

    def call():
        try:
            box["result"] = roundtrip.new_notebook("Endpoint test", "my_project",
                                                   "default", None)
        except BaseException as exc:  # noqa: BLE001
            box["raised"] = exc

    started = time.monotonic()
    t_call = threading.Thread(target=call, daemon=True)
    t_call.start()
    t_call.join(BOUND_S)
    blocked = t_call.is_alive()
    elapsed = time.monotonic() - started
    release.set()  # the call is done or abandoned; free the lock
    return box, blocked, elapsed, rm_config


def test_new_notebook_returns_error_envelope_within_bound(monkeypatch, tmp_path):
    box, blocked, elapsed, rm_config = _run_new_notebook(monkeypatch, tmp_path)
    assert not blocked, (
        f"rm_new_notebook still blocked after {BOUND_S}s: it is waiting on the "
        f"rmapi lock (default {rm_config.DEFAULT_RMAPI_LOCK_TIMEOUT_S}s per call, "
        f"several calls per notebook)")
    assert "raised" not in box, f"raised instead of returning an envelope: {box['raised']!r}"
    result = box["result"]
    assert result.get("ok") is False
    text = str(result).lower()
    assert "another rm tool" in text
    assert "nothing was uploaded" in text
    assert "retry" in text
    assert elapsed < BOUND_S


def test_default_run_rmapi_lock_timeout_is_unchanged():
    import inspect
    import rm_config

    sig = inspect.signature(rm_config.run_rmapi)
    assert sig.parameters["lock_timeout"].default is None
    assert rm_config.DEFAULT_RMAPI_LOCK_TIMEOUT_S == 600.0


def test_push_lock_timeout_env_override(monkeypatch):
    from rm_mcp import config

    monkeypatch.delenv("RM_MCP_PUSH_LOCK_TIMEOUT_S", raising=False)
    assert config.push_lock_timeout_s() == 120.0
    monkeypatch.setenv("RM_MCP_PUSH_LOCK_TIMEOUT_S", "7")
    assert config.push_lock_timeout_s() == 7.0
    monkeypatch.setenv("RM_MCP_PUSH_LOCK_TIMEOUT_S", "garbage")
    assert config.push_lock_timeout_s() == 120.0
