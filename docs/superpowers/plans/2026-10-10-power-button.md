# Power Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One press of the box's power button (the Banana Pro's SW1, or a panel
button wired in parallel with it) shuts down after the current rip is in the library.
A double press shuts down after the current step. In both cases nothing in flight is
lost.

**Architecture:**
- **A root helper, `packaging/powerkey.py`,** runs under `systemd-inhibit` so logind
  ignores the key. It tells a single press from a double one and writes the answer to
  `/run/riparr/powerkey.request`. If Riparr is not running, or doesn't consume the
  request within 10 s, it powers off by itself.
- **A new module, `server/riparr/power.py`,** consumes the request, holds the shutdown
  mode, and works out what it is still waiting on. When that is nothing, it calls the
  existing `P.power_action("poweroff")` door.
- **`rip.py` asks `power`** before starting work, and parks a TV season at its safe
  points.

**Tech Stack:** Python 3 standard library (root helper and engine), FastAPI (API),
vanilla JS (UI), systemd units, bash (`apply-system.sh`).

**Spec:** `docs/superpowers/specs/2026-10-10-power-button-design.md`

## Global Constraints

- **The root helper is standard library only.** It runs with `/usr/bin/python3`, never
  the venv.
- **The service account never gains privileges.** It talks to root only through files in
  `/run/riparr`, the same pattern as the existing doors.
- **The button is never dead.** If `riparr.service` is down or doesn't answer within
  **10 s**, the helper calls `systemctl poweroff`. If the helper itself is down, logind's
  default (`poweroff`) applies.
- **Double-press window: 0.6 s.**
- **A pending shutdown is not persisted**, and the button has no cancel gesture. Cancel is
  web-only.
- **A "step" is rip, or send+verify,** never finer. Identification plus rip counts as one
  step (see Deviations).
- **The web Shut down stays immediate.**
- **Tests follow the existing `server/*.test.py` style:** a plain script, a `check()`
  helper, mock hardware via `RIPARR_APPLIANCE=0`, and run with `python3 server/<name>.test.py`.
  No pytest.
- **Comments and docstrings match the surrounding code:** explain *why*, in full
  sentences.

## Deviations from the spec (decided while planning)

1. **`power.py` runs its own one-second thread** (`power.start()` from `main._startup`),
   not a tick on the LED thread. The LED loop runs every 0.05 s and swallows every
   exception, and shutdown logic should not depend on the LED module.
2. **There is no separate "identifying" safe point.** `_identify` returns a job in a state
   `_run_job` depends on immediately, and re-queuing it would mean re-identifying on the
   next boot. Identification is seconds long, so in after_step it simply carries on into
   the rip, and the job stops after the rip like any other.
3. **Two endpoints instead of one.** Besides the `power` field in `GET /api/status`, there
   is a light `GET /api/system/power` for the banner to poll every 3 s, because
   `/api/status` runs `optical_diagnosis()` and a dozen others.

## Review Focus

1. **A double press during a cache-mode film rip.** The rip finishes, the disc ejects,
   the box powers off, and after boot the upload runs. It must not shut down mid-rip
   because the job left `_cancel` for a moment at handoff.
2. **Cancel from the web, then a disc already in the tray.** Auto Rip must pick it up,
   because `_watch_discs` did not record the signature while held. A disc that was
   *already processed* must not be ripped twice.
3. **A press while idle with a `needs_input` job.** It must shut down rather than wait
   forever on a question nobody is answering.
4. **A TV season in after_step, between episodes.** Done episodes stay done, and the
   remainder resumes after boot. It must not be marked failed by `recover()`, because it
   is parked in `transferring` with `local_path` present.
5. **The poweroff door missing (an old install).** The mode clears and the banner shows
   the reason, rather than sitting on "Shutting down…" forever.

Each of these has a test in the task that owns the code: (1) Task 3, (2) Task 3,
(3) Task 2, (4) Task 3, (5) Task 2.

---

## File structure

| File | Responsibility |
|---|---|
| `packaging/powerkey.py` (new) | Root helper: finds the key, classifies presses, hands off or powers off |
| `packaging/riparr-powerkey.service` (new) | Runs it under the logind inhibitor |
| `packaging/apply-system.sh` | Installs the helper and unit, and enables the unit |
| `server/riparr/platform.py` | Two rows in `SYSTEM_COMPONENTS` |
| `server/riparr/power.py` (new) | Shutdown mode, what it waits on, consuming requests, firing the door |
| `server/riparr/rip.py` | Gates, `in_flight()`, `Deferred`, `_park()` |
| `server/riparr/led.py` | `shutting_down` state, top priority |
| `server/riparr/main.py` | Start the thread, `power` in status, the two endpoints |
| `server/static/app.js`, `app.css`, `index.html` | LED words, the banner and Cancel |
| `server/powerkey.test.py`, `server/power.test.py`, `server/power-pipeline.test.py` (new) | Tests |
| `docs/guide/08-troubleshooting.md`, `CHANGELOG.md` | Wiring and behaviour |

