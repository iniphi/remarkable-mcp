"""The published tool surface is code, not a claim in a document.

For weeks a to-do asserted "ship the 6-tool rm-mcp surface publicly" while the
repo defined 24 tools and no subset existed anywhere in the tree. surface.py
ends that by making the surfaces executable and verified at startup. These
tests pin the three of them, and -- more importantly -- pin the properties that
make `core` safe to publish: no Zotero, no Notion, no vision spend, nothing
reaching an AGPL-linked script.

RM_MCP_SURFACE is read at import, so every case runs in a child interpreter.

Run: python -m pytest tests/test_surface.py
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
# pass both embedded in 104_stacks and in the standalone public tree.
from rm_mcp import config as _config  # noqa: E402

TOOLS_DIR = _config.TOOLS_DIR

_PREAMBLE = (
    f"import sys, json\n"
    f"sys.path.insert(0, {str(RM_MCP_DIR)!r})\n"
    f"sys.path.insert(0, {str(TOOLS_DIR)!r})\n"
)


def _run(env: dict[str, str], body: str) -> subprocess.CompletedProcess:
    child = os.environ.copy()
    child.pop("RM_MCP_SURFACE", None)
    child.update(env)
    return subprocess.run([sys.executable, "-c", _PREAMBLE + body],
                          capture_output=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL,
                          timeout=180, env=child)


def registered(surface_name: str | None) -> list[str]:
    """Tool names actually registered under a given RM_MCP_SURFACE."""
    env = {} if surface_name is None else {"RM_MCP_SURFACE": surface_name}
    proc = _run(env, "from rm_mcp import server\n"
                     "print(json.dumps(sorted(server.mcp.registered)))\n")
    if proc.returncode != 0:
        raise AssertionError(f"import failed:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def constant(name: str) -> list[str]:
    proc = _run({}, f"from rm_mcp import surface\n"
                    f"print(json.dumps(sorted(surface.{name})))\n")
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestDefaultFollowsTheBuild(unittest.TestCase):
    """Unset means `full` on a complete install, `core` on the trimmed public
    build -- so a published tree never advertises tools whose scripts it does
    not ship. Asserted as the rule, since this suite runs in both trees."""

    def setUp(self) -> None:
        # "Is rm_pull.py present?" is the question; WHERE it sits is not.
        # The S112 split moved it to tools/rm/, so a flat stat started
        # answering "no" on the desk and this class began asserting the public
        # tree's counts against the private build. Ask both layouts.
        self.full_build = ((TOOLS_DIR / "rm_pull.py").is_file()
                           or (TOOLS_DIR / "rm" / "rm_pull.py").is_file())

    def test_default_matches_the_build(self):
        expected = "full" if self.full_build else "core"
        proc = _run({}, "from rm_mcp import surface\n"
                        "print(json.dumps(surface.active_surface()))\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout.strip().splitlines()[-1]), expected)

    def test_unset_registers_the_default_surface(self):
        expected = registered("full") if self.full_build else registered("core")
        self.assertEqual(registered(None), expected)

    def test_full_surface_is_every_tool_the_build_ships(self):
        """`full` means "no filter", so its SIZE is a property of the build.

        This asserted a literal 24 until 2026-09-06, when the Stacks lane
        (stacks_lane.py -- rm_capture_todos, and rm_triage_inbox before its
        2026-09-16 withdrawal) stopped shipping
        publicly. A correct public build then failed a test whose own class
        docstring says it runs in both trees. The count is derived, not fixed.
        """
        # 25 on the desk since 2026-09-20, when rm_create was added (24 before
        # that, from 2026-09-16's withdrawal of rm_triage_inbox with the Inbox
        # lane it served); 18 on the public tree, where every workflow, vision
        # and AGPL tool lives in a private lane the build drops, so `full` and
        # `core` are the same set there.
        expected = 25 if self.full_build else 18
        self.assertEqual(len(registered("full")), expected)

    def test_explicit_surface_overrides_the_build_default(self):
        """An explicit value must win even where the default differs."""
        self.assertEqual(registered("remote"), constant("REMOTE_TOOLS"))


class TestSurfacesRegisterExactly(unittest.TestCase):
    def test_core_registers_exactly_core_tools(self):
        self.assertEqual(registered("core"), constant("CORE_TOOLS"))

    def test_remote_registers_exactly_remote_tools(self):
        self.assertEqual(registered("remote"), constant("REMOTE_TOOLS"))

    def test_surfaces_nest(self):
        full, core, remote = (set(registered("full")), set(registered("core")),
                              set(registered("remote")))
        self.assertLessEqual(remote, core, "remote must be a subset of core")
        self.assertLessEqual(core, full, "core must be a subset of full")


class TestUnknownSurfaceRefusesToStart(unittest.TestCase):
    def test_typo_is_fatal_not_a_silent_fallback(self):
        """A mistyped surface must never quietly publish everything."""
        proc = _run({"RM_MCP_SURFACE": "coer"}, "from rm_mcp import server\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not a known surface", proc.stderr)


class TestCoreIsSafeToPublish(unittest.TestCase):
    """The properties that actually justify publishing `core`."""

    # Tools that shell out to a Zotero- or Notion-coupled CLI, or spend on
    # vision. Excluded from core for scope, not licence.
    WORKFLOW_TOOLS = {"rm_push_reading", "rm_pull", "rm_pull_notebook",
                      "rm_capture_todos", "rm_write_note1",
                      "rm_interpret_page"}

    # Tools that reach a script importing fitz. Excluded for licence.
    # rm_render and rm_page_image LEFT this set on 2026-08-27: the AGPL
    # rasterizer moved out of tools/rm_render_page.py into the unshipped
    # tools/rm_eval_pdfium.py, so the scripts they shell out to are clean.
    # rm_flatten stays permanently -- the pypdfium2 eval is explicit that there
    # is no permissive drop-in for the fitz.Story rich-text path.
    # rm_get_highlights LEFT this set on 2026-09-10 (S100): the text-layer
    # intersection was ported from PyMuPDF to pypdfium2, so it ships in core.
    AGPL_TOOLS = {"rm_flatten"}

    def test_core_excludes_the_workflow_lane(self):
        self.assertEqual(set(registered("core")) & self.WORKFLOW_TOOLS, set())

    def test_core_excludes_every_agpl_reaching_tool(self):
        self.assertEqual(set(registered("core")) & self.AGPL_TOOLS, set())

    def test_render_blocked_tools_are_never_published(self):
        """Whatever sits in BLOCKED_ON_RENDER_PORT must stay out of core.

        The set emptied on 2026-08-27 when the AGPL rasterizer left
        rm_render_page.py, so this holds vacuously today. It is kept because it
        is the named guard for this class of block -- if a tool is ever blocked
        again, this is what stops it reaching core -- not because it currently
        catches anything."""
        blocked = set(constant("BLOCKED_ON_RENDER_PORT"))
        self.assertEqual(set(registered("core")) & blocked, set())

    def test_render_tools_are_published_now_the_port_landed(self):
        """The other half of the 2026-08-27 port, asserted so it cannot silently
        regress: rm_render and rm_page_image are the tools it unblocked, and
        rm_page_image is what completes SHIP-FLAVOUR-2.md's documented six-tool
        remote surface."""
        self.assertLessEqual({"rm_render", "rm_page_image"}, set(registered("core")))
        self.assertIn("rm_page_image", set(registered("remote")))

    def test_core_still_covers_the_named_infrastructure(self):
        """push / pull / diff / delete / create -- the stated product."""
        core = set(registered("core"))
        for tool in ("rm_push_file", "rm_push_content", "rm_new_notebook",
                     "rm_pull_project", "rm_get_highlights",
                     "rm_diff", "rm_delete", "rm_move",
                     "rm_ensure_project_folder", "rm_health", "rm_list"):
            with self.subTest(tool=tool):
                self.assertIn(tool, core)


class TestMissingScriptPreflight(unittest.TestCase):
    """A build without a script must say so, not emit a bare interpreter error."""

    def test_absent_script_returns_actionable_rc127(self):
        proc = _run({}, "from rm_mcp import runner\n"
                        "p = runner.run_script('rm_definitely_absent.py', [], 'rm_x')\n"
                        "print(json.dumps({'rc': p.returncode, 'err': p.stderr}))\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out["rc"], 127)
        self.assertIn("not available in this build", out["err"])
        self.assertIn("rm_definitely_absent.py", out["err"])

    def test_present_script_is_unaffected(self):
        proc = _run({}, "from rm_mcp import runner\n"
                        "p = runner.run_script('rm_diff.py', ['--help'], 'rm_diff')\n"
                        "print(json.dumps({'rc': p.returncode}))\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertNotEqual(out["rc"], 127, "a real script must not hit the preflight")


if __name__ == "__main__":
    unittest.main()
