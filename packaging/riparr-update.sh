#!/usr/bin/env bash
# Root half of a Riparr update. Started by riparr-update.service, which fires when the
# web service writes a release tag to /run/riparr/update.request.
#
# Why this exists, 2026-11-05:
#
# The in-app updater used to run as the service account and replace /opt/riparr
# itself, then touch provision.request so a root unit would install the units and
# helpers from the tree it had just written. The checksum check was real, but it was
# the *service* doing it -- so a compromised service could skip it, write whatever it
# liked into packaging/, and have root install that. Root was executing content the
# service account could write, which is the one thing the one-way doors exist to
# prevent.
#
# Now the service's whole say in an update is one line in one file: a release tag.
# Everything else happens here, as root, against a root-owned tree:
#
#   1. the tag is checked against a strict pattern (a version, nothing else)
#   2. the archive and its SHA256SUMS.txt are fetched from the release
#   3. the archive's SHA-256 is checked against the published one
#   4. its build provenance is checked, as far as this box can (see verify_provenance)
#   5. it is unpacked into /opt/riparr/releases/<tag>, refused if any path escapes
#   6. a virtualenv is built inside it with `pip install --require-hashes`
#   7. only then is /opt/riparr/current pointed at it, atomically
#   8. apply-system.sh installs the units and helpers from the now-verified tree
#   9. the service is restarted
#
# Any failure before step 7 leaves the running release untouched and removes the
# partial one. The previous release is kept, so `riparr-update.sh --rollback` is a
# symlink swap.
#
# THIS FILE MUST LIVE OUTSIDE anything the service can write. apply-system.sh installs
# it in /usr/local/lib/riparr as root:root 0755. The service reads progress back from
# a root-owned status directory, never from its own.
#
# Usage (as root):
#   riparr-update.sh --request /run/riparr/update.request   # what the door runs
#   riparr-update.sh --tag v1.2.3                           # the same, by hand
#   riparr-update.sh --from-tree DIR --version V            # what install.sh uses
#   riparr-update.sh --rollback                             # back to the previous one
# Add --no-restart to leave the service alone afterwards.
set -uo pipefail

ROOT="${RIPARR_INSTALL_DIR:-/opt/riparr}"
LIB="${RIPARR_LIB_DIR:-/usr/local/lib/riparr}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=packaging/status-lib.sh
. "$HERE/status-lib.sh"
STATE="$STATUS_DIR/update.state"
REPO="${RIPARR_REPO:-jackharvest/riparr}"
BASE="${RIPARR_RELEASE_BASE:-https://github.com/$REPO/releases/download}"
ASSET=riparr-server.tar.gz
SUMS=SHA256SUMS.txt
BUNDLE="$ASSET.sigstore.json"
# auto: gh, then sigstore-python, then a structural check. Or name one, for tests.
VERIFIER="${RIPARR_UPDATE_VERIFIER:-auto}"
REQUIRE_PROVENANCE="${RIPARR_UPDATE_REQUIRE_PROVENANCE:-0}"
export PIP_CACHE_DIR="${RIPARR_PIP_CACHE:-/var/cache/riparr/pip}"

[ "$(id -u)" = 0 ] || { echo "riparr-update.sh must run as root" >&2; exit 1; }
if ! printf '%s' "$REPO" | grep -Eq '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'; then
  echo "RIPARR_REPO must be owner/name, got '$REPO'" >&2; exit 2
fi

MODE="" TAG="" TREE="" VERSION="" RESTART=1 REQUEST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --request)   MODE=tag; REQUEST="$2"; shift ;;
    --tag)       MODE=tag; TAG="$2"; shift ;;
    --from-tree) MODE=tree; TREE="$2"; shift ;;
    --version)   VERSION="$2"; shift ;;
    --rollback)  MODE=rollback ;;
    --no-restart) RESTART=0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$MODE" ] || { echo "nothing to do: --request, --tag, --from-tree or --rollback" >&2; exit 2; }

# ── progress, for the web page ──
# JSON in a root-owned directory, world-readable: it carries progress, not secrets.
PROVENANCE="" VERSION_OUT=""
say() {   # phase, progress (0..1), message, [detail]
  python3 - "$1" "$2" "$3" "${4:-}" "$VERSION_OUT" "$PROVENANCE" "$STATE" <<'PY'
import json, os, sys, time
phase, progress, message, detail, version, prov, path = sys.argv[1:8]
d = {"phase": phase, "progress": float(progress), "message": message, "detail": detail,
     "version": version, "provenance": prov, "at": int(time.time())}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(d, f)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
  echo "riparr-update: $3${4:+ -- $4}"
}

WORK=""
cleanup() { [ -n "$WORK" ] && rm -rf "$WORK"; }
fail() {   # message, [detail], [exit code]
  say error 0 "$1" "${2:-}"
  cleanup
  exit "${3:-1}"
}

