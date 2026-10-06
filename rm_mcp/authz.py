"""Wire-level authorisation and abuse controls for the streamable-http lane.

stdio is untouched by every line of this file. A stdio server has no network
surface, no headers and no tokens: the client is a local desktop registration
the user launched themselves. Everything here is about the OTHER lane -- the
one deployed at a public URL, where the only thing between the internet and a
paired reMarkable account is a string in an environment variable.

WHY THE CHECKS LIVE AT THE HTTP LAYER AND NOT IN THE TOOLS
----------------------------------------------------------
The obvious design is: stash the caller's scope in a contextvar in the
middleware, read it inside the tool. That is WRONG here, and the way it is
wrong is silent.

FastMCP's streamable-http transport does not run tool handlers in the task
that served the HTTP request. StreamableHTTPSessionManager owns a task group
created in the app's lifespan; the first request of a session calls
`task_group.start(run_server)`, and every LATER request for that session is
routed into that already-running task. So a contextvar set per-request either
does not reach the handler at all, or -- worse -- reaches it frozen at the
value from whichever request happened to open the session. A session opened
with a write token would keep write scope for every subsequent request on it,
including ones presenting a read token. That is a privilege-escalation hole
that would pass a naive unit test.

So authorisation is decided BEFORE the ASGI app is entered, from the request
itself: parse the JSON-RPC body, read the tool name, compare the required
scope against the scope the presented token grants. Nothing about it depends
on task-local state surviving a hand-off it does not survive.

The tool-layer guard in manage.py stays as the second layer -- see
config.destructive_allowed(), which now asks whether this deployment has an
admin credential at all.

`tools/list` IS FILTERED TO THE CALLER'S SCOPE
-----------------------------------------------
The gate above decides what a token may CALL. On its own that leaves a read
token looking at a menu of write tools and collecting a 403 for each one it
tries -- correct, but it makes the agent driving it discover the boundary by
walking into it. So the response is filtered too: see filter_tool_list() here
and ToolListFilterMiddleware in wire.py, which rewrite the tools array on the
way out so a caller is only ever shown what it can actually use.

This is presentation, not enforcement, and the distinction matters. Hiding a
tool is not what stops it being called -- the scope gate is, and it would
still refuse the call if a client asked for a tool it never saw listed. Never
weaken the gate on the grounds that the list already hides the tool.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import threading
import time
from collections import deque
from typing import Any, Iterable

# -- scopes ------------------------------------------------------------------

READ, WRITE, ADMIN = 1, 2, 3

SCOPE_NAMES: dict[int, str] = {READ: "read", WRITE: "write", ADMIN: "admin"}

# Which scope each tool needs. The axis is BLAST RADIUS, not cost:
#
#   read   nothing on the device or in Zotero changes. Pulls and renders write
#          only to the server's own session dir, which is already bounded by
#          config.KEEP_OUTPUTS.
#   write  something is created or modified -- a device push, a folder, a
#          Zotero note. Recoverable: it adds, it does not remove.
#   admin  rm_move / rm_delete. The two tools that can unmake a library, and
#          the whole reason a leaked read token must not be a leaked write one.
#
# A tool missing from this table is treated as ADMIN (fail closed) AND makes
# verify_tools() refuse to start, so the mistake surfaces at deploy time
# rather than as an unguarded tool on a public endpoint.
TOOL_SCOPES: dict[str, int] = {
    # read -------------------------------------------------------------------
    "rm_health": READ,
    "rm_list": READ,
    "rm_diff": READ,
    "rm_page_ink": READ,
    "rm_page_image": READ,
    "rm_render": READ,
    "rm_flatten": READ,
    "rm_get_highlights": READ,
    "rm_interpret_page": READ,
    "rm_pull": READ,
    "rm_pull_project": READ,
    # The asynchronous pull: same scope as rm_pull_project, because it does the
    # same thing (writes only to the server's own session dir) in three calls.
    "rm_pull_project_start": READ,
    "rm_pull_project_status": READ,
    "rm_pull_project_fetch": READ,
    "rm_pull_notebook": READ,
    "rm_capture_todos": READ,
    # write ------------------------------------------------------------------
    "rm_ensure_project_folder": WRITE,
    "rm_push_pdf": WRITE,
    "rm_push_file": WRITE,
    "rm_push_image": WRITE,
    "rm_push_content": WRITE,
    "rm_create": WRITE,
    "rm_new_notebook": WRITE,
    "rm_push_dir": WRITE,
    "rm_push_reading": WRITE,
    "rm_write_note1": WRITE,
    # admin ------------------------------------------------------------------
    "rm_move": ADMIN,
    "rm_delete": ADMIN,
}

# Tools that rasterise pages or spend vision tokens. These are the RAM-heavy
# pipelines: each call allocates page bitmaps and leaves a per-call output dir
# resident until config's eviction reclaims it, and on Cloud Run that
# filesystem is RAM. A caller that stays under the general rate limit can
# still OOM an instance by hammering only these, so they get a second,
# tighter bucket of their own.
HEAVY_TOOLS: frozenset[str] = frozenset({
    "rm_render",
    "rm_page_image",
    "rm_interpret_page",
    "rm_flatten",
    "rm_get_highlights",
    "rm_pull",
    "rm_pull_project",
    # Only the START is heavy: it launches the render. status and fetch are
    # polling calls and must not be charged to the tight heavy bucket, or a
    # caller waiting on a job would rate-limit itself.
    "rm_pull_project_start",
    "rm_pull_notebook",
    "rm_push_content",
    "rm_create",
    "rm_push_image",
})

# Protocol methods that are not tool calls. initialize / tools/list / ping and
# the notification traffic are metadata about the connection, so they need no
# more than the lowest scope -- a read token must be able to complete a
# handshake or it cannot read anything either.
_NON_TOOL_METHOD_SCOPE = READ

# Matches config.destructive_allowed()'s set exactly. A wider set here would
# mean RM_MCP_ALLOW_DESTRUCTIVE=on granted admin at the wire while the
# tool-layer guard still refused it -- two answers to one question.
_TRUTHY = ("1", "true", "yes")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


# -- token table -------------------------------------------------------------

# Env var -> scope it grants. RM_MCP_AUTH_TOKEN is the pre-scoping name and is
# resolved separately (see _legacy_scope) because what it grants depends on
# the deprecated RM_MCP_ALLOW_DESTRUCTIVE toggle it used to pair with.
SCOPED_TOKEN_VARS: dict[str, int] = {
    "RM_MCP_READ_TOKEN": READ,
    "RM_MCP_WRITE_TOKEN": WRITE,
    "RM_MCP_ADMIN_TOKEN": ADMIN,
}

LEGACY_TOKEN_VAR = "RM_MCP_AUTH_TOKEN"

# Prepared, NOT yet applied. Flip to False only after the deploy sequence in
# the "retire the legacy RM_MCP_AUTH_TOKEN secret" to-do has actually run
# against the live revision, in this order:
#
#   1. `gcloud run services update rm-mcp --remove-secrets=RM_MCP_AUTH_TOKEN`
#      (drops the env binding -- Claude cannot run this; a human or an
#      allow-ruled Bash does)
#   2. redeploy / confirm the new revision serves 100% traffic
#   3. re-run `rm_health` and confirm `legacy_token_in_use` is false
#   4. flip this constant to False, redeploy this image
#   5. disable, then delete, the `rm-mcp-auth-token` Secret Manager secret
#
# Flipping it BEFORE step 3 would drop RM_MCP_AUTH_TOKEN's write grant while
# the live deployment might still be depending on it for auth -- the token
# table would go empty for anyone still presenting only the legacy secret,
# which on the streamable-http path is a self-inflicted 401 outage, not a
# security fix. tools/rm_cloud.py already prefers write > admin > read >
# legacy and needs no change either way (docstring there, and
# the cloud-lane notes).
ACCEPT_LEGACY_TOKEN = True


def _legacy_scope() -> int:
    """What RM_MCP_AUTH_TOKEN grants, preserving its old behaviour exactly.

    Before scoping, that one token could call every tool, and rm_move /
    rm_delete additionally required RM_MCP_ALLOW_DESTRUCTIVE=1. Mapping it to
    ADMIN only when that toggle is set reproduces both halves, so an existing
    deployment keeps working unchanged while new ones use scoped tokens.
    """
    return ADMIN if _flag("RM_MCP_ALLOW_DESTRUCTIVE") else WRITE


def token_table() -> dict[bytes, int]:
    """Configured token -> highest scope it grants, read at call time.

    Bytes, not str: hmac.compare_digest raises TypeError on a str containing
    non-ASCII, and the presented value comes off the wire where an attacker
    chooses the bytes.
    """
    table: dict[bytes, int] = {}
    for var, scope in SCOPED_TOKEN_VARS.items():
        value = os.environ.get(var, "").strip()
        if value:
            key = value.encode("utf-8")
            table[key] = max(scope, table.get(key, 0))
    if ACCEPT_LEGACY_TOKEN:
        legacy = os.environ.get(LEGACY_TOKEN_VAR, "").strip()
        if legacy:
            key = legacy.encode("utf-8")
            table[key] = max(_legacy_scope(), table.get(key, 0))
    return table


def admin_token_configured() -> bool:
    """Whether this deployment has minted a credential that may destroy.

    This is what replaces the binary RM_MCP_ALLOW_DESTRUCTIVE toggle as the
    opt-in: issuing an admin token IS the deliberate act, and unlike the
    toggle it also names WHICH caller may do it.
    """
    return ADMIN in token_table().values()


def granted_scope(presented: bytes, table: dict[bytes, int]) -> int | None:
    """Scope the presented secret grants, or None if it matches nothing.

    Compares against every configured token with no early exit. Short-
    circuiting on a match would make response time depend on which token was
    presented; comparing with `==` anywhere here would leak a matching prefix
    through timing. The loop's cost leaks only the NUMBER of configured
    tokens, which is not a secret.
    """
    granted: int | None = None
    for token, scope in table.items():
        if hmac.compare_digest(presented, token):
            granted = scope if granted is None else max(granted, scope)
    return granted


# -- request classification --------------------------------------------------

# Where the classifier leaves its verdict for the middlewares behind it, so
# the body is parsed exactly once per request. A private, dotted key: the ASGI
# scope is a shared dict and Starlette owns the undotted names in it.
SCOPE_KEY = "rm_mcp.call"

# Where the auth middleware leaves the scope the presented token bought, so
# the tools/list filter behind it can rewrite the menu to match. Set only
# after a token has been accepted, so its absence means "not authenticated
# here" and the filter declines to guess.
GRANTED_KEY = "rm_mcp.granted"

# Requests whose body we will buffer. Anything larger is refused with 413
# rather than read into memory -- the buffering happens before authentication,
# so an unbounded read here would be a trivial memory DoS for an anonymous
# caller. Tool arguments are paths and short strings; the only genuinely
# variable one is rm_push_content's markdown.
try:
    MAX_BODY_BYTES = max(1024, int(
        os.environ.get("RM_MCP_MAX_BODY_BYTES", 16 * 1024 * 1024)))
except ValueError:
    MAX_BODY_BYTES = 16 * 1024 * 1024


class Call:
    """What one HTTP request is asking for, as far as policy is concerned."""

    __slots__ = ("method", "tool", "required", "heavy")

    def __init__(self, method: str | None, tool: str | None,
                 required: int, heavy: bool) -> None:
        self.method = method
        self.tool = tool
        self.required = required
        self.heavy = heavy

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"Call(method={self.method!r}, tool={self.tool!r}, "
                f"required={SCOPE_NAMES.get(self.required)}, "
                f"heavy={self.heavy})")


def classify(body: bytes) -> Call:
    """Required scope for a JSON-RPC request body.

    Fails CLOSED in every direction it cannot read: an unparseable body, an
    unexpected shape, or a tool name absent from TOOL_SCOPES all demand ADMIN.
    A well-formed protocol message is never affected, and a malformed one from
    a low-scope caller is refused before it reaches the server rather than
    guessed at.
    """
    if not body:
        # GET (the SSE stream) and DELETE (session teardown) carry no body, so
        # there is no tool name to read. Neither touches the device: the worst
        # a read token can do with DELETE is end a session whose id it already
        # knows, which is a nuisance, not a path to the library.
        return Call(None, None, _NON_TOOL_METHOD_SCOPE, False)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return Call(None, None, ADMIN, False)

    # A JSON-RPC batch is a list. The request as a whole needs whatever its
    # most privileged member needs, and counts as heavy if any member is.
    messages = payload if isinstance(payload, list) else [payload]
    if not messages:
        return Call(None, None, ADMIN, False)

    required = _NON_TOOL_METHOD_SCOPE
    heavy = False
    method: str | None = None
    tool: str | None = None
    for message in messages:
        if not isinstance(message, dict):
            return Call(None, None, ADMIN, False)
        method = message.get("method") or method
        if message.get("method") != "tools/call":
            continue
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str):
            return Call(method, None, ADMIN, False)
        tool = name
        required = max(required, TOOL_SCOPES.get(name, ADMIN))
        heavy = heavy or name in HEAVY_TOOLS
    return Call(method, tool, required, heavy)


# -- tools/list filtering ----------------------------------------------------

def visible_tools(names: Iterable[str], granted: int) -> list[str]:
    """The subset of `names` a caller at `granted` scope may actually call.

    An unclassified tool needs ADMIN (the same fail-closed default classify()
    uses), so a tool added without a scope row disappears from every menu
    below admin rather than being advertised and then refused.
    """
    return [n for n in names if TOOL_SCOPES.get(n, ADMIN) <= granted]


def _filter_message(message: Any, granted: int) -> Any:
    """Rewrite one JSON-RPC response so result.tools fits the caller's scope.

    Returns a NEW object rather than mutating: the caller may hand us a
    payload it still holds a reference to, and a filtered menu leaking back
    into a shared structure is the kind of bug that shows up once, remotely,
    in a request that is already gone.

    Anything that is not a tools/list result -- an error response, a payload
    with no tools array, a shape we do not recognise -- passes through
    untouched. This function must never be the reason a response fails.
    """
    if not isinstance(message, dict):
        return message
    result = message.get("result")
    if not isinstance(result, dict):
        return message
    tools = result.get("tools")
    if not isinstance(tools, list):
        return message
    kept = [
        tool for tool in tools
        if not isinstance(tool, dict)
        or TOOL_SCOPES.get(tool.get("name"), ADMIN) <= granted
    ]
    if len(kept) == len(tools):
        return message
    return {**message, "result": {**result, "tools": kept}}


def filter_tool_list(body: bytes, granted: int) -> bytes:
    """Filter a tools/list response body, preserving however it was framed.

    The transport answers in one of two shapes depending on what the client
    accepted: a bare JSON object, or an SSE stream whose payload sits on a
    `data:` line. Rather than guess which, this walks the body line by line
    and rewrites only `data:` payloads, falling back to treating the whole
    body as JSON when there are none. The framing -- event names, ids, blank
    lines, CRLF vs LF -- is passed through byte for byte, because it is the
    transport's business and not ours.

    Fails SAFE, not closed: any body it cannot parse is returned unchanged.
    A caller seeing one tool too many is a cosmetic fault; a caller getting a
    corrupted response because a filter tried too hard is a broken server, and
    the scope gate is what actually refuses the call either way.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body

    def _rewrite(payload: str) -> str:
        try:
            parsed = json.loads(payload)
        except ValueError:
            return payload
        if isinstance(parsed, list):
            filtered: Any = [_filter_message(m, granted) for m in parsed]
        else:
            filtered = _filter_message(parsed, granted)
        if filtered == parsed:
            return payload
        return json.dumps(filtered, separators=(",", ":"))

    if "data:" not in text:
        return _rewrite(text).encode("utf-8")

    # Split on newlines but KEEP them, so CRLF framing survives the round trip.
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if not stripped.startswith("data:"):
            out.append(line)
            continue
        ending = line[len(stripped):]
        payload = stripped[len("data:"):]
        lead = payload[:len(payload) - len(payload.lstrip())]
        out.append(f"data:{lead}{_rewrite(payload.strip())}{ending}")
    return "".join(out).encode("utf-8")