---

### Task 1: The root helper and its install

**Files:**
- Create: `packaging/powerkey.py`
- Create: `packaging/riparr-powerkey.service`
- Modify: `packaging/apply-system.sh` (helper block after line 46, unit list at line 90,
  enable block after line 133)
- Modify: `server/riparr/platform.py` (`SYSTEM_COMPONENTS`, after the `riparr-poweroff.service` row)
- Test: `server/powerkey.test.py`

**Interfaces:**
- Produces: the file `/run/riparr/powerkey.request` containing `single\n` or `double\n`,
  written atomically. This is the only contract with Task 2.
- Produces (for its own test): `has_key(caps_text, code=116, bits_per_word=None) -> bool`,
  `find_power_key(sysroot="/sys") -> str | None`, class `Presses(window=0.6)` with
  `press(t) -> "double" | None`, `due(t) -> "single" | None` and
  `timeout(t) -> float | None`, `hand_off(kind, path) -> None`, and
  `stale(path, handed_at, now, timeout=10.0) -> bool`.

- [ ] **Step 1: Write the failing test** — `server/powerkey.test.py`

```python
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
for ev, caps in (("event0", "0"), ("event3", power_caps)):
    d = os.path.join(root, "class", "input", ev, "device", "capabilities")
    os.makedirs(d)
    with open(os.path.join(d, "key"), "w") as f:
        f.write(caps + "\n")
check("picks the one with KEY_POWER", pk.find_power_key(root), "/dev/input/event3")
empty = tempfile.mkdtemp(prefix="riparr-powerkey-none-")
check("no power key, no device", pk.find_power_key(empty), None)

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
```

- [ ] **Step 2: Run the test and check it fails**

Run: `python3 server/powerkey.test.py`
Expected: FAIL with `FileNotFoundError` for `packaging/powerkey.py`.

- [ ] **Step 3: Write `packaging/powerkey.py`**

```python
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


def find_power_key(sysroot="/sys"):
    """The input device that can send KEY_POWER, or None.

    Found by capability rather than by name, so a board whose PMIC is not an AXP209
    works the same way, and a board with no power key at all simply has nothing to hold.
    """
    pattern = os.path.join(sysroot, "class", "input", "event*", "device",
                           "capabilities", "key")
    for caps in sorted(glob.glob(pattern)):
        try:
            with open(caps) as f:
                text = f.read()
        except OSError:
            continue
        if has_key(text):
            return "/dev/input/" + caps.split(os.sep)[-4]
    return None


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

    A rename, so the service can never read half a word.
    """
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
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


def main():
    dev = find_power_key()
    if not dev:
        log("No power key on this board; nothing to hold.")
        return 0
    log("Holding the power key on %s" % dev)
    presses = Presses()
    handed_at = None

    def deliver(kind, now):
        nonlocal handed_at
        if not riparr_running():
            poweroff("%s press, and Riparr is not running" % kind)
            return
        try:
            hand_off(kind)
        except OSError as e:
            poweroff("%s press, and Riparr could not be told: %s" % (kind, e))
            return
        log("%s press handed to Riparr" % kind.capitalize())
        handed_at = now

    with open(dev, "rb", buffering=0) as f:
        while True:
            now = time.monotonic()
            waits = [w for w in (
                presses.timeout(now),
                None if handed_at is None else max(0.0, handed_at + HANDOFF_TIMEOUT - now),
            ) if w is not None]
            ready, _, _ = select.select([f], [], [], min(waits) if waits else None)
            now = time.monotonic()
            if ready:
                data = f.read(EVENT.size)
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


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the test and check it passes**

Run: `python3 server/powerkey.test.py`
Expected: every line `ok`, then `all good`.

- [ ] **Step 5: Write `packaging/riparr-powerkey.service`**

```ini
[Unit]
Description=Riparr power button — shut down when it is safe to
Documentation=https://github.com/jackharvest/riparr
After=systemd-logind.service

