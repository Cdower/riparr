#!/usr/bin/env python3
"""The power button against the real rip pipeline, with the mock drive and share.

`power.test.py` proves when the box may turn off. This proves the engine listens: that
nothing new starts while a shutdown is pending, and that a season stopped at a step
boundary is parked where the sender and `recover()` both pick it up -- not failed.

Run: python3 server/power-pipeline.test.py
"""
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="riparr-power-pipe-")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")
os.environ["RIPARR_MOCK_CONTENT"] = "tv"
os.environ["RIPARR_APPLIANCE"] = "0"
os.environ["RIPARR_MOCK_FAST"] = "1"

from riparr import db, power, rip, shares as SH  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


def share_files():
    out = []
    for root, _dirs, files in os.walk(SH.MOCK_SHARE_ROOT):
        out += [os.path.relpath(os.path.join(root, f), SH.MOCK_SHARE_ROOT) for f in files]
    return sorted(out)


def fresh_share():
    shutil.rmtree(SH.MOCK_SHARE_ROOT, ignore_errors=True)
    for row in db.list_shares():
        db.delete_share(row["id"])
    return db.add_share("Media", "nas", "Media", None, None, make_default=True)


def new_job(label):
    os.environ["RIPARR_MOCK_LABEL"] = label
    return db.get_job(db.create_job(state="queued", disc_label=label,
                                    fingerprint="fp-" + label))


# The mock card reports 16 GB free and the mock season needs 23, so the preflight in
# _run_job would refuse the rip before the power button is ever consulted.
rip._staging_free = lambda: 64 * 2 ** 30

db.init()
db.set("setup_complete", True)
db.set("tv_metadata", False)
db.set("on_season_disc", "auto")
db.set("transfer_mode", "burst")
fresh_share()

print("nothing new starts while a shutdown is pending")
power.reset()
power.request("single")
job_id, why = rip.enqueue()
check("enqueue refused", job_id, None)
check("with the reason", why, power.HOLD_MESSAGE)
power.cancel()

print("in_flight is the jobs a worker is inside")
rip._cancel[41] = threading.Event()
check("listed", rip.in_flight(), [41])
rip._cancel.pop(41)
check("and gone after", rip.in_flight(), [])

print("a double press during a season rip parks it after the rip")
power.reset()
job = new_job("PARKED_S02_D01")
_identify = rip._identify


def identify_then_press(j, s):
    # The press arrives while the rip is under way, as it does on the box.
    out = _identify(j, s)
    power.request("double")
    return out


rip._identify = identify_then_press
rip._run_job(job)
rip._identify = _identify
parked = db.get_job(job["id"])
check("left transferring", parked["state"], "transferring")
check("nothing sent yet", parked["bytes_sent"] or 0, 0)
check("still on the card", os.path.isdir(parked["local_path"] or ""), True)
check("nothing in the library", share_files(), [])
check("power is no longer waiting on it", power.waiting_on(), [])

print("after the restart, recover() keeps it and the sender finishes it")
power.reset()                            # a reboot forgets the pending shutdown
rip.recover()
check("not failed by recover", db.get_job(job["id"])["state"], "transferring")
rip._send_one(db.get_job(job["id"]))
check("done", db.get_job(job["id"])["state"], "done")
check("every episode landed", len(share_files()), 6)

print("a double press between episodes stops after the current one")
fresh_share()
power.reset()
job = new_job("BETWEEN_S03_D01")
s = rip._settings()
job = rip._identify(job, s)
base = rip._rip_season(job, s, threading.Event())
db.update_job(job["id"], state="transferring", bytes_sent=0)
power.request("double")
try:
    rip._transfer_season(job, s, base, threading.Event())
    raised = False
except rip.Deferred:
    raised = True
check("deferred after the first episode", raised, True)
check("one episode in the library", len(share_files()), 1)
check("and marked done", [e["state"] for e in db.episode_plan(db.get_job(job["id"]))
                          ["episodes"]][:1], ["done"])

print("a cancel at the step boundary is a cancel, not a park")
fresh_share()
power.reset()
job = new_job("CANCELBOUND_S04_D01")
s = rip._settings()
job = rip._identify(job, s)
base = rip._rip_season(job, s, threading.Event())
db.update_job(job["id"], state="transferring", bytes_sent=0)
power.request("double")


class LateCancel(threading.Event):
    """Not cancelled until the first episode is marked done, so the cancel lands exactly
    on the boundary where the step-boundary check also fires."""

    def is_set(self):
        eps = db.episode_plan(db.get_job(job["id"]))["episodes"]
        return eps[0].get("state") == "done"


try:
    rip._transfer_season(job, s, base, LateCancel())
    got = "returned"
except rip.Cancelled:
    got = "Cancelled"
except rip.Deferred:
    got = "Deferred"
check("Cancelled wins over Deferred", got, "Cancelled")
power.reset()

print("_park leaves a finished job alone")
done_job = new_job("PARKFINAL_S01_D01")
db.update_job(done_job["id"], state="cancelled")
rip._park(done_job["id"], "test")
check("still cancelled", db.get_job(done_job["id"])["state"], "cancelled")

print("a job started just as the hold lands is left queued")
power.reset()
job = new_job("RACE_S01_D01")
power.request("single")
rip._run_job(job)
check("still queued", db.get_job(job["id"])["state"], "queued")
check("and no cancel event left behind", job["id"] in rip._cancel, False)
power.reset()

print("_send_one parks a deferred season rather than failing it")
fresh_share()
power.reset()
job = new_job("SENDPARK_S01_D01")
s = rip._settings()
job = rip._identify(job, s)
rip._rip_season(job, s, threading.Event())
db.update_job(job["id"], state="transferring", bytes_sent=0)
power.request("double")
rip._send_one(db.get_job(job["id"]))
row = db.get_job(job["id"])
check("still transferring", row["state"], "transferring")
check("bytes_sent reset so the sender takes it again", row["bytes_sent"] or 0, 0)
power.cancel()
rip._send_one(db.get_job(job["id"]))
check("finishes once cancelled", db.get_job(job["id"])["state"], "done")
check("all six there", len(share_files()), 6)

print("")
shutil.rmtree(_tmp, ignore_errors=True)
shutil.rmtree(SH.MOCK_SHARE_ROOT, ignore_errors=True)
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
