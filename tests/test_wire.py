"""Offline tests for the streamable-http guard chain -- no network, no device.

Covers the four properties the network lane now claims:

  1. token SCOPING -- a read token cannot push and cannot delete, a write
     token cannot delete, and the legacy single token still behaves exactly
     as it did before scoping existed;
  2. IP ALLOWLISTING -- addresses outside RM_MCP_ALLOWED_IPS are dropped, and
     X-Forwarded-For is only believed when the deploy says a proxy is there
     (and then only from the right, never the client-supplied leftmost entry);
  3. RATE LIMITING -- a general bucket and a tighter one for the RAM-heavy
     render/vision tools, both sliding-window;
  4. the chain FAILS CLOSED -- an unclassifiable body, an unknown tool, and a
     missing classifier all demand admin rather than defaulting open.

The middlewares are exercised as ASGI apps directly: build a scope, drive
them with a fake receive/send, assert the status. No server, no uvicorn.

Run: python -m pytest rm-mcp/tests/test_wire.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))

from rm_mcp import authz, wire  # noqa: E402

# Stripped from the inherited environment for every test, so a developer
# machine with a real token or allowlist exported cannot change a verdict.
_WIRE_VARS = frozenset({
    "RM_MCP_TRANSPORT", "RM_MCP_ALLOW_DESTRUCTIVE", "RM_MCP_AUTH_TOKEN",
    "RM_MCP_ALLOWED_IPS", "RM_MCP_TRUST_FORWARDED_FOR", "RM_MCP_FORWARDED_HOPS",
    "RM_MCP_RATE_LIMIT", "RM_MCP_HEAVY_RATE_LIMIT",
    "RM_MCP_RATE_WINDOW_SECONDS", "RM_MCP_MAX_BODY_BYTES",
    *authz.SCOPED_TOKEN_VARS,
})


def clean_env(**overrides):
    env = {k: v for k, v in os.environ.items() if k not in _WIRE_VARS}
    env.update(overrides)
    return mock.patch.dict(os.environ, env, clear=True)


# -- ASGI harness -------------------------------------------------------------

class _Sink:
    """Collects an ASGI response and records whether the app was reached."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[bytes, bytes] = {}
        self.body = b""
        self.reached = False
        self.received_body: bytes | None = None

    async def send(self, message: dict) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = dict(message.get("headers") or [])
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")

    def app(self):
        """A terminal ASGI app that records that it was entered."""
        async def _app(scope, receive, send):
            self.reached = True
            # Drain the replayed body so the buffering path is exercised.
            chunks = []
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    break
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            self.received_body = b"".join(chunks)
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        return _app


