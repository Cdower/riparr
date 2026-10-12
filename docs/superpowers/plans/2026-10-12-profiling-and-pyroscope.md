# Profiling and Pyroscope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Know where the service spends its CPU on the box, spend less of it on the
paths the browser polls every second, and let a box stream a continuous CPU profile to
Grafana Cloud Profiles (Pyroscope) once its owner pastes in three values on the Settings
page.

**Architecture:**
- **A new module, `server/riparr/profiling.py`,** is a sampling profiler written against
  the standard library only. A thread wakes 100 times a second, asks CPython for every
  thread's current frame, weights each stack by the CPU time that thread burned since the
  last tick (a per-thread CPU clock from `pthread_getcpuclockid`), and folds the stacks
  into ten-second windows. A second thread POSTs each window to Pyroscope's `/ingest`
  endpoint in its folded text format with HTTP basic auth. Nothing is written to the card.
- **Settings, API and UI** follow the ntfy pattern exactly: defaults in `db.DEFAULTS`, the
  token in `SECRET_SETTINGS`, a Settings → General section with a "Save and send a test"
  row, applied live from `put_settings` with no restart.
- **`server/bench.py`** is a repeatable local benchmark: it starts the service in mock
  mode, logs in, drives the browser's polling mix, and reports per-endpoint latency, the
  process's CPU seconds idle and under load, and the hottest stacks from the profiler's
  local dump. Every optimisation task runs it before and after and records both.
- **The optimisations** are the ones the code reading found, confirmed by the bench:
  `/api/status` calling `optical_drives()` three times and spawning `iw` twice per poll;
  a settings `SELECT` per `db.get`; a `COUNT(*)` and a secret lookup on every
  authenticated request; medians recomputed on every queue poll; MakeMKV progress written
  to the database on every stdout line; three `async def` handlers doing blocking work on
  the event loop.

**Tech Stack:** Python 3 standard library (profiler, bench), FastAPI (API), vanilla JS
(UI), SQLite.

**Spec:** This plan is its own spec. The "Design" section below is the binding authority
for the profiler's wire format and settings names.

## Why not the `pyroscope-io` package

The box is an ARMv7 userland (`uname -m` says `armv7l`, Debian trixie, Python 3.13.5).
`pyroscope-io` 1.2.5 ships CPython wheels for x86_64 and aarch64 only, and its native
half is Rust, so there is no building it on the box either. `server/pyproject.toml`
already refuses native builds for exactly this reason. Pyroscope's `/ingest` endpoint
accepts folded stacks as plain text, which is a dozen lines of `urllib`, so the profiler
is pure Python and the dependency list does not change.

## Global Constraints

- **No new runtime dependency.** `server/pyproject.toml` dependencies, `uv.lock` and
  `requirements.txt` are not touched. The profiler and the bench use the standard
  library only.
- **The profiler costs nothing measurable when idle and under 2 % of one core while
  sampling.** It never reads `/proc` per tick, never walks the stack of a thread whose
  CPU clock did not advance, and sleeps on an absolute schedule so it does not drift.
- **Nothing new writes to the SD card.** Windows go over the network, or to
  `RIPARR_PROFILE_DIR` when that is set, and nowhere else. The pending-upload queue is
  bounded at **6 windows**; older windows are dropped, never spooled.
- **The token is a secret.** `profiling_token` joins `SECRET_SETTINGS`, is masked in
  `/api/settings`, and never appears in a log line or an error message.
- **No API response changes shape.** Every optimisation is invisible to the browser: same
  keys, same values, same status codes. `/api/status` keeps every field it has.
- **Caches are correct, not just fast.** Every cache names its invalidation: either every
  write path invalidates it (and the task lists the write paths it found by grep), or it
  has a TTL and the task states why that staleness is harmless.
- **Before and after.** Tasks 4 and 5 run `server/bench.py` before their first change and
  after their last, and put both result tables in the report and the numbers in the
  commit message.
- **Tests follow the existing `server/*.test.py` style:** a plain script with a
  `check()` helper, `RIPARR_APPLIANCE=0` and `RIPARR_DB` in a temp dir set before the
  import, run with `python3 server/<name>.test.py`. No pytest. Every test must pass on
  macOS (development) and Linux (CI).
- **Comments and docstrings match the surrounding code:** explain *why*, in full
  sentences.
- **Run the whole existing suite before committing a task** that touches `db.py`,
  `main.py`, `platform.py` or `rip.py`: every `server/*.test.py` except
  `music-pipeline.test.py` when its tools are missing. Tests use `uv run --locked python
  <name>.test.py` from `server/`.

## Design

### Wire format

One upload per closed window:

