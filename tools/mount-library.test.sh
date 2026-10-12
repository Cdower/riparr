#!/bin/bash
# What mount-library.sh does when the shares change under it.
#
# The script is idempotent, and that was the bug: "already mounted" was the answer
# whenever *anything* was at /srv/library, so moving the default share to another NAS,
# or deleting the default, left the old one mounted there and direct-mode rips kept
# landing on it. Everything that touches a mount is stubbed and recorded here; the
# script itself and the database it reads are real.
#
# Run: bash tools/mount-library.test.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/riparr-mount-test.XXXXXX")  # one form that both mktemps accept
BIN="$T/bin"; mkdir -p "$BIN"
STATE="$T/mounts"; : > "$STATE"           # one "target<TAB>source" line per mount
MOUNT="$T/srv/library"
FAILED=0

check() {  # name, got, want
  if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', wanted '$3'"; FAILED=1; fi
}

cat > "$BIN/mountpoint" <<EOF
#!/bin/sh
grep -q "^\$2	" "$STATE"
EOF
cat > "$BIN/findmnt" <<EOF
#!/bin/sh
for a in "\$@"; do t="\$a"; done
awk -F'\t' -v t="\$t" '\$1==t {print \$2}' "$STATE"
EOF
cat > "$BIN/mount" <<EOF
#!/bin/sh
# mount -t cifs //host/share target -o opts
printf '%s\t%s\n' "\$4" "\$3" >> "$STATE"
EOF
cat > "$BIN/umount" <<EOF
#!/bin/sh
for a in "\$@"; do t="\$a"; done
grep -v "^\$t	" "$STATE" > "$STATE.new"; mv "$STATE.new" "$STATE"
EOF
printf '#!/bin/sh\necho 0\n' > "$BIN/id"
printf '#!/bin/sh\nexit 0\n' > "$BIN/mount.cifs"
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"

export RIPARR_DB="$T/riparr.db" RIPARR_LIBRARY_MOUNT="$MOUNT" RIPARR_RUN_DIR="$T"
# The server's own interpreter when there is one (a checkout), else whatever python3
# is on the path (CI, which installs the server's dependencies into it).
PY="$REPO/server/.venv/bin/python"; [ -x "$PY" ] || PY=python3
export RIPARR_PYTHON="$PY"
dbpy() { ( cd "$REPO/server" && RIPARR_APPLIANCE=0 "$PY" -c "from riparr import db; $1" ); }

mounted() { sort "$STATE" | sed "s|$T||g" | tr '\t' ' ' | tr '\n' ';'; }
run() { bash "$REPO/tools/mount-library.sh" >/dev/null; }

dbpy "db.init(); db.add_share('A', 'nas-a', 'Media', None, None, make_default=True); db.add_share('B', 'nas-b', 'Media', None, None)"
A=1; B=2

echo "the default share mounts at the library path"
run
check "only the default, at the library path" "$(mounted)" "/srv/library //nas-a/Media;"
run
check "a second run leaves it alone" "$(mounted)" "/srv/library //nas-a/Media;"

echo "moving the default moves the mount"
dbpy "db.set_default_share($B)"
run
check "the new default is at the library path, the old one is gone" "$(mounted)" "/srv/library //nas-b/Media;"

echo "a share something points at mounts beside it"
dbpy "db.set('movie_share_id', $A)"
run
check "both, each at its own path" "$(mounted)" "/srv/library //nas-b/Media;/srv/library-$A //nas-a/Media;"

echo "a share nothing points at any more is unmounted"
dbpy "db.set('movie_share_id', None)"
run
check "only the default is left" "$(mounted)" "/srv/library //nas-b/Media;"

echo "deleting the default promotes the other and the mount follows"
dbpy "db.delete_share($B)"
run
check "the promoted share is at the library path" "$(mounted)" "/srv/library //nas-a/Media;"

echo "a mount that was there before the database changed is replaced, not kept"
: > "$STATE"; printf '%s\t%s\n' "$MOUNT" "//stale/Old" >> "$STATE"
run
check "the stale mount is gone" "$(mounted)" "/srv/library //nas-a/Media;"

rm -rf "$T"
echo
if [ "$FAILED" = 1 ]; then echo "FAILED"; exit 1; fi
echo "all good"
