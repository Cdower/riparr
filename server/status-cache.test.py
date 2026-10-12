#!/usr/bin/env python3
"""The caches behind /api/status: each one remembers, and each one forgets on cue.

Run: python3 server/status-cache.test.py
"""
import os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
tmp = tempfile.mkdtemp()
os.environ["RIPARR_DB"] = os.path.join(tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"

from riparr import db, main, platform as P
db.init()

failures = []
def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  got=%r want=%r" % (got, want)))
    if not ok: failures.append(name)


# ── the TTL cache, on a clock the test owns ──

now = [100.0]
calls = []

@P._ttl(3, clock=lambda: now[0])
def answer(x):
    calls.append(x)
    return {"x": x}

answer(1); answer(1)
check("two calls inside the TTL run the function once", calls, [1])
now[0] += 3.5
answer(1)
check("a call across the TTL runs it again", calls, [1, 1])
answer(2)
check("distinct arguments are remembered separately", calls, [1, 1, 2])
P._ttl_clear()
answer(2)
check("_ttl_clear forces a miss", calls, [1, 1, 2, 2])
r = answer(2); r["x"] = "mutated"
check("a caller mutating the answer does not change the cache", answer(2), {"x": 2})


# ── settings ──

db.set("theme", "x")
check("set then get", db.get("theme"), "x")
check("set returns the value (main._secret relies on it)", db.set("theme", "y"), "y")
check("get sees the new value", db.get("theme"), "y")
db.conn().execute("UPDATE settings SET value='\"raw\"' WHERE key='theme'")
db.conn().commit()
db.settings_changed()
check("a raw UPDATE followed by settings_changed() is seen", db.get("theme"), "raw")
db.get("theme")                                   # cached again
os.environ["RIPARR_DB"] = os.path.join(tmp, "other.db")
db.DB_PATH = os.environ["RIPARR_DB"]
db._local.c = None
db.init()
check("init() clears the settings cache", db.get("theme"), db.DEFAULTS.get("theme"))
lst = db.set("listy", [1, 2]); got = db.get("listy"); got.append(3)
check("a caller mutating a list does not change the cache", db.get("listy"), [1, 2])


# ── counting queries ──

real_conn = db.conn
executed = []

class Counting:
    def __init__(self, c): self.c = c
    def execute(self, sql, *a):
        executed.append(sql)
        return self.c.execute(sql, *a)
    def __getattr__(self, name): return getattr(self.c, name)

def counting_conn():
    return Counting(real_conn())

db.conn = counting_conn
try:
    db.create_user("ann", "password1")
    executed.clear()
    first = db.has_users(); second = db.has_users()
    check("has_users is True once there is a user", (first, second), (True, True))
    check("has_users asks the database once", len(executed), 1)
    db.clear_users()
    executed.clear()
    check("has_users after clear_users is False", db.has_users(), False)
    check("clear_users resets the memo", len(executed), 1)
    db.has_users()
    check("a False answer is asked again", len(executed), 2)

    # typical_stage_seconds: two done jobs with stages, so there is a median.
    for _ in range(2):
        jid = db.create_job(state="queued", disc_family="dvd")
        db.update_job(jid, stages='[{"name": "scan", "started": 10, "ended": 70}]',
                      started_at=1, finished_at=100)
        db.update_job(jid, state="done")
    executed.clear()
    a = db.typical_stage_seconds(kind="dvd"); b = db.typical_stage_seconds(kind="dvd")
    check("typical_stage_seconds has an answer", a == b and bool(a), True)
    check("typical_stage_seconds hits the database once for two calls",
          sum("FROM jobs" in s for s in executed), 1)
    executed.clear()
    db.typical_job_seconds(kind="dvd"); db.typical_job_seconds(kind="dvd")
    check("typical_job_seconds hits the database once for two calls",
          sum("FROM jobs" in s for s in executed), 1)
    db.update_job(jid, state="failed")
    executed.clear()
    db.typical_stage_seconds(kind="dvd")
    check("update_job with a state invalidates", sum("FROM jobs" in s for s in executed), 1)
finally:
    db.conn = real_conn


# ── /api/status asks for the drives once ──

real_drives = P.optical_drives
drive_calls = []
def counted_drives():
    drive_calls.append(1)
    return real_drives()
P.optical_drives = counted_drives
try:
    st = main.status(user=None)
finally:
    P.optical_drives = real_drives
check("status() calls optical_drives exactly once", len(drive_calls), 1)
# The key set of /api/status at 106d35a, before this change.
BASE_KEYS = {"version", "hostname", "system", "storage", "optical", "clock", "wifi",
             "makemkv", "drives", "share", "setup_complete", "autorip", "duplicate",
             "library", "led", "power"}
check("status() keeps every key it had", set(st), BASE_KEYS)
check("autorip keeps its keys", set(st["autorip"]),
      {"enabled", "ready", "blockers", "checks", "requested"})
check("the autorip checks are all there", len(st["autorip"]["checks"]), 5)


# ── the secret is read once ──

main._SECRET = None
s1 = main._secret()
executed.clear()
db.conn = counting_conn
try:
    s2 = main._secret()
    check("the secret is read once", (s1 == s2, len(executed)), (True, 0))
    s3 = main._rotate_secret()
    check("rotating replaces the remembered secret", (main._secret() == s3, s3 != s1),
          (True, True))
finally:
    db.conn = real_conn

print("\n%d failures" % len(failures) if failures else "\nall good")
sys.exit(1 if failures else 0)
