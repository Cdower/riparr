#!/bin/bash
# The root-side update helper, against a fake release on disk.
#
# What is asserted: a tag that does not match the strict pattern is refused before any
# network is touched; an archive whose checksum does not match its published SHA256SUMS
# is refused and the box is left as it was; an archive with a path that escapes its
# directory is refused; a good release lands in releases/<tag>, root-owned and closed
# to other writers, `current` is swapped to it, the previous release is kept for
# rollback and older ones are pruned; --rollback swaps back; and the state file the
# web page reads says what happened at every step.
#
# The network is a file:// URL (RIPARR_RELEASE_BASE), systemctl and apply-system.sh are
# stubs on PATH / in the helper directory, and the virtualenv is real but built from an
# empty requirements file so it costs seconds, not minutes.
#
# Needs root, for the ownership assertions. Run: sudo bash packaging/riparr-update.test.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)

if [ "$(id -u)" != 0 ]; then
  echo "riparr-update.test.sh needs root (it checks what root hands to the service); run it with sudo"
  exit 0
fi

T=$(mktemp -d "${TMPDIR:-/tmp}/riparr-update-test.XXXXXX") && [ -n "$T" ] || { echo "mktemp failed"; exit 1; }
trap 'rm -rf "$T"' EXIT
FAILED=0
check() {  # name, got, want
  if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', wanted '$3'"; FAILED=1; fi
}

BIN="$T/bin"; mkdir -p "$BIN"
CALLS="$T/calls"; : > "$CALLS"
printf '#!/bin/sh\necho "systemctl $*" >> %q\nexit 0\n' "$CALLS" > "$BIN/systemctl"
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"

LIB="$T/lib"; mkdir -p "$LIB"
printf '#!/bin/sh\necho "apply-system $RIPARR_INSTALL_DIR" >> %q\nexit 0\n' "$CALLS" > "$LIB/apply-system.sh"
chmod +x "$LIB/apply-system.sh"

ROOT="$T/opt/riparr"
STATUS="$T/status"
REL="$T/releases"             # the fake GitHub: releases/<tag>/<asset>
export RIPARR_INSTALL_DIR="$ROOT" RIPARR_LIB_DIR="$LIB" RIPARR_STATUS_DIR="$STATUS" \
       RIPARR_RUN_DIR="$T/run" RIPARR_RELEASE_BASE="file://$REL" RIPARR_REPO="example/riparr" \
       RIPARR_PIP_CACHE="$T/pip-cache"
mkdir -p "$T/run"

UPDATE="$HERE/riparr-update.sh"

# A release: an archive shaped like riparr-server.tar.gz (one top-level riparr/) and
# its SHA256SUMS.txt beside it. `bad` makes the published checksum wrong.
make_release() {  # tag, marker, [bad|traversal]
  local tag="$1" marker="$2" kind="${3:-}" stage="$T/stage-$1"
  rm -rf "$stage"; mkdir -p "$stage/riparr/server/riparr" "$stage/riparr/packaging" "$stage/riparr/tools"
  printf '# %s\n' "$marker" > "$stage/riparr/server/riparr/main.py"
  printf '__version__ = "%s"\n' "${tag#v}" > "$stage/riparr/server/riparr/__init__.py"
  : > "$stage/riparr/server/requirements.txt"
  # The release ships the same stub the helper directory holds, because a verified
  # release's apply-system.sh replaces root's copy before it runs.
  cp "$LIB/apply-system.sh" "$stage/riparr/packaging/apply-system.sh"
  mkdir -p "$REL/$tag"
  if [ "$kind" = traversal ]; then
    # A member that escapes: python builds it because tar refuses to.
    python3 - "$stage" "$REL/$tag/riparr-server.tar.gz" <<'PY'
import sys, tarfile, io
stage, out = sys.argv[1], sys.argv[2]
with tarfile.open(out, "w:gz") as t:
    t.add(stage + "/riparr", arcname="riparr")
    ti = tarfile.TarInfo("riparr/../../escaped"); data = b"x"; ti.size = 1
    t.addfile(ti, io.BytesIO(data))
PY
  else
    tar czf "$REL/$tag/riparr-server.tar.gz" -C "$stage" riparr
  fi
  ( cd "$REL/$tag" && sha256sum riparr-server.tar.gz > SHA256SUMS.txt )
  if [ "$kind" = bad ]; then
    sed -i 's/^./0/' "$REL/$tag/SHA256SUMS.txt"
  fi
}

state() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get(sys.argv[2],""))' "$STATUS/update.state" "$1"; }
marker() { sed -n 's/^# //p' "$ROOT/current/server/riparr/main.py"; }

echo "a tag that is not a version is refused before anything is fetched"
rm -rf "$ROOT"; mkdir -p "$ROOT"
printf 'v1.0.0; rm -rf /\n' > "$T/run/update.request"
"$UPDATE" --request "$T/run/update.request" >/dev/null 2>&1
check "exit is non-zero" "$?" "2"
check "the request file was consumed" "$([ -e "$T/run/update.request" ] || echo gone)" "gone"
check "state says error" "$(state phase)" "error"
check "nothing was unpacked" "$([ -e "$ROOT/releases" ] || echo none)" "none"
for bad in "../v1" "v1.0.0.0.0" "" "latest" "v1.0.0 "; do
  "$UPDATE" --tag "$bad" >/dev/null 2>&1
  check "tag '$bad' is refused" "$?" "2"
done

echo "a release whose checksum does not match is refused and leaves the box alone"
make_release v1.0.0 ONE bad
"$UPDATE" --tag v1.0.0 >/dev/null 2>&1
check "exit is non-zero" "$([ "$?" != 0 ] && echo nonzero)" "nonzero"
check "state says error" "$(state phase)" "error"
check "the message says checksum" "$(state message | grep -ci checksum)" "1"
check "no release directory was left behind" "$(ls "$ROOT/releases" 2>/dev/null | wc -l)" "0"
check "current does not exist" "$([ -e "$ROOT/current" ] || echo none)" "none"

echo "an archive with a path that escapes is refused"
make_release v1.0.1 ESC traversal
"$UPDATE" --tag v1.0.1 >/dev/null 2>&1
check "exit is non-zero" "$([ "$?" != 0 ] && echo nonzero)" "nonzero"
check "state says error" "$(state phase)" "error"
check "nothing escaped" "$([ -e "$ROOT/escaped" ] || [ -e "$T/opt/escaped" ] || echo clean)" "clean"
check "no release directory was left behind" "$(ls "$ROOT/releases" 2>/dev/null | wc -l)" "0"

echo "a good release is installed, root-owned, and becomes current"
make_release v1.0.0 ONE
: > "$CALLS"
"$UPDATE" --tag v1.0.0 >"$T/out" 2>&1
check "exit 0" "$?" "0"
check "state says done" "$(state phase)" "done"
check "state carries the version" "$(state version)" "1.0.0"
check "current points at the release" "$(readlink "$ROOT/current")" "releases/v1.0.0"
check "the code is there" "$(marker)" "ONE"
check "the release is root's" "$(stat -c '%u' "$ROOT/releases/v1.0.0/server/riparr/main.py")" "0"
check "nothing in it is writable by anyone else" "$(find "$ROOT/releases/v1.0.0" ! -type l -perm /022 | wc -l)" "0"
check "the install directory is root's and closed" "$(stat -c '%u %a' "$ROOT")" "0 755"
check "there is a virtualenv" "$([ -x "$ROOT/releases/v1.0.0/.venv/bin/python" ] && echo yes)" "yes"
check "the virtualenv is root's" "$(stat -c '%u' "$ROOT/releases/v1.0.0/.venv/bin/python")" "0"
check "compatibility links exist" "$(readlink "$ROOT/server") $(readlink "$ROOT/.venv")" "current/server current/.venv"
check "apply-system.sh ran against the install directory" "$(grep -c "^apply-system $ROOT" "$CALLS")" "1"
check "the service was restarted" "$(grep -c '^systemctl restart riparr.service' "$CALLS")" "1"
check "the state file is world-readable" "$(stat -c '%a' "$STATUS/update.state")" "644"
check "provenance level is recorded" "$([ -n "$(state provenance)" ] && echo yes)" "yes"