[Service]
Type=simple
# The inhibitor is what stops logind powering off on the key. It lasts exactly as long
# as powerkey.py does, so if this unit stops, the button goes back to logind's
# immediate poweroff rather than to nothing.
ExecStart=/usr/bin/systemd-inhibit --what=handle-power-key --mode=block \
          --who=Riparr --why="Riparr decides when it is safe to shut down" \
          /usr/bin/python3 /usr/local/lib/riparr/powerkey.py
# A board with no power key exits 0, and that is final: on-failure does not restart it.
Restart=on-failure
RestartSec=5

# Not sandboxed, for the same reason as riparr-netwatch.service: it runs systemctl
# poweroff, which a private namespace would let succeed and change nothing.
ProtectHome=yes
Nice=10

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 6: Install it from `packaging/apply-system.sh`**

After the `netwatch-settings.py` block (ends line 46), add:

```bash
# The power button's root half. Run as an argument to the system python3, never the
# venv: it must keep working when /opt/riparr is half-updated or broken.
if [ -f "$PKG/powerkey.py" ]; then
    install -o root -g root -m 0644 "$PKG/powerkey.py" "$LIB/powerkey.py"
fi
```

In the unit list (line 90), after `riparr-netwatch.service \`, add:

```bash
         riparr-powerkey.service \
```

After the netwatch enable block (ends line 133), add:

```bash
# Restarted rather than left alone: it holds no state worth keeping, and an update to
# powerkey.py does nothing until the process running the old one is replaced.
if [ -f /etc/systemd/system/riparr-powerkey.service ]; then
    systemctl enable --quiet riparr-powerkey.service 2>/dev/null || true
    systemctl restart riparr-powerkey.service 2>/dev/null || true
    say "power button handler started"
fi
```

- [ ] **Step 7: List it in `SYSTEM_COMPONENTS`** (`server/riparr/platform.py`, after the `riparr-poweroff.service` row)

```python
    ("riparr-powerkey.service", "unit",   "The power button",                   True),
```

and after the `netwatch-settings.py` row:

```python
    ("powerkey.py",             "script", "The power button",                   True),
```

- [ ] **Step 8: Syntax checks**

Run: `bash -n packaging/apply-system.sh && python3 -m py_compile packaging/powerkey.py server/riparr/platform.py && echo ok`
Expected: `ok`

- [ ] **Step 9: Commit**

```bash
git add packaging/powerkey.py packaging/riparr-powerkey.service packaging/apply-system.sh \
        server/riparr/platform.py server/powerkey.test.py
git commit -m "Power button: a root helper that holds the key and hands presses to Riparr"
```

---

### Task 2: `power.py`, the shutdown decision

**Files:**
- Create: `server/riparr/power.py`
- Test: `server/power.test.py`

**Interfaces:**
- Consumes: the `/run/riparr/powerkey.request` file from Task 1; `rip.in_flight() -> list[int]`
  (added in Task 3, imported lazily inside a hook, so this task does not need it to
  exist); `db.list_jobs(states=..., limit=...)`; `P.power_action("poweroff") -> (bool, str)`.
- Produces:
  - `AFTER_RIP = "after_rip"`, `AFTER_STEP = "after_step"`, and `HOLD_MESSAGE` (str).
  - `REQUEST` (str path; tests override it).
  - `hooks`, a dict with the keys `in_flight`, `identifying`, `sendable`, `poweroff` and `now` (tests override it).
  - `request(kind: str) -> None`, where `kind` is `"single"` or `"double"`.
  - `cancel() -> (bool, str)`.
  - `holding() -> bool` and `at_step_boundary() -> bool`.
  - `waiting_on(mode=None) -> list[int]`.
  - `status() -> {"mode", "since", "fired", "error", "waiting_on"}`.
  - `tick() -> None`, `start() -> None`, `reset() -> None`.

- [ ] **Step 1: Write the failing test** — `server/power.test.py`

```python
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

print("")
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
```

- [ ] **Step 2: Run the test and check it fails**

Run: `python3 server/power.test.py`
Expected: FAIL with `ImportError: cannot import name 'power'`.

- [ ] **Step 3: Write `server/riparr/power.py`**

```python
"""
The power button: shut down when it is safe to, not when it is pressed.

`packaging/powerkey.py` holds the key as root and tells this module one thing -- one
press or two -- through a file in /run/riparr. Everything after that is decided here,
because only the service knows what is in flight.

## Two promises

One press: **everything already started reaches your library**, then the box turns off.
Nothing new starts -- no queued disc, no Auto Rip -- and jobs that are queued or waiting
for an answer stay exactly as they are for next time.

Two presses: **finish the step you are in.** A rip finishes ripping, and its upload waits
on the card for the next boot. An upload finishes and verifies. A season stops between
episodes. Those are the only places a reboot loses nothing: `rip.recover()` fails
anything caught mid-rip and re-sends anything caught mid-upload from the start.

## Not persisted

A pending shutdown lives in memory. If the box restarts some other way, the wish to turn
it off went with it, and a box that powered itself off on boot because of a press from
yesterday would be the stranger outcome.
"""
import os
import threading
import time

