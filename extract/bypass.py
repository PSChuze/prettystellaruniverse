"""DNAS bypass builder for the US PSU discs: prepatched ISO, .xdelta, PCSX2 pnach, Cheat Device codes.

Everything comes from patches/bypass.json (read its "_" block first). Standard library only, plus
the xdelta3 binary for the `xdelta` step.

Usage:
  python bypass.py check  <game> <disc.iso>              verify the disc and every site, write nothing
  python bypass.py iso    <game> <disc.iso> <out.iso>    prepatched image (the source is only read)
  python bypass.py xdelta <game> <disc.iso> <out.iso> <out.xdelta>   encode, then decode and compare
  python bypass.py pnach  <game>                         patches/<serial>_<crc>.pnach
  python bypass.py cheats                                patches/cheats/PSU-Online.txt (games with a load_base)
  python bypass.py files  <game> <boot_elf> <PSU_US.GZ>  verify the pnach/codes against files copied off the disc
<game> is the serial (SLUS-21194) or the short name (PSU, AotI).

The disc edit is size-neutral: ERX/PSU_US.GZ is re-deflated to exactly its original length (level-9
deflate for most of the module, the tail as stored blocks, split so the total lands on the byte),
so the ISO9660 layout and every LBA the loader knows are untouched. The gzip header is kept byte
for byte and the trailer (CRC-32, ISIZE) is recomputed. Each output is re-read and re-inflated
before it is reported as built.
"""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import iso  # noqa: E402

PATCHES = os.path.join(HERE, "..", "patches")
MANIFEST = os.path.join(PATCHES, "bypass.json")
R_MIPS_26 = 4


def load():
    return json.load(open(MANIFEST, encoding="utf-8"))["games"]


def game(games, key):
    for serial, g in games.items():
        if key.upper() in (serial.upper(), g["short"].upper()):
            return serial, g
    sys.exit("unknown game %r (want one of %s)" % (key, ", ".join(games)))


def h(x):
    return int(x, 16)


# ---- the disc ------------------------------------------------------------------------------

def find(f, path):
    f.seek(16 * iso.SECTOR)
    root = f.read(iso.SECTOR)[156:190]
    rlba, = struct.unpack("<I", root[2:6])
    rsz, = struct.unpack("<I", root[10:14])
    want = "/" + path.upper()
    for full, lba, sz, is_dir in iso.walk(f, rlba, rsz):
        if full.upper() == want and not is_dir:
            return lba, sz
    sys.exit("%s is not on this disc" % path)


def sha1_file(path):
    s = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 24), b""):
            s.update(chunk)
    return s.hexdigest()


def check_source(g, src):
    size = os.path.getsize(src)
    if size != g["source"]["size"]:
        sys.exit("%s is %d bytes; the %s dump is %d. Wrong disc, or not a 2048-byte-sector .iso"
                 % (src, size, g["short"], g["source"]["size"]))
    if g["source"]["sha1"]:
        got = sha1_file(src)
        if got != g["source"]["sha1"]:
            sys.exit("%s sha1 %s, expected %s (the Redump dump). Not patching a different image"
                     % (src, got, g["source"]["sha1"]))


# ---- gzip / module -------------------------------------------------------------------------

def gz_split(gz):
    if gz[:3] != b"\x1f\x8b\x08":
        sys.exit("module is not gzip")
    flg, p = gz[3], 10
    if flg & 4:
        xl, = struct.unpack_from("<H", gz, p)
        p += 2 + xl
    for bit in (8, 16):
        if flg & bit:
            p = gz.index(b"\0", p) + 1
    if flg & 2:
        p += 2
    d = zlib.decompressobj(-15)
    raw = d.decompress(gz[p:])
    if not d.eof or len(d.unused_data) != 8:
        sys.exit("module gzip has trailing data; refusing to guess")
    crc, isize = struct.unpack("<II", d.unused_data)
    if crc != zlib.crc32(raw) or isize != len(raw) & 0xFFFFFFFF:
        sys.exit("module gzip trailer does not match its data")
    return gz[:p], raw


def sections(m):
    shoff, = struct.unpack_from("<I", m, 0x20)
    shes, shn, shstr = struct.unpack_from("<HHH", m, 0x2E)
    secs = [struct.unpack_from("<10I", m, shoff + i * shes) for i in range(shn)]
    so = secs[shstr][4]
    return {m[so + s[0]:m.index(b"\0", so + s[0])].decode(): s for s in secs}


