#!/usr/bin/env python3
"""MakeMKV talks far faster than anyone needs to hear, and three handlers blocked the loop.

`makemkvcon` writes a PRGV line many times a second. Each one used to cost a directory
listing and an UPDATE jobs with a commit, on a 1 GHz board that is also serving the
browser's polls. Separately, `put_settings`, `config_import` and the backup upload were
`async def` handlers doing SQLite and zip work directly on the event loop, so every poll
in flight waited for them.

Run: python3 server/progress-throttle.test.py
"""
import inspect
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="riparr-throttle-test-")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"

from riparr import db, main, rip  # noqa: E402

failures = []


def check(name, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else ": got %r, wanted %r" % (got, want)))
    if not ok:
        failures.append(name)


db.init()


class FakeProc:
    """Sixty PRGV lines ten milliseconds apart, then a MSG, like a fast-talking makemkvcon."""

    def __init__(self, *a, **k):
        self.stdout = self._lines()
        self.returncode = 0

    def _lines(self):
        for _ in range(60):
            time.sleep(0.01)
            yield "PRGV:1,2,3\n"
        yield 'MSG:5003,0,0,"done"\n'

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass


print("PRGV lines are written at most once per interval")
out_dir = os.path.join(_tmp, "out")
os.makedirs(out_dir)
open(os.path.join(out_dir, "t.mkv"), "w").close()      # so the run has a file to return
sizes = iter(range(1000, 10 ** 6, 1000))
rip.subprocess.Popen = FakeProc
rip._output_size = lambda d: next(sizes)
rip.PROGRESS_INTERVAL = 0.1
calls = {"progress": 0, "first": 0}


def on_progress(done, eta, phase):
    calls["progress"] += 1


def on_first_byte():
    calls["first"] += 1


rip._run_makemkv({"id": 1, "_device": "/dev/null"}, {"min_title_seconds": 120}, 0,
                 out_dir, threading.Event(), 10 ** 9,
                 on_progress=on_progress, on_first_byte=on_first_byte)
check("the first byte is announced exactly once", calls["first"], 1)
check("60 lines in about 0.6 s at 0.1 s is a handful of writes, not 60",
      3 <= calls["progress"] <= 12, True)
print("       (%d on_progress calls)" % calls["progress"])

print("\nsettings, import and upload run on the threadpool")
for fn in (main.put_settings, main.config_import, main.system_backup_upload):
    check("%s is not a coroutine function" % fn.__name__, inspect.iscoroutinefunction(fn), False)
check("put_settings takes the body directly",
      main.put_settings({"theme": "dark"}, user=None)["theme"], "dark")

print("\n%d failures" % len(failures) if failures else "\nall good")
sys.exit(1 if failures else 0)
