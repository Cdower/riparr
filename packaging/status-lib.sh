#!/bin/bash
# Where root-side helpers publish progress and results for the web service to read.
#
# Root's, never the service's: a helper writing into the service's own /run/riparr
# could be made to write through a symlink the service planted, into any file on the
# box. The service reads this directory; it cannot write it. riparr-tmpfiles.conf
# creates it at boot and apply-system.sh at install; this makes sure of it for a
# helper that runs before either has, and is sourced by every helper that writes here.
STATUS_DIR="${RIPARR_STATUS_DIR:-/run/riparr-status}"
install -d -o root -g root -m 0755 "$STATUS_DIR"
