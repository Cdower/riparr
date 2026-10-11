#!/usr/bin/env python3
"""When the power button may actually power the box off.

Everything `power` knows about the rest of the box comes through `power.hooks`, so this
drives it with plain lists standing in for the rip engine: which jobs a worker is inside,
which are being identified, which are on the card waiting to be sent.

Run: python3 server/power.test.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="riparr-power-test-")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"

from riparr import power  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


world = {"in_flight": [], "identifying": [], "sendable": [], "off": [], "door": (True, "ok")}


def door():
    world["off"].append(True)
    return world["door"]


power.hooks.update(
    in_flight=lambda: list(world["in_flight"]),
    identifying=lambda: list(world["identifying"]),
    sendable=lambda: list(world["sendable"]),
    poweroff=door,
    now=lambda: 1000.0,
)
power.REQUEST = os.path.join(_tmp, "powerkey.request")


def fresh(**kw):
    power.reset()
    world.update(in_flight=[], identifying=[], sendable=[], off=[], door=(True, "ok"))
    world.update(kw)


def press(kind):
    with open(power.REQUEST, "w") as f:
        f.write(kind + "\n")
    power.tick()


print("idle")
fresh()
press("single")
check("one press while idle powers off", world["off"], [True])
check("and the request file was consumed", os.path.exists(power.REQUEST), False)
fresh()
press("double")
check("so do two", world["off"], [True])

print("a needs_input job does not hold the box up")
fresh()                                  # needs_input is in none of the three lists
press("single")
check("powers off", world["off"], [True])

print("one press waits for the rip to reach the library")
fresh(in_flight=[7])
press("single")
check("mode is after_rip", power.status()["mode"], power.AFTER_RIP)
check("holding", power.holding(), True)
check("not a step boundary", power.at_step_boundary(), False)
check("waiting on the job", power.status()["waiting_on"], [7])
check("not off yet", world["off"], [])
world["in_flight"] = []
world["sendable"] = [7]                  # ripped, handed to the sender, not yet picked up
power.tick()
check("still waiting while it sits on the card", world["off"], [])
world["sendable"] = []
power.tick()
check("off once it is done", world["off"], [True])
power.tick()
check("and only once", world["off"], [True])

print("two presses stop at the step")
fresh(in_flight=[8], sendable=[5])
press("double")
check("mode is after_step", power.status()["mode"], power.AFTER_STEP)
check("a step boundary", power.at_step_boundary(), True)
check("does not wait on what is parked on the card", power.status()["waiting_on"], [8])
world["in_flight"] = []
power.tick()
check("off once the running step ends", world["off"], [True])

print("identification counts as running")
fresh(identifying=[9])
press("double")
check("waiting on the job being identified", power.status()["waiting_on"], [9])
check("not off", world["off"], [])

print("upgrades, never downgrades")
fresh(in_flight=[3])
press("single")
press("double")
check("single then double is after_step", power.status()["mode"], power.AFTER_STEP)
press("single")
check("a later single does not go back", power.status()["mode"], power.AFTER_STEP)
check("since is kept from the first press", power.status()["since"], 1000.0)

print("cancel")
fresh(in_flight=[3])
press("single")
ok, _ = power.cancel()
check("cancel succeeds", ok, True)
check("no longer holding", power.holding(), False)
world["in_flight"] = []
power.tick()
check("and does not power off later", world["off"], [])

print("the door is missing")
fresh(door=(False, "This copy of Riparr was installed before shutdown existed."))
press("single")
st = power.status()
check("mode clears", st["mode"], None)
check("the reason is kept for the banner", st["error"],
      "This copy of Riparr was installed before shutdown existed.")
check("the work gates open again", power.holding(), False)

print("junk in the request file")
fresh()
press("triple")
check("ignored", (power.status()["mode"], world["off"]), (None, []))

print("cancel after it has fired")
fresh()
press("single")
ok, msg = power.cancel()
check("refused", ok, False)

print("a press after it has fired")
fresh()
press("single")
press("single")
check("is left for powerkey.py's own fallback", os.path.exists(power.REQUEST), True)
os.remove(power.REQUEST)

print("the box is still up long after it fired")
clock = {"t": 1000.0}
power.hooks["now"] = lambda: clock["t"]
fresh()
press("single")
clock["t"] += 100
power.tick()
check("not yet given up at 100 s", power.status()["fired"], True)
clock["t"] += 30
power.tick()
st = power.status()
check("given up after 120 s", (st["mode"], st["fired"]), (None, False))
check("the error says so", "still running" in (st["error"] or ""), True)
check("the work gates open again too", power.holding(), False)
power.hooks["now"] = lambda: 1000.0

print("the shutdown call itself blows up")
fresh()
world["door"] = None


def boom():
    raise RuntimeError("no dbus")


power.hooks["poweroff"] = boom
press("single")
st = power.status()
check("mode clears", (st["mode"], st["fired"]), (None, False))
check("the error says why", "no dbus" in (st["error"] or ""), True)
check("the work gates open again", power.holding(), False)
power.hooks["poweroff"] = door

print("a check that cannot be answered")
fresh(in_flight=[4])
bad = {"on": True}


def flaky():
    if bad["on"]:
        raise RuntimeError("database is locked")
    return list(world["in_flight"])


power.hooks["in_flight"] = flaky
press("single")
st = power.status()
check("mode stays pending", st["mode"], power.AFTER_RIP)
check("status reports the error and no ids", (bool(st["error"]), st["waiting_on"]), (True, []))
check("does not power off on a guess", world["off"], [])
bad["on"] = False
world["in_flight"] = []
power.tick()
check("powers off once it can tell", world["off"], [True])
check("and the error is gone", power.status()["error"], None)
power.hooks["in_flight"] = lambda: list(world["in_flight"])

print("")
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
