"""Network-lane wire tests on mcp 2.x -- in-process, no network, no device.

The REAL MCPServer app (server.mcp.streamable_http_app) is wrapped in the REAL
wire.harden chain and driven through Starlette's TestClient. Nothing in the
chain is mocked.

Covers: a legacy (2025-06-18) client handshake without Mcp-Session-Id under
stateless mode, server/discover not being refused by authz, tools/call scope
checks unchanged on legacy and modern requests, tools/list filtering when the
response carries resultType, and RM_MCP_STATELESS=0 restoring session ids.

Run with the rm-mcp venv: .venv/Scripts/python.exe -m pytest tests/test_mcp2_wire.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

RM_MCP_DIR = Path(__file__).resolve().parent.parent
if str(RM_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(RM_MCP_DIR))

from starlette.testclient import TestClient  # noqa: E402

from rm_mcp import authz, server, wire  # noqa: E402

_WIRE_VARS = frozenset({
    "RM_MCP_TRANSPORT", "RM_MCP_ALLOW_DESTRUCTIVE", "RM_MCP_AUTH_TOKEN",
    "RM_MCP_ALLOWED_IPS", "RM_MCP_TRUST_FORWARDED_FOR", "RM_MCP_FORWARDED_HOPS",
    "RM_MCP_RATE_LIMIT", "RM_MCP_HEAVY_RATE_LIMIT", "RM_MCP_STATELESS",
    "RM_MCP_RATE_WINDOW_SECONDS", "RM_MCP_MAX_BODY_BYTES",
    *authz.SCOPED_TOKEN_VARS,
})
TOKENS = {"RM_MCP_READ_TOKEN": "read-secret",
          "RM_MCP_WRITE_TOKEN": "write-secret",
          "RM_MCP_ADMIN_TOKEN": "admin-secret"}
LEGACY = "2025-06-18"
MODERN = "2026-07-28"


def env(**overrides):
    base = {k: v for k, v in os.environ.items() if k not in _WIRE_VARS}
    base.update(TOKENS)
    base.update(overrides)
    return mock.patch.dict(os.environ, base, clear=True)


def rpc(method: str, params: dict | None = None, id_: int | None = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return msg


def decode(resp) -> dict:
    """JSON or SSE-framed body -> the JSON-RPC message."""
    text = resp.text
    if "data:" in text and not text.lstrip().startswith("{"):
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
    return json.loads(text)


def headers(token: str, version: str = LEGACY, **extra) -> dict:
    h = {"x-api-key": token, "content-type": "application/json",
         "accept": "application/json, text/event-stream",
         "mcp-protocol-version": version}
    h.update(extra)
    return h


def build_app():
    return wire.harden(server.mcp.streamable_http_app(
        host="127.0.0.1", stateless_http=server.stateless_http()))


INIT = rpc("initialize", {
    "protocolVersion": LEGACY, "capabilities": {},
    "clientInfo": {"name": "t", "version": "0"}})


class TestStatelessLegacyClient(unittest.TestCase):

    def test_legacy_handshake_and_read_call_without_session_id(self):
        with env():
            self.assertTrue(server.stateless_http())
            with TestClient(build_app(), base_url="http://127.0.0.1:8000") as c:
                h = headers("read-secret")
                r = c.post("/mcp", json=INIT, headers=h)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertNotIn("mcp-session-id", r.headers)
                r = c.post("/mcp", json=rpc("notifications/initialized", id_=None),
                           headers=h)
                self.assertIn(r.status_code, (200, 202), r.text)
                r = c.post("/mcp", json=rpc("tools/list", {}, 2), headers=h)
                self.assertEqual(r.status_code, 200, r.text)
                names = {t["name"] for t in decode(r)["result"]["tools"]}
                self.assertIn("rm_health", names)
                r = c.post("/mcp", json=rpc(
                    "tools/call", {"name": "rm_health", "arguments": {}}, 3),
                    headers=h)
                self.assertEqual(r.status_code, 200, r.text)
                msg = decode(r)
                self.assertIn("result", msg, msg)

    def test_stateless_off_restores_session_ids(self):
        with env(RM_MCP_STATELESS="0"):
            self.assertFalse(server.stateless_http())
            with TestClient(build_app(), base_url="http://127.0.0.1:8000") as c:
                r = c.post("/mcp", json=INIT, headers=headers("read-secret"))
                self.assertEqual(r.status_code, 200, r.text)
                self.assertTrue(r.headers.get("mcp-session-id"))
        for off in ("false", "no", "off"):
            with env(RM_MCP_STATELESS=off):
                self.assertFalse(server.stateless_http())


class TestMethodScopes(unittest.TestCase):

    def test_server_discover_not_refused_for_read_token(self):
        with env():
            with TestClient(build_app(), base_url="http://127.0.0.1:8000") as c:
                r = c.post("/mcp", json=rpc("server/discover", {}),
                           headers=headers("read-secret", MODERN))
                self.assertNotIn(r.status_code, (401, 403), r.text)

    def test_explicit_rows_are_read(self):
        for m in ("server/discover", "subscriptions/listen", "resources/list",
                  "resources/read", "prompts/list", "prompts/get",
                  "completion/complete", "logging/setLevel"):
            body = json.dumps(rpc(m, {})).encode()
            self.assertEqual(authz.classify(body).required, authz.READ, m)

    def test_unknown_method_still_only_read(self):
        # A method-not-found must reach the server, not a 401/403.
        body = json.dumps(rpc("made/up", {})).encode()
        self.assertEqual(authz.classify(body).required, authz.READ)


class TestToolCallScopeUnchanged(unittest.TestCase):

    CALL = rpc("tools/call", {"name": "rm_push_file", "arguments": {"path": "a"}})

    def test_write_tool_with_read_token_legacy(self):
        with env():
            with TestClient(build_app(), base_url="http://127.0.0.1:8000") as c:
                r = c.post("/mcp", json=self.CALL, headers=headers("read-secret"))
                self.assertEqual(r.status_code, 403, r.text)

    def test_write_tool_with_read_token_modern_header(self):
        with env():
            with TestClient(build_app(), base_url="http://127.0.0.1:8000") as c:
                r = c.post("/mcp", json=self.CALL, headers=headers(
                    "read-secret", MODERN, **{"mcp-name": "rm_push_file"}))
                self.assertEqual(r.status_code, 403, r.text)

    def test_mcp_name_header_cannot_downgrade_the_check(self):
        # The tool in the body decides, whatever Mcp-Name claims.
        with env():
            with TestClient(build_app(), base_url="http://127.0.0.1:8000") as c:
                r = c.post("/mcp", json=self.CALL, headers=headers(
                    "read-secret", MODERN, **{"mcp-name": "rm_health"}))
                self.assertEqual(r.status_code, 403, r.text)


class TestToolListFilterWithResultType(unittest.TestCase):

    def _terminal(self, framing: str):
        payload = {"jsonrpc": "2.0", "id": 1, "result": {
            "resultType": "complete",
            "tools": [{"name": "rm_list", "inputSchema": {}},
                      {"name": "rm_push_file", "inputSchema": {}},
                      {"name": "rm_delete", "inputSchema": {}}]}}
        raw = json.dumps(payload)
        body = (f"event: message\ndata: {raw}\n\n" if framing == "sse" else raw)
        ctype = b"text/event-stream" if framing == "sse" else b"application/json"

        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", ctype)]})
            await send({"type": "http.response.body", "body": body.encode()})
        return app

    def _list(self, token: str, framing: str) -> dict:
        with env():
            c = TestClient(wire.harden(self._terminal(framing)),
                           base_url="http://127.0.0.1:8000")
            r = c.post("/mcp", json=rpc("tools/list", {}),
                       headers=headers(token, MODERN))
            self.assertEqual(r.status_code, 200, r.text)
            return decode(r)["result"]

    def test_read_token_sees_only_read_tools_json_and_sse(self):
        for framing in ("json", "sse"):
            res = self._list("read-secret", framing)
            self.assertEqual([t["name"] for t in res["tools"]], ["rm_list"],
                             framing)
            self.assertEqual(res.get("resultType"), "complete", framing)

    def test_admin_token_sees_everything(self):
        res = self._list("admin-secret", "json")
        self.assertEqual(len(res["tools"]), 3)


if __name__ == "__main__":
    unittest.main()
