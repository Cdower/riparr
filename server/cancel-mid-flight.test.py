#!/usr/bin/env python3
"""A cancel that lands while nothing is listening for it.

Two places a cancel used to be heard and then ignored:

  * during the identify scan. `enqueue` holds the API thread for minutes inside
    `fingerprint()`; Cancel marks the row cancelled meanwhile, and `enqueue` then
    wrote state='queued' over it and woke the worker. The disc the user had just
    cancelled was ripped.
  * during a real upload. `Transport.put` checked its cancel event only on the mock
    path; against smbclient the transfer ran to the end, the file landed in the
    library under a job marked cancelled, and nothing removed it.

Run: python3 server/cancel-mid-flight.test.py
"""
import os
import shutil
import stat
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="riparr-cancel-test-")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"
os.environ["RIPARR_MOCK_FAST"] = "1"

from riparr import db, platform as P, rip, shares as SH  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


db.init()
db.set("setup_complete", True)
rip.STAGING = os.path.join(_tmp, "staging")
os.makedirs(rip.STAGING)


def latest_job():
    row = db.conn().execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


print("a cancel during the identify scan sticks")
real_fingerprint = rip.fingerprint


def fingerprint_then_cancel(drive, on_progress=None):
    # The scan is running; the user presses Cancel on the row that exists for it.
    job = db.active_job()
    ok, _ = rip.cancel(job["id"])
    check("cancel accepted the identifying job", ok, True)
    return real_fingerprint(drive, on_progress=on_progress)


rip.fingerprint = fingerprint_then_cancel
rip._wake.clear()
try:
    job_id, why = rip.enqueue()
finally:
    rip.fingerprint = real_fingerprint
row = latest_job()
check("enqueue hands back no job", job_id, None)
check("and says why", bool(why), True)
check("the row is still cancelled", row["state"], "cancelled")
check("the worker was not woken", rip._wake.is_set(), False)
check("the fingerprint was still recorded", bool(row["fingerprint"]), True)

print("a cancel during a real upload stops smbclient and removes the partial file")
log_path = os.path.join(_tmp, "smbclient.log")
fake = os.path.join(_tmp, "smbclient")
with open(fake, "w") as f:
    f.write('#!/bin/sh\n'
            'for a in "$@"; do last="$a"; done\n'
            'echo "$last" >> "%s"\n'
            'case "$last" in put*) sleep 20;; esac\n'
            'exit 0\n' % log_path)
os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
local = os.path.join(_tmp, "film.mkv")
with open(local, "wb") as f:
    f.write(os.urandom(4096))

real_smbclient, real_mock = SH.SMBCLIENT, P.MOCK
SH.SMBCLIENT, P.MOCK = fake, False
try:
    t = SH.Transport({"host": "nas", "path": "Media", "username": "", "password": ""})
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    t0 = time.time()
    r = t.put(local, "Movies/Film (2001)/Film (2001).mkv", cancel=cancel, poll=60)
    took = time.time() - t0
finally:
    SH.SMBCLIENT, P.MOCK = real_smbclient, real_mock
check("put returned once cancelled, not when smbclient finished", took < 5, True)
check("and reported the cancel", r.get("ok"), False)
check("by name", r.get("error"), "Cancelled")
commands = open(log_path).read().splitlines() if os.path.exists(log_path) else []
check("the partial file was deleted from the share",
      any(c.startswith('del "Movies/Film (2001)/Film (2001).mkv"') for c in commands), True)

print("")
shutil.rmtree(_tmp, ignore_errors=True)
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
