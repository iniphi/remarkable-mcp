"""The asynchronous project pull: start, status, fetch.

The OOM fix for long pulls (ruled 2026-10-01). Everything here is offline: no
rmapi, no network, no substrate
script. A fake backend stands in for the device and the renderer, and builds a
20-page document, so the tests exercise the job machinery -- the store, the
worker's bounded residency, the failure and TTL rules, the tool contracts --
rather than the renderer, which test_pull_project_loop and the render goldens
already cover. One test wires the REAL backend to the same fakes that suite
uses, to prove the per-page subprocess call shape.

Run: python -m pytest tests/test_async_pull.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from rm_mcp import authz, jobs, pull_worker, roundtrip, surface  # noqa: E402

PAGES = 20

# Captured before any test patches it, so the clamp can be tested for real.
_REAL_PAGE_WORKERS = pull_worker.page_workers


class FakeBackend:
    """A 20-page document with no device behind it.

    Residency is measured here INDEPENDENTLY of the worker's own probe: on every
    render it counts pages that have started but whose result.json is not yet
    committed to the store, which is the on-disk definition of "resident".
    """

    def __init__(self, store: jobs.JobStore, pages: int = PAGES, *,
                 fail_page: int | None = None, gate: threading.Event | None = None,
                 render_delay: float = 0.0) -> None:
        self.store = store
        self.pages = pages
        self.fail_page = fail_page
        self.gate = gate
        self.render_delay = render_delay
        self._lock = threading.Lock()
        self._started: set[int] = set()
        self.job_id: str | None = None
        self.max_resident = 0
        self.rendered: list[int] = []

    def fetch(self, spec, work_dir):
        self.job_id = work_dir.parent.name      # <root>/<job_id>/work
        if self.gate is not None:
            self.gate.wait(timeout=20)
        extracted = work_dir / "extracted"
        extracted.mkdir(parents=True, exist_ok=True)
        return extracted, f"/101_Demo/{spec.name}", []

    def page_count(self, extracted):
        return self.pages

    def render_page(self, spec, extracted, page, out_dir):
        with self._lock:
            self._started.add(page)
            committed = set(self.store.done_pages(self.job_id))
            resident = len(self._started - committed)
            self.max_resident = max(self.max_resident, resident)
            self.rendered.append(page)
        if self.render_delay:
            time.sleep(self.render_delay)
        if page == self.fail_page:
            raise pull_worker.JobError("config", "renderer exploded", "retry")
        png = out_dir / f"page_{page:03d}.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        return [png]

    def interpret_page(self, spec, page_dir):
        return {"status": "by calling agent", "files": [],
                "agent_reads_pages": True}

    def document_steps(self, spec, extracted, doc_dir):
        return {"document_kind": "pdf", "step_status": {"flatten": "ok"}}, []


class AsyncPullCase(unittest.TestCase):
    """A temp store, the default store patched to it, and a fake backend."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rm_jobs_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = jobs.JobStore(self.tmp / "jobs", ttl=3600)
        for patcher in (
                mock.patch.object(jobs, "default_store", return_value=self.store),
                mock.patch.object(pull_worker, "page_workers", return_value=1)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self, backend: FakeBackend, **kw):
        """Start a job through the real tool function, with `backend` injected."""
        kw.setdefault("name", "Brief")
        # "101_Demo", not "Demo": a desk with RM_MCP_PROJECT_PATTERN=^\d{3}_...
        # (tools/.env) rejects a bare name, so the fixture must be valid under
        # both the public any-name default and the desk's NNN_name pin.
        kw.setdefault("project", "101_Demo")
        args = dict(flatten=True, highlights=True, interpret=True, backend=None,
                    pages=None, profile="analysis", dry_run=False)
        args.update(kw)

        with mock.patch.object(pull_worker, "make_backend", lambda: backend):
            return pull_worker.start_pull(**args)

    def wait(self, job_id: str, states=(jobs.DONE, jobs.FAILED), timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            record = self.store.get(job_id)
            if record and record["state"] in states:
                return record
            time.sleep(0.01)
        self.fail(f"job {job_id} did not reach {states}: {self.store.get(job_id)}")


class TestStartReturnsAtOnce(AsyncPullCase):
    def test_start_returns_a_job_id_while_the_work_is_still_blocked(self):
        gate = threading.Event()
        backend = FakeBackend(self.store, gate=gate)
        began = time.time()
        result = self.start(backend)
        elapsed = time.time() - began
        self.assertTrue(result["ok"], result)
        job_id = result["data"]["job_id"]
        self.assertTrue(jobs.valid_job_id(job_id))
        self.assertLess(elapsed, 2.0, "start must not wait for the render")
        # The fetch is still gated, so nothing can have finished.
        self.assertIn(self.store.get(job_id)["state"], (jobs.QUEUED, jobs.RUNNING))
        self.assertEqual(self.store.done_pages(job_id), [])
        gate.set()
        self.wait(job_id)

    def test_dry_run_makes_no_job(self):
        result = self.start(FakeBackend(self.store), dry_run=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["dry_run"])
        self.assertNotIn("job_id", result["data"])
        self.assertFalse(self.store.root.exists() and any(self.store.root.iterdir()))


class TestJobRunsToDone(AsyncPullCase):
    def setUp(self) -> None:
        super().setUp()
        self.backend = FakeBackend(self.store)
        self.job_id = self.start(self.backend)["data"]["job_id"]
        self.wait(self.job_id)

    def test_status_reaches_done_with_all_pages(self):
        status = pull_worker.pull_status(self.job_id)
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["data"]["state"], "done")
        self.assertEqual(status["data"]["pages_done"], PAGES)
        self.assertEqual(status["data"]["pages_total"], PAGES)

    def test_fetch_returns_all_twenty_page_results_by_reference(self):
        fetched = pull_worker.pull_fetch(self.job_id)
        self.assertTrue(fetched["ok"], fetched)
        results = fetched["data"]["results"]
        self.assertEqual([r["page"] for r in results], list(range(1, PAGES + 1)))
        for record in results:
            self.assertEqual(len(record["files"]), 1)
            self.assertTrue(Path(record["files"][0]).is_file(),
                            "a result is a reference to a file that exists")
        self.assertEqual(fetched["data"]["missing"], [])
        self.assertTrue(fetched["data"]["complete"])
        self.assertEqual(fetched["data"]["document"]["step_status"]["flatten"], "ok")
        self.assertTrue(fetched["data"]["document"]["agent_reads_pages"])

    def test_fetch_honours_a_page_range(self):
        fetched = pull_worker.pull_fetch(self.job_id, "3-5,9")
        self.assertEqual([r["page"] for r in fetched["data"]["results"]],
                         [3, 4, 5, 9])

    def test_fetch_reports_a_bad_range_as_a_config_error(self):
        fetched = pull_worker.pull_fetch(self.job_id, "five")
        self.assertFalse(fetched["ok"])
        self.assertEqual(fetched["error"]["system"], "config")

    def test_pages_selection_limits_the_job(self):
        backend = FakeBackend(self.store)
        job_id = self.start(backend, pages="2,4-6")["data"]["job_id"]
        self.wait(job_id)
        status = pull_worker.pull_status(job_id)["data"]
        self.assertEqual(status["done_pages"], [2, 4, 5, 6])
        self.assertEqual(status["pages_total"], 4)
        self.assertEqual(status["document_pages"], PAGES)


