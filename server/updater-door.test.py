"""The update door: what the service may say to root, and how it reads the answer.

Run: python3 server/updater-door.test.py

Until 0.8 the service downloaded, verified and swapped /opt/riparr itself, then asked
root to install units from the tree it had just written -- so a compromised service
could write anything into packaging/ and have root install it. Now the service's whole
say is one release tag in one file, and the root side (packaging/riparr-update.sh,
tested in packaging/riparr-update.test.sh) does everything else against a tree the
service cannot write.

What is asserted here, on the service side:

  1. the request file carries the tag and nothing else, and a tag that is not a
     version is never written
  2. install() refuses on a box without the door and says what to do
  3. install() waits for the root side and reports what it said -- done, error, or
     still running -- never a success it did not see
  4. an older run's state file is not mistaken for this one's answer
  5. the checksum file is found by the release's own asset URL, never built from a
     component version (the bug that compared one release's numbers to another's
     download; the root side now does the comparing, the service still finds the URL)
"""
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="riparr-update-test-")
os.environ["RIPARR_STATUS_DIR"] = os.path.join(_tmp, "status")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")

from riparr import updater as U       # noqa: E402
from riparr import platform as P      # noqa: E402

FAILURES = []


def check(name, ok, why=""):
    print(("  ok  " if ok else "  FAIL ") + name + (("  -- " + why) if why and not ok else ""))
    if not ok:
        FAILURES.append(name)


RUN = os.path.join(_tmp, "run")
STATUS = os.environ["RIPARR_STATUS_DIR"]
UNIT = os.path.join(_tmp, "riparr-update.path")
os.makedirs(RUN)
os.makedirs(STATUS)
P.RUN_DIR = RUN
P.UPDATE_REQUEST = os.path.join(RUN, "update.request")
P.UPDATE_UNIT = UNIT
P.IS_APPLIANCE = True
P.MOCK = False


def door(present=True):
    if present:
        open(UNIT, "w").close()
    elif os.path.exists(UNIT):
        os.unlink(UNIT)


def request_text():
    try:
        with open(P.UPDATE_REQUEST) as f:
            return f.read()
    except OSError:
        return None


def clear():
    for p in (P.UPDATE_REQUEST, os.path.join(STATUS, "update.state")):
        if os.path.exists(p):
            os.unlink(p)


def root_says(phase, delay=0.0, **fields):
    """A stand-in for riparr-update.sh: write the state file after `delay`."""
    def go():
        time.sleep(delay)
        d = {"phase": phase, "progress": 1.0, "message": fields.pop("message", ""),
             "detail": "", "version": "9.9.9", "provenance": "none",
             "at": int(time.time())}
        d.update(fields)
        with open(os.path.join(STATUS, "update.state"), "w") as f:
            json.dump(d, f)
    t = threading.Thread(target=go)
    t.start()
    return t


U.check = lambda repo=None: {
    "status": "update", "latest": "9.9.9", "tag": "9.9.9", "tag_name": "v9.9.9",
    "asset": {"name": "riparr-server.tar.gz", "url": "stub", "size": 1},
}

print("the request is the tag and nothing else")
door(); clear()
check("a version is accepted", P.request_update("v9.9.9"))
check("the file holds exactly the tag", request_text() == "v9.9.9\n", repr(request_text()))
clear()
for bad in ("v9.9.9; rm -rf /", "../v1", "latest", "", "v9.9.9.9.9", None, 42,
            "v9.9.9\n", "https://evil/x", "v99999.0.0"):
    check("%r is refused" % (bad,), not P.request_update(bad))
    check("and nothing was written for %r" % (bad,), request_text() is None)

print("a box without the door is told what to do")
door(False); clear()
r = U.install()
check("refuses", not r.get("ok"), str(r))
check("names the installer", "install.sh" in (r.get("detail") or ""), str(r))
check("nothing was requested", request_text() is None)

print("the answer is what root said")
door(); clear()
t = root_says("done", delay=0.3, message="Updated to 9.9.9. Riparr is restarting.",
              provenance="bound")
r = U.install(timeout=5, poll=0.1)
t.join()
check("done is a success", r.get("ok") is True, str(r))
check("with the version", r.get("version") == "9.9.9", str(r))
check("the request was placed", request_text() == "v9.9.9\n", repr(request_text()))
check("the restart is reported as a restart", r.get("restarted") == "restart", str(r))
check("needs_restart is false", r.get("needs_restart") is False, str(r))
check("provenance is passed through", r.get("provenance") == "bound", str(r))

