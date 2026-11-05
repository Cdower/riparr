#!/usr/bin/env bash
# Install or refresh the parts of Riparr that live outside /opt/riparr.
#
# Why this is its own script, 2026-08-29:
#
# There were two ways to get a new version onto a box and only one of them applied
# system changes. `tools/install.sh` copies the systemd units and helper scripts into
# place; the in-app updater swapped /opt/riparr, pip-installed and restarted the
# service -- it never touched /etc/systemd/system or /usr/local/lib. So a release that
# added a unit shipped the file and installed nothing, on every box that updated from
# the web page. The unit sat in /opt/riparr/packaging looking installed.
#
# That is the same shape as every bug in JOURNAL.md worth remembering: something that
# reads as implemented and does nothing. The fix is one definition with two callers --
# install.sh runs it, and the update door (riparr-update.service) runs it after a
# release has been verified and unpacked.
#
# What it trusts, 2026-11-05:
#
# Until this version /opt/riparr was owned by the service account, because the in-app
# updater ran as that account and replaced the tree. This script then installed units
# and root-side helpers *from that tree*, as root. A compromised service could edit
# packaging/riparr-reboot.service, touch /run/riparr/provision.request, and have root
# run anything. The one-way doors were sound; what they opened onto was not.
#
# So now: /opt/riparr is root's. Releases are unpacked by a root helper into
# /opt/riparr/releases/<version> after their checksum and provenance are checked, and
# `current` is a root-owned symlink. The service account can read the code it runs and
# cannot write a byte of it. This script refuses to install anything that is not a
# regular file owned by root with no group or other write bit, in a directory that is
# the same -- by name, loudly, and with a non-zero exit so the unit that ran it records
# the refusal. See docs/security.md.
#
# Must be idempotent: it runs on every install and after every update.
#
# Every absolute path has an override so packaging/apply-system.test.sh can run this
# against a temporary directory. The defaults are what the box uses.

set -euo pipefail

INSTALL_DIR="${RIPARR_INSTALL_DIR:-/opt/riparr}"
LIB="${RIPARR_LIB_DIR:-/usr/local/lib/riparr}"
SYSROOT="${RIPARR_SYSROOT:-}"
UNITS="${RIPARR_UNIT_DIR:-$SYSROOT/etc/systemd/system}"
RUN="${RIPARR_RUN_DIR:-/run/riparr}"
STATUS="${RIPARR_STATUS_DIR:-/run/riparr-status}"

[ "$(id -u)" = 0 ] || { echo "apply-system.sh must run as root" >&2; exit 1; }

say() { printf '  %s\n' "$*"; }
refuse() { printf '  REFUSED %s\n' "$*" >&2; refused=1; }
refused=0

