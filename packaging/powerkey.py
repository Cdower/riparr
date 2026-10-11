#!/usr/bin/env python3
"""The power button, held for Riparr. Runs as root, standard library only.

logind's answer to the power key is to power off at once, which is the wrong answer
halfway through a three-hour rip. This process runs under
`systemd-inhibit --what=handle-power-key --mode=block`, so logind leaves the key alone,
and reads the key itself instead.

It decides only one thing -- one press or two -- and hands that to Riparr as a file,
because the service is unprivileged and cannot be asked anything else. Riparr decides
*when* it is safe, and then uses the same poweroff door the web page does.

The button is never allowed to go dead. If Riparr is not running, or has not picked the
request up within HANDOFF_TIMEOUT, this powers off by itself: losing a rip is better than
a button that does nothing. If this process is not running, the inhibitor goes with it
and logind's own behaviour comes back.
"""
import glob
import os
import select
import struct
import subprocess
import sys
import time

KEY_POWER = 116
EV_KEY = 1
DOUBLE_WINDOW = 0.6
HANDOFF_TIMEOUT = 10.0
SCAN_EVERY = 2.0                      # seconds between looks for a late-probing device
SCAN_TRIES = 30                       # ... for up to a minute
REQUEST = "/run/riparr/powerkey.request"

# struct input_event in the native layout: two longs of timestamp, then type, code,
# value. 16 bytes on armhf, 24 on arm64 -- calcsize gets both right.
EVENT = struct.Struct("llHHi")


def log(msg):
    print(msg, flush=True)                # journald stamps it


def has_key(caps_text, code=KEY_POWER, bits_per_word=None):
    """Is `code` set in a /sys capabilities/key bitmap?

    The kernel prints the bitmap as hex words of one `long` each, most significant word
    first, so bit 116 is in the second-to-last word on a 64-bit kernel and the fourth-to-
    last on a 32-bit one.
    """
    bits = bits_per_word or struct.calcsize("l") * 8
    words = caps_text.split()
    idx, bit = divmod(code, bits)
    if idx >= len(words):
        return False
    return bool((int(words[len(words) - 1 - idx], 16) >> bit) & 1)


def find_power_keys(sysroot="/sys"):
    """Every input device that can send KEY_POWER, in order. Empty if there is none.

    Found by capability rather than by name, so a board whose PMIC is not an AXP209
    works the same way, and a board with no power key at all simply has nothing to hold.
    All of them, because the inhibitor stops logind listening to every one: reading only
    the first would leave a press on another device unanswered.
    """
    pattern = os.path.join(sysroot, "class", "input", "event*", "device",
                           "capabilities", "key")
    found = []
    for caps in sorted(glob.glob(pattern)):
        try:
            with open(caps) as f:
                text = f.read()
        except OSError:
            continue
        if has_key(text):
            found.append("/dev/input/" + caps.split(os.sep)[-4])
    return found


class Presses:
    """One press or two. A press opens a window; a second inside it is a double."""

    def __init__(self, window=DOUBLE_WINDOW):
        self.window = window
        self.first = None

    def press(self, t):
        if self.first is not None and t - self.first <= self.window:
            self.first = None
            return "double"
        self.first = t
        return None

    def due(self, t):
        if self.first is not None and t - self.first > self.window:
            self.first = None
            return "single"
        return None

    def timeout(self, t):
        if self.first is None:
            return None
        return max(0.0, self.first + self.window - t)


def hand_off(kind, path=REQUEST):
    """Write the request where Riparr looks for it, all at once.

    A rename, so the service can never read half a word. /run/riparr belongs to the
    unprivileged service, so nothing in it can be trusted: the temp file is unlinked first
    (which removes a planted symlink rather than following it) and then created with
    O_EXCL | O_NOFOLLOW, so root never writes through a link. O_NONBLOCK stops a planted
    FIFO from hanging the button. Losing a race raises OSError, and the caller powers off.
    """
    tmp = path + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(kind + "\n")
    os.rename(tmp, path)


def stale(path, handed_at, now, timeout=HANDOFF_TIMEOUT):
    """Has Riparr had the request for `timeout` seconds without taking it?"""
    return handed_at is not None and now - handed_at >= timeout and os.path.exists(path)


def riparr_running():
    return subprocess.run(["systemctl", "is-active", "--quiet", "riparr.service"]
                          ).returncode == 0


def poweroff(why):
    log("Powering off: %s" % why)
    subprocess.run(["systemctl", "poweroff"])


def wait_for_keys(find=find_power_keys, sleep=time.sleep, tries=SCAN_TRIES):
    """The power-key devices, rescanning for a while if there are none yet.

    The input driver can probe after this unit starts; exiting at once would leave the
    inhibitor gone and the button with nothing holding it.
    """
    devs = find()
    for _ in range(tries):
        if devs:
            break
        sleep(SCAN_EVERY)
        devs = find()
    return devs


def main():
    devs = wait_for_keys()
    if not devs:
        log("No power key on this board; nothing to hold.")
        return 0
    files = []
    for dev in devs:
        try:
            files.append(open(dev, "rb", buffering=0))
        except OSError as e:
            log("Could not open %s: %s" % (dev, e))
    log("Holding the power key on %s" % ", ".join(f.name for f in files))
    presses = Presses()
    handed_at = None

    def deliver(kind, now):
        nonlocal handed_at
        if not riparr_running():
            poweroff("%s press, and Riparr is not running" % kind)
            return
        # Keep the first hand-off time while the request is still sitting there, so
        # pressing again cannot keep postponing the fallback on a hung Riparr.
        first = handed_at if handed_at is not None and os.path.exists(REQUEST) else now
        try:
            hand_off(kind)
        except OSError as e:
            poweroff("%s press, and Riparr could not be told: %s" % (kind, e))
            return
        log("%s press handed to Riparr" % kind.capitalize())
        handed_at = first

    while files:
        now = time.monotonic()
        waits = [w for w in (
            presses.timeout(now),
            None if handed_at is None else max(0.0, handed_at + HANDOFF_TIMEOUT - now),
        ) if w is not None]
        ready, _, _ = select.select(files, [], [], min(waits) if waits else None)
        now = time.monotonic()
        for f in ready:
            try:
                data = f.read(EVENT.size)
            except OSError as e:
                data = b""
                log("Lost %s: %s" % (f.name, e))
            if not data:                  # an empty read means the device is gone
                files.remove(f)
                f.close()
                continue
            if len(data) == EVENT.size:
                _sec, _usec, etype, code, value = EVENT.unpack(data)
                if etype == EV_KEY and code == KEY_POWER and value == 1:
                    kind = presses.press(now)
                    if kind:
                        deliver(kind, now)
        kind = presses.due(now)
        if kind:
            deliver(kind, now)
        if stale(REQUEST, handed_at, now):
            poweroff("Riparr did not answer within %d seconds" % HANDOFF_TIMEOUT)
            handed_at = None
        elif handed_at is not None and not os.path.exists(REQUEST):
            handed_at = None
    log("Every power-key device has gone; exiting so systemd restarts this.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