door(); clear()
t = root_says("error", delay=0.3, message="The download did not match its published "
              "checksum. Nothing was changed.", detail="expected 0000, got ffff")
r = U.install(timeout=5, poll=0.1)
t.join()
check("error is a failure", r.get("ok") is False, str(r))
check("with root's message", "checksum" in r.get("message", ""), str(r))
check("and root's detail", r.get("detail") == "expected 0000, got ffff", str(r))

door(); clear()
r = U.install(timeout=0.5, poll=0.1)
check("no answer in time is not a success", r.get("ok") is False, str(r))
check("but is marked pending", r.get("pending") is True, str(r))

print("an older run's state is not this run's answer")
door(); clear()
with open(os.path.join(STATUS, "update.state"), "w") as f:
    json.dump({"phase": "done", "message": "Updated to 1.0.0.", "version": "1.0.0",
               "at": int(time.time()) - 60}, f)
r = U.install(timeout=0.5, poll=0.1)
check("a stale done is ignored", r.get("ok") is False and r.get("pending") is True, str(r))

print("install_state reads root's file")
door(); clear()
check("none before root has spoken", U.install_state() is None)
root_says("downloading", message="Downloading").join()
st = U.install_state()
check("then root's phase", st and st.get("phase") == "downloading", str(st))

print("not on the appliance, nothing is requested")
door(); clear()
P.IS_APPLIANCE = False
r = U.install()
check("refuses", not r.get("ok"), str(r))
check("nothing was requested", request_text() is None)
P.IS_APPLIANCE = True

# ── the checksum file comes from *this* release ──
#
# The tag and a component version stopped being the same string when the two halves
# began versioning apart. Both updaters built the checksums URL out of a version, so on
# a release tagged v0.2.3 the Preparer asked for v0.1.21's checksums (404 -> "publishes
# no checksum") and the appliance asked for v0.2.2's (a real file, the wrong numbers ->
# "the download did not match its published checksum"). The service no longer
# compares anything -- root does, from the tag -- but check() still reports the sums
# URL, and the Preparer still uses its own copy of this logic.
print("the checksums come from the release being installed")

SUMS = "deadbeef" * 8 + "  riparr-server.tar.gz\n" + ("cafe" * 16) + "  x.dmg\n"
RIGHT = "https://example.invalid/v0.2.3/SHA256SUMS.txt"


class _Resp:
    def __init__(self, body): self.body = body.encode()
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _only(expected_url):
    """A urlopen that serves the sums file at one URL and 404s everywhere else."""
    def fake(req, timeout=None, **kw):
        url = getattr(req, "full_url", req)
        if url != expected_url:
            raise OSError("404 at %s" % url)
        return _Resp(SUMS)
    return fake


assets_json = [{"name": "riparr-server.tar.gz", "browser_download_url": "u"},
               {"name": "SHA256SUMS.txt", "browser_download_url": RIGHT}]
check("the appliance finds the sums asset by name", U._sums_url(assets_json) == RIGHT,
      str(U._sums_url(assets_json)))

import urllib.request as _ur   # noqa: E402
_saved = _ur.urlopen
try:
    _ur.urlopen = _only(RIGHT)
    got = U._expected_hash("riparr-server.tar.gz", U._sums_url(assets_json), "r/r", "0.2.3")
    check("the appliance reads it from that URL", got == "deadbeef" * 8, str(got))
    stray = U._expected_hash("riparr-server.tar.gz", None, "r/r", "0.2.2")
    check("no component version is used as a release path", stray is None, str(stray))
finally:
    _ur.urlopen = _saved

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools", "preparer"))
try:
    import core as PC
except Exception as e:                                   # pragma: no cover
    PC = None
    print("  (skipped preparer half: %s)" % e)

if PC is not None:
    pa = [{"name": "riparr-preparer-macos.dmg", "url": "u"},
          {"name": "SHA256SUMS.txt", "url": RIGHT}]
    saved = PC.urlopen
    try:
        PC.urlopen = _only(RIGHT)
        got = PC.published_sha256(pa, "riparr-server.tar.gz", "0.2.3", "r/r")
        check("the preparer reads it from the asset URL", got == "deadbeef" * 8, str(got))
        stray = PC.published_sha256([], "riparr-server.tar.gz", "0.1.21", "r/r")
        check("and never from its own version", stray is None, str(stray))
    finally:
        PC.urlopen = saved

print()
if FAILURES:
    print("FAILED: " + ", ".join(FAILURES))
    sys.exit(1)
print("all good")
