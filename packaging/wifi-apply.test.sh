#!/bin/bash
# What wifi-apply.sh writes into wpa_supplicant's config from the service's database,
# and what it refuses to.
#
# The network list is the service's to write and this script runs as root, so a saved
# SSID is untrusted input to a root-parsed config file. What is asserted: a plain list
# is written in priority order with the country kept; an SSID carrying a newline (which
# would end the ssid= line and start a directive of the attacker's choosing) is
# skipped; an SSID over 32 bytes is skipped; a "psk" that is not 64 hex digits is
# skipped; the network already in the file is adopted and handed back; and the state
# file lands in the root-owned status directory.
#
# Everything that touches the system -- wpa_cli, iw, systemctl -- is a stub on PATH,
# and the config directory and status directory are temporary.
#
# Run: bash packaging/wifi-apply.test.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/riparr-wifi-test.XXXXXX") && [ -n "$T" ] || { echo "mktemp failed"; exit 1; }
trap 'rm -rf "$T"' EXIT
FAILED=0
check() {  # name, got, want
  if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', wanted '$3'"; FAILED=1; fi
}

BIN="$T/bin"; mkdir -p "$BIN" "$T/wpa" "$T/status"
printf '#!/bin/sh\nexit 0\n' > "$BIN/systemctl"
printf '#!/bin/sh\nexit 0\n' > "$BIN/wpa_cli"
printf '#!/bin/sh\necho "	SSID: Home"\n' > "$BIN/iw"
printf '#!/bin/sh\nexit 0\n' > "$BIN/chown"     # the config is root's on the box; here it is ours
printf '#!/bin/sh\nexit 0\n' > "$BIN/sleep"     # the script waits for an association; the stub answers at once
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"
export RIPARR_DB="$T/riparr.db" RIPARR_WIFI_IFACE=wlan0 RIPARR_WPA_DIR="$T/wpa" RIPARR_STATUS_DIR="$T/status"
CONF="$T/wpa/wpa_supplicant-wlan0.conf"

PY="$REPO/server/.venv/bin/python"; [ -x "$PY" ] || PY=python3
dbpy() { ( cd "$REPO/server" && RIPARR_APPLIANCE=0 "$PY" -c "from riparr import db; $1" ) \
           || { echo "setup failed: $1"; exit 1; }; }
run() { bash "$REPO/packaging/wifi-apply.sh" > "$T/out" 2> "$T/err"; }
ssids() { sed -n 's/^\tssid="\(.*\)"$/\1/p' "$CONF" | tr '\n' ';'; }

GOOD_PSK=$(printf 'a%.0s' $(seq 1 64))

printf 'ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev\nupdate_config=1\ncountry=GB\n\nnetwork={\n\tssid="Home"\n\tpsk=%s\n\tpriority=5\n}\n' "$GOOD_PSK" > "$CONF"
dbpy "db.init(); db.set('wifi_networks', [
  {'ssid': 'Office', 'psk': '$GOOD_PSK', 'open': False},
  {'ssid': 'Cafe', 'psk': '', 'open': True},
])"

echo "a plain list is written in order, and the network already there is kept"
run
check "the networks, best first, then the one adopted from the file" "$(ssids)" "Office;Cafe;Home;"
check "the country survives" "$(grep -c '^country=GB$' "$CONF")" "1"
check "the open network has no key" "$(awk '/ssid="Cafe"/{f=1} f&&/key_mgmt=NONE/{print "none"; exit}' "$CONF")" "none"
check "the state file is in the status directory" "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["phase"])' "$T/status/wifi.state")" "done"
check "and nothing was written into the service's runtime directory" "$([ -e /run/riparr/wifi.state ] && echo yes || echo no)" "no"
check "the merged list is handed back, readable by the service only" "$(stat -c '%a' "$T/status/wifi.merged.json")" "640"

echo "a name or key that could change the file's meaning is skipped"
dbpy "db.set('wifi_networks', [
  {'ssid': 'Office', 'psk': '$GOOD_PSK', 'open': False},
  {'ssid': 'Evil\"\\n\\tpsk=0\\n\\tctrl_interface=/tmp\\n\\tssid=\"x', 'psk': '$GOOD_PSK', 'open': False},
  {'ssid': 'TooLongTooLongTooLongTooLongTooLong', 'psk': '', 'open': True},
  {'ssid': 'Tab\\tName', 'psk': '', 'open': True},
  {'ssid': 'BadKey', 'psk': 'hunter2', 'open': False},
  {'ssid': 'Cafe', 'psk': '', 'open': True},
])"
run
check "only the good ones are written" "$(ssids)" "Office;Cafe;Home;"
check "no directive was injected" "$(grep -c 'ctrl_interface=/tmp' "$CONF")" "0"
check "no passphrase was written as a key" "$(grep -c 'hunter2' "$CONF")" "0"
check "each refusal is named" "$(grep -c 'skipping' "$T/err")" "4"
check "the config still parses as networks" "$(grep -c '^network={$' "$CONF")" "3"

if [ "$FAILED" = 0 ]; then echo "all ok"; else echo "FAILED"; exit 1; fi
