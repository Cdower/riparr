#!/usr/bin/env bash
# Install or upgrade Riparr.
#
# Run it from inside a release you have verified (the README has the four lines:
# download the archive and SHA256SUMS.txt, `sha256sum -c`, unpack, then):
#
#   sudo bash riparr/tools/install.sh
#
# Never `curl | bash`: a script piped from the network runs before anyone has checked
# what it is, and this one runs as root.
#
# Safe to run again: re-running upgrades in place and keeps your database, settings and
# shares. Reads the port the preparer wrote to the boot partition, if it is there.
#
# Two kinds of machine, 2026-11-05:
#
#   appliance   a board the Preparer wrote a card for, with riparr.conf on the boot
#               partition, or any board with a device-tree model. Riparr is the whole
#               point of the machine, so the installer sets the machine up around it:
#               a kernel TCP setting that stops a dead host costing two minutes per
#               rip, avahi owning the .local name and advertising the service, the
#               web page allowed to restart and shut the box down, the Wi-Fi watchdog
#               allowed to reboot a box whose radio has died.
#   host        anything else: a PC, a VM, a server that does other things. Nothing
#               system-wide is touched, and nothing on a web page can turn the
#               machine off. The service, its units and its root-side helpers are
#               installed exactly as on an appliance.
#
# The kind is detected (see decide_mode) and said; --appliance and --host override it,
# and the individual --no-* flags turn one thing off whatever the mode. Every change
# made outside /opt/riparr, /var/lib/riparr and the units is printed as it is made and
# listed again at the end, and tools/uninstall.sh (or --undo) takes each one back out.
#
# Options:
#   --appliance            set the machine up around Riparr (the default on a board)
#   --host                 touch nothing system-wide; power controls off (the default
#                          on anything that is not a board)
#   --no-sysctl            do not write the TCP fast-fail sysctl
#   --no-mdns              do not install avahi, hand it the .local name, or advertise
#   --no-power             RIPARR_ALLOW_POWER=0: no restart or shutdown from the page
#   --no-netwatch-reboot   RIPARR_NETWATCH_REBOOT=0: the watchdog never reboots
#   --undo                 run tools/uninstall.sh instead (see it for --purge)
set -euo pipefail

REPO_URL="${RIPARR_REPO_URL:-https://github.com/jackharvest/riparr}"
BRANCH="${RIPARR_BRANCH:-main}"
INSTALL_DIR="${RIPARR_INSTALL_DIR:-/opt/riparr}"
DATA_DIR=/var/lib/riparr
SERVICE=riparr.service
RIPARR_USER=riparr
BUILD_USER=riparr-build
# The operator's configuration file, read by every Riparr unit after the boot
# partition's riparr.conf. Host mode writes the two switches here.
DEFAULTS_FILE="${RIPARR_DEFAULTS_FILE:-/etc/default/riparr}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n\n' "$*" >&2; exit 1; }

# Every system-wide change, for the summary at the end.
CHANGES=()
changed() { CHANGES+=("$*"); printf '  \033[33m~\033[0m %s\n' "$*"; }

# ── the mode and the flags ──
MODE=""           # appliance | host; empty until decided
DO_SYSCTL=""      # yes | no; empty means "whatever the mode says"
DO_MDNS=""
DO_POWER=""
DO_NETWATCH_REBOOT=""
UNDO=0

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --appliance) MODE=appliance ;;
      --host) MODE=host ;;
      --no-sysctl) DO_SYSCTL=no ;;
      --no-mdns) DO_MDNS=no ;;
      --no-power) DO_POWER=no ;;
      --no-netwatch-reboot) DO_NETWATCH_REBOOT=no ;;
      --undo) UNDO=1 ;;
      -h|--help) sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
  done
}

