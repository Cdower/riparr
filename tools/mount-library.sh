#!/bin/bash
# Mount every share a rip could be written to, so MakeMKV can write straight to it.
#
# Why this exists: on the reference board the SD card is the *slowest* thing in the
# pipeline. Measured, 600 MB, 2026-08-26:
#
#     write to the card         9.4 MB/s
#     write through this mount  18.0 MB/s
#     read back through it      17.8 MB/s
#
# So writing the rip straight to the NAS is not a workaround for a small card, it is
# roughly twice as fast -- and it removes the card as a size limit, which is the only
# reason a 22 GiB Blu-ray could not be ripped on a 32 GB card.
#
# Why *every* share: films and television can be sent to different machines. Mounting
# only the default one meant the second destination silently lost the fast path and
# staged every rip on the card, with nothing anywhere saying so. The default share
# keeps /srv/library -- that path is baked into installed unit files and into every box
# already running -- and each additional share is mounted at /srv/library-<id> beside
# it, matching platform.library_mount().
#
# Run by systemd as root via `ExecStartPre=+`, because mounting needs privileges the
# service account does not have and must never be given. Idempotent: an existing mount
# is left alone. Never fails the unit -- a box that will not start because a NAS is
# asleep is worse than a box that falls back to the card.
set -uo pipefail

DB="${RIPARR_DB:-/var/lib/riparr/riparr.db}"
MOUNT="${RIPARR_LIBRARY_MOUNT:-/srv/library}"
PY="${RIPARR_PYTHON:-/opt/riparr/.venv/bin/python}"

log() { echo "riparr-mount: $*"; }

[ -x "$PY" ] || PY=python3
command -v mount.cifs >/dev/null || { log "cifs-utils is not installed; skipping"; exit 0; }

# One line per share to mount: id<TAB>is_default<TAB>host<TAB>share<TAB>user<TAB>domain.
# Only shares something is actually configured to write to -- mounting a share nothing
# points at is pointless, and every mount is a socket to a NAS we would rather let
# sleep.
# Read-only, and not at all if the database is not there yet. This runs as root, and a
# plain sqlite3.connect() *creates* a missing file: on a fresh box apply-system.sh starts
# this unit before riparr.service has ever run, so the database came into existence
# empty and owned by root, and the service -- which runs as riparr -- died on its first
# write with "attempt to write a readonly database". Every fresh install since this unit
# was started at install time, on every platform.
[ -f "$DB" ] || { echo "riparr-mount: no database yet; skipping"; exit 0; }
ROWS=$("$PY" - "$DB" <<'PYEOF'
import json, sqlite3, sys

def setting(c, key, default=None):
    try:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default
    except Exception:
        return default

try:
    c = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
    c.row_factory = sqlite3.Row
    shares = {r["id"]: r for r in c.execute("SELECT * FROM shares")}
    default = c.execute(
        "SELECT id FROM shares ORDER BY is_default DESC, id LIMIT 1").fetchone()
except Exception:
    raise SystemExit(0)
if not shares or not default:
    raise SystemExit(0)
default_id = default["id"]

wanted = []
# Every destination kind in db.KINDS. Music was added after films and television, and a
# list that names them by hand is a list that silently stops mounting the next one.
for key in ("movie_share_id", "tv_share_id", "music_share_id"):
    sid = setting(c, key)
    sid = sid if sid in shares else default_id
    if sid not in wanted:
        wanted.append(sid)
if default_id not in wanted:
    wanted.append(default_id)

for sid in wanted:
    r = shares[sid]
    share = (r["path"] or "").strip("/").split("/")[0]
    user, dom = r["username"] or "", "-"
    for sep in ("\\", "/"):
        if sep in user:
            dom, _, user = user.partition(sep); break
    else:
        if "@" in user:
            user, _, dom = user.partition("@")
    if not (r["host"] and share):
        continue
    print("\t".join([str(sid), "1" if sid == default_id else "0",
                     r["host"], share, user or "-", dom or "-"]))
PYEOF
) || { log "could not read the shares"; exit 0; }

