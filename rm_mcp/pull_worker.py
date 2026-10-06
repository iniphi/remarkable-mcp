"""Asynchronous project pull: start a job, poll it, fetch finished pages.

The OOM half of the "long pulls" problem (see jobs.py for the why). Three
tools, registered via register(mcp) so server.py stays under its size ceiling,
the same pattern manage.py and pages.py use:

    rm_pull_project_start   validates exactly as rm_pull_project does, makes a
                            job, and returns its id at once
    rm_pull_project_status  state and pages done / total
    rm_pull_project_fetch   finished page results BY REFERENCE, optional range

The synchronous rm_pull_project is untouched and keeps its exact behaviour.

HOW A JOB RUNS. A daemon thread in the server process fetches the bundle once,
deletes the downloaded .rmdoc the moment it is extracted (so the bundle and its
extraction are never both resident), then renders ONE PAGE PER SUBPROCESS
(tools/rm_render_page.py --pages N). Peak memory of the heavy step is therefore
one page, not the document, and the subprocess hands its memory back to the OS
when it exits. Each page's result is committed to disk as it completes, so a
caller can fetch pages 1-5 while page 6 is still rendering. Document-level
steps (typed text, flatten, highlights) run last, after every page is safe.

At most RM_MCP_PULL_PAGE_WORKERS pages (default 1, hard ceiling 2) are in
flight at once. A ResidencyProbe counts them, which is what the tests assert.

FAILURE. The first page that fails stops the job: it is marked failed with that
page's number, pages already committed stay fetchable, and the failed page's
partial artefacts are deleted. A vision-interpretation failure on a page is not
a page failure -- it mirrors the synchronous tool, where the same failure is a
step warning, not an error.

KNOWN LIMIT (stated rather than hidden). Finished pages' PNGs stay on disk until
the job's TTL, and on Cloud Run that disk is RAM. This bounds the render-time
peak, not the retained total; the TTL (RM_MCP_JOB_TTL_SECONDS) is the lever for
the latter. A background thread also needs CPU allocated between requests
(Cloud Run "CPU always allocated"); that is a deployment setting, not code.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from . import config, device, jobs, roundtrip
from .config import safe_filename_stem
from .envelope import (REMEDIES, RmapiAuthError, RmapiThrottledError,
                       err_result, make_warning, ok_result)

MAX_PAGE_WORKERS = 2
PAGE_TIMEOUT_S = 600

_NO_JOB = ("no such job (the id is unknown, malformed, or has expired)")
_NO_JOB_REMEDY = ("start a new job with rm_pull_project_start; finished jobs "
                  "are kept for RM_MCP_JOB_TTL_SECONDS (default one hour)")


def page_workers() -> int:
    """Concurrent pages, from RM_MCP_PULL_PAGE_WORKERS, clamped to 1..2."""
    raw = os.environ.get("RM_MCP_PULL_PAGE_WORKERS", "").strip()
    try:
        value = int(raw) if raw else 1
    except ValueError:
        value = 1
    return max(1, min(MAX_PAGE_WORKERS, value))


class JobError(Exception):
    """A failure that ends the job, with the system to blame and a remedy."""

    def __init__(self, system: str, message: str, remedy: str,
                 page: int | None = None,
                 data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.system, self.message, self.remedy, self.page = (
            system, message, remedy, page)
        self.data = data

    def as_dict(self) -> dict[str, Any]:
        out = {"system": self.system, "message": self.message,
               "remedy": self.remedy, "page": self.page}
        if self.data:
            out["data"] = self.data
        return out


@dataclass(frozen=True)
class PullSpec:
    """Everything the worker needs, fixed at start so the job is replayable."""

    name: str
    project: str            # the resolved project code
    flatten: bool
    highlights: bool
    interpret: bool
    backend: str
    pages: str | None
    profile: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PullSpec":
        return cls(**raw)


class ResidencyProbe:
    """Counts pages whose artefacts the worker currently holds.

    A page is resident from the moment its render begins until its result has
    been committed and the worker has let go of it. `peak` is the high-water
    mark -- the number the bounded-memory claim is tested against.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.peak = 0

    @contextmanager
    def hold(self, page: int) -> Iterator[None]:
        with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)
        try:
            yield
        finally:
            with self._lock:
                self.current -= 1


