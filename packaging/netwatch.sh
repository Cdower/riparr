#!/bin/bash
# Riparr Wi-Fi watchdog.
#
# Why this exists, measured on the reference box 2026-08-29:
#
# The Orange Pi Zero 2W's radio is a UNISOC AW859A driven by `sprdwl_ng` /
# `uwe5622_bsp_sdio` -- a vendor blob with no health reporting. When its firmware
# wedges it leaves the carrier asserted and the association nominally intact, so
# nothing upstream ever learns the link is dead:
#
#   * the box stayed up 17.5 hours with an unbroken hourly heartbeat
#   * the kernel logged NOTHING about wlan0 in a seven-day boot
#   * systemd-networkd logged 14 lines, all from boot -- no carrier loss, no
#     re-association, no DHCP complaint
#   * the only evidence was one line: networkd-wait-online timing out
#
# So there is no event to subscribe to and nothing to react to. The only way to know
# the radio is dead is to send a packet and see whether anything comes back. Hence a
# poller rather than a hook.
#
# Escalation, cheapest first, because each rung costs more than the last:
#
#   1. restart wpa_supplicant   -- re-associates; fixes a lost/stale association
#   2. reload the driver module -- rebuilds firmware state; fixes a wedged blob
#   3. reboot                   -- fixes whatever the first two did not
#
# A rung is only climbed after the one below it was given a fair chance to work.
#
# THE REBOOT NEVER INTERRUPTS A RIP. system.py:356 already argues that a machine
# deciding the moment is right to interrupt somebody's disc is not acceptable, and a
# watchdog is exactly the kind of machine that would. A rip in progress defers the
# reboot indefinitely -- the box is already unreachable, so waiting costs nothing that
# is not already lost, while rebooting would additionally destroy 40 minutes of work.
#
# Which interface, and whether to run at all, 2026-11-05:
#
# This used to assume wlan0. On a Raspberry Pi on Ethernet that interface exists and
# carries nothing, so every probe failed and the box rebooted itself every twelve
# minutes, forever. Now the interface is the one carrying the default route, and the
# watchdog only runs when that interface is wireless -- a wired link that goes quiet
# has a cable to look at, not a firmware blob. RIPARR_NETWATCH_IFACE names an
# interface explicitly and is watched whatever it is.
#
# The reboot rung is behind RIPARR_NETWATCH_REBOOT=1 as well as the web-page setting.
# This script's own default is 0: run anywhere it was not explicitly set up for, it
# will re-associate and reload a driver and never restart the machine. The shipped
# appliance unit (riparr-netwatch.service) sets it to 1, because on the appliance the
# reboot is the feature; tools/install.sh in host mode sets it back to 0 in
# /etc/default/riparr.
#
# The decisions are functions (decide, should_watch, parse_settings) with no side
# effects, so packaging/netwatch.test.sh can exercise them without root or a radio.
# Set RIPARR_NETWATCH_LIB=1 to source this file without starting the loop.

set -u

IFACE_EXPLICIT="${RIPARR_NETWATCH_IFACE:-}"
MODULE="${RIPARR_NETWATCH_MODULE:-sprdwl_ng}"
REBOOT_ALLOWED="${RIPARR_NETWATCH_REBOOT:-0}"

# Every INTERVAL seconds, one probe. 60s is a compromise: often enough that a dead
# radio is caught in minutes rather than hours, rare enough to be invisible.
INTERVAL="${RIPARR_NETWATCH_INTERVAL:-60}"

DB="${RIPARR_DB:-/var/lib/riparr/riparr.db}"
# The system interpreter, never the service's virtualenv: this runs as root and
# needs only the standard library.
PY="${RIPARR_PYTHON:-python3}"
READER="$(dirname "$0")/netwatch-settings.py"

# Consecutive failures before each rung, read from the database on every probe so a
# change in the web interface takes effect within the minute with nothing to restart.
# One small query against a file already in page cache; a config-reload path would be
# more machinery than the thing it manages.
#
# These defaults match db.DEFAULTS and are what applies if the database cannot be read
# at all -- during first boot, say. A watchdog that refuses to run because it could not
# find a preference is worse than one running on sensible numbers.
#
# Deliberately not tighter than a few minutes: a router rebooting, or a firmware upgrade
# on the access point, is a normal event that resolves itself, and a watchdog that
# reboots the appliance over it is worse than the fault it is guarding against.
ENABLED=1
FAIL_ASSOC="${RIPARR_NETWATCH_FAIL_ASSOC:-3}"
FAIL_RELOAD="${RIPARR_NETWATCH_FAIL_RELOAD:-6}"
FAIL_REBOOT="${RIPARR_NETWATCH_FAIL_REBOOT:-12}"

