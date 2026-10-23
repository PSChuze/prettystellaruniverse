"""Assembler for TSB2 zone/quest scripts (PS2 PSU and AotI), and the lossless source format that
`tsbdis.py --source` emits.  Disassemble -> edit -> assemble; unedited sources rebuild byte-identical.

TSB2 layout (see tsbdis.py): "TSB2", u32 funcs-table offset (from byte 12), u32 funcs-table size,
routines chained by `next` (offset from byte 12, 0 = last), then the funcs table (32-byte names).
Routine: u32 next | name[32] | u32 size | u32 kind | u32 nvars | body.  Code (kind 0x4C): size =
code bytes + 4, body = code words, u32 0 (a closing `ret` the assembler adds), then nvars x
{u32 funcs index, u32 code word index} fix-ups, last word first (the fixed-up operand holds
0xFFFFFFFF).  Number var (0x3C): size 0, nvars 0, no body.  String var (0x49): size 0, nvars =
length, no body.  The funcs table holds only names that some fix-up uses,
in first-use order (routine order, then fix-up order), so the assembler derives it.

Source format (one statement per line, `;` starts a comment, names with odd characters "quoted"):

  .var  NAME                 number variable (kind 0x3C)
  .str  NAME LEN             string variable (kind 0x49, nvars = LEN)
  .func NAME                 code routine (kind 0x4C); instructions follow until the next directive
  .routine NAME kind=K nvars=N size=S data=HEX       anything else, kept raw
      attributes (only written when the bytes are not the canonical ones): pad=HEX (raw 32-byte
      name field), size=N, nvars=N, tail=N (the word after the code), data=HEX (bytes after the
      canonical body), fixorder=i.j.. (fix-up table order as indexes into the fix-ups sorted by word;
      fixorder=asc for first word first; the usual order, last word first, is not written)
  .sym  NAME [pad=HEX]       explicit funcs table, in order (only when the derived one differs;
                             names that the code uses but the list misses are appended)

  LABEL:                     jump target (the disassembler writes L<word index>)
  push 12 | pushf 1.5 | pushs "text" [, WORDS] | pushs j"Shift-JIS text" (file is UTF-8)
  get NAME | set NAME | gets NAME | sets NAME | call NAME | call4c NAME | syscall 12
  jmp LABEL | jz LABEL | jnz LABEL
  ret nop add sub mul div mod shl shr band bor bxor inc dec neg abs eq neq gteq gt lteq lt land
  lor save restore savep sync, and op0xNN for unnamed opcodes (no operands assumed)
  .word W, W, ...            raw words; W = number, or &NAME (fix-up, placeholder 0xFFFFFFFF)

Any operand may also be written =NUMBER (the raw word, no fix-up) or &NAME (a fix-up to NAME), and
a fix-up whose placeholder is not 0xFFFFFFFF is written &NAME!0xHEX.  0x60 is `call` (what EGS's
compiler emits for a function call), 0x4C is `call4c` (seen on disc, same operand shape).

Usage:
  python tsbasm.py dis   <zone.nbl | script.bin | tsb> [-o out.tsa]      (= tsbdis.py --source)
  python tsbasm.py asm   <in.tsa> -o <out.tsb | out.bin>                  (.bin: script.bin, PRS)
  python tsbasm.py merge <base: nbl | script.bin | tsb | tsa> <add.tsa> -o <out.tsb | .bin | .tsa>
        routines in add.tsa replace same-named ones in the base, the rest are appended; new
        symbols land at the end of the funcs table, so existing indexes keep their values
  python tsbasm.py roundtrip [-j N] [--all-members] <path ...>
        every TSB2 found in NBLs / AFS (nested) / script.bin / tsb files (directories are walked)
        must disassemble and assemble back byte-identical; NBL members are opened only when named
        *script* unless --all-members; -j N runs N worker processes
"""
import hashlib
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nbl  # noqa: E402
import prs  # noqa: E402
import tsbdis  # noqa: E402