# -- the real backend ---------------------------------------------------------

class RmapiBackend:
    """Device fetch + substrate scripts. Tests replace this whole class."""

    def fetch(self, spec: PullSpec, work_dir: Path
              ) -> tuple[Path, str, list[dict[str, Any]]]:
        """Download and extract the bundle. Returns (extracted, device_path, warnings)."""
        try:
            device_dir, warnings = roundtrip.canonical_project_dir(spec.project)
            device_path = f"{device_dir}/{spec.name}"
            bundle = device.get(device_path, work_dir / "download")
        except ValueError as exc:
            raise JobError("config", str(exc), "pass project=<NNN_name>") from exc
        except RmapiAuthError as exc:
            raise JobError("rmapi", str(exc), REMEDIES["not_authenticated"]) from exc
        except RmapiThrottledError as exc:
            # A 429 is a wait, not a retry: carry the throttled remedy and the
            # retry time, as pages.py and roundtrip.py do via throttled_result.
            raise JobError(
                "cloud", str(exc), REMEDIES["rmapi_throttled"],
                data={"throttled_until": exc.until_iso,
                      "retry_after_s": exc.retry_after_s}) from exc
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            raise JobError(
                "cloud", f"could not fetch {spec.name}: {exc}",
                "check the name with rm_list; if the document exists but the "
                "get persistently fails: " + REMEDIES["stale_revision"]) from exc
        extracted = roundtrip._extract_bundle(bundle, work_dir / "extracted")
        # The bundle is the same bytes as its extraction. Keeping both doubles
        # the RAM-backed footprint for nothing, so drop it as soon as it is open.
        shutil.rmtree(bundle.parent, ignore_errors=True)
        return extracted, device_path, warnings

    def page_count(self, extracted: Path) -> int:
        content = next(iter(sorted(extracted.glob("*.content"))), None)
        if content is None:
            raise JobError("config", "the bundle has no .content file",
                           "the document may not be a reMarkable bundle; check "
                           "it with rm_list")
        try:
            data = json.loads(content.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JobError("config", f"unreadable .content file: {exc}",
                           "retry; if it persists the bundle may be corrupt") from exc
        pages = (data.get("cPages") or {}).get("pages") or data.get("pages") or []
        return len(pages)

    def render_page(self, spec: PullSpec, extracted: Path, page: int,
                    out_dir: Path) -> list[Path]:
        """Render exactly one page in its own subprocess."""
        from . import render_profiles

        profile = render_profiles.get_profile(spec.profile)
        args = [str(extracted), "--out", str(out_dir),
                *profile.render_flags(), "--pages", str(page)]
        try:
            proc = roundtrip.run_script("rm_render_page.py", args,
                                        "rm_pull_project",
                                        timeout=PAGE_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise JobError("cloud", f"page {page} render timed out after "
                           f"{exc.timeout}s", REMEDIES["timeout"], page) from exc
        pngs = sorted(out_dir.glob("*.png")) if out_dir.is_dir() else []
        if proc.returncode != 0:
            raise JobError("config", f"render failed: "
                           f"{(proc.stderr or '').strip()[:400]}",
                           "retry the job; if the same page fails again, pull "
                           "the rest with pages= and inspect this one with "
                           "rm_render", page)
        if not pngs:
            raise JobError("config", "render produced no image for this page",
                           "the page may be outside the document; check "
                           "data.document_pages from rm_pull_project_status",
                           page)
        return pngs

    def interpret_page(self, spec: PullSpec, page_dir: Path) -> dict[str, Any]:
        """The vision step for one page, mirroring the synchronous decision."""
        script, args = roundtrip._interpret_cmd(page_dir, spec.backend, None)
        # Same expression as pull_project_doc, deliberately: this must not start
        # spending vision money where the synchronous tool would not.
        metered_claude = (spec.backend == "claude"
                          and (config.TOOLS_DIR / script).is_file()
                          and bool(os.environ.get("ANTHROPIC_API_KEY")))
        if spec.backend == "claude" and not metered_claude:
            return {"status": "by calling agent", "files": [],
                    "agent_reads_pages": True}
        proc = roundtrip.run_script(script, args, "rm_interpret_page",
                                    timeout=PAGE_TIMEOUT_S)
        files = sorted(page_dir.glob("*.interpret*.json"))
        if proc.returncode == 127:
            return {"status": f"skipped: {spec.backend} backend not available "
                              f"in this build", "files": [],
                    "agent_reads_pages": True}
        if proc.returncode != 0 and not files:
            return {"status": f"failed: {(proc.stderr.strip() or 'failed')[:300]}",
                    "files": [], "failed": True}
        return {"status": f"ok ({len(files)} file(s), {spec.backend})",
                "files": [str(p) for p in files]}

    def document_steps(self, spec: PullSpec, extracted: Path, doc_dir: Path
                       ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Typed text, flatten, highlights -- once per document, after the pages."""
        doc_dir.mkdir(parents=True, exist_ok=True)
        has_pdf = any(extracted.glob("*.pdf"))
        document: dict[str, Any] = {
            "document_kind": "pdf" if has_pdf else "notebook"}
        status: dict[str, str] = {}
        warnings: list[dict[str, Any]] = []

        def failed(step: str, detail: str) -> None:
            status[step] = f"failed: {detail}"
            warnings.append(make_warning(
                "step_failed", f"{spec.name}: {step} failed -- {detail}",
                data={"step": step}))

        typed_path = doc_dir / "typed_text.json"
        proc = roundtrip.run_script(
            "rm_extract_text.py", [str(extracted), "--out", str(typed_path)],
            "rm_pull_project")
        if proc.returncode == 0 and typed_path.is_file():
            try:
                typed = json.loads(typed_path.read_text(encoding="utf-8"))
                document["typed_text_path"] = str(typed_path)
                status["typed_text"] = (
                    f"ok ({len(typed.get('pages', []))} page(s) with text)")
            except (OSError, json.JSONDecodeError) as exc:
                failed("typed_text", f"unreadable output: {exc}")
        else:
            failed("typed_text", (proc.stderr.strip() or "failed")[:400])

        if spec.flatten and not has_pdf:
            status["flatten"] = "skipped: native notebook, no PDF layer to flatten onto"
        elif spec.flatten:
            flat = doc_dir / f"{safe_filename_stem(Path(spec.name).stem)}.flat.pdf"
            proc = roundtrip.run_script(
                "rm_flatten.py", [str(extracted), "--out", str(flat), "--quiet"],
                "rm_flatten")
            if proc.returncode == 0 and flat.is_file():
                document["annotated_pdf"] = str(flat)
                status["flatten"] = "ok"
            elif proc.returncode == 127:
                status["flatten"] = "skipped: not available in this build"
            else:
                failed("flatten", (proc.stderr.strip() or "no output")[:400])

        if spec.highlights and not has_pdf:
            status["highlights"] = ("skipped: native notebook, no text layer "
                                    "to intersect")
        elif spec.highlights:
            proc = roundtrip.run_script(
                "rm_extract_highlights.py", [str(extracted), "--json"],
                "rm_get_highlights")
            if proc.returncode == 0:
                try:
                    records = json.loads(proc.stdout)
                    path = doc_dir / "highlights.json"
                    path.write_text(json.dumps(records), encoding="utf-8")
                    document["highlights_path"] = str(path)
                    status["highlights"] = f"ok ({len(records)} record(s))"
                except json.JSONDecodeError:
                    failed("highlights", "non-JSON output")
            else:
                failed("highlights", (proc.stderr.strip() or "failed")[:400])

        document["step_status"] = status
        return document, warnings


def make_backend() -> RmapiBackend:
    """Seam: tests replace this to run a job against a fake 20-page document."""
    return RmapiBackend()


# -- the job runner -----------------------------------------------------------

def run_pull_job(store: jobs.JobStore, job_id: str, backend: Any = None,
                 probe: ResidencyProbe | None = None,
                 workers: int | None = None) -> None:
    """Run one job to a terminal state. Never raises: failure is recorded."""
    try:
        _run(store, job_id, backend or make_backend(),
             probe or ResidencyProbe(), workers or page_workers())
    except JobError as exc:
        store.fail(job_id, exc.as_dict())
    except Exception as exc:  # noqa: BLE001 -- the job must end in a state
        store.fail(job_id, {
            "system": "config", "message": f"{type(exc).__name__}: {exc}",
            "remedy": "retry the job; if it recurs, report the message",
            "page": None})
    finally:
        _active.discard(job_id)


def _run(store: jobs.JobStore, job_id: str, backend: Any,
         probe: ResidencyProbe, workers: int) -> None:
    record = store.get(job_id)
    if record is None:
        return
    spec = PullSpec.from_dict(record["spec"])

    extracted, device_path, warnings = backend.fetch(spec, store.work_dir(job_id))
    total = backend.page_count(extracted)
    selected = jobs.parse_page_spec(spec.pages, total)
    if not selected:
        raise JobError("config",
                       f"no pages selected (document has {total}, "
                       f"pages={spec.pages!r})",
                       "pass pages= within the document's page count")
    store.update(job_id, state=jobs.RUNNING, pages_total=total,
                 pages_selected=selected, extracted_dir=str(extracted),
                 device_path=device_path, warnings=warnings)

    stop = threading.Event()
    lock = threading.Lock()
    failures: list[JobError] = []
    page_status: dict[int, dict[str, Any]] = {}

    def one_page(page: int) -> None:
        if stop.is_set():
            return
        out_dir = store.page_dir(job_id, page)
        try:
            with probe.hold(page):
                out_dir.mkdir(parents=True, exist_ok=True)
                pngs = backend.render_page(spec, extracted, page, out_dir)
                interp: dict[str, Any] = {"status": "skipped: interpret=False",
                                          "files": []}
                if spec.interpret:
                    interp = backend.interpret_page(spec, out_dir)
                store.record_page(job_id, page, {
                    "files": [str(p) for p in pngs],
                    "interpretations": list(interp.get("files", [])),
                    "interpret_status": interp.get("status"),
                })
            with lock:
                page_status[page] = interp
        except JobError as exc:
            _page_failed(exc if exc.page else _with_page(exc, page), out_dir)
        except Exception as exc:  # noqa: BLE001
            _page_failed(JobError(
                "config", f"{type(exc).__name__}: {exc}",
                "retry the job; if the same page fails again, pull the rest "
                "with pages=", page), out_dir)

    def _page_failed(exc: JobError, out_dir: Path) -> None:
        stop.set()
        shutil.rmtree(out_dir, ignore_errors=True)
        with lock:
            failures.append(exc)

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="rm-pull-page") as pool:
        list(pool.map(one_page, selected))

    if failures:
        first = min(failures, key=lambda e: e.page or 0)
        store.fail(job_id, first.as_dict(), warnings=warnings)
        return

    document, doc_warnings = backend.document_steps(
        spec, extracted, store.job_dir(job_id) / "document")
    statuses = [s.get("status", "") for s in page_status.values()]
    document["step_status"] = {
        "get": "ok",
        "render": f"ok ({len(selected)} page(s), one subprocess each)",
        "interpret": ("skipped: interpret=False" if not spec.interpret
                      else _summarise(statuses)),
        **document.get("step_status", {}),
    }
    if any(s.get("agent_reads_pages") for s in page_status.values()):
        document["agent_reads_pages"] = True
        document["guidance"] = roundtrip._AGENT_GUIDANCE
    for status in statuses:
        if status.startswith("failed"):
            doc_warnings.append(make_warning(
                "step_failed", f"{spec.name}: interpret failed -- {status}",
                data={"step": "interpret"}))
    store.finish(job_id, document=document, warnings=warnings + doc_warnings)


def _with_page(exc: JobError, page: int) -> JobError:
    return JobError(exc.system, exc.message, exc.remedy, page, exc.data)


def _summarise(statuses: list[str]) -> str:
    """One line for N per-page interpret outcomes."""
    if not statuses:
        return "skipped: no pages"
    distinct = sorted(set(statuses))
    return distinct[0] if len(distinct) == 1 else "; ".join(distinct)


# -- the tools' implementations ----------------------------------------------

_active: set[str] = set()
_start_lock = threading.Lock()


def _spawn(target: Callable[[], None], name: str) -> None:
    """Seam: start the worker thread. Daemon, so it never blocks shutdown."""
    threading.Thread(target=target, name=name, daemon=True).start()


def start_pull(name: str, project: str | None, flatten: bool, highlights: bool,
               interpret: bool, backend: str | None, pages: str | None,
               profile: str | None, dry_run: bool) -> dict[str, Any]:
    """Validate like rm_pull_project, make a job, return its id at once."""
    backend, rprofile, option_error = roundtrip.check_pull_options(backend, profile)
    if option_error is not None:
        return option_error
    try:
        code = config.resolve_project(project)
    except ValueError as exc:
        return err_result("config", str(exc), "pass project=<NNN_name>")
    if pages:
        try:
            jobs.parse_page_spec(pages)
        except ValueError as exc:
            return err_result("config", str(exc),
                              'use a page selection like "1,3,5-7"')

    spec = PullSpec(name=name, project=code, flatten=flatten,
                    highlights=highlights, interpret=interpret,
                    backend=backend, pages=pages or None, profile=rprofile.name)
    plan = {"name": name, "project": code,
            "steps": [s for s, on in (("get", True), ("render per page", True),
                                      ("flatten", flatten),
                                      ("highlights", highlights),
                                      (f"interpret[{backend}]", interpret)) if on],
            "render_profile": rprofile.name, "pages": pages}
    if dry_run:
        return ok_result({"dry_run": True, **plan})

    store = jobs.default_store()
    with _start_lock:
        job_id = store.create(spec.to_dict())
        _active.add(job_id)
    _spawn(lambda: run_pull_job(store, job_id), f"rm-pull-{job_id[:8]}")
    return ok_result({
        "job_id": job_id, "state": jobs.QUEUED, **plan,
        "ttl_seconds": store.ttl,
        "next": "poll rm_pull_project_status(job_id); fetch finished pages "
                "with rm_pull_project_fetch(job_id, pages=...)"})


def _orphaned(record: dict[str, Any]) -> bool:
    """A non-terminal job whose worker is gone (restart, or a dead thread)."""
    if record["state"] in jobs.TERMINAL_STATES:
        return False
    if record.get("owner") != jobs.PROCESS_TOKEN:
        return True
    return record["job_id"] not in _active


def _load(job_id: str) -> tuple[jobs.JobStore, dict[str, Any] | None]:
    store = jobs.default_store()
    with _start_lock:   # a job is created and marked active as one step
        record = store.get(job_id)
        orphaned = record is not None and _orphaned(record)
    if record is not None and orphaned:
        store.fail(job_id, {
            "system": "config",
            "message": "the worker for this job is gone (the server "
                       "restarted, or the worker thread died)",
            "remedy": "start the job again with rm_pull_project_start; pages "
                      "already finished remain fetchable until the TTL",
            "page": None})
        record = store.get(job_id)
    return store, record


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _progress(store: jobs.JobStore, record: dict[str, Any]) -> dict[str, Any]:
    done = store.done_pages(record["job_id"])
    selected = record.get("pages_selected") or []
    error = record.get("error")
    return {
        "job_id": record["job_id"],
        "state": record["state"],
        "pages_done": len(done),
        "pages_total": len(selected) if selected else None,
        "document_pages": record.get("pages_total"),
        "done_pages": done,
        "failed_page": error.get("page") if error else None,
        "error": error,
        "created_at": _iso(record.get("created_at")),
        "updated_at": _iso(record.get("updated_at")),
        "expires_at": _iso(record.get("expires_at")),
    }


def pull_status(job_id: str) -> dict[str, Any]:
    store, record = _load(job_id)
    if record is None:
        return err_result("config", _NO_JOB, _NO_JOB_REMEDY)
    return ok_result(_progress(store, record), warnings=record.get("warnings") or [])


def pull_fetch(job_id: str, pages: str | None = None) -> dict[str, Any]:
    store, record = _load(job_id)
    if record is None:
        return err_result("config", _NO_JOB, _NO_JOB_REMEDY)
    selected = record.get("pages_selected") or []
    try:
        requested = (jobs.parse_page_spec(pages, record.get("pages_total"))
                     if pages else None)
    except ValueError as exc:
        return err_result("config", str(exc),
                          'use a page selection like "1,3,5-7"')
    results = store.page_results(record["job_id"], requested)
    have = {r["page"] for r in results}
    wanted = (requested if requested is not None else selected)
    if selected and requested is not None:
        wanted = [p for p in requested if p in set(selected)]
    missing = [p for p in wanted if p not in have]
    data = {
        **_progress(store, record),
        "results": results,
        "missing": missing,
        "complete": record["state"] == jobs.DONE and not missing,
        "extracted_dir": record.get("extracted_dir"),
        "device_path": record.get("device_path"),
        "document": record.get("document"),
    }
    return ok_result(data, warnings=record.get("warnings") or [])


def register(mcp) -> None:
    """Attach the three async-pull tools to the server's FastMCP instance."""

    @mcp.tool()
    async def rm_pull_project_start(
            name: str, project: str | None = None, flatten: bool = True,
            highlights: bool = True, interpret: bool = True,
            backend: str | None = None, pages: str | None = None,
            profile: str = "analysis", dry_run: bool = False) -> dict:
        """Start an ASYNCHRONOUS pull of an annotated document; returns a job id.

        Use this instead of rm_pull_project for long documents or on the hosted
        server. rm_pull_project does the whole document inside one request, and
        on a RAM-backed instance a big one can exhaust memory and come back as
        an empty 200. This renders ONE PAGE PER SUBPROCESS in the background,
        so peak memory is a page, not the document, and the request returns at
        once. Arguments and validation are exactly rm_pull_project's.

        Then poll rm_pull_project_status(job_id) and collect finished pages with
        rm_pull_project_fetch(job_id, pages="1-5"). Pages are fetchable as soon
        as each completes. The first failing page stops the job and is named.
        Results are kept for RM_MCP_JOB_TTL_SECONDS (default one hour).

        Args:
            name: Device document name inside the project folder (as shown by
                rm_list), without extension.
            project: Project code (NNN_name) -- pass explicitly.
            flatten: Produce the flat annotated PDF (document-level, runs last).
            highlights: Extract highlight records (document-level, runs last).
            interpret: Render pages + run the vision pass per page.
            backend: claude | gemini-pro | gemini-flash (default claude, which
                costs nothing because the calling agent reads the page itself).
            pages: Page selection like "1,3,5-7" (default: all).
            profile: "analysis" (default) | "publication".
            dry_run: Return the plan and make no job.
        Returns:
            RmResult with data.job_id, data.state="queued", data.plan fields.
        """
        return await asyncio.to_thread(
            start_pull, name, project, flatten, highlights, interpret,
            backend, pages, profile, dry_run)

    @mcp.tool()
    async def rm_pull_project_status(job_id: str) -> dict:
        """State and progress of a job from rm_pull_project_start.

        Cheap and safe to poll. state is queued | running | done | failed.

        Args:
            job_id: The id rm_pull_project_start returned.
        Returns:
            RmResult with data.state, data.pages_done, data.pages_total,
            data.done_pages, data.failed_page and data.error (system, message,
            remedy, page) when failed, data.expires_at. An unknown or expired
            id is an error, not an empty result.
        """
        return await asyncio.to_thread(pull_status, job_id)

    @mcp.tool()
    async def rm_pull_project_fetch(job_id: str,
                                    pages: str | None = None) -> dict:
        """Finished page results of a job, BY REFERENCE, optionally a range.

        Works on a running job (returns what is finished so far, and lists the
        rest in data.missing), a done job, and a failed one (the pages that
        completed before the failure). Results are file paths on the server,
        like rm_pull_project's -- read the PNGs yourself and report what the ink
        says; data.document carries the typed-text, annotated-PDF and
        highlights references plus the per-step status once the job is done.

        Args:
            job_id: The id rm_pull_project_start returned.
            pages: Optional selection like "1,3,5-7" (default: every finished
                page).
        Returns:
            RmResult with data.results = [{page, files, interpretations,
            interpret_status, completed_at}], data.missing, data.complete,
            data.document, plus the status fields.
        """
        return await asyncio.to_thread(pull_fetch, job_id, pages)
