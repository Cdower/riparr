#!/bin/bash
# The installer's decisions and the uninstaller's reach, without a board.
#
#   * decide_mode: a riparr.conf on the boot partition, or a device-tree model, means
#     appliance; anything else means host.
#   * parse_args and settle_switches: --host turns every system-wide change off,
#     --appliance turns them on, and each --no-* flag turns one off whatever the mode.
#   * set_default_kv: the operator file is created with a header, a key is replaced in
#     place, other lines survive, and a key turned back on is removed.
#   * uninstall.sh against a fake root: every file install.sh and apply-system.sh
#     write is gone afterwards, the operator's own drop-ins are left, and --purge
#     also removes the code and the data.
#
# Run: bash tools/install.test.sh   (the uninstall half needs root for userdel, and is
# skipped without it)
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/riparr-install-test.XXXXXX") && [ -n "$T" ] || { echo "mktemp failed"; exit 1; }
trap 'rm -rf "$T"' EXIT
FAILED=0
check() {  # name, got, want
  if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', wanted '$3'"; FAILED=1; fi
}

RIPARR_INSTALL_LIB=1
RIPARR_DEFAULTS_FILE="$T/etc/default/riparr"
# shellcheck source=tools/install.sh
. "$HERE/install.sh"

echo "what kind of machine"
mkdir -p "$T/boot"; : > "$T/model"
check "no riparr.conf, no model: host" "$(decide_mode "$T/boot" "$T/model")" "host"
check "no boot partition at all: host" "$(decide_mode "" "$T/model")" "host"
printf 'Raspberry Pi 4 Model B\0' > "$T/model"
check "a device-tree model: appliance" "$(decide_mode "" "$T/model")" "appliance"
: > "$T/model"; : > "$T/boot/riparr.conf"
check "a riparr.conf: appliance" "$(decide_mode "$T/boot" "$T/model")" "appliance"

echo "the switches"
reset() { MODE=""; DO_SYSCTL=""; DO_MDNS=""; DO_POWER=""; DO_NETWATCH_REBOOT=""; }
switches() { echo "$DO_SYSCTL $DO_MDNS $DO_POWER $DO_NETWATCH_REBOOT"; }
reset; MODE=appliance; settle_switches
check "appliance: everything on" "$(switches)" "yes yes yes yes"
reset; MODE=host; settle_switches
check "host: everything off" "$(switches)" "no no no no"
reset; parse_args --host; settle_switches
check "--host sets host" "$MODE $(switches)" "host no no no no"
reset; parse_args --appliance --no-sysctl; settle_switches
check "--no-sysctl on an appliance turns only that off" "$(switches)" "no yes yes yes"
reset; parse_args --appliance --no-mdns --no-power --no-netwatch-reboot; settle_switches
check "each --no-* flag is its own" "$(switches)" "yes no no no"
reset; MODE=host; parse_args --no-power; settle_switches
check "a --no-* flag on a host changes nothing" "$(switches)" "no no no no"
check "--undo is recognised" "$(parse_args --undo; echo $UNDO)" "1"
check "an unknown flag is refused" "$( (parse_args --bogus) 2>&1 | grep -c 'unknown option')" "1"

echo "the operator file"
CHANGES=()
set_default_kv RIPARR_ALLOW_POWER 0
check "created with a header and the key" "$(grep -c '^#' "$RIPARR_DEFAULTS_FILE") $(grep -c '^RIPARR_ALLOW_POWER=0$' "$RIPARR_DEFAULTS_FILE")" "3 1"
check "the change is recorded" "${#CHANGES[@]}" "1"
printf 'RIPARR_HOST=127.0.0.1\n' >> "$RIPARR_DEFAULTS_FILE"
set_default_kv RIPARR_ALLOW_POWER 0
check "setting the same value again changes nothing" "${#CHANGES[@]}" "1"
set_default_kv RIPARR_ALLOW_POWER 1
check "a new value replaces the line in place" "$(grep -c '^RIPARR_ALLOW_POWER=' "$RIPARR_DEFAULTS_FILE") $(grep -c '^RIPARR_ALLOW_POWER=1$' "$RIPARR_DEFAULTS_FILE")" "1 1"
check "the operator's other line survives" "$(grep -c '^RIPARR_HOST=127.0.0.1$' "$RIPARR_DEFAULTS_FILE")" "1"
unset_default_kv RIPARR_ALLOW_POWER
check "a key turned back on is removed" "$(grep -c '^RIPARR_ALLOW_POWER=' "$RIPARR_DEFAULTS_FILE")" "0"
check "and the rest is still there" "$(grep -c '^RIPARR_HOST=127.0.0.1$' "$RIPARR_DEFAULTS_FILE")" "1"