# The reader prints KEY=number lines from values a web form put in the database. They
# are read as exactly that -- a known name, a run of digits -- and nothing else is
# accepted. This used to be `eval`, which is the wrong tool for anything that passed
# through a file the service account can write, however the values were produced.
parse_settings() {   # stdin: KEY=value lines; sets ENABLED/FAIL_* for the ones that fit
    local line key val
    while IFS= read -r line; do
        case "$line" in
            ENABLED=*|FAIL_ASSOC=*|FAIL_RELOAD=*|FAIL_REBOOT=*) ;;
            *) continue ;;
        esac
        key="${line%%=*}"; val="${line#*=}"
        case "$val" in
            ''|*[!0-9]*) continue ;;
        esac
        printf -v "$key" '%s' "$val"
    done
    return 0
}

load_settings() {
    [ -f "$READER" ] || return 0
    local out
    out="$("$PY" "$READER" "$DB" 2>/dev/null)" || return 0
    [ -n "$out" ] && parse_settings <<< "$out"
    return 0
}

log() { logger -t riparr-netwatch -p daemon.notice -- "$@"; echo "riparr-netwatch: $*"; }
warn() { logger -t riparr-netwatch -p daemon.warning -- "$@"; echo "riparr-netwatch: $*" >&2; }

# ── the decisions ──

# The interface carrying the default route, or nothing if there is none yet.
default_iface() {
    ip route show default 2>/dev/null |
        awk '{ for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit } }'
}

is_wireless() { [ -d "/sys/class/net/$1/wireless" ]; }

# Whether to watch, and what. Prints one of:
#   watch:<iface>   watch this interface
#   wait            no default route yet; ask again later
#   skip:<reason>   not a job for this box; exit quietly
# Arguments: the explicit interface (may be empty), the default-route interface (may
# be empty), and whether that one is wireless (1/0).
should_watch() {   # explicit, route_iface, route_is_wireless
    local explicit="$1" route="$2" wireless="$3"
    if [ -n "$explicit" ]; then echo "watch:$explicit"; return; fi
    if [ -z "$route" ]; then echo "wait"; return; fi
    if [ "$wireless" = 1 ]; then echo "watch:$route"; return; fi
    echo "skip:$route carries the default route and is not wireless"
}

# What to do after `fails` consecutive failed probes. Prints one of:
#   wait          not yet
#   reassociate   rung 1 (once per outage)
#   reload        rung 2 (once per outage)
#   defer         would reboot, but a rip is in progress
#   reboot        rung 3
# Rungs are guarded by did_* rather than by equality on the counter, so a rung fires
# once per outage instead of once per matching count -- and a longer outage does not
# re-run the cheap fix forever. The reboot needs both the threshold (which the web
# page's "restart as a last resort" switch pushes out of reach when off) and the
# environment's permission.
decide() {   # fails did_assoc did_reload ripping fail_assoc fail_reload fail_reboot reboot_allowed
    local fails="$1" did_assoc="$2" did_reload="$3" ripping="$4"
    local fail_assoc="$5" fail_reload="$6" fail_reboot="$7" allowed="$8"
    if [ "$fails" -ge "$fail_reboot" ] && [ "$allowed" = 1 ]; then
        if [ "$ripping" = 1 ]; then echo defer; else echo reboot; fi
        return
    fi
    if [ "$fails" -ge "$fail_reload" ] && [ "$did_reload" = 0 ]; then echo reload; return; fi
    if [ "$fails" -ge "$fail_assoc" ] && [ "$did_assoc" = 0 ]; then echo reassociate; return; fi
    echo wait
}

# ── the actions ──

# The gateway is looked up every probe rather than cached at start. A box that is
# carried between networks -- which is the whole point of the saved-network list --
# gets a different gateway at each one, and a cached address would make this report a
# permanent outage on a network that is working perfectly.
gateway() {
    ip route show default dev "$IFACE" 2>/dev/null |
        awk '/default/ {print $3; exit}'
}