echo "the next release keeps the previous one and prunes the rest"
make_release v1.1.0 TWO
"$UPDATE" --tag v1.1.0 >/dev/null 2>&1
check "exit 0" "$?" "0"
check "current is the new one" "$(marker)" "TWO"
check "previous points at the old one" "$(readlink "$ROOT/previous")" "releases/v1.0.0"
make_release v1.2.0 THREE
"$UPDATE" --tag v1.2.0 >/dev/null 2>&1
check "current is the newest" "$(marker)" "THREE"
check "previous is the one before" "$(readlink "$ROOT/previous")" "releases/v1.1.0"
check "the oldest was pruned" "$(ls "$ROOT/releases" | tr '\n' ' ')" "v1.1.0 v1.2.0 "

echo "a release that is already installed is not re-fetched"
: > "$CALLS"
"$UPDATE" --tag v1.2.0 >/dev/null 2>&1
check "exit 0" "$?" "0"
check "state says it is already current" "$(state message | grep -c 'already')" "1"
check "the service was not restarted" "$(grep -c 'restart' "$CALLS")" "0"

echo "rollback swaps current and previous"
"$UPDATE" --rollback >/dev/null 2>&1
check "exit 0" "$?" "0"
check "current is the previous one" "$(marker)" "TWO"
check "previous is what was current" "$(readlink "$ROOT/previous")" "releases/v1.2.0"
check "state says rolled back" "$(state phase)" "done"

echo "a tree on disk installs the same way (what install.sh uses)"
mkdir -p "$T/tree/server/riparr" "$T/tree/packaging" "$T/tree/tools" "$T/tree/.venv/bin" "$T/tree/.git"
printf '# TREE\n' > "$T/tree/server/riparr/main.py"
printf '__version__ = "2.0.0"\n' > "$T/tree/server/riparr/__init__.py"
: > "$T/tree/server/requirements.txt"
printf 'junk\n' > "$T/tree/.venv/bin/python"
chown -R nobody "$T/tree"
"$UPDATE" --from-tree "$T/tree" --version 2.0.0 >/dev/null 2>&1
check "exit 0" "$?" "0"
check "current is the tree's version" "$(marker)" "TREE"
check "the tree's own venv and .git were not copied" "$([ -e "$ROOT/releases/2.0.0/.git" ] || echo no) $(stat -c '%u' "$ROOT/releases/2.0.0/.venv/bin/python")" "no 0"
check "it is root's now" "$(stat -c '%u' "$ROOT/releases/2.0.0/server/riparr/main.py")" "0"
"$UPDATE" --from-tree "$T/tree" --version 2.0.0 >/dev/null 2>&1
check "reinstalling the same version works" "$? $(marker)" "0 TREE"

echo "strict provenance refuses a release that carries no attestation"
make_release v3.0.0 FOUR
RIPARR_UPDATE_REQUIRE_PROVENANCE=1 "$UPDATE" --tag v3.0.0 >/dev/null 2>&1
check "exit is non-zero" "$([ "$?" != 0 ] && echo nonzero)" "nonzero"
check "the message says provenance" "$(state message | grep -ci provenance)" "1"
check "current was left alone" "$(marker)" "TREE"

if [ "$FAILED" = 0 ]; then echo "all ok"; else echo "FAILED"; exit 1; fi
