#!/usr/bin/env python3
"""A library share that isn't answering: what the sender and the health check do about it.

Two finished rips sat on a card for a night waiting for a NAS whose hostname had stopped
resolving. The hourly health check said the share was reachable the whole time, because
it never looked. The sender was stuck inside the first job, polling the share it had been
handed when it started, so replacing the share in Settings changed nothing, and the second
job -- which could have gone -- sat behind it. This pins the behaviour that would have
avoided every part of that.

Run: python3 server/share-away.test.py
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="riparr-share-away-test-")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"
os.environ["RIPARR_MOCK_FAST"] = "1"

from riparr import db, rip, shares as SH, system as SY  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


def contains(name, haystack, needle):
    check(name, needle in (haystack or ""), True)


# The network is the one thing a test cannot have. Everything else is real.
share_up = {"value": True}
SH.Transport.reachable = lambda self: share_up["value"]


def share_files():
    out = []
    for root, _dirs, files in os.walk(SH.MOCK_SHARE_ROOT):
        for f in files:
            out.append(os.path.relpath(os.path.join(root, f), SH.MOCK_SHARE_ROOT))
    return sorted(out)


def fresh_share(host="nas"):
    for row in db.list_shares():
        db.delete_share(row["id"])
    return db.add_share("Media", host, "Media", None, None, make_default=True)


def staged_job(title, fingerprint):
    """A film that has come off the disc and is waiting on the card to be sent."""
    job_id = db.create_job(title=title, disc_label=title.upper(), kind="movie",
                           fingerprint=fingerprint, state="transferring",
                           phase="Waiting to send to your library", mode="burst",
                           output="mkv", disc_family="dvd", bytes_sent=0)
    d = os.path.join(rip.STAGING, "job-%d" % job_id)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s-B1_t00.mkv" % title)
    with open(path, "wb") as f:
        f.write(os.urandom(4096))
    db.update_job(job_id, local_path=path, bytes_total=os.path.getsize(path))
    return db.get_job(job_id)


shutil.rmtree(SH.MOCK_SHARE_ROOT, ignore_errors=True)
rip.STAGING = os.path.join(_tmp, "staging")
os.makedirs(rip.STAGING)
# Stand in for /run/riparr, which only exists on the box.
SY.RUNDIR = os.path.join(_tmp, "run")
os.makedirs(SY.RUNDIR)
db.init()
SY.init()
db.set("setup_complete", True)
db.set("transfer_mode", "burst")
db.set("verify_mode", "quick")
db.set("movie_template", "{Title}/{Title}.mkv")

# ── the health check looks, rather than assuming ─────────────────────────────

print("the share check tells the truth")
share = fresh_share("nas")
share_up["value"] = True
r = SY.run_task("share")
check("a share that answers passes", r["error"], None)
share_up["value"] = False
r = SY.run_task("share")
contains("a share that does not answer is reported", r["error"], "nas")
check("and the scheduled table shows it",
      next(t for t in SY.task_list() if t["name"] == "share")["last_error"], r["error"])

# ── a job whose share is away steps aside ────────────────────────────────────

print("a job that cannot be sent does not block the ones that can")
share_up["value"] = False
first = staged_job("First", "fp-first")
second = staged_job("Second", "fp-second")
check("the oldest is up first", rip._next_to_send()["id"], first["id"])
rip._send_one(first)
row = db.get_job(first["id"])
check("it is still a transfer", row["state"], "transferring")
check("and says what it is waiting for", row["phase"], "Waiting for your library to come back")
check("nothing reached the share", share_files(), [])
check("the sender moves on to the next job", (rip._next_to_send() or {}).get("id"), second["id"])
rip._send_one(second)
check("with every job waiting, nothing is picked", rip._next_to_send(), None)

# ── the share is looked up again on every try ────────────────────────────────

print("a share replaced while a job waits is used when the job is retried")
db.delete_share(share)
fresh_share("nas-by-ip")
share_up["value"] = True
check("a job is not retried before its time", rip._next_to_send(), None)
rip._share_away[first["id"]] = (rip._share_away[first["id"]][0], time.time() - 1)
check("when its time comes it is picked", (rip._next_to_send() or {}).get("id"), first["id"])
rip._send_one(db.get_job(first["id"]))
row = db.get_job(first["id"])
check("it finished", row["state"], "done")
check("in the new share, not the one it was first given",
      share_files(), ["nas-by-ip/Media/Movies/First/First.mkv"])
check("and it is no longer remembered as waiting", first["id"] in rip._share_away, False)

# ── patience has a limit, measured on the clock ──────────────────────────────

print("a share that has been away six hours is a failure, not a wait")
share_up["value"] = False
started, _ = rip._share_away[second["id"]]
rip._share_away[second["id"]] = (started - 6 * 3600 - 1, 0)
rip._send_one(db.get_job(second["id"]))
row = db.get_job(second["id"])
check("the job failed", row["state"], "failed")
contains("and the message says why", row["error"], "six hours")
check("the rip is still on the card for the retry", os.path.exists(row["local_path"]), True)
check("it has been forgotten as waiting", second["id"] in rip._share_away, False)

print("a cancelled job stops waiting")
share_up["value"] = False
third = staged_job("Third", "fp-third")
rip._send_one(third)
check("it was waiting", third["id"] in rip._share_away, True)
rip.cancel(third["id"])
check("and now it is not", third["id"] in rip._share_away, False)
check("and the sender has nothing to pick up", rip._next_to_send(), None)

print("")
shutil.rmtree(_tmp, ignore_errors=True)
shutil.rmtree(SH.MOCK_SHARE_ROOT, ignore_errors=True)
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
