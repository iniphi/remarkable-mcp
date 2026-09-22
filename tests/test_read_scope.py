r"""Read-tool path scoping on the network-exposed lane (security finding B1).

rm_move / rm_delete / rm_push_dir have always been confined to the managed
roots by manage._guard_path. The READ tools -- rm_list, rm_render, rm_page_ink,
rm_page_image -- accepted a free-form device path with no equivalent gate, so
any holder of the single shared x-api-key could enumerate and rasterise the
whole paired account (Business Time, Science, personal notebooks), not just the
collaboration folders. Found in the 2026-08-19 pre-ship security review.

The fix follows the codebase's existing principle: transport is the policy.
stdio is a trusted local registration and is UNCHANGED; streamable-http is
scoped by default. These tests lock both halves.

Run: python -m pytest tests/test_read_scope.py
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

RM_MCP_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = RM_MCP_DIR.parent / "tools"
for _p in (RM_MCP_DIR, TOOLS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from rm_mcp import config, manage  # noqa: E402


class TestReadScopeEnforced(unittest.TestCase):
    def test_stdio_is_not_enforced(self):
        with mock.patch.dict(os.environ, {"RM_MCP_TRANSPORT": "stdio"}, clear=False):
            os.environ.pop("RM_MCP_ALLOW_READ_ANYWHERE", None)
            self.assertFalse(config.read_scope_enforced())

    def test_default_transport_is_not_enforced(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RM_MCP_TRANSPORT", None)
            os.environ.pop("RM_MCP_ALLOW_READ_ANYWHERE", None)
            self.assertFalse(config.read_scope_enforced())

    def test_streamable_http_is_enforced(self):
        with mock.patch.dict(os.environ,
                             {"RM_MCP_TRANSPORT": "streamable-http"}, clear=False):
            os.environ.pop("RM_MCP_ALLOW_READ_ANYWHERE", None)
            self.assertTrue(config.read_scope_enforced())

    def test_explicit_optin_disables_enforcement(self):
        with mock.patch.dict(os.environ,
                             {"RM_MCP_TRANSPORT": "streamable-http",
                              "RM_MCP_ALLOW_READ_ANYWHERE": "1"}, clear=False):
            self.assertFalse(config.read_scope_enforced())


class TestGuardReadPath(unittest.TestCase):
    """Fenced cases patch a concrete root: the public default is the whole
    device (MANAGED_ROOTS == ("/",)), where nothing is outside anything."""

    def setUp(self) -> None:
        self.fenced = mock.patch.object(manage, "MANAGED_ROOTS", ("/00_Projects",))

    def test_noop_when_not_enforced(self):
        """The desk lane must be completely unchanged -- root browse still works."""
        with mock.patch.object(config, "read_scope_enforced", return_value=False):
            self.assertIsNone(manage.guard_read_path("/"))
            self.assertIsNone(manage.guard_read_path("/Business Time/private"))

    def test_blocks_outside_managed_roots_when_enforced(self):
        with mock.patch.object(config, "read_scope_enforced", return_value=True), \
                self.fenced:
            out = manage.guard_read_path("/Business Time/private")
        self.assertIsNotNone(out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["system"], "config")
        self.assertIn("outside the managed roots", out["error"]["message"])

    def test_root_browse_blocked_when_enforced(self):
        with mock.patch.object(config, "read_scope_enforced", return_value=True), \
                self.fenced:
            self.assertIsNotNone(manage.guard_read_path("/"))

    def test_whole_device_scope_blocks_nothing(self):
        """The public default: guard_scope "whole device" is loud, not a fence."""
        with mock.patch.object(config, "read_scope_enforced", return_value=True), \
                mock.patch.object(manage, "MANAGED_ROOTS", ("/",)):
            self.assertIsNone(manage.guard_read_path("/"))
            self.assertIsNone(manage.guard_read_path("/Business Time/private"))

    def test_allows_managed_roots_when_enforced(self):
        with mock.patch.object(config, "read_scope_enforced", return_value=True):
            for root in manage.MANAGED_ROOTS:
                self.assertIsNone(manage.guard_read_path(root))
                self.assertIsNone(manage.guard_read_path(root + "/some/doc"))

    def test_prefix_lookalike_is_not_treated_as_inside(self):
        """'/00_ProjectsEvil' must not pass just because it starts with the root."""
        with mock.patch.object(config, "read_scope_enforced", return_value=True), \
                self.fenced:
            self.assertIsNotNone(manage.guard_read_path("/00_ProjectsEvil/doc"))

    def test_allow_anywhere_escape_still_works(self):
        with mock.patch.object(config, "read_scope_enforced", return_value=True):
            self.assertIsNone(
                manage.guard_read_path("/Business Time/x", allow_anywhere=True))


if __name__ == "__main__":
    unittest.main()
