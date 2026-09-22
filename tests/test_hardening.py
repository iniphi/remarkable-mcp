"""Offline tests for the network-exposure hardening -- no device, no network.

Covers the two properties that matter when the server runs streamable-http on
a public endpoint whose only gate is a shared secret:

  1. destructive tools (rm_move / rm_delete) refuse to EXECUTE on that
     transport unless the deploy opts in, while dry runs and the local stdio
     registration are unaffected;
  2. the shared-secret comparison is constant-time.

Run: python -m pytest 104_stacks/rm-mcp/tests/test_hardening.py
"""

from __future__ import annotations

import hmac
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))

from rm_mcp import authz, config, manage  # noqa: E402

# Everything that can change a destructive verdict. Stripped from the
# inherited environment so a developer machine with a token exported does not
# quietly change what these tests assert.
_POLICY_VARS = frozenset({
    "RM_MCP_TRANSPORT", "RM_MCP_ALLOW_DESTRUCTIVE", "RM_MCP_AUTH_TOKEN",
    *authz.SCOPED_TOKEN_VARS,
})


class TestDestructivePolicy(unittest.TestCase):
    """config.destructive_allowed() -- transport is the policy."""

    def _env(self, **overrides):
        env = {k: v for k, v in os.environ.items()
               if k not in _POLICY_VARS}
        env.update(overrides)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_stdio_allows(self):
        """The local desktop registration must keep working as before."""
        with self._env():
            self.assertTrue(config.destructive_allowed())
        with self._env(RM_MCP_TRANSPORT="stdio"):
            self.assertTrue(config.destructive_allowed())

    def test_streamable_http_denies(self):
        with self._env(RM_MCP_TRANSPORT="streamable-http"):
            self.assertFalse(config.destructive_allowed())

    def test_explicit_optin_overrides(self):
        for truthy in ("1", "true", "TRUE", "yes"):
            with self._env(RM_MCP_TRANSPORT="streamable-http",
                           RM_MCP_ALLOW_DESTRUCTIVE=truthy):
                self.assertTrue(config.destructive_allowed())

    def test_junk_optin_does_not_enable(self):
        for junk in ("0", "false", "no", "maybe", ""):
            with self._env(RM_MCP_TRANSPORT="streamable-http",
                           RM_MCP_ALLOW_DESTRUCTIVE=junk):
                self.assertFalse(config.destructive_allowed())


class TestDestructiveGuardWiring(unittest.TestCase):
    """The guard must sit on the execute path of both tools, after dry run."""

    REMOTE = {"RM_MCP_TRANSPORT": "streamable-http"}

    def test_delete_execute_refused_and_device_untouched(self):
        doc = "/00_Projects/104_Stacks/victim"
        with mock.patch.dict(os.environ, self.REMOTE), \
                mock.patch.object(manage.device, "ls") as ls, \
                mock.patch.object(manage.device, "rm") as rm:
            ls.return_value = [{"name": "victim", "type": "doc"}]
            result = manage.delete_impl(doc, dry_run=False,
                                        allow_anywhere=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["system"], "config")
        self.assertIn("disabled", result["error"]["message"])
        rm.assert_not_called()

    def test_move_execute_refused_and_device_untouched(self):
        with mock.patch.dict(os.environ, self.REMOTE), \
                mock.patch.object(manage.device, "mv") as mv:
            result = manage.move_impl("/00_Projects/104_Stacks/a",
                                      "/00_Projects/104_Stacks/b",
                                      dry_run=False, allow_anywhere=False)
        self.assertFalse(result["ok"])
        mv.assert_not_called()

    def test_dry_run_still_returns_the_plan_when_disabled(self):
        """Planning is read-only, so it stays available on the remote."""
        doc = "/00_Projects/104_Stacks/victim"
        with mock.patch.dict(os.environ, self.REMOTE), \
                mock.patch.object(manage.device, "ls") as ls:
            ls.return_value = [{"name": "victim", "type": "doc"}]
            result = manage.delete_impl(doc, dry_run=True,
                                        allow_anywhere=False)
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["dry_run"])
        self.assertEqual(result["data"]["plan"]["action"], "delete")

    def test_stdio_execute_still_reaches_the_device(self):
        """The guard must not break the local lane."""
        doc = "/00_Projects/104_Stacks/victim"
        env = {k: v for k, v in os.environ.items()
               if k not in _POLICY_VARS}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(manage.device, "ls") as ls, \
                mock.patch.object(manage.device, "rm") as rm:
            ls.return_value = [{"name": "victim", "type": "doc"}]
            result = manage.delete_impl(doc, dry_run=False,
                                        allow_anywhere=False)
        self.assertTrue(result["ok"])
        rm.assert_called_once_with(doc)

    def test_allow_anywhere_does_not_bypass_the_guard(self):
        """allow_anywhere widens the path scope, not the execute policy."""
        with mock.patch.dict(os.environ, self.REMOTE), \
                mock.patch.object(manage.device, "mv") as mv:
            result = manage.move_impl("/Science/a", "/Science/b",
                                      dry_run=False, allow_anywhere=True)
        self.assertFalse(result["ok"])
        mv.assert_not_called()


