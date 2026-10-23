"""PSU PS2 game module (ERX) tool: unpack, sections, server table, certs, xrefs.

The boot ELF (SLUS_211.94 / SLUS_216.31) is a loader. The game is ERX/PSU_US.GZ:
plain gzip around a relocatable EE ELF (e_type 0xFF91, image based at 0). Nothing
is DNAS-encrypted. Because the image is based at 0 and relocations are REL, the
file still holds pre-relocation addresses: a pointer in .data to a string equals
the string's section address, which is how `net` and `xref` find things.

Usage:
  python erx.py unpack   <disc_dir> <out_dir>      gunzip every ERX/*.GZ
  python erx.py sections <PSU_US.erx>
  python erx.py net      <PSU_US.erx>              {host*, port} pairs for *.segaonline.jp
  python erx.py certs    <PSU_US.erx> <out_dir>    embedded X.509 certs + RSA keys (DER)
  python erx.py xref     <PSU_US.erx> <string>     .data pointers + lui/addiu pairs
"""
import gzip
import os
import re
import struct
import sys


def load(path):
    return open(path, "rb").read()


def sections(d):
    shoff, = struct.unpack_from("<I", d, 0x20)
    shes, shn, shstr = struct.unpack_from("<HHH", d, 0x2E)
    secs = [struct.unpack_from("<10I", d, shoff + i * shes) for i in range(shn)]
    so = secs[shstr][4]
    out = {}
    for s in secs:
        name = d[so + s[0]:d.index(b"\0", so + s[0])].decode()
        out[name] = {"type": s[1], "addr": s[3], "off": s[4], "size": s[5]}
    return out


class Image:
    def __init__(self, path):
        self.d = load(path)
        self.s = sections(self.d)
        self.data = self.s[".data"]
        self.text = self.s[".text"]

    def off2va(self, off):
        for sec in (self.text, self.data):
            if sec["off"] <= off < sec["off"] + sec["size"]:
                return off - sec["off"] + sec["addr"]
        return None

    def va2off(self, va):
        for sec in (self.text, self.data):
            if sec["addr"] <= va < sec["addr"] + sec["size"]:
                return va - sec["addr"] + sec["off"]
        return None

    def data_ptrs(self, va):
        lo, hi = self.data["off"], self.data["off"] + self.data["size"]
        needle = struct.pack("<I", va)
        o = self.d.find(needle, lo, hi)
        while o != -1:
            if o % 4 == 0:
                yield o
            o = self.d.find(needle, o + 1, hi)


class ExecImage:
    """A plain EE ELF (e_type 2, e.g. the SLUS boot ELF): same interface as Image, via PT_LOADs."""

    def __init__(self, path):
        self.d = load(path)
        phoff, = struct.unpack_from("<I", self.d, 0x1C)
        phes, phn = struct.unpack_from("<HH", self.d, 0x2A)
        self.segs = []
        for i in range(phn):
            ptype, off, va, _pa, filesz, _memsz, flags, _al = struct.unpack_from("<8I", self.d, phoff + i * phes)
            if ptype == 1 and filesz:
                self.segs.append({"addr": va, "off": off, "size": filesz, "flags": flags})
        self.text = self.segs[0]
        self.data = {"addr": 0, "off": 0, "size": 0}
        self.s = {f"seg{i}": g for i, g in enumerate(self.segs)}

    def off2va(self, off):
        for g in self.segs:
            if g["off"] <= off < g["off"] + g["size"]:
                return off - g["off"] + g["addr"]
        return None

    def va2off(self, va):
        for g in self.segs:
            if g["addr"] <= va < g["addr"] + g["size"]:
                return va - g["addr"] + g["off"]
        return None

    def data_ptrs(self, va):
        needle = struct.pack("<I", va)
        for g in self.segs[1:]:
            o = self.d.find(needle, g["off"], g["off"] + g["size"])
            while o != -1:
                if o % 4 == 0:
                    yield o
                o = self.d.find(needle, o + 1, g["off"] + g["size"])