class TestBoundedResidency(AsyncPullCase):
    """No more than two pages' artefacts are held at once, whatever the load."""

    def run_job(self, workers: int):
        backend = FakeBackend(self.store, render_delay=0.005)
        probe = pull_worker.ResidencyProbe()
        spec = pull_worker.PullSpec("Brief", "101_Demo", True, True, True,
                                    "claude", None, "analysis")
        job_id = self.store.create(spec.to_dict())
        pull_worker.run_pull_job(self.store, job_id, backend=backend,
                                 probe=probe, workers=workers)
        self.assertEqual(self.store.get(job_id)["state"], "done")
        self.assertEqual(len(self.store.done_pages(job_id)), PAGES)
        return backend, probe

    def test_sequential_worker_holds_one_page(self):
        backend, probe = self.run_job(workers=1)
        self.assertEqual(probe.peak, 1)
        self.assertEqual(backend.max_resident, 1)

    def test_two_workers_never_hold_more_than_two(self):
        backend, probe = self.run_job(workers=2)
        self.assertLessEqual(probe.peak, 2)
        self.assertLessEqual(backend.max_resident, 2)
        self.assertEqual(probe.current, 0, "every page was released")

    def test_the_configured_worker_count_is_clamped_to_two(self):
        for raw, expected in (("9", 2), ("2", 2), ("1", 1), ("0", 1),
                              ("junk", 1), ("", 1)):
            with self.subTest(raw=raw), \
                    mock.patch.dict(os.environ, {"RM_MCP_PULL_PAGE_WORKERS": raw}):
                self.assertEqual(_REAL_PAGE_WORKERS(), expected)


