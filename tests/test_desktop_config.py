"""rm_mcp.desktop: the Claude desktop app registration helpers.

Added 2026-11-01 after the rm server failed to load in the desktop app
("Connection closed"): its config named the system Python, which has no
mcp 2.x. Everything here is offline: paths, platforms and environments are
injected, and no real %APPDATA% is touched.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))

from rm_mcp import desktop  # noqa: E402


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rm_desktop_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = self.tmp / "Claude" / "claude_desktop_config.json"
        self.launcher = self.tmp / "run_server.py"

    def write_cfg(self, data) -> None:
        self.cfg.parent.mkdir(parents=True, exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data)
        self.cfg.write_text(text, encoding="utf-8")

    def read_cfg(self) -> dict:
        return json.loads(self.cfg.read_text(encoding="utf-8"))


class TestPaths(unittest.TestCase):
    def test_windows_config(self):
        p = desktop.desktop_config_path("win32", {"APPDATA": "C:/Users/x/AppData/Roaming"},
                                        Path("/home/x"))
        self.assertEqual(p, Path("C:/Users/x/AppData/Roaming") / "Claude"
                         / "claude_desktop_config.json")

    def test_windows_without_appdata_falls_back_to_home(self):
        p = desktop.desktop_config_path("win32", {}, Path("/home/x"))
        self.assertEqual(p.name, "claude_desktop_config.json")
        self.assertIn("Claude", p.parts)

    def test_macos_config(self):
        p = desktop.desktop_config_path("darwin", {}, Path("/Users/x"))
        self.assertEqual(p, Path("/Users/x") / "Library" / "Application Support"
                         / "Claude" / "claude_desktop_config.json")

    def test_linux_config(self):
        p = desktop.desktop_config_path("linux", {}, Path("/home/x"))
        self.assertEqual(p, Path("/home/x") / ".config" / "Claude"
                         / "claude_desktop_config.json")

    def test_fatal_log_paths(self):
        w = desktop.fatal_log_path("win32", {"LOCALAPPDATA": "C:/L"}, Path("/h"))
        self.assertEqual(w, Path("C:/L") / "rm-mcp" / "rm-mcp-fatal.log")
        m = desktop.fatal_log_path("darwin", {}, Path("/Users/x"))
        self.assertEqual(m, Path("/Users/x") / "Library" / "Logs" / "rm-mcp"
                         / "rm-mcp-fatal.log")
        x = desktop.fatal_log_path("linux", {"XDG_STATE_HOME": "/st"}, Path("/h"))
        self.assertEqual(x, Path("/st") / "rm-mcp" / "rm-mcp-fatal.log")
        d = desktop.fatal_log_path("linux", {}, Path("/h"))
        self.assertEqual(d, Path("/h") / ".local" / "state" / "rm-mcp"
                         / "rm-mcp-fatal.log")


class TestEntry(unittest.TestCase):
    def test_entry_shape(self):
        e = desktop.desktop_entry("C:/py/python.exe", Path("C:/rm/run_server.py"))
        self.assertEqual(e, {"command": "C:/py/python.exe",
                             "args": [str(Path("C:/rm/run_server.py"))]})
        self.assertNotIn("type", e)


class TestWrite(TmpCase):
    def test_missing_file_is_created(self):
        r = desktop.write_desktop_entry(self.cfg, "py", self.launcher)
        self.assertEqual(r["state"], "created")
        self.assertIsNone(r["backup"])
        data = self.read_cfg()
        self.assertEqual(data["mcpServers"]["rm"]["command"], "py")
        text = self.cfg.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"))
        self.assertIn('\n  "mcpServers"', text)

    def test_merge_keeps_other_keys_and_servers(self):
        self.write_cfg({"theme": "dark", "mcpServers": {"other": {"command": "x"}}})
        r = desktop.write_desktop_entry(self.cfg, "py", self.launcher)
        self.assertEqual(r["state"], "created")
        data = self.read_cfg()
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["mcpServers"]["other"], {"command": "x"})
        self.assertEqual(data["mcpServers"]["rm"]["command"], "py")

    def test_identical_entry_is_kept_without_write(self):
        entry = desktop.desktop_entry("py", self.launcher)
        self.write_cfg({"mcpServers": {"rm": entry}})
        before = self.cfg.read_text(encoding="utf-8")
        mtime = self.cfg.stat().st_mtime_ns
        r = desktop.write_desktop_entry(self.cfg, "py", self.launcher)
        self.assertEqual(r["state"], "kept")
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), before)
        self.assertEqual(self.cfg.stat().st_mtime_ns, mtime)
        self.assertEqual(list(self.cfg.parent.glob("*.bak-*")), [])

    def test_different_entry_is_replaced_with_backup(self):
        self.write_cfg({"keep": 1, "mcpServers": {
            "rm": {"command": "C:/Python313/python.exe", "args": ["old.py"]},
            "other": {"command": "x"}}})
        r = desktop.write_desktop_entry(self.cfg, "venvpy", self.launcher)
        self.assertEqual(r["state"], "updated")
        self.assertEqual(r["previous_command"], "C:/Python313/python.exe")
        backup = Path(r["backup"])
        self.assertTrue(backup.is_file())
        self.assertIn(".bak-", backup.name)
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))
                         ["mcpServers"]["rm"]["args"], ["old.py"])
        data = self.read_cfg()
        self.assertEqual(data["keep"], 1)
        self.assertEqual(data["mcpServers"]["other"], {"command": "x"})
        self.assertEqual(data["mcpServers"]["rm"]["command"], "venvpy")

    def test_update_keeps_the_rm_entrys_own_keys(self):
        # An rm entry may carry its own env block; replacing the entry
        # wholesale would drop it, which is the clobbering this must not do.
        self.write_cfg({"mcpServers": {"rm": {
            "command": "C:/Python313/python.exe", "args": ["old.py"],
            "env": {"RMAPI_BIN": "C:/bin/rmapi.exe"}}}})
        r = desktop.write_desktop_entry(self.cfg, "venvpy", self.launcher)
        self.assertEqual(r["state"], "updated")
        rm = self.read_cfg()["mcpServers"]["rm"]
        self.assertEqual(rm["command"], "venvpy")
        self.assertEqual(rm["args"], [str(self.launcher)])
        self.assertEqual(rm["env"], {"RMAPI_BIN": "C:/bin/rmapi.exe"})

    def test_matching_entry_with_extra_keys_is_kept(self):
        entry = {**desktop.desktop_entry("py", self.launcher), "env": {"A": "1"}}
        self.write_cfg({"mcpServers": {"rm": entry}})
        before = self.cfg.read_text(encoding="utf-8")
        r = desktop.write_desktop_entry(self.cfg, "py", self.launcher)
        self.assertEqual(r["state"], "kept")
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), before)

    def test_config_with_a_bom_is_read_not_refused(self):
        self.cfg.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}),
                            encoding="utf-8-sig")
        r = desktop.write_desktop_entry(self.cfg, "py", self.launcher)
        self.assertEqual(r["state"], "created")
        self.assertEqual(self.read_cfg()["mcpServers"]["other"], {"command": "x"})
        check = desktop.registration_check("py", self.cfg)
        self.assertEqual(check["state"], "match")

    def test_unparseable_json_is_refused_and_untouched(self):
        self.write_cfg("{ not json")
        r = desktop.write_desktop_entry(self.cfg, "py", self.launcher)
        self.assertEqual(r["state"], "refused")
        self.assertIn("reason", r)
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), "{ not json")
        self.assertEqual(list(self.cfg.parent.glob("*.bak-*")), [])

    def test_non_object_mcpservers_is_refused(self):
        self.write_cfg({"mcpServers": ["nope"]})
        r = desktop.write_desktop_entry(self.cfg, "py", self.launcher)
        self.assertEqual(r["state"], "refused")
        self.assertEqual(self.read_cfg(), {"mcpServers": ["nope"]})

    def test_non_object_top_level_is_refused(self):
        self.write_cfg("[1, 2]")
        r = desktop.write_desktop_entry(self.cfg, "py", self.launcher)
        self.assertEqual(r["state"], "refused")


class TestRegistrationCheck(TmpCase):
    def test_absent(self):
        r = desktop.registration_check("py", self.cfg)
        self.assertEqual(r["state"], "absent")
        self.assertEqual(r["path"], str(self.cfg))

    def test_not_registered(self):
        self.write_cfg({"mcpServers": {"other": {"command": "x"}}})
        self.assertEqual(desktop.registration_check("py", self.cfg)["state"],
                         "not_registered")

    def test_match(self):
        self.write_cfg({"mcpServers": {"rm": {"command": sys.executable, "args": []}}})
        r = desktop.registration_check(sys.executable, self.cfg)
        self.assertEqual(r["state"], "match")
        self.assertEqual(r["command"], sys.executable)

    def test_mismatch(self):
        self.write_cfg({"mcpServers": {"rm": {"command": "C:/Python313/python.exe"}}})
        r = desktop.registration_check(sys.executable, self.cfg)
        self.assertEqual(r["state"], "mismatch")
        self.assertEqual(r["command"], "C:/Python313/python.exe")
        self.assertEqual(r["expected"], sys.executable)

    def test_unreadable(self):
        self.write_cfg("{ broken")
        self.assertEqual(desktop.registration_check("py", self.cfg)["state"],
                         "unreadable")

    def test_missing_command_is_unreadable(self):
        self.write_cfg({"mcpServers": {"rm": {"args": []}}})
        self.assertEqual(desktop.registration_check("py", self.cfg)["state"],
                         "unreadable")

    def test_never_raises_on_directory(self):
        self.cfg.mkdir(parents=True)
        self.assertIn(desktop.registration_check("py", self.cfg)["state"],
                      ("absent", "unreadable"))


class TestRegisterCli(TmpCase):
    """run_server.py --register-desktop: the desktop entry and nothing else.

    The repair after an upgrade moves the server to a new interpreter. It must
    not re-run the init walk, whose --yes defaults would rewrite tools/.env.
    """

    def run_cli(self, *argv: str, python: str = "venvpy") -> tuple[int, str]:
        from io import StringIO
        buf = StringIO()
        rc = desktop.register_cli(list(argv), python, self.launcher, out=buf)
        return rc, buf.getvalue()

    def test_writes_the_entry_and_reports(self):
        rc, out = self.run_cli("--desktop-config", str(self.cfg))
        self.assertEqual(rc, 0)
        self.assertEqual(self.read_cfg()["mcpServers"]["rm"]["command"], "venvpy")
        self.assertIn("desktop config written", out)
        self.assertIn("quit", out)

    def test_repairs_a_stale_entry_with_backup(self):
        self.write_cfg({"mcpServers": {"rm": {"command": "C:/Python313/python.exe",
                                              "args": ["old.py"]}}})
        rc, out = self.run_cli("--desktop-config", str(self.cfg))
        self.assertEqual(rc, 0)
        self.assertIn("C:/Python313/python.exe", out)
        self.assertEqual(len(list(self.cfg.parent.glob("*.bak-*"))), 1)

    def test_writes_nothing_else(self):
        self.run_cli("--desktop-config", str(self.cfg))
        made = sorted(p.relative_to(self.tmp).as_posix()
                      for p in self.tmp.rglob("*") if p.is_file())
        self.assertEqual(made, ["Claude/claude_desktop_config.json"])

    def test_refusal_exits_1_and_leaves_the_file(self):
        self.write_cfg("{ broken")
        rc, out = self.run_cli("--desktop-config", str(self.cfg))
        self.assertEqual(rc, 1)
        self.assertIn("not written", out)
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), "{ broken")

    def test_unknown_option_exits_2(self):
        rc, _ = self.run_cli("--bogus")
        self.assertEqual(rc, 2)

    def test_warns_when_not_run_from_a_venv(self):
        from unittest import mock
        with mock.patch.object(desktop.sys, "prefix", "C:/Py"), \
                mock.patch.object(desktop.sys, "base_prefix", "C:/Py"):
            rc, out = self.run_cli("--desktop-config", str(self.cfg))
        self.assertEqual(rc, 0)
        self.assertIn("not in a virtual environment", out)

    def test_no_venv_warning_inside_a_venv(self):
        from unittest import mock
        with mock.patch.object(desktop.sys, "prefix", "C:/rm/.venv"), \
                mock.patch.object(desktop.sys, "base_prefix", "C:/Py"):
            _, out = self.run_cli("--desktop-config", str(self.cfg))
        self.assertNotIn("virtual environment", out)


class TestFatalLog(TmpCase):
    def test_appends_with_timestamp_and_interpreter(self):
        log = self.tmp / "logs" / "rm-mcp" / "fatal.log"
        self.assertEqual(desktop.append_fatal("boom one", log), log)
        desktop.append_fatal("boom two", log)
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn(sys.executable, lines[0])
        self.assertTrue(lines[0].endswith("boom one"))
        self.assertRegex(lines[0], r"^\d{4}-\d{2}-\d{2}T")

    def test_oserror_returns_none(self):
        blocker = self.tmp / "file"
        blocker.write_text("x", encoding="utf-8")
        # A path whose parent is a file cannot be created.
        self.assertIsNone(desktop.append_fatal("x", blocker / "sub" / "f.log"))


if __name__ == "__main__":
    unittest.main()
