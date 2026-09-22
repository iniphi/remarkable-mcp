"""Offline unit tests for rm-mcp -- no device, no network, no heavy deps.

Run: python -m pytest tests/test_offline.py
(or python -m unittest discovery from the rm-mcp dir).
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))

from rm_mcp import config, envelope, runner  # noqa: E402


class TestResolveProject(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(config.resolve_project("101_Overseer"), "101_Overseer")

    def test_explicit_strips_slashes(self):
        self.assertEqual(config.resolve_project("/104_stacks/"), "104_stacks")

    def test_rejects_path_traversal(self):
        for bad in ("101/evil", "..", "101_a/../b", "a\\b"):
            with self.assertRaises(ValueError):
                config.resolve_project(bad)

    def test_rejects_non_code(self):
        """NNN_name is the desk's shape (tools/.env). The public default accepts
        any folder name since 2026-09-10, so there the same inputs resolve."""
        for name in ("Overseer", "1_x", "abcd_x"):
            if config._IS_NNN_PATTERN:
                with self.assertRaises(ValueError):
                    config.resolve_project(name)
            else:
                self.assertEqual(config.resolve_project(name), name)

    def test_env_fallback(self):
        import os
        old = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = r"C:\projects\203_lightroom"
        try:
            self.assertEqual(config.resolve_project(None), "203_lightroom")
        finally:
            if old is None:
                del os.environ["CLAUDE_PROJECT_DIR"]
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = old

    def test_device_dir(self):
        root = config.PROJECTS_DEVICE_ROOT.rstrip("/")   # "" when the root is "/"
        self.assertEqual(config.project_device_dir("101_Overseer"),
                         f"{root}/101_Overseer")


class TestSafeFilenameStem(unittest.TestCase):
    def test_illegal_chars(self):
        self.assertEqual(config.safe_filename_stem('a/b:c*d?"e<f>g|h'),
                         "a b c d e f g h")

    def test_empty(self):
        self.assertEqual(config.safe_filename_stem(""), "untitled")
        self.assertEqual(config.safe_filename_stem("  .,- "), "untitled")

    def test_truncation(self):
        stem = config.safe_filename_stem("x" * 200)
        self.assertLessEqual(len(stem), config.FILENAME_STEM_MAX)


class TestEnvelope(unittest.TestCase):
    @staticmethod
    def _proc(rc: int, stdout: str = "", stderr: str = ""):
        return subprocess.CompletedProcess(args=["x"], returncode=rc,
                                           stdout=stdout, stderr=stderr)

    def test_rc0_is_none(self):
        self.assertIsNone(envelope.classify(self._proc(0), "t"))

    def test_zotero_keys(self):
        err = envelope.classify(
            self._proc(2, stderr="ZOTERO_API_KEY / ZOTERO_USER_ID missing"), "t")
        self.assertEqual(err["system"], "zotero")

    def test_auth_hint(self):
        err = envelope.classify(
            self._proc(1, stderr="please enter the one-time code"), "t")
        self.assertEqual(err["system"], "rmapi")
        self.assertIn("paired", err["remedy"])
        self.assertIn("never re-auths", err["remedy"])

    def test_vision_key(self):
        err = envelope.classify(
            self._proc(2, stderr="ANTHROPIC_API_KEY missing from .env"), "t")
        self.assertEqual(err["system"], "vision")

    def test_ok_and_err_shapes(self):
        ok = envelope.ok_result({"a": 1})
        self.assertTrue(ok["ok"])
        self.assertIsNone(ok["error"])
        err = envelope.err_result("cloud", "m", "r")
        self.assertFalse(err["ok"])
        self.assertEqual(err["error"]["system"], "cloud")


class TestRunnerParsers(unittest.TestCase):
    def test_push_totals(self):
        line = ("=== Total: 3 pushed, 1 renamed, 2 unchanged, "
                "0 failed, 1 without a PDF ===")
        self.assertEqual(runner.parse_push_totals(f"noise\n{line}\n"),
                         {"pushed": 3, "renamed": 1, "unchanged": 2,
                          "failed": 0, "without_pdf": 1})

    def test_push_totals_absent(self):
        self.assertIsNone(runner.parse_push_totals("no totals here"))

    def test_pull_done(self):
        self.assertEqual(runner.parse_pull_done("...\nDone: 2 pulled, 1 failed\n"),
                         {"pulled": 2, "failed": 1})


class TestDeviceLsParse(unittest.TestCase):
    def test_tab_split(self):
        # Mirrors rmapi ls output: "[d]\tname" / "[f]\tname"
        from rm_mcp import device
        sample = "[d]\tReading\n[f]\tSome Paper Title\n\nnoise-without-tab\n"
        entries = []
        for line in sample.splitlines():
            line = line.rstrip()
            if not line or "\t" not in line:
                continue
            kind, name = line.split("\t", 1)
            entries.append({"name": name.strip(),
                            "type": "folder" if kind.strip("[]") == "d" else "doc"})
        self.assertEqual(entries, [{"name": "Reading", "type": "folder"},
                                   {"name": "Some Paper Title", "type": "doc"}])
        self.assertTrue(callable(device.ls))


if __name__ == "__main__":
    unittest.main()
