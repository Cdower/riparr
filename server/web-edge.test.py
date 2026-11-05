#!/usr/bin/env python3
"""The edge of the web service: body limit, access-token gate, CSRF checks, headers.

Drives the ASGI app directly with hand-built scopes -- no network, no uvicorn, no
httpx -- so each check is a few milliseconds and the assertions are about exactly what
went in and came out.

Run: python3 server/web-edge.test.py
"""
import asyncio
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
tmp = tempfile.mkdtemp()
os.environ["RIPARR_DB"] = os.path.join(tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"
os.environ.pop("RIPARR_AUTH_TOKEN", None)
os.environ.pop("RIPARR_COOKIE_SECURE", None)
os.environ.pop("RIPARR_MAX_BODY_BYTES", None)

from riparr import db, main, web as WEB   # noqa: E402

db.init()
app = main.app
failures = []


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(name)


def request(method, path, headers=None, body=b"", chunks=None, content_length=None):
    """(status, headers dict, body bytes) for one request through the whole app."""
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if body and content_length is None:
        content_length = len(body)
    if content_length is not None:
        hdrs.append((b"content-length", str(content_length).encode()))
    if chunks is None:
        chunks = [body]
    scope = {"type": "http", "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": hdrs, "server": ("127.0.0.1", 9797), "client": ("127.0.0.1", 1)}
    pending = list(chunks)
    out = {"status": None, "headers": [], "body": b""}

    async def receive():
        if pending:
            c = pending.pop(0)
            return {"type": "http.request", "body": c, "more_body": bool(pending)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = [(k.decode(), v.decode()) for k, v in msg["headers"]]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    asyncio.run(app(scope, receive, send))
    h = {}
    for k, v in out["headers"]:
        h.setdefault(k, v)
    return out["status"], h, out["body"]


def cookies_of(headers_list_body):
    return headers_list_body


def set_cookies(status_headers):
    """Every Set-Cookie header from a raw response, as a dict of name -> full line."""
    return status_headers


def json_of(body):
    try:
        return json.loads(body.decode())
    except ValueError:
        return None


SAME = {"Origin": "http://riparr.local:9797", "Host": "riparr.local:9797",
        "X-Riparr-Request": "1", "Content-Type": "application/json"}

print("security headers on every response")
for path in ("/", "/api/auth/me", "/static/app.css", "/nope"):
    st, h, _ = request("GET", path)
    check("%s carries a CSP" % path, "script-src 'self'" in h.get("content-security-policy", ""), True)
    check("%s forbids framing" % path, (h.get("x-frame-options"), "frame-ancestors 'none'" in h.get("content-security-policy", "")), ("DENY", True))
    check("%s has nosniff" % path, h.get("x-content-type-options"), "nosniff")
    check("%s keeps referrers home" % path, h.get("referrer-policy"), "same-origin")
check("an error response carries them too", request("POST", "/api/drive/eject", {"Origin": "http://evil.example", "Host": "riparr.local:9797"})[1].get("x-content-type-options"), "nosniff")

print("CSRF: a state-changing request must come from this page")
st, _, _ = request("POST", "/api/drive/eject", {"Origin": "http://evil.example", "Host": "riparr.local:9797", "X-Riparr-Request": "1"})
check("a foreign Origin is refused", st, 403)
st, _, _ = request("POST", "/api/drive/eject", {"Sec-Fetch-Site": "cross-site", "X-Riparr-Request": "1"})
check("Sec-Fetch-Site: cross-site is refused", st, 403)
st, _, _ = request("POST", "/api/drive/eject", {"Sec-Fetch-Site": "same-site", "X-Riparr-Request": "1"})
check("Sec-Fetch-Site: same-site (a sibling host) is refused", st, 403)
st, _, _ = request("POST", "/api/drive/eject", {"Origin": "http://riparr.local:9797", "Host": "riparr.local:9797"})
check("a same-origin request without the page's header is refused", st, 403)
st, _, _ = request("POST", "/api/drive/eject", {})
check("a bare request (a form post has no header) is refused", st, 403)
st, _, _ = request("POST", "/api/drive/eject", SAME)
check("the page's own request goes through", st != 403, True)
st, _, _ = request("POST", "/api/drive/eject", {"Sec-Fetch-Site": "same-origin", "X-Riparr-Request": "1"})
check("same-origin by Sec-Fetch-Site goes through", st != 403, True)
st, _, _ = request("POST", "/api/drive/eject", {"Sec-Fetch-Site": "none", "X-Riparr-Request": "1"})
check("a direct request (Sec-Fetch-Site: none) goes through", st != 403, True)
st, _, _ = request("PUT", "/api/settings", {"Origin": "http://evil.example", "Host": "riparr.local:9797"}, b"{}")
check("PUT is covered", st, 403)
st, _, _ = request("DELETE", "/api/system/events", {"Origin": "http://evil.example", "Host": "riparr.local:9797"})
check("DELETE is covered", st, 403)
st, _, _ = request("GET", "/api/status", {"Origin": "http://evil.example", "Host": "riparr.local:9797"})
check("GET is never refused for origin", st != 403, True)
st, _, _ = request("GET", "/", {"Sec-Fetch-Site": "cross-site"})
check("a cross-site navigation to the page itself is fine", st, 200)

print("the body limit")
os.environ["RIPARR_MAX_BODY_BYTES"] = "1000"
st, _, b = request("POST", "/api/config/import", SAME, b"{}", content_length=5000)
check("a declared length over the limit is a 413 before anything is read", st, 413)
check("and says the limit", b"1000" in b, True)
big = [b"x" * 400, b"x" * 400, b"x" * 400]
st, _, _ = request("POST", "/api/config/import", SAME, chunks=big)
check("a streamed body that grows past the limit is cut off with 413", st, 413)
st, _, _ = request("POST", "/api/config/import", SAME, b"{}")
check("a small body is read normally", st != 413, True)
st, _, _ = request("POST", "/api/config/import", SAME, b"{}", content_length="abc")
check("a nonsense Content-Length is a 400", st, 400)
del os.environ["RIPARR_MAX_BODY_BYTES"]
check("the default limit is 64 MiB", WEB.max_body(), 64 * 1024 * 1024)

print("cookies")
db.create_user("admin", "password1")
st, h, _ = request("POST", "/api/auth/login", SAME, b'{"username": "admin", "password": "password1"}')
check("login works", st, 200)
session = h.get("set-cookie", "")
check("the session cookie is HttpOnly and SameSite=lax", ("HttpOnly" in session, "SameSite=lax" in session), (True, True))
check("and not Secure on plain HTTP", "Secure" in session, False)
os.environ["RIPARR_COOKIE_SECURE"] = "1"
st, h, _ = request("POST", "/api/auth/login", SAME, b'{"username": "admin", "password": "password1"}')
check("RIPARR_COOKIE_SECURE=1 adds Secure", "Secure" in h.get("set-cookie", ""), True)
del os.environ["RIPARR_COOKIE_SECURE"]
cookie_value = session.split(";")[0]

print("the access-token gate")
check("off by default", WEB.gate_token(), "")
st, _, _ = request("GET", "/api/status", {"Cookie": cookie_value})
check("without a gate, a session is enough", st, 200)
os.environ["RIPARR_AUTH_TOKEN"] = "s3cret-token"
st, _, b = request("GET", "/api/status", {"Cookie": cookie_value})
check("with a gate, a session alone is refused", st, 401)
check("and told why", b"access token" in b, True)
st, _, _ = request("GET", "/api/status", {"Cookie": cookie_value, "Authorization": "Bearer wrong"})
check("a wrong bearer token is refused", st, 401)
st, _, _ = request("GET", "/api/status", {"Cookie": cookie_value, "Authorization": "Bearer s3cret-token"})
check("the bearer token opens it", st, 200)
st, _, _ = request("POST", "/api/drive/eject", {"Cookie": cookie_value, "Authorization": "Bearer s3cret-token", "Origin": "http://elsewhere.example", "Host": "riparr.local:9797"})
check("a bearer-authenticated client is not a browser: no CSRF check", st != 403 and st != 401, True)
st, _, b = request("GET", "/api/setup/state")
check("setup/state says only that there is a gate", json_of(b).get("gate") is True and "hostname" not in json_of(b), True)
st, _, b = request("GET", "/api/auth/me")
check("auth/me says there is a gate", json_of(b).get("gate"), True)
st, _, b = request("GET", "/", {})
check("the page itself is served", st, 200)
st, _, _ = request("POST", "/api/auth/login", SAME, b'{"username": "admin", "password": "password1"}')
check("a login without the token is refused", st, 401)
st, _, _ = request("POST", "/api/auth/login", SAME, b'{"username": "admin", "password": "password1", "token": "wrong"}')
check("a login with the wrong token is refused", st, 401)
main._login_succeeded()                       # no delay for the rest of the test
st, h, _ = request("POST", "/api/auth/login", SAME, b'{"username": "admin", "password": "password1", "token": "s3cret-token"}')
check("a login with the token works", st, 200)
# Two Set-Cookie headers: the session and the gate. The driver keeps the first of each
# name, so read them all from the raw list.
raw = h
st, h2, _ = request("POST", "/api/auth/login", SAME, b'{"username": "admin", "password": "password1", "token": "s3cret-token"}')
# Rebuild from a fresh request to capture both cookies.
scope_cookies = {}


def all_set_cookies():
    hdrs = [("content-type", "application/json"), ("x-riparr-request", "1"),
            ("origin", "http://riparr.local:9797"), ("host", "riparr.local:9797")]
    body = b'{"username": "admin", "password": "password1", "token": "s3cret-token"}'
    hdrs.append(("content-length", str(len(body))))
    scope = {"type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/api/auth/login", "raw_path": b"/api/auth/login", "query_string": b"",
             "headers": [(k.encode(), v.encode()) for k, v in hdrs],
             "server": ("127.0.0.1", 9797), "client": ("127.0.0.1", 1)}
    out = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out.extend(v.decode() for k, v in msg["headers"] if k == b"set-cookie")
    asyncio.run(app(scope, receive, send))
    return out


cookies = all_set_cookies()
gate = [c for c in cookies if c.startswith(WEB.GATE_COOKIE + "=")]
check("the login sets a gate cookie", len(gate), 1)
check("the gate cookie is HttpOnly", "HttpOnly" in gate[0], True)
gate_value = gate[0].split(";")[0]
st, _, _ = request("GET", "/api/status", {"Cookie": cookie_value + "; " + gate_value})
check("session plus gate cookie opens it", st, 200)
st, _, _ = request("GET", "/api/status", {"Cookie": cookie_value + "; " + WEB.GATE_COOKIE + "=forged"})
check("a forged gate cookie does not", st, 401)
main._rotate_secret()
st, _, _ = request("GET", "/api/status", {"Cookie": cookie_value + "; " + gate_value})
check("a password change (secret rotation) revokes the gate cookie too", st, 401)
del os.environ["RIPARR_AUTH_TOKEN"]
check("gate removed: back to sessions", request("GET", "/api/auth/me")[0], 200)

print()
if failures:
    print("FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all good")