```
POST {profiling_url}/ingest?name=riparr.cpu{host=<hostname>,version=<__version__>}
     &from=<unix seconds>&until=<unix seconds>&sampleRate=100
     &format=folded&units=samples&spyName=riparr&aggregationType=sum
Authorization: Basic base64(profiling_user:profiling_token)
Content-Type: text/plain

<thread name>;riparr/rip.py:_run_job;riparr/rip.py:_rip;riparr/rip.py:_run_makemkv 42
...
```

- The query string is built with `urllib.parse.urlencode`, so the braces and commas in
  `name` are percent-encoded. `profiling_url` is used with its trailing slash stripped.
- On a platform without a per-thread CPU clock (macOS), every live thread is weighted 1
  per tick instead, and the name is `riparr.wall`. `profiling.MODE` says which.
- A frame is `<path>:<function>`. The path is relative to `server/` for anything under
  the `riparr` package (`riparr/rip.py`), the part after `site-packages/` for a
  dependency (`starlette/routing.py`), and the basename otherwise. No line numbers: they
  multiply the number of distinct stacks for nothing.
- The root frame is the thread name. Threads named like `AnyIO worker thread` or
  `ThreadPoolExecutor-0_3` are all reported as `worker`. The profiler's own two threads
  are never sampled.
- A sample count is the thread's CPU nanoseconds since the previous tick divided by the
  tick length (10 ms), accumulated as a float and rounded half-up (`int(v + 0.5)`, never
  Python's banker's `round`) when the window is folded. Rows that round to 0 are dropped.
- `tick(now)` samples first, then closes the window if `now - window_start >= window`.
  `window_start` is set to `now` by the first tick and again by every close, including
  `flush`. `from_ts = int(window_start)`, `until_ts = int(now)`. Empty windows are not
  reported.
- Stacks are capped at 64 frames, leaf-most kept.

### Settings

| Key | Default | Type | Meaning |
|---|---|---|---|
| `profiling_enabled` | `False` | bool | Upload to Grafana Cloud |
| `profiling_url` | `""` | str | e.g. `https://profiles-prod-001.grafana.net` |
| `profiling_user` | `""` | str | The numeric Grafana Cloud stack ID |
| `profiling_token` | `""` | str, secret | An access policy token with `profiles:write` |

`RIPARR_PROFILE_DIR=<dir>` (environment) makes the sampler run and write every closed
window to `<dir>/<from>-<until>.folded`, whether or not uploads are configured. This is
how `bench.py` sees inside the process.

### Module interface (`server/riparr/profiling.py`)

```python
SAMPLE_HZ = 100
WINDOW_SECONDS = 10
MAX_PENDING = 6
MAX_DEPTH = 64
MODE = "cpu" | "wall"          # decided at import from thread_cpu_clock availability

def frame_name(frame) -> str
def thread_name(thread) -> str
def thread_cpu_clock(ident) -> int | None     # a clockid for time.clock_gettime_ns, or None
def folded(stacks: dict[tuple, float]) -> str

class Sampler:
    def __init__(self, hz=SAMPLE_HZ, window=WINDOW_SECONDS, on_window=None,
                 clock=None, frames=None, threads=None)
    # clock(ident) -> cpu ns or None; frames() -> {ident: frame}; threads() -> [Thread]
    # All three default to the real thing; the test injects fakes.
    def tick(self, now) -> None       # one sample, closes the window when due
    def flush(self, now) -> None      # closes the current window early
    def start(self) / stop(self)      # stop() flushes

class Uploader:
    def __init__(self, url, user, token, name, tags, hz=SAMPLE_HZ)
    def push(self, from_ts, until_ts, text) -> None   # queue, bounded by MAX_PENDING
    def send(self, from_ts, until_ts, text) -> tuple[bool, str]   # one POST, synchronous
    def start(self) / stop(self)

def apply() -> None     # read the four settings and RIPARR_PROFILE_DIR; start/stop/reconfigure
def start() -> None     # alias of apply(), called from main._startup
def stop() -> None      # called from main's shutdown hook; flushes
def status() -> dict    # {"enabled", "running", "uploading", "mode", "last_upload_at", "last_error"}
def test() -> dict      # {"ok": bool, "error": str | None}; sends a one-line profile now
```

## Review Focus

1. **The CPU attribution is right.** A thread that slept for the whole tick contributes
   nothing; a thread that burned 30 ms in one tick at 100 Hz contributes 3 samples to the
   stack it was on *at the tick*; a thread seen for the first time contributes nothing
   until its second tick. Task 1's test drives `tick()` by hand with a fake clock.
2. **Credentials never leak.** The token is not in `/api/settings`, not in any log, not in
   the error string the test button shows. Task 2's test checks `_redact`.
3. **The caches cannot serve a stale answer the user can see.** Task 4 lists every write
   path for each cache. Changing a setting on the Settings page and reading it back on
   the next poll must return the new value.