if [ "$(id -u)" != 0 ]; then
  echo "(skipping the uninstall half: needs root for userdel)"
else
  echo "uninstall takes everything back out"
  R="$T/root"
  mkdir -p "$R/etc/systemd/system/riparr.service.d" "$R/etc/systemd/system/aw859a-bluetooth.service.d" \
           "$R/etc/systemd/system.conf.d" "$R/etc/sysctl.d" "$R/etc/systemd/resolved.conf.d" \
           "$R/etc/avahi/services" "$R/etc/tmpfiles.d" "$R/etc/default" "$R/usr/local/lib/riparr" \
           "$R/opt/riparr/releases/1.0.0" "$R/var/lib/riparr" "$R/var/cache/riparr" "$R/run/riparr" \
           "$R/run/riparr-status" "$R/srv/staging"
  cp "$HERE/../packaging/"*.service "$HERE/../packaging/"*.path "$HERE/../packaging/"*.slice "$R/etc/systemd/system/"
  : > "$R/etc/systemd/system/riparr.service.d/override.conf"
  : > "$R/etc/systemd/system/aw859a-bluetooth.service.d/riparr-no-bluetooth.conf"
  : > "$R/etc/systemd/system.conf.d/riparr-watchdog.conf"
  : > "$R/etc/sysctl.d/60-riparr-fastfail.conf"
  : > "$R/etc/systemd/resolved.conf.d/20-riparr-avahi.conf"
  : > "$R/etc/avahi/services/riparr.service"
  : > "$R/etc/tmpfiles.d/riparr.conf"
  : > "$R/etc/default/riparr"
  : > "$R/usr/local/lib/riparr/apply-system.sh"
  : > "$R/var/lib/riparr/riparr.db"
  BIN="$T/bin"; mkdir -p "$BIN"
  for b in systemctl sysctl userdel; do printf '#!/bin/sh\nexit 0\n' > "$BIN/$b"; done
  chmod +x "$BIN"/*
  export PATH="$BIN:$PATH"
  RIPARR_SYSROOT="$R" bash "$HERE/uninstall.sh" > "$T/un.out" 2>&1
  check "exits 0" "$?" "0"
  check "the units are gone" "$(find "$R/etc/systemd/system" -maxdepth 1 -name 'riparr*' -type f | wc -l)" "0"
  check "the operator's drop-in is left" "$([ -f "$R/etc/systemd/system/riparr.service.d/override.conf" ] && echo yes)" "yes"
  check "the Bluetooth drop-in is gone" "$([ -e "$R/etc/systemd/system/aw859a-bluetooth.service.d/riparr-no-bluetooth.conf" ] || echo gone)" "gone"
  for f in etc/systemd/system.conf.d/riparr-watchdog.conf etc/sysctl.d/60-riparr-fastfail.conf \
           etc/systemd/resolved.conf.d/20-riparr-avahi.conf etc/avahi/services/riparr.service \
           etc/tmpfiles.d/riparr.conf etc/default/riparr usr/local/lib/riparr run/riparr-status run/riparr; do
    check "$f is gone" "$([ -e "$R/$f" ] || echo gone)" "gone"
  done
  check "without --purge the code stays" "$([ -d "$R/opt/riparr/releases/1.0.0" ] && echo yes)" "yes"
  check "and the data stays" "$([ -f "$R/var/lib/riparr/riparr.db" ] && echo yes)" "yes"
  removals() { grep -c $'\e\\[33m-' "$1" || true; }     # the "- removed" lines, coloured
  n=$(removals "$T/un.out")
  check "each removal is named (units, helpers, settings)" "$([ "$n" -ge 30 ] && echo many || echo "$n")" "many"
  RIPARR_SYSROOT="$R" bash "$HERE/uninstall.sh" > "$T/un2.out" 2>&1
  check "running again finds nothing to remove" "$(removals "$T/un2.out")" "0"
  RIPARR_SYSROOT="$R" bash "$HERE/uninstall.sh" --purge > "$T/un3.out" 2>&1
  check "--purge removes the code" "$([ -e "$R/opt/riparr" ] || echo gone)" "gone"
  check "--purge removes the data" "$([ -e "$R/var/lib/riparr" ] || echo gone)" "gone"
  check "--purge removes the cache and staging" "$([ -e "$R/var/cache/riparr" ] || [ -e "$R/srv/staging" ] || echo gone)" "gone"
fi

if [ "$FAILED" = 0 ]; then echo "all ok"; else echo "FAILED"; exit 1; fi
