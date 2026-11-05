"""Unpack a tar archive into a directory, refusing anything that would land outside it.

Shared by riparr-update.sh (a release archive) and makemkv-install.sh (MakeMKV's
tarballs): both are remote input, and the two used to carry their own copies of this
walk, which is how a fix to one would have missed the other. Run as

    python3 safe-untar.py ARCHIVE DEST

Refused, with a non-zero exit and nothing extracted: a member whose path resolves
outside DEST, a symlink or hard link pointing outside it, a device node. On Python
3.12+ (and the 3.11 the box may have) the `data` filter enforces the same rules a
second time during extraction.
"""
import os
import sys
import tarfile


def main(archive, dest):
    base = os.path.realpath(dest)
    with tarfile.open(archive) as t:
        for m in t.getmembers():
            target = os.path.realpath(os.path.join(dest, m.name))
            if target != base and not target.startswith(base + os.sep):
                raise SystemExit("unsafe path in archive: %s" % m.name)
            if m.issym() or m.islnk():
                link = os.path.realpath(os.path.join(os.path.dirname(target), m.linkname))
                if not link.startswith(base + os.sep):
                    raise SystemExit("unsafe link in archive: %s -> %s" % (m.name, m.linkname))
            if m.isdev():
                raise SystemExit("device node in archive: %s" % m.name)
        if hasattr(tarfile, "data_filter"):
            t.extractall(dest, filter="data")
        else:
            t.extractall(dest)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: safe-untar.py ARCHIVE DEST")
    main(sys.argv[1], sys.argv[2])