# -- client address ----------------------------------------------------------

def client_ip(scope: dict) -> str | None:
    """The address policy should judge this request by.

    By default the socket peer, which no client can forge. A deployment behind
    a proxy (Cloud Run, any load balancer) sees the proxy's address there
    instead, so it must opt into X-Forwarded-For -- and then the only entry
    that is not attacker-supplied is the one the NEAREST TRUSTED proxy
    appended. So we count hops from the RIGHT, never take the leftmost value.

    On Cloud Run, Google appends the peer it saw to any client-supplied
    header: RM_MCP_TRUST_FORWARDED_FOR=1 with the default one hop is correct
    there. Trusting the header without a proxy in front, or with the hop count
    too high, hands an attacker the ability to name their own source address.
    """
    if _flag("RM_MCP_TRUST_FORWARDED_FOR"):
        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"x-forwarded-for", b"").decode("latin-1")
        chain = [part.strip() for part in raw.split(",") if part.strip()]
        if chain:
            try:
                hops = max(1, int(os.environ.get("RM_MCP_FORWARDED_HOPS", "1")))
            except ValueError:
                hops = 1
            if hops <= len(chain):
                return chain[-hops]
            # Fewer entries than declared hops means the chain is not the one
            # this deployment was configured for. Fall through to the peer
            # rather than trust a value from further left than allowed.
    client = scope.get("client")
    if client:
        return client[0]
    return None


