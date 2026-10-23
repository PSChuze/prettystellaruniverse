"""Sega PSU (PS2) NBL archive lister/extractor, with the game's Blowfish variant, plus AFS splitting.

NBL layout (all little endian; offsets from the start of the NBL):
  0x00  "NMLL"  u16 2, u16 name-buffer size (the parser allocates it; sum of name lengths + NULs)
  0x08  u32 header size (member table ends here; data starts at the next 0x800 boundary)
  0x0C  u32 member count
  0x10  u32 data size (uncompressed)
  0x14  u32 compressed data size (0 = data not compressed)
  0x18  u32 pointer-table size (bytes)
  0x1C  u32 Blowfish key (0 = not encrypted); the 4 bytes as stored are the key
  0x20  u32 TMLL header size, 0x24 u32 TMLL data size (0 = no TMLL), 0x28 u32 TMLL compressed
        size, 0x2C u32 TMLL member count
  0x30  member records, `count` of them, stride = record +4 (0x60; the last may hold 0):
        +0x00 type tag ("REL\0", "STD\0", "SNR\0", "SNJ\0", ...)  +0x04 u32 record size
        +0x10 char name[0x20]   +0x30 u32 data offset  +0x34 u32 size
        +0x38 u32 pointer-table offset  +0x3C u32 pointer-table size       (0x10..0x40 encrypted)
        +0x40 "NSIF" info chunk (plain; absent/zero for STD)
  align(hdr, 0x800)             data section: `csize` bytes if compressed, else `dsize`
  align(data end, 0x800)        pointer table (`ptsize` bytes, plain u32 offsets into the data
                                 that the loader relocates; member +0x38/+0x3C index into it)
  align(ptable end, 0x800)      optional "TMLL" texture section, same shape: 0x08 header size,
                                 0x0C count, 0x10 data size, 0x14 compressed size, records at
                                 0x20 ("NNVR" etc, same 0x60 layout, same key, 0x10..0x40
                                 encrypted); its data section is PRS but NOT encrypted.
                                 TMLL record +0x40.. (plain) is the texture file header:
                                 "GBIX"+"PVRT" for NNVR (pixel data comes from the section),
                                 a whole "PVPL" palette for NNPL. `x` joins header + data.

Crypto, as the game does it (PSU_US.erx; addresses are the pre-relocation image addresses):
  0x7cd0   NMLL header parser: key = hdr+0x1C; if nonzero, ctx = alloc(0x104c) (0x156b0) and
           0x156c0(ctx, &key, 4); ctx kept at obj+0x8c. Key also handed to the TMLL reader
           (0x7f00 -> tmll+0x38; 0xe720 parses "TMLL" and makes its own ctx at tmll+0x40).
  0x156c0  init: vtable 0x5d9200 (+0x0C encrypt 0x15e00, +0x10 decrypt 0x15740), then 0x15e80.
  0x15e80  key schedule: P (18 words at ctx+4) <- 0x5677d0, S (4x256 at ctx+0x4c) <- 0x567820
           (the standard pi tables); P[i] ^= key bytes packed LITTLE endian (k[j] | k[j+1]<<8 ..,
           cycling); then the usual chain of encryptions of (0,0) through 0x16010 fills P and S.
  0x157c0  block decrypt (0x16010 encrypt), halves are LE u32 words at buf+0 (L) and buf+4 (R).
           Standard 16-round Feistel, EXCEPT F takes the bytes of x in little-endian order:
             F(x) = ((S0[x & 0xff] + S1[(x >> 8) & 0xff]) ^ S2[(x >> 16) & 0xff]) + S3[x >> 24]
           (standard Blowfish uses S0[x >> 24] .. S3[x & 0xff]); this is why a stock Blowfish
           does not decode. ECB, no chaining.
  0x15740  buffer decrypt (ctx, buf, len): len // 8 blocks in place; a tail < 8 bytes stays plain.
  Regions the game decrypts:
    0x7a40 / 0xe4f0  every member record, record+0x10 .. +0x40 (0x7cc0 returns 0x10, length
                     0x40 - 0x10): the name and the 4 offset/size words. Tags and NSIF stay plain.
    0x7460 (0x74d4)  the whole data section as read from disc (length obj+0x9c = csize if
                     compressed else dsize), BEFORE decompression (0x3cd0, PRS).
    Nothing else: the pointer table and the TMLL data section are never passed to the ctx.
  So: decrypt records; decrypt data (the compressed stream); then PRS-decompress; then split.

Usage:
  python nbl.py list   <file.nbl>                     header, member table, flags
  python nbl.py x      <file.nbl> <outdir>            decrypt + decompress + split members
                                                      (TMLL members go to <outdir>/tmll/;
                                                       pointer table to <outdir>/_ptrtable.bin)
  python nbl.py afs    <file.afs> <outdir> [--nbl]    split an AFS by its name table; --nbl
                                                      also unpacks each NBL into <name>_nbl/
  python nbl.py verify <file.nbl> [--bytes N]         run the game's own init + decrypt code
                                                      (unicorn) on the records and the first N
                                                      data bytes (default 0x10000; 0 = all)
                                                      and compare with the Python port
  Options: --erx PSU_US.erx (Blowfish P/S tables are read from it; default the psu module under the work
           directory, else the aoti one: workdir.py. Both discs carry the same tables.)
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import erx  # noqa: E402
import prs  # noqa: E402
import workdir  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ERX = None              # workdir.any_erx() when the tables are first needed
workdir.work()

BF_P_VA = 0x5677d0
BF_S_VA = 0x567820
FN_INIT = 0x156c0
FN_DECRYPT = 0x15740
FN_MEMCPY = 0x48a9a4
REC_ENC_START, REC_ENC_END = 0x10, 0x40
M32 = 0xFFFFFFFF


def align(x, a=0x800):
    return (x + a - 1) & ~(a - 1)


# ---------------------------------------------------------------- Blowfish (game variant)

_tables = {}


def bf_tables(erx_path):
    erx_path = erx_path or workdir.any_erx()
    if erx_path not in _tables:
        im = erx.open_image(erx_path)
        po, so = im.va2off(BF_P_VA), im.va2off(BF_S_VA)
        if po is None or struct.unpack_from("<I", im.d, po)[0] != 0x243F6A88:
            # AotI's module holds the same tables at 0x5cb420 / 0x5cb470: find P by its first words
            po = im.d.find(struct.pack("<2I", 0x243F6A88, 0x85A308D3))
            if po < 0:
                raise SystemExit(f"{erx_path}: Blowfish P table not found")
            so = po + BF_S_VA - BF_P_VA
        p = list(struct.unpack_from("<18I", im.d, po))
        s = list(struct.unpack_from("<1024I", im.d, so))
        _tables[erx_path] = (p, s)
    return _tables[erx_path]


class Blowfish:
    """PSU's Blowfish: LE key packing, LE data words, F with little-endian byte order."""

    def __init__(self, key, erx_path=DEFAULT_ERX):
        p0, s0 = bf_tables(erx_path)
        self.P = p0[:]
        self.S = [s0[i * 256:(i + 1) * 256] for i in range(4)]
        n, j = len(key), 0
        for i in range(18):
            self.P[i] ^= key[j % n] | key[(j + 1) % n] << 8 | key[(j + 2) % n] << 16 | key[(j + 3) % n] << 24
            j = (j + 4) % n
        lr = (0, 0)
        for i in range(0, 18, 2):
            lr = self.encrypt_block(*lr)
            self.P[i], self.P[i + 1] = lr
        for box in self.S:
            for i in range(0, 256, 2):
                lr = self.encrypt_block(*lr)
                box[i], box[i + 1] = lr

    def _f(self, x):
        s0, s1, s2, s3 = self.S
        return ((((s0[x & 0xFF] + s1[(x >> 8) & 0xFF]) & M32) ^ s2[(x >> 16) & 0xFF]) + s3[x >> 24]) & M32

    def encrypt_block(self, l, r):
        P, f = self.P, self._f
        for i in range(16):
            l ^= P[i]
            r ^= f(l)
            l, r = r, l
        return r ^ P[17], l ^ P[16]

    def decrypt_block(self, l, r):
        P, f = self.P, self._f
        for i in range(17, 1, -1):
            l ^= P[i]
            r ^= f(l)
            l, r = r, l
        return r ^ P[0], l ^ P[1]

    def decrypt(self, buf):
        b = bytearray(buf)
        for o in range(0, len(b) & ~7, 8):
            l, r = struct.unpack_from("<2I", b, o)
            struct.pack_into("<2I", b, o, *self.decrypt_block(l, r))
        return bytes(b)

    def encrypt(self, buf):
        """Inverse of decrypt (the game never encrypts; nblpack.py does, to rebuild keyed archives)."""
        b = bytearray(buf)
        for o in range(0, len(b) & ~7, 8):
            l, r = struct.unpack_from("<2I", b, o)
            struct.pack_into("<2I", b, o, *self.encrypt_block(l, r))
        return bytes(b)


