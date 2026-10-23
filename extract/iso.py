"""Minimal ISO9660 walker for the PSU discs.

list shows the ISO9660 records as they are (some archivers drop files on PS2 discs). Skip the big archives
with --skip.

Usage:
  python iso.py list <disc.iso>
  python iso.py x    <disc.iso> <outdir> [--skip CDPSU0.AFS,CDPSU1.AFS,PSUROFS.CVM]
"""
import os
import struct
import sys

SECTOR = 2048


def walk(f, lba, size, path=""):
    f.seek(lba * SECTOR)
    d = f.read(size)
    i = 0
    while i < len(d):
        n = d[i]
        if n == 0:
            i = (i // SECTOR + 1) * SECTOR
            continue
        r = d[i:i + n]
        i += n
        ext, = struct.unpack("<I", r[2:6])
        sz, = struct.unpack("<I", r[10:14])
        flags, nl = r[25], r[32]
        name = r[33:33 + nl]
        if name in (b"\0", b"\1"):
            continue
        name = name.decode("latin1").split(";")[0]
        full = f"{path}/{name}"
        is_dir = bool(flags & 2)
        yield full, ext, sz, is_dir
        if is_dir:
            yield from walk(f, ext, sz, full)


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    cmd, src = sys.argv[1], sys.argv[2]
    skip = sys.argv[sys.argv.index("--skip") + 1].split(",") if "--skip" in sys.argv else []
    with open(src, "rb") as f:
        f.seek(16 * SECTOR)
        root = f.read(SECTOR)[156:190]
        rlba, = struct.unpack("<I", root[2:6])
        rsz, = struct.unpack("<I", root[10:14])
        for full, lba, sz, is_dir in list(walk(f, rlba, rsz)):
            if cmd == "list":
                print(f"{'D' if is_dir else ' '} lba={lba:7d} size={sz:11d} {full}")
                continue
            if is_dir or any(full.lstrip("/").startswith(s) for s in skip):
                continue
            out = os.path.join(sys.argv[3], *full.lstrip("/").split("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            f.seek(lba * SECTOR)
            with open(out, "wb") as fh:
                left = sz
                while left:
                    chunk = f.read(min(left, 1 << 20))
                    fh.write(chunk)
                    left -= len(chunk)


if __name__ == "__main__":
    main()
