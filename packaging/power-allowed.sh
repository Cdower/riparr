#!/bin/sh
# Root half of the power gate. Run as ExecCondition= by riparr-reboot.service,
# riparr-poweroff.service and riparr-powerkey.service, which load the same
# configuration files the web service does (the boot partition's riparr.conf, then
# /etc/default/riparr).
#
# RIPARR_ALLOW_POWER=0 turns the web page's Restart and Shut down, and the physical
# power button's handling, off at the root side -- so a service that has been talked
# into writing a request anyway still cannot reboot the host. The web service hides
# the controls and refuses the request too; this is the half it cannot get around.
#
# The default is on: an appliance with no power switch needs a safe way to stop, and
# this is it. tools/install.sh in host mode writes RIPARR_ALLOW_POWER=0 to
# /etc/default/riparr, because on a shared host nothing on a web page should be able
# to turn the machine off.
#
# Usage: power-allowed.sh [reboot|poweroff]
#
# When refused, the request file for that action is removed here. ExecCondition= runs
# before ExecStartPre=, so without this the file would survive and the path unit would
# fire the service again, and again, until systemd gave up on it.
set -u
action="${1:-}"
if [ "${RIPARR_ALLOW_POWER:-1}" = 0 ]; then
    case "$action" in
        reboot|poweroff) rm -f "/run/riparr/$action.request" ;;
    esac
    logger -t riparr-power -p daemon.warning -- \
        "Refused ${action:-power-key handling}: RIPARR_ALLOW_POWER=0 on this box."
    exit 1
fi
exit 0