from . import db, platform as P, system as SY

log = SY.component("Power")

REQUEST = os.path.join(P.RUN_DIR, "powerkey.request")
# Off the box there is no /run/riparr. This lets the banner be tried on a laptop.
REQUEST = os.environ.get("RIPARR_POWER_REQUEST", REQUEST)
AFTER_RIP = "after_rip"
AFTER_STEP = "after_step"
HOLD_MESSAGE = ("Riparr is shutting down. Cancel that on the web page to rip this disc.")


def _in_flight():
    from . import rip                     # rip imports this module
    return rip.in_flight()


def _identifying():
    return [j["id"] for j in db.list_jobs(states=("identifying",), limit=50)]


def _sendable():
    """Ripped to the card and handed over, but not yet picked up by the sender.

    The rip worker lets go of a job a moment before the sender takes it. Without this,
    a press landing in that gap would read as "nothing running" and power off with a
    film still on the card.
    """
    return [j["id"] for j in db.list_jobs(states=("transferring",), limit=50)
            if not j.get("bytes_sent") and j.get("local_path")]


def _poweroff():
    return P.power_action("poweroff")


# Everything this module knows about the rest of the box comes through here, which is
# what lets power.test.py check the decisions without a rip running.
hooks = {"in_flight": _in_flight, "identifying": _identifying, "sendable": _sendable,
         "poweroff": _poweroff, "now": time.time}

_lock = threading.Lock()
_state = {"mode": None, "since": None, "fired": False, "error": None}


def reset():
    with _lock:
        _state.update(mode=None, since=None, fired=False, error=None)


def request(kind):
    """A press, as reported by powerkey.py. Upgrades the mode; never downgrades it."""
    if kind not in ("single", "double"):
        log.warning("Ignoring a power-key request that says %r.", kind)
        return
    want = AFTER_STEP if kind == "double" else AFTER_RIP
    with _lock:
        if _state["fired"]:
            return
        if _state["mode"] == AFTER_STEP:
            want = AFTER_STEP
        if want == _state["mode"]:
            return
        _state.update(mode=want, since=_state["since"] or hooks["now"](), error=None)
    log.info("Power button: %s press, shutting down %s.", kind,
             "after the current step" if want == AFTER_STEP else "after the current rip")


def cancel():
    with _lock:
        if _state["fired"]:
            return False, "The box is already shutting down."
        was = _state["mode"]
        _state.update(mode=None, since=None, error=None)
    if was:
        log.info("Shutdown cancelled from the web page.")
    return True, "Cancelled. Riparr carries on as normal."


def holding():
    """True while a shutdown is pending: nothing new may start."""
    return _state["mode"] is not None


def at_step_boundary():
    """True when running work should stop at its next safe point."""
    return _state["mode"] == AFTER_STEP


def waiting_on(mode=None):
    mode = mode or _state["mode"]
    if not mode:
        return []
    ids = set(hooks["in_flight"]()) | set(hooks["identifying"]())
    if mode == AFTER_RIP:
        ids |= set(hooks["sendable"]())
    return sorted(ids)


def status():
    with _lock:
        st = dict(_state)
    st["waiting_on"] = waiting_on(st["mode"]) if st["mode"] else []
    return st


def _consume_request():
    try:
        with open(REQUEST) as f:
            kind = f.read().strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("Could not read the power-key request: %s", e)
        return None
    try:
        os.unlink(REQUEST)                # this is what tells powerkey.py we heard it
    except OSError as e:
        log.warning("Could not clear the power-key request: %s", e)
    return kind


def tick():
    kind = _consume_request()
    if kind:
        request(kind)
    with _lock:
        mode, fired = _state["mode"], _state["fired"]
    if not mode or fired or waiting_on(mode):
        return
    with _lock:
        if _state["mode"] != mode or _state["fired"]:
            return                        # cancelled while we were looking
        _state["fired"] = True
    log.info("Nothing left to wait for; shutting down.")
    ok, message = hooks["poweroff"]()
    if not ok:
        log.error("Could not shut down: %s", message)
        with _lock:
            _state.update(mode=None, since=None, fired=False, error=message)


