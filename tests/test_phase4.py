"""Offline unit tests for the rm-mcp v2 Phase 4 features.

Covers V2-2 (zotero name->key resolution), V2-8 (canonical_child case
canonicalization), V2-3 (rm_move/rm_delete plans + refusals), V2-4
(rm_push_dir planning + per-file execution), V2-11 (push_local_file state
recording + state_record_failed), and session retention (prune_sessions).
No device, no network, no Zotero keys required.

Run: python -m pytest tests/test_phase4.py
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))
TOOLS_DIR = RM_MCP_DIR.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import rm_config  # noqa: E402
try:
    import rm_state_remote  # noqa: E402
except ImportError:  # pragma: no cover -- the public tree has no cloud lane
    # Only used below to force the shared-ledger lane OFF for the duration of
    # a test. Where the module is absent the lane cannot be on in the first
    # place, so None and "already local-only" are the same state.
    rm_state_remote = None
from rm_mcp import config, device, manage, roundtrip  # noqa: E402

# Derive the cross-project device root from config so these tests survive a
# device-side rename (e.g. /Projects -> /00_Projects on 2026-07-03).
# PROOT is a PREFIX for building paths ("" when the root is "/", so that
# f"{PROOT}/x" never doubles the slash); PROOT_DIR is the root itself, the
# path the code lists.
PROOT_DIR = config.PROJECTS_DEVICE_ROOT
PROOT = PROOT_DIR.rstrip("/")


class TestCanonicalChild(unittest.TestCase):
    def test_exact_match_wins(self):
        with mock.patch.object(device, "ls", return_value=[
                {"name": "200_Poetics", "type": "folder"},
                {"name": "200_poetics", "type": "folder"}]):
            name, warnings = device.canonical_child("/Projects", "200_Poetics")
        self.assertEqual((name, warnings), ("200_Poetics", []))

    def test_unique_ci_match_uses_device_casing(self):
        with mock.patch.object(device, "ls", return_value=[
                {"name": "999_rmmcp_test", "type": "folder"}]):
            name, warnings = device.canonical_child("/Projects",
                                                    "999_RMMCP_TEST")
        self.assertEqual(name, "999_rmmcp_test")
        self.assertEqual(warnings[0]["code"], "project_case_matched")

    def test_no_match_passes_through(self):
        with mock.patch.object(device, "ls", return_value=[]):
            name, warnings = device.canonical_child("/Projects", "300_new")
        self.assertEqual((name, warnings), ("300_new", []))

    def test_unlistable_parent_falls_through(self):
        with mock.patch.object(device, "ls",
                               side_effect=RuntimeError("no such dir")):
            name, warnings = device.canonical_child("/Projects", "300_new")
        self.assertEqual((name, warnings), ("300_new", []))

    def test_want_type_cross_type_collision_warns(self):
        # A hand-made NOTEBOOK (doc) collides with the folder we want to ensure:
        # keep the requested folder name but flag the collision + remedy.
        with mock.patch.object(device, "ls", return_value=[
                {"name": "200_poetics", "type": "doc"}]):
            name, warnings = device.canonical_child(
                "/Projects", "200_Poetics", want_type="folder")
        self.assertEqual(name, "200_Poetics")
        self.assertEqual(warnings[0]["code"], "project_notebook_collision")
        self.assertEqual(warnings[0]["data"]["collides_type"], "doc")

    def test_want_type_same_type_ci_match_still_reuses_casing(self):
        with mock.patch.object(device, "ls", return_value=[
                {"name": "200_poetics", "type": "folder"}]):
            name, warnings = device.canonical_child(
                "/Projects", "200_Poetics", want_type="folder")
        self.assertEqual(name, "200_poetics")
        self.assertEqual(warnings[0]["code"], "project_case_matched")


class TestMoveDelete(unittest.TestCase):
    def test_move_dry_run_returns_plan(self):
        result = manage.move_impl(f"{PROOT}/999_t/a", f"{PROOT}/999_t/b",
                                  dry_run=True, allow_anywhere=False)
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["dry_run"])
        self.assertEqual(result["data"]["plan"]["action"], "move")

    def test_move_refuses_outside_roots(self):
        # A fenced deployment; the public default (whole device) fences nothing.
        with mock.patch.object(manage, "MANAGED_ROOTS", ("/Projects",)):
            result = manage.move_impl("/Business Time/x", "/Projects/999_t/x",
                                      dry_run=True, allow_anywhere=False)
        self.assertFalse(result["ok"])
        self.assertIn("allow_anywhere", result["error"]["remedy"])

    def test_move_allow_anywhere_overrides(self):
        result = manage.move_impl("/Business Time/x", "/Business Time/y",
                                  dry_run=True, allow_anywhere=True)
        self.assertTrue(result["ok"])

    def test_move_executes(self):
        with mock.patch.object(device, "mv") as mv:
            result = manage.move_impl(f"{PROOT}/999_t/a", f"{PROOT}/999_t/b",
                                      dry_run=False, allow_anywhere=False)
        mv.assert_called_once_with(f"{PROOT}/999_t/a", f"{PROOT}/999_t/b")
        self.assertTrue(result["data"]["moved"])

    def test_delete_refuses_non_empty_folder(self):
        def fake_ls(path, timeout=30):
            if path == PROOT_DIR:
                return [{"name": "999_t", "type": "folder"}]
            return [{"name": "child", "type": "doc"}]

        with mock.patch.object(device, "ls", side_effect=fake_ls):
            result = manage.delete_impl(f"{PROOT}/999_t", dry_run=False,
                                        allow_anywhere=False)
        self.assertFalse(result["ok"])
        self.assertIn("never recurses", result["error"]["remedy"])

    def test_delete_refuses_when_contents_cannot_be_listed(self):
        # A folder whose contents could not be read is not an empty folder.
        # Until 2026-10-01 a failed child listing set children = [] and the
        # delete went ahead on a folder nobody had looked inside.
        failures = (RuntimeError("rmapi ls failed: connection reset by peer"),
                    subprocess.TimeoutExpired(cmd="rmapi", timeout=30))
        for failure, dry_run in [(f, d) for f in failures for d in (True, False)]:
            with self.subTest(failure=type(failure).__name__, dry_run=dry_run):
                def fake_ls(path, timeout=30, failure=failure):
                    if path == PROOT_DIR:
                        return [{"name": "999_t", "type": "folder"}]
                    raise failure

                with mock.patch.object(device, "ls", side_effect=fake_ls), \
                        mock.patch.object(device, "rm") as rm:
                    result = manage.delete_impl(f"{PROOT}/999_t",
                                                dry_run=dry_run,
                                                allow_anywhere=False)
                rm.assert_not_called()
                self.assertFalse(result["ok"])
                self.assertIn("could not be listed", result["error"]["message"])
                self.assertNotIn("plan", result["data"])

    def test_delete_dry_run_classifies_doc(self):
        # rmapi ls on a DOC path succeeds and lists the doc itself
        # (2026-07-03 live-smoke finding) -- classification must come from
        # the parent listing's entry type, never from listing the target.
        def fake_ls(path, timeout=30):
            if path == f"{PROOT}/999_t":
                return [{"name": "doc", "type": "doc"}]
            self.fail(f"unexpected ls of {path}")

        with mock.patch.object(device, "ls", side_effect=fake_ls):
            result = manage.delete_impl(f"{PROOT}/999_t/doc", dry_run=True,
                                        allow_anywhere=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["plan"]["kind"], "doc")

    def test_delete_missing_target_errors(self):
        with mock.patch.object(device, "ls", return_value=[]):
            result = manage.delete_impl(f"{PROOT}/999_t/ghost", dry_run=True,
                                        allow_anywhere=False)
        self.assertFalse(result["ok"])
        self.assertIn("does not exist", result["error"]["message"])

    def test_delete_empty_folder_executes(self):
        def fake_ls(path, timeout=30):
            if path == PROOT_DIR:
                return [{"name": "999_t", "type": "folder"}]
            return []

        with mock.patch.object(device, "ls", side_effect=fake_ls), \
                mock.patch.object(device, "rm") as rm:
            result = manage.delete_impl(f"{PROOT}/999_t", dry_run=False,
                                        allow_anywhere=False)
        rm.assert_called_once_with(f"{PROOT}/999_t")
        self.assertTrue(result["data"]["deleted"])
        self.assertEqual(result["data"]["kind"], "empty_folder")


class StatePathsMixin(unittest.TestCase):
    """Redirect rm_config's state-file globals into a temp dir per test.

    Second copy of the mixin in tests/test_substrate.py -- see the long note
    there for why RM_STATE_BUCKET must be unset too: without it, load_state()
    reads the SHARED GCS ledger and update_state() writes to it, so these
    tests mutate the ledger the desk and Cloud Run both plan against. This
    class is the more dangerous of the two, because TestPushLocalFile
    exercises the 'pushed'/'projects_pushed' namespaces that rm_pull's
    dedupe planner walks.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rmstate_p4_")
        tmp = Path(self._tmp.name)
        self._saved = {
            name: getattr(rm_config, name)
            for name in ("STATE_FILE", "STATE_BAK", "STATE_TMP",
                         "STATE_LOCK_FILE")
        }
        rm_config.STATE_FILE = tmp / ".rm_state.json"
        rm_config.STATE_BAK = tmp / ".rm_state.json.bak"
        rm_config.STATE_TMP = tmp / ".rm_state.json.tmp"
        rm_config.STATE_LOCK_FILE = tmp / ".rm_state.lock"
        self._bucket_env = (rm_state_remote.ENV_BUCKET
                            if rm_state_remote is not None else None)
        if self._bucket_env is not None:
            self._saved_bucket = os.environ.get(self._bucket_env)
            os.environ[self._bucket_env] = ""

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(rm_config, name, value)
        if self._bucket_env is not None:
            if self._saved_bucket is None:
                os.environ.pop(self._bucket_env, None)
            else:
                os.environ[self._bucket_env] = self._saved_bucket
        self._tmp.cleanup()


