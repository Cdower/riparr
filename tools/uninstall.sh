#!/usr/bin/env bash
# Take Riparr back out, including every system-wide change tools/install.sh made.
#
#   sudo bash tools/uninstall.sh          # units, helpers, sysctl, mDNS, operator file
#   sudo bash tools/uninstall.sh --purge  # ...and /opt/riparr, /var/lib/riparr (the
#                                         # database, settings and rip history), the
#                                         # riparr and riparr-build accounts
#
# Without --purge the code and the data stay, so running install.sh again brings the
# box back exactly as it was. Idempotent: run it twice and the second run finds
# nothing to do. Each thing removed is printed; nothing it does not recognise as
# Riparr's is touched. MakeMKV, libdvdcss and the music CD tools are left installed --
# they were asked for through the web page and are useful on their own; the lines to
# remove them are at the end.
#
# Every absolute path has an override so tools/uninstall.test.sh can run this against a
# temporary directory. The defaults are what the box uses.
set -euo pipefail

SYSROOT="${RIPARR_SYSROOT:-}"
INSTALL_DIR="${RIPARR_INSTALL_DIR:-$SYSROOT/opt/riparr}"
DATA_DIR="${RIPARR_DATA_DIR:-$SYSROOT/var/lib/riparr}"
CACHE_DIR="${RIPARR_CACHE_DIR:-$SYSROOT/var/cache/riparr}"
LIB="${RIPARR_LIB_DIR:-$SYSROOT/usr/local/lib/riparr}"
UNITS="${RIPARR_UNIT_DIR:-$SYSROOT/etc/systemd/system}"
DEFAULTS_FILE="${RIPARR_DEFAULTS_FILE:-$SYSROOT/etc/default/riparr}"
RIPARR_USER=riparr
BUILD_USER=riparr-build

PURGE=0
for a in "$@"; do
  case "$a" in
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" = 0 ] || { echo "Run this with sudo:  sudo bash tools/uninstall.sh" >&2; exit 1; }

say()     { printf '\n\033[1m%s\033[0m\n' "$*"; }
removed() { printf '  \033[33m-\033[0m %s\n' "$*"; }
kept()    { printf '  %s\n' "$*"; }

# rm, saying so, only if it is there.
gone() {   # path, description
  if [ -e "$1" ] || [ -L "$1" ]; then
    rm -rf -- "$1"
    removed "${2:-$1}"
  fi
}

say "Stopping Riparr"
# The service first, so nothing is mid-rip when its helpers go; then every door and
# watchdog. Units that are not there are not an error.
for u in riparr.service riparr-netwatch.service riparr-thermal.service riparr-powerkey.service \
         riparr-library.service; do
  systemctl disable --now "$u" >/dev/null 2>&1 || true
done
for u in "$UNITS"/riparr-*.path; do
  [ -e "$u" ] || continue
  systemctl disable --now "$(basename "$u")" >/dev/null 2>&1 || true
done
kept "every Riparr unit stopped and disabled"

say "System units and helpers"
for u in "$UNITS"/riparr.service "$UNITS"/riparr.slice "$UNITS"/riparr-*.service "$UNITS"/riparr-*.path; do
  gone "$u"
done
gone "$UNITS/aw859a-bluetooth.service.d/riparr-no-bluetooth.conf" \
     "$UNITS/aw859a-bluetooth.service.d/riparr-no-bluetooth.conf (Bluetooth back under the vendor unit)"
rmdir "$UNITS/aw859a-bluetooth.service.d" 2>/dev/null || true
for d in riparr.service.d riparr.slice.d riparr-library.service.d riparr-remount.service.d; do
  if [ -d "$UNITS/$d" ]; then
    # Drop-ins are the operator's. Say they are there; do not delete them.
    kept "left $UNITS/$d (your drop-ins)"
  fi
done
gone "$LIB" "$LIB (root-side helpers)"
gone "$SYSROOT/etc/tmpfiles.d/riparr.conf"
gone "$SYSROOT/run/riparr-status" "/run/riparr-status (helper status)"
gone "$SYSROOT/run/riparr" "/run/riparr (requests)"
gone "$SYSROOT/etc/systemd/system.conf.d/riparr-watchdog.conf" \
     "/etc/systemd/system.conf.d/riparr-watchdog.conf (hardware watchdog no longer fed by systemd)"
systemctl daemon-reload >/dev/null 2>&1 || true
systemctl daemon-reexec >/dev/null 2>&1 || true

say "System-wide settings"
if [ -e "$SYSROOT/etc/sysctl.d/60-riparr-fastfail.conf" ]; then
  gone "$SYSROOT/etc/sysctl.d/60-riparr-fastfail.conf" \
       "/etc/sysctl.d/60-riparr-fastfail.conf (net.ipv4.tcp_syn_retries back to the kernel default, 6)"
  sysctl -q -w net.ipv4.tcp_syn_retries=6 >/dev/null 2>&1 || true
fi
if [ -e "$SYSROOT/etc/systemd/resolved.conf.d/20-riparr-avahi.conf" ]; then
  gone "$SYSROOT/etc/systemd/resolved.conf.d/20-riparr-avahi.conf" \
       "/etc/systemd/resolved.conf.d/20-riparr-avahi.conf (systemd-resolved's mDNS responder back to its default)"
  systemctl restart systemd-resolved >/dev/null 2>&1 || true
fi
if [ -e "$SYSROOT/etc/avahi/services/riparr.service" ]; then
  gone "$SYSROOT/etc/avahi/services/riparr.service" "/etc/avahi/services/riparr.service (no longer advertised over Bonjour)"
  systemctl reload avahi-daemon >/dev/null 2>&1 || true
fi
gone "$DEFAULTS_FILE" "$DEFAULTS_FILE (operator configuration)"
kept "avahi-daemon itself is left installed: other things may use it"

if [ "$PURGE" = 1 ]; then
  say "Code, data and accounts (--purge)"
  gone "$INSTALL_DIR" "$INSTALL_DIR (every release and its virtualenv)"
  gone "$DATA_DIR" "$DATA_DIR (database, settings, rip history, MakeMKV key)"
  gone "$CACHE_DIR" "$CACHE_DIR (pip and build caches)"
  for acct in "$RIPARR_USER" "$BUILD_USER"; do
    if id -u "$acct" >/dev/null 2>&1; then
      userdel "$acct" >/dev/null 2>&1 && removed "account $acct" || kept "could not remove account $acct"
    fi
  done
  gone "$SYSROOT/srv/staging" "/srv/staging"
else
  say "Kept"
  kept "$INSTALL_DIR and $DATA_DIR, and the riparr and riparr-build accounts: run again with --purge to remove them"
fi

say "Not touched"
kept "MakeMKV (/usr/bin/makemkvcon, /usr/lib/libmakemkv*, /usr/lib/libdriveio*, /usr/lib/libmmbd*, /usr/share/MakeMKV)"
kept "libdvdcss (/usr/local/lib/libdvdcss.so*) and the apt packages the page installed (dvdbackup, cd-paranoia, flac, ...)"
kept "the kernel's hardware watchdog setting is back to its default; a frozen box now stays frozen"
echo
echo "Done."
