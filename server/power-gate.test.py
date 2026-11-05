#!/usr/bin/env python3
"""The power gate: RIPARR_ALLOW_POWER=0 turns the web page's Restart and Shut down off.

The root side enforces the same variable (packaging/power-allowed.sh, run as
ExecCondition= by the reboot, poweroff and power-key units); this is the service's
half -- the refusal the page sees, and the flag the page hides the controls on.

Run: python3 server/power-gate.test.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
tmp = tempfile.mkdtemp()
os.environ["RIPARR_DB"] = os.path.join(tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"
os.environ.pop("RIPARR_ALLOW_POWER", None)

from fastapi import HTTPException            # noqa: E402
from riparr import db, main, platform as P   # noqa: E402

db.init()
failures = []


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(name)


class _Req:
    cookies = {}


print("the default is on: an appliance with no power switch needs a way to stop")
check("allowed by default", P.power_allowed(), True)
check("the page is told so", main.me(_Req())["features"]["power"], True)
ok, msg = P.power_action("reboot")
check("a reboot request goes through (simulated off the box)", ok, True)
check("an unknown action is still refused", P.power_action("halt")[0], False)

print("RIPARR_ALLOW_POWER=0 turns it off")
for value in ("0", "no", "false", "off"):
    os.environ["RIPARR_ALLOW_POWER"] = value
    check("%r means off" % value, P.power_allowed(), False)
os.environ["RIPARR_ALLOW_POWER"] = "0"
check("the page is told to hide the controls", main.me(_Req())["features"]["power"], False)
for action in ("reboot", "poweroff"):
    ok, msg = P.power_action(action)
    check("%s is refused" % action, ok, False)
    check("and says why", "RIPARR_ALLOW_POWER" in msg, True)
try:
    main.system_power(main.PowerAction(action="poweroff"), user="x")
    check("the endpoint refuses with a 400", False, True)
except HTTPException as e:
    check("the endpoint refuses with a 400", e.status_code, 400)

print("anything else means on")
for value in ("1", "yes", ""):
    os.environ["RIPARR_ALLOW_POWER"] = value
    check("%r means on" % value, P.power_allowed(), True)

print()
if failures:
    print("FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all good")