# Appliance if the Preparer wrote a riparr.conf to the boot partition, or if the
# machine has a device-tree model at all. This is a different question from the
# service's IS_APPLIANCE (real hardware versus a developer's mock), so it is a
# different test: a VM with a riparr.conf is a dedicated machine as far as system-wide
# settings go, and still runs the service against real drives. Arguments are the boot
# directory (may be empty) and the device-tree model file, so the decision can be
# tested without a board.
decide_mode() {   # boot_dir, model_file
  local boot="$1" model="$2"
  if [ -n "$boot" ] && [ -f "$boot/riparr.conf" ]; then echo appliance; return; fi
  if [ -s "$model" ]; then echo appliance; return; fi
  echo host
}

# After decide_mode and parse_args: each switch is what the flag said, else what the
# mode says.
settle_switches() {
  local mode_default=yes
  [ "$MODE" = host ] && mode_default=no
  : "${DO_SYSCTL:=$mode_default}"
  : "${DO_MDNS:=$mode_default}"
  : "${DO_POWER:=$mode_default}"
  : "${DO_NETWATCH_REBOOT:=$mode_default}"
}

# KEY=VALUE in the defaults file: replaced if present, appended if not, the file
# created with a header if absent. Idempotent, and it leaves any other line alone.
set_default_kv() {   # key, value
  local key="$1" val="$2" f="$DEFAULTS_FILE"
  if [ ! -f "$f" ]; then
    install -d -m 0755 "$(dirname "$f")"
    cat > "$f" <<'HEAD'
# Riparr's operator configuration. Read by every Riparr unit after the boot
# partition's riparr.conf, so a line here wins. Written by tools/install.sh; edit
# freely. tools/uninstall.sh removes it.
HEAD
    chmod 0644 "$f"
  fi
  if grep -q "^${key}=" "$f"; then
    if ! grep -q "^${key}=${val}\$" "$f"; then
      sed -i "s|^${key}=.*|${key}=${val}|" "$f"
      changed "$f: ${key}=${val}"
    fi
  else
    printf '%s=%s\n' "$key" "$val" >> "$f"
    changed "$f: ${key}=${val}"
  fi
}

# A key removed from the defaults file, if it is there. Used when a flag turns a
# feature back on after an earlier install turned it off.
unset_default_kv() {   # key
  local key="$1" f="$DEFAULTS_FILE"
  [ -f "$f" ] && grep -q "^${key}=" "$f" || return 0
  sed -i "/^${key}=/d" "$f"
  changed "$f: ${key} removed (back to the unit's default)"
}

