"""
The edge of the web service: what every request is checked for before a handler sees
it, and what every response carries on the way out.

The box sits on a LAN behind one login, and most of the time that is the whole threat
model. These are for the rest of the time -- a box reachable from further away than the
living room, a browser that visited a hostile page on the same network, a client that
sends more than the service should ever read -- and each one costs the stock appliance
nothing: the headers are inert to the page that ships, the CSRF checks pass every
request the page makes, the body limit is far above the largest upload the page
offers, and the shared-secret gate is off unless RIPARR_AUTH_TOKEN is set.

Four things, in the order a request meets them:

  1. a body limit     RIPARR_MAX_BODY_BYTES (default 64 MiB): a Content-Length over it
                      is refused with 413 before anything is read, and a streamed body
                      that grows past it is cut off at the byte
  2. the gate         RIPARR_AUTH_TOKEN, when set: every /api/* request needs either
                      `Authorization: Bearer <token>` or the gate cookie that a login
                      carrying the token earns, except the handful the page needs
                      before signing in -- which say only that a gate is up
  3. CSRF             a POST, PUT, PATCH or DELETE to /api/* that is not bearer-
                      authenticated must come from this origin: Sec-Fetch-Site may not
                      say cross-site, an Origin header must match the Host, and the
                      request must carry the X-Riparr-Request header the page's own
                      client sends (a form, an image tag or a top-level navigation
                      cannot). The session cookie is already SameSite=Lax; this is the
                      half that holds when it is not
  4. the headers      Content-Security-Policy (scripts and connections to this origin
                      only, no framing), nosniff, same-origin referrer, no camera or
                      microphone, same-origin opener

docs/hardened-deployment.md puts them together with a loopback bind and a proxy.
"""
import hmac
import json
import os
from urllib.parse import urlsplit

from starlette.exceptions import HTTPException

from . import platform as P

DEFAULT_MAX_BODY = 64 * 1024 * 1024
GATE_COOKIE = "riparr_gate"
GATE_MAX_AGE = 60 * 60 * 24 * 30
REQUEST_HEADER = b"x-riparr-request"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# What the page has to be able to ask before anyone has signed in or presented the
# token: whether an account exists, who it is, and the two ways in.
GATE_OPEN_PATHS = {"/api/setup/state", "/api/auth/me", "/api/auth/login", "/api/setup/user"}

CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; "
       "img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
       "font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
       "frame-ancestors 'none'")
HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]


def gate_token():
    """The shared secret, or "" when the gate is off. Read every time, so a test -- or
    a unit restarted with a new environment -- sees the value in force."""
    return (os.environ.get("RIPARR_AUTH_TOKEN") or "").strip()


def max_body():
    try:
        return int(os.environ.get("RIPARR_MAX_BODY_BYTES") or DEFAULT_MAX_BODY)
    except ValueError:
        return DEFAULT_MAX_BODY


def cookie_secure():
    """Whether cookies carry the Secure flag. Off by default: the appliance speaks
    plain HTTP on the LAN, and a Secure cookie over HTTP is a cookie the browser
    never sends back. On behind a TLS proxy or `tailscale serve`."""
    return P.env_flag("RIPARR_COOKIE_SECURE", False)


def token_matches(presented):
    want = gate_token()
    if not want or not isinstance(presented, str):
        return False
    return hmac.compare_digest(presented.encode(), want.encode())


def _headers(scope):
    """The request headers as {name: value}, built once per request."""
    return {k: v.decode("latin-1") for k, v in scope.get("headers") or []}


def _too_large(limit):
    return "That is larger than this box accepts (%d bytes)." % limit


async def _reply(send, status, detail):
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())] + HEADERS})
    await send({"type": "http.response.body", "body": body})


def _bearer_ok(headers):
    """True when the request authenticates with the gate token in a bearer header."""
    auth = headers.get(b"authorization") or ""
    return auth.lower().startswith("bearer ") and token_matches(auth[7:].strip())


def _gate_cookie_ok(headers, verify):
    """True when the request carries a gate cookie that `verify` accepts."""
    for part in (headers.get(b"cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == GATE_COOKIE and v:
            return bool(verify(v))
    return False


def gate_passed(scope):
    """The middleware's verdict for this request: did it present the token, by bearer
    header or by gate cookie? Recorded once in the scope so the handlers that are open
    before the gate (setup/state) can shape their answer without deciding again."""
    return bool(scope.get("riparr.gate_ok"))


def _same_origin(headers):
    """Is this request from this origin? None when the browser said nothing either
    way; True/False when it did."""
    sfs = (headers.get(b"sec-fetch-site") or "").lower()
    if sfs:
        # "same-site" (a sibling host under the same registrable domain) counts as
        # another origin: the page is never served that way.
        return sfs in ("same-origin", "none")
    origin = headers.get(b"origin")
    if origin is None:
        return None
    try:
        netloc = urlsplit(origin).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc.lower() == (headers.get(b"host") or "").lower()


class _TooLarge(HTTPException):
    """Raised from inside receive() when a streamed body passes the limit. A Starlette
    HTTPException on purpose: FastAPI turns any other exception raised while it reads a
    body into a 400 "error parsing the body", which is the wrong answer; this one it
    re-raises, and the app's own exception handling turns it into the 413."""

    def __init__(self, limit):
        super().__init__(status_code=413, detail=_too_large(limit))


class Edge:
    """The ASGI middleware. `verify_gate` turns a gate cookie's value into truth."""

    def __init__(self, app, verify_gate):
        self.app = app
        self.verify_gate = verify_gate

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path") or ""
        method = (scope.get("method") or "GET").upper()
        api = path.startswith("/api/")
        headers = _headers(scope)

        # 1. the body limit, from the declared length first
        limit = max_body()
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    return await _reply(send, 413, _too_large(limit))
            except ValueError:
                return await _reply(send, 400, "Bad Content-Length")

        # 2. the gate. The token is looked at only when something needs it: a GET of a
        # static file on an ungated box costs no header scan and no compare.
        token = gate_token()
        bearer = False
        if api and (token or method in UNSAFE_METHODS):
            bearer = _bearer_ok(headers)
        if token and api:
            scope["riparr.gate_ok"] = bearer or _gate_cookie_ok(headers, self.verify_gate)
            if not scope["riparr.gate_ok"] and path not in GATE_OPEN_PATHS:
                return await _reply(send, 401, "This box needs its access token. Sign in "
                                    "with it, or send it as a bearer token.")

        # 3. CSRF, for anything that changes state from a browser
        if api and method in UNSAFE_METHODS and not bearer:
            if _same_origin(headers) is False:
                return await _reply(send, 403, "Cross-site request refused.")
            if REQUEST_HEADER not in headers:
                return await _reply(send, 403, "State-changing requests need the "
                                    "X-Riparr-Request header (the page sends it), or "
                                    "a bearer token.")

        # 1 again: a streamed body that grows past the limit
        total = 0
        started = False

        async def limited_receive():
            nonlocal total
            msg = await receive()
            if msg["type"] == "http.request":
                total += len(msg.get("body") or b"")
                if total > limit:
                    raise _TooLarge(limit)
            return msg

        # 4. the headers, on every response that goes out
        async def send_with_headers(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
                hdrs = list(msg.get("headers") or [])
                have = {k.lower() for k, _ in hdrs}
                hdrs += [(k, v) for k, v in HEADERS if k not in have]
                msg = dict(msg, headers=hdrs)
            await send(msg)

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except _TooLarge:
            if not started:
                await _reply(send, 413, _too_large(limit))