K_CODE, K_NUM, K_STR = 0x4C, 0x3C, 0x49
FIX = 0xFFFFFFFF
HDR = 48  # next + name[32] + size + kind + nvars

MNEM = {op: mn for op, (mn, _) in tsbdis.OPS.items()}
MNEM[0x4C] = "call4c"
NARGS = {op: n for op, (_, n) in tsbdis.OPS.items()}
BYNAME = {mn: op for op, mn in MNEM.items()}
JUMPS = {0x2C, 0x2D, 0x2E}
SYMOPS = {0x3C, 0x3D, 0x49, 0x4A, 0x4C, 0x60}
INTOPS = {0x02, 0x61}


class TsbError(Exception):
    pass


# ---------------------------------------------------------------- binary -> model

def parse(tsb):
    if tsb[:4] != b"TSB2":
        raise TsbError("no TSB2 magic")
    fpos, fsize = struct.unpack_from("<II", tsb, 4)
    if fsize % 32 or 12 + fpos + fsize != len(tsb):
        raise TsbError(f"funcs table {fpos:#x}+{fsize:#x} does not end the file ({len(tsb):#x})")
    symraw = [tsb[12 + fpos + i:12 + fpos + i + 32] for i in range(0, fsize, 32)]
    syms = [r.split(b"\0")[0].decode("latin1") for r in symraw]
    routines, pos = [], 0
    while True:
        a = 12 + pos
        if pos + HDR > fpos:
            raise TsbError(f"routine at {pos:#x} runs into the funcs table")
        nxt, = struct.unpack_from("<I", tsb, a)
        rawname = tsb[a + 4:a + 36]
        size, kind, nvars = struct.unpack_from("<III", tsb, a + 36)
        end = nxt if nxt else fpos
        if end <= pos:
            raise TsbError(f"routine at {pos:#x}: next {nxt:#x} goes backwards")
        body = tsb[a + HDR:12 + end]
        r = {"name": rawname.split(b"\0")[0].decode("latin1"), "rawname": rawname, "kind": kind,
             "size": size, "nvars": nvars}
        if kind == K_CODE and size >= 4 and size % 4 == 0 and size + 8 * nvars <= len(body):
            n = (size - 4) // 4
            r["words"] = list(struct.unpack_from(f"<{n}I", body, 0))
            r["tail"], = struct.unpack_from("<I", body, 4 * n)
            r["fix"] = [struct.unpack_from("<II", body, size + 8 * k) for k in range(nvars)]
            r["data"] = body[size + 8 * nvars:]
            for s, at in r["fix"]:
                if s >= len(syms) or at >= n:
                    raise TsbError(f"{r['name']}: fix-up ({s}, {at}) out of range")
        else:
            r["data"] = body
        routines.append(r)
        if nxt == 0:
            break
        pos = nxt
    if 12 + pos + routine_len(routines[-1]) != 12 + fpos:
        raise TsbError("routines do not end at the funcs table")
    return {"routines": routines, "syms": syms, "symraw": symraw}


def routine_len(r):
    n = HDR + len(r.get("data", b""))
    if "words" in r:
        n += 4 * len(r["words"]) + 4 + 8 * len(r["fix"])
    return n


# ---------------------------------------------------------------- model -> source

NAME_RE = re.compile(r"^[#-%'-+\-.0-9<>?@A-~]+$")  # printable ASCII minus space " , ; = & ! :


def qstr(b):
    out = []
    for c in b:
        if 0x20 <= c < 0x7F and c not in (0x22, 0x5C):
            out.append(chr(c))
        else:
            out.append({0x22: '\\"', 0x5C: "\\\\", 0x0A: "\\n", 0x09: "\\t"}.get(c, f"\\x{c:02x}"))
    return '"' + "".join(out) + '"'