4. **`/api/status` returns byte-identical JSON** in mock mode before and after Task 4,
   apart from fields that are timestamps.
5. **A rip's progress bar still moves.** Task 5 throttles the progress branch to twice a
   second; the first-byte callback must still fire once, and the final size is still
   written after the loop.

---

## File structure

| File | Responsibility |
|---|---|
| `server/riparr/profiling.py` (new) | Sampler, folded format, uploader, settings glue |
| `server/profiling.test.py` (new) | The profiler, driven by hand and against a stub server |
| `server/riparr/db.py` | Four defaults; settings read cache; `has_users` memo; `typical_*` cache |
| `server/riparr/main.py` | Secret, startup/shutdown, `/api/profiling`, settings apply, one-pass `/api/status`, memoised secret, three sync handlers |
| `server/riparr/platform.py` | TTL cache helper; cached `wifi_status`, `_ntp_synced`, `usb_host_ports`, `makemkv_status`, `_wifi_iface`; `optical_diagnosis(drives=None)` |
| `server/riparr/rip.py` | Throttled progress branch |
| `server/static/app.js` | Settings → General → Performance profiling section and its test row |
| `server/bench.py` (new) | The benchmark and profile report |
| `server/profiling-settings.test.py`, `server/status-cache.test.py`, `server/progress-throttle.test.py`, `server/bench.test.py` (new) | Tests |
| `docs/guide/07-settings-reference.md`, `docs/guide/08-troubleshooting.md`, `server/README.md`, `CHANGELOG.md`, `.github/workflows/ci.yml` | Docs and CI |

---

### Task 1: The sampler and uploader

**Files:**
- Create: `server/riparr/profiling.py`
- Test: `server/profiling.test.py`

**Interfaces:** exactly the module interface in Design. Task 2 wires `apply()`, `stop()`,
`status()` and `test()` into `main.py`; Task 3 relies on `RIPARR_PROFILE_DIR` and on
`stop()` flushing.

**Settings access:** `apply()` reads `db.get("profiling_enabled")`, `db.get("profiling_url")`,
`db.get("profiling_user")`, `db.get("profiling_token")`. Task 2 adds the defaults; until
then `db.get` returns `None` for them, which `apply()` must treat as off (`or ""`,
`bool(...)`).

**Logging:** `log = system.component("Profiling")`. On an upload failure log one
`warning` ("Profile upload failed: %s", detail) and then `debug` for each further failure
in the same streak; on the next success log `info` ("Profile uploads resumed"). The
detail string is the HTTP status and the first 200 characters of the body, or the
`URLError` reason. Never the token, never the full URL with credentials.

**URL check:** only `http` and `https` with a host are accepted; anything else makes
`send()` return `(False, "The profiling URL must start with http:// or https://")` without
a request, and `apply()` treat the configuration as incomplete.

- [ ] **Step 1: Write the failing test** — `server/profiling.test.py`

```python
#!/usr/bin/env python3
"""The sampling profiler, driven by hand.

The thread that samples and the thread that uploads are both exercised without either
running: `tick()` is public so the test owns the clock, and the uploader's `send()` is one
synchronous POST against a stub server in this process.

Run: python3 server/profiling.test.py
"""
import base64, http.server, json, os, sys, tempfile, threading, time, urllib.parse

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
    def log_message(self, *a): pass
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
```

- [ ] **Step 2: Run it, see it fail on import** — `cd server && uv run --locked python profiling.test.py`
  (expected: `ModuleNotFoundError: riparr.profiling`)

- [ ] **Step 3: Write `server/riparr/profiling.py`** to the interface in Design. Notes
  that the test does not spell out:
  - `thread_cpu_clock(ident)`: `ctypes.CDLL(None).pthread_getcpuclockid(ctypes.c_ulong(ident),
    ctypes.byref(ctypes.c_int()))`; return the clockid on 0, `None` on any error or when the
    symbol is missing (macOS). CPython's `Thread.ident` *is* `pthread_self()` on POSIX.
    Cache the clockid per ident; forget it when the thread is gone or the clock read raises.
  - `MODE` is `"cpu"` when `thread_cpu_clock(threading.get_ident())` is an int at import,
    else `"wall"`. In wall mode every live thread weighs 1 per tick.
  - The sampler thread is `riparr-profiler`, daemon; the uploader thread is
    `riparr-profiler-upload`, daemon. Neither appears in a sample.
  - Scheduling: `next_tick += period` after each tick; `time.sleep(max(0, next_tick - time.time()))`.
    If the loop falls more than one window behind, resynchronise rather than spin.
  - `Uploader.push` appends to a `collections.deque(maxlen=MAX_PENDING)` and sets an
    `Event`; the thread drains it. `send` uses `urllib.request` with a 15 s timeout.
  - `apply()` holds a module lock. It computes `want_sampler = enabled_with_creds or
    bool(os.environ.get("RIPARR_PROFILE_DIR"))` and `want_upload = enabled_with_creds`,
    where creds means url (valid scheme), user and token are all non-empty. It stops what
    is no longer wanted, starts what is newly wanted, and restarts the uploader when its
    url/user/token changed. The sampler's `on_window` writes the file when the dir is set
    (`os.makedirs(exist_ok=True)`; a failed write is logged at debug and ignored — the
    card is never the point) and pushes to the uploader when there is one.
  - `test()` builds a fresh `Uploader` from the current settings (not the running one, so
    it works before Enable is ticked) and sends `"riparr-profiler;riparr/profiling.py:test 1\n"`
    with `from=now-1, until=now`. Returns `{"ok": True, "error": None}` or
    `{"ok": False, "error": detail}`. With incomplete settings the error is
    `"Fill in the URL, the stack ID and the token first."`.
  - `status()["last_upload_at"]` is the unix time of the last successful `send` by the
    running uploader or `None`; `last_error` the last failure detail or `None`.

