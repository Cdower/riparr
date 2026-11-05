# Security: the threat model, the doors, and the invariant

Riparr is a web service that rips discs. It runs as an unprivileged account and asks
root for a short list of fixed things. This page is the record of what that account can
and cannot do, how each privileged action is reached, what is checked on the way, and
how to prove it on a board. It is written for whoever changes `packaging/` next.

## The invariant

> **A compromised `riparr` process cannot obtain root through any door, and root never
> executes, installs or enables content the `riparr` account could have written.**

Everything below exists to keep that sentence true. The tests that hold it are
`packaging/apply-system.test.sh`, `packaging/riparr-update.test.sh`,
`packaging/staged-install.test.sh`, `packaging/wifi-apply.test.sh`,
`packaging/netwatch.test.sh` and `tools/mount-library.test.sh`; CI runs all of them,
the first three as root.

## Threat model

**What we defend against.** Someone who has taken over the web service -- through a bug
in it, a stolen session, or a guessed password -- and can now run anything as the
`riparr` account: write any file it can write, create any request file, hold any
socket it is allowed. They must not get root, must not change the code root runs, and
must not make root write somewhere of their choosing. We also defend the path by which
code reaches the box: a release must be the bytes this repository's workflow built from
a commit in it, not something a hijacked account or a leaked token published.

**What we do not defend against.** Root on the box, the SD card in someone's hand, the
kernel, and a bug in MakeMKV's closed-source `makemkvcon` reading a hostile disc (it
runs as the service account inside the sandbox below, which is the most that can be
done with a binary nobody can read). The web interface is one login on a LAN; a
network where that is not enough is what
[hardened-deployment.md](hardened-deployment.md) is for.

## What the service account can write

- `/var/lib/riparr` -- its state: the database (0600), the MakeMKV settings, logs,
  backups.
- `/srv/staging` and the mounted library shares under `/srv` -- rips.
- `/run/riparr` -- its runtime directory, where it leaves requests for root.
- `/tmp` -- private to the unit.

And nothing else. `/opt/riparr` is root's, every unit and helper is root's, the boot
partition and `/etc/default/riparr` are root's. The unit's `ProtectSystem=strict` makes
the rest of the filesystem read-only to the process even where permissions would allow
a write.

## The one-way doors

The service creates a file in `/run/riparr`; a root-side `.path` unit notices it and
runs a root-side `.service` whose command line is fixed in the unit file and whose
script lives in `/usr/local/lib/riparr`, root-owned. The service cannot choose what
runs. With one exception (the update), nothing in the request file is read.

| Request file | Root runs | What it reads from the service | Guard |
|---|---|---|---|
| `makemkv.request` | `makemkv-run.sh` → `makemkv-install.sh` | nothing from the file; tarballs under `/var/lib/riparr/makemkv` only if their SHA-256 matches the pinned manifest, copied into a root-owned build directory before use | compiled and staged as `riparr-build`; root installs an allow-list of paths (`staged-install.sh`) |
| `dvdtools.request` | `dvdtools-install.sh` | nothing | libdvdcss built and staged as `riparr-build`, pinned hash; allow-listed install |
| `cdtools.request`, `cdtools-ffmpeg.request` | `cdtools-install.sh` | the existence of the ffmpeg file, nothing in it | apt only |
| `wifi.request` | `wifi-apply.sh` | the `wifi_networks` setting from the database | SSID must be ≤ 32 bytes with no control characters, PSK must be 64 hex digits; anything else is skipped by name |
| `remount.request` | `mount-library.sh` | the `shares` table | host, share, domain, user and password are matched against strict patterns; a row that could change a mount option or add a credentials line is skipped by name; the password goes in a root-only 0600 file, never on a command line |
| `usbhost.request` | `usbhost-fix.sh` | nothing | reads the device tree only |
| `reboot.request`, `poweroff.request` | `systemctl reboot` / `poweroff` | nothing | `ExecCondition=power-allowed.sh`: refused, and the request removed, when `RIPARR_ALLOW_POWER=0` |
| `restart.request` | `systemctl restart riparr` | nothing | |
| `provision.request` | `apply-system.sh` | nothing | installs only root-owned, unwritable regular files from the root-owned release tree; refuses the rest by name, exits non-zero |
| `update.request` | `riparr-update.sh` | **one line: a release tag** | must match `^v?\d{1,4}\.\d{1,4}\.\d{1,4}$`; the file is removed before it is parsed; see below |

The other direction -- root telling the service something -- goes through
`/run/riparr-status`, which is root's. A root helper that wrote into `/run/riparr`
could be made to write through a symlink the service planted there, into any file on
the box; so none does. The one root process that must leave something in `/run/riparr`
(the power button handler, whose request the service deletes to acknowledge) creates
it with `O_EXCL | O_NOFOLLOW` and never writes through an existing name.

Root helpers run the system `python3`, never the service's virtualenv, and none of them
`source` or `eval` anything that passed through a service-writable file: the watchdog
reads its thresholds as `NAME=digits` lines and discards anything else.

## What an update verifies

The service's whole say in an update is the tag. Root then:

1. fetches `riparr-server.tar.gz` and `SHA256SUMS.txt` from the release;
2. checks the archive's SHA-256 against the published one -- **integrity**: the bytes
   are the ones the release published;