# One ICMP echo, 5s. Success means the radio moved a packet and something answered,
# which is the only claim this script makes or needs.
#
# The gateway rather than the internet: an appliance that writes to a NAS on the LAN
# needs the LAN, and an ISP outage is not a reason to reboot. It is also the one host
# guaranteed to exist on every network the box is carried to.
probe() {
    local gw
    gw="$(gateway)" || return 1
    [ -n "$gw" ] || return 1
    ping -c 1 -W 5 -I "$IFACE" "$gw" >/dev/null 2>&1
}

# A rip is any live makemkvcon. Checked by process rather than by asking the Riparr
# API, because the API needs a session and this runs as a system daemon -- and because
# the process is the thing that would actually be destroyed by a reboot.
ripping() {
    pgrep -x makemkvcon >/dev/null 2>&1 || pgrep -f 'makemkvcon ' >/dev/null 2>&1
}

# Raspberry Pi OS manages Wi-Fi with NetworkManager; Armbian, as provisioned, with
# networkd and wpa_supplicant@. Raspberry Pi OS also ships the wpa_supplicant@ template
# (NetworkManager drives wpa_supplicant itself), so "does the unit exist" cannot tell
# them apart -- and restarting wpa_supplicant@wlan0 there would start a second
# supplicant fighting NetworkManager for the radio. "Is it running" can: on Armbian the
# unit is up even while the radio is wedged, and on a Pi it never is.
uses_nm() {
    ! systemctl is-active --quiet "wpa_supplicant@$IFACE.service" 2>/dev/null &&
        systemctl is-active --quiet NetworkManager 2>/dev/null
}

reassociate() {
    if uses_nm; then
        log "Re-associating: asking NetworkManager to reconnect $IFACE."
        nmcli device disconnect "$IFACE" >/dev/null 2>&1 || true
        sleep 2
        nmcli device connect "$IFACE" >/dev/null 2>&1 ||
            warn "NetworkManager could not reconnect $IFACE."
        return
    fi
    log "Re-associating: restarting wpa_supplicant@$IFACE."
    systemctl restart "wpa_supplicant@$IFACE.service" 2>/dev/null ||
        warn "Could not restart wpa_supplicant@$IFACE."
    # networkd re-runs DHCP on carrier, but only if it notices the carrier moved.
    networkctl reconfigure "$IFACE" >/dev/null 2>&1 || true
}

reload_driver() {
    if ! lsmod 2>/dev/null | grep -q "^${MODULE} "; then
        warn "Module $MODULE is not loaded; skipping the reload rung."
        return 1
    fi
    log "Reloading the $MODULE driver."
    if uses_nm; then
        # NetworkManager notices the interface going and coming back on its own.
        modprobe -r "$MODULE" 2>/dev/null || warn "rmmod $MODULE was refused."
        sleep 2
        modprobe "$MODULE" 2>/dev/null || warn "modprobe $MODULE failed."
        sleep 5
        nmcli device connect "$IFACE" >/dev/null 2>&1 || true
        return
    fi
    # wpa_supplicant holds the interface; stop it first or the rmmod is refused and
    # the rung silently does nothing, which reads in the log as "the reload didn't
    # help" when in fact it never happened.
    systemctl stop "wpa_supplicant@$IFACE.service" 2>/dev/null || true
    modprobe -r "$MODULE" 2>/dev/null || warn "rmmod $MODULE was refused."
    sleep 2
    modprobe "$MODULE" 2>/dev/null || warn "modprobe $MODULE failed."
    sleep 5
    systemctl start "wpa_supplicant@$IFACE.service" 2>/dev/null || true
    networkctl reconfigure "$IFACE" >/dev/null 2>&1 || true
}

# After a hard reset the box can come back as riparr-2.local: avahi hears the name it
# announced before the crash still cached on the network, takes it for somebody else's,
# and renames itself -- found 2026-10-13, after the watchdog rebooted a frozen box, when
# riparr.local simply stopped answering. If nothing answers for our own name, the name
# is free again and is ours to take back. If something does (a second Riparr), leave it.
reclaim_name() {
    command -v avahi-resolve >/dev/null 2>&1 || return 0
    command -v avahi-set-host-name >/dev/null 2>&1 || return 0
    local want got
    want=$(hostname 2>/dev/null) || return 0
    [ -n "$want" ] || return 0
    got=$(timeout 10 avahi-resolve -4 -n "$want.local" 2>/dev/null | awk '{print $2}')
    if [ -z "$got" ]; then
        log "Nothing answers for $want.local (renamed after a restart?); taking the name back."
        avahi-set-host-name "$want" 2>/dev/null || warn "Couldn't set the mDNS name back to $want."
    fi
}