_stop = threading.Event()
_thread = None


def _loop():
    while not _stop.wait(1):
        try:
            tick()
        except Exception:
            log.exception("Power check failed; trying again in a second.")


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="riparr-power", daemon=True)
    _thread.start()
```

- [ ] **Step 4: Run the test and check it passes**

Run: `python3 server/power.test.py`
Expected: every line `ok`, then `all good`.

- [ ] **Step 5: Commit**

```bash
git add server/riparr/power.py server/power.test.py
git commit -m "Power button: decide when it is safe to shut down"
```

---

### Task 3: Rip engine gates

**Files:**
- Modify: `server/riparr/rip.py`
  - imports (line 40)
  - `_watch_discs` (line 133)
  - `enqueue` (line 189)
  - `_transfer_season` loop (line 2231)
  - `Cancelled` (line 3334)
  - `_run_job` season branch (~line 3379), and its excepts (~line 3417)
  - `_loop` (line 3650)
  - `_send_loop` (line 3665)
  - `_send_one` excepts (~line 3718)
- Test: `server/power-pipeline.test.py`

**Interfaces:**
- Consumes: from Task 2, `power.holding()`, `power.at_step_boundary()`,
  `power.HOLD_MESSAGE`, `power.request(kind)`, `power.cancel()` and `power.reset()`.
- Produces: `rip.in_flight() -> list[int]`, `rip.Deferred` (Exception), and
  `rip._park(job_id, why) -> None`.

- [ ] **Step 1: Write the failing test** — `server/power-pipeline.test.py`

```python
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
power.request("double")
rip._run_job(job)
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
```

- [ ] **Step 2: Run the test and check it fails**

Run: `python3 server/power-pipeline.test.py`
Expected: the first check (`enqueue refused`) FAILs, then the run errors with
`AttributeError: module 'riparr.rip' has no attribute 'in_flight'`.

- [ ] **Step 3: Import `power`** (`rip.py` line 40)

```python
from . import db, tv, led as LED, notify, platform as P, power, shares as SH, system as SY
```

- [ ] **Step 4: Add `Deferred`, `in_flight()` and `_park()`** directly after `class Cancelled` (line 3334, keeping its body)

```python
class Deferred(Exception):
    """Stopped at a safe point because the power button asked for it.

    Not a failure and not a cancellation: the job is parked exactly where a reboot would
    have left it, so the sender picks it up afterwards.
    """


def in_flight():
    """Jobs a worker is inside right now. `power` waits on these."""
    return sorted(_cancel.keys())


def _park(job_id, why):
    """Leave a job on the card for the sender, because a shutdown is waiting on it.

    `transferring` with `bytes_sent=0` and `local_path` set is what `next_sending_job`
    takes and what `recover()` keeps, so this is the state a clean stop has to land in.
    """
    log.info("Job %d: %s; the rest happens after the restart.", job_id, why)
    db.update_job(job_id, state="transferring", bytes_sent=0,
                  phase="Waiting to send to your library")
```

- [ ] **Step 5: Gate `enqueue`.** Add these as the first lines of `enqueue()`'s body, after its docstring:

```python
    # Covers the API and Auto Rip alike. A shutdown is waiting for work to *finish*;
    # starting more would push the moment it can happen out by hours.
    if power.holding():
        return None, power.HOLD_MESSAGE
```

- [ ] **Step 6: Gate `_watch_discs`.** Insert this between the `sig = (...)` line and `if sig == seen:`:

```python
            # Before `seen` is updated, on purpose: a disc that goes in while a
            # shutdown is pending is still "new" if the shutdown is cancelled, so Auto
            # Rip takes it then. A disc already dealt with is not new and is left alone.
            if power.holding():
                continue
```

- [ ] **Step 7: Gate the two workers.** In `_loop`, make the first lines inside `while not _stop.is_set():`:

```python
        if power.holding():
            _wake.wait(5)
            _wake.clear()
            continue
```

In `_send_loop`, make the first lines inside `while not _stop.is_set():`:

```python
        # Only on a double press. A single press is waiting for rips to reach the
        # library, so the sender keeps draining the card.
        if power.at_step_boundary():
            _send_wake.wait(5)
            _send_wake.clear()
            continue
