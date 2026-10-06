"""A small on-disk job store for asynchronous pulls.

WHY THIS EXISTS. A large single rm_pull_project call renders every page inside
one request. On Cloud Run the container filesystem is RAM, so the bundle, the
extracted tree and every rendered PNG all count against the instance memory
limit at once, and the failure is the worst kind: the instance is OOM-killed
mid-request and the client sees HTTP 200 with an empty body. Output eviction,
an image-size cap and the heavy-tool rate bucket only mitigate it. The cure is
to stop doing the whole document in one request: start a job, let a worker
render one page at a time, and let the caller collect finished pages by
reference while the rest are still being made.

This module is the store half. It knows nothing about reMarkable documents:
a job is an id, a state, a spec the worker needs, per-page results, and a TTL.

LAYOUT (everything under one root, one directory per job):

    <root>/<job_id>/job.json            state + spec + document-level results
    <root>/<job_id>/pages/<NNNN>/       one directory per page
        result.json                     written LAST, atomically, when the page
                                        is complete -- its existence IS the
                                        definition of "this page is done"
        ...                             the page's artefacts (png, interpretation)
    <root>/<job_id>/work/               the fetched bundle's extraction

CONCURRENCY. Every JSON file is written by writing a sibling temp file and
os.replace()-ing it, so a reader never sees a torn file. Progress is DERIVED
from which result.json files exist rather than kept as a counter in job.json,
so two page workers finishing at once never contend over a shared field and a
status reader needs no lock at all. job.json changes only on state transitions,
and only the coordinating thread writes it.

TTL. Every job carries expires_at, refreshed on each state change. A finished
job's results stay fetchable until then; a job that stops updating (the process
died mid-run) expires the same way. Expired jobs are removed lazily, on every
store access, so no background reaper is needed. Default one hour, override
with RM_MCP_JOB_TTL_SECONDS. No dependencies beyond the standard library.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
STATES = (QUEUED, RUNNING, DONE, FAILED)
TERMINAL_STATES = (DONE, FAILED)

DEFAULT_TTL_SECONDS = 3600

# Job ids are minted here and nowhere else, so anything that is not exactly
# 32 lowercase hex characters is not ours -- and is never joined onto a path.
_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_WRITE_RETRIES = 8

# Identifies THIS server process. A job records the token of the process that
# created it; a job still queued/running under a different token has lost its
# worker (the server restarted), which a status call can then say plainly
# instead of reporting "running" forever.
PROCESS_TOKEN = uuid.uuid4().hex


def ttl_seconds() -> int:
    """The configured job TTL in seconds (RM_MCP_JOB_TTL_SECONDS)."""
    raw = os.environ.get("RM_MCP_JOB_TTL_SECONDS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_TTL_SECONDS
    except ValueError:
        return DEFAULT_TTL_SECONDS
    return value if value > 0 else DEFAULT_TTL_SECONDS


def valid_job_id(job_id: object) -> bool:
    return isinstance(job_id, str) and bool(_ID_RE.match(job_id))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace path with payload.

    os.replace can fail transiently on Windows while a reader has the target
    open, so a few short retries; on POSIX it succeeds first time.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    for attempt in range(_WRITE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _WRITE_RETRIES - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON file written by _write_json; None if absent or unreadable."""
    for attempt in range(_WRITE_RETRIES):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (PermissionError, json.JSONDecodeError):
            # os.replace is atomic, so a decode error means a foreign writer;
            # a permission error means a Windows replace is mid-flight.
            if attempt == _WRITE_RETRIES - 1:
                return None
            time.sleep(0.02 * (attempt + 1))
    return None