async def _ok_app(scope, receive, send):
    """Terminal app for chains driven more than once (rate-limit floods)."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def http_scope(path: str = "/mcp", client: str | None = "203.0.113.9",
               headers: dict[str, str] | None = None) -> dict:
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1"))
           for k, v in (headers or {}).items()]
    return {
        "type": "http",
        "path": path,
        "method": "POST",
        "headers": raw,
        "client": (client, 54321) if client else None,
    }


def body_receive(body: bytes):
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}
    return receive


def tool_call(name: str, **arguments) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode("utf-8")


def run_chain(scope: dict, body: bytes, sink: _Sink, app=None) -> _Sink:
    """Drive the full hardened chain once and return the sink."""
    chain = wire.harden(app or sink.app())
    asyncio.run(chain(scope, body_receive(body), sink.send))
    return sink


# -- 1. token scoping ---------------------------------------------------------

class TestTokenScoping(unittest.TestCase):

    TOKENS = {"RM_MCP_READ_TOKEN": "read-secret",
              "RM_MCP_WRITE_TOKEN": "write-secret",
              "RM_MCP_ADMIN_TOKEN": "admin-secret"}

    def _call(self, token: str | None, body: bytes) -> _Sink:
        headers = {"x-api-key": token} if token is not None else {}
        sink = _Sink()
        with clean_env(**self.TOKENS):
            run_chain(http_scope(headers=headers), body, sink)
        return sink

    def test_read_token_may_read(self):
        sink = self._call("read-secret", tool_call("rm_list", path="/x"))
        self.assertEqual(sink.status, 200)
        self.assertTrue(sink.reached)

    def test_read_token_may_not_push(self):
        sink = self._call("read-secret", tool_call("rm_push_file", path="a"))
        self.assertEqual(sink.status, 403)
        self.assertFalse(sink.reached)
        self.assertIn(b"requires write scope", sink.body)

    def test_read_token_may_not_delete(self):
        """The headline property: a leaked read token cannot wipe the library."""
        sink = self._call("read-secret", tool_call("rm_delete", path="/x"))
        self.assertEqual(sink.status, 403)
        self.assertFalse(sink.reached)

    def test_write_token_may_not_delete(self):
        """A write token adds; only admin removes."""
        sink = self._call("write-secret", tool_call("rm_delete", path="/x"))
        self.assertEqual(sink.status, 403)
        self.assertFalse(sink.reached)
        self.assertIn(b"requires admin scope", sink.body)

    def test_write_token_may_push_and_read(self):
        for body in (tool_call("rm_push_file", path="a"),
                     tool_call("rm_list", path="/x")):
            sink = self._call("write-secret", body)
            self.assertEqual(sink.status, 200)

    def test_admin_token_may_do_everything(self):
        for name in ("rm_list", "rm_push_file", "rm_delete", "rm_move"):
            sink = self._call("admin-secret", tool_call(name))
            self.assertEqual(sink.status, 200, name)

    def test_unknown_token_is_401_not_403(self):
        """401 = wrong credential, 403 = under-privileged one. Operators need
        to be able to tell those apart from the response alone."""
        sink = self._call("not-a-token", tool_call("rm_list"))
        self.assertEqual(sink.status, 401)
        self.assertFalse(sink.reached)

    def test_missing_header_is_401(self):
        sink = self._call(None, tool_call("rm_list"))
        self.assertEqual(sink.status, 401)

    def test_bearer_prefix_is_accepted(self):
        sink = self._call("Bearer read-secret", tool_call("rm_list"))
        self.assertEqual(sink.status, 200)

    def test_handshake_needs_only_read(self):
        """A read token that cannot initialize cannot read either."""
        for method in ("initialize", "tools/list", "ping"):
            body = json.dumps({"jsonrpc": "2.0", "id": 1,
                               "method": method}).encode()
            sink = self._call("read-secret", body)
            self.assertEqual(sink.status, 200, method)

    def test_non_ascii_token_does_not_crash_the_comparison(self):
        """compare_digest raises TypeError on non-ASCII str -- the presented
        value is attacker-chosen, so the comparison works on bytes."""
        sink = self._call("tøken-é", tool_call("rm_list"))
        self.assertEqual(sink.status, 401)


class TestLegacyTokenCompatibility(unittest.TestCase):
    """RM_MCP_AUTH_TOKEN must keep behaving exactly as it did before scoping."""

    def _call(self, body: bytes, **env) -> _Sink:
        sink = _Sink()
        with clean_env(RM_MCP_AUTH_TOKEN="legacy-secret", **env):
            run_chain(http_scope(headers={"x-api-key": "legacy-secret"}),
                      body, sink)
        return sink

    def test_legacy_token_reads_and_writes(self):
        for name in ("rm_list", "rm_push_file"):
            self.assertEqual(self._call(tool_call(name)).status, 200, name)

    def test_legacy_token_cannot_delete_without_the_flag(self):
        self.assertEqual(self._call(tool_call("rm_delete")).status, 403)

    def test_legacy_flag_restores_destructive_access(self):
        sink = self._call(tool_call("rm_delete"), RM_MCP_ALLOW_DESTRUCTIVE="1")
        self.assertEqual(sink.status, 200)


class TestStartupValidation(unittest.TestCase):

    def test_no_token_refuses_to_start(self):
        with clean_env(RM_MCP_TRANSPORT="streamable-http"):
            with self.assertRaises(SystemExit) as ctx:
                authz.validate_startup()
        self.assertIn("RM_MCP_READ_TOKEN", str(ctx.exception))

    def test_any_single_scoped_token_is_enough(self):
        with clean_env(RM_MCP_READ_TOKEN="r"):
            self.assertEqual(authz.validate_startup(), {b"r": authz.READ})

    def test_duplicate_secret_across_scopes_is_fatal(self):
        """The same string in two slots resolves to the higher scope, so a
        read token pasted into the write slot silently becomes a write token."""
        with clean_env(RM_MCP_READ_TOKEN="same", RM_MCP_WRITE_TOKEN="same"):
            with self.assertRaises(SystemExit):
                authz.validate_startup()

    def test_malformed_allowlist_is_fatal_at_startup(self):
        with clean_env(RM_MCP_READ_TOKEN="r",
                       RM_MCP_ALLOWED_IPS="203.0.113.0/24,not-an-ip"):
            with self.assertRaises(SystemExit) as ctx:
                authz.validate_startup()
        self.assertIn("not-an-ip", str(ctx.exception))

    def test_status_never_leaks_a_token(self):
        with clean_env(RM_MCP_READ_TOKEN="read-secret",
                       RM_MCP_ADMIN_TOKEN="admin-secret"):
            reported = json.dumps(authz.status())
        self.assertNotIn("read-secret", reported)
        self.assertNotIn("admin-secret", reported)
        self.assertIn("read", json.loads(reported)["scopes_configured"])
        self.assertIn("admin", json.loads(reported)["scopes_configured"])

    def test_every_registered_tool_has_a_scope(self):
        """The guard that stops a new tool shipping unclassified."""
        from rm_mcp import server
        authz.verify_tools(server.mcp.registered)   # must not raise
        self.assertGreaterEqual(len(server.mcp.registered), 6)

    def test_no_scope_entry_outlives_its_tool(self):
        """The REVERSE of the guard above, and the reason it was needed.

        verify_tools only checks that every registered tool has a scope. It says
        nothing about a scope entry whose tool has gone -- so when
        rm_triage_inbox was withdrawn on 2026-09-16 with the Inbox lane it
        served, its `"rm_triage_inbox": READ` entry stayed in TOOL_SCOPES and
        the suite went green. A dead name in the authz table is not harmless:
        it is the table a reader trusts to enumerate the wire surface.

        Full build only. The public tree drops the private lane, so its
        TOOL_SCOPES legitimately names tools that are not registered there.
        """
        from rm_mcp.config import TOOLS_DIR
        from _toolpath import tool_script
        if not tool_script(TOOLS_DIR, "rm_pull.py").is_file():
            self.skipTest("public build: the private lane is absent by design")
        from rm_mcp import server
        orphans = sorted(set(authz.TOOL_SCOPES) - set(server.mcp.registered))
        self.assertEqual(orphans, [],
                         "TOOL_SCOPES classifies tools that no longer exist")

    def test_verify_tools_rejects_an_unclassified_tool(self):
        with self.assertRaises(SystemExit) as ctx:
            authz.verify_tools(["rm_list", "rm_brand_new"])
        self.assertIn("rm_brand_new", str(ctx.exception))

    def test_destructive_tools_are_admin_only(self):
        """A regression fence: neither may ever be demoted by accident."""
        for name in ("rm_move", "rm_delete"):
            self.assertEqual(authz.TOOL_SCOPES[name], authz.ADMIN, name)


# -- 2. IP allowlisting -------------------------------------------------------

class TestIpAllowlist(unittest.TestCase):

    def _call(self, client: str | None, allowed: str,
              headers: dict[str, str] | None = None, **env) -> _Sink:
        sink = _Sink()
        hdrs = {"x-api-key": "admin-secret", **(headers or {})}
        with clean_env(RM_MCP_ADMIN_TOKEN="admin-secret",
                       RM_MCP_ALLOWED_IPS=allowed, **env):
            run_chain(http_scope(client=client, headers=hdrs),
                      tool_call("rm_list"), sink)
        return sink

    def test_unset_allowlist_admits_everyone(self):
        sink = self._call("198.51.100.7", "")
        self.assertEqual(sink.status, 200)

    def test_address_in_range_is_admitted(self):
        sink = self._call("203.0.113.9", "203.0.113.0/24")
        self.assertEqual(sink.status, 200)

    def test_address_outside_range_is_dropped(self):
        sink = self._call("198.51.100.7", "203.0.113.0/24")
        self.assertEqual(sink.status, 403)
        self.assertFalse(sink.reached)

    def test_bare_address_entry_works(self):
        self.assertEqual(self._call("203.0.113.9", "203.0.113.9").status, 200)
        self.assertEqual(self._call("203.0.113.8", "203.0.113.9").status, 403)

    def test_ipv6_cidr(self):
        self.assertEqual(self._call("2001:db8::1", "2001:db8::/32").status, 200)
        self.assertEqual(self._call("2001:db9::1", "2001:db8::/32").status, 403)

    def test_unknown_peer_is_refused_when_an_allowlist_is_set(self):
        """An allowlist that cannot see the peer must not admit everyone."""
        sink = self._call(None, "203.0.113.0/24")
        self.assertEqual(sink.status, 403)

    def test_forwarded_for_is_ignored_unless_trusted(self):
        """Otherwise any caller names their own source address."""
        sink = self._call("198.51.100.7", "203.0.113.0/24",
                          headers={"x-forwarded-for": "203.0.113.9"})
        self.assertEqual(sink.status, 403)

    def test_forwarded_for_is_read_from_the_right_when_trusted(self):
        """The trusted proxy APPENDS what it saw; entries to its left are
        whatever the client sent. One hop means the last entry."""
        sink = self._call("10.0.0.1", "203.0.113.0/24",
                          headers={"x-forwarded-for": "1.2.3.4, 203.0.113.9"},
                          RM_MCP_TRUST_FORWARDED_FOR="1")
        self.assertEqual(sink.status, 200)

    def test_spoofed_leftmost_entry_does_not_get_in(self):
        sink = self._call("10.0.0.1", "203.0.113.0/24",
                          headers={"x-forwarded-for": "203.0.113.9, 198.51.100.7"},
                          RM_MCP_TRUST_FORWARDED_FOR="1")
        self.assertEqual(sink.status, 403)

    def test_hop_count_selects_further_left(self):
        sink = self._call("10.0.0.1", "203.0.113.0/24",
                          headers={"x-forwarded-for": "1.2.3.4, 203.0.113.9, 10.0.0.2"},
                          RM_MCP_TRUST_FORWARDED_FOR="1",
                          RM_MCP_FORWARDED_HOPS="2")
        self.assertEqual(sink.status, 200)

    def test_short_chain_falls_back_to_the_peer(self):
        """Fewer entries than declared hops means this is not the chain the
        deployment was configured for -- do not read further left than allowed."""
        with clean_env(RM_MCP_TRUST_FORWARDED_FOR="1",
                       RM_MCP_FORWARDED_HOPS="3"):
            resolved = authz.client_ip(http_scope(
                client="10.0.0.1", headers={"x-forwarded-for": "1.2.3.4"}))
        self.assertEqual(resolved, "10.0.0.1")

    def test_health_endpoint_stays_reachable(self):
        """A platform health probe has no credential and an arbitrary source."""
        sink = _Sink()
        with clean_env(RM_MCP_ADMIN_TOKEN="admin-secret",
                       RM_MCP_ALLOWED_IPS="203.0.113.0/24"):
            run_chain(http_scope(path="/health", client="198.51.100.7"),
                      b"", sink)
        self.assertEqual(sink.status, 200)
        self.assertTrue(sink.reached)


# -- 3. rate limiting ---------------------------------------------------------

class TestSlidingWindowLimiter(unittest.TestCase):

    def test_allows_up_to_the_limit_then_refuses(self):
        limiter = authz.SlidingWindowLimiter(limit=3, window=60)
        for i in range(3):
            self.assertIsNone(limiter.check("k", now=1000 + i))
        self.assertIsNotNone(limiter.check("k", now=1002))

    def test_window_slides_rather_than_resetting(self):
        """A fixed window lets 2x the limit through across a boundary."""
        limiter = authz.SlidingWindowLimiter(limit=2, window=10)
        limiter.check("k", now=1000)
        limiter.check("k", now=1005)
        self.assertIsNotNone(limiter.check("k", now=1009))
        # 1000 has aged out by 1011; exactly one slot frees, not both.
        self.assertIsNone(limiter.check("k", now=1011))
        self.assertIsNotNone(limiter.check("k", now=1011))

    def test_keys_are_independent(self):
        limiter = authz.SlidingWindowLimiter(limit=1, window=60)
        self.assertIsNone(limiter.check("a", now=1000))
        self.assertIsNone(limiter.check("b", now=1000))

    def test_zero_limit_disables(self):
        limiter = authz.SlidingWindowLimiter(limit=0, window=60)
        for i in range(50):
            self.assertIsNone(limiter.check("k", now=1000 + i))

    def test_retry_after_is_at_least_one_second(self):
        limiter = authz.SlidingWindowLimiter(limit=1, window=60)
        limiter.check("k", now=1000)
        self.assertGreaterEqual(limiter.check("k", now=1059.9), 1.0)

    def test_key_count_is_bounded(self):
        """An attacker rotating source addresses must not grow this map."""
        limiter = authz.SlidingWindowLimiter(limit=5, window=60)
        limiter.MAX_KEYS = 8
        for i in range(200):
            limiter.check(f"key-{i}", now=1000 + i)
        self.assertLessEqual(len(limiter._hits), 8)


class TestRateLimitMiddleware(unittest.TestCase):

    def _flood(self, name: str, count: int, **env) -> list[int]:
        """Drive one chain `count` times, so the limiter state accumulates."""
        statuses = []
        with clean_env(RM_MCP_ADMIN_TOKEN="admin-secret", **env):
            chain = wire.harden(_ok_app)
            for _ in range(count):
                sink = _Sink()
                asyncio.run(chain(
                    http_scope(headers={"x-api-key": "admin-secret"}),
                    body_receive(tool_call(name)), sink.send))
                statuses.append(sink.status)
        return statuses

    def test_general_bucket_sheds_a_flood(self):
        statuses = self._flood("rm_list", 8, RM_MCP_RATE_LIMIT="5",
                               RM_MCP_HEAVY_RATE_LIMIT="0")
        self.assertEqual(statuses[:5], [200] * 5)
        self.assertEqual(statuses[5:], [429] * 3)

    def test_heavy_bucket_is_tighter_than_the_general_one(self):
        """The RAM-heavy pipelines are capped well below the general limit:
        a caller politely under 100/min can still OOM an instance with
        nothing but rm_render."""
        statuses = self._flood("rm_render", 6, RM_MCP_RATE_LIMIT="100",
                               RM_MCP_HEAVY_RATE_LIMIT="2")
        self.assertEqual(statuses[:2], [200, 200])
        self.assertTrue(all(s == 429 for s in statuses[2:]))

    def test_light_tools_are_not_charged_to_the_heavy_bucket(self):
        statuses = self._flood("rm_list", 6, RM_MCP_RATE_LIMIT="100",
                               RM_MCP_HEAVY_RATE_LIMIT="2")
        self.assertEqual(statuses, [200] * 6)

    def test_429_carries_retry_after(self):
        with clean_env(RM_MCP_ADMIN_TOKEN="admin-secret", RM_MCP_RATE_LIMIT="1"):
            chain = wire.harden(_ok_app)
            for _ in range(2):
                sink = _Sink()
                asyncio.run(chain(
                    http_scope(headers={"x-api-key": "admin-secret"}),
                    body_receive(tool_call("rm_list")), sink.send))
        self.assertEqual(sink.status, 429)
        self.assertIn(b"retry-after", sink.headers)

    def test_health_is_never_rate_limited(self):
        with clean_env(RM_MCP_ADMIN_TOKEN="admin-secret", RM_MCP_RATE_LIMIT="1"):
            chain = wire.harden(_ok_app)
            statuses = []
            for _ in range(5):
                sink = _Sink()
                asyncio.run(chain(http_scope(path="/health"),
                                  body_receive(b""), sink.send))
                statuses.append(sink.status)
        self.assertEqual(statuses, [200] * 5)

    def test_heavy_set_covers_the_render_and_vision_pipelines(self):
        for name in ("rm_render", "rm_page_image", "rm_interpret_page",
                     "rm_flatten"):
            self.assertIn(name, authz.HEAVY_TOOLS, name)


# -- 4. fail-closed classification -------------------------------------------

class TestClassificationFailsClosed(unittest.TestCase):

    def test_unknown_tool_requires_admin(self):
        call = authz.classify(tool_call("rm_some_future_tool"))
        self.assertEqual(call.required, authz.ADMIN)

    def test_unparseable_body_requires_admin(self):
        for body in (b"{not json", b"\xff\xfe\x00", b"[]"):
            self.assertEqual(authz.classify(body).required, authz.ADMIN)

    def test_missing_tool_name_requires_admin(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"arguments": {}}}).encode()
        self.assertEqual(authz.classify(body).required, authz.ADMIN)

    def test_batch_takes_the_highest_scope_in_it(self):
        """A read call bundled with a delete is a delete."""
        batch = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "rm_list"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "rm_delete"}},
        ]).encode()
        call = authz.classify(batch)
        self.assertEqual(call.required, authz.ADMIN)

    def test_batch_is_heavy_if_any_member_is(self):
        batch = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "rm_list"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "rm_render"}},
        ]).encode()
        self.assertTrue(authz.classify(batch).heavy)

    def test_read_token_cannot_smuggle_a_delete_inside_a_batch(self):
        batch = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "rm_list"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "rm_delete"}},
        ]).encode()
        sink = _Sink()
        with clean_env(RM_MCP_READ_TOKEN="read-secret"):
            run_chain(http_scope(headers={"x-api-key": "read-secret"}),
                      batch, sink)
        self.assertEqual(sink.status, 403)
        self.assertFalse(sink.reached)

    def test_auth_alone_fails_closed_without_the_classifier(self):
        """Defence in depth: a chain assembled without BodyClassifier must
        not wave a read token through on the strength of an absent verdict."""
        sink = _Sink()
        with clean_env(RM_MCP_READ_TOKEN="read-secret"):
            app = wire.ScopedTokenAuthMiddleware(sink.app())
            asyncio.run(app(http_scope(headers={"x-api-key": "read-secret"}),
                            body_receive(tool_call("rm_list")), sink.send))
        self.assertEqual(sink.status, 403)
        self.assertFalse(sink.reached)


# -- body handling ------------------------------------------------------------

class TestBodyHandling(unittest.TestCase):

    def test_body_is_replayed_intact_to_the_app(self):
        """The classifier consumes the body; the app must still receive it."""
        body = tool_call("rm_push_content", content="x" * 5000)
        sink = _Sink()
        with clean_env(RM_MCP_ADMIN_TOKEN="admin-secret"):
            run_chain(http_scope(headers={"x-api-key": "admin-secret"}),
                      body, sink)
        self.assertEqual(sink.status, 200)
        self.assertEqual(sink.received_body, body)

    def test_chunked_body_is_reassembled(self):
        body = tool_call("rm_list")
        parts = [body[:10], body[10:]]
        sink = _Sink()

        async def receive():
            if parts:
                chunk = parts.pop(0)
                return {"type": "http.request", "body": chunk,
                        "more_body": bool(parts)}
            return {"type": "http.disconnect"}

        with clean_env(RM_MCP_ADMIN_TOKEN="admin-secret"):
            chain = wire.harden(sink.app())
            asyncio.run(chain(http_scope(headers={"x-api-key": "admin-secret"}),
                              receive, sink.send))
        self.assertEqual(sink.received_body, body)

    def test_receive_delegates_upstream_after_the_body(self):
        """The regression that unit tests missed and a live run caught.

        A streaming response keeps calling receive() to watch for the client
        hanging up. The first version of _replay answered every such call with
        a fabricated http.disconnect, so every tools/call returned 200 with an
        EMPTY event stream. The message after the body must come from the real
        upstream receive, not be invented here.
        """
        seen = []
        upstream_calls = []

        async def upstream():
            if not upstream_calls:
                upstream_calls.append("body")
                return {"type": "http.request", "body": tool_call("rm_list"),
                        "more_body": False}
            upstream_calls.append("wait")
            # A marker the middleware could not have fabricated.
            return {"type": "http.disconnect", "from_upstream": True}

        async def app(scope, receive, send):
            seen.append(await receive())     # the replayed body
            seen.append(await receive())     # must reach upstream
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": b""})

        sink = _Sink()
        with clean_env(RM_MCP_ADMIN_TOKEN="admin-secret"):
            chain = wire.BodyClassifierMiddleware(app)
            asyncio.run(chain(http_scope(), upstream, sink.send))

        self.assertEqual(seen[0]["type"], "http.request")
        self.assertEqual(seen[0]["body"], tool_call("rm_list"))
        self.assertTrue(seen[1].get("from_upstream"),
                        "second receive() was fabricated, not delegated")
        self.assertEqual(upstream_calls, ["body", "wait"])

    def test_oversized_body_is_refused_before_authentication(self):
        """The buffering sits in front of the token check, so it is bounded --
        an unbounded read there is a memory DoS any anonymous caller can run."""
        sink = _Sink()
        with clean_env(RM_MCP_ADMIN_TOKEN="admin-secret"):
            app = wire.BodyClassifierMiddleware(sink.app(), max_bytes=1024)
            app = wire.IpAllowlistMiddleware(app)
            asyncio.run(app(http_scope(), body_receive(b"x" * 5000), sink.send))
        self.assertEqual(sink.status, 413)
        self.assertFalse(sink.reached)

    def test_lifespan_passes_through_untouched(self):
        """The session manager starts its task group in lifespan -- a guard
        that swallowed it would break the server rather than protect it."""
        seen = []

        async def app(scope, receive, send):
            seen.append(scope["type"])

        with clean_env(RM_MCP_ADMIN_TOKEN="admin-secret",
                       RM_MCP_ALLOWED_IPS="203.0.113.0/24"):
            chain = wire.harden(app)
            asyncio.run(chain({"type": "lifespan"}, body_receive(b""),
                              _Sink().send))
        self.assertEqual(seen, ["lifespan"])


# -- 5. tools/list scope filtering -------------------------------------------

def tools_list_body() -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": "tools/list", "params": {}}).encode()


def sse(payload: dict) -> bytes:
    return ("event: message\r\ndata: " + json.dumps(payload)
            + "\r\n\r\n").encode()


def tools_result(*names: str) -> dict:
    return {"jsonrpc": "2.0", "id": 1,
            "result": {"tools": [{"name": n, "description": f"does {n}"}
                                 for n in names]}}


ALL_SIX = ("rm_health", "rm_list", "rm_page_ink", "rm_page_image",
           "rm_push_file", "rm_push_content")


class TestToolListFiltering(unittest.TestCase):
    """A caller is shown only the tools its token can actually call."""

    TOKENS = {"RM_MCP_READ_TOKEN": "read-secret",
              "RM_MCP_WRITE_TOKEN": "write-secret",
              "RM_MCP_ADMIN_TOKEN": "admin-secret"}

    def _listed(self, token: str, names=ALL_SIX, framing=sse) -> list[str]:
        """Drive tools/list through the real chain; return the tools shown."""
        sink = _Sink()

        async def app(scope, receive, send):
            while True:                      # drain the replayed body
                message = await receive()
                if message["type"] != "http.request":
                    break
                if not message.get("more_body", False):
                    break
            body = framing(tools_result(*names))
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            await send({"type": "http.response.body", "body": body})

        with clean_env(**self.TOKENS):
            chain = wire.harden(app)
            asyncio.run(chain(http_scope(headers={"x-api-key": token}),
                              body_receive(tools_list_body()), sink.send))
        self.assertEqual(sink.status, 200)
        text = sink.body.decode()
        payload = json.loads(text.split("data:", 1)[1] if "data:" in text
                             else text)
        return [t["name"] for t in payload["result"]["tools"]]

    def test_read_token_sees_only_read_tools(self):
        shown = self._listed("read-secret")
        self.assertEqual(shown, ["rm_health", "rm_list", "rm_page_ink",
                                 "rm_page_image"])
        self.assertNotIn("rm_push_file", shown)

    def test_write_token_sees_read_and_write(self):
        self.assertEqual(sorted(self._listed("write-secret")),
                         sorted(ALL_SIX))

    def test_admin_token_sees_destructive_tools(self):
        shown = self._listed("admin-secret", names=(*ALL_SIX, "rm_delete"))
        self.assertIn("rm_delete", shown)

    def test_write_token_does_not_see_destructive_tools(self):
        shown = self._listed("write-secret", names=(*ALL_SIX, "rm_delete"))
        self.assertNotIn("rm_delete", shown)

    def test_unclassified_tool_is_hidden_below_admin(self):
        """Fail closed: a tool with no scope row must not be advertised."""
        shown = self._listed("read-secret", names=(*ALL_SIX, "rm_future_tool"))
        self.assertNotIn("rm_future_tool", shown)

    def test_plain_json_framing_also_filtered(self):
        shown = self._listed(
            "read-secret", framing=lambda p: json.dumps(p).encode())
        self.assertNotIn("rm_push_file", shown)

    def test_sse_framing_is_preserved_byte_for_byte(self):
        """The event name, CRLF and blank-line terminator are the transport's
        business -- filtering the payload must not reframe the stream."""
        sink = _Sink()

        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body",
                        "body": sse(tools_result(*ALL_SIX))})

        with clean_env(**self.TOKENS):
            chain = wire.harden(app)
            asyncio.run(chain(http_scope(headers={"x-api-key": "read-secret"}),
                              body_receive(tools_list_body()), sink.send))
        text = sink.body.decode()
        self.assertTrue(text.startswith("event: message\r\ndata: "))
        self.assertTrue(text.endswith("\r\n\r\n"))

    def test_content_length_is_corrected_when_declared(self):
        sink = _Sink()
        payload = json.dumps(tools_result(*ALL_SIX)).encode()

        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length",
                                     str(len(payload)).encode())]})
            await send({"type": "http.response.body", "body": payload})

        with clean_env(**self.TOKENS):
            chain = wire.harden(app)
            asyncio.run(chain(http_scope(headers={"x-api-key": "read-secret"}),
                              body_receive(tools_list_body()), sink.send))
        declared = int(sink.headers[b"content-length"])
        self.assertEqual(declared, len(sink.body))
        self.assertLess(declared, len(payload))

    def test_sse_gets_no_invented_content_length(self):
        """Declaring a length on a length-less stream ends it early."""
        sink = _Sink()

        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            await send({"type": "http.response.body",
                        "body": sse(tools_result(*ALL_SIX))})

        with clean_env(**self.TOKENS):
            chain = wire.harden(app)
            asyncio.run(chain(http_scope(headers={"x-api-key": "read-secret"}),
                              body_receive(tools_list_body()), sink.send))
        self.assertNotIn(b"content-length", sink.headers)

    def test_chunked_response_is_reassembled_before_filtering(self):
        sink = _Sink()
        body = sse(tools_result(*ALL_SIX))

        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": body[:20],
                        "more_body": True})
            await send({"type": "http.response.body", "body": body[20:],
                        "more_body": False})

        with clean_env(**self.TOKENS):
            chain = wire.harden(app)
            asyncio.run(chain(http_scope(headers={"x-api-key": "read-secret"}),
                              body_receive(tools_list_body()), sink.send))
        text = sink.body.decode()
        shown = [t["name"] for t in
                 json.loads(text.split("data:", 1)[1])["result"]["tools"]]
        self.assertNotIn("rm_push_file", shown)

    def test_error_response_passes_through_untouched(self):
        sink = _Sink()

        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 500,
                        "headers": []})
            await send({"type": "http.response.body", "body": b"boom"})

        with clean_env(**self.TOKENS):
            chain = wire.harden(app)
            asyncio.run(chain(http_scope(headers={"x-api-key": "read-secret"}),
                              body_receive(tools_list_body()), sink.send))
        self.assertEqual(sink.status, 500)
        self.assertEqual(sink.body, b"boom")


class TestFilterIsTightlyScoped(unittest.TestCase):
    """Everything that is not an authenticated tools/list streams untouched."""

    def test_tool_call_response_is_never_buffered(self):
        """rm_page_image returns base64 PNGs. Buffering those to inspect them
        would recreate the RAM pressure the heavy bucket exists to prevent."""
        sink = _Sink()
        seen_more_body = []
        big = b"x" * 200_000

        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": big,
                        "more_body": True})
            await send({"type": "http.response.body", "body": b"tail",
                        "more_body": False})

        async def watching_send(message):
            if message["type"] == "http.response.body":
                seen_more_body.append(message.get("more_body", False))
            await sink.send(message)

        with clean_env(RM_MCP_ADMIN_TOKEN="admin-secret"):
            chain = wire.harden(app)
            asyncio.run(chain(
                http_scope(headers={"x-api-key": "admin-secret"}),
                body_receive(tool_call("rm_page_image")), watching_send))
        # Two body messages arrived separately -- the stream was relayed, not
        # collected. A buffering filter would have emitted exactly one.
        self.assertEqual(seen_more_body, [True, False])
        self.assertEqual(sink.body, big + b"tail")

    def test_unauthenticated_request_is_not_filtered(self):
        """No token means no scope to filter by -- and a 401 before the app."""
        sink = _Sink()
        with clean_env(RM_MCP_READ_TOKEN="read-secret"):
            run_chain(http_scope(headers={"x-api-key": "wrong"}),
                      tools_list_body(), sink)
        self.assertEqual(sink.status, 401)
        self.assertFalse(sink.reached)


class TestVisibleTools(unittest.TestCase):

    def test_matches_the_scope_table(self):
        names = sorted(authz.TOOL_SCOPES)
        self.assertNotIn("rm_delete", authz.visible_tools(names, authz.WRITE))
        self.assertIn("rm_delete", authz.visible_tools(names, authz.ADMIN))
        self.assertEqual(
            authz.visible_tools(names, authz.READ),
            sorted(n for n, s in authz.TOOL_SCOPES.items() if s == authz.READ))

    def test_unparseable_body_is_returned_unchanged(self):
        """Fails SAFE: a corrupted response is worse than one extra menu row,
        and the scope gate refuses the call either way."""
        for body in (b"{not json", b"\xff\xfe", b"event: ping\r\n\r\n"):
            self.assertEqual(authz.filter_tool_list(body, authz.READ), body)

    def test_non_tools_payload_is_returned_unchanged(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "result": {"content": []}}).encode()
        self.assertEqual(authz.filter_tool_list(body, authz.READ), body)

    def test_does_not_mutate_the_caller_payload(self):
        original = tools_result(*ALL_SIX)
        snapshot = json.dumps(original, sort_keys=True)
        authz.filter_tool_list(json.dumps(original).encode(), authz.READ)
        self.assertEqual(json.dumps(original, sort_keys=True), snapshot)


if __name__ == "__main__":
    unittest.main()