```

- [ ] **Step 8: Park a season after its rip.** In `_run_job`, change the season branch to:

```python
        if job.get("kind") == "tv" and job.get("_plan"):
            base = _rip_season(job, s, cancel_ev)
            LED.announce("done")
            P.eject()
            # Direct mode wrote the episodes straight into the library, so there is no
            # card copy to come back to: the step is the whole job.
            if power.at_step_boundary() and not use_direct(s, "tv"):
                _park(job["id"], "the season is ripped")
                return
            transport, folder, base = _transfer_season(job, s, base, cancel_ev)
            _finish_season(job, s, transport, folder, base)
            return
```

- [ ] **Step 9: Stop between episodes.** In `_transfer_season`, make the first lines inside `for n, row in enumerate(pending, 1):`:

```python
        # Each episode is sent, verified and marked done before the next, so the gap
        # between two of them is a safe point. The first is always sent: a double press
        # means finish this step, and starting the season is the step.
        if n > 1 and not direct and power.at_step_boundary():
            raise Deferred()
```

- [ ] **Step 10: Catch `Deferred`.** In `_run_job`, add this immediately before `except Cancelled:`:

```python
    except Deferred:
        db.stage_end(job_id)
        _park(job_id, "stopped between episodes")
```

In `_send_one`, add this immediately before `except Cancelled:`:

```python
    except Deferred:
        db.stage_end(job["id"])
        _park(job["id"], "stopped between episodes")
```

- [ ] **Step 11: Run the new test and the existing pipelines**

Run:
```bash
python3 server/power-pipeline.test.py && python3 server/tv-pipeline.test.py \
  && python3 server/backup-pipeline.test.py && python3 server/music-pipeline.test.py
```
Expected: each ends `all good`.

- [ ] **Step 12: Commit**

```bash
git add server/riparr/rip.py server/power-pipeline.test.py
git commit -m "Power button: hold new work, and park a season at a safe point"
```

---

### Task 4: LED, API and the banner

**Files:**
- Modify: `server/riparr/led.py` (imports line 50, `STATES` line 82, `current_state()` line 117)
- Modify: `server/riparr/main.py` (imports line 19, `_startup` line 92, `status()` line 362, new endpoints after `system_power` line 1609)
- Modify: `server/static/index.html` (after line 79), `server/static/app.css` (after `.alert.bad`, line 1512), `server/static/app.js` (`LED_WORDS` line 2949, the poweroff handler line 4700, `boot()` line 4782)

**Interfaces:**
- Consumes: from Task 2, `power.holding()`, `power.status()`, `power.cancel()` and `power.start()`.
- Produces: `GET /api/system/power` returning `power.status()`; `POST /api/system/power/cancel`
  returning `{"ok": true, "message": str}` (400 if it was refused); the `"power"` key in `GET /api/status`.

- [ ] **Step 1: LED state.** In `led.py`, change the import line to:

```python
from . import db, platform as P, power, system as SY
```

Add a row to `STATES`, after `"no_wifi"`:

```python
    "shutting_down": (WHITE, "blink"),    # the power button was pressed
```

Make these the first lines of `current_state()`'s body, after its docstring:

```python
    # Above everything, including failure: once somebody has pressed the button, the
    # one thing they need to know is that the box heard them and is not off yet.
    if power.holding():
        return "shutting_down"
```

- [ ] **Step 2: Check the LED by hand**

Run:
```bash
cd server && RIPARR_APPLIANCE=0 RIPARR_DB=/tmp/riparr-led-check.db python3 -c "
from riparr import db, led, power
db.init()
power.hooks.update(in_flight=lambda: [1], identifying=lambda: [], sendable=lambda: [])
print(led.current_state())
power.request('single'); print(led.current_state())
power.cancel(); print(led.current_state())" ; rm -f /tmp/riparr-led-check.db*
```
Expected: a normal state (for example `no_wifi` or `booting`), then `shutting_down`, then
the first state again.

- [ ] **Step 3: API.** In `main.py`:
  - In the `from . import (...)` block, add `power as PWR,` after `platform as P,`.
  - In `_startup()`, add `PWR.start()` after `LED.start()`.
  - In `status()`, add this entry after `"led": {...},`:

```python
        "power": PWR.status(),
```

After `system_power` (ends line 1609), add:

```python
@app.get("/api/system/power")
def system_power_status(user=Depends(require_user)):
    """What a power-button press is waiting for. Polled by the banner every few seconds,
    so it does not go through /api/status and its dozen hardware probes."""
    return PWR.status()