- [ ] **Step 4: Run the test until it passes**, then `uv run --locked ruff check riparr/profiling.py profiling.test.py`
  from the repo root's ruff config (`cd` to repo root: `uv run --directory server ruff check ../server`) and
  `uv run --directory server --locked ty check --exit-zero-on-warning --output-format concise`.

- [ ] **Step 5: Commit** — `profiling.py: a stdlib sampling profiler that speaks Pyroscope's folded ingest format`

---

### Task 2: Settings, API, UI and docs for the profiler

**Files:**
- Modify: `server/riparr/db.py` (`DEFAULTS`, after `smtp_to`)
- Modify: `server/riparr/main.py` (`SECRET_SETTINGS`, `_startup`, a shutdown hook, `put_settings`, two endpoints)
- Modify: `server/static/app.js` (`settingsPages.general`, after the Log files section; the test-button wiring next to `data-test-notify`)
- Modify: `docs/guide/07-settings-reference.md`, `CHANGELOG.md`, `server/README.md`
- Test: `server/profiling-settings.test.py`

**Depends on:** Task 1's `profiling.apply()`, `stop()`, `status()`, `test()`.

- [ ] **Step 1: Write the failing test** — `server/profiling-settings.test.py`

```python
#!/usr/bin/env python3
"""The profiler's settings: shaped right, masked right, applied on save.

Run: python3 server/profiling-settings.test.py
"""
import os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
tmp = tempfile.mkdtemp()
os.environ["RIPARR_DB"] = os.path.join(tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"

from fastapi import HTTPException
from riparr import db, main, profiling as PR
db.init()

failures = []
def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  got=%r want=%r" % (got, want)))
    if not ok: failures.append(name)

check("defaults", {k: db.DEFAULTS[k] for k in ("profiling_enabled", "profiling_url", "profiling_user", "profiling_token")},
      {"profiling_enabled": False, "profiling_url": "", "profiling_user": "", "profiling_token": ""})
check("a string where a bool belongs is refused", db.check_setting("profiling_enabled", "yes") is None, False)
check("a url string is accepted", db.check_setting("profiling_url", "https://x.grafana.net"), None)
check("the token is a secret", "profiling_token" in main.SECRET_SETTINGS, True)
db.set("profiling_token", "glc_abc")
check("the token is masked", main._redact(db.all_settings())["profiling_token"], main.SECRET_MASK)
db.set("profiling_token", "")
check("an empty token is not masked", main._redact(db.all_settings())["profiling_token"], "")

# The test button: 400 with the reason when it fails, the result when it works.
PR.test = lambda: {"ok": False, "error": "HTTP 401: nope"}
try:
    main.profiling_test(user=None); check("a failed test is a 400", True, False)
except HTTPException as e:
    check("a failed test is a 400 with the reason", (e.status_code, e.detail), (400, "HTTP 401: nope"))
PR.test = lambda: {"ok": True, "error": None}
check("a passing test returns the result", main.profiling_test(user=None), {"ok": True, "error": None})
check("GET /api/profiling is the module status", set(main.profiling_status(user=None)) >=
      {"enabled", "running", "uploading", "mode", "last_upload_at", "last_error"}, True)

# Saving any profiling_* key re-applies the module.
applied = []
PR.apply = lambda: applied.append(1)
import asyncio
class Req:
    async def json(self): return {"profiling_enabled": False, "profiling_url": "https://x"}
# put_settings may be sync (Task 5 makes it so) or async (before Task 5): handle both.
import inspect
if inspect.iscoroutinefunction(main.put_settings):
    out = asyncio.run(main.put_settings(Req(), user=None))
else:
    out = main.put_settings({"profiling_enabled": False, "profiling_url": "https://x"}, user=None)
check("saving a profiling_* key applies the module", applied, [1])
check("the saved url comes back", out["profiling_url"], "https://x")

print("\n%d failures" % len(failures) if failures else "\nall good")
sys.exit(1 if failures else 0)
```