# ---------------------------------------------------------------- NBL parsing

def cstr(b):
    return b.split(b"\0", 1)[0].decode("ascii", "backslashreplace")


def parse_records(d, base, table_off, count, bf):
    recs, o = [], base + table_off
    for _ in range(count):
        tag = d[o:o + 4]
        rsize, = struct.unpack_from("<I", d, o + 4)
        body = d[o + REC_ENC_START:o + REC_ENC_END]
        if bf:
            body = bf.decrypt(body)
        off, size, poff, psize = struct.unpack_from("<4I", body, 0x20)
        recs.append({"type": cstr(tag), "name": cstr(body[:0x20]), "off": off, "size": size,
                     "ptr_off": poff, "ptr_size": psize, "rec": o - base,
                     "tail": d[o + 0x40:o + rsize] if rsize > 0x40 else b""})
        o += rsize or 0x60
    return recs


def parse(d, erx_path=DEFAULT_ERX):
    if d[:4] != b"NMLL":
        raise SystemExit("not an NBL (no NMLL magic)")
    ver, namebuf, hsize, count, dsize, csize, ptsize, key = struct.unpack_from("<HHIIIIII", d, 4)
    th_size, t_dsize, t_csize, t_count = struct.unpack_from("<4I", d, 0x20)
    keyb = d[0x1C:0x20]
    bf = Blowfish(keyb, erx_path) if key else None
    nb = {"ver": ver, "namebuf": namebuf, "hsize": hsize, "count": count, "dsize": dsize,
          "csize": csize, "ptsize": ptsize, "key": keyb, "bf": bf,
          "tmll_hsize": th_size, "tmll_dsize": t_dsize, "tmll_csize": t_csize, "tmll_count": t_count}
    nb["members"] = parse_records(d, 0, 0x30, count, bf)
    nb["data_off"] = align(hsize)
    nb["data_len"] = csize or dsize
    nb["ptr_off"] = align(nb["data_off"] + nb["data_len"])
    nb["tmll_off"] = None
    nb["tmll"] = []
    if t_dsize:
        t = align(nb["ptr_off"] + ptsize)
        if d[t:t + 4] != b"TMLL":
            print(f"warning: TMLL expected at {t:#x}, found {d[t:t + 4]!r}", file=sys.stderr)
        else:
            nb["tmll_off"] = t
            h, c, tds, tcs = struct.unpack_from("<4I", d, t + 8)
            nb["tmll_hsize"], nb["tmll_count"], nb["tmll_dsize"], nb["tmll_csize"] = h, c, tds, tcs
            nb["tmll"] = parse_records(d, t, 0x20, c, bf)
            nb["tmll_data_off"] = t + align(h)
    return nb