def qtext(b):
    """String operand: Shift-JIS text as j"..." when it decodes and re-encodes exactly."""
    if any(c >= 0x80 for c in b) and all(c >= 0x20 for c in b):
        try:
            t = b.decode("cp932")
            if t.encode("cp932") == b:
                return 'j"' + t.replace("\\", "\\\\").replace('"', '\\"') + '"'
        except UnicodeError:
            pass
    return qstr(b)


def qname(name):
    if NAME_RE.match(name) and not re.match(r"^[-+0-9.]", name):
        return name
    return qstr(name.encode("latin1"))


def fmt_float(w):
    v, = struct.unpack("<f", struct.pack("<I", w))
    if v != v:
        return None
    for p in range(1, 18):
        s = f"{v:.{p}g}"
        if struct.pack("<f", float(s)) == struct.pack("<I", w):
            s = repr(float(s))  # 90.0 rather than 9e+01
            return s if struct.pack("<f", float(s)) == struct.pack("<I", w) else None
    return None


def s32(w):
    return w - (1 << 32) if w & 0x80000000 else w


def decode(words):
    """Instruction starts: [(index, opcode, total words)] as the VM would step them."""
    out, i, n = [], 0, len(words)
    while i < n:
        op = words[i]
        k = NARGS.get(op, 0)
        if k == -1:
            k = 1 + words[i + 1] if i + 1 < n else n
        if i + 1 + k > n:
            out.append((i, None, 1))  # operands run off the end: raw word
            i += 1
            continue
        out.append((i, op, 1 + k))
        i += 1 + k
    return out


def code_source(r, syms):
    words, fixmap = r["words"], {}
    for s, at in r["fix"]:
        fixmap.setdefault(at, []).append(syms[s])
    if any(len(v) > 1 for v in fixmap.values()):
        raise TsbError(f"{r['name']}: two fix-ups on one word")
    fixmap = {k: v[0] for k, v in fixmap.items()}
    ins = decode(words)
    starts = {i for i, _, _ in ins} | {len(words)}

    def sym_tok(at, bare):
        w = words[at]
        t = qname(fixmap[at]) if bare else "&" + qname(fixmap[at])
        return t if w == FIX else f"{t}!{w:#x}"

    def raw_line(i, cnt):
        toks = [sym_tok(j, False) if j in fixmap else f"{words[j]:#x}" for j in range(i, i + cnt)]
        return ".word " + ", ".join(toks)

    lines, labels = [], set()
    for i, op, cnt in ins:
        if op is None or any(j in fixmap for j in range(i, i + cnt)
                             if not (j == i + 1 and NARGS.get(op) == 1)):
            lines.append((i, raw_line(i, cnt)))
            continue
        mn = MNEM.get(op, f"op{op:#x}")
        if cnt == 1:
            lines.append((i, mn))
            continue
        if op == 0x46:
            cnt_w = words[i + 1]
            blob = struct.pack(f"<{cnt_w}I", *words[i + 2:i + 2 + cnt_w])
            s = blob.split(b"\0")[0]
            if len(s) == len(blob) or any(blob[len(s):]):
                lines.append((i, raw_line(i, cnt)))
                continue
            t = f"pushs {qtext(s)}"
            if cnt_w != len(s) // 4 + 1:
                t += f", {cnt_w}"
            lines.append((i, t))
            continue
        a, w = i + 1, words[i + 1]
        if a in fixmap:
            arg = sym_tok(a, op in SYMOPS)
        elif op in JUMPS:
            tgt = a + 1 + s32(w)
            if tgt in starts:
                arg = f"L{tgt}"
                labels.add(tgt)
            else:
                arg = f"={w:#x}"
        elif op in INTOPS:
            arg = str(s32(w))
        elif op == 0x03:
            arg = fmt_float(w) or f"={w:#x}"
        else:
            arg = f"={w:#x}"
        lines.append((i, f"{mn} {arg}"))
    out = []
    for i, t in lines:
        if i in labels:
            out.append(f"  L{i}:")
        out.append(f"    {t}")
    if len(words) in labels:
        out.append(f"  L{len(words)}:")
    return out


