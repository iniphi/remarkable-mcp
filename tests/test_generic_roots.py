"""The device layout is configurable, not hardcoded to one person's tablet.

rm-mcp grew inside a private monorepo, so its device lane was written as a
literal /00_Projects + /00_Projects/<project> pair, and a project folder had to
match the author's NNN_name convention. None of that is usable by anyone else, and the
public ship needs it to be. Settled 2026-08-21: config.py reads the layout from
the environment, and config is the single source of truth that manage.py and
server.py both alias.

These knobs are read at IMPORT time (a deployment sets them once, before the
server starts), so every case here runs in a fresh child interpreter with the
environment set. Testing them in-process would prove nothing.

Run: python -m pytest tests/test_generic_roots.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))

# Layout-agnostic: config knows where the substrate lives, and this suite must
# pass both embedded in the private monorepo and in the standalone public tree.
from rm_mcp import config as _config  # noqa: E402

TOOLS_DIR = _config.TOOLS_DIR

from _toolpath import tool_script  # noqa: E402

# Cleared in every child so a value set on the developer's own shell cannot
# mask a broken default.
KNOBS = ("RM_MCP_PROJECTS_ROOT", "RM_MCP_MANAGED_ROOTS", "RM_MCP_PROJECT_PATTERN",
         "RM_ROOT", "RM_MCP_TRANSPORT", "RM_MCP_ALLOW_READ_ANYWHERE",
         "RM_MCP_ALLOW_DESTRUCTIVE")

_PREAMBLE = (
    f"import sys, json\n"
    f"sys.path.insert(0, {str(RM_MCP_DIR)!r})\n"
    f"sys.path.insert(0, {str(TOOLS_DIR)!r})\n"
    "from rm_mcp import config, manage\n"
)


def _run(env: dict[str, str], body: str) -> object:
    child = os.environ.copy()
    for knob in KNOBS:
        child.pop(knob, None)
    child.update(env)
    proc = subprocess.run([sys.executable, "-c", _PREAMBLE + body],
                          capture_output=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL,
                          timeout=120, env=child)
    if proc.returncode != 0:
        raise AssertionError(f"probe failed:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def probe(env: dict[str, str], expr: str) -> object:
    """Import config/manage with `env` applied and return the value of `expr`."""
    return _run(env, f"print(json.dumps({expr}))\n")


def probe_error(env: dict[str, str], expr: str) -> str | None:
    """Return the error message `expr` raises, or None if it did not raise."""
    return _run(env, "try:\n"
                     f"    {expr}\n"
                     "    print(json.dumps(None))\n"
                     "except Exception as exc:\n"
                     "    print(json.dumps(str(exc)))\n")


class TestDefaultsUnchanged(unittest.TestCase):
    """The desk must behave exactly as it did before the genericisation."""

    def test_default_projects_root(self):
        """The desk pins /00_Projects in tools/.env; the public default is the
        device root since 2026-09-10 -- a project is any top-level folder."""
        desk = tool_script(TOOLS_DIR, "rm_pull.py").is_file()
        self.assertEqual(probe({}, "config.PROJECTS_DEVICE_ROOT"),
                         "/00_Projects" if desk else "/")

    def test_default_managed_roots(self):
        """MANAGED_ROOTS follows RM_ROOT, and RM_ROOT differs by build.

        The desk pins RM_ROOT in its own gitignored config; the public
        tree has no such file and takes the generic default that rm_config and
        the shipped .env.example agree on. Asserting the desk's value in both
        trees made a correct public build look broken (2026-09-06).
        """
        desk = tool_script(TOOLS_DIR, "rm_pull.py").is_file()
        # The desk's RM_ROOT is read back from the child's own config, so the
        # test carries no copy of the desk's private value.
        expected = (["/00_Projects", probe({}, "config.RM_ROOT")] if desk
                    else ["/"])   # both roots at "/" collapse to the whole device
        self.assertEqual(probe({}, "list(config.MANAGED_ROOTS)"), expected)

    def test_manage_aliases_config(self):
        self.assertEqual(probe({}, "list(manage.MANAGED_ROOTS)"),
                         probe({}, "list(config.MANAGED_ROOTS)"))

    def test_default_pattern_accepts_nnn_name(self):
        self.assertEqual(probe({}, "config.resolve_project('100_thesis')"),
                         "100_thesis")

    def test_default_pattern_on_the_desk_still_requires_nnn(self):
        """tools/.env pins the NNN shape on the desk; the public default accepts
        any folder name (ruled 2026-09-10 with the root move)."""
        desk = tool_script(TOOLS_DIR, "rm_pull.py").is_file()
        err = probe_error({}, "config.resolve_project('Research')")
        if desk:
            self.assertIsNotNone(err, "the desk must still require NNN_name")
            self.assertIn("NNN_name", err)
        else:
            self.assertIsNone(err)

    def test_public_default_accepts_any_folder_name(self):
        env = {"RM_MCP_PROJECT_PATTERN": "^[^/\\\\]+$", "RM_MCP_PROJECTS_ROOT": "/"}
        self.assertEqual(probe(env, "config.resolve_project('My Thesis')"), "My Thesis")
        self.assertEqual(probe(env, "config.project_device_dir('My Thesis')"), "/My Thesis")

    def test_public_default_never_detects_a_project_from_the_cwd(self):
        """With the permissive shape a parent walk would push into whatever
        directory the agent happened to be in, so detection trusts only
        CLAUDE_PROJECT_DIR and otherwise asks for project= explicitly."""
        env = {"RM_MCP_PROJECT_PATTERN": "^[^/\\\\]+$", "CLAUDE_PROJECT_DIR": ""}
        err = probe_error(env, "config.resolve_project(None)")
        self.assertIsNotNone(err)
        self.assertIn("project=", err)
        env["CLAUDE_PROJECT_DIR"] = "C:/Users/someone/code/thesis"
        self.assertEqual(probe(env, "config.resolve_project(None)"), "thesis")

    def test_root_lane_never_doubles_the_slash(self):
        self.assertEqual(probe({"RM_MCP_PROJECTS_ROOT": "/", "RM_MCP_PROJECT_PATTERN": "^[^/]+$"},
                               "config.project_device_dir('Thesis')"), "/Thesis")


class TestProjectsRootOverride(unittest.TestCase):
    def test_projects_root_is_configurable(self):
        self.assertEqual(probe({"RM_MCP_PROJECTS_ROOT": "/Work"},
                               "config.PROJECTS_DEVICE_ROOT"), "/Work")

    def test_override_is_normalised(self):
        """Trailing and missing slashes are the obvious way to get this wrong."""
        for raw in ("Work", "/Work/", "  /Work  ", "Work/"):
            with self.subTest(raw=raw):
                self.assertEqual(probe({"RM_MCP_PROJECTS_ROOT": raw},
                                       "config.PROJECTS_DEVICE_ROOT"), "/Work")

    def test_project_device_dir_follows_the_override(self):
        self.assertEqual(probe({"RM_MCP_PROJECTS_ROOT": "/Work"},
                               "config.project_device_dir('100_thesis')"),
                         "/Work/100_thesis")

    def test_managed_roots_follow_the_override(self):
        self.assertIn("/Work", probe({"RM_MCP_PROJECTS_ROOT": "/Work"},
                                     "list(config.MANAGED_ROOTS)"))


class TestManagedRootsOverride(unittest.TestCase):
    def test_explicit_list_replaces_the_pair(self):
        self.assertEqual(probe({"RM_MCP_MANAGED_ROOTS": "/Alpha,/Beta"},
                               "list(config.MANAGED_ROOTS)"), ["/Alpha", "/Beta"])

    def test_entries_are_normalised_and_deduplicated(self):
        self.assertEqual(probe({"RM_MCP_MANAGED_ROOTS": "Alpha/, /Alpha ,/Beta/"},
                               "list(config.MANAGED_ROOTS)"), ["/Alpha", "/Beta"])

    def test_bare_slash_is_the_whole_device(self):
        """A managed root of "/" subsumes every other entry and is reported as
        such -- loud, not dropped (ruled 2026-09-10 with the root move)."""
        roots = probe({"RM_MCP_MANAGED_ROOTS": "/,/Alpha"},
                      "list(config.MANAGED_ROOTS)")
        self.assertEqual(roots, ["/"])
        self.assertEqual(probe({"RM_MCP_MANAGED_ROOTS": "/"}, "config.guard_scope()"),
                         "whole device")
        self.assertEqual(probe({"RM_MCP_MANAGED_ROOTS": "/Alpha"}, "config.guard_scope()"),
                         "managed roots only")
        self.assertIsNone(probe({"RM_MCP_MANAGED_ROOTS": "/"},
                                "manage._guard_path('/Anything/at/all', False)"))

    def test_never_empty(self):
        """An all-slash override must still leave a usable root, not nothing."""
        self.assertTrue(probe({"RM_MCP_MANAGED_ROOTS": "/, /"},
                              "list(config.MANAGED_ROOTS)"))

    def test_rm_root_without_a_leading_slash_is_normalised(self):
        """RM_ROOT comes from the substrate config, which only strips a TRAILING
        slash. A rootless entry can never match _guard_path (which always
        normalises the candidate to a leading slash), so the root would silently
        stop being managed -- found live on 2026-08-21."""
        env = {"RM_ROOT": "Notes", "RM_MCP_PROJECTS_ROOT": "/Alpha"}
        roots = probe(env, "list(config.MANAGED_ROOTS)")
        self.assertIn("/Notes", roots)
        self.assertNotIn("Notes", roots)
        self.assertIsNone(probe(env, "manage._guard_path('/Notes/Reading', False)"),
                          "a rootless RM_ROOT must still guard its own subtree")


class TestGuardHonoursConfiguredRoots(unittest.TestCase):
    """The knobs are pointless unless the path guard actually follows them."""

    def test_guard_allows_a_configured_root(self):
        self.assertIsNone(probe({"RM_MCP_MANAGED_ROOTS": "/Alpha"},
                                "manage._guard_path('/Alpha/thing', False)"))

    def test_guard_blocks_the_old_default_once_repointed(self):
        out = probe({"RM_MCP_MANAGED_ROOTS": "/Alpha"},
                    "manage._guard_path('/00_Projects/100_thesis', False)")
        self.assertIsNotNone(out)
        self.assertFalse(out["ok"])
        self.assertIn("outside the managed roots", out["error"]["message"])

    def test_error_message_names_the_configured_roots(self):
        out = probe({"RM_MCP_MANAGED_ROOTS": "/Alpha,/Beta"},
                    "manage._guard_path('/Elsewhere', False)")
        self.assertIn("/Alpha", out["error"]["message"])
        self.assertIn("/Beta", out["error"]["message"])
        self.assertIn("/Alpha", out["error"]["remedy"])
        self.assertNotIn("100_thesis", out["error"]["remedy"],
                         "remedy must name the configured roots, not the old ones")

    def test_allow_anywhere_still_escapes(self):
        self.assertIsNone(probe({"RM_MCP_MANAGED_ROOTS": "/Alpha"},
                                "manage._guard_path('/Elsewhere', True)"))


class TestProjectPatternOverride(unittest.TestCase):
    CUSTOM = r"^[A-Za-z][A-Za-z0-9_\-]*$"

    def test_custom_pattern_accepts_a_plain_name(self):
        self.assertEqual(probe({"RM_MCP_PROJECT_PATTERN": self.CUSTOM},
                               "config.resolve_project('Research')"), "Research")

    def test_custom_pattern_error_quotes_the_pattern_not_nnn_name(self):
        err = probe_error({"RM_MCP_PROJECT_PATTERN": self.CUSTOM},
                          "config.resolve_project('9bad')")
        self.assertIsNotNone(err)
        self.assertNotIn("NNN_name", err,
                         "a custom pattern must not be described as NNN_name")
        self.assertNotIn(_config.NNN_EXAMPLE, err,
                         "a custom pattern must not carry the NNN_name example")

    def test_nnn_example_names_no_private_project(self):
        """The example reaches a user's error message; it must be neutral."""
        self.assertEqual(_config.NNN_EXAMPLE, "100_thesis")

    def test_path_separators_refused_whatever_the_pattern(self):
        """Separator rejection precedes the pattern, so even ^.*$ cannot escape."""
        for bad in ("a/b", "a\\b", ".."):
            with self.subTest(bad=bad):
                err = probe_error({"RM_MCP_PROJECT_PATTERN": r"^.*$"},
                                  f"config.resolve_project({bad!r})")
                self.assertIsNotNone(err, f"{bad!r} must be refused")
                self.assertIn("single path segment", err)


class TestRootJoinAtTheDeviceRoot(unittest.TestCase):
    """RM_ROOT defaults to "/" in the public build, and under() joined onto it
    as "//Sketches" (found 2026-10-06 by a doctest that no test ran)."""

    def test_joining_under_the_root_gives_one_slash(self):
        self.assertEqual(probe({}, "__import__('rm_config').under('/', 'Reading', 'Some Paper')"),
                         "/Reading/Some Paper")
        self.assertEqual(probe({}, "__import__('rm_config').under('/')"), "/")

    def test_derived_lanes_under_a_root_rm_root(self):
        got = probe({"RM_ROOT": "/"}, "[__import__('rm_config').SKETCHES_ROOT, "
                                      "__import__('rm_config').INBOX_ROOT, "
                                      "__import__('rm_config').PROJECTS_ROOT]")
        self.assertEqual(got, ["/Sketches", "/Inbox", "/Projects"])

    def test_a_named_root_is_unchanged(self):
        self.assertEqual(probe({}, "__import__('rm_config').under('110_notes', 'session-1')"),
                         "/110_notes/session-1")


if __name__ == "__main__":
    unittest.main()