class TestFailingPage(AsyncPullCase):
    def test_a_failing_page_fails_the_job_and_names_the_page(self):
        backend = FakeBackend(self.store, fail_page=7)
        job_id = self.start(backend)["data"]["job_id"]
        record = self.wait(job_id)
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["error"]["page"], 7)
        self.assertIn("renderer exploded", record["error"]["message"])

        status = pull_worker.pull_status(job_id)["data"]
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["failed_page"], 7)
        # Sequential: 1-6 committed, 7 failed and its partial output removed.
        self.assertEqual(status["done_pages"], [1, 2, 3, 4, 5, 6])
        self.assertFalse(self.store.page_dir(job_id, 7).exists())
        # The job stopped: later pages were never rendered.
        self.assertNotIn(8, backend.rendered)

    def test_completed_pages_stay_fetchable_after_a_failure(self):
        backend = FakeBackend(self.store, fail_page=4)
        job_id = self.start(backend)["data"]["job_id"]
        self.wait(job_id)
        fetched = pull_worker.pull_fetch(job_id)
        self.assertTrue(fetched["ok"])
        self.assertEqual([r["page"] for r in fetched["data"]["results"]], [1, 2, 3])
        self.assertFalse(fetched["data"]["complete"])
        self.assertEqual(fetched["data"]["failed_page"], 4)

    def test_a_fetch_failure_fails_the_job_without_a_page(self):
        class Unreachable(FakeBackend):
            def fetch(self, spec, work_dir):
                raise pull_worker.JobError("rmapi", "not paired", "pair it")

        backend = Unreachable(self.store)
        job_id = self.start(backend)["data"]["job_id"]
        record = self.wait(job_id)
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["error"]["system"], "rmapi")
        self.assertIsNone(record["error"]["page"])

    def test_an_unexpected_exception_still_ends_in_a_state(self):
        class Boom(FakeBackend):
            def page_count(self, extracted):
                raise KeyError("surprise")

        job_id = self.start(Boom(self.store))["data"]["job_id"]
        record = self.wait(job_id)
        self.assertEqual(record["state"], "failed")
        self.assertIn("KeyError", record["error"]["message"])