def derived_syms(routines_fix):
    """routines_fix: list of fix-up name lists in table order."""
    seen, out = set(), []
    for names in routines_fix:
        for s in names:
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def to_source(m):
    syms, rs = m["syms"], m["routines"]
    out = ["; TSB2 source (tsbasm.py); assemble with: python tsbasm.py asm <this> -o out.tsb"]
    used = derived_syms([[syms[s] for s, _ in r.get("fix", [])] for r in rs])
    plain = all(raw == s.encode("latin1").ljust(32, b"\0") for s, raw in zip(syms, m["symraw"]))
    if used != syms or not plain:
        for s, raw in zip(syms, m["symraw"]):
            pad = "" if raw == s.encode("latin1").ljust(32, b"\0") else f" pad={raw.hex()}"
            out.append(f".sym {qname(s)}{pad}")
    for r in rs:
        attrs = []
        name = r["name"]
        if r["rawname"] != name.encode("latin1").ljust(32, b"\0"):
            attrs.append(f"pad={r['rawname'].hex()}")
        kind = r["kind"]
        if "words" in r:
            head = f".func {qname(name)}"
            if r["size"] != 4 * len(r["words"]) + 4:
                attrs.append(f"size={r['size']}")
            if r["tail"]:
                attrs.append(f"tail={r['tail']:#x}")
            srt = sorted(at for _, at in r["fix"])
            inv = [srt.index(at) for _, at in r["fix"]]
            if inv == list(range(len(srt))) and len(srt) > 1:
                attrs.append("fixorder=asc")
            elif inv != list(range(len(srt)))[::-1]:
                attrs.append("fixorder=" + ".".join(str(j) for j in inv))
            if r["data"]:
                attrs.append(f"data={r['data'].hex()}")
        elif kind == K_NUM and r["size"] == 0 and r["nvars"] == 0 and not r["data"]:
            head = f".var {qname(name)}"
        elif kind == K_STR and r["size"] == 0 and not r["data"]:
            head = f".str {qname(name)} {r['nvars']}"
        else:
            head = f".routine {qname(name)}"
            attrs += [f"kind={kind:#x}", f"nvars={r['nvars']}", f"size={r['size']}"]
            if r["data"]:
                attrs.append(f"data={r['data'].hex()}")
        out.append(" ".join([head] + attrs))
        if "words" in r:
            out += code_source(r, syms)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- source -> model -> binary

def tokenize(line, lno):
    """Split one line into tokens: quoted strings stay whole, commas separate, ; comments."""
    toks, i, n, cur = [], 0, len(line), ""
    while i < n:
        c = line[i]
        if c == '"':
            sjis = cur == "j"
            if sjis:
                cur = ""
            j, buf = i + 1, []
            while j < n and line[j] != '"':
                if line[j] == "\\":
                    e = line[j + 1:j + 2]
                    if e == "x":
                        buf.append(int(line[j + 2:j + 4], 16))
                        j += 4
                        continue
                    if e not in ("n", "t", '"', "\\") or not e:
                        raise TsbError(f"line {lno}: unknown escape \\{e}")
                    buf.append({"n": 10, "t": 9, '"': 34, "\\": 92}[e])
                    j += 2
                    continue
                ch = line[j]
                try:
                    buf += ch.encode("cp932") if sjis else ch.encode("latin1")
                except UnicodeError:
                    raise TsbError(f"line {lno}: {ch!r} has no {'Shift-JIS' if sjis else 'latin1'} byte(s)")
                j += 1
            if j >= n:
                raise TsbError(f"line {lno}: unterminated string")
            cur += "\0STR" + bytes(buf).hex() + "\0"
            i = j + 1
            continue
        if c == ";":
            break
        if c.isspace() or c == ",":
            if cur:
                toks.append(cur)
                cur = ""
            i += 1
            continue
        cur += c
        i += 1
    if cur:
        toks.append(cur)
    return toks


