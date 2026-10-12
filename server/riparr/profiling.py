"""
Where the service spends its CPU, measured on the box and streamed to Pyroscope.

The box is a 1 GHz ARMv7 board, and every second the browser polls a handful of
endpoints. Guessing which of them costs what has already been wrong once, so this module
measures it: a thread wakes `SAMPLE_HZ` times a second, asks CPython for every thread's
current frame, and weighs each stack by the CPU time its thread burned since the last
tick. The stacks fold into ten-second windows in the folded text format that Grafana
Pyroscope's `/ingest` endpoint accepts as-is.

## Why not `pyroscope-io`

It ships wheels for x86_64 and aarch64 only, and its native half is Rust, so on this
ARMv7 userland it neither installs nor builds. Folded stacks over HTTP are a dozen lines
of `urllib`, so the whole profiler is the standard library and the dependency list does
not change.

## Why a per-thread CPU clock

A wall-clock sampler sees every idle thread parked in `select()` at full weight and
drowns the one thread actually doing work. `pthread_getcpuclockid` gives each thread its
own CPU clock, so a thread that slept through a tick contributes nothing and its stack
is never even walked. macOS has no such call; there every live thread weighs 1 per tick
and the profile is named `riparr.wall` so nobody mistakes it for CPU.

## A burst that ends before the tick

The clock says how much CPU a thread burned since the last tick, not where, and the only
stack there is to charge it to is the one the thread is on when the tick lands. A
threadpool worker that handles a request in a millisecond and goes back to waiting would
be charged to `threading.py:wait`, and the event loop's per-request work to
`selectors.py:select`: the flame graph would say the service spends its CPU waiting, which
is the one thing a CPU profile must never say. So when a thread's leaf frame is in one of
the modules a thread parks in (`PARKING`), the delta goes to the last stack that thread
was seen on with a leaf anywhere else, which is where the work most likely happened. A
thread that has never been caught busy keeps the parked stack, because there is nothing
better to say about it. Wall mode moves nothing: there a tick weighs 1 wherever the thread
is, and a parked thread really is spending its wall time parked.

Two limits remain. The remembered stack is the last busy one the sampler happened to see,
so a thread whose bursts always fall between ticks, and run different code each time, has
all of them charged to whichever busy stack it was last caught on. And `time.sleep` is C,
with no Python frame of its own, so a short burst that ends in a sleep leaves the thread's
leaf in the function that called `sleep`: the CPU is charged to the right function but not
to the callees that did the work.

## Nothing here touches the card

Closed windows go over the network, or to `RIPARR_PROFILE_DIR` when that is set (which
is how `bench.py` sees inside the process), and nowhere else. The upload queue is
bounded at `MAX_PENDING` windows: when Grafana is unreachable the oldest are dropped,
never spooled.
"""
import base64
import collections
import ctypes
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, db, platform as P, system as SY

log = SY.component("Profiling")

SAMPLE_HZ = 100
WINDOW_SECONDS = 10
MAX_PENDING = 6
MAX_DEPTH = 64

TIMEOUT = 15
# How long stopping the uploader waits for its last send. apply() holds the module lock
# while it waits, and a settings save should not hang on Grafana for the full TIMEOUT.
STOP_WAIT = 2

# The modules a thread waits in: locks and conditions, queues, the event loop's select,
# socket and TLS reads, a child process's exit, and the executors' idle loop. A leaf frame
# in one of these means the thread is parked, so the CPU it burned since the last tick was
# burned somewhere else (see "A burst that ends before the tick" above). Compared on the
# path's tail, so the standard library's copy and a vendored one both count.
PARKING = (
    "threading.py", "queue.py", "selectors.py", "socket.py", "ssl.py", "subprocess.py",
    "concurrent/futures/thread.py", "concurrent/futures/_base.py",
    "asyncio/base_events.py", "asyncio/selector_events.py",
)

# The profiler's own threads. Both names start with the same prefix so that one check
# keeps them out of every sample: a profile dominated by the profiler is no profile.
SAMPLER_THREAD = "riparr-profiler"
UPLOADER_THREAD = "riparr-profiler-upload"

BAD_URL = "The profiling URL must start with http:// or https://"
INCOMPLETE = "Fill in the URL, the stack ID and the token first."


# ─────────────────────────────── the clock ───────────────────────────────