class TestPushLocalFile(StatePathsMixin):
    def _push(self, tmp: Path, record_side_effect=None):
        pdf = tmp / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                device, "ls", return_value=[
                    {"name": "999_rmmcp_test", "type": "folder"}]))
            stack.enter_context(mock.patch.object(device, "mkdir_p"))
            stack.enter_context(mock.patch.object(device, "put"))
            if record_side_effect is not None:
                stack.enter_context(mock.patch.object(
                    roundtrip.rm_config, "record_project_push",
                    side_effect=record_side_effect))
            return roundtrip.push_local_file(pdf, "999_RMMCP_TEST", None)

    def test_push_records_projects_pushed(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            result = self._push(Path(tmp_str))
        self.assertTrue(result["ok"])
        # V2-8: device casing won.
        self.assertEqual(result["data"]["device_dir"],
                         f"{PROOT}/999_rmmcp_test")
        self.assertIn("project_case_matched",
                      [w["code"] for w in result["warnings"]])
        # V2-11: recorded under projects_pushed, never pushed.
        state = rm_config.load_state()
        entry = state["projects_pushed"][f"{PROOT}/999_rmmcp_test/paper"]
        self.assertEqual(entry["via"], "rm-mcp")
        self.assertEqual(entry["project"], "999_rmmcp_test")
        self.assertNotIn(f"{PROOT}/999_rmmcp_test/paper",
                         state.get("pushed", {}))

    def test_state_failure_still_ok_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            result = self._push(Path(tmp_str),
                                record_side_effect=OSError("disk full"))
        self.assertTrue(result["ok"])
        codes = [w["code"] for w in result["warnings"]]
        self.assertIn("state_record_failed", codes)


class TestPushDir(unittest.TestCase):
    def test_dry_run_plans_and_skips(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            for name in ("b.pdf", "a.pdf", "c.epub", "notes.txt"):
                (tmp / name).write_bytes(b"x")
            result = manage.push_dir_impl(str(tmp), "999_rmmcp_test",
                                          glob="*", limit=None, dry_run=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["counts"]["planned"], 3)
        self.assertEqual(result["data"]["counts"]["skipped"], 1)
        sources = [Path(i["source"]).name for i in result["data"]["items"]]
        self.assertEqual(sources, ["a.pdf", "b.pdf", "c.epub"])
        self.assertTrue(all(i["status"] == "would_push"
                            for i in result["data"]["items"]))

    def test_limit_applies(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            for name in ("a.pdf", "b.pdf", "c.pdf"):
                (tmp / name).write_bytes(b"x")
            result = manage.push_dir_impl(str(tmp), "999_rmmcp_test",
                                          glob="*.pdf", limit=2, dry_run=True)
        self.assertEqual(result["data"]["counts"]["planned"], 2)

    def test_execution_merges_results(self):
        def fake_push(local, project, title):
            if local.name == "bad.pdf":
                return {"ok": False, "data": {}, "warnings": [],
                        "error": {"system": "cloud", "message": "boom",
                                  "remedy": "r"}, "log_tail": []}
            return {"ok": True,
                    "data": {"device_path": f"/Projects/999_t/{local.stem}"},
                    "warnings": [{"code": "project_case_matched",
                                  "message": "m", "possible_data_loss": False,
                                  "data": {}}],
                    "error": None, "log_tail": []}

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            for name in ("good.pdf", "bad.pdf"):
                (tmp / name).write_bytes(b"x")
            with mock.patch.object(roundtrip, "push_local_file",
                                   side_effect=fake_push):
                result = manage.push_dir_impl(str(tmp), "999_t", glob="*.pdf",
                                              limit=None, dry_run=False)
        self.assertTrue(result["ok"])  # skip-and-continue: partial ok
        self.assertEqual(result["data"]["counts"],
                         {"pushed": 1, "failed": 1, "planned": 2})
        # Duplicate warnings are merged.
        self.assertEqual(len(result["warnings"]), 1)

    def test_all_failed_is_error(self):
        def fail_push(local, project, title):
            return {"ok": False, "data": {}, "warnings": [],
                    "error": {"system": "cloud", "message": "boom",
                              "remedy": "r"}, "log_tail": []}

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            (tmp / "a.pdf").write_bytes(b"x")
            with mock.patch.object(roundtrip, "push_local_file",
                                   side_effect=fail_push):
                result = manage.push_dir_impl(str(tmp), "999_t", glob="*.pdf",
                                              limit=None, dry_run=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["counts"]["failed"], 1)

    def test_missing_dir_is_config_error(self):
        result = manage.push_dir_impl(r"C:\does\not\exist", "999_t",
                                      glob="*.pdf", limit=None, dry_run=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["system"], "config")


class TestPruneSessions(unittest.TestCase):
    def _make_dir(self, root: Path, stamp: str) -> Path:
        d = root / f"{stamp}-1234"
        d.mkdir()
        (d / "marker.txt").write_text("x", encoding="utf-8")
        return d

    def test_prunes_old_beyond_keep_last(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            root = Path(tmp_str)
            old = [self._make_dir(
                root, time.strftime("%Y%m%d-%H%M%S",
                                    time.localtime(time.time()
                                                   - (30 + i) * 86400)))
                for i in range(3)]
            fresh = self._make_dir(root, time.strftime("%Y%m%d-%H%M%S"))
            deleted = config.prune_sessions(root, keep_days=14, keep_last=2)
        # Newest 2 kept (fresh + newest old); the 2 oldest are old enough.
        self.assertEqual(len(deleted), 2)
        self.assertNotIn(str(fresh), deleted)
        self.assertIn(str(old[-1]), deleted)

    def test_keep_last_protects_old_dirs(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            root = Path(tmp_str)
            self._make_dir(
                root, time.strftime("%Y%m%d-%H%M%S",
                                    time.localtime(time.time()
                                                   - 100 * 86400)))
            deleted = config.prune_sessions(root, keep_days=14, keep_last=20)
        self.assertEqual(deleted, [])

    def test_young_dirs_survive_beyond_keep_last(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            root = Path(tmp_str)
            for i in range(4):
                self._make_dir(
                    root, time.strftime("%Y%m%d-%H%M%S",
                                        time.localtime(time.time()
                                                       - i * 3600)))
            deleted = config.prune_sessions(root, keep_days=14, keep_last=1)
        self.assertEqual(deleted, [])

    def test_opt_out_env(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            root = Path(tmp_str)
            self._make_dir(
                root, time.strftime("%Y%m%d-%H%M%S",
                                    time.localtime(time.time()
                                                   - 100 * 86400)))
            with mock.patch.dict(os.environ,
                                 {"RM_MCP_KEEP_SESSIONS": "all"}):
                deleted = config.prune_sessions(root, keep_days=0,
                                                keep_last=0)
        self.assertEqual(deleted, [])

    def test_never_deletes_current_session_dir(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            root = Path(tmp_str)
            stamp = time.strftime(
                "%Y%m%d-%H%M%S",
                time.localtime(time.time() - 100 * 86400))
            current = self._make_dir(root, stamp)
            with mock.patch.object(config, "SESSION_DIR", current):
                deleted = config.prune_sessions(root, keep_days=0,
                                                keep_last=0)
            self.assertEqual(deleted, [])
            self.assertTrue(current.is_dir())


class TestBoundedOutputRetention(unittest.TestCase):
    """new_out_dir must evict per-call dirs beyond KEEP_OUTPUTS so a long-lived
    server does not pile rendered artifacts into RAM-backed storage across pulls
    (the per-session memory leak). See config._register_and_evict."""

    def setUp(self):
        self._saved_out_dirs = list(config._out_dirs)
        config._out_dirs.clear()

    def tearDown(self):
        config._out_dirs.clear()
        config._out_dirs.extend(self._saved_out_dirs)

    def test_evicts_oldest_beyond_keep(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            session = Path(tmp_str)
            with mock.patch.object(config, "SESSION_DIR", session), \
                    mock.patch.object(config, "KEEP_OUTPUTS", 2):
                made = [config.new_out_dir("rm_pull") for _ in range(5)]
                for d in made:
                    if d.exists():
                        (d / "page.png").write_text("x", encoding="utf-8")
            # Only the newest 2 survive on disk; the first 3 are gone.
            self.assertFalse(made[0].exists())
            self.assertFalse(made[2].exists())
            self.assertTrue(made[3].exists())
            self.assertTrue(made[4].exists())
            self.assertEqual(len(config._out_dirs), 2)

    def test_keep_one_leaves_only_current(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            session = Path(tmp_str)
            with mock.patch.object(config, "SESSION_DIR", session), \
                    mock.patch.object(config, "KEEP_OUTPUTS", 1):
                a = config.new_out_dir("rm_diff")
                b = config.new_out_dir("rm_diff")
            self.assertFalse(a.exists())
            self.assertTrue(b.exists())

    def test_survivors_stay_readable(self):
        # The just-returned dir (and the window before it) must remain usable --
        # rm_pull_project -> rm_interpret_page reads the same png_dir next turn.
        with tempfile.TemporaryDirectory() as tmp_str:
            session = Path(tmp_str)
            with mock.patch.object(config, "SESSION_DIR", session), \
                    mock.patch.object(config, "KEEP_OUTPUTS", 3):
                keep = config.new_out_dir("rm_pull_project")
                (keep / "page_001.png").write_text("ink", encoding="utf-8")
                for _ in range(2):
                    config.new_out_dir("rm_render")
            self.assertTrue((keep / "page_001.png").is_file())


class TestOutputGracePeriod(unittest.TestCase):
    """A dir younger than OUTPUT_GRACE_SECONDS is never evicted, however deep the
    queue. This is the fix for the concurrent-caller eviction race: SESSION_DIR is
    keyed per PROCESS, not per request, so without a grace window caller B's calls
    can rmtree the dir caller A was just handed. See config._register_and_evict."""

    def setUp(self):
        self._saved_out_dirs = list(config._out_dirs)
        self._saved_retained = config._grace_retained
        config._out_dirs.clear()
        config._grace_retained = 0

    def tearDown(self):
        config._out_dirs.clear()
        config._out_dirs.extend(self._saved_out_dirs)
        config._grace_retained = self._saved_retained

    def test_fresh_dirs_are_never_evicted(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            session = Path(tmp_str)
            with mock.patch.object(config, "SESSION_DIR", session), \
                    mock.patch.object(config, "KEEP_OUTPUTS", 1), \
                    mock.patch.object(config, "OUTPUT_GRACE_SECONDS", 300):
                made = [config.new_out_dir("rm_pull") for _ in range(3)]
            for d in made:
                self.assertTrue(d.exists(), f"{d} evicted inside its grace window")
            # Two are held ABOVE KEEP_OUTPUTS deliberately.
            self.assertEqual(config._grace_retained, 2)

    def test_concurrent_caller_dir_survives(self):
        # The actual regression. Caller A is handed a dir and reads it on a LATER
        # request; caller B hammers the same warm instance in between.
        with tempfile.TemporaryDirectory() as tmp_str:
            session = Path(tmp_str)
            with mock.patch.object(config, "SESSION_DIR", session), \
                    mock.patch.object(config, "KEEP_OUTPUTS", 2), \
                    mock.patch.object(config, "OUTPUT_GRACE_SECONDS", 300):
                caller_a = config.new_out_dir("rm_pull_project")
                (caller_a / "page_001.png").write_text("ink", encoding="utf-8")
                for _ in range(5):          # caller B, five calls
                    config.new_out_dir("rm_render")
            self.assertTrue(
                (caller_a / "page_001.png").is_file(),
                "caller A's returned dir was deleted by caller B's requests")

    def test_evicts_once_past_grace(self):
        # Same queue, but the oldest dirs have aged out of the window.
        with tempfile.TemporaryDirectory() as tmp_str:
            session = Path(tmp_str)
            now = 1_000_000.0
            made = []
            with mock.patch.object(config, "SESSION_DIR", session), \
                    mock.patch.object(config, "KEEP_OUTPUTS", 1), \
                    mock.patch.object(config, "OUTPUT_GRACE_SECONDS", 300):
                for i in range(3):
                    out = session / "rm_pull" / f"{i:04d}"
                    out.mkdir(parents=True, exist_ok=True)
                    made.append(out)
                    # Each call lands 200s after the last: by the third, the
                    # first is 400s old (evictable) and the second 200s (not).
                    config._register_and_evict(out, now=now + i * 200)
            self.assertFalse(made[0].exists(), "aged-out dir should be evicted")
            self.assertTrue(made[1].exists(), "dir still in grace must survive")
            self.assertTrue(made[2].exists())

    def test_grace_zero_keeps_legacy_fifo(self):
        # stdio default: one co-located caller, no race, reclaim space promptly.
        with tempfile.TemporaryDirectory() as tmp_str:
            session = Path(tmp_str)
            with mock.patch.object(config, "SESSION_DIR", session), \
                    mock.patch.object(config, "KEEP_OUTPUTS", 1), \
                    mock.patch.object(config, "OUTPUT_GRACE_SECONDS", 0):
                a = config.new_out_dir("rm_diff")
                b = config.new_out_dir("rm_diff")
            self.assertFalse(a.exists())
            self.assertTrue(b.exists())
            self.assertEqual(config._grace_retained, 0)

    def test_status_surfaces_retention_pressure(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            session = Path(tmp_str)
            with mock.patch.object(config, "SESSION_DIR", session), \
                    mock.patch.object(config, "KEEP_OUTPUTS", 1), \
                    mock.patch.object(config, "OUTPUT_GRACE_SECONDS", 300):
                for _ in range(3):
                    config.new_out_dir("rm_pull")
                status = config.output_retention_status()
            self.assertEqual(status["tracked"], 3)
            self.assertEqual(status["keep_outputs"], 1)
            self.assertEqual(status["grace_seconds"], 300)
            self.assertEqual(status["retained_in_grace"], 2)


if __name__ == "__main__":
    unittest.main()