@app.post("/api/system/power/cancel")
def system_power_cancel(user=Depends(require_user)):
    ok, message = PWR.cancel()
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message}
```

- [ ] **Step 4: Banner markup and style.** In `index.html`, after `<main class="content" id="content"></main>` (line 79), add:

```html
  <!-- Outside #content, which every route replaces: the banner has to survive navigation. -->
  <div class="power-banner alert warn hidden" id="power-banner" role="status"></div>
```

In `app.css`, after `.alert.bad` (line 1512), add:

```css
/* The power button's "not off yet". Fixed, because it is about the box, not the page. */
.power-banner {
  position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%);
  z-index: 80; margin: 0; max-width: min(640px, calc(100vw - 32px));
  display: flex; gap: 12px; align-items: center;
}
```

- [ ] **Step 5: LED words, banner logic and copy.** In `app.js`, add an entry to `LED_WORDS` after `no_wifi`:

```javascript
  shutting_down: "White, blinking — shutting down once it's safe to",
```

Directly after the `$("#sys-poweroff").onclick = ...;` handler (ends line 4707), add:

```javascript
/* The power button. A press is only a wish until the rip it is waiting on finishes,
   which can be hours, so the page says so and offers the way back. */
let powerTimer = null;
async function refreshPowerBanner() {
  const el = $("#power-banner");
  let p;
  try { p = await api.get("/api/system/power"); } catch (e) { return; }
  if (p.error && !p.mode) {
    el.innerHTML = `<span>Couldn't shut down: ${esc(p.error)}</span>`;
    el.classList.remove("hidden");
    return;
  }
  if (!p.mode) { el.classList.add("hidden"); return; }
  const what = p.mode === "after_step" ? "after this step" : "once this rip is in your library";
  const n = (p.waiting_on || []).length;
  const wait = p.fired ? "Shutting down now."
             : n ? `Waiting on ${n} job${n === 1 ? "" : "s"}.` : "Nothing left to wait for.";
  el.innerHTML = `<span><strong>Shutting down ${what}.</strong> ${wait}</span>
    ${p.fired ? "" : `<button class="btn sm" id="power-cancel">Cancel</button>`}`;
  el.classList.remove("hidden");
  const btn = $("#power-cancel");
  if (btn) btn.onclick = async () => {
    try { await api.post("/api/system/power/cancel"); toast("Shutdown cancelled"); }
    catch (e) { toast(e.message, "bad"); }
    refreshPowerBanner();
  };
}
```

In `boot()`, after `renderChrome();`, add:

```javascript
  refreshPowerBanner();
  if (!powerTimer) powerTimer = setInterval(refreshPowerBanner, 3000);
```

Then update the confirm copy in the `#sys-poweroff` handler. The box now has a power
button, but a box without one wired still needs replugging:

```javascript
  if (!confirm("Shut down Riparr now?\n\nAnything ripping or uploading stops. To start it "
               + "again, press the power button, or unplug the cable and plug it back in."))
    return;
```

- [ ] **Step 6: Check it in mock mode**