def section_data(d, off, length, dsize, compressed, bf):
    raw = d[off:off + length]
    if bf:
        raw = bf.decrypt(raw)
    if compressed:
        out = prs.decompress(raw)
        if len(out) != dsize:
            print(f"warning: PRS gave {len(out):#x} bytes, header says {dsize:#x}", file=sys.stderr)
        return out
    return raw


def nbl_data(d, nb):
    return section_data(d, nb["data_off"], nb["data_len"], nb["dsize"], nb["csize"] != 0, nb["bf"])


def tmll_data(d, nb):
    if nb["tmll_off"] is None:
        return b""
    ln = nb["tmll_csize"] or nb["tmll_dsize"]
    return section_data(d, nb["tmll_data_off"], ln, nb["tmll_dsize"], nb["tmll_csize"] != 0, None)


def cmd_list(path, erx_path):
    d = open(path, "rb").read()
    nb = parse(d, erx_path)
    k = nb["key"]
    print(f"{path}: NMLL v{nb['ver']}  header {nb['hsize']:#x}  members {nb['count']}  "
          f"data {nb['dsize']:#x} @ {nb['data_off']:#x}  "
          f"{'PRS ' + hex(nb['csize']) if nb['csize'] else 'uncompressed'}  "
          f"ptrtable {nb['ptsize']:#x} @ {nb['ptr_off']:#x}  "
          f"{'encrypted key ' + k.hex() if nb['bf'] else 'plain'}  namebuf {nb['namebuf']:#x}")
    print(f"  {'type':4} {'offset':>8} {'size':>8} {'ptroff':>7} {'ptrsz':>6}  name")
    for m in nb["members"]:
        print(f"  {m['type']:4} {m['off']:8x} {m['size']:8x} {m['ptr_off']:7x} {m['ptr_size']:6x}  {m['name']}")
    if nb["tmll_off"] is not None:
        print(f"  TMLL @ {nb['tmll_off']:#x}: members {nb['tmll_count']}  data {nb['tmll_dsize']:#x} @ "
              f"{nb['tmll_data_off']:#x}  {'PRS ' + hex(nb['tmll_csize']) if nb['tmll_csize'] else 'uncompressed'}"
              f"  (records encrypted, data plain)")
        for m in nb["tmll"]:
            print(f"  {m['type']:4} {m['off']:8x} {m['size']:8x} {'':7} {'':6}  {m['name']}")


