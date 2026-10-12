#!/usr/bin/env python3
"""A disc's fingerprint is its structure. No structure, no fingerprint.

The same DVD was ripped twice in one night because its first fingerprint was hashed from
a title scan that had returned nothing -- silently -- and its second from the real
titles. Two fingerprints, one disc, and the duplicate check had nothing to match. A
fingerprint built from the label alone is not a weaker fingerprint, it is a different
one, so a scan that comes back empty has to be a refusal, not a hash.

Run: python3 server/fingerprint.test.py
"""
import hashlib
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="riparr-fingerprint-test-")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"

from riparr import db, rip  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


def contains(name, haystack, needle):
    check(name, needle in (haystack or ""), True)


drive = {"device": "/dev/sr0", "label": "POKEMON_MEWTWO_RETURNS", "media": "DVD-R",
         "size_bytes": 3636133888, "present": True, "media_kind": "dvd"}
real_read_titles = rip.read_titles

print("a readable disc hashes its titles")
rip.read_titles = lambda device, disc=None, on_progress=None: [
    {"index": 0, "seconds": 3777}, {"index": 1, "seconds": 622}]
want = hashlib.sha1(b"POKEMON_MEWTWO_RETURNS|DVD-R|0:3777|1:622").hexdigest()
check("label, media and every title, in order", rip.fingerprint(drive), want)

print("a scan that finds nothing is not an identity")
rip.read_titles = lambda device, disc=None, on_progress=None: []
try:
    got = rip.fingerprint(drive)
except rip.UnreadableDisc as e:
    got = e
check("it refuses", isinstance(got, rip.UnreadableDisc), True)
contains("and says what to do", str(got), "again")

print("a scan that dies is not an identity either")


def _dies(device, disc=None, on_progress=None):
    raise RuntimeError("makemkvcon fell over")


rip.read_titles = _dies
try:
    got = rip.fingerprint(drive)
except rip.UnreadableDisc as e:
    got = e
check("it refuses", isinstance(got, rip.UnreadableDisc), True)

print("putting the disc in is told the same thing")
db.init()
db.set("setup_complete", True)
rip.read_titles = lambda device, disc=None, on_progress=None: []
job_id, why = rip.enqueue()
check("no job was queued", job_id, None)
contains("the reason is the scan", why, "read")
rows = db.list_jobs(limit=5)
check("the attempt is on record, as cancelled",
      [r["state"] for r in rows], ["cancelled"])
check("with that reason", rows[0]["error"], why)
check("and no disc was remembered under a made-up fingerprint",
      db.conn().execute("SELECT COUNT(*) n FROM discs").fetchone()["n"], 0)

rip.read_titles = real_read_titles
print("")
shutil.rmtree(_tmp, ignore_errors=True)
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