class TestTtl(AsyncPullCase):
    def test_results_are_removed_when_the_ttl_expires(self):
        clock = [1_000.0]
        store = jobs.JobStore(self.tmp / "ttl", ttl=60, clock=lambda: clock[0])
        job_id = store.create({"name": "x"})
        store.mark_running(job_id, 2, [1, 2])
        page_dir = store.page_dir(job_id, 1)
        page_dir.mkdir(parents=True)
        (page_dir / "page_001.png").write_bytes(b"png")
        store.record_page(job_id, 1, {"files": [str(page_dir / "page_001.png")]})
        store.finish(job_id)

        clock[0] += 59
        self.assertIsNotNone(store.get(job_id), "alive inside the TTL")
        self.assertTrue((page_dir / "page_001.png").is_file())

        clock[0] += 120
        self.assertIsNone(store.get(job_id), "gone after the TTL")
        self.assertFalse(store.job_dir(job_id).exists(),
                         "expiry removes the artefacts, not just the record")

    def test_activity_refreshes_the_ttl(self):
        clock = [0.0]
        store = jobs.JobStore(self.tmp / "ttl2", ttl=60, clock=lambda: clock[0])
        job_id = store.create({})
        clock[0] = 50
        store.update(job_id, state=jobs.RUNNING)
        clock[0] = 100                      # 100s after creation, 50s after update
        self.assertIsNotNone(store.get(job_id))

    def test_sweep_reaps_expired_jobs_on_the_next_access(self):
        clock = [0.0]
        store = jobs.JobStore(self.tmp / "ttl3", ttl=10, clock=lambda: clock[0])
        old = store.create({})
        clock[0] = 100
        new = store.create({})          # create() sweeps
        self.assertFalse(store.job_dir(old).exists())
        self.assertTrue(store.job_dir(new).exists())

    def test_status_of_an_expired_job_is_an_error_not_a_crash(self):
        clock = [0.0]
        store = jobs.JobStore(self.tmp / "ttl4", ttl=5, clock=lambda: clock[0])
        job_id = store.create({})
        clock[0] = 10
        with mock.patch.object(jobs, "default_store", return_value=store):
            result = pull_worker.pull_status(job_id)
        self.assertFalse(result["ok"])
        self.assertIn("expired", result["error"]["message"])


class TestUnknownJobId(AsyncPullCase):
    def test_status_and_fetch_handle_ids_that_do_not_exist(self):
        for bad in ("0" * 32, "", "not-a-job", "../../etc/passwd", "A" * 32, None):
            with self.subTest(job_id=bad):
                for call in (pull_worker.pull_status, pull_worker.pull_fetch):
                    result = call(bad)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"]["system"], "config")
                    self.assertTrue(result["error"]["remedy"])

    def test_an_id_is_never_joined_onto_a_path_unvalidated(self):
        with self.assertRaises(ValueError):
            self.store.job_dir("../x")

    def test_a_job_whose_worker_is_gone_is_reported_failed(self):
        job_id = self.store.create({})
        self.store.update(job_id, owner="a-previous-server-process",
                          state=jobs.RUNNING)
        self.assertEqual(self.store.get(job_id)["state"], "running")
        status = pull_worker.pull_status(job_id)
        self.assertEqual(status["data"]["state"], "failed")
        self.assertIn("worker", status["data"]["error"]["message"])


class TestValidationMatchesTheSynchronousTool(AsyncPullCase):
    def test_bad_backend_is_refused_with_the_same_error(self):
        sync = roundtrip.pull_project_doc("Brief", "101_Demo", True, True, True,
                                          "nonsense", None, True, "analysis")
        started = pull_worker.start_pull("Brief", "101_Demo", True, True, True,
                                         "nonsense", None, "analysis", False)
        self.assertFalse(started["ok"])
        self.assertEqual(started["error"], sync["error"])

    def test_bad_profile_is_refused_with_the_same_error(self):
        sync = roundtrip.pull_project_doc("Brief", "101_Demo", True, True, True,
                                          None, None, True, "nonsense")
        started = pull_worker.start_pull("Brief", "101_Demo", True, True, True,
                                         None, None, "nonsense", False)
        self.assertEqual(started["error"], sync["error"])

    def test_bad_project_is_refused(self):
        started = pull_worker.start_pull("Brief", "a/b", True, True, True,
                                         None, None, "analysis", False)
        self.assertFalse(started["ok"])
        self.assertEqual(started["error"]["system"], "config")
        self.assertEqual(list(self.store.root.glob("*")) if self.store.root.exists()
                         else [], [], "a refused start must not leave a job")

    def test_bad_page_selection_is_refused_before_a_job_exists(self):
        started = pull_worker.start_pull("Brief", "101_Demo", True, True, True,
                                         None, "1-x", "analysis", False)
        self.assertFalse(started["ok"])