main() {
  parse_args "$@"
  [ "$(id -u)" = 0 ] || die "Run this with sudo:  sudo bash tools/install.sh"
  [ "$(uname -s)" = Linux ] || die "Riparr installs on Linux. This is $(uname -s)."

  if [ "$UNDO" = 1 ]; then
    exec bash "$(dirname "$0")/uninstall.sh"
  fi

  # ── where the boot partition is mounted differs by OS version ──
  # config.txt only exists on Raspberry Pi. Armbian on Allwinner keeps /boot inside the
  # root filesystem with armbianEnv.txt instead, and riparr.conf is written straight there.
  local BOOT="" d
  for d in /boot/firmware /boot; do
    [ -d "$d" ] || continue
    if [ -f "$d/riparr.conf" ] || [ -f "$d/config.txt" ] || [ -f "$d/armbianEnv.txt" ]; then
      BOOT="$d"; break
    fi
  done

  local PORT=9797
  if [ -n "$BOOT" ] && [ -f "$BOOT/riparr.conf" ]; then
    # shellcheck disable=SC1090
    . "$BOOT/riparr.conf"
    PORT="${RIPARR_PORT:-9797}"
  fi

  [ -n "$MODE" ] || MODE=$(decide_mode "$BOOT" /proc/device-tree/model)
  settle_switches

  say "Installing Riparr"
  info "port $PORT · $INSTALL_DIR · $(cat /proc/device-tree/model 2>/dev/null | tr -d '\0' || echo 'not a board')"
  if [ "$MODE" = appliance ]; then
    info "mode: appliance — this machine is set up around Riparr (--host to install without touching the system)"
  else
    info "mode: host — nothing system-wide is changed and the web page cannot power the machine (--appliance to change that)"
  fi
  info "sysctl: $DO_SYSCTL · mDNS: $DO_MDNS · power controls: $DO_POWER · watchdog may reboot: $DO_NETWATCH_REBOOT"

  # ── 1. packages ──
  say "1/6  Packages"
  export DEBIAN_FRONTEND=noninteractive
  local MISSING=() pkg
  # avahi-daemon is what makes http://<hostname>.local resolve. Raspberry Pi OS Lite
  # usually has it, but "usually" is not good enough for the one address we print.
  # Armbian ships no avahi at all, which is why the Preparer switches on the
  # systemd-resolved responder instead — see the handoff below. Only in appliance
  # mode, or with mDNS asked for: on a host, the name is the host's business.
  # smbclient and cifs-utils are how the box talks to a network share -- the first to
  # browse and test it, the second to mount it. Neither is installed by default, and
  # without smbclient the share step of the first-run wizard fails with a bare 500.
  # eject is how the box gives a disc back -- there is no button on the enclosure. It is
  # NOT part of a minimal Debian: without it every failure path that returns the disc
  # raised FileNotFoundError straight through the handler that called it, so a rip failed
  # with "No such file or directory: 'eject'" and the real reason was never recorded.
  # wpasupplicant brings wpa_cli, which is how the box scans for networks and asks the
  # supplicant to re-read its config after the network list changes. The daemon itself is
  # already running -- it is what joined the network the Preparer wrote -- but a minimal
  # image can carry the daemon without the client.
  # iw reads the live association back (which SSID, what signal, which band). Without it
  # a perfectly healthy 5 GHz link reports as "not connected".
  local PKGS="python3-venv python3-pip git ca-certificates smbclient cifs-utils eject wpasupplicant iw"
  [ "$DO_MDNS" = yes ] && PKGS="$PKGS avahi-daemon avahi-utils"
  for pkg in $PKGS; do
    dpkg -s "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
  done
  if [ ${#MISSING[@]} -gt 0 ]; then
    info "installing: ${MISSING[*]}"
    apt-get update -qq
    apt-get install -y -qq "${MISSING[@]}" >/dev/null
  fi

  if [ "$DO_MDNS" = yes ]; then
    systemctl enable --now avahi-daemon >/dev/null 2>&1 || true

    # ── hand the .local name from systemd-resolved to avahi ──
    # The Preparer turns on resolved's mDNS responder so the box is reachable by name
    # *before* Riparr exists — the setup that gets us here depends on it. Installing
    # avahi puts a second responder on UDP 5353 for the same name. Both answer, both
    # answer correctly today, and either can decide tomorrow that the other is a name
    # conflict and rename itself to riparr-2.local — silently breaking the one address
    # this script prints.
    #
    # So: one owner. avahi wins because it also publishes service records, which
    # resolved does not. The handoff is guarded — resolved keeps the name unless avahi
    # is demonstrably answering, because a box that is nameless is much worse than a
    # box with a redundant responder.
    if systemctl is-active --quiet avahi-daemon && command -v avahi-resolve >/dev/null 2>&1; then
      if avahi-resolve -n "$(hostname).local" >/dev/null 2>&1; then
        if [ ! -f /etc/systemd/resolved.conf.d/20-riparr-avahi.conf ]; then
          mkdir -p /etc/systemd/resolved.conf.d
          cat > /etc/systemd/resolved.conf.d/20-riparr-avahi.conf <<'CONF'
# Written by tools/install.sh. avahi-daemon now owns <hostname>.local; two mDNS
# responders for one name is how a box silently renames itself to <hostname>-2.
[Resolve]
MulticastDNS=no
CONF
          systemctl restart systemd-resolved >/dev/null 2>&1 || true
          changed "/etc/systemd/resolved.conf.d/20-riparr-avahi.conf: systemd-resolved's mDNS responder off (avahi owns $(hostname).local)"
        fi
        ok "avahi owns $(hostname).local (resolved's responder stood down)"
      else
        info "avahi is not answering yet — leaving systemd-resolved to hold the name"
      fi
    fi

    # Publish Riparr as a service, not just a name. This is the half avahi does that
    # systemd-resolved cannot, and it is why the handoff above is worth making: the box
    # now shows up in anything that browses Bonjour, with the right port already attached.
    if [ -d /etc/avahi/services ]; then
      local want
      want=$(cat <<SERVICE
<?xml version="1.0" standalone='no'?><!--*-nxml-*-->
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- Written by tools/install.sh -->
<service-group>
  <name replace-wildcards="yes">Riparr on %h</name>
  <service>
    <type>_http._tcp</type>
    <port>$PORT</port>
    <txt-record>path=/</txt-record>
  </service>
</service-group>
SERVICE
)
      if [ "$(cat /etc/avahi/services/riparr.service 2>/dev/null)" != "$want" ]; then
        printf '%s\n' "$want" > /etc/avahi/services/riparr.service
        systemctl reload avahi-daemon >/dev/null 2>&1 || \
          systemctl restart avahi-daemon >/dev/null 2>&1 || true
        changed "/etc/avahi/services/riparr.service: advertised as \"Riparr on $(hostname)\" over Bonjour"
      fi
      ok "advertised as \"Riparr on $(hostname)\" over Bonjour"
    fi
  else
    info "mDNS left alone (--no-mdns, or host mode): reach the box by its address or your own DNS"
  fi
  ok "dependencies present"

  # ── 2. accounts ──
  say "2/6  Accounts"
  if ! id -u "$RIPARR_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /var/lib/"$RIPARR_USER" \
            --shell /usr/sbin/nologin "$RIPARR_USER"
    changed "account $RIPARR_USER created"
  fi
  # The optical drive is root:cdrom, vcgencmd (temperature, throttling) needs video, and
  # the status LED writes to /dev/spidev* which is root:spi. Each group is added only if
  # it exists: `spi` is absent until SPI is enabled in the device tree, and a box with no
  # LED must still install cleanly.
  # netdev is new: wpa_supplicant's control socket is GROUP=netdev, and without it the
  # service cannot scan for networks or ask the supplicant to re-read its config. It
  # still cannot *write* the config -- that goes through the root bridge below.
  local g
  for g in cdrom video spi gpio netdev; do
    getent group "$g" >/dev/null && usermod -aG "$g" "$RIPARR_USER"
  done
  install -d -o "$RIPARR_USER" -g "$RIPARR_USER" "$DATA_DIR"
  # The dedicated staging partition (D4) does not exist on a stock image. Create
  # the directory anyway: the service declares it writable, and the status page reports
  # free space from it. Falls back to a plain directory on the root filesystem.
  install -d -o "$RIPARR_USER" -g "$RIPARR_USER" -m 0775 /srv/staging
  ok "user '$RIPARR_USER' ready; staging at /srv/staging"

  # ── 3. source ──
  say "3/6  Riparr"
  local UPGRADE=no
  { [ -e "$INSTALL_DIR/current" ] || [ -d "$INSTALL_DIR/server" ]; } && UPGRADE=yes

  # A checkout sitting next to this script wins over the network. Someone who copied a
  # working tree to the device meant to install *that* -- and the verified install path
  # in the README is exactly that: download the release archive, check it, unpack it,
  # run this script from inside it.
  local SRC="" CLONE=""
  if [ -f "$(dirname "$0")/../server/riparr/main.py" ]; then
    SRC="$(cd "$(dirname "$0")/.." && pwd)"
  fi
  if [ -z "$SRC" ]; then
    # No tree beside the script: fetch the branch. This is the unverified developer
    # path -- git over HTTPS, no checksum, no attestation -- and it says so. The README's
    # install path never comes through here.
    info "no checkout beside this script; cloning $REPO_URL ($BRANCH) -- unverified"
    CLONE=$(mktemp -d)
    git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$CLONE/riparr" \
      || die "Could not download Riparr from $REPO_URL. Check the network and try again."
    SRC="$CLONE/riparr"
  fi
  local VERSION
  VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/server/riparr/__init__.py" 2>/dev/null || echo "")
  [ -n "$VERSION" ] || die "Could not read the version from $SRC/server/riparr/__init__.py"
  info "installing Riparr $VERSION from $SRC"

  # MakeMKV and libdvdcss are compiled by this account, never by root. The one
  # definition of it is in packaging/staged-install.sh; the installers call the same
  # function, for boxes that predate it.
  if ! id -u "$BUILD_USER" >/dev/null 2>&1; then
    # shellcheck source=packaging/staged-install.sh
    . "$SRC/packaging/staged-install.sh"
    ensure_build_user
    changed "account $BUILD_USER created (compiles MakeMKV and libdvdcss unprivileged)"
  fi

  # ── 4. the release tree and its python environment ──
  say "4/6  Python environment"
  # /opt/riparr belongs to root. The tree goes into /opt/riparr/releases/<version>, a
  # root-owned virtualenv is built inside it with every package checked against its
  # pinned hash, and /opt/riparr/current is pointed at it. The service account can read
  # the code it runs and cannot write a byte of it; the in-app updater asks root
  # (riparr-update.sh) to do the same for each release it downloads and verifies. See
  # packaging/riparr-update.sh and docs/security.md.
  #
  # The two helpers that do this are installed first, from the tree being installed:
  # whoever runs `sudo bash tools/install.sh` has already decided to run this tree as
  # root. Every other helper and unit is installed by apply-system.sh, which refuses
  # anything in the tree that is not root-owned by then.
  install -d -o root -g root -m 0755 /usr/local/lib/riparr
  install -o root -g root -m 0755 "$SRC/packaging/riparr-update.sh" /usr/local/lib/riparr/riparr-update.sh
  install -o root -g root -m 0755 "$SRC/packaging/apply-system.sh" /usr/local/lib/riparr/apply-system.sh
  info "installing dependencies (a few minutes on a Zero 2 W)"
  RIPARR_INSTALL_DIR="$INSTALL_DIR" /usr/local/lib/riparr/riparr-update.sh \
      --from-tree "$SRC" --version "$VERSION" --no-restart \
    || die "Riparr could not be installed. The message above says why; re-run to try again."
  [ -n "$CLONE" ] && rm -rf "$CLONE"
  ok "Riparr $VERSION in $INSTALL_DIR/releases/$VERSION (root-owned; current -> it)"

  # ── 5. service ──
  say "5/6  Service"
  # Everything that lives outside /opt/riparr -- units, and the root-side helper scripts
  # the one-way doors call -- was installed by apply-system.sh as the last step of the
  # release install above. One definition, two callers: this script, and the update
  # door after an in-app update. See packaging/apply-system.sh.
  #
  # The scripts deliberately go to /usr/local/lib/riparr, NOT /opt/riparr, and
  # apply-system.sh refuses any that are not root-owned and closed to other writers.

  # ── a dead host must not cost two minutes ──
  # MakeMKV contacts its own server on every invocation. That server has been returning
  # 525, and the IP it dials black-holes, so connect() sat in SYN-SENT through all six
  # default SYN retries -- about 130 seconds -- before MakeMKV would look at the disc.
  # A rip paid it twice, and it read as "reading the disc is slow". Measured on the
  # reference board: 130s before, 7.2s after.
  #
  # System-wide, which is why it is an appliance-mode thing: on a host that does other
  # work, two SYN retries is somebody else's decision.
  if [ "$DO_SYSCTL" = yes ]; then
    if [ ! -f /etc/sysctl.d/60-riparr-fastfail.conf ]; then
      install -d -m 0755 /etc/sysctl.d
      cat > /etc/sysctl.d/60-riparr-fastfail.conf <<'SYSCTL'
# Written by Riparr's installer. 2 retries is about 7 seconds: long enough to ride out
# a blip on a LAN, short enough that an unreachable host is an inconvenience, not a hang.
net.ipv4.tcp_syn_retries = 2
SYSCTL
      changed "/etc/sysctl.d/60-riparr-fastfail.conf: net.ipv4.tcp_syn_retries=2 (system-wide)"
    fi
    sysctl -q -p /etc/sysctl.d/60-riparr-fastfail.conf 2>/dev/null || true
  else
    info "sysctl left alone (--no-sysctl, or host mode): a dead MakeMKV server costs a rip about two minutes"
  fi

  # Belt and braces: ask MakeMKV not to make the call at all. This is also the file its
  # registration key belongs in, which is why it is created here and not left to chance.
  install -d -o "$RIPARR_USER" -g "$RIPARR_USER" -m 0755 "$DATA_DIR/.MakeMKV"
  if [ ! -f "$DATA_DIR/.MakeMKV/settings.conf" ]; then
    cat > "$DATA_DIR/.MakeMKV/settings.conf" <<'MKSET'
#
# Written by Riparr's installer.
#
app_UpdateEnabled = "0"
MKSET
    chown "$RIPARR_USER":"$RIPARR_USER" "$DATA_DIR/.MakeMKV/settings.conf"
  fi

  # ── the two switches a host gets turned off ──
  # Both are read by the units themselves (ExecCondition= on the power units, the
  # environment of the watchdog), so the service cannot talk its way round them.
  if [ "$DO_POWER" = no ]; then
    set_default_kv RIPARR_ALLOW_POWER 0
    info "restart and shut down from the web page: off"
  else
    unset_default_kv RIPARR_ALLOW_POWER
  fi
  if [ "$DO_NETWATCH_REBOOT" = no ]; then
    set_default_kv RIPARR_NETWATCH_REBOOT 0
    info "the Wi-Fi watchdog may re-associate and reload the driver, never reboot"
  else
    unset_default_kv RIPARR_NETWATCH_REBOOT
  fi

  # apply-system.sh already did daemon-reload and enabled every path unit and the
  # watchdog. What is left is the web service itself, which it deliberately does not
  # start -- an install has more to do first (see above), and a restart belongs at the
  # end of that, not in the middle of it.
  ok "MakeMKV, updates, Wi-Fi and the USB-C fix all work from the web interface"
  [ "$DO_POWER" = yes ] && ok "restart and shut down work from the web interface"
  systemctl enable --quiet "$SERVICE"
  # The data directory belongs to the service account, all of it. A root helper that
  # opened the database before the service first ran used to leave an empty root-owned
  # riparr.db behind, and the service died on its first write ("attempt to write a
  # readonly database"). Those helpers open it read-only now; this repairs a box that was
  # already caught, and costs nothing on one that was not.
  chown -R "$RIPARR_USER":"$RIPARR_USER" "$DATA_DIR"
  systemctl restart "$SERVICE"
  ok "riparr.service enabled and started"

  # ── 6. confirm it is actually answering ──
  say "6/6  Checking it works"
  local HOST UP=no
  HOST=$(hostname)
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/setup/state" >/dev/null 2>&1; then
      UP=yes; break
    fi
    sleep 1
  done

  if [ "$UP" != yes ]; then
    printf '\n\033[31m✗ Riparr installed but is not answering on port %s.\033[0m\n\n' "$PORT"
    echo "  What it says for itself:"
    journalctl -u "$SERVICE" -n 25 --no-pager | sed 's/^/    /'
    echo
    echo "  Once fixed:  sudo systemctl restart $SERVICE"
    exit 1
  fi

  printf '\n\033[32m✓ Riparr is running.\033[0m\n\n'
  if [ "$UPGRADE" = yes ]; then
    echo "  Upgraded to $VERSION. Your settings and history were kept."
  else
    echo "  Open it and finish setup:"
  fi
  if [ "$DO_MDNS" = yes ]; then
    printf '\n      \033[1mhttp://%s.local:%s\033[0m\n\n' "$HOST" "$PORT"
    echo "  If that name doesn't resolve, use the address instead:"
  fi
  printf '      http://%s:%s\n\n' "$(hostname -I | awk '{print $1}')" "$PORT"

  if [ ${#CHANGES[@]} -gt 0 ]; then
    echo "  Changed outside Riparr's own directories this run (tools/uninstall.sh takes each back):"
    local c
    for c in "${CHANGES[@]}"; do echo "    ~ $c"; done
    echo
  fi
}

if [ "${RIPARR_INSTALL_LIB:-0}" != 1 ]; then
  main "$@"
fi