- [ ] **Step 2: Run it, see it fail** (`KeyError: 'profiling_enabled'`).

- [ ] **Step 3: Implement.**
  - `db.DEFAULTS`: the four keys from Design, after `"smtp_to"`, with a comment saying
    what they are for and that the token is masked by `main.SECRET_SETTINGS`.
  - `main.py`: `from . import profiling as PROF`; `SECRET_SETTINGS = ("smtp_password",
    "ntfy_token", "profiling_token")`; `PROF.start()` at the end of `_startup`;
    `@app.on_event("shutdown") def _shutdown(): PROF.stop()`; in `put_settings`, after
    the `log_files` block:
    ```python
    if any(k.startswith("profiling_") for k in body):
        PROF.apply()
        SY.component("Profiling").info(
            "Profiling: %s", "sending to Grafana Cloud" if PROF.status()["uploading"] else "off")
    ```
    Endpoints, beside the notification ones:
    ```python
    @app.get("/api/profiling")
    def profiling_status(user=Depends(require_user)):
        return PROF.status()

    @app.post("/api/profiling/test")
    def profiling_test(user=Depends(require_user)):
        r = PROF.test()
        if not r.get("ok"):
            raise HTTPException(status_code=400, detail=r.get("error"))
        return r
    ```
  - `app.js`, `settingsPages.general`, a new section directly after Log files:
    ```html
    <div class="section"><h2>Performance profiling</h2><div>
      <label class="f"><span>Send a CPU profile to Grafana Cloud</span>
        <input type="checkbox" data-set="profiling_enabled" ${s.profiling_enabled ? "checked" : ""}>
        <span class="help">Every ten seconds the box sends which of its own functions were
          using the processor, and for how long, to your Grafana Cloud stack's Profiles
          service. Nothing else goes: no disc names, no file names, no settings. Off unless
          you are helping find a performance problem.</span></label>
      <label class="f"><span>Profiles URL</span>
        <input data-set="profiling_url" value="${esc(s.profiling_url || "")}" placeholder="https://profiles-prod-001.grafana.net">
        <span class="help">In Grafana Cloud: your stack → Pyroscope → Details. The URL that
          starts with <code>https://profiles-prod-</code>.</span></label>
      <label class="f"><span>Stack ID</span>
        <input data-set="profiling_user" value="${esc(s.profiling_user || "")}" placeholder="123456" inputmode="numeric">
        <span class="help">The number shown as <i>User</i> on the same page.</span></label>
      <label class="f"><span>Token</span>
        <input data-set="profiling_token" type="password" value="${esc(s.profiling_token || "")}" autocomplete="off">
        <span class="help">An access policy token with the <code>profiles:write</code> scope.
          Make one under Security → Access Policies; it is shown once.</span></label>
      <div class="btn-row"><button class="btn" data-test-profiling>Save and send a test profile</button>
        <span class="test-out" id="test-profiling"></span></div>
    </div></div>
    ```
    Wire `[data-test-profiling]` exactly the way `[data-test-notify]` is wired (find it near
    the `/api/notifications/test` call): save the page's settings first, then
    `api.post("/api/profiling/test")`, and write "Sent — check Grafana in a minute" or the
    error detail into `#test-profiling`.
  - `docs/guide/07-settings-reference.md`: a row **Performance profiling** in the System
    table, and a `### Performance profiling` subsection after Updates: what is sent and
    what is not, the three values and where in Grafana Cloud each one lives, and that the
    test button is how to know it works.
  - `CHANGELOG.md`, under Unreleased: one paragraph in the file's voice — a box can now
    stream a CPU profile to Grafana Cloud from Settings → General, off by default, nothing
    installed, nothing written to the card.
  - `server/README.md` Layout table: `riparr/profiling.py` — the sampling profiler and its
    Pyroscope uploader.

- [ ] **Step 4: Run** the new test, `uv run --locked python default-share.test.py`,
  `backup-restore.test.py` (settings round-trip), `node --check server/static/app.js`,
  ruff and ty.

- [ ] **Step 5: Commit** — `Settings → General → Performance profiling: three values and a test button send CPU profiles to Grafana Cloud`

---

### Task 3: The benchmark

**Files:**
- Create: `server/bench.py`
- Test: `server/bench.test.py`
- Modify: `server/README.md` (Tests section: how to run the bench)

