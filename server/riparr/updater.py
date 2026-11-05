"""
Updates, against the official repository.

An appliance with no screen must be able to update itself without the user finding a
terminal, and must never leave itself unbootable if the download is bad. This side
checks GitHub for a release and asks for it by tag; the root side
(packaging/riparr-update.sh) downloads, verifies, unpacks, swaps and restarts, and the
running version is never touched until the new one is in place and checked.
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

from . import __version__
from . import platform as P

REPO = "jackharvest/riparr"
GITHUB_API = "https://api.github.com"
INSTALL_DIR = os.environ.get("RIPARR_INSTALL_DIR", "/opt/riparr")


def current_version():
    return __version__


def check(repo=REPO, timeout=8):
    """Never raises. A box with no internet still has to run."""
    url = "%s/repos/%s/releases/latest" % (GITHUB_API, repo)
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "riparr/%s" % __version__,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "no-releases", "current": __version__, "repo": repo,
                    "message": "No published releases yet."}
        return {"status": "error", "current": __version__, "repo": repo,
                "message": "GitHub returned HTTP %s" % e.code}
    except Exception as e:
        return {"status": "offline", "current": __version__, "repo": repo,
                "message": "Could not reach GitHub: %s" % e}

    tag_name = data.get("tag_name") or ""
    tag = tag_name.lstrip("v")
    assets = data.get("assets") or []
    payload = _pick_asset(assets)
    # The tag is the release's name, not this half's version. The web interface and the
    # Preparer move independently, so a release that only changes the Preparer must not
    # tell every box it is out of date -- and the other way round.
    latest = _component_version(assets, "appliance", tag)
    return {
        "status": "update" if _newer(latest, __version__) else "current",
        "current": __version__,
        "latest": latest,
        "tag": tag,
        # The tag as GitHub spells it, because it is what the root side builds the
        # download URL from -- `tag` above has the "v" stripped for display.
        "tag_name": tag_name,
        "repo": repo,
        "notes": data.get("body") or "",
        "published": data.get("published_at"),
        "url": data.get("html_url"),
        "asset": payload,
        # The checksums file, located by asset URL rather than by building a path out of
        # a version. `latest` is this half's version and stopped being the tag when the
        # two halves began versioning apart, so a constructed path now points at the
        # wrong release -- or at no release at all.
        "sums_url": _sums_url(assets),
        "can_install": bool(payload) and P.IS_APPLIANCE,
        "message": ("Version %s is available." % latest) if _newer(latest, __version__)
                   else "Riparr is up to date.",
    }


# The appliance payload, and nothing else. The same release also carries the Preparer
# for three desktop operating systems, and one of those is a .tar.gz whose name starts
# with "riparr-" -- a looser match picks up `riparr-preparer-linux-beta.tar.gz` and
# unpacks a desktop GUI app over /opt/riparr, which leaves a box that cannot start and
# has no screen to say so.
SERVER_ASSET = "riparr-server.tar.gz"



def _pick_asset(assets):
    for a in assets:
        if (a.get("name") or "").lower() == SERVER_ASSET:
            return {"name": a["name"], "url": a["browser_download_url"],
                    "size": a.get("size")}
    return None


# How long the request that started an update waits for the root side to finish
# before answering "still running". A download plus a pip install on a Zero 2 W is
# minutes, not seconds; the state file keeps saying what is happening either way.
INSTALL_TIMEOUT = 25 * 60


def install(repo=REPO, wait=True, timeout=INSTALL_TIMEOUT, poll=1.0):
    """Ask the root side to install the latest release, and wait for its answer.

    Refuses to run anywhere but the appliance -- asking for /opt/riparr to be replaced
    on a development Mac would be a genuinely destructive surprise.

    This process used to do the download, the checksum, the swap and the pip install
    itself, as the service account, and then ask root to install units from the tree
    it had just written. That was the hole: a compromised service could write anything
    into packaging/ and have root install it. Now this side writes exactly one word --
    the release tag -- into its request file, and `packaging/riparr-update.sh` does the
    rest as root against a tree this account cannot write: fetch, verify the checksum
    and the build provenance, unpack, `pip install --require-hashes`, swap, apply the
    system units, restart. Progress comes back through a root-owned status file, which
    is what this polls.

    The answer is honest about what happened. "done" with a version means the new
    release is on disk and the service is about to restart. "error" carries the root
    side's reason. A timeout means the work is still going on and the page should
    look again, not that it failed.
    """
    if not P.IS_APPLIANCE:
        return {"ok": False,
                "message": "Updates install on the appliance only. "
                           "This process is running in development mode."}
    info = check(repo)
    if info["status"] != "update":
        return {"ok": False, "message": info.get("message", "Nothing to install.")}
    if not info.get("asset"):
        return {"ok": False,
                "message": "That release has no appliance archive, so there is nothing "
                           "to install. The download page has the desktop app only."}
    tag = info.get("tag_name") or ""
    if not P.UPDATE_TAG.fullmatch(tag):
        return {"ok": False,
                "message": "That release's tag (%r) is not a version, so it was not "
                           "requested. Nothing was changed." % tag}
    if not P.update_door_available():
        return {"ok": False,
                "message": "This box was installed before updates moved to the root "
                           "side, so the web page cannot install this one. Nothing was "
                           "changed.",
                "detail": "Once, over SSH, from a verified release archive (see the "
                          "README): sudo bash riparr/tools/install.sh. After that, "
                          "updates install from here again."}

    started = int(time.time())
    if not P.request_update(tag):
        return {"ok": False,
                "message": "Could not ask the system to install the update. Nothing "
                           "was changed."}
    if not wait:
        return {"ok": True, "pending": True, "version": info["latest"],
                "message": "Installing %s in the background." % info["latest"]}

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        st = P.update_state()
        if not st or (st.get("at") or 0) < started:
            continue                    # an older run's result, or nothing yet
        phase = st.get("phase")
        if phase == "done":
            return {"ok": True, "message": st.get("message") or
                    "Updated to %s. Riparr is restarting." % st.get("version", info["latest"]),
                    "version": st.get("version") or info["latest"],
                    "provenance": st.get("provenance"),
                    "restarted": "restart", "needs_restart": False, "provisioned": True}
        if phase == "error":
            return {"ok": False,
                    "message": st.get("message") or "The update failed. Nothing was changed.",
                    "detail": st.get("detail") or ""}
    return {"ok": False, "pending": True, "version": info["latest"],
            "message": "The update is still running. This page will show its progress; "
                       "nothing needs doing.",
            "state": P.update_state()}


def install_state():
    """The root side's latest word on an update, for the page to show."""
    return P.update_state()


