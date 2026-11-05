#!/bin/bash
# Shared by makemkv-install.sh and dvdtools-install.sh: build as an unprivileged
# account, install as root, and only what was expected.
#
# Why, 2026-11-05: both installers used to run `./configure && make && make install`
# as root, inside a directory that could be the service account's (makemkv-run.sh
# offered /var/lib/riparr/makemkv as a source, and the installer preferred an
# already-extracted makemkv-oss-* directory there to the checksummed tarball beside
# it). A `configure` the service had edited would have run as root. So:
#
#   * the tarballs are checked against their pinned hashes and copied into a build
#     directory that is root's; nothing is ever built where the service can write
#   * the build itself -- configure, make, ninja -- runs as `riparr-build`, a system
#     account with no home, no shell and no files of its own
#   * `make install` runs as that account too, into a staging directory (DESTDIR)
#   * root then copies out of the staging directory only the files it expected, by
#     an allow-list of paths, and refuses the rest by name. A build that had been
#     subverted could put a file anywhere under the staging directory; it cannot make
#     root install it anywhere but the handful of places MakeMKV and libdvdcss live.
#
# Sourced, not run. apply-system.sh installs it beside the helpers.

BUILD_USER="${RIPARR_BUILD_USER:-riparr-build}"

# The account exists after this, on any box, so a web-page install on a box that
# predates it still builds unprivileged.
ensure_build_user() {
  id -u "$BUILD_USER" >/dev/null 2>&1 && return 0
  useradd --system --no-create-home --home-dir /nonexistent \
          --shell /usr/sbin/nologin "$BUILD_USER"
}

# Run a command as the build user, in a directory it owns. HOME points at the build
# directory because the GNU toolchain and meson both want somewhere to write.
as_build_user() {   # dir, command...
  local dir="$1"; shift
  runuser -u "$BUILD_USER" -- env HOME="$dir" PATH="/usr/local/bin:/usr/bin:/bin" \
      bash -c 'cd "$1" && shift && exec "$@"' -- "$dir" "$@"
}

# Copy the allowed artefacts out of a DESTDIR tree into the running system.
#
# Arguments: the staging directory, then one or more allow patterns -- shell globs
# against the path *relative to the staging directory*, like usr/bin/makemkvcon or
# 'usr/lib/libmakemkv.so*'. A regular file that matches is installed root:root with
# its own execute bit; a symlink that matches is recreated only if it points at a
# plain name in the same directory (how libfoo.so -> libfoo.so.1 is spelled). Anything
# else -- an unmatched path, a link elsewhere, a directory with a setuid file, a
# hard link, a device -- is refused and named. Returns non-zero if anything was
# refused, and installs nothing at all if a single path would escape.
install_staged() {   # stage, pattern...
  local stage="$1"; shift
  local patterns=("$@") rel f refused=0 n=0
  [ -d "$stage" ] || { echo "install_staged: no staging directory at $stage" >&2; return 1; }
  while IFS= read -r -d '' f; do
    rel="${f#"$stage"/}"
    case "$rel" in
      *..*) echo "install_staged: refusing $rel" >&2; return 1 ;;
    esac
    local ok=0 p
    for p in "${patterns[@]}"; do
      # shellcheck disable=SC2254
      case "$rel" in $p) ok=1; break ;; esac
    done
    if [ "$ok" != 1 ]; then
      echo "install_staged: not installing $rel (not on the allow-list)" >&2
      refused=1; continue
    fi
    if [ -L "$f" ]; then
      local target
      target=$(readlink "$f")
      case "$target" in
        */*|''|.|..) echo "install_staged: not installing link $rel -> $target" >&2; refused=1; continue ;;
      esac
      install -d -o root -g root -m 0755 "/$(dirname "$rel")"
      ln -sfn "$target" "/$rel"
      n=$((n + 1))
    elif [ -f "$f" ]; then
      local mode=0644
      [ -x "$f" ] && mode=0755
      install -d -o root -g root -m 0755 "/$(dirname "$rel")"
      install -o root -g root -m "$mode" "$f" "/$rel"
      n=$((n + 1))
    else
      echo "install_staged: not installing $rel (not a regular file)" >&2
      refused=1
    fi
  done < <(find "$stage" -mindepth 1 \( -type f -o -type l \) -print0 | sort -z)
  echo "install_staged: installed $n file(s) from $stage"
  [ "$refused" = 0 ]
}
