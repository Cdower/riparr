#!/bin/bash
# What apply-system.sh refuses, and what it does to a box that predates the root-owned
# release layout.
#
# The invariant under test: root never executes, installs or enables content the
# service account can write. Every unit and helper it installs has to be a regular
# file owned by root with no group or other write bit, in a directory that is the same;
# anything else is refused by name, the rest still goes in, and the script exits
# non-zero so a provisioning unit records the refusal.
#
# Needs root, because the refusals are about file ownership and only root can give a
# file to another account. Everything else -- systemctl, mountpoint, ldconfig -- is a
# stub on PATH, and every path the script writes is redirected into a temp directory.
#
# Run: sudo bash packaging/apply-system.test.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)

if [ "$(id -u)" != 0 ]; then
  echo "apply-system.test.sh needs root (it gives files to another account); run it with sudo"
  exit 0
fi

T=$(mktemp -d "${TMPDIR:-/tmp}/riparr-apply-test.XXXXXX") && [ -n "$T" ] || { echo "mktemp failed"; exit 1; }
trap 'rm -rf "$T"' EXIT
FAILED=0
check() {  # name, got, want
  if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', wanted '$3'"; FAILED=1; fi
}

# An unprivileged account to own the hostile files. nobody exists everywhere.
OTHER=nobody

BIN="$T/bin"; mkdir -p "$BIN"
CALLS="$T/systemctl.calls"; : > "$CALLS"
cat > "$BIN/systemctl" <<EOF
#!/bin/sh
echo "\$*" >> "$CALLS"
case "\$1" in
  cat) exit 1 ;;            # no aw859a unit here
  is-active) exit 1 ;;
esac
exit 0
EOF
printf '#!/bin/sh\nexit 1\n' > "$BIN/mountpoint"
printf '#!/bin/sh\nexit 0\n' > "$BIN/ldconfig"
printf '#!/bin/sh\nexit 0\n' > "$BIN/rfkill"
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"

# A fresh fake box: the install directory, the unit directory, the helper directory,
# the runtime directories, and a sysroot for the handful of absolute paths that remain.
fresh() {
  rm -rf "$T/box"
  mkdir -p "$T/box/opt/riparr" "$T/box/units" "$T/box/lib" "$T/box/run" "$T/box/root"
  export RIPARR_INSTALL_DIR="$T/box/opt/riparr" RIPARR_UNIT_DIR="$T/box/units" \
         RIPARR_LIB_DIR="$T/box/lib" RIPARR_RUN_DIR="$T/box/run" \
         RIPARR_STATUS_DIR="$T/box/run-status" RIPARR_SYSROOT="$T/box/root"
}

# A release tree in the new layout: releases/<v> root-owned, current pointing at it,
# and the compatibility links the units and the documentation use.
layout() {  # version
  local root="$RIPARR_INSTALL_DIR" v="$1"
  mkdir -p "$root/releases/$v"
  cp -a "$REPO/packaging" "$root/releases/$v/packaging"
  mkdir -p "$root/releases/$v/tools" "$root/releases/$v/server/riparr"
  cp "$REPO/tools/makemkv-install.sh" "$REPO/tools/mount-library.sh" "$root/releases/$v/tools/"
  printf '__version__ = "%s"\n' "$v" > "$root/releases/$v/server/riparr/__init__.py"
  chown -R root:root "$root"; chmod -R go-w "$root"
  ln -sfn "releases/$v" "$root/current"
  for l in server packaging tools; do ln -sfn "current/$l" "$root/$l"; done
}

run_apply() {
  "$RIPARR_INSTALL_DIR/packaging/apply-system.sh" > "$T/out" 2>&1
  echo $?
}

echo "a clean root-owned tree installs everything"
fresh; layout 1.0.0
rc=$(run_apply)
check "exits 0" "$rc" "0"
check "the service unit is installed" "$(stat -c '%u %a' "$RIPARR_UNIT_DIR/riparr.service")" "0 644"
check "a helper is installed root 0755" "$(stat -c '%u %a' "$RIPARR_LIB_DIR/wifi-apply.sh")" "0 755"
check "the update helper is installed" "$(stat -c '%u %a' "$RIPARR_LIB_DIR/riparr-update.sh")" "0 755"
check "apply-system.sh refreshes its own root copy" "$(cmp -s "$RIPARR_LIB_DIR/apply-system.sh" "$REPO/packaging/apply-system.sh" && echo same)" "same"
check "the status directory exists, root 0755" "$(stat -c '%u %a' "$RIPARR_STATUS_DIR")" "0 755"
check "daemon-reload was asked for" "$(grep -c '^daemon-reload' "$CALLS")" "1"
check "the slice is installed" "$(stat -c '%u %a' "$RIPARR_UNIT_DIR/riparr.slice")" "0 644"

