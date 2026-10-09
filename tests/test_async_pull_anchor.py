"""The asynchronous pull surfaces anchor_unresolved, like rm_render does.

rm_render_page prints `ANCHOR-UNRESOLVED <n>` for a page whose text-anchored ink
could not be placed. roundtrip.anchor_warnings turns that into an envelope
warning for rm_render / rm_page_image; the async pull used to drop it. Offline:
the render subprocess is mocked, no device, no rmapi.

Run: python -m pytest tests/test_async_pull_anchor.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from rm_mcp import pull_worker, roundtrip  # noqa: E402
from test_async_pull import AsyncPullCase  # noqa: E402
from test_pull_project_loop import FakeScripts, _bundle_zip  # noqa: E402


class MarkerScripts(FakeScripts):
    """FakeScripts whose renderer prints an ANCHOR-UNRESOLVED marker."""

    def __init__(self, marker: str = "", **kw) -> None:
        super().__init__(**kw)
        self.marker = marker

    def __call__(self, script, args, tool, timeout=None):
        proc = super().__call__(script, args, tool, timeout)
        if script == "rm_render_page.py" and self.marker:
            return subprocess.CompletedProcess(
                args=proc.args, returncode=0, stdout=self.marker, stderr="")
        return proc


class TestAnchorWarningFromAsyncPull(AsyncPullCase):
    def run_job(self, marker: str) -> tuple[dict, str]:
        bundle = _bundle_zip(self.tmp, with_pdf=False)
        fakes = MarkerScripts(marker, flatten_rc=127, ink=True)

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
        return record, job_id

    @staticmethod
    def anchors(envelope: dict) -> list[dict]:
        return [w for w in envelope.get("warnings", [])
                if w.get("code") == "anchor_unresolved"]

    def test_marker_becomes_one_warning_on_status_and_fetch(self):
        _, job_id = self.run_job("p1.png  [anchors placed=1 unresolved=3]  "
                                 "[ANCHOR-UNRESOLVED 3]\n")
        for env in (pull_worker.pull_status(job_id),
                    pull_worker.pull_fetch(job_id)):
            found = self.anchors(env)
            self.assertEqual(len(found), 1, env.get("warnings"))
            self.assertEqual(found[0]["data"]["unresolved"], 3)
            self.assertTrue(found[0]["possible_data_loss"])

    def test_same_shape_as_the_synchronous_warning(self):
        _, job_id = self.run_job("[ANCHOR-UNRESOLVED 2]\n")
        got = self.anchors(pull_worker.pull_status(job_id))[0]
        self.assertEqual(got, roundtrip.anchor_warnings("[ANCHOR-UNRESOLVED 2]")[0])

    def test_no_marker_means_no_warning(self):
        _, job_id = self.run_job("")
        self.assertEqual(self.anchors(pull_worker.pull_status(job_id)), [])

    def test_zero_count_means_no_warning(self):
        _, job_id = self.run_job("[ANCHOR-UNRESOLVED 0]\n")
        self.assertEqual(self.anchors(pull_worker.pull_status(job_id)), [])


if __name__ == "__main__":
    unittest.main()
