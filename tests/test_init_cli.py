"""run_server.py --init: the non-interactive walk an agent runs.

Added 2026-09-10. The device never enters these tests: the rmapi
probe is replaced by a fake, so what is checked is what --init WRITES -- the
merged .env, the routing note, the registration with absolute paths -- and
that re-running does not clobber a hand-edited file.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))

from rm_mcp import init_cli  # noqa: E402


class InitCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rm_init_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = self.tmp / "tools" / ".env"
        self.routing = self.tmp / "ROUTING.md"
        patches = [
            mock.patch.object(init_cli, "env_path", lambda: self.env),
            mock.patch.object(init_cli, "routing_path", lambda: self.routing),
            mock.patch.object(init_cli, "_rmapi_status",
                              lambda rmapi: ("C:/bin/rmapi.exe", True, ["Notes", "Thesis"])),
            mock.patch.object(init_cli.config, "RM_MCP_DIR", self.tmp),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_init(self, *argv: str) -> str:
        from io import StringIO
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = init_cli.run(["--yes", *argv])
        self.assertEqual(rc, 0)
        return buf.getvalue()


class TestNonInteractive(InitCase):
    def test_defaults_write_env_at_the_device_root(self):
        out = self.run_init()
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("RM_MCP_PROJECTS_ROOT=/\n", text)
        self.assertIn("RM_ROOT=/\n", text)
        self.assertIn("RMAPI_BIN=C:/bin/rmapi.exe\n", text)
        self.assertNotIn("RM_MCP_MANAGED_ROOTS", text)
        self.assertIn("WHOLE DEVICE", out)
        self.assertFalse(self.routing.is_file(), "no routing answers, no routing file")

    def test_flags_land_in_env_and_routing(self):
        out = self.run_init("--projects-root", "Work", "--managed-roots", "/Work, /Notes",
                            "--notes-to", "the Obsidian vault at ~/notes/inbox",
                            "--directives-to", "GitHub issues on my/repo")
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("RM_MCP_PROJECTS_ROOT=/Work\n", text)
        self.assertIn("RM_MCP_MANAGED_ROOTS=/Work,/Notes\n", text)
        routing = self.routing.read_text(encoding="utf-8")
        self.assertIn("notes_to: the Obsidian vault at ~/notes/inbox", routing)
        self.assertIn("directives_to: GitHub issues on my/repo", routing)
        self.assertIn("rm_pull_project", routing)
        self.assertIn(str(self.routing), out)

    def test_registration_uses_this_interpreter_and_absolute_launcher(self):
        out = self.run_init()
        self.assertIn(sys.executable, out)
        self.assertIn(str(self.tmp / "run_server.py"), out)
        self.assertIn("claude mcp add rm --", out)
        cli, block = init_cli.registration(sys.executable, self.tmp / "run_server.py")
        parsed = json.loads(block)
        self.assertEqual(parsed["rm"]["command"], sys.executable)
        self.assertEqual(parsed["rm"]["type"], "stdio")

    def test_rerun_merges_and_keeps_hand_edits(self):
        self.env.parent.mkdir(parents=True)
        self.env.write_text("# mine\nGEMINI_API_KEY=keep-me\nRM_ROOT=/Old\n", encoding="utf-8")
        self.run_init("--projects-root", "/New")
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("# mine\n", text)
        self.assertIn("GEMINI_API_KEY=keep-me\n", text)
        self.assertIn("RM_ROOT=/New\n", text)
        self.assertEqual(text.count("RM_ROOT="), 1, "replaced in place, not appended twice")
        self.assertEqual(text.count("RM_MCP_PROJECTS_ROOT="), 1)

    def test_unpaired_rmapi_is_reported_not_hidden(self):
        with mock.patch.object(init_cli, "_rmapi_status",
                               lambda rmapi: ("C:/bin/rmapi.exe", False, [])):
            out = self.run_init("--no-pair")
        self.assertIn("not answer as paired", out)

    def test_missing_rmapi_prints_the_build_hint(self):
        with mock.patch.object(init_cli, "_rmapi_status", lambda rmapi: ("", None, [])):
            out = self.run_init()
        self.assertIn("go install github.com/ddvk/rmapi", out)


class TestWindowsPathRefused(InitCase):
    """Git Bash rewrites `/x` into `C:/Program Files/Git/x` before Python sees it."""

    def run_refused(self, *argv: str) -> str:
        from io import StringIO
        out, err = StringIO(), StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = init_cli.run(["--yes", *argv])
        self.assertNotEqual(rc, 0)
        self.assertFalse(self.env.is_file(), "a refused answer must not reach .env")
        message = out.getvalue() + err.getvalue()
        self.assertIn("MSYS_NO_PATHCONV=1", message)
        self.assertIn("Windows path", message)
        return message

    def test_projects_root_with_drive_is_refused(self):
        self.run_refused("--projects-root", "C:/Program Files/Git/rm-mcp-fft")

    def test_projects_root_with_leading_slash_drive_is_refused(self):
        self.run_refused("--projects-root", "/C:/Program Files/Git/x")

    def test_projects_root_with_backslash_is_refused(self):
        self.run_refused("--projects-root", "\\Work\\Sub")

    def test_managed_root_with_drive_is_refused(self):
        self.run_refused("--managed-roots", "/Work,C:/Program Files/Git/rm-mcp-fft")

    def test_managed_root_with_backslash_is_refused(self):
        self.run_refused("--managed-roots", "\\Work")

    def test_normal_roots_still_work(self):
        self.run_init("--projects-root", "/Projects")
        self.assertIn("RM_MCP_PROJECTS_ROOT=/Projects\n", self.env.read_text(encoding="utf-8"))
        self.env.unlink()
        self.run_init("--projects-root", "/")
        self.assertIn("RM_MCP_PROJECTS_ROOT=/\n", self.env.read_text(encoding="utf-8"))

    def test_managed_roots_without_leading_slash_are_normalised(self):
        self.run_init("--managed-roots", "Reading, /Notes")
        self.assertIn("RM_MCP_MANAGED_ROOTS=/Reading,/Notes\n",
                      self.env.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
