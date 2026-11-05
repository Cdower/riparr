#!/bin/bash
# Builds and installs MakeMKV on the appliance, after tools/bootstrap.sh.
#
# makemkvcon links against libmakemkv.so.1 and libdriveio.so.0, which only exist
# in the OSS package -- so the OSS build cannot be skipped even though an official
# makemkvcon binary ships in makemkv-bin (arm64 and armhf; its Makefile picks by
# `uname -m`).
#
# What is trusted, and by whom, 2026-11-05:
#
#   * The tarballs. Their SHA-256 is pinned in packaging/makemkv-manifest.json, with a
#     note of where each hash came from. GuinpinSoft publishes no checksums of its own
#     (the forum post and the download page carry only links), so the pin is the
#     whole of the trust: a tarball from any source -- makemkv.com, a mirror, a copy
#     the Preparer left on the card, a copy the web service put in /var/lib/riparr --
#     is used only if its bytes hash to the pinned value, and it is copied into a
#     build directory that is root's before anything reads it twice.
#   * The build. configure, make and `make install` run as `riparr-build`, a system
#     account with nothing of its own, into a staging directory. Root then copies only
#     the files it expects out of that directory (packaging/staged-install.sh). The
#     makemkv-oss source is GPL and compiled here; makemkvcon itself is a closed-source
#     binary from makemkv-bin that this script installs as it came -- there is nothing
#     to compile and nothing to inspect, and the hash is the only claim made about it.
#   * The licence. MakeMKV's EULA is between the user and GuinpinSoft; this script
#     unpacks nothing until --accept-eula says they have accepted it (D14). Its free
#     beta key expires at the end of every month, and Riparr renews it from the forum
#     (see docs/guide/07-settings-reference.md).
#
# Usage:  ./makemkv-install.sh --accept-eula [--jobs N] [--srcdir DIR]
#   --srcdir   a directory that may already hold the tarballs (read, never written)
set -euo pipefail

# Run from a systemd unit there may be no HOME at all, and `set -u` turns that into a
# fatal error on this line -- before any argument is parsed, so even an explicit
# --srcdir cannot save it.
: "${HOME:=/root}"
export HOME

JOBS=2                      # a high -j risks OOM during C++ compilation
SRC="$HOME/makemkv"
ACCEPT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --accept-eula) ACCEPT=1 ;;
    --jobs) JOBS="$2"; shift ;;
    --srcdir) SRC="$2"; shift ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac; shift
done

# Consent is checked BEFORE anything is unpacked or built. MakeMKV's EULA is an
# agreement between the user and GuinpinSoft, and this script does not enter into it
# on anyone's behalf (D14).
if [ "$ACCEPT" != "1" ]; then
  cat <<EOF

MakeMKV is proprietary software made by GuinpinSoft inc, with its own licence.

  Read it:  https://www.makemkv.com/eula/
            (also at makemkv-bin-*/src/eula_en_linux.txt once unpacked)

  Then re-run with --accept-eula to confirm you accept it.

This script does not accept the licence on your behalf, and unpacks nothing until
you have.

EOF
  exit 1
fi

