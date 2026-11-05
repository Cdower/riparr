# Hardened deployment

The stock appliance is a box on your own Wi-Fi, reachable by name, with one login. For
most people that is the right amount of security: the threat is somebody on the sofa,
not somebody on the internet. This page is for the other cases -- a box on a network
you share with people you do not fully trust, a box you reach over a VPN, or Riparr
installed on a machine that does other things.

Everything here is a switch that is already there. Nothing needs patching, and the
stock appliance keeps working if you flip none of them.

## The recipe

1. **Listen on loopback, put something in front.** In `/etc/default/riparr` (or the
   card's `riparr.conf`):

   ```
   RIPARR_HOST=127.0.0.1
   ```

   then reach it through [Tailscale](https://tailscale.com/kb/1312/serve):

   ```bash
   sudo tailscale serve --bg 9797
   ```

   which gives you `https://<box>.<tailnet>.ts.net` with a real certificate, only on
   your tailnet. Or any reverse proxy you already run (Caddy, nginx, Traefik) with TLS;
   the service speaks plain HTTP on `127.0.0.1:9797` and needs nothing special from
   the proxy.

2. **Mark the cookies secure**, now that there is TLS in front:

   ```
   RIPARR_COOKIE_SECURE=1
   ```

3. **Require an access token** as well as the login. Pick a long random string:

   ```
   RIPARR_AUTH_TOKEN=$(openssl rand -hex 24)
   ```

   and put the result in the file. The sign-in form grows an "Access token" field; API
   clients send `Authorization: Bearer <token>`. Until the token has been presented,
   the service answers nothing but "there is a gate". A wrong token costs the same
   escalating delay as a wrong password.

4. **Turn the power controls off.** Nothing on a web page should be able to turn a
   shared machine off:

   ```
   RIPARR_ALLOW_POWER=0
   ```

   The page hides Restart and Shut down, the service refuses them, and the root-side
   units refuse them too (`packaging/power-allowed.sh` runs as `ExecCondition=`), so
   even a compromised service cannot reboot the host. The physical power button, if
   there is one, goes back to the system's own handling.

5. **Keep the watchdog from rebooting.** It still re-associates and reloads the Wi-Fi
   driver; it never restarts the machine:

   ```
   RIPARR_NETWATCH_REBOOT=0
   ```

   On a wired machine it does nothing at all unless `RIPARR_NETWATCH_IFACE` names an
   interface.

6. **Restart the units:**

   ```bash
   sudo systemctl restart riparr riparr-netwatch
   ```

`sudo bash tools/install.sh --host` does steps 4 and 5 for you (and skips the
system-wide sysctl and mDNS changes an appliance gets); steps 1 to 3 are a choice
about your network, so they stay yours.

## Optional extras

- **Verified provenance only.** With `gh` ([GitHub CLI](https://cli.github.com/)) or
  `sigstore` (Python) installed on the box, `RIPARR_UPDATE_REQUIRE_PROVENANCE=1` makes
  the updater refuse any release whose build attestation it cannot fully verify. See
  [what an update verifies](security.md#what-an-update-verifies).
- **The share password out of the database.** Give it to the mount unit as a systemd
  credential instead: `packaging/examples/library-credentials.conf`.
- **Resource limits.** Everything Riparr runs is in `riparr.slice`;
  `packaging/examples/riparr-slice-throttle.conf` demotes all of it at once.
- **A smaller body limit.** `RIPARR_MAX_BODY_BYTES` caps uploads (backups, settings
  imports); the default is 64 MiB.

## What you get, and what you do not

With all of the above, the web interface is reachable only through your proxy or
tailnet, every request needs the token and a login, the sign-in cookies are
`HttpOnly`, `SameSite=Lax` and `Secure`, state-changing requests are refused unless
they come from the page itself (origin check plus a custom header) or carry the bearer
token, the page cannot be framed or load scripts from anywhere else, and nothing on it
can power the machine or make it reboot.

What stays true regardless: the service runs unprivileged in a tight sandbox and asks
root for a handful of fixed actions through one-way doors
([security.md](security.md)); a compromised service cannot obtain root through any of
them. What this page does not do is protect against someone with root on the box, or
with the SD card in their hand -- that is the same as owning it.
