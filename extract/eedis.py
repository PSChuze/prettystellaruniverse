"""Annotating disassembler for PSU_US.erx and the SLUS boot ELF (capstone, MIPS LE + R5900 lq/sq).

The ERX is based at 0 with REL relocations, so jal targets and lui/addiu pairs in the file
are the real (pre-load) addresses. The boot ELF is an ordinary executable (text at 0x100000).
Strings built by lui+addiu/ori/lw are labelled inline.

Usage:
  <erx> may also be a plain ELF such as <work>/psu/disc/SLUS_211.94.
  python eedis.py fn      <erx> <addr>          disassemble the function containing addr
  python eedis.py range   <erx> <start> <end>
  python eedis.py callers <erx> <addr>          every jal to addr (addr = function start)
  python eedis.py strref  <erx> <text>          functions that build a pointer into a string
                                                containing text (substring match)

Names: pass --names file.tsv (addr<TAB>name) to label jal targets; names.tsv next to this file by default.
"""
import os
import re
import struct
import sys

import capstone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import erx  # noqa: E402

REG = ("zero at v0 v1 a0 a1 a2 a3 t0 t1 t2 t3 t4 t5 t6 t7 "
       "s0 s1 s2 s3 s4 s5 s6 s7 t8 t9 k0 k1 gp sp fp ra").split()
NAMES_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "names.tsv")


class Dis:
    def __init__(self, path, names=None):
        self.im = erx.open_image(path)
        t = self.im.text
        self.base = t["addr"]
        self.code = self.im.d[t["off"]:t["off"] + t["size"]]
        self.words = struct.unpack_from(f"<{len(self.code) // 4}I", self.code)
        self.md = capstone.Cs(capstone.CS_ARCH_MIPS, capstone.CS_MODE_MIPS64 | capstone.CS_MODE_LITTLE_ENDIAN)
        self.md.skipdata = True  # R5900-only encodings would otherwise end the listing
        self.names = {}
        p = names or NAMES_DEFAULT
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                if line.strip() and not line.startswith("#"):
                    a, n = line.rstrip("\n").split("\t")[:2]
                    self.names[int(a, 16)] = n

    def string_at(self, va):
        o = self.im.va2off(va)
        if o is None:
            return None
        e = self.im.d.find(b"\0", o, o + 200)
        b = self.im.d[o:e] if e != -1 else b""
        if len(b) >= 3 and all(32 <= c < 127 or c in (9, 10) for c in b):
            return b.decode()
        return None

    def fn_start(self, addr):
        # walk back to the nearest `addiu sp,sp,-N` that follows a `jr ra` (+ delay slot)
        i = (addr - self.base) // 4
        while i > 0:
            w = self.words[i]
            if w >> 16 == 0x27BD and w & 0x8000:
                j = i - 1
                while j > 0 and self.words[j] == 0:
                    j -= 1
                if self.words[j - 1] == 0x03E00008 or self.words[j] == 0x03E00008:
                    return self.base + i * 4
            i -= 1
        return self.base

    def fn_end(self, start):
        i = (start - self.base) // 4
        while i < len(self.words):
            if self.words[i] == 0x03E00008:
                # stop at the first jr ra followed by a new prologue or padding
                nxt = self.words[i + 2] if i + 2 < len(self.words) else 0
                if nxt >> 16 == 0x27BD or nxt == 0:
                    return self.base + (i + 2) * 4
            i += 1
        return self.base + len(self.words) * 4

    def lines(self, start, end):
        hi = {}
        out = []
        b = self.base
        for ins in self.md.disasm(self.code[start - b:end - b], start):
            w = struct.unpack_from("<I", self.code, ins.address - b)[0]
            op, rs, rt, imm = w >> 26, (w >> 21) & 31, (w >> 16) & 31, w & 0xFFFF
            simm = imm - 0x10000 if imm & 0x8000 else imm
            note = ""
            if op == 0xF:
                hi[rt] = imm << 16
            elif op in (0x9, 0x23, 0x2B, 0x21, 0x24, 0x25, 0x20) and rs in hi:  # addiu/lw/sw/lh/lbu/lhu/lb
                va = (hi[rs] + simm) & 0xFFFFFFFF
                s = self.string_at(va)
                note = f"  ; {va:#x}" + (f' "{s[:70]}"' if s else "")
                n = self.names.get(va)
                if n:
                    note += f" <{n}>"
            elif op == 0xD and rs in hi:  # ori
                note = f"  ; {hi[rs] | imm:#x}"
            if op == 3 or op == 2:
                tgt = (w & 0x3FFFFFF) << 2
                note = f"  ; -> {tgt:#x}" + (f" <{self.names[tgt]}>" if tgt in self.names else "")
            mn, ops = ins.mnemonic, ins.op_str
            if op in (0x1E, 0x1F):  # R5900 lq / sq (capstone reads them as MSA)
                mn, ops = ("lq", "sq")[op - 0x1E], f"${REG[rt]}, {simm:#x}(${REG[rs]})"
            out.append(f"{ins.address:08x}: {w:08x}  {mn:8s} {ops}{note}")
        return out

    def callers(self, addr):
        want = 0x0C000000 | (addr >> 2)
        return [self.base + i * 4 for i, w in enumerate(self.words) if w == want]

    def strref(self, text):
        hits = []
        for m in re.finditer(re.escape(text.encode()), self.im.d):
            o = m.start()
            while o > 0 and 32 <= self.im.d[o - 1] < 127:
                o -= 1
            va = self.im.off2va(o)
            if va is None:
                continue
            h, lo = (va + 0x8000) >> 16, va & 0xFFFF
            for i, w in enumerate(self.words):
                if w >> 26 == 0xF and w & 0xFFFF == h:
                    rt = (w >> 16) & 31
                    for w2 in self.words[i + 1:i + 16]:
                        if w2 & 0xFFFF == lo and (w2 >> 21) & 31 == rt:
                            hits.append((self.base + i * 4, va, self.string_at(va)))
                            break
        return hits


def main():
    a = [x for x in sys.argv[1:]]
    names = None
    if "--names" in a:
        k = a.index("--names")
        names = a[k + 1]
        del a[k:k + 2]
    if len(a) < 3:
        raise SystemExit(__doc__)
    cmd, path = a[0], a[1]
    d = Dis(path, names)
    if cmd == "fn":
        s = d.fn_start(int(a[2], 16))
        e = d.fn_end(s)
        print(f"; function {s:#x}..{e:#x}  callers: {[hex(c) for c in d.callers(s)][:12]}")
        print("\n".join(d.lines(s, e)))
    elif cmd == "range":
        print("\n".join(d.lines(int(a[2], 16), int(a[3], 16))))
    elif cmd == "callers":
        for c in d.callers(int(a[2], 16)):
            print(f"{c:#x} in fn {d.fn_start(c):#x}")
    elif cmd == "strref":
        for at, va, s in d.strref(a[2]):
            print(f"{at:#x} (fn {d.fn_start(at):#x})  {va:#x} {s!r}")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
