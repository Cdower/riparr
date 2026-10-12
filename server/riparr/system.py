"""
The four things the System pages report on: events, scheduled tasks, log files, backups.

These exist because an appliance with no screen has to be able to answer "what has this
thing been doing?" from the web interface alone. The *arrs settled this shape years ago
and it is worth copying wholesale (see DECISIONS.md D12): a rolling event log, a table
of recurring tasks with last/next execution, downloadable log files, and dated backups.

Two appliance-specific constraints shape everything here:

  * **The SD card is the enemy.** Every write is a write to flash that will eventually
    wear out, so the log files are small and few, the event table is capped and trimmed
    on a schedule, and nothing polls tightly.
  * **Power gets pulled.** The scheduler keeps its state in SQLite (WAL, D4) rather than
    in memory, so "last execution" survives a yank and tasks do not all stampede at once
    on the next boot.
"""
import glob
import io
import json
import logging
import logging.handlers
import os
import tempfile
import threading
import time
import zipfile

from . import db
from . import platform as P
from . import shares as SH

# Everything lives beside the database, so one directory is the whole appliance state.
DATA_DIR = os.path.dirname(db.DB_PATH)
LOG_DIR = os.path.join(DATA_DIR, "logs")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

# systemd's RuntimeDirectory= for the service: tmpfs on every systemd system, owned by
# the service account, and already in use for the MakeMKV-installer handshake. It is the
# "memory" home for the log files. Not /tmp: riparr.service sets PrivateTmp=, which hands
# the service a bind of the host's /tmp, and Raspberry Pi OS keeps that on the card.
RUNDIR = "/run/riparr"

# The `log_files` setting. Anything else is read as "card".
LOG_MODES = ("card", "memory", "off")

# Small and few, deliberately. 1 MB x 5 is about a fortnight of ordinary operation and
# caps the log directory at 10 MB across both files -- see the SD-card note above.
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 5

EVENT_CAP = 5000          # rows kept in the events table; trimmed by Clean Up Events
BACKUP_KEEP = 5           # dated backups kept; the oldest is dropped past this

log = logging.getLogger("riparr")


def component(name):
    """A child logger whose name becomes the Component column on the Events page.

    Prowlarr's Events table is only useful because every row says which part of the app
    spoke. A single "riparr" for everything would make the column dead weight.
    """
    return logging.getLogger("riparr." + name)


# ─────────────────────────────── events ───────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY,
  at        INTEGER NOT NULL,
  level     TEXT NOT NULL,
  component TEXT NOT NULL,
  message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_at ON events(at DESC);

CREATE TABLE IF NOT EXISTS task_runs (
  name        TEXT PRIMARY KEY,
  last_start  INTEGER,
  last_end    INTEGER,
  last_error  TEXT
);