# ── the request: one line, one tag, nothing else trusted ──
if [ -n "$REQUEST" ]; then
  # Consumed first, whatever it says, so a bad request cannot loop the path unit.
  TAG=$(head -c 64 "$REQUEST" 2>/dev/null | head -n 1 | tr -d '\r')
  rm -f "$REQUEST"
fi

valid_tag() { printf '%s' "$1" | grep -Eq '^v?[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$'; }
valid_version() { case "$1" in v*) return 1 ;; esac; valid_tag "$1"; }
tree_version() { sed -n 's/^__version__ = "\(.*\)"/\1/p' "$1/server/riparr/__init__.py" 2>/dev/null || true; }

if [ "$MODE" = tag ]; then
  valid_tag "$TAG" || fail "The update request did not name a release version, so nothing was done." "got: '$TAG'" 2
  VERSION_OUT="${TAG#v}"
elif [ "$MODE" = tree ]; then
  [ -d "$TREE" ] || fail "No tree at $TREE" "" 2
  valid_version "$VERSION" || fail "--version must be a version, got '$VERSION'" "" 2
  TAG="$VERSION"
  VERSION_OUT="$VERSION"
fi

# One update at a time. A second request while one is running waits its turn, and by
# then is usually a no-op ("already current").
install -d -m 0755 "$ROOT" "$ROOT/releases"
chown root:root "$ROOT" "$ROOT/releases"
exec 9> "$ROOT/releases/.lock"
flock 9

current_target() { readlink "$ROOT/current" 2>/dev/null || true; }

# ── the parts common to every way in ──

# Point current at a release, keeping the one it was on as previous. Both links are
# replaced with rename(2), so a reader never sees a missing current. The
# compatibility links let every documented path keep working.
flip_to() {   # releases/<name>
  local new="$1" old
  old=$(current_target)
  ln -sfn "$new" "$ROOT/current.new"
  mv -T "$ROOT/current.new" "$ROOT/current"
  if [ -n "$old" ] && [ "$old" != "$new" ]; then
    ln -sfn "$old" "$ROOT/previous.new"
    mv -T "$ROOT/previous.new" "$ROOT/previous"
  fi
  local l
  for l in server packaging tools .venv; do
    ln -sfn "current/$l" "$ROOT/$l.new"
    mv -T "$ROOT/$l.new" "$ROOT/$l"
  done
}

