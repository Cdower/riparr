#!/bin/bash
# The Wi-Fi watchdog's decisions, without a radio, a database or root.
#
# netwatch.sh is sourced with RIPARR_NETWATCH_LIB=1, which defines its functions and
# starts nothing. What is asserted:
#
#   * should_watch: an explicit interface is always watched; no default route means
#     wait; a wired default route means skip (the bug: a Pi on Ethernet rebooted every
#     twelve minutes because wlan0 existed and carried nothing); a wireless one is
#     watched.
#   * decide: the ladder climbs re-associate, reload, reboot in that order, each rung
#     once per outage; a rip defers the reboot; the reboot never happens unless
#     RIPARR_NETWATCH_REBOOT=1, whatever the counters say; a threshold pushed out of
#     reach by the web page ("restart as a last resort" off) also never reboots.
#   * parse_settings: only the four known names with digit values are taken; anything
#     else from the reader's output is ignored rather than executed.
#
# Run: bash packaging/netwatch.test.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
FAILED=0
check() {  # name, got, want
  if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', wanted '$3'"; FAILED=1; fi
}

RIPARR_NETWATCH_LIB=1
# shellcheck source=packaging/netwatch.sh
. "$HERE/netwatch.sh"

echo "which interface, if any"
check "an explicit interface is watched whatever it is" "$(should_watch eth0 wlan0 1)" "watch:eth0"
check "an explicit interface is watched with no route at all" "$(should_watch wlan1 '' 0)" "watch:wlan1"
check "no default route yet means wait" "$(should_watch '' '' 0)" "wait"
check "a wired default route means skip" "$(should_watch '' eth0 0)" "skip:eth0 carries the default route and is not wireless"
check "a wireless default route is watched" "$(should_watch '' wlan0 1)" "watch:wlan0"

echo "the ladder"
#        fails assoc reload rip  A  R  B allowed
check "nothing yet" "$(decide 2 0 0 0 3 6 12 1)" "wait"
check "re-associate at the first threshold" "$(decide 3 0 0 0 3 6 12 1)" "reassociate"
check "but only once per outage" "$(decide 4 1 0 0 3 6 12 1)" "wait"
check "reload at the second" "$(decide 6 1 0 0 3 6 12 1)" "reload"
check "and only once" "$(decide 7 1 1 0 3 6 12 1)" "wait"
check "reboot at the third" "$(decide 12 1 1 0 3 6 12 1)" "reboot"
check "a rip defers the reboot" "$(decide 12 1 1 1 3 6 12 1)" "defer"
check "still deferring later" "$(decide 40 1 1 1 3 6 12 1)" "defer"
check "a long outage does not repeat the cheap rungs" "$(decide 11 1 1 0 3 6 12 1)" "wait"

echo "the reboot is opt-in"
check "not allowed: no reboot however long it has been" "$(decide 12 1 1 0 3 6 12 0)" "wait"
check "not allowed: still none at a hundred" "$(decide 100 1 1 0 3 6 12 0)" "wait"
check "not allowed: the lower rungs still run" "$(decide 3 0 0 0 3 6 12 0)" "reassociate"
check "the page's switch off (threshold out of reach) also never reboots" "$(decide 500 1 1 0 3 6 1000000000 1)" "wait"
check "the script's own default is not allowed" "$REBOOT_ALLOWED" "0"

echo "the settings reader's output is parsed, never executed"
ENABLED=1; FAIL_ASSOC=3; FAIL_RELOAD=6; FAIL_REBOOT=12
parse_settings <<'EOF'
ENABLED=0
FAIL_ASSOC=5
FAIL_RELOAD=10
FAIL_REBOOT=1000000000
EOF
check "the four values are taken" "$ENABLED $FAIL_ASSOC $FAIL_RELOAD $FAIL_REBOOT" "0 5 10 1000000000"
touch_marker="$(mktemp)"; rm -f "$touch_marker"
parse_settings <<EOF
FAIL_ASSOC=7; touch $touch_marker
PATH=/evil
FAIL_RELOAD=\$(touch $touch_marker)
IFACE=eth0
FAIL_REBOOT=
ENABLED=yes
EOF
check "a value with a command in it is ignored" "$FAIL_ASSOC" "5"
check "an unknown name is ignored" "${PATH%%:*}" "${PATH%%:*}"
check "a substitution is ignored" "$FAIL_RELOAD" "10"
check "an empty value is ignored" "$FAIL_REBOOT" "1000000000"
check "a non-numeric value is ignored" "$ENABLED" "0"
check "nothing was executed" "$([ -e "$touch_marker" ] && echo ran || echo no)" "no"

if [ "$FAILED" = 0 ]; then echo "all ok"; else echo "FAILED"; exit 1; fi