[ "$(id -u)" = 0 ] || { echo "makemkv-install.sh must run as root (it installs into /usr)"; exit 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Beside this script on a box (/usr/local/lib/riparr, root's copies -- see
# apply-system.sh); ../packaging in a checkout.
LIBDIR=""
for d in "$HERE" "$HERE/../packaging"; do
  [ -f "$d/staged-install.sh" ] && { LIBDIR="$d"; break; }
done
[ -n "$LIBDIR" ] || { echo "staged-install.sh not found beside $HERE"; exit 2; }
# shellcheck source=packaging/staged-install.sh
. "$LIBDIR/staged-install.sh"

MANIFEST="${RIPARR_MAKEMKV_MANIFEST:-}"
if [ -z "$MANIFEST" ]; then
  for m in "$HERE/makemkv-manifest.json" "$HERE/../packaging/makemkv-manifest.json"; do
    [ -f "$m" ] && { MANIFEST="$m"; break; }
  done
fi
MANIFEST="${MANIFEST:-$HERE/makemkv-manifest.json}"

# Where the build happens: root's, fresh every run, nowhere the service can write.
BUILD="${RIPARR_MAKEMKV_BUILD:-/var/cache/riparr/makemkv-build}"
rm -rf "$BUILD"
install -d -o root -g root -m 0755 "$(dirname "$BUILD")" "$BUILD"

# The version to build. Everything below looks for this version only: a source
# directory that once held an older MakeMKV -- the Preparer's copy from the day the
# card was written -- otherwise wins, and "upgrade" rebuilds what is already installed.
WANT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version",""))' \
       "$MANIFEST" 2>/dev/null || true)
[ -n "$WANT" ] || { echo "No version in $MANIFEST"; exit 2; }

manifest_rows() {
  # name<TAB>sha256<TAB>where<TAB>url, one line per source.
  python3 - "$MANIFEST" <<'PYEOF'
import json, sys
try:
    m = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(0)
for pkg in m.get("packages", []):
    for u in pkg.get("urls", []):
        if u.get("url"):
            print("\t".join([pkg["name"], pkg["sha256"],
                             u.get("where") or u["url"], u["url"]]))
PYEOF
}

sha_of() { sha256sum "$1" | cut -d" " -f1; }

# ── the tarballs: a copy whose hash matches, or a download whose hash matches ──
#
# This used to download into --srcdir and prefer an already-extracted directory
# there. --srcdir can be the service account's (/var/lib/riparr/makemkv), so that was
# root running a `configure` the service could have edited. Now --srcdir is only ever
# read, a copy is taken only after its hash matched, and the build happens here.
obtain_tarballs() {
  local rows names
  rows=$(manifest_rows) || return 1
  [ -n "$rows" ] || { echo "No download manifest at $MANIFEST"; return 1; }

  names=$(printf '%s\n' "$rows" | cut -f1 | awk '!seen[$0]++')
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    local want dest got
    want=$(printf '%s\n' "$rows" | awk -F'\t' -v n="$name" '$1==n{print $2; exit}')
    dest="$BUILD/$name"
    if [ -s "$SRC/$name" ]; then
      # Copied first, hashed once: the copy is what gets unpacked, and a file in a
      # directory somebody else can write may not be the same file twice.
      cp -- "$SRC/$name" "$dest"
      if [ "$(sha_of "$dest")" = "$want" ]; then
        echo "Already have $name in $SRC (checksum matches)"
        continue
      fi
      rm -f "$dest"
    fi
    got=0
    while IFS=$'\t' read -r n _ where url; do
      [ "$n" = "$name" ] || continue
      echo "Downloading $name from $where"
      # --location: Launchpad answers with a 303 to its file host.
      # --compressed: makemkv.com serves the .tar.gz with Content-Encoding: gzip on
      #   top of the gzip that is already the file, and anything replaying its
      #   response does the same. Without this the file arrives doubly compressed --
      #   valid gzip, wrong contents, failed checksum, and no hint as to why.
      if curl -fsSL --compressed --retry 2 --connect-timeout 20 --max-time 900 \
              -o "$dest.part" "$url"; then
        if [ "$(sha_of "$dest.part")" = "$want" ]; then
          mv "$dest.part" "$dest"; got=1; echo "  ok - checksum matches"; break
        fi
        echo "  $where served a file that did not match its checksum; trying the next"
      else
        echo "  $where did not answer; trying the next"
      fi
      rm -f "$dest.part"
    done <<< "$rows"
    if [ "$got" != "1" ]; then
      echo "Could not download $name from any source."
      echo "Copy it to $SRC by hand and run this again."
      return 2
    fi
  done <<< "$names"
  return 0
}

obtain_tarballs

echo "Extracting tarballs in $BUILD"
mkdir -p "$BUILD/src"
for f in "$BUILD"/makemkv-*-"$WANT".tar.gz; do
  [ -s "$f" ] || { echo "$(basename "$f") is empty -- download it again"; exit 2; }
  # Nothing in the tarball may land outside src/; a device node, a link out or an
  # absolute name is a reason to stop, not something to tidy (packaging/safe-untar.py,
  # shared with the release updater).
  python3 -I "$LIBDIR/safe-untar.py" "$f" "$BUILD/src" \
    || { echo "Could not unpack $(basename "$f") safely"; exit 2; }
done
OSS="$BUILD/src/makemkv-oss-$WANT"
BIN="$BUILD/src/makemkv-bin-$WANT"
[ -d "$OSS" ] && [ -d "$BIN" ] || {
  echo "Both makemkv-oss and makemkv-bin are required; found:"
  echo "  oss: $([ -d "$OSS" ] && echo "$OSS" || echo none)"
  echo "  bin: $([ -d "$BIN" ] && echo "$BIN" || echo none)"
  exit 2; }

VER="$WANT"

LOG="$HOME/validation"; mkdir -p "$LOG"
echo "=== MakeMKV $VER · $(uname -m) · $(nproc) cores · $(free -m | awk '/Mem:/{print $2}') MB RAM ==="

# ── build dependencies ──
# This has to stand on its own. It is reachable from the web interface, where nobody
# has run bootstrap.sh and there may be no compiler on the box at all, and the failure
# mode without this is a C++ build dying on a missing header several minutes in.
#
# `time` is the GNU one, from its own package -- Debian does not ship /usr/bin/time by
# default, and bash's `time` keyword is not a substitute for `-v`. Missing it used to
# abort the build before make ever started.
DEPS="build-essential pkg-config libssl-dev libexpat1-dev zlib1g-dev
      libavcodec-dev libavutil-dev libavformat-dev time"
MISSING=""
for pkg in $DEPS; do
  dpkg -s "$pkg" >/dev/null 2>&1 || MISSING="$MISSING $pkg"
done
if [ -n "$MISSING" ]; then
  echo "=== 0. Build tools:$MISSING ==="
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  # shellcheck disable=SC2086
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $MISSING \
    || { echo "Could not install build tools:$MISSING"; exit 2; }
fi

# Qt5 is deliberately absent: MakeMKV needs it only for the GUI, which --disable-gui
# skips. It is a large install for nothing on a headless box.

# ── the unprivileged account that does the compiling ──
ensure_build_user
STAGE="$BUILD/stage"
mkdir -p "$STAGE"
chown -R "$BUILD_USER" "$BUILD/src" "$STAGE"

echo "=== 1. Build OSS libraries (libmakemkv, libdriveio) — the slow part, as $BUILD_USER ==="
# --disable-gui avoids pulling in Qt5, which is a large and pointless install headless
as_build_user "$OSS" ./configure --disable-gui --prefix=/usr 2>&1 | tail -15

# Peak RSS here is the R1 measurement, so measure it when we can -- but never let the
# measurement be the reason the build does not happen.
TIME=""
[ -x /usr/bin/time ] && TIME="/usr/bin/time -v"
# stderr carries both the timing report and any compiler errors, which is what makes
# this file worth printing on failure.
# Spelled out rather than through as_build_user, because /usr/bin/time has to wrap the
# make itself to measure it; the environment is the one as_build_user sets.
$TIME runuser -u "$BUILD_USER" -- env HOME="$BUILD" PATH="/usr/local/bin:/usr/bin:/bin" \
  make -C "$OSS" -j"$JOBS" 2> "$LOG/makemkv-oss-build.time" || {
    echo "BUILD FAILED — last lines of $LOG/makemkv-oss-build.time:"
    tail -25 "$LOG/makemkv-oss-build.time" | sed 's/^/    /'
    grep -iE "maximum resident|Elapsed" "$LOG/makemkv-oss-build.time" || true
    echo "If this was an OOM: re-run with --jobs 1, or confirm zram is active (free -h)."
    exit 1
}
grep -E "Maximum resident set size|Elapsed \(wall" "$LOG/makemkv-oss-build.time" || true
as_build_user "$OSS" make install DESTDIR="$STAGE"

echo "=== 2. Stage the makemkvcon binary for this CPU ($(uname -m)) ==="
# The Makefile gates on tmp/eula_accepted, produced by an interactive `less` prompt
# that cannot run headlessly. --accept-eula above is the user's acceptance.
as_build_user "$BIN" mkdir -p tmp
as_build_user "$BIN" sh -c 'echo accepted > tmp/eula_accepted'
as_build_user "$BIN" make install DESTDIR="$STAGE"

echo "=== 3. Install, as root, only what MakeMKV is expected to put in place ==="
# Everything MakeMKV installs lives in these few places. A file anywhere else in the
# staging tree is refused and named; the install still completes if the files that
# matter are there, and the verify step below is what decides that.
install_staged "$STAGE" \
  'usr/bin/makemkvcon' 'usr/bin/mmccextr' 'usr/bin/mmgplsrv' 'usr/bin/makemkv' \
  'usr/lib/libmakemkv.so*' 'usr/lib/libdriveio.so*' 'usr/lib/libmmbd.so*' \
  'usr/share/MakeMKV/*' 'usr/share/applications/makemkv.desktop' \
  'usr/share/icons/*/*/apps/makemkv.png' 'usr/share/icons/*/*/apps/makemkv.svg' \
  || echo "    (some staged files were not installed; see above)"
ldconfig

echo "=== 4. Verify ==="
BIN_PATH="$(command -v makemkvcon || echo /usr/bin/makemkvcon)"
[ -x "$BIN_PATH" ] || { echo "makemkvcon was not installed"; exit 1; }
echo "    $BIN_PATH"
ldd "$BIN_PATH" | sed 's/^/    /'

# makemkvcon has no --version and no --help: it exits non-zero and prints usage, which
# under `set -e` used to abort the script *after* a completely successful build. And
# there is no cheap way to ask a running makemkvcon either — `-r info disc:99` blocks
# for 20+ seconds enumerating drives, and the version strings inside the binary belong
# to bundled libraries, not to MakeMKV.
#
# So record it here, where it is already known from the tarball, for anything that
# wants to display it without paying that cost.
install -d -m 0755 /usr/local/lib/riparr
printf '%s\n' "$VER" > /usr/local/lib/riparr/makemkv.version
chmod 0644 /usr/local/lib/riparr/makemkv.version

# Reaching this line required --accept-eula, so this file records the fact that the
# installed binary was installed under an accepted licence. The old check looked for
# ~/.MakeMKV/eula_accepted, which MakeMKV does not create — the bin package's Makefile
# gate is a different file in the build directory — so it was always false, and the
# interface kept asking for consent that had already been given.
printf 'accepted %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  > /usr/local/lib/riparr/makemkv.eula
chmod 0644 /usr/local/lib/riparr/makemkv.eula
echo "    version $VER (recorded in /usr/local/lib/riparr/makemkv.version)"
printf '%s\n' "$VER" > "$LOG/makemkv-version.txt"

# The build tree is the build user's and holds nothing worth keeping.
rm -rf "$BUILD/src" "$STAGE"

echo
echo "=== 5. Peak memory of a real operation (R1 measurement) ==="
echo "With a disc inserted, run:"
echo "    /usr/bin/time -v makemkvcon -r info disc:0 2> $LOG/makemkv-info.time"
echo "    grep 'Maximum resident' $LOG/makemkv-info.time"
echo
echo "DONE. Build artifacts logged in $LOG"