# Everything under releases/ that is neither current nor previous, and any work
# directory an earlier run left behind.
prune() {
  local keep_c keep_p d
  keep_c=$(current_target); keep_p=$(readlink "$ROOT/previous" 2>/dev/null || true)
  for d in "$ROOT"/releases/* "$ROOT"/releases/.work.*; do
    [ -e "$d" ] || continue
    case "releases/$(basename "$d")" in
      "$keep_c"|"$keep_p") continue ;;
    esac
    rm -rf "$d"
  done
}

# The tree is root's and nobody else can write a byte of it. Files keep their execute
# bits; group and other lose write.
own() {
  chown -R root:root "$1"
  chmod -R go-w "$1"
  chmod 0755 "$1"
}

build_venv() {   # release dir
  local rel="$1" reqs="$1/server/requirements.txt"
  say installing 0.6 "Installing what this version needs" "pip, with every package checked against its pinned hash"
  if [ ! -x "$rel/.venv/bin/python" ]; then
    rm -rf "$rel/.venv"
    python3 -m venv "$rel/.venv" || return 1
  fi
  install -d -m 0755 "$PIP_CACHE_DIR"
  # --require-hashes: a requirements file without a hash for every package is refused,
  # and a download whose hash differs is refused. This is the line that makes the
  # lock file the thing the box runs, rather than whatever PyPI served today.
  "$rel/.venv/bin/pip" install --quiet --disable-pip-version-check \
      --require-hashes -r "$reqs" || return 1
  own "$rel/.venv"
}

# The tree is root's by now (own), so its own apply-system.sh is run: its first act
# is to refuse anything in the tree that is not root's, and its last is to refresh
# root's copy in $LIB. One trust predicate, in one place. Root's copy is the fallback
# for a tree that ships none (a test tree, a very old release).
apply_system() {
  local script="$ROOT/current/packaging/apply-system.sh"
  say applying 0.9 "Installing system units and helpers from the new version"
  [ -f "$script" ] && [ ! -L "$script" ] || script="$LIB/apply-system.sh"
  if [ -x "$script" ]; then
    RIPARR_INSTALL_DIR="$ROOT" "$script" || return 1
  fi
}

# A verified, root-owned tree becomes releases/<name>: the directory of the same
# name, if there is one (reinstalling the running version), is moved aside rather
# than deleted under the service, removed once current has moved on.
place_release() {   # tree, name
  local tree="$1" dest="$ROOT/releases/$2"
  if [ -d "$dest" ]; then
    mv -T "$dest" "$dest.old"
  fi
  mv -T "$tree" "$dest"
  flip_to "releases/$2"
  rm -rf "$dest.old"
}

restart_service() {
  [ "$RESTART" = 1 ] || return 0
  # A moment's grace so the service can read the "done" state and finish answering
  # the request that asked for this, before the process answering it is replaced.
  sleep 2
  systemctl restart riparr.service || true
}

finish() {   # message
  prune
  apply_system || fail "The new version is in place but its system parts could not be installed. See the journal for riparr-update."
  say "done" 1 "$1" ""
  restart_service
  cleanup
  exit 0
}

# ── rollback ──
if [ "$MODE" = rollback ]; then
  prev=$(readlink "$ROOT/previous" 2>/dev/null || true)
  [ -n "$prev" ] && [ -d "$ROOT/$prev" ] || fail "There is no previous version to go back to." "" 2
  VERSION_OUT=$(tree_version "$ROOT/$prev")
  say swapping 0.8 "Going back to the previous version" "$prev"
  flip_to "$prev"
  finish "Rolled back to ${VERSION_OUT:-the previous version}."
fi

# ── a tree already on disk (install.sh from a checkout or an unpacked, verified archive) ──
if [ "$MODE" = tree ]; then
  [ -f "$TREE/server/riparr/main.py" ] || fail "$TREE does not look like Riparr (no server/riparr/main.py)." "" 2
  PROVENANCE="local-tree"
  WORK=$(mktemp -d "$ROOT/releases/.work.XXXXXX")
  say copying 0.3 "Copying Riparr $VERSION into place" "$TREE"
  # --exclude .venv: a working tree copied off a Mac carries a virtualenv full of
  # x86/arm64-Darwin binaries that would sit in /opt doing nothing but confusing
  # people -- and a venv built elsewhere is not one this box can trust anyway.
  mkdir -p "$WORK/tree"
  tar -cf - -C "$TREE" --exclude=./.venv --exclude=./.git --exclude=__pycache__ --exclude='*.pyc' . \
    | tar -xf - -C "$WORK/tree" || fail "Could not copy $TREE"
  own "$WORK/tree"
  build_venv "$WORK/tree" || fail "Dependencies failed to install. Nothing was changed."
  place_release "$WORK/tree" "$VERSION"
  rm -rf "$WORK"; WORK=""
  finish "Installed Riparr $VERSION."
fi

# ── a release tag: fetch, verify, unpack ──
rel_dir="$ROOT/releases/$TAG"
if [ "$(current_target)" = "releases/$TAG" ] && [ -x "$rel_dir/.venv/bin/python" ]; then
  say "done" 1 "Riparr $VERSION_OUT is already the running version." ""
  exit 0
fi

WORK=$(mktemp -d "$ROOT/releases/.work.XXXXXX")
say downloading 0.1 "Downloading Riparr $VERSION_OUT" "$BASE/$TAG/$ASSET"
fetch() {   # url, dest
  curl -fsSL --retry 3 --connect-timeout 20 --max-time 900 -o "$2" "$1"
}
sha_of() { sha256sum "$1" | cut -d' ' -f1; }
fetch "$BASE/$TAG/$ASSET" "$WORK/$ASSET" \
  || fail "Could not download the release archive." "$BASE/$TAG/$ASSET"
fetch "$BASE/$TAG/$SUMS" "$WORK/$SUMS" \
  || fail "That release publishes no checksum for the appliance archive, so the download cannot be verified. Nothing was changed." "$BASE/$TAG/$SUMS"

say verifying 0.3 "Checking the download against its published checksum"
expected=$(awk -v n="$ASSET" '{ f=$2; sub(/^\*/, "", f); if (f == n) { print $1; exit } }' "$WORK/$SUMS")
actual=$(sha_of "$WORK/$ASSET")
[ -n "$expected" ] || fail "The release's checksum file does not list the appliance archive. Nothing was changed."
[ "$expected" = "$actual" ] || fail "The download did not match its published checksum. Nothing was changed." "expected ${expected:0:16}, got ${actual:0:16}"