class JobStore:
    """The on-disk store. Cheap to construct; holds no open handles."""

    def __init__(self, root: Path, ttl: int | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.ttl = ttl if ttl is not None else ttl_seconds()
        self._clock = clock

    # -- paths ---------------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        if not valid_job_id(job_id):
            raise ValueError(f"not a job id: {job_id!r}")
        return self.root / job_id

    def work_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "work"

    def page_dir(self, job_id: str, page: int) -> Path:
        return self.job_dir(job_id) / "pages" / f"{int(page):04d}"

    # -- lifecycle -----------------------------------------------------------

    def create(self, spec: dict[str, Any]) -> str:
        """Make a queued job and return its id."""
        self.sweep()
        job_id = uuid.uuid4().hex
        now = self._clock()
        self._save(job_id, {
            "job_id": job_id,
            "state": QUEUED,
            "spec": spec,
            "pages_total": None,
            "pages_selected": [],
            "error": None,
            "document": None,
            "warnings": [],
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
            "expires_at": now + self.ttl,
            "owner": PROCESS_TOKEN,
        })
        return job_id

    def get(self, job_id: object) -> dict[str, Any] | None:
        """The job record, or None for an unknown, malformed or expired id."""
        if not valid_job_id(job_id):
            return None
        self.sweep()
        record = _read_json(self.job_dir(job_id) / "job.json")
        if record is None:
            return None
        if record.get("expires_at", 0) <= self._clock():
            self.delete(job_id)
            return None
        return record

    def update(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        """Merge fields into the job record and refresh its TTL."""
        record = _read_json(self.job_dir(job_id) / "job.json")
        if record is None:
            return None
        now = self._clock()
        record.update(fields)
        record["updated_at"] = now
        record["expires_at"] = now + self.ttl
        if record["state"] in TERMINAL_STATES and not record.get("finished_at"):
            record["finished_at"] = now
        self._save(job_id, record)
        return record

    def mark_running(self, job_id: str, pages_total: int,
                     pages_selected: list[int]) -> None:
        self.update(job_id, state=RUNNING, pages_total=pages_total,
                    pages_selected=list(pages_selected))

    def finish(self, job_id: str, document: dict[str, Any] | None = None,
               warnings: list[dict[str, Any]] | None = None) -> None:
        self.update(job_id, state=DONE, document=document,
                    warnings=warnings or [])

    def fail(self, job_id: str, error: dict[str, Any],
             document: dict[str, Any] | None = None,
             warnings: list[dict[str, Any]] | None = None) -> None:
        self.update(job_id, state=FAILED, error=error, document=document,
                    warnings=warnings or [])

    def delete(self, job_id: str) -> None:
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)

    def sweep(self) -> list[str]:
        """Remove every expired job. Returns the ids removed."""
        removed: list[str] = []
        if not self.root.is_dir():
            return removed
        now = self._clock()
        for entry in self.root.iterdir():
            if not (entry.is_dir() and valid_job_id(entry.name)):
                continue
            record = _read_json(entry / "job.json")
            if record is None:
                # A directory with no readable record is only ever a job being
                # created (job.json is the first file written) or debris.
                # Leave a young one alone; reap one older than the TTL.
                try:
                    age = now - entry.stat().st_mtime
                except OSError:
                    continue
                if age <= self.ttl:
                    continue
            elif record.get("expires_at", 0) > now:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
        return removed

    # -- per-page results ----------------------------------------------------

    def record_page(self, job_id: str, page: int,
                    result: dict[str, Any]) -> None:
        """Commit one page's result. Written last, so its existence means done."""
        payload = {**result, "page": int(page), "completed_at": self._clock()}
        _write_json(self.page_dir(job_id, page) / "result.json", payload)

    def done_pages(self, job_id: str) -> list[int]:
        """Page numbers with a committed result, ascending."""
        pages_root = self.job_dir(job_id) / "pages"
        if not pages_root.is_dir():
            return []
        done = []
        for entry in pages_root.iterdir():
            if (entry / "result.json").is_file() and entry.name.isdigit():
                done.append(int(entry.name))
        return sorted(done)

    def page_results(self, job_id: str,
                     pages: list[int] | None = None) -> list[dict[str, Any]]:
        """Committed results, ascending, optionally limited to `pages`."""
        wanted = self.done_pages(job_id)
        if pages is not None:
            keep = set(pages)
            wanted = [p for p in wanted if p in keep]
        results = []
        for page in wanted:
            record = _read_json(self.page_dir(job_id, page) / "result.json")
            if record is not None:
                results.append(record)
        return results

    # -- internals -----------------------------------------------------------

    def _save(self, job_id: str, record: dict[str, Any]) -> None:
        _write_json(self.job_dir(job_id) / "job.json", record)


_default_store: JobStore | None = None
_default_lock = threading.Lock()


def default_store() -> JobStore:
    """The process-wide store, under the server's session directory.

    RM_MCP_JOBS_DIR overrides the root. The default sits beside the per-call
    output dirs but is NOT registered with config's output eviction: that
    eviction is by count and would delete a job's pages out from under a caller
    mid-fetch. Jobs are reaped by TTL, here, instead.
    """
    global _default_store
    from . import config

    root_env = os.environ.get("RM_MCP_JOBS_DIR", "").strip()
    root = Path(root_env) if root_env else config.SESSION_DIR / "jobs"
    with _default_lock:
        if _default_store is None or _default_store.root != root:
            _default_store = JobStore(root)
        return _default_store


def parse_page_spec(spec: str | None, total: int | None = None) -> list[int]:
    """Parse '1,3,5-7' into ascending 1-indexed page numbers.

    With `total`, numbers outside 1..total are dropped (the render script's own
    behaviour). Without it the spec is only syntax-checked. A malformed spec
    raises ValueError, which the tools turn into a config error with a remedy.
    """
    if not spec or not spec.strip():
        return list(range(1, total + 1)) if total is not None else []
    pages: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            if "-" in chunk:
                first, last = chunk.split("-", 1)
                lo, hi = int(first), int(last)
            else:
                lo = hi = int(chunk)
        except ValueError:
            raise ValueError(f"bad page selection {chunk!r}") from None
        if lo < 1 or hi < lo:
            raise ValueError(f"bad page selection {chunk!r}")
        if total is None:
            pages.update((lo, hi))
        else:
            pages.update(range(lo, min(hi, total) + 1))
    return sorted(p for p in pages if total is None or 1 <= p <= total)