try:
    _pthread_getcpuclockid = ctypes.CDLL(None).pthread_getcpuclockid
    _pthread_getcpuclockid.argtypes = [ctypes.c_ulong, ctypes.POINTER(ctypes.c_int)]
    _pthread_getcpuclockid.restype = ctypes.c_int
except (AttributeError, OSError):
    _pthread_getcpuclockid = None


def thread_cpu_clock(ident):
    """A clockid for `time.clock_gettime_ns` that reads one thread's CPU time, or None.

    CPython's `Thread.ident` is `pthread_self()` on POSIX, so it is exactly what
    `pthread_getcpuclockid` wants. None means the platform has no such clock (macOS) or
    the thread is gone; both are answered the same way, by not weighing that thread.
    """
    if _pthread_getcpuclockid is None or not hasattr(time, "clock_gettime_ns"):
        return None
    cid = ctypes.c_int()
    try:
        if _pthread_getcpuclockid(ctypes.c_ulong(ident), ctypes.byref(cid)) != 0:
            return None
    except (OSError, ctypes.ArgumentError):
        return None
    return cid.value


MODE = "cpu" if isinstance(thread_cpu_clock(threading.get_ident()), int) else "wall"


# ─────────────────────────────── naming ───────────────────────────────

# The directory that holds the `riparr` package. Paths under it are reported relative to
# it, so a frame reads `riparr/rip.py:_rip` on the box and on a laptop alike.
_SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep

# Every tick names every frame of every busy thread, and the set of distinct files is
# small, so the path arithmetic is done once per file rather than once per frame.
_paths = {}


def _display_path(filename):
    got = _paths.get(filename)
    if got is None:
        # Dependencies first: the venv lives inside server/, so a starlette frame would
        # otherwise read `.venv/lib/python3.13/site-packages/starlette/routing.py`.
        for marker in ("site-packages" + os.sep, "dist-packages" + os.sep):
            i = filename.rfind(marker)
            if i >= 0:
                got = filename[i + len(marker):].replace(os.sep, "/")
                break
        else:
            if filename.startswith(_SERVER):
                got = filename[len(_SERVER):].replace(os.sep, "/")
            else:
                got = os.path.basename(filename)
        _paths[filename] = got
    return got


# The same per-file caching for the parking test, which runs once per walked stack.
_parking = {}


def _parks(filename):
    got = _parking.get(filename)
    if got is None:
        tail = filename.replace(os.sep, "/")
        got = any(tail == m or tail.endswith("/" + m) for m in PARKING)
        _parking[filename] = got
    return got


def frame_name(frame):
    """`<path>:<function>`, with no line number.

    Line numbers would split one function into as many stacks as it has call sites,
    which multiplies the upload and makes the flame graph harder to read for nothing.
    """
    code = frame.f_code
    return _display_path(code.co_filename) + ":" + code.co_name


def thread_name(thread):
    """The root frame of a stack: the thread's name, with pool workers merged.

    AnyIO and `concurrent.futures` number their workers, and the numbers mean nothing:
    worker 3 today is worker 7 tomorrow. One `worker` root shows the threadpool's cost
    as one tower instead of a dozen slivers.
    """
    name = thread.name or "?"
    if name.startswith("AnyIO worker thread") or name.startswith("ThreadPoolExecutor-"):
        return "worker"
    # A semicolon is the folded format's frame separator.
    return name.replace(";", "_")


def folded(stacks):
    """Pyroscope's folded text: one `frame;frame;frame count` line per stack.

    Counts accumulate as floats (a thread that burned 5 ms is half a sample) and are
    rounded half-up here, once, rather than with Python's banker's `round`, which would
    send 0.5 to 0 and lose every lightly loaded thread. Rows that round to 0 are dropped.
    """
    lines = []
    for stack, weight in stacks.items():
        n = int(weight + 0.5)
        if n > 0:
            lines.append("%s %d\n" % (";".join(stack), n))
    lines.sort()
    return "".join(lines)


# ─────────────────────────────── the sampler ───────────────────────────────

