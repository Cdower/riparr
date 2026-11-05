#!/bin/bash
# What root copies out of a build user's staging directory, and what it refuses.
#
# Needs root: the point is what lands in the real filesystem with root's ownership.
# Everything is installed under a throwaway prefix by running the function in a
# chroot-free way: the staging tree's paths are relative, so the test uses a staging
# tree whose relative paths all begin with a temporary directory's own path.
#
# Run: sudo bash packaging/staged-install.test.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
if [ "$(id -u)" != 0 ]; then
  echo "staged-install.test.sh needs root; run it with sudo"
  exit 0
fi
T=$(mktemp -d "${TMPDIR:-/tmp}/riparr-staged-test.XXXXXX") && [ -n "$T" ] || { echo "mktemp failed"; exit 1; }
trap 'rm -rf "$T"' EXIT
FAILED=0
check() {  # name, got, want
  if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', wanted '$3'"; FAILED=1; fi
}

# shellcheck source=packaging/staged-install.sh
. "$HERE/staged-install.sh"

# "/" + rel must land inside $T, so the staging tree mirrors $T's own path.
OUT="$T/out"; STAGE="$T/stage"
S="$STAGE$OUT"
mkdir -p "$S/usr/bin" "$S/usr/lib" "$S/usr/share/MakeMKV" "$S/etc/cron.d" "$S/usr/lib/evil"
printf '#!/bin/sh\necho makemkvcon\n' > "$S/usr/bin/makemkvcon"; chmod 755 "$S/usr/bin/makemkvcon"
printf 'lib\n' > "$S/usr/lib/libmakemkv.so.1.0.0"
ln -s libmakemkv.so.1.0.0 "$S/usr/lib/libmakemkv.so.1"
ln -s libmakemkv.so.1 "$S/usr/lib/libmakemkv.so"
printf 'data\n' > "$S/usr/share/MakeMKV/appdata.tar"
printf '* * * * * root touch /pwned\n' > "$S/etc/cron.d/evil"              # not on the list
printf 'x\n' > "$S/usr/lib/evil/libmakemkv.so.9"                          # wrong directory
ln -s /etc/shadow "$S/usr/lib/libdriveio.so.0"                            # a link elsewhere
printf 'x\n' > "$S/usr/bin/sudo-me"; chmod 4755 "$S/usr/bin/sudo-me"     # not on the list
chown -R nobody "$STAGE"

install_staged "$STAGE" \
  "${OUT#/}/usr/bin/makemkvcon" "${OUT#/}/usr/lib/libmakemkv.so*" "${OUT#/}/usr/lib/libdriveio.so*" \
  "${OUT#/}/usr/share/MakeMKV/*" > "$T/log" 2>&1
rc=$?
check "something was refused, so the status says so" "$rc" "1"
check "the binary is installed root 0755" "$(stat -c '%u %a' "$OUT/usr/bin/makemkvcon")" "0 755"
check "the library is installed root 0644" "$(stat -c '%u %a' "$OUT/usr/lib/libmakemkv.so.1.0.0")" "0 644"
check "its soname link is recreated" "$(readlink "$OUT/usr/lib/libmakemkv.so.1")" "libmakemkv.so.1.0.0"
check "and the dev link" "$(readlink "$OUT/usr/lib/libmakemkv.so")" "libmakemkv.so.1"
check "the data file is installed" "$(cat "$OUT/usr/share/MakeMKV/appdata.tar")" "data"
check "the cron file is not" "$([ -e "$OUT/etc/cron.d/evil" ] && echo yes || echo no)" "no"
check "the library in the wrong directory is not" "$([ -e "$OUT/usr/lib/evil/libmakemkv.so.9" ] && echo yes || echo no)" "no"
check "the link to /etc/shadow is not" "$([ -e "$OUT/usr/lib/libdriveio.so.0" ] || [ -L "$OUT/usr/lib/libdriveio.so.0" ] && echo yes || echo no)" "no"
check "the setuid file is not" "$([ -e "$OUT/usr/bin/sudo-me" ] && echo yes || echo no)" "no"
check "each refusal is named" "$(grep -c 'not installing' "$T/log")" "4"
check "nothing kept the build user's ownership" "$(find "$OUT" ! -user root | wc -l)" "0"

echo "a path that escapes refuses the whole install"
rm -rf "$OUT" "$STAGE"; mkdir -p "$S/usr/bin"
printf 'x\n' > "$S/usr/bin/makemkvcon"
mkdir -p "$STAGE/..evil"; printf 'x\n' > "$STAGE/..evil/f"
install_staged "$STAGE" "${OUT#/}/usr/bin/makemkvcon" > "$T/log" 2>&1
check "returns failure" "$?" "1"
check "and installed nothing" "$([ -e "$OUT/usr/bin/makemkvcon" ] && echo yes || echo no)" "no"

if [ "$FAILED" = 0 ]; then echo "all ok"; else echo "FAILED"; exit 1; fi