# ── provenance ──
# Integrity (the checksum above) says the bytes are the ones the release published.
# Provenance says the release was built by this repository's own workflow from a
# commit in it, which is what stops a leaked token or a hijacked account publishing
# a release the checksum would happily bless. The attestation is a Sigstore bundle
# published beside the archive. What this box can do with it depends on what it has:
#
#   verified    `gh attestation verify` or sigstore-python checked the signature and
#               the certificate chain against Sigstore's trust root. The claim is real.
#   bound       Neither is installed (a stock appliance has neither). The bundle was
#               parsed and names exactly this archive's SHA-256 as the subject of a
#               SLSA provenance statement from this repository -- but the signature
#               was not checked, so this proves the publisher attached an attestation
#               for these bytes, not who signed it.
#   none        The release carries no bundle (every release before 0.8).
#
# A bundle that is present but fails to verify is a refusal, never a downgrade.
# RIPARR_UPDATE_REQUIRE_PROVENANCE=1 refuses anything less than `verified`. Off by
# default because a stock appliance cannot reach `verified`; on for any box where
# gh or sigstore-python is installed and the operator wants the guarantee.
verify_provenance() {
  local archive="$1" bundle="$WORK/$BUNDLE"
  if ! fetch "$BASE/$TAG/$BUNDLE" "$bundle"; then
    PROVENANCE=none
    return 0
  fi
  local how="$VERIFIER"
  if [ "$how" = auto ]; then
    if command -v gh >/dev/null 2>&1; then how=gh
    elif python3 -c 'import sigstore' 2>/dev/null; then how=sigstore
    else how=structural; fi
  fi
  case "$how" in
    gh)
      gh attestation verify "$archive" --repo "$REPO" --bundle "$bundle" >"$WORK/verify.log" 2>&1 \
        || fail "The release's build attestation did not verify, so it was not installed." "$(tail -n 5 "$WORK/verify.log")"
      PROVENANCE=verified ;;
    sigstore)
      python3 -m sigstore verify github --bundle "$bundle" --repository "$REPO" "$archive" >"$WORK/verify.log" 2>&1 \
        || fail "The release's build attestation did not verify, so it was not installed." "$(tail -n 5 "$WORK/verify.log")"
      PROVENANCE=verified ;;
    structural)
      python3 - "$bundle" "$actual" "$REPO" <<'PY' >"$WORK/verify.log" 2>&1 \
        || fail "The release's build attestation does not describe this archive, so it was not installed." "$(tail -n 5 "$WORK/verify.log")"
import base64, json, sys
bundle, digest, repo = sys.argv[1:4]
b = json.load(open(bundle))
env = b.get("dsseEnvelope") or {}
payload = json.loads(base64.b64decode(env.get("payload", "")))
assert payload.get("_type") == "https://in-toto.io/Statement/v1", "not an in-toto statement"
assert "slsa.dev/provenance" in (payload.get("predicateType") or ""), "not SLSA provenance"
subjects = [s for s in payload.get("subject", []) if (s.get("digest") or {}).get("sha256") == digest]
assert subjects, "archive digest %s is not a subject of the attestation" % digest[:16]
pred = payload.get("predicate") or {}
blob = json.dumps(pred)
assert ("github.com/%s" % repo) in blob, "attestation does not name %s" % repo
print("bound: subject %s, repository %s" % (digest[:16], repo))
PY
      PROVENANCE=bound ;;
    none)
      PROVENANCE=none ;;
    *) fail "Unknown verifier '$how'" "" 2 ;;
  esac
}
verify_provenance "$WORK/$ASSET"
if [ "$REQUIRE_PROVENANCE" = 1 ] && [ "$PROVENANCE" != verified ]; then
  fail "This box requires verified build provenance and this release's could not be verified here (level: $PROVENANCE). Nothing was changed." \
       "Install gh or sigstore-python, or unset RIPARR_UPDATE_REQUIRE_PROVENANCE."
fi
say verifying 0.4 "Checksum matches; provenance: $PROVENANCE"

# ── unpack ──
# Refuse anything that would land outside the directory: absolute names, `..`, links
# that point out, device nodes (packaging/safe-untar.py, shared with the MakeMKV
# installer).
say unpacking 0.5 "Unpacking Riparr $VERSION_OUT"
mkdir -p "$WORK/unpack"
python3 -I "$HERE/safe-untar.py" "$WORK/$ASSET" "$WORK/unpack" >"$WORK/unpack.log" 2>&1 \
  || fail "The archive could not be unpacked safely, so it was not installed." "$(tail -n 5 "$WORK/unpack.log")"
tree="$WORK/unpack/riparr"
[ -f "$tree/server/riparr/main.py" ] \
  || fail "That archive does not look like Riparr, so it was not installed. Nothing was changed."
got_version=$(tree_version "$tree")
[ -n "$got_version" ] && VERSION_OUT="$got_version"
own "$tree"
build_venv "$tree" || fail "The new version could not install what it needs, so it was not installed. Nothing was changed."

# ── swap ──
say swapping 0.8 "Switching to Riparr $VERSION_OUT"
place_release "$tree" "$TAG"
finish "Updated to $VERSION_OUT. Riparr is restarting."