STR_RE = re.compile(r"\0STR([0-9a-f]*)\0")


def unq(tok):
    """Token -> text with quoted parts decoded (latin1)."""
    return STR_RE.sub(lambda mt: bytes.fromhex(mt.group(1)).decode("latin1"), tok)


def is_str(tok):
    return STR_RE.fullmatch(tok) is not None


def num(tok, lno):
    try:
        return int(unq(tok), 0) & 0xFFFFFFFF
    except ValueError:
        raise TsbError(f"line {lno}: bad number {unq(tok)!r}")


def parse_attrs(toks, lno):
    out = {}
    for t in toks:
        if "=" not in t:
            raise TsbError(f"line {lno}: expected key=value, got {unq(t)!r}")
        k, v = t.split("=", 1)
        out[k] = v
    return out


def parse_source(text):
    """-> {"syms": [(name, raw)] or None, "routines": [routine dict with 'items' for code]}"""
    syms, rs, cur = [], [], None
    for lno, line in enumerate(text.splitlines(), 1):
        toks = tokenize(line, lno)
        if not toks:
            continue
        t0 = toks[0]
        if t0 == ".sym":
            a = parse_attrs(toks[2:], lno)
            name = unq(toks[1])
            raw = bytes.fromhex(a["pad"]) if "pad" in a else name.encode("latin1").ljust(32, b"\0")
            syms.append((name, raw))
            continue
        if t0 in (".var", ".str", ".func", ".routine"):
            name = unq(toks[1])
            rest = toks[2:]
            r = {"name": name, "lno": lno}
            if t0 == ".str":
                r["nvars"] = num(rest[0], lno)
                rest = rest[1:]
            a = parse_attrs(rest, lno)
            r["kind"] = {".var": K_NUM, ".str": K_STR, ".func": K_CODE}.get(t0)
            if t0 == ".routine":
                r["kind"] = num(a["kind"], lno)
                r["nvars"] = num(a.get("nvars", "0"), lno)
            if t0 == ".var":
                r["nvars"] = 0
            r["rawname"] = (bytes.fromhex(a["pad"]) if "pad" in a
                            else name.encode("latin1").ljust(32, b"\0"))
            if len(r["rawname"]) != 32:
                raise TsbError(f"line {lno}: routine name longer than 32 bytes")
            r["size"] = num(a["size"], lno) if "size" in a else None
            r["data"] = bytes.fromhex(a.get("data", ""))
            if t0 == ".func":
                r["items"] = []
                r["tail"] = num(a.get("tail", "0"), lno)
                fo = a.get("fixorder")
                r["fixorder"] = "asc" if fo == "asc" else (
                    [int(x) for x in fo.split(".") if x] if fo is not None else None)
            elif r["size"] is None:
                r["size"] = 0
            rs.append(r)
            cur = r
            continue
        if cur is None or "items" not in cur:
            raise TsbError(f"line {lno}: {unq(t0)!r} outside a .func")
        if len(toks) == 1 and t0.endswith(":") and not is_str(t0[:-1]):
            cur["items"].append(("label", unq(t0[:-1]), lno))
            continue
        cur["items"].append(("ins", toks, lno))
    return {"syms": syms or None, "routines": rs}


def operand(tok, op, lno):
    """-> ('raw', word) | ('sym', name, placeholder) | ('label', name)"""
    if tok.startswith("="):
        return ("raw", num(tok[1:], lno))
    ph = FIX
    if "!" in tok and not is_str(tok):
        tok, p = tok.rsplit("!", 1)
        ph = num(p, lno)
    if tok.startswith("&"):
        return ("sym", unq(tok[1:]), ph)
    if op in SYMOPS:
        return ("sym", unq(tok), ph)
    if op in JUMPS:
        return ("label", unq(tok))
    if op == 0x03:
        return ("raw", struct.unpack("<I", struct.pack("<f", float(unq(tok))))[0])
    return ("raw", num(tok, lno))