**Depends on:** Task 1 (`RIPARR_PROFILE_DIR`, `stop()` flushing) and Task 2 (the shutdown
hook that calls `stop()`).

**Behaviour:**

```
uv run --locked python bench.py [--cycles 200] [--idle 15] [--json PATH] [--keep DIR]
uv run --locked python bench.py --url http://riparr.local:9797 --user U --password P [--cycles 200]
```

Local mode:
1. Pick a free port. Make a temp dir. Start `sys.executable -m uvicorn riparr.main:app
   --host 127.0.0.1 --port N --log-level warning` with `cwd=server/` and env
   `RIPARR_DB=<tmp>/bench.db`, `RIPARR_APPLIANCE=0`, `RIPARR_MOCK_FAST=1`,
   `RIPARR_PROFILE_DIR=<tmp>/prof`, `PYTHONUNBUFFERED=1`. Wait up to 30 s for
   `GET /api/setup/state` to answer; exit 2 with the child's stderr if it does not.
2. `POST /api/setup/user` with `{"username": "bench", "password": "benchbench1"}` (read
   `SetupUser` in main.py for the exact field names and any password rule), keep the
   cookie; `POST /api/auth/login` if setup did not set one; `POST /api/setup/complete`.
3. Warm up: one full cycle, not measured.
4. **Idle phase:** sleep `--idle` seconds making no requests. CPU before and after from
   `ps -o cputime= -p PID`, parsed from `[[dd-]hh:]mm:ss` to seconds (this works on macOS
   and Linux; `/proc` is not portable to the development machine). Report idle CPU seconds
   and the percentage of one core.
5. **Poll phase:** `--cycles` cycles of the browser's mix on one keep-alive
   `http.client.HTTPConnection`: `GET /api/queue`, `GET /api/status`, `GET /api/autorip`,
   and on every third cycle `GET /api/system/power`. Record wall latency per request.
   Report per endpoint: count, p50, p95, max in ms; and the phase's CPU seconds and CPU
   milliseconds per cycle.
6. SIGTERM the server, wait up to 10 s (the shutdown hook flushes the profiler's last
   window), then read every `<tmp>/prof/*.folded`. Report the top 15 frames by **self**
   samples (the leaf frame of each stack) and the top 15 by **inclusive** samples (any
   frame in the stack, counted once per stack), each with count and percentage of all
   samples. Note `MODE` in the heading: on macOS this is a wall profile.
7. Print everything as Markdown. With `--json PATH`, also write
   `{"idle": {...}, "poll": {...per endpoint...}, "cpu_ms_per_cycle": x, "self": [...], "inclusive": [...]}`.
   With `--keep DIR`, copy the `.folded` files there (they load in speedscope.app as
   "collapsed stacks").

`--url` mode skips 1, 4, 6 and 7's profile part: log in with the given user and password
and run the poll phase against a real box.

Pure standard library. One file, functions named for the phases.

- [ ] **Step 1: Write the failing test** — `server/bench.test.py`: runs
  `[sys.executable, "bench.py", "--cycles", "6", "--idle", "1", "--json", <tmp>/out.json]`
  with `cwd=server/` and a 120 s timeout; checks exit 0, that stdout contains
  `| /api/status |`, `Idle`, `self` and `inclusive` headings, and that the JSON has
  `poll["/api/status"]["p50_ms"] > 0`, `idle["cpu_seconds"] >= 0`, and a non-empty `self`
  list. Same `check()` style as the other tests.
- [ ] **Step 2: Run it, see it fail** (no `bench.py`).
- [ ] **Step 3: Write `server/bench.py`.**
- [ ] **Step 4: Run the test**, then run the bench by hand once with the defaults and
  paste its output into your report: this is the project's first baseline.
- [ ] **Step 5: `server/README.md`**, Tests section: a short paragraph on `bench.py`, what it
  measures, and the `--url` form for a real box.
- [ ] **Step 6: Commit** — `bench.py: the browser's polling mix against a mock box, with CPU seconds and the hottest stacks`

---

### Task 4: One pass over `/api/status`, and the reads every request repeats

**Files:**
- Modify: `server/riparr/platform.py`, `server/riparr/db.py`, `server/riparr/main.py`
- Test: `server/status-cache.test.py`

**Before anything:** from `server/`, `uv run --locked python bench.py --cycles 300 --idle 15 --json <workspace>/task4-before.json`
and put the Markdown in your report under "Before".

**Changes, each with a comment that says why:**