def safe_name(name, used):
    n = "".join(c if c.isalnum() or c in "._-" else "_" for c in name) or "unnamed"
    base, i = n, 1
    while n.lower() in used:
        n = f"{base}.{i}"
        i += 1
    used.add(n.lower())
    return n


def extract_nbl(d, outdir, erx_path, quiet=False):
    nb = parse(d, erx_path)
    os.makedirs(outdir, exist_ok=True)
    data = nbl_data(d, nb)
    used = set()
    names = []
    for m in nb["members"]:
        fn = safe_name(m["name"], used)
        with open(os.path.join(outdir, fn), "wb") as f:
            f.write(data[m["off"]:m["off"] + m["size"]])
        names.append(fn)
        if not quiet:
            print(f"  {m['type']:4} {m['size']:8x}  {fn}")
    if nb["ptsize"]:
        with open(os.path.join(outdir, "_ptrtable.bin"), "wb") as f:
            f.write(d[nb["ptr_off"]:nb["ptr_off"] + nb["ptsize"]])
    if nb["tmll"]:
        tdir = os.path.join(outdir, "tmll")
        os.makedirs(tdir, exist_ok=True)
        tdata = tmll_data(d, nb)
        tused = set()
        for m in nb["tmll"]:
            fn = safe_name(m["name"], tused)
            # the record tail holds the file header: GBIX+PVRT for NNVR (+ pixel data from the
            # section), a complete PVPL palette for NNPL (0xe5b8 reads it from the record)
            body = tdata[m["off"]:m["off"] + m["size"]] if m["type"] == "NNVR" else b""
            with open(os.path.join(tdir, fn), "wb") as f:
                f.write(m["tail"] + body)
            if not quiet:
                print(f"  {m['type']:4} {m['size']:8x}  tmll/{fn}")
    return nb


# ---------------------------------------------------------------- AFS

def afs_entries(d):
    if d[:4] != b"AFS\0":
        raise SystemExit("not an AFS")
    n, = struct.unpack_from("<I", d, 4)
    ents = [struct.unpack_from("<2I", d, 8 + i * 8) for i in range(n)]
    names = [f"{i:04d}.bin" for i in range(n)]
    noff, nsize = struct.unpack_from("<2I", d, 8 + n * 8)
    if noff and nsize >= n * 0x30 and noff + nsize <= len(d):
        names = [cstr(d[noff + i * 0x30:noff + i * 0x30 + 0x20]) or names[i] for i in range(n)]
    return list(zip(names, ents))


def cmd_afs(path, outdir, do_nbl, erx_path):
    d = open(path, "rb").read()
    os.makedirs(outdir, exist_ok=True)
    used = set()
    for name, (off, size) in afs_entries(d):
        fn = safe_name(name, used)
        blob = d[off:off + size]
        with open(os.path.join(outdir, fn), "wb") as f:
            f.write(blob)
        tag = blob[:4].decode("latin-1")
        print(f"{off:8x} {size:8x}  {fn}  [{tag!r}]")
        if do_nbl and blob[:4] == b"NMLL":
            extract_nbl(blob, os.path.join(outdir, fn.rsplit(".", 1)[0] + "_nbl"), erx_path)


# ---------------------------------------------------------------- verification by emulation

