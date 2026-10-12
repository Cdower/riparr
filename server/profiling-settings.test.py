#!/usr/bin/env python3
"""The profiler's settings: shaped right, masked right, applied on save.

Run: python3 server/profiling-settings.test.py
"""
import os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
tmp = tempfile.mkdtemp()
os.environ["RIPARR_DB"] = os.path.join(tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"

from fastapi import HTTPException
from riparr import db, main, profiling as PR
db.init()

failures = []
def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  got=%r want=%r" % (got, want)))
    if not ok: failures.append(name)

check("defaults", {k: db.DEFAULTS[k] for k in ("profiling_enabled", "profiling_url", "profiling_user", "profiling_token")},
      {"profiling_enabled": False, "profiling_url": "", "profiling_user": "", "profiling_token": ""})
check("a string where a bool belongs is refused", db.check_setting("profiling_enabled", "yes") is None, False)
check("a url string is accepted", db.check_setting("profiling_url", "https://x.grafana.net"), None)
check("the token is a secret", "profiling_token" in main.SECRET_SETTINGS, True)
db.set("profiling_token", "glc_abc")
check("the token is masked", main._redact(db.all_settings())["profiling_token"], main.SECRET_MASK)
db.set("profiling_token", "")
check("an empty token is not masked", main._redact(db.all_settings())["profiling_token"], "")

# The test button: 400 with the reason when it fails, the result when it works.
PR.test = lambda: {"ok": False, "error": "HTTP 401: nope"}
try:
    main.profiling_test(user=None); check("a failed test is a 400", True, False)
except HTTPException as e:
    check("a failed test is a 400 with the reason", (e.status_code, e.detail), (400, "HTTP 401: nope"))
PR.test = lambda: {"ok": True, "error": None}
check("a passing test returns the result", main.profiling_test(user=None), {"ok": True, "error": None})
check("GET /api/profiling is the module status", set(main.profiling_status(user=None)) >=
      {"enabled", "running", "uploading", "mode", "last_upload_at", "last_error"}, True)

# Saving any profiling_* key re-applies the module.
applied = []
PR.apply = lambda: applied.append(1)
import asyncio
class Req:
    async def json(self): return {"profiling_enabled": False, "profiling_url": "https://x"}
# put_settings may be sync (Task 5 makes it so) or async (before Task 5): handle both.
import inspect
if inspect.iscoroutinefunction(main.put_settings):
    out = asyncio.run(main.put_settings(Req(), user=None))
else:
    out = main.put_settings({"profiling_enabled": False, "profiling_url": "https://x"}, user=None)
check("saving a profiling_* key applies the module", applied, [1])
check("the saved url comes back", out["profiling_url"], "https://x")

# The other two doors (config import, backup restore) share system.restore_settings,
# so the hook lives there too -- and only fires when a profiling_* key actually landed.
from riparr import system as SY
applied.clear()
SY.restore_settings({"profiling_url": "https://y"})
check("restoring a profiling_* key applies the module", applied, [1])
check("and stored it", db.get("profiling_url"), "https://y")
applied.clear()
SY.restore_settings({"profiling_enabled": "yes"})
check("a rejected profiling_* key does not apply", applied, [])
other = next(k for k in db.DEFAULTS if not k.startswith("profiling_") and k != "log_files")
SY.restore_settings({other: db.DEFAULTS[other]})
check("an unrelated key does not apply", applied, [])

print("\n%d failures" % len(failures) if failures else "\nall good")
sys.exit(1 if failures else 0)