def _parse_allowlist(raw: str) -> list[Any]:
    """Parse RM_MCP_ALLOWED_IPS into networks. Unparseable entries are fatal.

    A typo in an allowlist that silently drops the entry either locks the
    operator out or lets the world in, depending which entry it was. Neither
    should be discovered in production.
    """
    nets: list[Any] = []
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise SystemExit(
                f"RM_MCP_ALLOWED_IPS entry {entry!r} is not an IP address or "
                f"CIDR block: {exc}. Refusing to start with an allowlist that "
                "does not mean what it says.")
    return nets


def allowlist() -> list[Any]:
    """Configured networks, or an empty list meaning 'no allowlist'."""
    return _parse_allowlist(os.environ.get("RM_MCP_ALLOWED_IPS", ""))


def ip_allowed(address: str | None, nets: Iterable[Any]) -> bool:
    nets = list(nets)
    if not nets:
        return True
    if not address:
        # An allowlist is configured but the peer is unknowable (a unix socket,
        # or a proxy setup that strips it). Refusing is the only answer that
        # keeps the allowlist meaning what the operator wrote.
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in nets)


# -- rate limiting -----------------------------------------------------------

def _int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


class SlidingWindowLimiter:
    """Per-key sliding-window counter. In-process, single-instance.

    A sliding window rather than a fixed one because a fixed window lets twice
    the limit through across a boundary, which for the heavy bucket is the
    difference between a bounded instance and an OOM.

    HONEST ABOUT ITS REACH: this is per-process state. Two Cloud Run instances
    enforce the limit twice over, and a scale-out multiplies the ceiling by
    the instance count. It is a guard against a hammering client, not a quota
    system; a real quota needs shared state this server deliberately does not
    have.
    """

    # Cap on distinct keys tracked, so an attacker rotating source addresses
    # cannot grow this map without bound. Evicting the least recently seen key
    # can only ever forgive requests, never invent them.
    MAX_KEYS = 4096

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, now: float | None = None) -> float | None:
        """Record a hit. Returns None if allowed, else seconds to wait."""
        if self.limit <= 0:
            return None
        now = time.monotonic() if now is None else now
        cutoff = now - self.window
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= self.MAX_KEYS:
                    self._evict(cutoff)
                hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                # The oldest hit in the window is the one whose expiry frees a
                # slot. Round up so a client honouring Retry-After succeeds.
                return max(1.0, (hits[0] + self.window) - now)
            hits.append(now)
            return None

    def _evict(self, cutoff: float) -> None:
        """Drop keys with nothing left in the window; else drop the oldest.

        Caller holds the lock.
        """
        stale = [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]
        if not stale:
            stale = [min(self._hits, key=lambda k: self._hits[k][-1])]
        for key in stale:
            self._hits.pop(key, None)


