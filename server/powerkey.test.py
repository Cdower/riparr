#!/usr/bin/env python3
"""The power-button helper's decisions, without a button.

`packaging/powerkey.py` runs as root on the box and reads a real input device, so the
parts worth testing are pulled out as plain functions: which device is the power key,
whether two presses were one gesture or two, and when Riparr has had long enough to
answer. The event loop that strings them together is exercised on the box.

Run: python3 server/powerkey.test.py
"""
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "powerkey", os.path.join(HERE, "..", "packaging", "powerkey.py"))
pk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pk)

failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


print("reading the key bitmap")
# What the AXP209 reports on the Banana Pro (32-bit longs): bit 116 is KEY_POWER.
check("KEY_POWER found in a 32-bit bitmap", pk.has_key("100000 0 0 0", bits_per_word=32), True)
check("and in a 64-bit one", pk.has_key("10000000000000 0", bits_per_word=64), True)
check("a keyboard without it is not a power key",
      pk.has_key("ffffffff fffffffe", bits_per_word=32), False)
check("an empty bitmap is not a power key", pk.has_key("", bits_per_word=32), False)
check("a bitmap too short to reach bit 116", pk.has_key("ffffffff", bits_per_word=32), False)

print("finding the device")
root = tempfile.mkdtemp(prefix="riparr-powerkey-")
bits = pk.struct.calcsize("l") * 8
power_caps = " ".join(["%x" % (1 << (116 % bits))] + ["0"] * (116 // bits))
for ev, caps in (("event0", "0"), ("event3", power_caps), ("event5", power_caps)):
    d = os.path.join(root, "class", "input", ev, "device", "capabilities")
    os.makedirs(d)
    with open(os.path.join(d, "key"), "w") as f:
        f.write(caps + "\n")
check("finds every device with KEY_POWER, in order", pk.find_power_keys(root),
      ["/dev/input/event3", "/dev/input/event5"])
empty = tempfile.mkdtemp(prefix="riparr-powerkey-none-")
check("no power key, no devices", pk.find_power_keys(empty), [])

print("waiting for a late device")
calls = {"n": 0, "slept": 0}


def late():
    calls["n"] += 1
    return ["/dev/input/event2"] if calls["n"] > 3 else []


def nap(_s):
    calls["slept"] += 1


check("rescans until it appears", pk.wait_for_keys(late, nap, 30), ["/dev/input/event2"])
check("sleeping between looks", calls["slept"], 3)
calls["slept"] = 0
check("gives up after the tries", pk.wait_for_keys(lambda: [], nap, 5), [])
check("five naps", calls["slept"], 5)

print("one press or two")
p = pk.Presses(window=0.6)
check("first press waits", p.press(10.0), None)
check("not yet single inside the window", p.due(10.5), None)
check("second press inside the window is a double", p.press(10.5), "double")
check("and nothing is left over", p.due(20.0), None)
check("a lone press", p.press(30.0), None)
check("becomes single once the window closes", p.due(30.61), "single")
check("once", p.due(31.0), None)
check("idle timeout is None", p.timeout(40.0), None)
p.press(50.0)
check("timeout counts down to the window's end", round(p.timeout(50.2), 2), 0.4)
check("two presses 0.7 s apart are two singles: first", p.due(50.7), "single")
check("and the second press starts its own wait", p.press(50.7), None)

print("handing off")
path = os.path.join(root, "powerkey.request")
pk.hand_off("double", path)
with open(path) as f:
    check("the request says what was pressed", f.read(), "double\n")
check("no temp file left behind", os.path.exists(path + ".tmp"), False)
pk.hand_off("single", path)
with open(path) as f:
    check("an existing request is replaced", f.read(), "single\n")
os.remove(path)
victim = os.path.join(root, "victim")
with open(victim, "w") as f:
    f.write("precious\n")
os.symlink(victim, path + ".tmp")
pk.hand_off("double", path)
with open(victim) as f:
    check("a planted link is not followed", f.read(), "precious\n")
with open(path) as f:
    check("and the request is still written", f.read(), "double\n")
check("the planted link is gone", os.path.lexists(path + ".tmp"), False)
check("not stale before the timeout", pk.stale(path, 100.0, 109.9), False)
check("stale once the timeout passes with the file still there",
      pk.stale(path, 100.0, 110.0), True)
os.remove(path)
check("not stale when Riparr took it", pk.stale(path, 100.0, 200.0), False)
check("not stale when nothing was handed off", pk.stale(path, None, 200.0), False)

shutil.rmtree(root, ignore_errors=True)
shutil.rmtree(empty, ignore_errors=True)
print("")
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
