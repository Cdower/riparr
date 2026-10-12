#!/usr/bin/env python3
"""What a restored backup puts back, and what a settings write is allowed to say.

A backup carried the shares and the disc history and the restore threw both away,
while telling the user the shares were back and only needed a password. On a fresh
card that meant "No library share" and every disc ever ripped being ripped again.
The export/import path already restored shares correctly; both now do.

Settings arrive from three doors -- the Settings page, an import and a restore --
and none of them looked at the value. A string where a number belongs took the
dashboard down on its next poll.

Run: python3 server/backup-restore.test.py
"""
import json
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="riparr-backup-restore-test-")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"

import asyncio  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from riparr import db, system as SY  # noqa: E402
from riparr import main as MAIN  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


db.init()
db.set("setup_complete", True)
SY.BACKUP_DIR = os.path.join(_tmp, "backups")

print("a backup restores the shares and the disc history")
sid = db.add_share("Media", "nas.local", "Media/Films", "chris", "hunter2",
                   make_default=True)
db.record_disc("fp-1", label="FILM_DISC", title="A Film", kind="movie", ripped_at=1000)
b = SY.create_backup()
with zipfile.ZipFile(os.path.join(SY.BACKUP_DIR, b["name"])) as z:
    payload = json.loads(z.read("riparr.json"))
check("the backup carries the share without its password",
      [(s["host"], s["path"], "password" in s) for s in payload["shares"]],
      [("nas.local", "Media/Films", False)])
check("and the disc", [d["fingerprint"] for d in payload["discs"]], ["fp-1"])

# A fresh card: nothing in either table.
db.delete_share(sid)
db.forget_disc("fp-1")
check("the share is gone", db.list_shares(), [])
check("the disc is gone", db.get_disc("fp-1"), None)

r = SY.restore_backup(b["name"])
check("restore answers ok", r.get("ok"), True)
shares = db.list_shares()
check("the share is back", [(s["host"], s["path"], s["username"]) for s in shares],
      [("nas.local", "Media/Films", "chris")])
check("as the default", [s["is_default"] for s in shares], [1])
check("without a password", db.share_by_id(shares[0]["id"]).get("password") or "", "")
check("and the restore says which share needs one",
      r.get("shares_need_password"), ["nas.local/Media/Films"])
check("the disc is back", (db.get_disc("fp-1") or {}).get("title"), "A Film")
check("and is reported", r.get("discs"), 1)

print("restoring over a box that already has them changes nothing")
r = SY.restore_backup(b["name"])
check("still one share", len(db.list_shares()), 1)
check("nothing newly restored", r.get("shares_restored"), [])

print("a bare export file restores the same way")
db.delete_share(db.list_shares()[0]["id"])
r = SY.import_backup("riparr-config.json", json.dumps(payload).encode("utf-8"))
check("the share is back from the JSON", len(db.list_shares()), 1)

print("a settings value of the wrong shape is refused")
check("a string where a number belongs", db.check_setting("warn_key_days", "7 days") is None, False)
check("a number is fine", db.check_setting("warn_key_days", 3), None)
check("a bool where a number belongs is not a number",
      db.check_setting("warn_key_days", True) is None, False)
check("an unknown transfer mode", db.check_setting("transfer_mode", "teleport") is None, False)
check("a known one", db.check_setting("transfer_mode", "burst"), None)
check("a share id may be null", db.check_setting("movie_share_id", None), None)
check("or a number", db.check_setting("movie_share_id", 4), None)
check("but not a word", db.check_setting("movie_share_id", "four") is None, False)
check("a list setting must be a list", db.check_setting("audio_languages", "eng") is None, False)
check("a key nobody defined", db.check_setting("made_up", 1) is None, False)

r = SY.import_backup("bad.json", json.dumps(
    {"settings": {"warn_key_days": "7 days", "theme": "plex"}}).encode("utf-8"))
check("an import keeps the good values", db.get("theme"), "plex")
check("and skips the bad one", db.get("warn_key_days"), 7)
check("and says so", r.get("rejected"), {"warn_key_days": db.check_setting("warn_key_days", "7 days")})



class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def put_settings(body):
    try:
        asyncio.run(MAIN.put_settings(FakeRequest(body), user=None))
        return 200, ""
    except HTTPException as e:
        return e.status_code, e.detail


code, detail = put_settings({"warn_key_days": "7 days", "theme": "plex"})
check("the settings endpoint refuses it", code, 400)
check("and names the key", "warn_key_days" in detail, True)
check("without writing it", db.get("warn_key_days"), 7)
check("or anything else in the same save", db.get("theme"), "plex")
code, _ = put_settings({"warn_key_days": 5})
check("a good value is accepted", code, 200)
check("and written", db.get("warn_key_days"), 5)
check("the dashboard still answers", isinstance(MAIN.status(user=None), dict), True)

print("")
shutil.rmtree(_tmp, ignore_errors=True)
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
