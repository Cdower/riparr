#!/usr/bin/env python3
"""The sampling profiler, driven by hand.

The thread that samples and the thread that uploads are both exercised without either
running: `tick()` is public so the test owns the clock, and the uploader's `send()` is one
synchronous POST against a stub server in this process.

Run: python3 server/profiling.test.py
"""
import base64, http.server, os, sys, tempfile, threading, time, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
tmp = tempfile.mkdtemp()
os.environ["RIPARR_DB"] = os.path.join(tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"
os.environ.pop("RIPARR_PROFILE_DIR", None)

from riparr import db, profiling as PR
db.init()

failures = []
def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  got=%r want=%r" % (got, want)))
    if not ok: failures.append(name)

# ── folded ──
check("folded rounds, drops zeros, sorts",
      PR.folded({("t", "a.py:f"): 2.6, ("t", "a.py:g"): 0.2, ("s", "b.py:h"): 1.0}),
      "s;b.py:h 1\nt;a.py:f 3\n")
check("folded of nothing is empty", PR.folded({}), "")

# ── frame_name ──
def _leaf(): return sys._getframe()
f = _leaf()
check("frame_name is path:function", PR.frame_name(f).endswith("profiling.test.py:_leaf"), True)
import starlette.routing
sf = starlette.routing.__file__
class Fake:
    def __init__(self, filename, name): self.f_code = type("c", (), {"co_filename": filename, "co_name": name})()
check("site-packages path is trimmed", PR.frame_name(Fake(sf, "x")), "starlette/routing.py:x")
check("riparr path is relative to server/",
      PR.frame_name(Fake(os.path.join(HERE, "riparr", "rip.py"), "r")), "riparr/rip.py:r")
check("other paths are basenames", PR.frame_name(Fake("/usr/lib/python3/foo.py", "q")), "foo.py:q")

# ── thread_name ──
class T:
    def __init__(self, name): self.name = name
check("anyio workers are 'worker'", PR.thread_name(T("AnyIO worker thread")), "worker")
check("executor workers are 'worker'", PR.thread_name(T("ThreadPoolExecutor-0_3")), "worker")
check("named threads keep their name", PR.thread_name(T("riparr-rip")), "riparr-rip")

# ── CPU attribution, by hand ──
# Two fake threads: 1 is on `_leaf`'s frame, 2 is on a frame from `_other`.
def _other(): return sys._getframe()
f1, f2 = _leaf(), _other()
cpu = {1: 0, 2: 0}
windows = []
s = PR.Sampler(hz=100, window=10, on_window=lambda a, b, t: windows.append((a, b, t)),
               clock=lambda ident: cpu.get(ident), frames=lambda: {1: f1, 2: f2},
               threads=lambda: [type("Th", (), {"ident": 1, "name": "riparr-rip"})(),
                                type("Th", (), {"ident": 2, "name": "AnyIO worker thread"})()])
s.tick(1000.00)                       # first sight: nothing attributed
cpu[1] += 30_000_000                  # 30 ms
s.tick(1000.01)
cpu[2] += 5_000_000                   # 5 ms -> 0.5 samples
s.tick(1000.02)
s.tick(1000.03)                       # nobody moved
s.flush(1000.04)
check("one window closed", len(windows), 1)
a, b, text = windows[0]
check("window bounds are int seconds", (a, b), (1000, 1000))
lines = sorted(text.splitlines())
check("30 ms at 100 Hz is 3 samples on the rip thread",
      any(l.startswith("riparr-rip;") and l.endswith(":_leaf 3") for l in lines), True)
check("the 5 ms thread rounds to 1 sample",
      any(l.startswith("worker;") and l.endswith(":_other 1") for l in lines), True)
check("an idle tick adds nothing", len(lines), 2)
cpu[1] += 10_000_000
s.tick(1010.05)                       # past the window: closes it
s.tick(1010.06)
check("the window closes on schedule", len(windows), 2)
cpu[1] += 10_000_000
s.tick(1005.00)                       # the wall clock stepped backwards
check("a backward clock step closes the window", len(windows), 3)
check("a backward step never sends from > until", windows[2][0] <= windows[2][1], True)

# ── the real clock, where there is one ──
clk = PR.thread_cpu_clock(threading.get_ident())
if sys.platform.startswith("linux"):
    check("linux has a per-thread CPU clock", isinstance(clk, int), True)
    check("MODE is cpu on linux", PR.MODE, "cpu")
else:
    check("MODE matches clock availability", PR.MODE, "cpu" if clk is not None else "wall")

# ── a real thread, really sampled ──
got = []
def _burn():
    end = time.time() + 0.4
    n = 0
    while time.time() < end: n += 1
real = PR.Sampler(hz=200, window=60, on_window=lambda a, b, t: got.append(t))
real.start()
th = threading.Thread(target=_burn, name="burner"); th.start(); th.join()
real.stop()
check("stop() flushes a window", len(got), 1)
check("the burning thread shows up", "burner;" in got[0] and ":_burn " in got[0], True)
check("the profiler does not sample itself", "riparr-profiler" in got[0], False)

# ── the uploader against a stub ──
seen = []
class Stub(http.server.BaseHTTPRequestHandler):
    status = 200
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        seen.append((self.path, self.headers.get("Authorization"),
                     self.headers.get("Content-Type"), self.rfile.read(n).decode()))
        self.send_response(Stub.status); self.end_headers(); self.wfile.write(b"nope" if Stub.status != 200 else b"")
    def log_message(self, format, *args): pass
srv = http.server.HTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = "http://127.0.0.1:%d/" % srv.server_port
up = PR.Uploader(url, "12345", "glc_secret", "riparr.cpu", {"host": "box", "version": "0.6.1"})
ok, detail = up.send(1700000000, 1700000010, "worker;a.py:f 3\n")
check("200 is ok", (ok, detail), (True, "HTTP 200"))
path, auth, ctype, body = seen[-1]
q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
check("posts to /ingest", urllib.parse.urlparse(path).path, "/ingest")
check("name carries app and tags", q["name"], ["riparr.cpu{host=box,version=0.6.1}"])
check("from/until/sampleRate", (q["from"], q["until"], q["sampleRate"]), (["1700000000"], ["1700000010"], ["100"]))
check("format/units/spyName/aggregation", (q["format"], q["units"], q["spyName"], q["aggregationType"]),
      (["folded"], ["samples"], ["riparr"], ["sum"]))
check("basic auth is user:token", auth, "Basic " + base64.b64encode(b"12345:glc_secret").decode())
check("body is the folded text", (ctype, body), ("text/plain", "worker;a.py:f 3\n"))
Stub.status = 401
ok, detail = up.send(1, 2, "x 1\n")
check("401 is not ok and says so", (ok, detail.startswith("HTTP 401")), (False, True))
check("the token is not in the error", "glc_secret" in detail, False)
check("a bad scheme is refused without a request",
      PR.Uploader("ftp://x", "u", "t", "riparr.cpu", {}).send(1, 2, "x 1\n")[0], False)
Stub.status = 200

# ── settings glue ──
db.set("profiling_enabled", False)
PR.apply()
check("off and no dump dir: nothing runs", PR.status()["running"], False)
os.environ["RIPARR_PROFILE_DIR"] = os.path.join(tmp, "prof")
PR.apply()
st = PR.status()
check("dump dir alone runs the sampler", (st["running"], st["uploading"]), (True, False))
end = time.time() + 0.3
while time.time() < end: pass           # give the sampler ticks, and this thread some CPU to see
PR.stop()
files = os.listdir(os.environ["RIPARR_PROFILE_DIR"])
check("stop() wrote a window file", len(files) >= 1 and all(f.endswith(".folded") for f in files), True)
os.environ.pop("RIPARR_PROFILE_DIR")
db.set("profiling_enabled", True); db.set("profiling_url", url)
db.set("profiling_user", "12345"); db.set("profiling_token", "glc_secret")
PR.apply()
st = PR.status()
check("enabled with creds: sampling and uploading", (st["running"], st["uploading"], st["enabled"]), (True, True, True))
r = PR.test()
check("test() posts and reports ok", r, {"ok": True, "error": None})
check("test() sent a one-line profile", seen[-1][3].strip().endswith(" 1"), True)
db.set("profiling_enabled", False)
PR.apply()
check("disabling stops both", (PR.status()["running"], PR.status()["uploading"]), (False, False))
srv.shutdown()

print("\n%d failures" % len(failures) if failures else "\nall good")
sys.exit(1 if failures else 0)