def patch_module(raw, g):
    """Returns the patched module. Verifies every expected word and relocation first."""
    m = bytearray(raw)
    secs = sections(m)
    text, rel = secs[".text"], secs[".rel.text"]
    if text[3] != 0:
        sys.exit(".text is not based at 0; the va == file-address assumption is wrong")
    sites = {h(e["va"]) for e in g["edits"]}
    for e in g["edits"]:
        va = h(e["va"])
        got, = struct.unpack_from("<I", m, text[4] + va)
        if got != h(e["expect"]):
            sys.exit("%s va %s holds %08x, expected %s" % (g["short"], e["va"], got, e["expect"]))

    # The relocation table: drop the R_MIPS_26 on each site, keep order, pad with zero entries
    # at the end, exactly as the retail table already ends in (0, 0) entries.
    n = rel[5] // 8
    ents = [struct.unpack_from("<II", m, rel[4] + 8 * k) for k in range(n)]
    hits = [e for e in ents if e[0] in sites and e[1] & 0xFF]
    if sorted(e[0] for e in hits) != sorted(sites) or any(e[1] & 0xFF != R_MIPS_26 for e in hits):
        sys.exit("expected exactly one R_MIPS_26 per site, found %s" % [(hex(a), hex(b)) for a, b in hits])
    keep = [e for e in ents if e not in hits]
    keep += [(0, 0)] * (n - len(keep))
    for k, (off, info) in enumerate(keep):
        struct.pack_into("<II", m, rel[4] + 8 * k, off, info)

    for e in g["edits"]:
        struct.pack_into("<I", m, text[4] + h(e["va"]), h(e["replace"]))
    return bytes(m)


def stored(data, final):
    """Stored deflate blocks for data (byte-aligned start), the last one final if asked."""
    out, i = bytearray(), 0
    while True:
        chunk = data[i:i + 0xFFFF]
        i += len(chunk)
        last = i >= len(data)
        out += bytes([1 if (last and final) else 0]) + struct.pack("<HH", len(chunk), len(chunk) ^ 0xFFFF) + chunk
        if last:
            return bytes(out)