# What is mounted at a path, as mount.cifs names it: //host/share. Lower-cased, because
# SMB hosts and share names are case-insensitive and the kernel reports what it was given.
source_at() { findmnt -n -o SOURCE "$1" 2>/dev/null | head -n1 | tr '[:upper:]' '[:lower:]'; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# First, what should no longer be mounted. The default keeps $MOUNT and every other
# wanted share keeps $MOUNT-<id>, so a mount at any other $MOUNT-* is a share that was
# deleted, or that nothing points at any more, or that became the default and moved.
# Left behind it holds a socket open to a NAS nothing writes to -- and the same NAS
# mounted twice is how "already mounted" was first noticed to be the wrong question.
WANTED=$'\n'
while IFS=$'\t' read -r SID ISDEF _rest; do
  [ -n "${SID:-}" ] || continue
  if [ "$ISDEF" = "1" ]; then WANTED+="$MOUNT"$'\n'; else WANTED+="$MOUNT-$SID"$'\n'; fi
done <<< "$ROWS"
for m in "$MOUNT" "$MOUNT"-*; do
  [ -d "$m" ] && mountpoint -q "$m" || continue
  case "$WANTED" in *$'\n'"$m"$'\n'*) continue;; esac
  log "unmounting $m: nothing is configured to write there any more"
  umount -l "$m" 2>/dev/null || log "could not unmount $m"
done

[ -n "$ROWS" ] || { log "no share configured; skipping"; exit 0; }

while IFS=$'\t' read -r SID ISDEF HOST SHARE USER DOMAIN; do
  [ -n "${SID:-}" ] || continue
  if [ "$ISDEF" = "1" ]; then TARGET="$MOUNT"; else TARGET="$MOUNT-$SID"; fi
  WANT="//$HOST/$SHARE"

  # Idempotent means "the right share is there", not "something is there". The default
  # can move to another NAS, and the old one used to stay at $MOUNT because this only
  # asked whether the path was a mountpoint -- so after a change of default every
  # direct-mode rip landed on the wrong machine until the next reboot.
  if mountpoint -q "$TARGET"; then
    HAVE=$(source_at "$TARGET")
    if [ "$HAVE" = "$(lower "$WANT")" ]; then
      log "already mounted at $TARGET"; continue
    fi
    log "$TARGET holds ${HAVE:-another share}; replacing it with $WANT"
    umount -l "$TARGET" 2>/dev/null || { log "could not unmount $TARGET"; continue; }
  fi

  # The share row holds the password, so it is read here and written only to a 0600
  # file. Never on a command line: /proc/*/cmdline is world-readable.
  CRED=$(mktemp "${RIPARR_RUN_DIR:-/run}/riparr-cred.XXXXXX") || continue
  chmod 600 "$CRED"
  "$PY" - "$DB" "$SID" >> "$CRED" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True); c.row_factory = sqlite3.Row
r = c.execute("SELECT * FROM shares WHERE id=?", (int(sys.argv[2]),)).fetchone()
user = (r["username"] or "") if r else ""
for sep in ("\\", "/"):
    if sep in user:
        _, _, user = user.partition(sep); break
else:
    if "@" in user:
        user = user.partition("@")[0]
print("username=%s" % user)
print("password=%s" % ((r["password"] if r else "") or ""))
PYEOF
  [ "$DOMAIN" != "-" ] && echo "domain=$DOMAIN" >> "$CRED"

  UID_R=$(id -u riparr 2>/dev/null || echo 0)
  GID_R=$(id -g riparr 2>/dev/null || echo 0)
  mkdir -p "$TARGET"

  # `soft` so a NAS that goes away returns an error instead of wedging the rip in
  # uninterruptible sleep -- D4 says the cable gets pulled, and a hung mount is the one
  # state this appliance cannot recover from on its own.
  BASE="credentials=$CRED,uid=$UID_R,gid=$GID_R,file_mode=0664,dir_mode=0775,soft,noatime"
  DONE=0
  for VERS in 3.1.1 3.0 2.1; do
    if mount -t cifs "//$HOST/$SHARE" "$TARGET" -o "$BASE,vers=$VERS" 2>/dev/null; then
      log "mounted //$HOST/$SHARE at $TARGET (SMB $VERS)"; DONE=1; break
    fi
  done
  [ "$DONE" = "1" ] || log "could not mount //$HOST/$SHARE — rips bound for it will stage on the card instead"
  rm -f "$CRED"
done <<< "$ROWS"

exit 0