def assemble_code(r):
    """-> (words, [(symname, word index)] in table order)"""
    words, fix, labels, jumps = [], [], {}, []
    for kind, a, lno in r["items"]:
        if kind == "label":
            if a in labels:
                raise TsbError(f"line {lno}: label {a} defined twice")
            labels[a] = len(words)
            continue
        toks = a
        mn = unq(toks[0])
        args = toks[1:]
        if mn == ".word":
            for t in args:
                o = operand(t, None, lno) if (t.startswith("&") or t.startswith("=")) else ("raw", num(t, lno))
                if o[0] == "sym":
                    fix.append((o[1], len(words)))
                    words.append(o[2])
                else:
                    words.append(o[1])
            continue
        if mn in BYNAME:
            op = BYNAME[mn]
        elif mn.startswith("op0x"):
            op = int(mn[2:], 16)
        else:
            raise TsbError(f"line {lno}: unknown mnemonic {mn!r}")
        if op == 0x46:
            if not args or not is_str(args[0]):
                raise TsbError(f"line {lno}: pushs needs a string")
            s = bytes.fromhex(STR_RE.fullmatch(args[0]).group(1))
            cnt = num(args[1], lno) if len(args) > 1 else len(s) // 4 + 1
            blob = s.ljust(4 * cnt, b"\0")
            if len(blob) != 4 * cnt or (len(args) > 1 and len(s) >= 4 * cnt):
                raise TsbError(f"line {lno}: string does not fit {cnt} words")
            words += [op, cnt] + list(struct.unpack(f"<{cnt}I", blob))
            continue
        want = NARGS.get(op, 0)
        if len(args) != want:
            raise TsbError(f"line {lno}: {mn} takes {want} operand(s), got {len(args)}")
        words.append(op)
        for t in args:
            o = operand(t, op, lno)
            if o[0] == "sym":
                fix.append((o[1], len(words)))
                words.append(o[2])
            elif o[0] == "label":
                jumps.append((len(words), o[1], lno))
                words.append(0)
            else:
                words.append(o[1])
    for at, lab, lno in jumps:
        if lab not in labels:
            raise TsbError(f"line {lno}: undefined label {lab}")
        words[at] = (labels[lab] - (at + 1)) & 0xFFFFFFFF
    if r.get("fixorder") == "asc":
        pass
    elif r.get("fixorder") is not None:
        order = r["fixorder"]
        if sorted(order) != list(range(len(fix))):
            raise TsbError(f"{r['name']}: fixorder does not match the {len(fix)} fix-ups")
        fix = [fix[j] for j in order]
    else:
        fix.reverse()  # the table lists them last word first
    return words, fix


def build(src):
    """Parsed source -> TSB2 bytes."""
    rs = src["routines"]
    if not rs:
        raise TsbError("no routines")
    codes = []
    for r in rs:
        codes.append(assemble_code(r) if "items" in r else None)
    used = derived_syms([[s for s, _ in c[1]] if c else [] for c in codes])
    if src["syms"]:
        table = list(src["syms"])
        have = {s for s, _ in table}
        table += [(s, s.encode("latin1").ljust(32, b"\0")) for s in used if s not in have]
    else:
        table = [(s, s.encode("latin1").ljust(32, b"\0")) for s in used]
    index = {}
    for k, (s, _) in enumerate(table):
        index.setdefault(s, k)
    for s, raw in table:
        if len(s.encode("latin1")) > 32 or len(raw) != 32:
            raise TsbError(f"symbol {s!r} longer than 32 bytes")
    blobs = []
    for r, c in zip(rs, codes):
        if c:
            words, fix = c
            size = r["size"] if r["size"] is not None else 4 * len(words) + 4
            body = struct.pack(f"<{len(words)}I", *words) + struct.pack("<I", r["tail"])
            body += b"".join(struct.pack("<II", index[s], at) for s, at in fix) + r["data"]
            nvars = len(fix)
        else:
            size, nvars, body = r["size"], r["nvars"], r["data"]
        blobs.append((r["rawname"], size, r["kind"], nvars, body))
    out, pos = [], 0
    for k, (rawname, size, kind, nvars, body) in enumerate(blobs):
        ln = HDR + len(body)
        nxt = pos + ln if k + 1 < len(blobs) else 0
        out.append(struct.pack("<I", nxt) + rawname + struct.pack("<III", size, kind, nvars) + body)
        pos += ln
    funcs = b"".join(raw for _, raw in table)
    return b"TSB2" + struct.pack("<II", pos, len(funcs)) + b"".join(out) + funcs


