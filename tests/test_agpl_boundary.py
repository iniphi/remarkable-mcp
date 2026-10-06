"""Licence guard for the public rm-mcp ship.

The public repo ships under a permissive licence. PyMuPDF (fitz) is AGPL-3.0,
and nothing permissive replaces it for the one job that needs a PDF engine --
pypdfium2 can rasterise but cannot DRAW into a PDF
(tests/fixtures/render/PYPDFIUM2_EVAL.md -- an earlier version of this
docstring cited a path outside the tree that never existed; corrected 2026-08-27).
So the rule, settled 2026-08-21, is:

    No file in the shipped manifest may import fitz. The single place fitz is
    still needed (rm_render_content's markdown/html/svg render) invokes it in a
    CHILD INTERPRETER, the same isolation roundtrip.py already uses for
    image->PDF conversion.

These tests fail the moment that stops being true -- which is the point. A
future edit that adds `import fitz` to a shipped module, or that makes a shipped
module import one of the cut AGPL modules, breaks the licence of the whole
public repo silently otherwise.

Run: python -m pytest tests/test_agpl_boundary.py
"""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))

# Ask config where the substrate lives rather than assuming a layout: this
# suite has to pass BOTH embedded in the private monorepo and in the standalone public
# tree, where tools/ sits beside the package instead of a level up.
from rm_mcp import config  # noqa: E402

PKG_DIR = Path(config.__file__).resolve().parent
TOOLS_DIR = config.TOOLS_DIR


def _tool(name: str) -> Path:
    """A shipped script by bare filename, in either layout.

    tools/ was split into rm/, zotero/, litgather/ and common/ on 2026-09-20
    while the PUBLIC tree stayed flat, so this suite runs against both
    shapes and must not assume either. config.resolve_script is the same
    resolver rm_mcp.runner uses, so the test looks where the server looks.
    """
    return config.resolve_script(name)

# The tools/ files that ship with the public core surface. Computed as the
# transitive closure of what the package imports and shells out to; pinned here
# so the closure test can prove it is still complete and still minimal.
SHIPPED_TOOLS = {
    "rm_bundle.py",
    "rm_config.py",
    "rm_diff.py",
    "rm_extract_highlights.py",
    "rm_extract_text.py",
    "rm_make_text_notebook.py",
    "rm_reading_ledger.py",
    "rm_render_content.py",
    "rm_render_page.py",
}
# rm_state_remote.py was in this set from 56f84bb until 2026-09-20 and
# was never in rm_build_public.MANIFEST -- one half of a lockstep both files
# claim to keep and nothing enforced. It kept THIS suite green while the
# public tree it describes could not import at all, because rm_config's
# import of it was hard. The import is guarded now and the cloud lane stays
# unshipped, so the right set is the 9 the build actually copies.
# test_manifest_matches_the_build_script (below) is what stops the two
# drifting again.

AGPL_MODULES = {"fitz", "pymupdf"}


