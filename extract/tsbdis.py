"""Disassembler for TSB2 zone/quest scripts (script.bin inside zone NBLs), PS2 PSU and AotI.

Format from EGS's script compiler (apps/egs/src/egs_script_compiler.erl, AGPL; this is a reader written
from the format, not a copy): "TSB2", u32 funcs table offset, u32 funcs table size, then the routines,
each  u32 next | name[32] | u32 size (code + 4) | u32 kind (0x4C code, 0x3C number var, 0x49 string var)
| u32 nvars | code words | u32 0 | nvars x {u32 symbol index, u32 code word index}. The fix-up list names
the 0xFFFFFFFF operands (calls, variable get/set) by index into the funcs table (32-byte names) at the end.

  python tsbdis.py <zone.nbl | script.bin | tsb> [routine substring ...]   (no names: list routines)
  python tsbdis.py --source <zone.nbl | script.bin | tsb>   the whole script as tsbasm.py source
                                                            (lossless: assembles back byte-identical)
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nbl  # noqa: E402
import prs  # noqa: E402

# opcode -> (mnemonic, operand words); -1 = string (count word, then that many words)
OPS = {
    0x00: ("ret", 0), 0x01: ("nop", 0), 0x02: ("push", 1), 0x03: ("pushf", 1), 0x04: ("add", 0),
    0x05: ("sub", 0), 0x06: ("mul", 0), 0x07: ("div", 0), 0x08: ("mod", 0), 0x09: ("shl", 0),
    0x0A: ("shr", 0), 0x0B: ("band", 0), 0x0C: ("bor", 0), 0x0D: ("bxor", 0), 0x0E: ("inc", 0),
    0x0F: ("dec", 0), 0x10: ("neg", 0), 0x11: ("abs", 0), 0x12: ("eq", 0), 0x13: ("neq", 0),
    0x14: ("gteq", 0), 0x15: ("gt", 0), 0x16: ("lteq", 0), 0x17: ("lt", 0), 0x18: ("land", 0),
    0x19: ("lor", 0), 0x1A: ("save", 0), 0x1B: ("restore", 0), 0x1D: ("savep", 0),
    0x2C: ("jmp", 1), 0x2D: ("jz", 1), 0x2E: ("jnz", 1), 0x3C: ("get", 1), 0x3D: ("set", 1),
    0x46: ("pushs", -1), 0x49: ("gets", 1), 0x4A: ("sets", 1), 0x4C: ("call", 1), 0x56: ("sync", 0),
    0x60: ("call", 1), 0x61: ("syscall", 1),
}


def load(path):
    d = open(path, "rb").read()
    if d[:4] == b"TSB2":
        return d
    if d[:4] == b"NMLL":
        nb = nbl.parse(d)
        data = nbl.nbl_data(d, nb)
        m = {x["name"]: x for x in nb["members"]}["script.bin"]
        d = data[m["off"]:m["off"] + m["size"]]
    out = prs.decompress(d, 0x1C)
    assert out[:4] == b"TSB2", out[:4]
    return out


def symbols(tsb):
    fpos, fsize = struct.unpack_from("<II", tsb, 4)
    base = 12 + fpos
    return [tsb[base + i:base + i + 32].split(b"\0")[0].decode("latin1") for i in range(0, fsize, 32)]


def routines(tsb):
    """(name, kind, code offset, code words, {word index: symbol})"""
    syms = symbols(tsb)
    pos = 0
    while True:
        a = 12 + pos
        nxt, = struct.unpack_from("<I", tsb, a)
        name = tsb[a + 4:a + 36].split(b"\0")[0].decode("latin1")
        size, kind, nvars = struct.unpack_from("<III", tsb, a + 36)
        code = a + 48
        n = max(size - 4, 0) // 4 if kind == 0x4C else 0
        words = list(struct.unpack_from(f"<{n}I", tsb, code))
        fix = {}
        if kind == 0x4C:
            vt = code + 4 * n + 4
            for k in range(nvars):
                s, at = struct.unpack_from("<II", tsb, vt + 8 * k)
                fix[at] = syms[s] if s < len(syms) else f"sym{s}"
        yield name, kind, code, words, fix
        if nxt == 0:
            return
        pos = nxt


def dis(words, fix):
    i, out = 0, []
    while i < len(words):
        op = words[i]
        mn, nargs = OPS.get(op, (f"op{op:#x}", 0))
        if nargs == -1:
            cnt = words[i + 1]
            s = struct.pack(f"<{cnt}I", *words[i + 2:i + 2 + cnt]).split(b"\0")[0].decode("latin1", "replace")
            out.append(f"{i:5d}  {mn} {s!r}")
            i += 2 + cnt
            continue
        args = []
        for k in range(1, nargs + 1):
            if i + k in fix:
                args.append(fix[i + k])
            elif mn in ("jmp", "jz", "jnz"):
                rel = struct.unpack("<i", struct.pack("<I", words[i + k]))[0]
                args.append(f"{rel:+d} -> {i + k + rel + 1}")
            elif mn == "pushf":
                args.append(f"{struct.unpack('<f', struct.pack('<I', words[i + k]))[0]:g}")
            else:
                args.append(str(struct.unpack("<i", struct.pack("<I", words[i + k]))[0]))
        out.append(f"{i:5d}  {mn} {', '.join(args)}".rstrip())
        i += 1 + nargs
    return out


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    if sys.argv[1] == "--source" and len(sys.argv) == 3:
        import tsbasm
        sys.stdout.buffer.write(tsbasm.disassemble(load(sys.argv[2])).encode("utf-8"))
        return
    tsb = load(sys.argv[1])
    want = sys.argv[2:]
    for name, kind, code, words, fix in routines(tsb):
        if kind != 0x4C:
            continue
        if not want:
            print(f"{name}  ({len(words)} words @ {code:#x})")
        elif any(w in name for w in want):
            print(f"== {name}  @ script+{code:#x}")
            print("\n".join(dis(words, fix)))


if __name__ == "__main__":
    main()