class TestScopeAndSurface(unittest.TestCase):
    TOOLS = ("rm_pull_project_start", "rm_pull_project_status",
             "rm_pull_project_fetch")

    def test_authz_scope_matches_rm_pull_project(self):
        for tool in self.TOOLS:
            with self.subTest(tool=tool):
                self.assertEqual(authz.TOOL_SCOPES[tool],
                                 authz.TOOL_SCOPES["rm_pull_project"])

    def test_surface_membership_matches_rm_pull_project(self):
        for tool in self.TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(tool, surface.CORE_TOOLS)
                self.assertNotIn(tool, surface.REMOTE_TOOLS)
        self.assertNotIn("rm_pull_project", surface.REMOTE_TOOLS)
        self.assertIn("rm_pull_project", surface.CORE_TOOLS)

    def test_only_the_start_is_charged_to_the_heavy_bucket(self):
        self.assertIn("rm_pull_project_start", authz.HEAVY_TOOLS)
        self.assertNotIn("rm_pull_project_status", authz.HEAVY_TOOLS)
        self.assertNotIn("rm_pull_project_fetch", authz.HEAVY_TOOLS)

    def test_the_tools_are_registered_on_the_server(self):
        from rm_mcp import server
        for tool in self.TOOLS:
            self.assertIn(tool, server.mcp.registered)
        authz.verify_tools(server.mcp.registered)    # must not raise


class TestThrottledDeviceGet(AsyncPullCase):
    """A 429 on the device get is a wait, not a retry (cloud-throttle rule)."""

    def test_a_throttled_get_fails_the_job_with_the_throttled_remedy(self):
        from rm_mcp.envelope import REMEDIES, RmapiThrottledError

        until = time.time() + 300
        exc = RmapiThrottledError("cloud throttled (429)", until)

        with mock.patch.object(roundtrip, "canonical_project_dir",
                               return_value=("/101_Demo", [])), \
                mock.patch.object(roundtrip.device, "get", side_effect=exc):
            job_id = pull_worker.start_pull(
                "Brief", "101_Demo", True, True, True, None, None, "analysis",
                False)["data"]["job_id"]
            record = self.wait(job_id)

        self.assertEqual(record["state"], "failed")
        error = record["error"]
        self.assertEqual(error["system"], "cloud")
        self.assertEqual(error["remedy"], REMEDIES["rmapi_throttled"])
        self.assertEqual(error["data"]["throttled_until"], exc.until_iso)
        self.assertIn("retry_after_s", error["data"])
        status = pull_worker.pull_status(job_id)["data"]
        self.assertEqual(status["error"]["data"]["throttled_until"],
                         exc.until_iso)


class TestRealBackendWiring(AsyncPullCase):
    """The production backend, against the fakes test_pull_project_loop uses."""

    def test_one_render_subprocess_per_page_and_the_bundle_is_dropped(self):
        from test_pull_project_loop import FakeScripts, _bundle_zip

        bundle = _bundle_zip(self.tmp, with_pdf=False)
        fakes = FakeScripts(flatten_rc=127, ink=True)

        def fake_get(device_path: str, dest: Path) -> Path:
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / bundle.name
            shutil.copy2(bundle, target)
            return target

        with mock.patch.object(roundtrip, "canonical_project_dir",
                               return_value=("/101_Demo", [])), \
                mock.patch.object(roundtrip.device, "get", side_effect=fake_get), \
                mock.patch.object(roundtrip, "run_script", fakes), \
                mock.patch.dict(os.environ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            job_id = pull_worker.start_pull(
                "Brief", "101_Demo", True, True, True, None, None, "analysis",
                False)["data"]["job_id"]
            record = self.wait(job_id)

        self.assertEqual(record["state"], "done", record)
        renders = [c for c in fakes.calls if c == "rm_render_page.py"]
        self.assertEqual(len(renders), 1, "one page, one render subprocess")
        self.assertFalse((self.store.work_dir(job_id) / "download").exists(),
                         "the downloaded bundle is dropped after extraction")
        fetched = pull_worker.pull_fetch(job_id)["data"]
        self.assertEqual([r["page"] for r in fetched["results"]], [1])
        self.assertEqual(fetched["document"]["document_kind"], "notebook")
        self.assertIn("typed_text_path", fetched["document"])
        self.assertTrue(fetched["document"]["agent_reads_pages"])


if __name__ == "__main__":
    unittest.main()
