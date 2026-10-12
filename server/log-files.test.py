#!/usr/bin/env python3
"""Where the two log files go, and that the choice takes effect without a restart.

The files exist for bug reports and nothing else reads them, so a user who would rather
spare the SD card can move them to memory or turn them off. This pins the three modes:
the right directory gets the lines, the old one stops getting them, the listing follows
the active directory, and the Events table keeps recording whatever the files are doing.

Run: python3 server/log-files.test.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="riparr-log-files-test-")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"

from riparr import db  # noqa: E402
from riparr import system as SY  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def events_say(text):
    rows = db.conn().execute("SELECT message FROM events").fetchall()
    return any(text in r["message"] for r in rows)


# Stand in for /run/riparr, which only exists on the box.
SY.RUNDIR = os.path.join(_tmp, "run")
os.makedirs(SY.RUNDIR)

db.init()
SY.init()
card = SY.LOG_DIR
memory = os.path.join(SY.RUNDIR, "logs")

# ── card, the default ──
print("card")
check("default mode", SY.log_mode(), "card")
check("active directory is beside the database", SY.log_dir(), card)
SY.component("Test").info("line one")
check("riparr.txt has the line", "line one" in read(os.path.join(card, "riparr.txt")), True)
check("riparr.debug.txt has the line",
      "line one" in read(os.path.join(card, "riparr.debug.txt")), True)
check("listing names both files",
      sorted(f["name"] for f in SY.log_files()), ["riparr.debug.txt", "riparr.txt"])
check("log_path resolves a live file",
      SY.log_path("riparr.txt"), os.path.join(card, "riparr.txt"))
check("events table has the line", events_say("line one"), True)

# ── memory ──
print("memory")
db.set("log_files", "memory")
SY.apply_log_setting()
check("mode", SY.log_mode(), "memory")
check("active directory is under the runtime directory", SY.log_dir(), memory)
SY.component("Test").info("line two")
check("memory riparr.txt has the line",
      "line two" in read(os.path.join(memory, "riparr.txt")), True)
check("card riparr.txt did not get it",
      "line two" in read(os.path.join(card, "riparr.txt")), False)
check("listing follows the active directory",
      all(SY.log_path(f["name"]).startswith(memory) for f in SY.log_files()), True)
check("events table has the line", events_say("line two"), True)

# ── off ──
print("off")
db.set("log_files", "off")
SY.apply_log_setting()
check("mode", SY.log_mode(), "off")
check("no active directory", SY.log_dir(), None)
SY.component("Test").info("line three")
check("card riparr.txt did not get it",
      "line three" in read(os.path.join(card, "riparr.txt")), False)
check("memory riparr.txt did not get it",
      "line three" in read(os.path.join(memory, "riparr.txt")), False)
check("listing is empty", SY.log_files(), [])
check("log_path finds nothing", SY.log_path("riparr.txt"), None)
check("deleting is a no-op", SY.delete_log_files(), 0)
check("events table still has the line", events_say("line three"), True)

# ── back to the card, and the old file is appended to, not replaced ──
print("card again")
db.set("log_files", "card")
SY.apply_log_setting()
SY.component("Test").info("line four")
text = read(os.path.join(card, "riparr.txt"))
check("line one is still there", "line one" in text, True)
check("line four was appended", "line four" in text, True)

# ── an unknown value means the default ──
print("unknown value")
db.set("log_files", "floppy")
check("falls back to the card", SY.log_mode(), "card")

# ── memory with no runtime directory: a temp directory, not a crash ──
print("no runtime directory")
SY.RUNDIR = os.path.join(_tmp, "missing")
db.set("log_files", "memory")
SY.apply_log_setting()
check("falls back to the system temp directory",
      os.path.dirname(SY.log_dir()), tempfile.gettempdir())
SY.component("Test").info("line five")
check("and writes there", "line five" in read(os.path.join(SY.log_dir(), "riparr.txt")), True)

# ── traversal is still refused ──
print("log_path")
db.set("log_files", "card")
SY.apply_log_setting()
check("rejects a slash", SY.log_path("../riparr.db"), None)
check("rejects a dotfile", SY.log_path(".hidden"), None)

print()
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all passed")
