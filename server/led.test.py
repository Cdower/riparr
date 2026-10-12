#!/usr/bin/env python3
"""What the status LED actually puts on the wire.

The colours themselves were proven on a real NeoPixel (see the `led` docstring); this pins
the three things that proof depended on, so they cannot quietly regress: the SPI clock is
set, the frame has a real reset on both sides, and a failure never escapes `_write`.

Run: python3 server/led.test.py
"""
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="riparr-led-test-")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"

import struct  # noqa: E402

from riparr import led  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


# ── encoding ──
print("encoding")
enc = led._encode((255, 0, 0))
check("one pixel is nine bytes", len(enc), 9)
bits = bin(int.from_bytes(enc, "big"))[2:].zfill(72)
check("green (first) is 0x00: eight 100s", bits[:24], "100" * 8)
check("red (second) is 0xff: eight 110s", bits[24:48], "110" * 8)
check("blue (third) is 0x00: eight 100s", bits[48:], "100" * 8)


# ── framing ──
# Swap the module's `os` and `fcntl` for recorders, so nothing touches a real device.
calls = {"ioctl": [], "writes": [], "closed": [], "opened": []}


def fake_env(ioctl_error=None, write_error=None):
    for k in calls:
        calls[k] = []

    def _open(path, flags):
        calls["opened"].append((path, flags))
        return 42

    def _write(fd, data):
        if write_error:
            raise write_error
        calls["writes"].append((fd, bytes(data)))
        return len(data)

    def _ioctl(fd, req, arg):
        if ioctl_error:
            raise ioctl_error
        calls["ioctl"].append((fd, req, bytes(arg)))

    led.os = types.SimpleNamespace(O_RDWR=os.O_RDWR, open=_open, write=_write,
                                   close=lambda fd: calls["closed"].append(fd),
                                   environ=os.environ, path=os.path, access=os.access,
                                   W_OK=os.W_OK)
    led.fcntl = types.SimpleNamespace(ioctl=_ioctl)


led.available = lambda: True
colour = (255, 0, 0)

print("framing")
fake_env()
check("returns True", led._write(colour), True)
check("opened the SPI device read-write", calls["opened"], [(led.SPI_DEV, os.O_RDWR)])
by_req = {req: arg for _fd, req, arg in calls["ioctl"]}
check("mode 0", by_req.get(0x40016b01), struct.pack("B", 0))
check("8 bits per word", by_req.get(0x40016b03), struct.pack("B", 8))
check("2.4 MHz", by_req.get(0x40046b04), struct.pack("I", 2400000))
check("exactly three ioctls", len(calls["ioctl"]), 3)
want = b"\x00" * 100 + led._encode(colour) + b"\x00" * 100
check("100 zeros + pixel + 100 zeros", [w for _fd, w in calls["writes"]], [want])
check("fd closed", calls["closed"], [42])

print("failure")
fake_env(ioctl_error=OSError("no such ioctl"))
check("ioctl error returns False", led._write(colour), False)
check("fd still closed after ioctl error", calls["closed"], [42])
fake_env(write_error=OSError("device gone"))
check("write error returns False", led._write(colour), False)
check("fd still closed after write error", calls["closed"], [42])

print("idle cost")
# The thread runs whether or not there is an LED, so that plugging one in later works.
# What it must not do is pay for the state -- a subprocess and three database reads --
# twenty times a second on a box with nothing attached and nothing to do.
import threading  # noqa: E402

asked = {"n": 0}


def counting_state():
    asked["n"] += 1
    return "ready"


led.current_state = counting_state
led.TICK, led.STATE_EVERY = 0.01, 0.2


def run_loop(seconds):
    asked["n"] = 0
    fake_env()
    led._stop.clear()
    t = threading.Thread(target=led._loop, daemon=True)
    t.start()
    t.join(seconds)
    led._stop.set()
    t.join(1)
    return asked["n"], len(calls["writes"])


led.available = lambda: False
n, writes = run_loop(0.5)
check("no LED: the state is never computed", n, 0)
check("no LED: nothing is written", writes, 0)
led.available = lambda: True
n, writes = run_loop(0.5)
check("LED: the state is computed about every STATE_EVERY, not every tick", n <= 4, True)
check("LED: the animation still runs every tick", writes >= 10, True)

print()
if failures:
    print("%d FAILED" % len(failures))
    sys.exit(1)
print("all passed")