Run: `cd server && RIPARR_POWER_REQUEST=/tmp/pk.request ./run.sh` (http://localhost:8000), and sign in.

1. **Waiting banner.** Start a rip from the Queue page (the mock drive always has a
   disc), then in another terminal run `echo single > /tmp/pk.request`. Within a second
   the banner reads "Shutting down once this rip is in your library. Waiting on 1 job."
   It stays while you move between pages. The System page's LED row says "White,
   blinking — shutting down once it's safe to".
2. **Cancel.** Press **Cancel**. The banner goes, a "Shutdown cancelled" toast shows, and
   the rip carries on.
3. **Shutdown banner.** With nothing running, run `echo single > /tmp/pk.request`. Mock
   mode's `P.power_action` returns `(True, "poweroff (simulated)")`, and the banner
   reads "Shutting down now." with no Cancel button. Restart the server to clear it.

- [ ] **Step 7: Run every test again**

Run: `python3 server/power.test.py && python3 server/power-pipeline.test.py && python3 server/powerkey.test.py`
Expected: `all good` three times.

- [ ] **Step 8: Commit**

```bash
git add server/riparr/led.py server/riparr/main.py \
        server/static/index.html server/static/app.css server/static/app.js
git commit -m "Power button: the LED, the banner, and Cancel"
```

---

### Task 5: Docs, then verification on the box

**Files:**
- Modify: `docs/guide/08-troubleshooting.md` (new section before `## Starting over`)
- Modify: `CHANGELOG.md` (new top section)

- [ ] **Step 1: Add the troubleshooting section** before `## Starting over`

````markdown
## Adding a power button

The Banana Pro already has one: **SW1**, wired to the power chip (AXP209) rather than to
the processor. That matters, because only the power chip can turn a box back *on* — a
button on a CON6 GPIO pin could shut it down but never wake it. So a button on your
enclosure goes **in parallel with SW1**, and then behaves exactly like it.

### Wiring

Use any momentary, normally-open push button and two thin wires.

1. **Unplug the box.**
2. Set a multimeter to continuity, with one probe on a known ground (the USB socket's
   shield, or CON6 pin 6). Touch each of SW1's pads in turn. The pads that beep are
   **ground**. The ones that only beep while you hold SW1 down are **PWRON**.
3. Solder one wire to a ground pad and one to a PWRON pad, and run both to your button.
   Polarity doesn't matter.
4. Check before you close the case: with the box unplugged, pressing your button should
   make PWRON beep against ground, just like pressing SW1.

### What a press does

| You do | The box does |
|---|---|
| **Press once**, idle | Shuts down cleanly. |
| **Press once**, busy | Finishes everything it has started, all the way into your library and verified, then shuts down. Nothing new starts. Queued discs wait for next time. |
| **Press twice** quickly, busy | Finishes the step it's on, then shuts down. A rip finishes ripping, and its upload happens after you turn it back on. An upload finishes and verifies. A TV season stops after the current episode. |
| **Hold 1 s** while off | Turns on. |
| **Hold 6 s** | Cuts power immediately, without shutting down cleanly. Only for when nothing else works. |

While it waits, the LED **blinks white** and every page shows a banner saying what
it's waiting on, with a **Cancel** button.

If Riparr itself isn't responding, a press still turns the box off within ten seconds.
````

- [ ] **Step 2: Add the changelog entry** at the top of `CHANGELOG.md`, above `## 0.6.1`

```markdown
## Unreleased

**The power button works, and it waits for your rip.** Press it once and the box finishes
what it's doing — all the way into your library — then turns off. Press it twice and it
stops after the current step instead, picking up where it left off when you turn it back
on. Press it again to turn it on. The LED blinks white while it waits, and the web page
shows what it's waiting for, with a button to cancel. A button on your enclosure can do
the same: see *Adding a power button* in Troubleshooting.
```

- [ ] **Step 3: Commit the docs**

```bash
git add docs/guide/08-troubleshooting.md CHANGELOG.md
git commit -m "Docs: the power button, and wiring one to the enclosure"
```

- [ ] **Step 4: Install on the box.** Copy the working tree and run the system half, the
  same way the Preparer's copy step does:

```bash
tar -C "$(git rev-parse --show-toplevel)" -cf - --exclude .git --exclude server/.venv . \
  | ssh -i ~/riparr-build/riparr_key root@riparr.local 'tar -xf - -C /opt/riparr'
ssh -i ~/riparr-build/riparr_key root@riparr.local \
  'bash /opt/riparr/packaging/apply-system.sh && systemctl restart riparr && \
   systemctl status riparr-powerkey --no-pager | head -5 && \
   journalctl -u riparr-powerkey -b --no-pager | tail -3 && \
   systemd-inhibit --list | grep -i riparr'
```

Expected:
- `riparr-powerkey` is `active (running)`.
- Its log says `Holding the power key on /dev/input/event0`.
- The inhibitor list shows `Riparr ... handle-power-key block`.

- [ ] **Step 5: Verify on hardware.** Each check except the last powers the box off.
  **Ask the user before each one**, since bringing it back needs them to press the button.

| # | Do | Expect |
|---|---|---|
| a | Idle, press once | The box powers off within ~2 s. Hold 1 s and it boots |
| b | Start a rip, press once | The LED blinks white and the banner says "once this rip is in your library". The rip, upload and verify finish, the job is `done`, then power off |
| c | Start a rip (cache mode), press twice | The rip finishes and ejects, then power off. After boot the job uploads to `done` |
| d | Press once mid-rip, then **Cancel** on the web | The banner goes away, the LED returns to ripping, and the rip completes. The box stays on |
| e | `systemctl stop riparr`, then press once | `journalctl -u riparr-powerkey` shows "Riparr is not running", and the box powers off |
| f | `systemctl stop riparr-powerkey`, then press once | logind powers off at once |
| g | `journalctl -u riparr -b` after (b) | `Power` component lines: the press, the mode, "Nothing left to wait for; shutting down." |

Record the outcome of each check in the session. Any failure goes back to the task that
owns the code.