def _imported_roots(path: Path) -> set[str]:
    """Top-level module names imported by a file, at ANY scope.

    AST-based, so a name that appears only inside a string literal -- such as
    the `import fitz` inside rm_render_content's child-interpreter snippet --
    is correctly NOT counted as an import of this module.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


# The package files the public build DROPS. Read from the build script rather
# than restated here, so the two can never disagree -- tools/rm_build_public.py
# both deletes them and verifies they did not leak (its `problems` check).
# Absent in the standalone public tree, where those files are already gone and
# excluding nothing is the correct answer; the import is optional for that case.
try:
    from rm_build_public import PRIVATE_LANE  # noqa: E402
    from rm_build_public import optional_import_roots  # noqa: E402
    PRIVATE_LANE_AVAILABLE = True
except ImportError:  # pragma: no cover -- the public tree has no build script
    PRIVATE_LANE: set[str] = set()
    PRIVATE_LANE_AVAILABLE = False

    def optional_import_roots(path: Path) -> set[str]:
        """Public-tree fallback: subtract nothing, and that is correct here.

        The unshipped-module check below only fires when the imported module
        EXISTS under tools/. In the public tree an optionally-imported private
        module is absent by construction, so the check cannot fire on it and
        there is nothing for this to subtract. The real implementation is only
        needed in the private tree, where the build script is importable.
        """
        return set()


def _shipped_files() -> list[Path]:
    """Every file the public tree actually carries.

    This globbed the WHOLE package until 2026-09-16, which asserted a stricter
    boundary than the build has: it held the private lane -- stacks_lane.py,
    zotero_lane.py, zotero.py -- to the shipped manifest even though the build
    deletes all three. The first import to trip it was stacks_lane's use of
    rm_index (for the read-scope guard's name resolution), a module that has no
    business in the public manifest and does not need to be in it.
    """
    package = [f for f in sorted(PKG_DIR.glob("*.py"))
               if f.name not in PRIVATE_LANE]
    return package + [_tool(n) for n in sorted(SHIPPED_TOOLS)]


class TestNoAgplInShippedManifest(unittest.TestCase):
    def test_no_shipped_file_imports_fitz(self):
        offenders = []
        for f in _shipped_files():
            hits = _imported_roots(f) & AGPL_MODULES
            if hits:
                offenders.append(f"{f.name}: {sorted(hits)}")
        self.assertEqual(
            offenders, [],
            "AGPL (PyMuPDF) imported by shipped file(s). Either isolate the call "
            "in a child interpreter (see rm_render_content._FITZ_RENDER_SNIPPET) "
            "or move the code to a module that is not shipped.")

    def test_render_content_names_fitz_only_inside_the_snippet(self):
        """The isolation is real, not just unimported: fitz is named in the
        file (inside the child-interpreter snippet) but never imported by it."""
        src = _tool("rm_render_content.py").read_text(encoding="utf-8")
        self.assertIn("import fitz", src,
                      "snippet should still carry the child-side import")
        self.assertNotIn("fitz", _imported_roots(_tool("rm_render_content.py")),
                         "fitz must not be a real import of this module")


class TestShippedManifestIsClosed(unittest.TestCase):
    """Every tools/ module a shipped file imports must itself be shipped.

    This is what stops the AGPL creeping back in by the side door: before the
    2026-08-21 split, rm_reading_ledger imported rm_extract_highlights (which
    imports fitz), so shipping rm_page_ink would have dragged AGPL in without a
    single `import fitz` appearing in any file we thought we were shipping.
    """

    def test_no_shipped_file_imports_an_unshipped_tools_module(self):
        # Imports guarded by `except ImportError` are subtracted: the module is
        # absent in the public tree and the handler is what runs there, so the
        # import cannot break it. The AGPL test above deliberately does NOT
        # subtract them -- a guarded `import fitz` still links AGPL wherever
        # fitz is installed. Same asymmetry as tools/rm_build_public.audit.
        leaks = []
        for f in _shipped_files():
            for root in _imported_roots(f) - optional_import_roots(f):
                candidate = f"{root}.py"
                if _tool(candidate).is_file() and candidate not in SHIPPED_TOOLS:
                    leaks.append(f"{f.name} -> {candidate}")
        self.assertEqual(
            leaks, [],
            "shipped file imports a tools/ module that is not in SHIPPED_TOOLS. "
            "Either add it to the manifest (and check it is AGPL-free), guard it "
            "with `except ImportError` if the public tree can genuinely run "
            "without it, or split the needed part out, as rm_bundle.py was split "
            "from rm_extract_highlights.py.")

    def test_manifest_matches_the_build_script(self):
        """SHIPPED_TOOLS and rm_build_public.MANIFEST must name the same files.

        Both files say they are "kept in lockstep" and until 2026-09-20 nothing
        checked it. 56f84bb added rm_state_remote.py to SHIPPED_TOOLS and not to
        MANIFEST, so this suite went green describing a public tree that could
        not import -- the drift was invisible precisely where it mattered.
        """
        try:
            from rm_build_public import MANIFEST_NAMES
        except ImportError:
            self.skipTest("public tree: no build script to compare against")
        # MANIFEST entries are paths relative to tools/ since the split
        # ("rm/rm_config.py"); MANIFEST_NAMES is the basenames, which is what
        # the public tree is called and what SHIPPED_TOOLS names.
        self.assertEqual(
            SHIPPED_TOOLS, MANIFEST_NAMES,
            "SHIPPED_TOOLS and rm_build_public.MANIFEST disagree. They describe "
            "the same 9 files -- the ones the build copies into the public "
            "tree -- so a file added to one must be added to the other, or "
            "this suite tests a tree the build does not produce.")

    def test_private_lane_is_real_and_excluded(self):
        # The exclusion above is only sound while these files genuinely exist
        # here and genuinely do not ship. If the build stops dropping one, it
        # must re-enter the manifest checks rather than quietly leave them.
        #
        # Skipped in the public tree, where PRIVATE_LANE is empty BY DESIGN
        # (see the import guard at the top of this file: the files are already
        # gone and excluding nothing is the correct answer). Asserting it
        # non-empty there contradicted this file's own documented behaviour.
        if not PRIVATE_LANE_AVAILABLE:
            self.skipTest("public tree: the private lane is already absent")
        self.assertTrue(PRIVATE_LANE, "PRIVATE_LANE came back empty in a tree "
                                      "that has tools/rm_build_public.py")
        shipped = {f.name for f in _shipped_files()}
        for name in PRIVATE_LANE:
            self.assertTrue((PKG_DIR / name).is_file(),
                            f"PRIVATE_LANE names {name}, which is not here")
            self.assertNotIn(name, shipped)

    def test_manifest_has_no_dead_entries(self):
        missing = [n for n in SHIPPED_TOOLS if not _tool(n).is_file()]
        self.assertEqual(missing, [], "SHIPPED_TOOLS names a file that does not exist")


class TestWorksWithoutPymupdf(unittest.TestCase):
    """fitz is an OPTIONAL runtime dependency of the shipped surface."""

    def _import_with_fitz_blocked(self, module: str) -> subprocess.CompletedProcess:
        code = (
            "import sys\n"
            "class Blocker:\n"
            "    def find_module(self, name, path=None):\n"
            "        if name == 'fitz':\n"
            "            raise ImportError('fitz blocked')\n"
            "sys.meta_path.insert(0, Blocker())\n"
            # The DIRECTORY the module actually lives in, not TOOLS_DIR: since
            # the split that is a bucket here and TOOLS_DIR itself in the flat
            # public tree. Resolving it the same way the server does keeps this
            # test honest in both layouts.
            f"sys.path.insert(0, {str(_tool(module + '.py').parent)!r})\n"
            f"import {module}\n"
            "assert 'fitz' not in sys.modules, 'fitz leaked into sys.modules'\n"
            "print('ok')\n"
        )
        return subprocess.run([sys.executable, "-c", code],
                              capture_output=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL,
                              timeout=120)

    def test_render_content_imports_without_fitz(self):
        proc = self._import_with_fitz_blocked("rm_render_content")
        self.assertEqual(proc.returncode, 0,
                         f"rm_render_content must import without PyMuPDF:\n{proc.stderr}")

    def test_bundle_imports_without_fitz(self):
        proc = self._import_with_fitz_blocked("rm_bundle")
        self.assertEqual(proc.returncode, 0,
                         f"rm_bundle must import without PyMuPDF:\n{proc.stderr}")

    def test_reading_ledger_imports_without_fitz(self):
        proc = self._import_with_fitz_blocked("rm_reading_ledger")
        self.assertEqual(proc.returncode, 0,
                         f"rm_reading_ledger must import without PyMuPDF:\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