class TestAdminTokenIsTheOptIn(unittest.TestCase):
    """Minting an admin credential replaces the binary destructive toggle.

    The old RM_MCP_ALLOW_DESTRUCTIVE said "somebody may delete". An admin
    token says WHICH caller may, and leaves the read and write tokens unable
    to -- the whole point of scoping. The flag is still honoured so existing
    deployments keep working, and both paths are asserted here.
    """

    def _env(self, **overrides):
        env = {k: v for k, v in os.environ.items() if k not in _POLICY_VARS}
        env["RM_MCP_TRANSPORT"] = "streamable-http"
        env.update(overrides)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_admin_token_enables_execution(self):
        with self._env(RM_MCP_ADMIN_TOKEN="admin-secret"):
            self.assertTrue(config.destructive_allowed())

    def test_read_or_write_tokens_alone_do_not(self):
        with self._env(RM_MCP_READ_TOKEN="r", RM_MCP_WRITE_TOKEN="w"):
            self.assertFalse(config.destructive_allowed())

    def test_legacy_token_alone_does_not(self):
        """RM_MCP_AUTH_TOKEN without the flag never could delete. Unchanged."""
        with self._env(RM_MCP_AUTH_TOKEN="legacy"):
            self.assertFalse(config.destructive_allowed())

    def test_legacy_token_plus_flag_still_does(self):
        with self._env(RM_MCP_AUTH_TOKEN="legacy",
                       RM_MCP_ALLOW_DESTRUCTIVE="1"):
            self.assertTrue(config.destructive_allowed())
            self.assertTrue(authz.admin_token_configured())

    def test_guard_still_refuses_the_device_without_an_admin_token(self):
        """The second layer holds even if the HTTP gate were bypassed."""
        with self._env(RM_MCP_WRITE_TOKEN="w"),                 mock.patch.object(manage.device, "rm") as rm:
            result = manage.delete_impl("/00_Projects/104_Stacks/victim",
                                        dry_run=False, allow_anywhere=False)
        self.assertFalse(result["ok"])
        rm.assert_not_called()


class TestConstantTimeAuth(unittest.TestCase):
    """The token comparison must not short-circuit on the first differing byte.

    The comparison moved out of server.py into authz.granted_scope when the
    single shared secret became a scope table -- the property it has to keep
    is the same one, and now has to hold across SEVERAL configured tokens.
    """

    def test_authz_uses_compare_digest(self):
        source = (RM_MCP_DIR / "rm_mcp" / "authz.py").read_text(
            encoding="utf-8")
        self.assertIn("hmac.compare_digest(presented, token)", source)
        self.assertNotIn("presented == token", source)

    def test_no_stale_comparison_left_in_server(self):
        source = (RM_MCP_DIR / "rm_mcp" / "server.py").read_text(
            encoding="utf-8")
        self.assertNotIn("_expected_value", source)

    def test_granted_scope_does_not_short_circuit(self):
        """Every configured token is compared, whichever one matches.

        A `break` on the first match would make response time depend on which
        token was presented -- an oracle for ordering the table.
        """
        calls = []
        table = {b"read-token": authz.READ, b"write-token": authz.WRITE,
                 b"admin-token": authz.ADMIN}
        real = hmac.compare_digest

        def counting(a, b):
            calls.append(b)
            return real(a, b)

        with mock.patch.object(authz.hmac, "compare_digest", counting):
            self.assertEqual(authz.granted_scope(b"read-token", table),
                             authz.READ)
        self.assertEqual(len(calls), len(table))

    def test_compare_digest_semantics_match_equality(self):
        secret = "s3cret-token-value"
        self.assertTrue(hmac.compare_digest(secret, secret))
        for wrong in ("", "s", "s3cret-token-valu", "s3cret-token-value2",
                      "S3CRET-TOKEN-VALUE"):
            self.assertFalse(hmac.compare_digest(wrong, secret))


if __name__ == "__main__":
    unittest.main()