3. checks the build attestation -- **provenance**: the release was built by this
   repository's own workflow from a commit in it. How far that goes depends on the box:

   | Level | When | What it proves |
   |---|---|---|
   | `verified` | `gh` or sigstore-python is installed | the Sigstore signature and certificate chain check out against Sigstore's trust root; the claim is real |
   | `bound` | neither is installed (a stock appliance) | the bundle the release carries names exactly this archive's SHA-256 as the subject of a SLSA provenance statement from this repository; the signature was not checked, so this proves the publisher attached an attestation for these bytes, not who signed it |
   | `none` | the release carries no bundle (every release before 0.8) | nothing beyond the checksum |

   A bundle that is present and fails to verify is a refusal, never a downgrade.
   `RIPARR_UPDATE_REQUIRE_PROVENANCE=1` refuses anything short of `verified`; it is off
   by default because a stock appliance cannot reach it;
4. unpacks into `/opt/riparr/releases/<tag>`, refusing any member whose path, link
   target or type would land outside;
5. builds a root-owned virtualenv there with `pip install --require-hashes`;
6. points `/opt/riparr/current` at it with a rename, keeps the previous release for
   `riparr-update.sh --rollback`, runs `apply-system.sh` from the verified tree, and
   restarts the service.

Failure anywhere before step 6 leaves the running release untouched. The level reached
is recorded in `/run/riparr-status/update.state` and shown on the Updates page.

`tools/install.sh` from a verified archive (the README's four lines) is the same path
without the download: the tree beside the script is copied into a release directory,
owned by root, and gets the same virtualenv build. Running it from a git checkout is
unverified and says so.

## The sandbox

`packaging/riparr.service` scores **1.7 ("OK")** under `systemd-analyze security`; it
scored 8.4 ("EXPOSED") before. `packaging/security-thresholds.txt` holds every unit's
ceiling and `packaging/security-score.sh` fails CI if any unit goes above it.

What is open, and why, is written in the unit beside each directive. In short: the
filesystem is read-only except the state, staging and `/srv` paths; devices are closed
except the optical drive (`block-sr`), SCSI generic (`char-sg`), the SPI LED
(`char-spidev`) and the Pi's firmware channel (`char-vchiq`); kernel tunables, modules,
logs, cgroups, the clock and the hostname are protected; other users' processes are
invisible; namespaces, realtime and setuid are refused; the capability set is empty;
system calls are limited to `@system-service` (refused ones get `EPERM`, not a kill)
and socket families to Unix, IPv4, IPv6 and netlink (`iw` and `ip` need netlink).

The root-side helpers are deliberately **not** sandboxed, and their ceilings pin them
where they are: each one mounts, reboots, writes `/etc` or `/sys`, or runs apt, and a
namespaced unit would do those things privately and invisibly -- the failure already
recorded in JOURNAL.md for the mount units.

### How the sandbox was checked, and what is still owed

- The unit parses and scores under `systemd-analyze security --offline`.
- A `strace -f` of the service and its children through the whole test suite (a real
  uvicorn, mock rips, backups, music, the status page) used 83 distinct system calls,
  every one inside `@system-service`.
- The device, path and address-family needs were taken from the code: every `/dev`,
  `/sys`, `/proc` and socket use was listed.
- The mock interface was driven in headless Chromium under the new headers: zero
  console errors or policy violations.

It has **not yet been run end to end on a board with a real drive** under these
settings. That is the one check this document cannot make from a container, and it is
owed before the next release. To make it:

```bash
# on a box with the new unit installed and a disc (or a loop-mounted ISO) in the drive
sudo systemctl restart riparr
journalctl -u riparr -f                  # watch for EPERM, "Permission denied", "Operation not permitted"
# then from the web page: identify the disc, rip it, eject it, test the LED, check the
# temperature on the status page, remount a share, install the music tools
```

A loop-mounted ISO is a block device of class `loop`, not `sr`; for that stand-in add
`DeviceAllow=block-loop r` in a drop-in. If anything is refused,
`packaging/examples/sandbox-off.conf` opens everything back up in one file so the
culprit can be found one directive at a time, and the unit's comment for that directive
is where the answer goes.

## The web interface

One login; sessions are signed cookies (`HttpOnly`, `SameSite=Lax`, `Secure` behind
TLS) and a password change revokes them all. State-changing requests must come from the
page itself (origin check and a custom header) or carry the bearer token; every response
carries a self-only Content-Security-Policy and the standard hardening headers; request
bodies are capped. `RIPARR_AUTH_TOKEN` adds a second factor the whole API needs.
`riparr/web.py` has the details and `server/web-edge.test.py` the proof.

## Supply chain, in one place

- `server/uv.lock` is the source of truth; `server/requirements.txt` is generated from
  it with a hash for every package, and every install path uses
  `pip install --require-hashes`.
- Releases carry a Sigstore build attestation; the workflow verifies it before
  publishing.
- Every GitHub Action is pinned to a commit SHA; Dependabot keeps the pins and the lock
  files current.
- MakeMKV's tarballs are pinned by SHA-256 in `packaging/makemkv-manifest.json`, which
  records where each hash came from. GuinpinSoft publishes no checksums; `makemkvcon`
  is closed-source and installed as it came; its free beta key expires at the end of
  every month and Riparr renews it from the forum.
- libdvdcss is pinned by SHA-256 against VideoLAN's published hash.
- CI runs shellcheck on every script and `pip-audit` on the lock.

## Known gaps

- **The password-reset file.** `riparr-reset` on the boot partition is honoured by the
  service, which must delete it to consume it -- and the boot partition is read-only to
  the service (it was under the old `ProtectSystem=full` too). Making it writable would
  hand a compromised service the file every root unit reads its configuration from, so
  the right fix is a root-side door that consumes the file; it is not in this change.
- **Provenance on a stock appliance stops at `bound`.** A full Sigstore verification
  needs a verifier the stock image does not carry.
- **The first update to this version goes through the old door.** A box that updated
  from the web page before 0.8 adopts the tree it is already running as release one,
  out loud; everything after that is verified.
