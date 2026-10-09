"""Persist rmapi's tree cache in the GCS state bucket.

A fresh Cloud Run instance has an empty filesystem, so rmapi must re-sync the
whole reMarkable document tree (>150 s). rmapi (ddvk fork) keeps that tree at
`<user cache dir>/rmapi/tree.cache` (Go os.UserCacheDir). This module restores
that file from the bucket before the warm `rmapi ls /`, and saves it back after
a successful warm, so only the first instance ever pays the full sync.

rm_state_remote lives in tools/rm and is NOT in the public manifest, so it is
imported lazily; where it is missing every step reports "skipped (no remote
support)". Nothing here raises: a cache is an optimisation, never a blocker.

CLI (from rm-mcp/):  python -m rm_mcp.tree_cache seed <path> | restore | save

Env: RM_STATE_BUCKET (shared with the ledger), RM_RMAPI_CACHE_OBJECT
(default rmapi/tree.cache), RM_RMAPI_CACHE_PATH (override the local path).
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_OBJECT = "rmapi/tree.cache"
_NO_SUPPORT = "skipped (no remote support)"


def cache_path() -> Path:
    """Where rmapi keeps tree.cache: Go's os.UserCacheDir() + rmapi/tree.cache."""
    override = (os.environ.get("RM_RMAPI_CACHE_PATH") or "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = (os.environ.get("LOCALAPPDATA") or "").strip()
    else:
        base = (os.environ.get("XDG_CACHE_HOME") or "").strip()
        if not base:
            home = (os.environ.get("HOME") or "").strip() or str(Path.home())
            base = str(Path(home) / ".cache")
    return Path(base) / "rmapi" / "tree.cache"


def object_name() -> str:
    return (os.environ.get("RM_RMAPI_CACHE_OBJECT") or "").strip() or DEFAULT_OBJECT


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _remote():
    """The rm_state_remote module, or None where it is not shipped."""
    try:
        from . import config  # noqa: F401  (puts tools/ buckets on sys.path)
        import rm_state_remote
        return rm_state_remote
    except ImportError:
        return None


def _ready():
    """(module, None) when a bucket is configured, else (None, skip reason)."""
    remote = _remote()
    if remote is None:
        return None, _NO_SUPPORT
    if not remote.configured():
        return None, "skipped (no bucket)"
    return remote, None


def restore() -> str:
    try:
        remote, reason = _ready()
        if remote is None:
            return reason
        path = cache_path()
        if path.is_file():
            return "kept local"
        data, _gen = remote.fetch_object(object_name())
        if data is None:
            return "no remote cache"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tree.")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return f"restored {len(data)} bytes"
    except Exception as exc:  # noqa: BLE001 - never block startup
        return f"restore failed: {type(exc).__name__}: {exc}"


def save() -> str:
    try:
        remote, reason = _ready()
        if remote is None:
            return reason
        path = cache_path()
        if not path.is_file():
            return "skipped (no local cache)"
        local = path.read_bytes()
        remote_data, generation = remote.fetch_object(object_name())
        if remote_data is not None and _sha(remote_data) == _sha(local):
            return "unchanged"
        remote.upload_object(object_name(), local,
                             generation or remote.GENERATION_ABSENT)
        return f"saved {len(local)} bytes"
    except Exception as exc:  # noqa: BLE001
        return f"save failed: {type(exc).__name__}: {exc}"


def seed(source: str) -> tuple[bool, str]:
    """Upload a given local cache file. Returns (ok, one-line outcome)."""
    try:
        remote, reason = _ready()
        if remote is None:
            return False, reason
        data = Path(source).read_bytes()
        _old, generation = remote.fetch_object(object_name())
        remote.upload_object(object_name(), data,
                             generation or remote.GENERATION_ABSENT)
        return True, f"seeded {len(data)} bytes to {object_name()}"
    except Exception as exc:  # noqa: BLE001
        return False, f"seed failed: {type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    if cmd == "seed" and len(args) == 2:
        ok, line = seed(args[1])
        print(line)
        return 0 if ok else 1
    if cmd == "restore":
        print(restore())
        return 0
    if cmd == "save":
        print(save())
        return 0
    print("usage: python -m rm_mcp.tree_cache seed <path> | restore | save",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