# ── the release layout ──
# /opt/riparr/releases/<version>/   a verified release, root-owned, read-only to others
# /opt/riparr/current               -> releases/<version>, swapped atomically
# /opt/riparr/server, packaging, tools, .venv -> current/..., so every path that was
#                                   ever documented or baked into a unit still works
#
# A box that updated from the web page before this version arrives here with the old
# layout: a real tree owned by the service account. It is adopted once -- moved into
# releases/<its version>, given to root, and never writable by the service again. That
# adopts the code the box is already running, which is no more than the old door
# already trusted; the next update through riparr-update.sh is the first one whose
# every byte was checked before root touched it. Said out loud, because it is the one
# moment this script makes root's something the service wrote.
adopt_legacy_layout() {
    [ -d "$INSTALL_DIR/server" ] && [ ! -L "$INSTALL_DIR/server" ] || return 0
    local v
    v=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$INSTALL_DIR/server/riparr/__init__.py" 2>/dev/null || true)
    v="${v:-adopted}"
    say "adopting the existing /opt/riparr tree as release $v (one-time layout change)"
    local dest="$INSTALL_DIR/releases/$v"
    rm -rf "$dest" "$INSTALL_DIR/.previous"
    mkdir -p "$INSTALL_DIR/releases"
    install -d -m 0755 "$dest"
    local name
    for name in "$INSTALL_DIR"/* "$INSTALL_DIR"/.[!.]*; do
        [ -e "$name" ] || [ -L "$name" ] || continue
        case "$(basename "$name")" in releases|current) continue ;; esac
        mv "$name" "$dest/"
    done
    chown -R root:root "$INSTALL_DIR"
    chmod -R go-w "$INSTALL_DIR"
    chmod 0755 "$INSTALL_DIR" "$INSTALL_DIR/releases" "$dest"
    ln -sfn "releases/$v" "$INSTALL_DIR/current"
    local l
    for l in server packaging tools .venv; do
        ln -sfn "current/$l" "$INSTALL_DIR/$l"
    done
}
adopt_legacy_layout

PKG="$INSTALL_DIR/packaging"
[ -d "$PKG" ] || { echo "No packaging directory at $PKG" >&2; exit 1; }

# A regular file, root's, that nobody else can write. Symlinks are refused outright:
# a link in a root-owned directory still names whatever the service wanted it to.
trusted_file() {
    [ -L "$1" ] && return 1
    [ -f "$1" ] || return 1
    local st
    st=$(stat -c '%u %a' "$1") || return 1
    [ "${st% *}" = 0 ] || return 1
    local mode="${st#* }"
    mode="${mode: -3}"
    [ "$(( 8#$mode & 8#022 ))" = 0 ]
}
trusted_dir() {
    local d
    d=$(readlink -f "$1") || return 1
    [ -d "$d" ] || return 1
    local st
    st=$(stat -c '%u %a' "$d") || return 1
    [ "${st% *}" = 0 ] || return 1
    local mode="${st#* }"
    mode="${mode: -3}"
    [ "$(( 8#$mode & 8#022 ))" = 0 ]
}

# The directories everything below is read from. If the service account could write
# either, every file in it is suspect, so nothing is installed at all.
for d in "$INSTALL_DIR" "$PKG" "$INSTALL_DIR/tools"; do
    [ -e "$d" ] || continue
    if ! trusted_dir "$d"; then
        refuse "$d is not a root-owned directory closed to other writers; installing nothing from it"
        exit 1
    fi
done

install -d -m 0755 "$LIB"
# Where root-side helpers publish progress and results back to the service. Root's,
# never the service's: a helper writing into a directory the service owns can be made
# to write through a symlink the service planted, into any file on the box.
install -d -o root -g root -m 0755 "$STATUS"

# ── helper scripts ──
# Every root-side helper the one-way doors call. Listed rather than globbed so a stray
# file in packaging/ cannot become a privileged script by being dropped there.
netwatch_changed=0
thermal_changed=0
for s in wifi-apply.sh usbhost-fix.sh makemkv-run.sh netwatch.sh dvdtools-install.sh \
         cdtools-install.sh thermal.sh riparr-update.sh power-allowed.sh; do
    [ -e "$PKG/$s" ] || [ -L "$PKG/$s" ] || continue
    if ! trusted_file "$PKG/$s"; then
        refuse "$PKG/$s is not a root-owned, unwritable regular file; not installed"
        continue
    fi
    if [ "$s" = netwatch.sh ] && ! cmp -s "$PKG/$s" "$LIB/$s"; then
        netwatch_changed=1
    fi
    if [ "$s" = thermal.sh ] && ! cmp -s "$PKG/$s" "$LIB/$s"; then
        thermal_changed=1
    fi
    install -o root -g root -m 0755 "$PKG/$s" "$LIB/$s"
done

# Data files beside the helpers: read by root, never executed, so 0644.
#   netwatch-settings.py   netwatch.sh runs it as an argument to the system python3
#   powerkey.py            the power button's root half, same arrangement
#   makemkv-manifest.json  the MakeMKV installer's one source of versions and hashes
#   staged-install.sh      sourced by the MakeMKV and DVD-tools installers, never run
for f in netwatch-settings.py powerkey.py makemkv-manifest.json staged-install.sh; do
    [ -e "$PKG/$f" ] || [ -L "$PKG/$f" ] || continue
    if ! trusted_file "$PKG/$f"; then
        refuse "$PKG/$f is not a root-owned, unwritable regular file; not installed"
        continue
    fi
    install -o root -g root -m 0644 "$PKG/$f" "$LIB/$f"
done

# Two helpers live in tools/ rather than packaging/ but are root-side like the rest and
# belong under /usr/local/lib for the same reason. makemkv-install.sh used to be placed
# only by install.sh, so a web update never refreshed it; mount-library.sh used to be
# executed straight out of /opt/riparr.
for s in makemkv-install.sh mount-library.sh; do
    [ -e "$INSTALL_DIR/tools/$s" ] || [ -L "$INSTALL_DIR/tools/$s" ] || continue
    if ! trusted_file "$INSTALL_DIR/tools/$s"; then
        refuse "$INSTALL_DIR/tools/$s is not a root-owned, unwritable regular file; not installed"
        continue
    fi
    install -o root -g root -m 0755 "$INSTALL_DIR/tools/$s" "$LIB/$s"
done

# This script's own root copy, refreshed last of the helpers so a box picks up a newer
# version on its next provision. GNU install unlinks the destination before writing,
# so the copy that is running right now is not rewritten underneath itself.
if trusted_file "$PKG/apply-system.sh"; then
    install -o root -g root -m 0755 "$PKG/apply-system.sh" "$LIB/apply-system.sh"
else
    refuse "$PKG/apply-system.sh is not a root-owned, unwritable regular file; root's copy not refreshed"
fi

# ── units ──
# Same argument: an explicit list, so this cannot install something unreviewed.
#
# riparr-library.service IS here as of 0.3.3, and its absence was a real bug: nothing
# had ever installed it, so no box mounted its share at boot. /srv/library did not even
# exist on the reference unit. That is what made a configured share read as permanently
# lost, with no way back from the interface short of deleting it and adding it again.
install -d -m 0755 "$UNITS"
for u in riparr.slice riparr.service \
         riparr-makemkv.service riparr-makemkv.path \
         riparr-dvdtools.service riparr-dvdtools.path \
         riparr-cdtools.service riparr-cdtools.path \
         riparr-poweroff.service riparr-poweroff.path \
         riparr-reboot.service riparr-reboot.path \
         riparr-usbhost.service riparr-usbhost.path \
         riparr-wifi.service riparr-wifi.path \
         riparr-netwatch.service \
         riparr-powerkey.service \
         riparr-thermal.service \
         riparr-provision.service riparr-provision.path \
         riparr-update.service riparr-update.path \
         riparr-restart.service riparr-restart.path \
         riparr-remount.service riparr-remount.path \
         riparr-library.service; do
    [ -e "$PKG/$u" ] || [ -L "$PKG/$u" ] || continue
    if ! trusted_file "$PKG/$u"; then
        refuse "$PKG/$u is not a root-owned, unwritable regular file; not installed"
        continue
    fi
    install -o root -g root -m 0644 "$PKG/$u" "$UNITS/$u"
done

# The root-side status directory has to exist at every boot, not only after a
# provision. systemd-tmpfiles recreates it from this.
if [ -f "$PKG/riparr-tmpfiles.conf" ] && trusted_file "$PKG/riparr-tmpfiles.conf"; then
    install -d -m 0755 "$SYSROOT/etc/tmpfiles.d"
    install -o root -g root -m 0644 "$PKG/riparr-tmpfiles.conf" "$SYSROOT/etc/tmpfiles.d/riparr.conf"
fi

systemctl daemon-reload

# ── enable what should be running ──
# `enable` without `--now` for the doors: they are path units and start themselves when
# the path appears. The watchdog is a plain service and does want starting, but only if
# it is not already running -- restarting it during an outage would reset the
# escalation counter and start the clock again from zero.
for p in riparr-makemkv riparr-dvdtools riparr-cdtools riparr-poweroff riparr-reboot riparr-usbhost riparr-wifi \
         riparr-provision riparr-update riparr-remount riparr-restart; do
    [ -f "$UNITS/$p.path" ] || continue
    systemctl enable --quiet "$p.path" 2>/dev/null || true
    systemctl start "$p.path" 2>/dev/null || true
done

# The library mount is a oneshot, not a door. Enabling it is what makes a share come
# back on its own after a reboot; starting it now is what makes it come back today,
# without the user having to reboot to get the fix they just installed.
if [ -f "$UNITS/riparr-library.service" ]; then
    systemctl enable --quiet riparr-library.service 2>/dev/null || true
    systemctl start riparr-library.service 2>/dev/null || true
    if mountpoint -q "$SYSROOT/srv/library" 2>/dev/null; then
        say "library share mounted at /srv/library"
    else
        say "library share not mounted yet — add or test a share in the web interface"
    fi
fi

if [ -f "$UNITS/riparr-netwatch.service" ]; then
    systemctl enable --quiet riparr-netwatch.service 2>/dev/null || true
    if ! systemctl is-active --quiet riparr-netwatch.service; then
        systemctl start riparr-netwatch.service 2>/dev/null || true
        say "Wi-Fi watchdog started"
    elif [ "$netwatch_changed" = 1 ]; then
        # A running watchdog keeps the old script until it restarts, so a fix to it
        # reached no box until its next reboot. Restarting resets its escalation count,
        # which is why it is otherwise left alone -- but an update arrived over this
        # network, so there is no outage in progress to lose track of.
        systemctl restart riparr-netwatch.service 2>/dev/null || true
        say "Wi-Fi watchdog restarted with the new version"
    else
        say "Wi-Fi watchdog already running (left alone)"
    fi
fi

# Restarted rather than left alone: it holds no state worth keeping, and an update to
# powerkey.py does nothing until the process running the old one is replaced.
if [ -f "$UNITS/riparr-powerkey.service" ]; then
    systemctl enable --quiet riparr-powerkey.service 2>/dev/null || true
    systemctl restart riparr-powerkey.service 2>/dev/null || true
    say "power button handler started"
fi

# The thermal guard: unlike the Wi-Fi watchdog it has no escalation to lose, so a new
# version simply replaces the old one.
if [ -f "$UNITS/riparr-thermal.service" ]; then
    systemctl enable --quiet riparr-thermal.service 2>/dev/null || true
    if [ "$thermal_changed" = 1 ] && systemctl is-active --quiet riparr-thermal.service; then
        systemctl restart riparr-thermal.service 2>/dev/null || true
        say "Thermal guard restarted with the new version"
    elif ! systemctl is-active --quiet riparr-thermal.service; then
        systemctl start riparr-thermal.service 2>/dev/null || true
        say "Thermal guard started"
    fi
fi

# DVD backups need dvdbackup and libdvdcss (see packaging/dvdtools-install.sh). Asked
# for rather than done here: this unit has a two-minute budget, and apt plus a compile
# that may have to wait for a MakeMKV build's apt lock does not fit in it. Only when
# /run/riparr exists -- creating it here as root would leave the service unable to
# write its own runtime directory. On a fresh install it does not exist yet, and the
# service asks for itself once it is running (backup.start).
if [ -f "$UNITS/riparr-dvdtools.path" ] && [ -d "$RUN" ]; then
    if ! command -v dvdbackup >/dev/null 2>&1 \
       || ! ldconfig -p 2>/dev/null | grep -q 'libdvdcss\.so\.2'; then
        touch "$RUN/dvdtools.request"
        say "DVD backup tools requested (they install in the background)"
    fi
fi

# Music CDs need cd-paranoia, flac and friends (packaging/cdtools-install.sh). Asked for
# the same way as the DVD tools and for the same reasons; only apt, so it is quick.
if [ -f "$UNITS/riparr-cdtools.path" ] && [ -d "$RUN" ]; then
    # Every always-installed tool, not just the first two: a release that adds an
    # encoder has to reach boxes that already have the others.
    for bin in cd-paranoia cd-info flac lame opusenc oggenc bsdtar; do
        if ! command -v "$bin" >/dev/null 2>&1; then
            touch "$RUN/cdtools.request"
            say "music CD tools requested (they install in the background)"
            break
        fi
    done
fi

# ── the hardware watchdog ──
# A box that freezes outright stays frozen until somebody pulls the plug -- found
# 2026-10-12, when the Orange Pi's Wi-Fi chip hung mid-rip and took the kernel with it:
# no network, no log after the hang, and nothing on the board was going to notice. Every
# supported board has a hardware watchdog; with systemd feeding it, a frozen box reboots
# itself within seconds and the interrupted rip carries on when the disc goes back in.
# 15 s is the longest both the Allwinner and the Broadcom watchdogs accept.
if [ -e "$SYSROOT/dev/watchdog" ] || [ -e "$SYSROOT/dev/watchdog0" ]; then
    want='[Manager]
RuntimeWatchdogSec=15s
RebootWatchdogSec=10min'
    conf="$SYSROOT/etc/systemd/system.conf.d/riparr-watchdog.conf"
    if [ "$(cat "$conf" 2>/dev/null)" != "$want" ]; then
        mkdir -p "$SYSROOT/etc/systemd/system.conf.d"
        printf '%s\n' "$want" > "$conf"
        systemctl daemon-reexec
        say "Hardware watchdog on: a frozen box reboots itself"
    fi
fi

# ── Bluetooth off on the Orange Pi's AW859A ──
# Riparr never uses Bluetooth, and on this chip it shares the radio with Wi-Fi. When the
# Wi-Fi firmware hung, the driver's own recovery tried to power-cycle the chip and was
# refused -- "can not power off, other module is on" -- because Bluetooth was holding it,
# so Wi-Fi never came back. Not starting hciattach is not enough: the driver's own
# sprdbt_tty powers the chip's Bluetooth side whenever the Bluetooth rfkill switch is
# unblocked, and the vendor unit unblocks everything ("power state:5" -- Wi-Fi and
# Bluetooth -- with no Bluetooth service running at all). So the radio is blocked as
# well. sunxi_addr still loads and Wi-Fi is still unblocked: the Wi-Fi side needs both.
if systemctl cat aw859a-bluetooth.service >/dev/null 2>&1; then
    want='[Service]
ExecStartPre=
ExecStartPre=/sbin/modprobe sunxi_addr
ExecStartPre=/usr/sbin/rfkill unblock wifi
ExecStartPre=/usr/sbin/rfkill block bluetooth
ExecStart=
ExecStart=/bin/true'
    conf="$UNITS/aw859a-bluetooth.service.d/riparr-no-bluetooth.conf"
    if [ "$(cat "$conf" 2>/dev/null)" != "$want" ]; then
        mkdir -p "$(dirname "$conf")"
        printf '%s\n' "$want" > "$conf"
        systemctl daemon-reload
        rfkill block bluetooth 2>/dev/null || true
        say "Bluetooth off (it blocked Wi-Fi recovery)"
    fi
fi

if [ "$refused" != 0 ]; then
    say "System units are NOT all up to date: something was refused (see above)"
    exit 1
fi
say "System units are up to date"