# Both spellings, because the answer to "which case is it?" should not be a brick.
# The release publishes SHA256SUMS.txt; the original code asked for sha256sums.txt,
# got a 404, returned None -- and the caller read None as "nothing to compare against"
# and installed the download unverified. A checksum that silently stops being checked
# is worse than no checksum at all, so a missing file is now a refusal (see install).
_SUMS_NAMES = ("SHA256SUMS.txt", "sha256sums.txt")


def _sums_url(assets):
    """The release's own URL for its checksums file, if it publishes one."""
    for a in assets or []:
        if (a.get("name") or "") in _SUMS_NAMES and a.get("browser_download_url"):
            return a["browser_download_url"]
    return None


def _expected_hash(name, sums_url=None, repo=REPO, tag=""):
    """The published SHA-256 for `name`, or None if it cannot be established.

    Prefers the URL the release itself gives for its checksums file. The constructed
    path is kept only for releases whose asset list could not be read, and is built from
    the *tag* -- never from a component version, which is what broke this: on a release
    tagged v0.2.3 carrying appliance 0.2.2, asking for v0.2.2's checksums fetches a
    different release's numbers and compares them against this one's download.
    """
    if sums_url:
        urls = [sums_url]
    elif tag:
        urls = ["https://github.com/%s/releases/download/v%s/%s" % (repo, tag, s)
                for s in _SUMS_NAMES]
    else:
        urls = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "riparr"})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode()
        except Exception:
            continue
        for line in body.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].lstrip("*") == name:
                return parts[0]
    return None


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _component_version(assets, key, fallback):
    """This half's version in a release, from the `versions.json` asset.

    The appliance and the Preparer are released together and versioned apart: one tag,
    two version numbers, and each updater reads only its own. Releases published before
    versions.json existed do not carry it, so the tag remains the fallback and those keep
    behaving exactly as they did.

    Never raises. A release we cannot read the manifest of is treated as one that does
    not have it, which is the conservative answer: compare against the tag.
    """
    for a in assets or []:
        if a.get("name") != "versions.json":
            continue
        url = a.get("browser_download_url")
        if not url:
            break
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                v = json.load(r).get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        except Exception:
            pass
        break
    return fallback


def _newer(a, b):
    def parts(v):
        return [int(x) if x.isdigit() else 0
                for x in re.split(r"[.\-+]", v or "") if x != ""]
    pa, pb = parts(a), parts(b)
    n = max(len(pa), len(pb))
    return (pa + [0] * (n - len(pa))) > (pb + [0] * (n - len(pb)))