def open_image(path):
    """Image for an ERX (e_type 0xFF91), ExecImage for a plain ELF."""
    et, = struct.unpack_from("<H", load(path)[:18], 16)
    return ExecImage(path) if et == 2 else Image(path)


def cmd_unpack(disc, out):
    os.makedirs(out, exist_ok=True)
    src = os.path.join(disc, "ERX")
    for n in sorted(os.listdir(src)):
        if n.upper().endswith(".GZ"):
            dst = os.path.join(out, n[:-3] + ".erx")
            with gzip.open(os.path.join(src, n)) as f, open(dst, "wb") as g:
                g.write(f.read())
            print(f"{n} -> {dst} ({os.path.getsize(dst)} bytes)")


def cmd_sections(path):
    d = load(path)
    et, = struct.unpack_from("<H", d, 16)
    print(f"e_type {et:#x}")
    for name, s in sections(d).items():
        if name:
            print(f"  {name:16s} type={s['type']:#x} addr={s['addr']:08x} off={s['off']:08x} size={s['size']:08x}")


def cmd_net(path):
    im = Image(path)
    for m in re.finditer(rb"[a-z0-9.-]+\.segaonline\.jp\0", im.d):
        host = m.group()[:-1].decode()
        va = im.off2va(m.start())
        for p in im.data_ptrs(va):
            port, = struct.unpack_from("<I", im.d, p + 4)
            print(f"{host:36s} port {port:5d}   (pair at va {im.off2va(p):#x})")


def cmd_certs(path, out):
    d = load(path)
    os.makedirs(out, exist_ok=True)
    pats = [("cert", rb"\x30\x82..\x30\x82"), ("rsakey", rb"\x30\x82..\x02\x01\x00\x02(?:\x81\x81|\x81\x80|\x82\x01\x01)")]
    for kind, pat in pats:
        for m in re.finditer(pat, d, re.S):
            n = int.from_bytes(d[m.start() + 2:m.start() + 4], "big") + 4
            der = d[m.start():m.start() + n]
            # every CN in order: a cert holds issuer first, then subject
            names = [der[i + 7:i + 7 + der[i + 6]].decode("latin1")
                     for i in (m2.start() for m2 in re.finditer(rb"\x06\x03U\x04\x03", der))]
            dst = os.path.join(out, f"{kind}_{m.start():x}.der")
            open(dst, "wb").write(der)
            print(f"{kind:6s} at {m.start():#x} len {n}  {names}  -> {dst}")


def cmd_xref(path, text):
    im = Image(path)
    o = im.d.index(text.encode() + b"\0")
    va = im.off2va(o)
    print(f"{text!r} at file {o:#x} va {va:#x}")
    for p in im.data_ptrs(va):
        print(f"  .data pointer at va {im.off2va(p):#x}")
    hi, lo = (va + 0x8000) >> 16, va & 0xFFFF
    t = im.text
    words = struct.unpack_from(f"<{t['size'] // 4}I", im.d, t["off"])
    for i, w in enumerate(words):
        if w >> 26 == 0xF and w & 0xFFFF == hi:
            rt = (w >> 16) & 31
            for w2 in words[i + 1:i + 12]:
                if w2 & 0xFFFF == lo and (w2 >> 21) & 31 == rt:
                    print(f"  lui/lo pair at text {t['addr'] + i * 4:#x}")
                    break


def main():
    a = sys.argv[1:]
    cmds = {"unpack": (cmd_unpack, 2), "sections": (cmd_sections, 1), "net": (cmd_net, 1),
            "certs": (cmd_certs, 2), "xref": (cmd_xref, 2)}
    if not a or a[0] not in cmds or len(a) - 1 != cmds[a[0]][1]:
        raise SystemExit(__doc__)
    cmds[a[0]][0](*a[1:])


if __name__ == "__main__":
    main()
