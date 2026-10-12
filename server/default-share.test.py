#!/usr/bin/env python3
"""Which share is the default, and how that changes.

The default is where every kind of disc goes unless pointed elsewhere, and it is the
share that mounts at /srv/library. It used to be set once -- the first share added --
and never again: adding a second share while the first existed left it a non-default,
removing the first left nothing flagged, and the interface had no control for it. A
box whose only share was not its default is how a NAS came to be mounted twice.

Run: python3 server/default-share.test.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="riparr-default-share-test-")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"

from riparr import db, platform as P  # noqa: E402
from riparr import main as MAIN  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


def defaults():
    return [s["id"] for s in db.list_shares() if s["is_default"]]


db.init()
db.set("setup_complete", True)

print("the first share is the default, the second is not")
first = db.add_share("Old NAS", "qnap.example", "Multimedia", None, None)
second = db.add_share("New NAS", "192.168.10.144", "Multimedia", None, None)
check("one default", defaults(), [first])
check("it mounts at the library path", P.library_mount(db.share_by_id(first)), P.LIBRARY_MOUNT)
check("the other beside it", P.library_mount(db.share_by_id(second)),
      "%s-%d" % (P.LIBRARY_MOUNT, second))

print("the default can be moved")
check("to a share that exists", db.set_default_share(second), True)
check("and there is still exactly one", defaults(), [second])
check("which every kind now falls back to", db.destination("movie")[0]["id"], second)
check("and which now has the library path", P.library_mount(db.share_by_id(second)),
      P.LIBRARY_MOUNT)
check("not to one that doesn't", db.set_default_share(999), False)
check("which changes nothing", defaults(), [second])

print("removing the default promotes the oldest share left")
db.set_default_share(first)
db.delete_share(first)
check("the remaining share is the default", defaults(), [second])
third = db.add_share("Spare drive", "spare.example", "Media", None, None)
check("adding another still leaves it alone", defaults(), [second])
db.delete_share(third)
check("removing a non-default changes nothing", defaults(), [second])

print("a table with shares and no default is repaired at startup")
db.conn().execute("UPDATE shares SET is_default=0")
db.conn().commit()
check("the flag is gone", defaults(), [])
db.init()
check("and back after init", defaults(), [second])

print("the interface has a way to do it")
fourth = db.add_share("Box sets", "sets.example", "Media", None, None)
r = MAIN.shares_set_default(fourth, user=None)
check("the endpoint answers ok", r.get("ok"), True)
check("and moved the default", defaults(), [fourth])
try:
    MAIN.shares_set_default(999, user=None)
    got = "no error"
except Exception as e:
    got = getattr(e, "status_code", None)
check("a share that doesn't exist is a 404", got, 404)

print("the mount follows the default, both ways")
asked = []
real_remount = P.request_remount
P.request_remount = lambda: asked.append(True) or True
try:
    fifth = db.add_share("Attic", "attic.example", "Media", None, None)
    MAIN.shares_set_default(fifth, user=None)
    check("making a share the default asks for a remount", len(asked), 1)
    MAIN.shares_delete(fifth, user=None)
    check("the oldest share left is promoted", defaults(), [second])
    check("and removing the default asks for one too", len(asked), 2)
    MAIN.shares_delete(db.add_share("Spare", "spare.example", "Media", None, None),
                       user=None)
    check("removing any share asks, so its mount is swept", len(asked), 3)
finally:
    P.request_remount = real_remount

print("")
shutil.rmtree(_tmp, ignore_errors=True)
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