def deflate_exact(raw, target):
    """Raw deflate of raw that is exactly target bytes long, or exit."""
    full = zlib.compressobj(9, zlib.DEFLATED, -15, 9)
    whole = full.compress(raw) + full.flush()
    if len(whole) > target:
        sys.exit("the patched module deflates to %d bytes, more than the %d available" % (len(whole), target))
    # Compress a prefix once, then try split points after it: prefix deflated + tail stored.
    # Each byte moved to the stored tail costs about 0.7 bytes, so the split sits a few tens of
    # KB before the end; the extra stored-block headers (5 bytes each) absorb the remainder.
    gap = target - len(whole)
    back = min(len(raw), max(0x10000, gap * 4))
    s0 = len(raw) - back
    base = zlib.compressobj(9, zlib.DEFLATED, -15, 9)
    head = base.compress(raw[:s0])

    def prefix(s):
        c = base.copy()
        return head + c.compress(raw[s0:s]) + c.flush(zlib.Z_FULL_FLUSH)

    def slack(s):
        return target - len(prefix(s)) - len(stored(raw[s:], True))

    # slack grows with s (less stored, more deflated). Find the last split with slack < 0...
    lo, hi = s0, len(raw)
    if slack(lo) >= 0:
        sys.exit("the stored tail is not long enough to fill %d bytes; widen `back`" % gap)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if slack(mid) < 0 else (lo, mid)
    # ...then walk forward to a split whose slack is a few whole stored-block headers.
    for s in range(hi, min(len(raw), hi + 4096)):
        sl = slack(s)
        if sl < 0 or sl % 5 or sl > 5 * 16:
            continue
        # Absorb the slack by cutting the stored tail into more blocks (5 bytes each), never empty.
        t, n = raw[s:], sl // 5 + 1
        if len(t) < n:
            continue
        cut = [len(t) * k // n for k in range(n + 1)]
        body = prefix(s) + b"".join(stored(t[cut[k]:cut[k + 1]], k == n - 1) for k in range(n))
        if len(body) == target and zlib.decompress(body, -15) == raw:
            return body
    sys.exit("could not fit the patched module into %d bytes" % target)


def build_gz(g, gz):
    header, raw = gz_split(gz)
    patched = patch_module(raw, g)
    body = deflate_exact(patched, len(gz) - len(header) - 8)
    new = header + body + struct.pack("<II", zlib.crc32(patched), len(patched) & 0xFFFFFFFF)
    assert len(new) == len(gz)
    h2, back = gz_split(new)
    assert h2 == header and back == patched
    return new, raw, patched


# ---- runtime forms -------------------------------------------------------------------------

def runtime_words(g):
    """(address, low halfword of the relocated retail word, replacement) per site."""
    base = g["module"]["load_base"]
    if not base:
        return None
    base = h(base)
    out = []
    for e in g["edits"]:
        w = h(e["expect"])
        if w >> 26 != 3:
            sys.exit("site %s is not a jal; the relocated guard below assumes R_MIPS_26" % e["va"])
        target = ((w & 0x3FFFFFF) << 2) + base
        live = (3 << 26) | ((target >> 2) & 0x3FFFFFF)
        out.append((base + h(e["va"]), live & 0xFFFF, h(e["replace"]), e))
    return out


def pnach(serial, g):
    words = runtime_words(g)
    if words is None:
        sys.exit("%s: no load_base in the manifest yet (%s)" % (g["short"], g["module"]["load_base_note"]))
    lines = [
        "gametitle=%s [%s]" % (g["title"], serial),
        "comment=DNAS bypass (PSU project). Sony's DNAS servers are gone, so the check can never pass.",
        "",
        "// Generated by extract/bypass.py from patches/bypass.json. Do not hand-edit.",
        "// Unlabelled on purpose: a [Section] patch is a toggleable cheat that defaults to OFF.",
        "// Game module %s loads at %s. Every write is guarded by an E-type compare of the site's"
        % (g["module"]["file"], g["module"]["load_base"]),
        "// relocated retail low halfword, so nothing is written before the module is in memory or",
        "// over other code (a memory-card System File build), and once written the guard goes quiet.",
        "//",
    ]
    for addr, lo, new, e in words:
        lines.append("//   va %s  %s -> %s" % (e["va"], e["was"], e["now"]))
    lines.append("")
    for addr, lo, new, e in words:
        lines.append("patch=1,EE,E001%04x,extended,%08x" % (lo, addr))
        lines.append("patch=1,EE,%08x,extended,%08x" % (0x20000000 | addr, new))
    path = os.path.join(PATCHES, "%s_%s.pnach" % (serial, g["elf_crc"]))
    open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
    print("wrote", os.path.normpath(path))


def cheats(games):
    out = [
        "// Phantasy Star Universe (US discs): the DNAS bypass as Cheat Device PS2 codes.",
        "//",
        "// For the ORIGINAL disc on a FreeMcBoot console. If you load an image from USB, HDD or the",
        "// network, use the prepatched image instead; you need one or the other, never both.",
        "// Generated by extract/bypass.py from patches/bypass.json. Do not hand-edit.",
        "//",
        "// The Enable Code is a 9-type hook in libpad2's read routine in the boot ELF; the engine",
        "// checks that opcode before installing, so the codes cannot fire on another game. Each write",
        "// is guarded by an E-type compare of the site's original halfword, so nothing is written",
        "// until the game module is loaded, and a memory-card System File build is left alone.",
        "//",
        "// Getting past DNAS is all these do. The console still needs DNS that sends the four",
        "// segaonline.jp names to the server.",
    ]
    for serial, g in games.items():
        words = runtime_words(g)
        if words is None:
            print("skipping %s: no load_base yet" % g["short"])
            continue
        hook = g["hook"]
        out += ["", "// " + "=" * 88, '"%s [%s]"' % (g["title"], serial), "",
                "Enable Code (must be on)",
                "9%07X %08X" % (h(hook["va"]), h(hook["op"])), "",
                "DNAS bypass (required)"]
        for addr, lo, new, e in words:
            out.append("E001%04X %08X" % (lo, addr))
            out.append("%08X %08X" % (0x20000000 | addr, new))
    os.makedirs(os.path.join(PATCHES, "cheats"), exist_ok=True)
    path = os.path.join(PATCHES, "cheats", "PSU-Online.txt")
    open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
    print("wrote", os.path.normpath(path))


def check_hook(g, src):
    """The hook opcode must be in this disc's boot ELF at the hook address."""
    with open(src, "rb") as f:
        lba, sz = find(f, g["boot_elf"])
        f.seek(lba * iso.SECTOR)
        check_elf(g, f.read(sz))


def check_elf(g, elf):
    phoff, = struct.unpack_from("<I", elf, 0x1C)
    phn, = struct.unpack_from("<H", elf, 0x2C)
    va = h(g["hook"]["va"])
    for i in range(phn):
        t, off, pva, pa, fs = struct.unpack_from("<5I", elf, phoff + 32 * i)
        if pva <= va < pva + fs:
            got, = struct.unpack_from("<I", elf, off + va - pva)
            if got != h(g["hook"]["op"]):
                sys.exit("hook %s holds %08x, expected %s" % (g["hook"]["va"], got, g["hook"]["op"]))
            crc = 0
            for (w,) in struct.iter_unpack("<I", elf[:len(elf) // 4 * 4]):
                crc ^= w
            if "%08X" % crc != g["elf_crc"]:
                sys.exit("boot ELF CRC %08X, manifest says %s" % (crc, g["elf_crc"]))
            return
    sys.exit("hook address is outside the boot ELF")


def check_files(g, elf_path, gz_path):
    """The runtime forms against loose disc files: the boot ELF (hook word, CRC) and the
    module (each site's retail word, its R_MIPS_26, and the relocated guard halfword)."""
    check_elf(g, open(elf_path, "rb").read())
    _, raw = gz_split(open(gz_path, "rb").read())
    patch_module(raw, g)  # verifies the words and relocations, result unused
    print("%s: boot ELF CRC %s, hook %s holds %s" % (g["short"], g["elf_crc"], g["hook"]["va"], g["hook"]["op"]))
    words = runtime_words(g)
    if words is None:
        sys.exit("%s: no load_base yet" % g["short"])
    for addr, lo, new, e in words:
        print("  va %s -> %08x: guard %04x (relocated jal at base %s), write %08x"
              % (e["va"], addr, lo, g["module"]["load_base"], new))


# ---- commands ------------------------------------------------------------------------------

def read_module(src, g):
    with open(src, "rb") as f:
        lba, sz = find(f, g["module"]["file"])
        f.seek(lba * iso.SECTOR)
        return lba, f.read(sz)


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    games = load()
    cmd = argv[1]
    if cmd == "cheats":
        return cheats(games)
    serial, g = game(games, argv[2])
    if cmd == "pnach":
        return pnach(serial, g)
    if cmd == "files":
        return check_files(g, argv[3], argv[4])

    src = argv[3]
    check_source(g, src)
    check_hook(g, src)
    lba, gz = read_module(src, g)
    new, raw, patched = build_gz(g, gz)
    print("%s: %s at LBA %d, %d bytes; module %d bytes; %d words + %d relocations changed; re-deflated to the same size"
          % (g["short"], g["module"]["file"], lba, len(gz), len(raw), len(g["edits"]), len(g["edits"])))
    if cmd == "check":
        return

    out = argv[4]
    if cmd == "iso":
        if os.path.abspath(out) == os.path.abspath(src):
            sys.exit("refusing to write over the source image")
        shutil.copyfile(src, out)
        with open(out, "r+b") as f:
            f.seek(lba * iso.SECTOR)
            f.write(new)
        _, back = read_module(out, g)
        if back != new:
            sys.exit("read-back of the written module does not match")
        print("wrote %s  sha1 %s" % (out, sha1_file(out)))
        return

    if cmd == "xdelta":
        xd = argv[5]
        if not os.path.exists(out):
            sys.exit("build the image first: bypass.py iso %s %s %s" % (argv[2], src, out))
        subprocess.run(["xdelta3", "-e", "-9", "-f", "-s", src, out, xd], check=True)
        test = xd + ".roundtrip.iso"
        subprocess.run(["xdelta3", "-d", "-f", "-s", src, xd, test], check=True)
        a, b = sha1_file(out), sha1_file(test)
        os.remove(test)
        if a != b:
            sys.exit("xdelta round trip produced %s, the image is %s" % (b, a))
        meta = {"game": serial, "title": g["title"], "source_sha1": sha1_file(src),
                "source_size": os.path.getsize(src), "patched_sha1": a,
                "xdelta_sha1": sha1_file(xd), "xdelta_size": os.path.getsize(xd)}
        json.dump(meta, open(xd + ".json", "w"), indent=2)
        print("wrote %s (%d bytes), round trip OK" % (xd, os.path.getsize(xd)))
        return
    sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