echo "a unit the service account could have written is refused, the rest still installs"
fresh; layout 1.0.0; : > "$CALLS"
chown "$OTHER" "$RIPARR_INSTALL_DIR/releases/1.0.0/packaging/riparr-reboot.service"
rc=$(run_apply)
check "exits non-zero" "$([ "$rc" != 0 ] && echo nonzero)" "nonzero"
check "the refused unit is not installed" "$([ -e "$RIPARR_UNIT_DIR/riparr-reboot.service" ] || echo absent)" "absent"
check "it is named in the output" "$(grep -ci "refused.*riparr-reboot.service" "$T/out")" "1"
check "the other units still went in" "$([ -f "$RIPARR_UNIT_DIR/riparr-poweroff.service" ] && echo yes)" "yes"

echo "a group-writable helper is refused"
fresh; layout 1.0.0
chmod g+w "$RIPARR_INSTALL_DIR/releases/1.0.0/packaging/netwatch.sh"
rc=$(run_apply)
check "exits non-zero" "$([ "$rc" != 0 ] && echo nonzero)" "nonzero"
check "netwatch.sh is not installed" "$([ -e "$RIPARR_LIB_DIR/netwatch.sh" ] || echo absent)" "absent"

echo "a world-writable helper is refused"
fresh; layout 1.0.0
chmod o+w "$RIPARR_INSTALL_DIR/releases/1.0.0/packaging/thermal.sh"
rc=$(run_apply)
check "thermal.sh is not installed" "$([ -e "$RIPARR_LIB_DIR/thermal.sh" ] || echo absent)" "absent"

echo "a symlink where a helper should be is refused, even if it points at root's file"
fresh; layout 1.0.0
rm "$RIPARR_INSTALL_DIR/releases/1.0.0/packaging/usbhost-fix.sh"
ln -s /etc/hostname "$RIPARR_INSTALL_DIR/releases/1.0.0/packaging/usbhost-fix.sh"
rc=$(run_apply)
check "usbhost-fix.sh is not installed" "$([ -e "$RIPARR_LIB_DIR/usbhost-fix.sh" ] || echo absent)" "absent"

echo "a packaging directory the service could write is refused as a whole"
fresh; layout 1.0.0
chown "$OTHER" "$RIPARR_INSTALL_DIR/releases/1.0.0/packaging"
rc=$(run_apply)
check "exits non-zero" "$([ "$rc" != 0 ] && echo nonzero)" "nonzero"
check "nothing was installed" "$(ls "$RIPARR_UNIT_DIR" | wc -l)" "0"

echo "a box on the old layout is moved onto the new one, once"
fresh
# The old layout: a real tree owned by the service account, venv and a leftover
# .previous from the in-process updater.
root="$RIPARR_INSTALL_DIR"
mkdir -p "$root/server/riparr" "$root/tools" "$root/.venv/bin" "$root/.previous/server"
cp -a "$REPO/packaging" "$root/packaging"
cp "$REPO/tools/makemkv-install.sh" "$REPO/tools/mount-library.sh" "$root/tools/"
printf '__version__ = "0.7.0"\n' > "$root/server/riparr/__init__.py"
printf '#!/bin/sh\n' > "$root/.venv/bin/python"; chmod +x "$root/.venv/bin/python"
chown -R "$OTHER" "$root"
rc=$(run_apply)
check "exits 0" "$rc" "0"
check "the tree now lives under releases/<version>" "$([ -f "$root/releases/0.7.0/server/riparr/__init__.py" ] && echo yes)" "yes"
check "current points at it" "$(readlink "$root/current")" "releases/0.7.0"
check "server is a compatibility link" "$(readlink "$root/server")" "current/server"
check "packaging is a compatibility link" "$(readlink "$root/packaging")" "current/packaging"
check "tools is a compatibility link" "$(readlink "$root/tools")" "current/tools"
check ".venv is a compatibility link" "$(readlink "$root/.venv")" "current/.venv"
check "the adopted venv came along" "$([ -x "$root/releases/0.7.0/.venv/bin/python" ] && echo yes)" "yes"
check "the install directory is root's" "$(stat -c '%u %a' "$root")" "0 755"
check "the release tree is root's" "$(stat -c '%u' "$root/releases/0.7.0/server/riparr/__init__.py")" "0"
check "nothing in it is writable by anyone else" "$(find "$root/releases" ! -type l -perm /022 | wc -l)" "0"
check ".previous is gone" "$([ -e "$root/.previous" ] || echo gone)" "gone"
check "and the units were installed from the adopted tree" "$([ -f "$RIPARR_UNIT_DIR/riparr.service" ] && echo yes)" "yes"
check "the adoption is said out loud" "$(grep -c 'adopt' "$T/out")" "1"
rc=$(run_apply)
check "running again is a no-op that still exits 0" "$rc" "0"
check "and does not adopt twice" "$(grep -c 'adopt' "$T/out")" "0"

if [ "$FAILED" = 0 ]; then echo "all ok"; else echo "FAILED"; exit 1; fi
