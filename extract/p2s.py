"""Read a PCSX2 savestate (.p2s): a zip whose members are stored, deflated or zstd (method 93).

Python's zipfile cannot open zstd members; this reads the raw member and decompresses it with the
`zstandard` module. Used to scan EE RAM offline (a PINE read is one word per request).

Usage:
  python p2s.py list <state.p2s>
  python p2s.py x    <state.p2s> <member> <out>          e.g. eeMemory.bin
  python p2s.py find <state.p2s> <erx> <va> [<va> ...]   where a module's .text bytes sit in EE RAM
"""
import struct
import sys
import zipfile
import zlib


def member(path, name):
    z = zipfile.ZipFile(path)
    info = z.getinfo(name)
    with open(path, "rb") as f:
        f.seek(info.header_offset)
        hdr = f.read(30)
        n, m = struct.unpack_from("<HH", hdr, 26)
        f.seek(info.header_offset + 30 + n + m)
        raw = f.read(info.compress_size)
    if info.compress_type == 0:
        return raw
    if info.compress_type == 8:
        return zlib.decompress(raw, -15)
    if info.compress_type == 93:
        import zstandard
        return zstandard.ZstdDecompressor().decompress(raw, max_output_size=info.file_size)
    raise SystemExit(f"unsupported compression {info.compress_type}")


def main():
    a = sys.argv[1:]
    if len(a) < 2:
        raise SystemExit(__doc__)
    if a[0] == "list":
        for i in zipfile.ZipFile(a[1]).infolist():
            print(f"{i.file_size:10d}  method {i.compress_type:3d}  {i.filename}")
    elif a[0] == "x":
        open(a[3], "wb").write(member(a[1], a[2]))
    elif a[0] == "find":
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import erx
        ee = member(a[1], "eeMemory.bin")
        im = erx.Image(a[2])
        t = im.text
        for v in a[3:]:
            va = int(v, 16)
            probe = im.d[t["off"] + va:t["off"] + va + 64]
            hits, i = [], ee.find(probe)
            while i != -1:
                hits.append(i)
                i = ee.find(probe, i + 1)
            print(f"va {va:#x}: hits {[hex(h) for h in hits]}  base {[hex(h - va) for h in hits]}")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