def rate_limits() -> tuple[int, int, float]:
    """(general limit, heavy limit, window seconds) from the environment.

    Defaults are chosen to be invisible to an agent working normally and
    obvious to one hammering: an MCP session's handshake plus a burst of
    ordinary calls sits well inside 120/minute, while 12 renders or vision
    calls a minute is already more sustained page rasterisation than the
    device lane produces in real use. Set either to 0 to disable it.
    """
    window = float(_int_env("RM_MCP_RATE_WINDOW_SECONDS", 60, minimum=1))
    return (_int_env("RM_MCP_RATE_LIMIT", 120),
            _int_env("RM_MCP_HEAVY_RATE_LIMIT", 12),
            window)


# -- startup validation ------------------------------------------------------

def verify_tools(names: Iterable[str]) -> None:
    """Refuse to start if any registered tool has no declared scope.

    The same argument as surface.verify: a tool added without a row in
    TOOL_SCOPES would be gated at ADMIN and quietly stop working, or -- if the
    default were ever loosened -- quietly become reachable by a read token.
    Neither should be discovered from a bug report.
    """
    missing = sorted(set(names) - set(TOOL_SCOPES))
    if missing:
        raise SystemExit(
            f"tool(s) with no scope in authz.TOOL_SCOPES: "
            f"{', '.join(missing)}. Add each to the read/write/admin table "
            "before shipping.")