def assemble(text):
    return build(parse_source(text))


def disassemble(tsb):
    return to_source(parse(tsb))


# ---------------------------------------------------------------- containers

def script_bin(tsb):
    """TSB2 -> script.bin member bytes: u32 size, u32 PRS length + 4, 0x14 zero bytes, PRS."""
    import prsenc
    c = prsenc.compress(tsb)
    return struct.pack("<II", len(tsb), len(c) + 4) + bytes(0x14) + c


def unwrap(d):
    """bytes of a .tsb / script.bin -> TSB2, or None."""
    if d[:4] == b"TSB2":
        return d
    if len(d) > 0x20 and b"TSB" in d[0x1C:0x24]:
        try:
            out = prs.decompress(d, 0x1C)
        except Exception:
            return None
        return out if out[:4] == b"TSB2" else None
    return None


def load_any(path):
    d = open(path, "rb").read()
    if path.lower().endswith(".tsa"):
        return None, d.decode("utf-8-sig")
    if d[:4] == b"NMLL":
        for _, tsb in nbl_scripts(d):
            return tsb, None
        raise TsbError(f"{path}: no script in this NBL")
    tsb = unwrap(d)
    if tsb is None:
        raise TsbError(f"{path}: not a TSB2 / script.bin / NBL / .tsa")
    return tsb, None


def nbl_scripts(d, any_member=False):
    """(member name, TSB2) for each script member; by default only members named *script*
    are opened (decrypting + unpacking big texture/model NBLs to look is slow)."""
    nb = nbl.parse(d)
    ms = [m for m in nb["members"] if any_member or "script" in m["name"].lower()]
    if not ms:
        return
    data = nbl.nbl_data(d, nb)
    for m in ms:
        tsb = unwrap(data[m["off"]:m["off"] + m["size"]])
        if tsb is not None:
            yield m["name"], tsb


def walk_blob(d, where, any_member=False):
    """Yield (where, tsb) for every script inside an NBL / AFS (nested) / script file."""
    if d[:4] == b"AFS\0":
        for name, (off, size) in nbl.afs_entries(d):
            yield from walk_blob(d[off:off + size], f"{where}:{name}", any_member)
    elif d[:4] == b"NMLL":
        try:
            for name, tsb in nbl_scripts(d, any_member):
                yield f"{where}:{name}", tsb
        except (Exception, SystemExit) as e:
            print(f"  skip {where}: {e}", file=sys.stderr)
    else:
        tsb = unwrap(d)
        if tsb is not None:
            yield where, tsb


