"""ASGI middleware chain for the network-exposed (streamable-http) lane.

Four guards, applied outermost-first, each cheap enough to sit in front of the
next:

    1. IpAllowlistMiddleware   drop unrecognised source addresses outright
    2. BodyClassifierMiddleware  buffer the JSON-RPC body ONCE, decide what
                                 this request is asking for
    3. RateLimitMiddleware     two buckets: everything, and the RAM-heavy tools
    4. ScopedTokenAuthMiddleware  which scope did the presented token buy

The order is the point. An address that is not allowed never gets its body
read; a body that is too large is refused before anything parses it; a flood
is shed before the token comparison; and the token comparison happens before
the MCP app is entered at all. The policy decisions themselves live in
authz.py -- this file is the plumbing that applies them.

Responses are written directly to `send` rather than through Starlette. These
guards must work on the leanest possible install, and building a 401 by hand
is four lines.

stdio never reaches any of this: see server.main(), which only wraps the app
when RM_MCP_TRANSPORT=streamable-http.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from . import authz

# Unauthenticated on purpose: a container platform's health probe has no
# credential to present, and the endpoint returns a fixed {"status": "ok"}
# with nothing about the device, the config or the tokens in it. Exempting it
# from the allowlist too keeps a probe working from whatever address the
# platform happens to use.
EXEMPT_PATHS = frozenset({"/health"})

Send = Callable[[dict], Awaitable[None]]
Receive = Callable[[], Awaitable[dict]]


async def _text_response(send: Send, status: int, body: str,
                         headers: list[tuple[bytes, bytes]] | None = None
                         ) -> None:
    """Minimal plain-text ASGI response, no framework required."""
    payload = body.encode("utf-8")
    base = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(payload)).encode("latin-1")),
    ]
    await send({"type": "http.response.start", "status": status,
                "headers": base + (headers or [])})
    await send({"type": "http.response.body", "body": payload})


def _exempt(scope: dict) -> bool:
    """True for traffic no guard should touch.

    Non-http scopes (lifespan above all) must pass through untouched: the
    streamable-http session manager starts its task group in lifespan, so a
    guard that swallowed it would break the server rather than protect it.
    """
    return scope.get("type") != "http" or scope.get("path") in EXEMPT_PATHS


class IpAllowlistMiddleware:
    """Refuse requests whose source address is not on RM_MCP_ALLOWED_IPS.

    Unset (the default) means no allowlist and no behaviour change. Where it
    is set, this is the cheapest possible drop: no body read, no token
    comparison, nothing allocated per request beyond the parsed networks.

    Parsed once at construction, so a malformed entry is a startup failure
    (authz.validate_startup calls the same parser) rather than a per-request
    exception, and so a long list is not re-parsed on every request.
    """

    def __init__(self, app) -> None:
        self._app = app
        self._nets = authz.allowlist()

    async def __call__(self, scope: dict, receive: Receive, send: Send) -> None:
        if _exempt(scope) or not self._nets:
            await self._app(scope, receive, send)
            return
        address = authz.client_ip(scope)
        if not authz.ip_allowed(address, self._nets):
            # No echo of the address that was refused: the response goes to
            # whoever sent it, and telling them what we resolved them to is a
            # free probe of the proxy configuration.
            await _text_response(send, 403, "forbidden")
            return
        await self._app(scope, receive, send)


class BodyClassifierMiddleware:
    """Buffer the request body once and record what the request is asking for.

    Both the rate limiter (is this a heavy tool?) and the auth gate (what
    scope does it need?) need the JSON-RPC method, and an ASGI body can only
    be consumed once -- so it is read here, classified here, and replayed to
    everything downstream.

    The buffering happens BEFORE authentication, which is why it is bounded:
    an unbounded read in front of the token check is a memory DoS any
    anonymous caller can run. Over authz.MAX_BODY_BYTES the request is
    refused with 413 without the oversized body ever being assembled.
    """

    def __init__(self, app, max_bytes: int | None = None) -> None:
        self._app = app
        self._max = authz.MAX_BODY_BYTES if max_bytes is None else max_bytes

    async def __call__(self, scope: dict, receive: Receive, send: Send) -> None:
        if _exempt(scope):
            await self._app(scope, receive, send)
            return
        body, overflowed = await self._buffer(receive)
        if overflowed:
            await _text_response(
                send, 413,
                f"request body exceeds {self._max} bytes "
                "(RM_MCP_MAX_BODY_BYTES)")
            return
        scope[authz.SCOPE_KEY] = authz.classify(body)
        await self._app(scope, _replay(body, receive), send)

    async def _buffer(self, receive: Receive) -> tuple[bytes, bool]:
        """Read the whole body, or bail the moment it passes the cap."""
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # http.disconnect: the client is gone. Return what we have and
                # let the app below deal with the closed connection.
                break
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self._max:
                return b"", True
            if chunk:
                chunks.append(chunk)
            if not message.get("more_body", False):
                break
        return b"".join(chunks), False


def _replay(body: bytes, upstream: Receive) -> Receive:
    """Hand the buffered body over once, then DELEGATE to the real receive.

    The delegation is not a nicety, it is the whole correctness of this
    middleware. The first version returned a fabricated `http.disconnect` for
    every call after the body, on the reasoning that the body was the only
    thing left to deliver. It is not: a streaming response keeps calling
    receive() to watch for the client hanging up, and an instant disconnect
    tells it the client already has.

    The effect was invisible to unit tests and fatal in practice -- every
    tools/call authenticated correctly, returned 200, and then delivered an
    EMPTY event stream, with uvicorn logging "ASGI callable returned without
    completing response". Caught only by driving the real server over HTTP.

    Awaiting upstream blocks until the client genuinely disconnects, which is
    exactly the signal the app is waiting for.
    """
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await upstream()

    return receive


class RateLimitMiddleware:
    """Two sliding-window buckets per client: everything, and the heavy tools.

    The general bucket is the ordinary flood guard. The heavy one exists
    because the expensive failure is not request volume, it is RAM: page
    rasterisation and vision interpretation each allocate bitmaps and leave a
    per-call output dir resident (config.KEEP_OUTPUTS bounds the count, not
    the peak), and on a RAM-backed container filesystem a caller staying
    politely under the general limit can still walk an instance into an OOM
    using nothing but rm_render.

    Sits in front of authentication deliberately: an unauthenticated flood is
    the flood most worth shedding, and the token comparison is not free.
    Keyed by source address, so a deployment behind a proxy that has not opted
    into RM_MCP_TRUST_FORWARDED_FOR shares one bucket across all callers --
    documented in .env.example, and the reason the defaults are generous.
    """

    def __init__(self, app, general: int | None = None,
                 heavy: int | None = None, window: float | None = None) -> None:
        self._app = app
        cfg_general, cfg_heavy, cfg_window = authz.rate_limits()
        window = cfg_window if window is None else window
        self._general = authz.SlidingWindowLimiter(
            cfg_general if general is None else general, window)
        self._heavy = authz.SlidingWindowLimiter(
            cfg_heavy if heavy is None else heavy, window)

    async def __call__(self, scope: dict, receive: Receive, send: Send) -> None:
        if _exempt(scope):
            await self._app(scope, receive, send)
            return
        key = authz.client_ip(scope) or "unknown"
        retry = self._general.check(key)
        if retry is None:
            call = scope.get(authz.SCOPE_KEY)
            if call is not None and call.heavy:
                retry = self._heavy.check(f"heavy:{key}")
        if retry is not None:
            await _text_response(
                send, 429, "rate limit exceeded",
                [(b"retry-after", str(int(retry)).encode("latin-1"))])
            return
        await self._app(scope, receive, send)


class ScopedTokenAuthMiddleware:
    """Require a token whose scope covers what the request is asking for.

    Replaces the single shared secret. 401 means the presented value matched
    no configured token; 403 means it matched one that does not reach far
    enough -- the distinction an operator needs to tell a wrong credential
    from an under-privileged one.

    The token table is read per request rather than captured at construction.
    That costs a dict rebuild per request and buys two things: a test can
    patch the environment without rebuilding the chain, and a process that
    reloads its own environment picks up a rotated secret without a restart.
    """

    def __init__(self, app, header_name: str = "x-api-key") -> None:
        self._app = app
        self._header = header_name.lower().encode("latin-1")

    async def __call__(self, scope: dict, receive: Receive, send: Send) -> None:
        if _exempt(scope):
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        presented = headers.get(self._header, b"")
        if presented.startswith(b"Bearer "):
            presented = presented[len(b"Bearer "):]
        granted = authz.granted_scope(presented, authz.token_table())
        if granted is None:
            await _text_response(send, 401, "unauthorized")
            return
        # A request that never reached the classifier (a chain built without
        # it) must not be waved through on the strength of a read token.
        call = scope.get(authz.SCOPE_KEY) or authz.Call(None, None,
                                                        authz.ADMIN, False)
        if granted < call.required:
            need = authz.SCOPE_NAMES[call.required]
            have = authz.SCOPE_NAMES[granted]
            target = call.tool or call.method or "this request"
            await _text_response(
                send, 403,
                f"forbidden: {target} requires {need} scope, token has {have}")
            return
        # Hand the resolved scope to the tools/list filter behind us. Only set
        # once a token has been accepted, so its absence downstream means "not
        # authenticated" rather than "read".
        scope[authz.GRANTED_KEY] = granted
        await self._app(scope, receive, send)


class ToolListFilterMiddleware:
    """Show a caller only the tools its token can actually call.

    The scope gate in front of this decides what may be CALLED; without this,
    a read token still sees the whole menu and learns the boundary by
    collecting a 403 per write tool. Filtering the response makes the menu
    tell the truth.

    PRESENTATION, NOT ENFORCEMENT. Hiding a tool is not what stops it being
    called -- ScopedTokenAuthMiddleware is, and it refuses a tool this filter
    never listed just the same. Never trade one for the other.

    SCOPED AS TIGHTLY AS POSSIBLE ON PURPOSE. It buffers the response body,
    and most responses here must never be buffered: rm_page_image returns
    base64 PNGs, and holding those in memory to inspect them would recreate
    the exact RAM pressure the heavy rate-limit bucket exists to prevent. So
    it engages only for a `tools/list` request that authenticated, and every
    other byte the server sends passes straight through untouched.
    """

    METHOD = "tools/list"

    def __init__(self, app) -> None:
        self._app = app

    def _applies(self, scope: dict) -> bool:
        if _exempt(scope):
            return False
        call = scope.get(authz.SCOPE_KEY)
        return (call is not None and call.method == self.METHOD
                and scope.get(authz.GRANTED_KEY) is not None)

    async def __call__(self, scope: dict, receive: Receive, send: Send) -> None:
        if not self._applies(scope):
            await self._app(scope, receive, send)
            return
        granted = scope[authz.GRANTED_KEY]
        # The start message is HELD rather than forwarded: filtering changes
        # the body length, so content-length cannot be sent until the new body
        # exists. Anything that is not a 200 is released immediately and the
        # body streams through unread.
        held: dict | None = None
        passthrough = False
        chunks: list[bytes] = []

        async def capture(message: dict) -> None:
            nonlocal held, passthrough
            if passthrough:
                await send(message)
                return
            if message["type"] == "http.response.start":
                if message.get("status") != 200:
                    passthrough = True
                    await send(message)
                else:
                    held = message
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = authz.filter_tool_list(b"".join(chunks), granted)
            start = held or {"type": "http.response.start", "status": 200,
                             "headers": []}
            headers = [(k, v) for k, v in (start.get("headers") or [])
                       if k.lower() != b"content-length"]
            # Only re-declare a length the original response declared. An SSE
            # stream is deliberately length-less, and inventing one for it
            # would tell the client the stream has ended.
            if any(k.lower() == b"content-length"
                   for k, _ in (start.get("headers") or [])):
                headers.append(
                    (b"content-length", str(len(body)).encode("latin-1")))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body,
                        "more_body": False})

        await self._app(scope, receive, capture)


def harden(app: Any, header_name: str = "x-api-key") -> Any:
    """Wrap the MCP app in the full guard chain, outermost guard first.

    ToolListFilterMiddleware sits INSIDE the auth gate, so it only ever runs
    on a request that presented a valid token and can read the scope that
    token bought.
    """
    app = ToolListFilterMiddleware(app)
    app = ScopedTokenAuthMiddleware(app, header_name=header_name)
    app = RateLimitMiddleware(app)
    app = BodyClassifierMiddleware(app)
    app = IpAllowlistMiddleware(app)
    return app