# Pick the interface, waiting for a default route to appear after boot. A box with
# no route for a long time is a box with no network to watch yet, not a fault.
choose_iface() {
    local said=0 route wireless verdict
    while :; do
        route=$(default_iface)
        wireless=0
        [ -n "$route" ] && is_wireless "$route" && wireless=1
        verdict=$(should_watch "$IFACE_EXPLICIT" "$route" "$wireless")
        case "$verdict" in
            watch:*) IFACE="${verdict#watch:}"; return 0 ;;
            skip:*)
                log "Not watching: ${verdict#skip:}. Set RIPARR_NETWATCH_IFACE to watch an interface anyway."
                return 1 ;;
            wait)
                if [ "$said" = 0 ]; then log "No default route yet; waiting for one."; said=1; fi
                sleep "$INTERVAL" ;;
        esac
    done
}

main() {
    local fails=0 did_assoc=0 did_reload=0 deferred=0 action

    choose_iface || exit 0

    # Not every board is the Orange Pi. If its driver is not loaded, take whichever
    # module actually drives the interface (brcmfmac on a Raspberry Pi), so the reload
    # rung reloads something real instead of logging "not loaded" and skipping.
    if ! lsmod 2>/dev/null | grep -q "^${MODULE} "; then
        local _m
        _m=$(basename "$(readlink -f "/sys/class/net/$IFACE/device/driver/module" 2>/dev/null)" 2>/dev/null)
        [ -n "$_m" ] && [ "$_m" != "module" ] && MODULE="$_m"
    fi

    load_settings
    log "Watching $IFACE. Probe every ${INTERVAL}s; re-associate after $FAIL_ASSOC, reload after $FAIL_RELOAD, reboot after $FAIL_REBOOT (reboot $([ "$REBOOT_ALLOWED" = 1 ] && echo allowed || echo not allowed by RIPARR_NETWATCH_REBOOT))."

    while :; do
        sleep "$INTERVAL"
        load_settings

        # Switched off mid-outage means stop, not finish the ladder. Somebody turning it
        # off while watching the box misbehave is asking it to stop now, not to carry on
        # to the reboot it had already decided on.
        if [ "$ENABLED" != "1" ]; then
            if [ "$fails" -gt 0 ]; then
                log "Watchdog switched off; abandoning recovery."
                fails=0; did_assoc=0; did_reload=0; deferred=0
            fi
            continue
        fi

        if probe; then
            if [ "$fails" -gt 0 ]; then
                log "$IFACE answered again after $fails failed probe(s)."
            fi
            fails=0; did_assoc=0; did_reload=0; deferred=0
            reclaim_name
            continue
        fi

        fails=$((fails + 1))
        warn "No answer from the gateway on $IFACE ($fails consecutive)."

        local rip=0
        ripping && rip=1
        action=$(decide "$fails" "$did_assoc" "$did_reload" "$rip" \
                        "$FAIL_ASSOC" "$FAIL_RELOAD" "$FAIL_REBOOT" "$REBOOT_ALLOWED")
        case "$action" in
            defer)
                # Logged once, not every minute: the outage is already the headline
                # and a repeated line would bury it.
                if [ "$deferred" -eq 0 ]; then
                    deferred=1
                    warn "Would reboot, but a rip is in progress. Holding until it finishes — the disc is worth more than the downtime."
                fi ;;
            reboot)
                warn "Still dead after $fails probes and both recovery attempts. Rebooting."
                systemctl reboot
                # If the reboot is refused, do not spin on it every minute.
                sleep 300 ;;
            reload)
                did_reload=1
                reload_driver ;;
            reassociate)
                did_assoc=1
                reassociate ;;
            wait) ;;
        esac
    done
}

if [ "${RIPARR_NETWATCH_LIB:-0}" != 1 ]; then
    main "$@"
fi