def emulate_decrypt(erx_path, key, chunks):
    """Run the game's 0x156c0 (init) and 0x15740 (decrypt) under unicorn; returns decrypted chunks.

    MIPS64 mode (the code uses daddu/sd/ld); R5900 lq/sq (only 16-byte register saves in these
    routines) are rewritten to ld/sd; memcpy (0x48a9a4) is replaced by a byte-copy stub.
    """
    from unicorn import Uc, UC_ARCH_MIPS, UC_MODE_MIPS64, UC_MODE_LITTLE_ENDIAN
    from unicorn.mips_const import (UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_RA,
                                    UC_MIPS_REG_SP)
    im = erx.open_image(erx_path)
    t, dsec = im.text, im.data
    code = bytearray(im.d[t["off"]:t["off"] + t["size"]])
    for o in range(0, len(code), 4):
        w, = struct.unpack_from("<I", code, o)
        op = w >> 26
        if op in (0x1E, 0x1F):  # lq / sq -> ld / sd
            struct.pack_into("<I", code, o, (w & 0x03FFFFFF) | ((0x37 if op == 0x1E else 0x3F) << 26))
    u = Uc(UC_ARCH_MIPS, UC_MODE_MIPS64 | UC_MODE_LITTLE_ENDIAN)
    u.mem_map(0, 0x800000)
    u.mem_write(0, bytes(code))
    u.mem_write(dsec["addr"], im.d[dsec["off"]:dsec["off"] + dsec["size"]])
    STACK, CTX, KEY, BUF, STOP = 0x1000000, 0x1100000, 0x1110000, 0x2000000, 0x1200000
    total = sum(len(c) for c in chunks)
    u.mem_map(STACK - 0x10000, 0x300000)
    u.mem_map(BUF, align(max(total, 1) + 0x1000, 0x1000))
    u.mem_write(STOP, b"\0\0\0\0" * 4)

    # memcpy (0x48a9a4) -> a plain byte-copy loop placed in spare memory, so no Python hooks
    MEMCPY_STUB = 0x7F0000
    stub = [0x0080102D, 0x10C00007, 0, 0x90A80000, 0x24A50001, 0xA0880000,  # move v0,a0; beqz a2; lbu
            0x24C6FFFF, 0x14C0FFFB, 0x24840001, 0x03E00008, 0]              # sb; a2--; bnez; a0++; jr ra
    u.mem_write(MEMCPY_STUB, struct.pack(f"<{len(stub)}I", *stub))
    u.mem_write(FN_MEMCPY, struct.pack("<2I", 0x08000000 | (MEMCPY_STUB >> 2), 0))

    def call(fn, *args):
        for r, v in zip((UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2), args):
            u.reg_write(r, v)
        u.reg_write(UC_MIPS_REG_SP, STACK)
        u.reg_write(UC_MIPS_REG_RA, STOP)
        u.emu_start(fn, STOP, count=1 << 30)  # count mode: plain runs crash this unicorn build

    u.mem_write(KEY, bytes(key))
    call(FN_INIT, CTX, KEY, len(key))
    out, o = [], BUF
    for c in chunks:
        u.mem_write(o, bytes(c))
        call(FN_DECRYPT, CTX, o, len(c))
        out.append(bytes(u.mem_read(o, len(c))))
        o += len(c)
    return out


def cmd_verify(path, nbytes, erx_path):
    d = open(path, "rb").read()
    nb = parse(d, erx_path)
    if not nb["bf"]:
        raise SystemExit(f"{path}: key is 0, nothing to verify")
    recs = [(0, m["rec"]) for m in nb["members"]]
    if nb["tmll_off"] is not None:
        recs += [(nb["tmll_off"], m["rec"]) for m in nb["tmll"]]
    chunks = [d[b + r + REC_ENC_START:b + r + REC_ENC_END] for b, r in recs]
    n = nb["data_len"] if nbytes == 0 else min(nbytes, nb["data_len"])
    chunks.append(d[nb["data_off"]:nb["data_off"] + n])
    emu = emulate_decrypt(erx_path, nb["key"], chunks)
    py = [nb["bf"].decrypt(c) for c in chunks]
    bad = [i for i, (a, b) in enumerate(zip(emu, py)) if a != b]
    print(f"{path}: key {nb['key'].hex()}  {len(recs)} records x 0x30 + data {n:#x} bytes: "
          f"game code vs Python {'MATCH' if not bad else 'MISMATCH in chunks ' + str(bad)}")
    print(f"  first record (game): {emu[0].hex(' ')}")
    print(f"  data[:16]   (game): {emu[-1][:16].hex(' ')}")
    return not bad


def _opt(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        v = argv[i + 1]
        del argv[i:i + 2]
        return v
    return default


def main():
    argv = sys.argv[1:]
    erx_path = _opt(argv, "--erx") or workdir.any_erx()
    nbytes = int(_opt(argv, "--bytes", "0x10000"), 0)
    do_nbl = "--nbl" in argv
    argv = [a for a in argv if a != "--nbl"]
    if len(argv) < 2:
        raise SystemExit(__doc__)
    cmd = argv[0]
    if cmd == "list":
        cmd_list(argv[1], erx_path)
    elif cmd == "x" and len(argv) >= 3:
        extract_nbl(open(argv[1], "rb").read(), argv[2], erx_path)
    elif cmd == "afs" and len(argv) >= 3:
        cmd_afs(argv[1], argv[2], do_nbl, erx_path)
    elif cmd == "verify":
        sys.exit(0 if cmd_verify(argv[1], nbytes, erx_path) else 1)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