1. **A TTL cache in `platform.py`:**
   ```python
   def _ttl(seconds):
       """Remember a function's answer for `seconds`, per distinct arguments. ..."""
   ```
   Thread-safe (one lock), keyed on positional args, with a module-level
   `_ttl_clear()` the tests can call. Apply it to:
   - `wifi_status` — 3 s. The browser polls `/api/status` every 1.2 s during a rip and the
     LED thread asks every second; each call was an `iw` subprocess.
   - `_wifi_iface` — 60 s.
   - `usb_host_ports` — 60 s. Device-tree globs and a boot-env read for a fact that does
     not change while the box is up.
   - `makemkv_status` — 2 s.
   - `clock_status`: do **not** cache the dict (it carries `now`). Extract the
     `timedatectl` question into `_ntp_synced()` and cache that for 30 s.
   Keep `MOCK` branches working: the cache sits outside the `if MOCK` return so mock
   callers see no difference except speed.
2. **`/api/status` computes each fact once.** `drives = P.optical_drives()`,
   `mk = P.makemkv_status()`, `share = db.default_share()`, `storage = P.storage_status()`
   at the top of `status()`; `_drive_report(drives=None)`, `_autorip_state(mk=None,
   share=None, drives=None, storage=None)` and `P.optical_diagnosis(drives=None)` accept
   them and compute their own when not given, so every other caller is unchanged. After
   this, `status()` calls `P.optical_drives()` exactly once (the test counts).
3. **`db.get` caches settings.** A module dict guarded by a lock; `set()` updates the
   entry; `db.settings_changed()` clears it, for anyone who writes the table another way.
   Grep for every write to the settings table (`INTO settings`, `UPDATE settings`,
   `DELETE FROM settings`, `executescript`) across `server/riparr/` and list what you
   found in the report; route each through `set()` or `settings_changed()`. `init()` must
   clear the cache (tests re-init against new paths).
4. **`db.has_users()` remembers True.** Once the table has a user it stays answered until
   a users-table write (`DELETE FROM users` at db.py:~564, any `INSERT INTO users`,
   `UPDATE users`) resets the memo. List the write sites in the report.
5. **`main._secret()` is read once.** Module-level memo filled on first read, replaced by
   `_rotate_secret()`. Confirm `db.set` returns the value (`_secret` relies on it) and
   keep that contract.
6. **`typical_job_seconds` and `typical_stage_seconds` are cached for 30 s** per
   argument tuple, and cleared in `update_job` whenever `state` is among the fields. A
   median over past rips changing 30 s late is invisible; a job finishing clears it
   anyway.

**Not in scope:** `/api/queue`'s `SELECT *`, the event-log commit per line, `optical_drives()`
itself. Note anything else the profile shows in your report instead of fixing it.

- [ ] **Step 1: Write the failing test** — `server/status-cache.test.py`. Mock mode, temp DB.
  Checks, with a fake clock injected into `_ttl` (make the helper take `clock=time.monotonic`
  so the test can pass its own):
  - a wrapped function is called once for two calls inside the TTL, twice across it, and
    separately per distinct args; `_ttl_clear()` forces a miss.
  - `db.set("theme", "x"); db.get("theme") == "x"`; a raw `conn().execute("UPDATE settings ...")`
    followed by `db.settings_changed()` is seen; `db.init()` clears.
  - `db.has_users()` is computed once for repeated calls (count the `conn().execute` calls
    by wrapping `db.conn`), and recomputed after `db.clear_users()` (or whatever the
    `DELETE FROM users` function is called).
  - `main.status(user=None)` in mock mode calls `P.optical_drives` exactly once (wrap it
    with a counter) and the response has every key the pre-change response had — build
    the expected key set from the plan's `status()` as it stands at BASE, and compare.
  - `db.typical_stage_seconds(...)` hits the DB once for two calls; `db.update_job(id, state="done")`
    invalidates.
- [ ] **Step 2: Run it, see it fail.**
- [ ] **Step 3: Implement the six changes.**
- [ ] **Step 4: Run the whole suite** (Global Constraints), ruff, ty.
- [ ] **Step 5: After:** `bench.py --cycles 300 --idle 15 --json <workspace>/task4-after.json`;
  put the Markdown under "After" in your report, and the headline numbers (CPU ms per
  cycle before → after, `/api/status` p50 before → after) in the commit message.
- [ ] **Step 6: Commit** — `One pass over /api/status: each fact computed once, the Wi-Fi and clock subprocesses remembered, settings and the user count read once`

---

### Task 5: The rip's hot loop, and the event loop

**Files:**
- Modify: `server/riparr/rip.py` (`_run_makemkv`, and any sibling loop you find), `server/riparr/main.py`
- Test: `server/progress-throttle.test.py`

**Before anything:** `bench.py --cycles 300 --idle 15 --json <workspace>/task5-before.json`
(this task's gains are mostly off the bench's path; record it anyway for the record).

**Changes:**

