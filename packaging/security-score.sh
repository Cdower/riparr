#!/bin/bash
# The exposure of every shipped systemd service, as `systemd-analyze security` scores
# it, against a ceiling per unit in packaging/security-thresholds.txt.
#
# A unit's sandbox is a list of directives, and a directive deleted by accident reads
# as nothing at all -- the unit still starts, the service still works, and the hole is
# invisible until somebody looks. The score is the one number that moves when that
# happens. CI fails if any unit scores above its ceiling; the ceiling moves down when
# the sandbox tightens and never up without a reason written next to it.
#
# Runs offline against a scratch root, so it needs no running systemd and no
# privileges -- just systemd-analyze from the systemd package.
#
# Usage: packaging/security-score.sh [--record]
#   --record  print the current scores in threshold-file form (to update the file)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
THRESHOLDS="$HERE/security-thresholds.txt"

command -v systemd-analyze >/dev/null || { echo "systemd-analyze is not installed"; exit 2; }

T=$(mktemp -d "${TMPDIR:-/tmp}/riparr-score.XXXXXX")
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/etc/systemd/system"
cp "$HERE"/*.service "$HERE"/*.path "$HERE"/*.slice "$T/etc/systemd/system/" 2>/dev/null || true

score_of() {   # unit -> "N.N"
  systemd-analyze security --offline=true --root="$T" "$1" 2>/dev/null \
    | sed -n 's/.*Overall exposure level for [^:]*: \([0-9.]*\).*/\1/p' | head -1
}

record=0
[ "${1:-}" = "--record" ] && record=1
fail=0
for f in "$HERE"/*.service; do
  u=$(basename "$f")
  s=$(score_of "$u")
  [ -n "$s" ] || { echo "could not score $u"; fail=1; continue; }
  if [ "$record" = 1 ]; then
    printf '%-28s %s\n' "$u" "$s"
    continue
  fi
  max=$(awk -v u="$u" '$1 == u { print $2 }' "$THRESHOLDS")
  if [ -z "$max" ]; then
    echo "FAIL  $u scores $s and has no ceiling in $(basename "$THRESHOLDS")"
    fail=1
  elif awk -v s="$s" -v m="$max" 'BEGIN { exit !(s > m) }'; then
    echo "FAIL  $u scores $s, over its ceiling of $max"
    fail=1
  else
    echo "ok    $u $s (ceiling $max)"
  fi
done
exit $fail