class Sampler:
    """Samples every thread's stack, weighed by its CPU time, into fixed windows.

    `tick()` is public and takes the time as an argument so a test can drive it by hand.
    `clock`, `frames` and `threads` default to the real thing and exist so the test can
    replace them; whenever a clock is given it is used, whatever `MODE` says.
    """

    def __init__(self, hz=SAMPLE_HZ, window=WINDOW_SECONDS, on_window=None,
                 clock=None, frames=None, threads=None):
        self.hz = hz
        self.window = window
        self.on_window = on_window
        self._tick_ns = 1e9 / hz
        self._clockids = {}
        if clock is not None:
            self._clock = clock
        elif MODE == "cpu":
            self._clock = self._cpu_ns
        else:
            self._clock = None
        self._frames = frames or sys._current_frames
        self._threads = threads or threading.enumerate
        self._last = {}            # ident -> CPU ns at the previous tick
        self._busy = {}            # ident -> the last stack seen with a non-parking leaf
        self._stacks = {}          # (thread, frame, ..., leaf) -> samples
        self._window_start = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def _cpu_ns(self, ident):
        cid = self._clockids.get(ident)
        if cid is None:
            cid = thread_cpu_clock(ident)
            if cid is None:
                return None
            self._clockids[ident] = cid
        try:
            return time.clock_gettime_ns(cid)
        except OSError:
            # The thread exited between the enumerate and the read, or its ident was
            # reused. Either way the cached clockid is no longer that thread's.
            self._clockids.pop(ident, None)
            return None

    def tick(self, now):
        """Take one sample, then close the window if it has run its length.

        `now` is wall-clock time, because Pyroscope wants unix seconds, and the box has
        no RTC: NTP steps the clock at boot, sometimes backwards. A backward step closes
        the window at once rather than holding it open for the size of the jump.
        """
        with self._lock:
            if self._window_start is None:
                self._window_start = now
            self._sample()
            if now < self._window_start or now - self._window_start >= self.window:
                self._close(now)

    def flush(self, now):
        """Close the current window early, so stopping does not lose its last seconds."""
        with self._lock:
            if self._window_start is None:
                self._window_start = now
            self._close(now)

    def _sample(self):
        frames = self._frames()
        live = set()
        for th in self._threads():
            ident = th.ident
            if ident is None or (th.name or "").startswith(SAMPLER_THREAD):
                continue
            live.add(ident)
            frame = frames.get(ident)
            if frame is None:
                continue
            if self._clock is None:
                weight = 1.0
            else:
                ns = self._clock(ident)
                if ns is None:
                    self._last.pop(ident, None)
                    self._busy.pop(ident, None)
                    continue
                prev = self._last.get(ident)
                self._last[ident] = ns
                # A thread seen for the first time has no baseline yet, and one whose
                # clock did not move was asleep: neither has a stack worth walking.
                if prev is None or ns <= prev:
                    continue
                weight = (ns - prev) / self._tick_ns
            parked = self._clock is not None and _parks(frame.f_code.co_filename)
            names = []
            while frame is not None and len(names) < MAX_DEPTH:
                names.append(frame_name(frame))
                frame = frame.f_back
            names.append(thread_name(th))
            key = tuple(reversed(names))
            # CPU seen on a parked thread was burned before it parked: charge it to where
            # the thread was last caught working, if it ever was (the module docstring
            # has the reasoning and the limits).
            if parked:
                key = self._busy.get(ident, key)
            elif self._clock is not None:
                self._busy[ident] = key
            self._stacks[key] = self._stacks.get(key, 0.0) + weight
        for gone in [i for i in self._last if i not in live]:
            del self._last[gone]
        for gone in [i for i in self._busy if i not in live]:
            del self._busy[gone]
        for gone in [i for i in self._clockids if i not in live]:
            del self._clockids[gone]

    def _close(self, now):
        stacks, self._stacks = self._stacks, {}
        start = now if self._window_start is None else self._window_start
        # After a backward clock step `now` is before the start; the window is reported
        # as ending where it began, because Pyroscope has no use for from > until.
        from_ts = int(start)
        until_ts = max(from_ts, int(now))
        self._window_start = now
        text = folded(stacks)
        if text and self.on_window is not None:
            try:
                self.on_window(from_ts, until_ts, text)
            except Exception as e:
                log.debug("Profile window handler failed: %s", e)

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=SAMPLER_THREAD, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop sampling and flush the window in progress."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self.flush(time.time())

    def _run(self):
        # An absolute schedule: each tick is due one period after the last was due, not
        # one period after it finished, so the rate does not drift with the work done.
        # It runs on the monotonic clock, because the wall clock is stepped by NTP at
        # boot and a step must neither stall the sampler nor fire a burst of ticks.
        # Missed ticks are skipped, not replayed: in cpu mode the next tick attributes
        # the whole CPU delta anyway, and a burst of catch-up ticks would land exactly
        # when the box is already starved. The wait is on the stop event so that
        # stopping does not wait out a period.
        period = 1.0 / self.hz
        next_tick = time.monotonic()
        while not self._stop.wait(max(0.0, next_tick - time.monotonic())):
            try:
                self.tick(time.time())
            except Exception as e:
                log.debug("Profile sample failed: %s", e)
            next_tick = max(next_tick + period, time.monotonic())


