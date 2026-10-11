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
CHECK_FAILED = "Riparr could not tell what is still running, so it has not shut down."
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
    st["waiting_on"] = []
    if st["mode"]:
        try:
            st["waiting_on"] = waiting_on(st["mode"])
        except Exception:
            # The status endpoint feeds the banner; it must never fail because a check did.
            st["error"] = st["error"] or CHECK_FAILED
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
    if not mode or fired:
        return
    try:
        waiting = waiting_on(mode)
    except Exception:
        # Powering off on a guess could cut a rip off, so an unknown state means wait.
        log.exception("Could not tell what is still running; trying again in a second.")
        with _lock:
            if _state["mode"] == mode:
                _state["error"] = CHECK_FAILED
        return
    with _lock:
        if _state["mode"] == mode and _state["error"] == CHECK_FAILED:
            _state["error"] = None
    if waiting:
        return
    with _lock:
        if _state["mode"] != mode or _state["fired"]:
            return                        # cancelled while we were looking
        _state["fired"] = True
    log.info("Nothing left to wait for; shutting down.")
    try:
        ok, message = hooks["poweroff"]()
    except Exception as e:
        # Left as it was, fired would stay set and strand the box in a shutdown that
        # never comes and cannot be cancelled.
        log.exception("Shutting down raised.")
        ok, message = False, "Riparr could not shut down: %s" % e
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