def validate_startup() -> dict[bytes, int]:
    """Check the wire config and return the token table. Raises SystemExit.

    Called only on the streamable-http path, where being wrong is a public
    endpoint with the wrong door on it.
    """
    table = token_table()
    if not table:
        legacy_hint = (f" (or the legacy {LEGACY_TOKEN_VAR})"
                       if ACCEPT_LEGACY_TOKEN else "")
        raise SystemExit(
            "no auth token configured, but RM_MCP_TRANSPORT=streamable-http "
            "-- refusing to start an unauthenticated server on the network. "
            "Set at least one of RM_MCP_READ_TOKEN, RM_MCP_WRITE_TOKEN, "
            f"RM_MCP_ADMIN_TOKEN{legacy_hint}.")

    # One secret in two scope variables is always a copy-paste error, and it
    # resolves to the HIGHER scope -- so a read token pasted into the write
    # slot silently becomes a write token. Fail rather than pick. Once legacy
    # acceptance is retired (ACCEPT_LEGACY_TOKEN=False) the var is dropped
    # from this check too -- a stray RM_MCP_AUTH_TOKEN left in the env at
    # that point grants nothing, so a collision with it is no longer a
    # meaningful copy-paste error to catch.
    checked_vars = ((*SCOPED_TOKEN_VARS, LEGACY_TOKEN_VAR) if ACCEPT_LEGACY_TOKEN
                    else tuple(SCOPED_TOKEN_VARS))
    seen: dict[str, str] = {}
    for var in checked_vars:
        value = os.environ.get(var, "").strip()
        if not value:
            continue
        if value in seen:
            raise SystemExit(
                f"{var} and {seen[value]} are set to the same secret. Scoped "
                "tokens must differ, or the lower scope is meaningless.")
        seen[value] = var

    allowlist()  # raises on a malformed entry, before anything is served
    return table


def status() -> dict:
    """Non-secret summary of the wire posture, for rm_health.

    Reports which scopes have a credential and whether the abuse controls are
    armed -- never a token, a length, or a prefix.
    """
    table = token_table()
    general, heavy, window = rate_limits()
    scopes = sorted({SCOPE_NAMES[s] for s in table.values()})
    return {
        "transport": os.environ.get("RM_MCP_TRANSPORT", "stdio"),
        "scopes_configured": scopes,
        "legacy_token_in_use": bool(
            os.environ.get(LEGACY_TOKEN_VAR, "").strip()),
        "ip_allowlist_entries": len(allowlist()),
        "trust_forwarded_for": _flag("RM_MCP_TRUST_FORWARDED_FOR"),
        "rate_limit_per_window": general,
        "heavy_rate_limit_per_window": heavy,
        "rate_window_seconds": int(window),
        "max_body_bytes": MAX_BODY_BYTES,
    }