# ─────────────────────────────── the uploader ───────────────────────────────

def _valid_url(url):
    try:
        u = urllib.parse.urlparse(url or "")
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.hostname)


class Uploader:
    """POSTs closed windows to Pyroscope's `/ingest`, from its own thread.

    The sampler hands windows over with `push()` and never waits on the network. A
    window that fails to send is dropped: a profile is a statistical picture, and a gap
    in it is much cheaper than a queue that grows while Grafana is unreachable.
    """

    def __init__(self, url, user, token, name, tags, hz=SAMPLE_HZ):
        self.url = (url or "").rstrip("/")
        self.user = user
        self.token = token
        self.name = name
        self.tags = tags
        self.hz = hz
        self.last_upload_at = None
        self.last_error = None
        self._pending = collections.deque(maxlen=MAX_PENDING)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._failing = False
        self._thread = None

    def push(self, from_ts, until_ts, text):
        self._pending.append((from_ts, until_ts, text))
        self._wake.set()

    def send(self, from_ts, until_ts, text):
        """One synchronous POST. Returns (ok, detail); the detail never holds the token."""
        if not _valid_url(self.url):
            return False, BAD_URL
        app = "%s{%s}" % (self.name, ",".join("%s=%s" % kv for kv in self.tags.items()))
        query = urllib.parse.urlencode({
            "name": app, "from": int(from_ts), "until": int(until_ts),
            "sampleRate": self.hz, "format": "folded", "units": "samples",
            "spyName": "riparr", "aggregationType": "sum",
        })
        auth = base64.b64encode(("%s:%s" % (self.user, self.token)).encode()).decode()
        req = urllib.request.Request(
            self.url + "/ingest?" + query, data=text.encode(), method="POST",
            headers={"Authorization": "Basic " + auth, "Content-Type": "text/plain"})
        # Everything a server could echo back that would let a reader sign in as this
        # box: the whole header value, the base64 credential on its own (which decodes to
        # user:token in one step), and the bare token. Longest first, so the header is
        # replaced whole rather than leaving "Basic ***" around a fragment.
        creds = [x for x in ("Basic " + auth, auth, self.token) if x]
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return True, "HTTP %d" % r.status
        except urllib.error.HTTPError as e:
            # Read well past the 200 characters shown, so that a credential which starts
            # inside them is read whole and scrubbed, rather than cut at the edge into a
            # prefix the replace below would not recognise.
            try:
                body = e.read(4096).decode("utf-8", "replace")
            except Exception:
                body = ""
            for x in creds:
                body = body.replace(x, "***")
            detail = ("HTTP %d %s" % (e.code, body.strip()[:200])).strip()
        except urllib.error.URLError as e:
            detail = str(e.reason)
        except ValueError:
            # Raised by urllib for a URL it cannot parse. Its message quotes the URL,
            # which may carry credentials, so it is not passed on.
            detail = "The profiling URL is not valid"
        except OSError as e:
            detail = str(e) or e.__class__.__name__
        # Belt and braces: whatever path the detail came from, a server that echoes the
        # Authorization header back must not put the credential on the Settings page, in
        # last_error or on the Events page.
        for x in creds:
            detail = detail.replace(x, "***")
        return False, detail

    def _deliver(self, from_ts, until_ts, text):
        ok, detail = self.send(from_ts, until_ts, text)
        if ok:
            self.last_upload_at = time.time()
            self.last_error = None
            if self._failing:
                log.info("Profile uploads resumed")
            self._failing = False
            return
        self.last_error = detail
        # One warning per outage, not one every ten seconds for as long as it lasts.
        if self._failing:
            log.debug("Profile upload failed: %s", detail)
        else:
            log.warning("Profile upload failed: %s", detail)
        self._failing = True

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=UPLOADER_THREAD, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop, after one try at whatever is still queued (the sampler's last flush).

        The join is bounded: shutdown must not hang on an unreachable host, and the
        thread is a daemon, so a straggler cannot hold the process open.
        """
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=STOP_WAIT)
            self._thread = None

    def _run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            while self._pending:
                self._deliver(*self._pending.popleft())
            if self._stop.is_set():
                return


# ─────────────────────────────── settings ───────────────────────────────

_lock = threading.Lock()
_sampler = None
_uploader = None
_profile_dir = None


def _settings():
    url = str(db.get("profiling_url") or "").strip()
    user = str(db.get("profiling_user") or "").strip()
    token = str(db.get("profiling_token") or "").strip()
    return url, user, token


def _complete(url, user, token):
    return _valid_url(url) and bool(user) and bool(token)


def _new_uploader(url, user, token):
    return Uploader(url, user, token, "riparr." + MODE,
                    {"host": P.hostname(), "version": __version__})


def _on_window(from_ts, until_ts, text):
    if _profile_dir:
        try:
            os.makedirs(_profile_dir, exist_ok=True)
            with open(os.path.join(_profile_dir, "%d-%d.folded" % (from_ts, until_ts)), "w") as f:
                f.write(text)
        except OSError as e:
            # The dump is a development aid; a profile that cannot be written is not
            # worth a warning on the Events page.
            log.debug("Profile window not written: %s", e)
    up = _uploader
    if up is not None:
        up.push(from_ts, until_ts, text)


def apply():
    """Bring the sampler and uploader in line with the settings and RIPARR_PROFILE_DIR.

    Called at startup and after every settings save, so it only stops what is no longer
    wanted, starts what newly is, and restarts the uploader when its credentials moved.
    The sampler does not restart for a credentials change; it hands windows to whichever
    uploader is current.
    """
    global _sampler, _uploader, _profile_dir
    with _lock:
        url, user, token = _settings()
        uploading = bool(db.get("profiling_enabled")) and _complete(url, user, token)
        _profile_dir = os.environ.get("RIPARR_PROFILE_DIR") or None
        want_sampler = uploading or bool(_profile_dir)

        # A sampler that is going away stops first, so its final flush still finds the
        # uploader that was recording it -- the same order as stop(), for the same reason.
        if _sampler is not None and not want_sampler:
            _sampler.stop()
            _sampler = None

        if _uploader is not None and (
                not uploading or (_uploader.url, _uploader.user, _uploader.token)
                != (url.rstrip("/"), user, token)):
            _uploader.stop()
            _uploader = None
        if uploading and _uploader is None:
            _uploader = _new_uploader(url, user, token)
            _uploader.start()

        if want_sampler and _sampler is None:
            _sampler = Sampler(on_window=_on_window)
            _sampler.start()
            log.info("Profiling started (%s mode%s)", MODE,
                     ", uploading" if uploading else "")


def start():
    apply()


def stop():
    """Stop both threads. The sampler's last window is flushed before the uploader goes."""
    global _sampler, _uploader
    with _lock:
        if _sampler is not None:
            _sampler.stop()
            _sampler = None
        if _uploader is not None:
            _uploader.stop()
            _uploader = None


def status():
    up = _uploader
    return {
        "enabled": bool(db.get("profiling_enabled")),
        "running": _sampler is not None,
        "uploading": up is not None,
        "mode": MODE,
        "last_upload_at": up.last_upload_at if up else None,
        "last_error": up.last_error if up else None,
    }


def test():
    """Send a one-line profile now, with the settings as saved.

    A fresh uploader rather than the running one, so the button works before Enable is
    ticked, which is when somebody pasting in credentials wants to know they are right.
    """
    url, user, token = _settings()
    if url and not _valid_url(url):
        return {"ok": False, "error": BAD_URL}
    if not _complete(url, user, token):
        return {"ok": False, "error": INCOMPLETE}
    now = int(time.time())
    ok, detail = _new_uploader(url, user, token).send(
        now - 1, now, "riparr-profiler;riparr/profiling.py:test 1\n")
    return {"ok": True, "error": None} if ok else {"ok": False, "error": detail}