1. **`PROGRESS_INTERVAL = 0.5`** at module level in `rip.py`, with a comment: MakeMKV
   writes a `PRGV:` line many times a second, and each one was a directory listing plus
   an `UPDATE jobs` with a commit. In `_run_makemkv`, the `PRGV` branch does its work at
   most once per `PROGRESS_INTERVAL` seconds (`last_progress` local, `time.time()`).
   The first-byte callback must still fire the first time the branch *runs* and sees a
   size; the final `update_job` after the loop is unchanged.
2. **Siblings.** Grep `server/riparr/` for `for line in` loops over a subprocess's stdout
   that call `db.update_job`, `on_progress` or `stage_*` per line (`music.py`'s
   cd-paranoia and encoder loops, `backup.py`, `tv.py`, `makemkv.py`, `shares.py`'s put
   watcher). Throttle each per-line database write the same way with the same constant.
   List every loop you looked at and what you did with it in the report, including
   "left alone, because ...".
3. **Three handlers leave the event loop.** `put_settings` and `config_import` become
   `def` taking `body: Any = Body(...)` (keep the `isinstance(body, dict)` → 400 check and
   every other line); `system_backup_upload` becomes `def` and reads with
   `file.file.read()`. FastAPI runs `def` handlers on its threadpool, so the database
   writes and the zip parse no longer stall every poll in flight.

- [ ] **Step 1: Write the failing test** — `server/progress-throttle.test.py`. Monkeypatch
  `rip.subprocess.Popen` with a fake whose `stdout` yields 60 `PRGV:1,2,3` lines with a
  `time.sleep(0.01)` between them and then `MSG:5003,0,0,"done"`, with `wait()`/`returncode 0`
  /`terminate()`; monkeypatch `rip._output_size` to return a growing number; set
  `rip.PROGRESS_INTERVAL = 0.1`; call `rip._run_makemkv(...)` with a stub job/settings
  (read the function to see what it needs — the mock-mode `s` and `job` from
  `cancel-mid-flight.test.py` are a good start), `on_progress` counting calls and
  `on_first_byte` counting calls. Check: `on_first_byte` called exactly once; `on_progress`
  called between 3 and 12 times (60 lines over ~0.6 s at 0.1 s = about 6), not 60. Then
  check `inspect.iscoroutinefunction` is False for the three handlers and that
  `main.put_settings({"theme": "dark"}, user=None)["theme"] == "dark"`.
- [ ] **Step 2: Run it, see it fail** (60 progress calls; the handlers are coroutines).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the whole suite** (Global Constraints) — `cancel-mid-flight`,
  `backup-pipeline`, `backup-restore`, `tv-pipeline`, `power-pipeline` matter most here —
  plus ruff and ty.
- [ ] **Step 5: After:** `bench.py --cycles 300 --idle 15 --json <workspace>/task5-after.json`; both tables in the report.
- [ ] **Step 6: Commit** — `Progress is written twice a second, not once per MakeMKV line; settings saves and imports leave the event loop`

---

### Task 6: CI, docs and the numbers

**Files:**
- Modify: `.github/workflows/ci.yml`, `server/README.md`, `docs/guide/08-troubleshooting.md`, `CHANGELOG.md`

**You will be given** the before/after tables from Tasks 4 and 5 in your dispatch.

- [ ] **Step 1: CI.** In the `check` job, after the existing server tests, add steps in the
  file's commenting style (one paragraph each on what broke or would break without it) for
  `python server/profiling.test.py`, `python server/profiling-settings.test.py`,
  `python server/status-cache.test.py`, `python server/progress-throttle.test.py`, and
  `python server/bench.test.py`. Check how the earlier test steps get `fastapi` on the
  path (a venv step, a pip install, or `PYTHONPATH`) and do the same.
- [ ] **Step 2: Troubleshooting.** `docs/guide/08-troubleshooting.md`: an entry "Profiles
  are not arriving in Grafana" — the test button, the Events page line
  `Profile upload failed: HTTP 401 ...` and what each status means (401: token or stack
  ID; 404: wrong URL; URLError: the box cannot reach the internet).
- [ ] **Step 3: CHANGELOG.** Under Unreleased, one paragraph in the file's voice about the
  speed-up: what the dashboard poll used to cost and what it costs now (the numbers you
  were given), the Wi-Fi subprocesses, and the progress writes.
- [ ] **Step 4: README.** `server/README.md`: the Tests table or paragraph lists the new
  tests in one line each.
- [ ] **Step 5: Verify** every test named in Step 1 passes locally
  (`uv run --locked python <name>` from `server/`), and that `ci.yml` is valid YAML
  (`python -c "import yaml, sys; yaml.safe_load(open('.github/workflows/ci.yml'))"` if
  PyYAML is available, otherwise `uvx --from pyyaml python -c ...`).
- [ ] **Step 6: Commit** — `CI runs the profiler, cache and throttle tests; the changelog says what the poll costs now`