def walk_files(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for f in sorted(files):
                    if f.lower().endswith((".nbl", ".afs", ".bin", ".tsb")):
                        yield os.path.join(root, f)
        else:
            yield p


def check(tsb):
    """'' when disassemble -> assemble gives the same bytes, else why not."""
    try:
        back = assemble(disassemble(tsb))
    except TsbError as e:
        return f"error: {e}"
    if back == tsb:
        return ""
    at = next((i for i in range(min(len(back), len(tsb))) if back[i] != tsb[i]), min(len(back), len(tsb)))
    return f"differs ({len(back):#x} vs {len(tsb):#x} bytes, first at {at:#x})"


def _check_file(args):
    path, any_member = args
    out = []
    for where, tsb in walk_blob(open(path, "rb").read(), path, any_member):
        out.append((where, hashlib.sha1(tsb).hexdigest(), tsb))
    return out


# ---------------------------------------------------------------- commands

def _out(argv):
    if "-o" in argv:
        k = argv.index("-o")
        o = argv[k + 1]
        del argv[k:k + 2]
        return o
    return None


def write_out(path, tsb=None, text=None):
    if path is None:
        sys.stdout.buffer.write((text if text is not None else disassemble(tsb)).encode("utf-8"))
        return
    if path.lower().endswith(".tsa"):
        open(path, "w", encoding="utf-8", newline="\n").write(text if text is not None else disassemble(tsb))
        return
    if tsb is None:
        tsb = assemble(text)
    open(path, "wb").write(script_bin(tsb) if path.lower().endswith(".bin") else tsb)


def cmd_roundtrip(paths, jobs=1, any_member=False):
    total, seen, bad, nfiles = 0, {}, 0, set()
    files = [(f, any_member) for f in walk_files(paths)]
    if jobs > 1:
        import multiprocessing
        pool = multiprocessing.Pool(jobs)
        results = pool.imap_unordered(_check_file, files)
    else:
        results = map(_check_file, files)
    for res in results:
        for where, h, tsb in res:
            total += 1
            nfiles.add(where[:2] + where[2:].split(":")[0])
            if h in seen:
                continue
            why = check(tsb)
            seen[h] = why
            if why:
                bad += 1
                print(f"FAIL {where}: {why}")
    print(f"{total} scripts in {len(nfiles)} files ({len(seen)} distinct): "
          f"{len(seen) - bad} round-trip byte-identical, {bad} fail")
    return bad == 0


def main():
    argv = sys.argv[1:]
    if not argv:
        raise SystemExit(__doc__)
    cmd = argv.pop(0)
    out = _out(argv)
    if cmd == "dis" and len(argv) == 1:
        tsb, _ = load_any(argv[0])
        write_out(out if out else None, tsb=tsb, text=disassemble(tsb))
    elif cmd == "asm" and len(argv) == 1 and out:
        write_out(out, text=open(argv[0], encoding="utf-8-sig").read())
    elif cmd == "merge" and len(argv) == 2 and out:
        tsb, text = load_any(argv[0])
        base = parse_source(text if text is not None else disassemble(tsb))
        add = parse_source(open(argv[1], encoding="utf-8-sig").read())
        byname = {r["name"]: k for k, r in enumerate(base["routines"])}
        for r in add["routines"]:
            if r["name"] in byname:
                base["routines"][byname[r["name"]]] = r
                print(f"replaced {r['name']}", file=sys.stderr)
            else:
                base["routines"].append(r)
                print(f"added {r['name']}", file=sys.stderr)
        if add["syms"]:
            if not base["syms"]:  # pin the base's derived table so its indexes stay put
                m = parse(build(base))
                base["syms"] = list(zip(m["syms"], m["symraw"]))
            have = {s for s, _ in base["syms"]}
            base["syms"] += [x for x in add["syms"] if x[0] not in have]
        new = build(base)
        write_out(out, tsb=new, text=disassemble(new) if out.lower().endswith(".tsa") else None)
    elif cmd == "roundtrip" and argv:
        jobs = 1
        if "-j" in argv:
            k = argv.index("-j")
            jobs = int(argv[k + 1])
            del argv[k:k + 2]
        anym = "--all-members" in argv
        argv = [a for a in argv if a != "--all-members"]
        sys.exit(0 if cmd_roundtrip(argv, jobs, anym) else 1)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    try:
        main()
    except TsbError as e:
        raise SystemExit(f"tsbasm: {e}")