CREATE TABLE IF NOT EXISTS task_history (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL,
  queued_at INTEGER NOT NULL,
  started_at INTEGER,
  ended_at  INTEGER,
  trigger   TEXT NOT NULL,
  error     TEXT
);
CREATE INDEX IF NOT EXISTS task_history_at ON task_history(queued_at DESC);
"""


class DbHandler(logging.Handler):
    """Mirrors the Python log into SQLite so the Events page has something to show.

    Attached alongside the file handler rather than instead of it: the file is what you
    download and send to someone, the table is what you page through in the browser.
    Failures here are swallowed on purpose -- a broken log must never take down a rip.
    """

    def emit(self, record):
        try:
            db.conn().execute(
                "INSERT INTO events(at, level, component, message) VALUES(?,?,?,?)",
                (int(record.created), record.levelname.lower(),
                 record.name.replace("riparr.", "") or "riparr",
                 self.format(record)))
            db.conn().commit()
        except Exception:
            pass


def init():
    """Create the tables and attach the handlers. Safe to call more than once."""
    c = db.conn()
    c.executescript(SCHEMA)
    c.commit()

    if getattr(log, "_riparr_configured", False):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)

    dbh = DbHandler()
    dbh.setLevel(logging.INFO)
    dbh.setFormatter(logging.Formatter("%(message)s"))

    log.setLevel(logging.DEBUG)
    log.addHandler(dbh)
    apply_log_setting()
    log._riparr_configured = True
    component("Riparr").info("Starting up")


# ─────────────────────────────── log files ───────────────────────────────

def log_mode():
    """The `log_files` setting, with anything unrecognised read as the default."""
    mode = db.get("log_files")
    return mode if mode in LOG_MODES else "card"


def log_dir():
    """Where the two log files are being written right now, or None when they are off."""
    mode = log_mode()
    if mode == "off":
        return None
    if mode == "memory":
        return _memory_dir()
    return LOG_DIR


def _memory_dir():
    # On the box /run/riparr always exists (RuntimeDirectory=). On a developer's machine
    # it does not, and the system temp directory is the honest stand-in.
    if os.path.isdir(RUNDIR) and os.access(RUNDIR, os.W_OK):
        return os.path.join(RUNDIR, "logs")
    return os.path.join(tempfile.gettempdir(), "riparr-logs")


# The handlers currently writing the files, so a change of setting can close them.
_file_handlers = []


def apply_log_setting():
    """Point the file handlers at wherever `log_files` says, replacing any that are open.

    Called at startup and again whenever the setting is saved, so the change takes
    effect without a restart. The Events handler is untouched: it is the operational
    record and lives in the database regardless.
    """
    for h in _file_handlers:
        log.removeHandler(h)
        h.close()
    _file_handlers.clear()

    where = log_dir()
    if where is None:
        return
    fmt = logging.Formatter("%(asctime)s|%(levelname)s|%(name)s|%(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    # Two files, matching the *arrs: riparr.txt is the ordinary record, riparr.debug.txt
    # keeps everything. Whoever is helping you asks for the debug one.
    try:
        os.makedirs(where, exist_ok=True)
        for name, level in (("riparr.txt", logging.INFO),
                            ("riparr.debug.txt", logging.DEBUG)):
            h = logging.handlers.RotatingFileHandler(
                os.path.join(where, name), maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUPS, encoding="utf-8")
            h.setLevel(level)
            h.setFormatter(fmt)
            log.addHandler(h)
            _file_handlers.append(h)
    except OSError as e:
        # A log directory that cannot be written is not a reason to refuse to start.
        # The Events page still records everything that matters.
        component("Logs").warning("Cannot write log files to %s (%s); none are being "
                                  "kept", where, e)


def events(limit=50, offset=0, levels=None):
    """Newest first, which is the only order this is ever read in."""
    sql = "SELECT * FROM events"
    args = []
    if levels:
        sql += " WHERE level IN (%s)" % ",".join("?" * len(levels))
        args += list(levels)
    total = db.conn().execute(
        sql.replace("SELECT *", "SELECT COUNT(*) c"), args).fetchone()["c"]
    sql += " ORDER BY at DESC, id DESC LIMIT ? OFFSET ?"
    rows = db.conn().execute(sql, args + [limit, offset]).fetchall()
    return {"total": total, "events": [dict(r) for r in rows]}


def clear_events():
    c = db.conn()
    c.execute("DELETE FROM events")
    c.commit()
    component("Events").info("Event log cleared")


def log_files():
    """Every rotated file, newest write first, as the Log Files page lists them."""
    where = log_dir()
    if where is None:
        return []
    out = []
    for path in glob.glob(os.path.join(where, "*.txt*")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({"name": os.path.basename(path),
                    "size": st.st_size,
                    "modified": int(st.st_mtime)})
    out.sort(key=lambda f: f["modified"], reverse=True)
    return out


def log_path(name):
    """Resolve a log filename, refusing anything that escapes the log directory."""
    where = log_dir()
    if where is None or "/" in name or "\\" in name or name.startswith("."):
        return None
    path = os.path.join(where, name)
    if os.path.realpath(os.path.dirname(path)) != os.path.realpath(where):
        return None
    return path if os.path.isfile(path) else None


def delete_log_files():
    """Clears the rotated backups, leaving the two files currently being written to.

    Deleting the live files out from under an open RotatingFileHandler leaves it writing
    to an unlinked inode, and the log silently stops appearing on disk.
    """
    live = {"riparr.txt", "riparr.debug.txt"}
    where = log_dir()
    n = 0
    for f in log_files():
        if f["name"] in live:
            continue
        try:
            os.unlink(os.path.join(where, f["name"]))
            n += 1
        except OSError:
            pass
    component("Logs").info("Deleted %d rotated log file(s)", n)
    return n


# ─────────────────────────────── backups ───────────────────────────────

def backups():
    out = []
    for path in glob.glob(os.path.join(BACKUP_DIR, "riparr_backup_*.zip")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({"name": os.path.basename(path),
                    "size": st.st_size,
                    "modified": int(st.st_mtime),
                    "kind": "scheduled" if "_scheduled_" in path else "manual"})
    out.sort(key=lambda b: b["modified"], reverse=True)
    return out


def backup_path(name):
    if "/" in name or "\\" in name or not name.endswith(".zip"):
        return None
    path = os.path.join(BACKUP_DIR, name)
    if os.path.realpath(os.path.dirname(path)) != os.path.realpath(BACKUP_DIR):
        return None
    return path if os.path.isfile(path) else None


def create_backup(kind="manual"):
    """A zip of the settings and the disc history -- everything that is not re-derivable.

    Deliberately *not* a copy of the database file: a live SQLite file plus its WAL is
    not safe to copy, and restoring a binary DB across a schema change is exactly the
    kind of thing that bricks an appliance. JSON restores into any later schema.
    """
    from . import __version__
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y.%m.%d_%H.%M.%S")
    name = "riparr_backup_v%s_%s_%s.zip" % (__version__, kind, stamp)
    path = os.path.join(BACKUP_DIR, name)

    payload = {
        "version": __version__,
        "created_at": int(time.time()),
        "settings": db.all_settings(),
        "shares": [dict(s) for s in db.list_shares()],
        "discs": [dict(d) for d in db.list_discs(limit=100000)],
    }
    # Credentials are in the share rows. A backup is a file the user downloads and mails
    # to themselves, so the password never goes in it -- the restore re-prompts.
    for s in payload["shares"]:
        s.pop("password", None)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("riparr.json", json.dumps(payload, indent=2))
    _prune_backups()
    component("Backup").debug("Wrote %s (%d bytes)", name, os.path.getsize(path))
    return {"name": name, "size": os.path.getsize(path), "kind": kind}


def _prune_backups():
    extra = backups()[BACKUP_KEEP:]
    for b in extra:
        try:
            os.unlink(os.path.join(BACKUP_DIR, b["name"]))
        except OSError:
            pass


def delete_backup(name):
    path = backup_path(name)
    if not path:
        return False
    os.unlink(path)
    component("Backup").info("Deleted %s", name)
    return True


def import_backup(filename, data):
    """Accept an uploaded backup and restore from it.

    Takes the bytes rather than a path so the caller never has to write an untrusted
    file into the backup directory before deciding whether it is one of ours. Both the
    zip this module writes and the bare riparr.json from Settings > Export are accepted,
    because a user who exported before backups existed still has the JSON.
    """
    try:
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                payload = json.loads(z.read("riparr.json"))
        else:
            payload = json.loads(data.decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": "Not a Riparr backup (%s)" % e}
    if not isinstance(payload.get("settings"), dict):
        return {"ok": False, "error": "Not a Riparr backup: no settings in it"}
    return _apply(payload, filename)


def restore_backup(name):
    path = backup_path(name)
    if not path:
        return {"ok": False, "error": "No such backup"}
    with zipfile.ZipFile(path) as z:
        payload = json.loads(z.read("riparr.json"))
    return _apply(payload, name)


def _apply(payload, name):
    """Put back everything the backup carried: settings, shares and the disc history.

    It used to restore the settings alone and throw the other two away -- while
    answering with a share count and a note about passwords, so the toast read as if
    the shares were back. On a fresh card that meant "No library share", and a disc
    table so empty that every film ever ripped was ripped again instead of refused.
    Settings > Export/Import had the share-restoring loop all along; both doors now
    go through the same three functions.
    """
    n, rejected = restore_settings(payload.get("settings") or {})
    restored, need_password = restore_shares(payload.get("shares") or [])
    discs = restore_discs(payload.get("discs") or [])
    component("Backup").warning(
        "Restored %d setting(s), %d share(s) and %d disc(s) from %s",
        n, len(restored), discs, name)
    out = {"ok": True, "settings": n, "rejected": rejected,
           "shares_restored": restored, "shares_need_password": need_password,
           "discs": discs}
    if need_password:
        out["note"] = ("Share passwords are not stored in a backup: re-enter the "
                       "password for %s before it can be used." % ", ".join(need_password))
    return out


def restore_settings(settings):
    """Store every setting that is a setting. Returns (stored, {key: why rejected}).

    The cookie key is never restored -- a backup is a file that gets mailed about.
    Anything that fails db.check_setting is skipped rather than stored, because a
    hand-edited file is exactly the input this door sees, and one bad value used to
    take the dashboard down on its next poll.
    """
    n, rejected = 0, {}
    for k, v in settings.items():
        if k == "session_secret":
            continue
        why = db.check_setting(k, v)
        if why:
            rejected[k] = why
            continue
        db.set(k, v)
        n += 1
    # Registering the key is not a db.set -- see main.put_settings.
    if "makemkv_key" in settings and "makemkv_key" not in rejected:
        from . import makemkv as MK
        MK.apply_key(settings["makemkv_key"])
        MK.record_expiry_for(settings["makemkv_key"])
    db.reconcile(settings)
    # The settings that drive a running thread follow the value the moment it lands,
    # whichever door it came through -- a restore used to leave the profiler uploading
    # with the old credentials, and the logs in the old place, until the next restart.
    # main.put_settings does the same for its door.
    if "log_files" in settings and "log_files" not in rejected:
        apply_log_setting()
    if any(k.startswith("profiling_") and k not in rejected for k in settings):
        from . import profiling as PROF  # imports system; so not at module level
        PROF.apply()
    return n, rejected


def restore_shares(shares):
    """Add every share not already present. Returns (restored, needing a password).

    Passwords are deliberately not in a backup, so a restored share arrives without
    one and is reported as needing it rather than silently created broken. A share
    already on the box (same host and path) is left exactly as it is, password and
    all.
    """
    existing = {(s["host"], s["path"]) for s in db.list_shares()}
    restored, need_password = [], []
    for sh in shares:
        host, path = sh.get("host"), sh.get("path")
        if not host or not path or (host, path) in existing:
            continue
        db.add_share(sh.get("name") or "%s/%s" % (host, path), host, path,
                     sh.get("username") or "", "",
                     make_default=bool(sh.get("is_default")))
        existing.add((host, path))
        restored.append("%s/%s" % (host, path))
        need_password.append("%s/%s" % (host, path))
    return restored, need_password


def restore_discs(discs):
    """Remember every disc the backup knew that this box does not. Returns how many.

    A disc the box already has is left alone: its record may be newer than the
    backup's. Only columns this schema has are written, so a backup from a later
    release restores into an earlier one without a crash.
    """
    columns = {r["name"] for r in db.conn().execute("PRAGMA table_info(discs)")}
    n = 0
    for d in discs:
        fp = d.get("fingerprint")
        if not fp or db.get_disc(fp):
            continue
        fields = {k: v for k, v in d.items() if k != "fingerprint" and k in columns}
        db.record_disc(fp, **fields)
        n += 1
    return n


# ─────────────────────────────── scheduled tasks ───────────────────────────────

def _task_check_health():
    P.system_status()
    P.optical_diagnosis()
    c = P.clock_status()
    if not c["plausible"]:
        component("Clock").error(
            "The system clock reads %s, which cannot be right. Anything Riparr says "
            "about dates -- key expiry, when a rip finished, when a task is due -- is "
            "wrong until the time syncs.",
            time.strftime("%d %b %Y %H:%M", time.localtime(c["now"])))
    elif c["synced"] is False:
        component("Clock").warning("The clock has not synchronised with a time server yet.")
    return "Health checked"


def _task_update_check():
    """Look for a new release and, if there is one, say so once.

    Two things this deliberately does not do.

    **It does not install anything.** Replacing the software while somebody is halfway
    through ripping a disc, because a machine decided the moment was right, is not a
    courtesy. The notification is a nudge; System -> Updates is where a person says yes.

    **It does not nag.** The check runs every six hours, and a box left alone for a
    fortnight would otherwise send the same notification fifty-six times. The version
    last announced is remembered, so each release is mentioned exactly once -- and a
    *newer* release than the one already announced is mentioned again, which is the
    behaviour that matters.

    `auto_check_updates` is honoured here, which it previously was not: the switch was
    on the settings page, defaulted to True, and was read by nothing at all. A setting
    that does nothing in either position is worse than no setting, because it answers
    the question wrongly instead of not answering it.
    """
    from . import updater
    if not db.get("auto_check_updates", True):
        return "Update check: skipped (turned off in settings)"

    r = updater.check()
    status = r.get("status", "unknown")
    if status != "update":
        return "Update check: %s" % status

    latest = r.get("latest") or ""
    component("Update").info("Version %s is available", latest)
    if db.get("last_update_announced", "") != latest:
        from . import notify
        notify.send(
            "update_available",
            title="Riparr %s is available" % latest,
            body=("You're on %s. System → Updates installs it and restarts, and "
                  "nothing is changed until you say so." % r.get("current", "?")))
        db.set("last_update_announced", latest)
    return "Update check: %s available" % latest


def _task_key_check():
    """The MakeMKV beta key expires roughly monthly and a lapsed key stops every rip.

    This is the single most likely way for a working box to quietly stop working, so it
    gets its own task rather than being folded into the health check. A beta key is
    renewed by the lookup itself (makemkv._maybe_renew_inner); what is left here is
    saying so when that could not happen.
    """
    # Ask the sources first, so the countdown does not depend on somebody having opened
    # the Settings page recently. A scheduled task can afford the wait, and forces a
    # fresh lookup -- the cache lives as long as the interval, so it would otherwise
    # answer with the lookup from last time. The backup service covers the forum when
    # it is unusable, which it frequently is.
    from . import makemkv as MK
    renewal_before = db.get("makemkv_key_renewal")
    try:
        MK.beta_key(force=True)
    except Exception:
        pass                              # a stale date still counts down correctly

    st = P.makemkv_status()
    days = st.get("days_left")
    if db.get("makemkv_key_renewal") != renewal_before:
        return "Renewed the beta key%s" % (
            ", good until %s" % st["key_expires"] if st.get("key_expires") else "")
    if st.get("key_stale"):
        component("MakeMKV").warning("A newer beta key has been published")
        from . import notify
        notify.send(
            "key_expiring",
            title="A newer MakeMKV key has been published",
            body=("The key on this box is an older one and may already have lapsed. "
                  "Settings \u2192 General fetches the current one in a click."))
        return "A newer key has been published"
    if days is None:
        return "No MakeMKV key registered"
    if days <= db.get("warn_key_days", 7):
        component("MakeMKV").warning("Key expires in %s day(s)", days)
        # Notified rather than only logged: a lapsed key is the classic "came home
        # after a month, ripped five discs, all five failed" -- and every warning
        # Riparr had for it lived on a page nobody had open.
        from . import notify
        notify.send(
            "key_expiring",
            title="MakeMKV key expires in %d day%s" % (days, "" if days == 1 else "s"),
            body=(("Riparr renews the beta key itself once the next one is published."
                   if db.get("auto_renew_beta_key", True) and st.get("key_type") == "beta"
                   else "Every rip fails the day it lapses. Settings \u2192 General "
                        "fetches the current beta key in one click."))
                 if days > 0 else
                 "The key has expired. Rips will fail until it's replaced.")
    return "MakeMKV key: %s day(s) left" % days


def _task_share_check():
    """Every share something is configured to write to, not just the default one.

    Films and television can be on different machines. Checking only the default share
    means the health check passes while the machine half the rips are bound for has
    been asleep for a week.
    """
    ids = db.shares_in_use()
    if not ids:
        return "No share configured"
    # Actually asked, with the same `smbclient` the sender uses. This used to list the
    # hostnames and call them reachable, and said so every hour of a night the sender
    # spent waiting on a name that no longer resolved.
    up, down = [], []
    for sid in ids:
        share = db.share_by_id(sid)
        if share:
            (up if SH.Transport(share).reachable() else down).append(share["host"])
    if down:
        raise RuntimeError("Share%s %s not answering" % (
            "s" if len(down) > 1 else "", ", ".join(down)))
    return "Share%s %s reachable" % ("s" if len(up) > 1 else "", ", ".join(up))


def _task_backup():
    b = create_backup(kind="scheduled")
    return "Wrote %s" % b["name"]


def _task_cleanup():
    c = db.conn()
    row = c.execute("SELECT COUNT(*) n FROM events").fetchone()
    excess = row["n"] - EVENT_CAP
    if excess > 0:
        c.execute("DELETE FROM events WHERE id IN "
                  "(SELECT id FROM events ORDER BY at ASC, id ASC LIMIT ?)", (excess,))
    c.execute("DELETE FROM task_history WHERE queued_at < ?",
              (int(time.time()) - 7 * 86400,))
    c.commit()
    return "Trimmed %d event(s)" % max(excess, 0)


# name, label, interval in seconds, function
TASKS = [
    ("health",  "Check Health",             6 * 3600,  _task_check_health, "Health"),
    ("update",  "Application Update Check", 6 * 3600,  _task_update_check, "Update"),
    # Six-hourly, not daily: the old key lapses at month-end and the new one appears
    # some hours later, and every hour between the two is an hour no disc can be read.
    ("key",     "MakeMKV Key Check",        6 * 3600,  _task_key_check,    "MakeMKV"),
    ("share",   "Share Check",              3600,      _task_share_check,  "Share"),
    ("backup",  "Backup",                   7 * 86400, _task_backup,       "Backup"),
    ("cleanup", "Clean Up Events",          24 * 3600, _task_cleanup,      "Housekeeping"),
]
_BY_NAME = {t[0]: t for t in TASKS}


def task_list():
    """The Scheduled table: interval, last execution, last duration, next execution."""
    now = int(time.time())
    rows = {r["name"]: r for r in db.conn().execute("SELECT * FROM task_runs")}
    out = []
    for name, label, interval, _fn, _comp in TASKS:
        r = rows.get(name)
        last_end = r["last_end"] if r else None
        duration = (last_end - r["last_start"]) if (r and last_end and r["last_start"]) else None
        out.append({
            "name": name,
            "label": label,
            "interval": interval,
            "last_execution": last_end,
            "last_duration": duration,
            "next_execution": (last_end + interval) if last_end else now,
            "last_error": r["last_error"] if r else None,
        })
    return out


def task_history(limit=20):
    rows = db.conn().execute(
        "SELECT * FROM task_history ORDER BY queued_at DESC, id DESC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["label"] = _BY_NAME[d["name"]][1] if d["name"] in _BY_NAME else d["name"]
        out.append(d)
    return out


def run_task(name, trigger="manual"):
    """Runs one task inline and records it. Returns the history row."""
    t = _BY_NAME.get(name)
    if not t:
        return None
    _, label, _interval, fn, comp = t
    c = db.conn()
    now = int(time.time())
    cur = c.execute("INSERT INTO task_history(name, queued_at, started_at, trigger) "
                    "VALUES(?,?,?,?)", (name, now, now, trigger))
    hid = cur.lastrowid
    c.commit()

    err = None
    try:
        msg = fn()
        component(comp).info(msg)
    except Exception as e:                      # a failing task must not stop the rest
        err = str(e)
        component(comp).error("%s failed: %s", label, e)

    end = int(time.time())
    c.execute("UPDATE task_history SET ended_at=?, error=? WHERE id=?", (end, err, hid))
    c.execute("INSERT INTO task_runs(name,last_start,last_end,last_error) VALUES(?,?,?,?) "
              "ON CONFLICT(name) DO UPDATE SET last_start=excluded.last_start, "
              "last_end=excluded.last_end, last_error=excluded.last_error",
              (name, now, end, err))
    c.commit()
    return {"name": name, "label": label, "started_at": now, "ended_at": end, "error": err}


# ─────────────────────────────── the scheduler ───────────────────────────────

_stop = threading.Event()


def _loop():
    """One thread, waking once a minute. Not a cron library and not asyncio.

    A minute of granularity is plenty when the shortest interval is an hour, and a plain
    daemon thread costs nothing on a 512 MB box. Due-ness is computed from `last_end` in
    SQLite, so pulling the power does not lose the schedule or cause every task to fire
    at once on the next boot.
    """
    while not _stop.wait(60):
        try:
            now = int(time.time())
            for t in task_list():
                if t["next_execution"] and t["next_execution"] <= now:
                    run_task(t["name"], trigger="scheduled")
        except Exception as e:
            component("Scheduler").error("Tick failed: %s", e)


def start_scheduler():
    threading.Thread(target=_loop, name="riparr-scheduler", daemon=True).start()


def stop_scheduler():
    _stop.set()
