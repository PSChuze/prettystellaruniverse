"""CRI CVM (ROFS) archive lister/extractor for PSUROFS.CVM, with the ROFS directory descrambler.

Layout of PSUROFS.CVM (ROFSROFSBLD Ver.1.52):
  0x0000  CVMH header (BE fields; u32 at 0x30 bit 0x10 = image is scrambled)
  0x0800  ZONE sector
  0x1000  sector table
  0x1800  ISO9660 image. LBAs in its PVD and directory records are relative to this point.
The volume descriptors (LBA 16..) and every directory extent are scrambled; file data is not.

Descrambler (CRI "RSU Ver.1.10", PSU_US.erx):
  0x3230d8  RFMAI descramble hook: 0x326dd8(buf, nsec*0x800, 0x800, lba, vol+0x24 key, 8)
  0x3269e0  block loop. Per 0x800 sector with ISO LBA `lba`, t = key[5]; per 8-byte chunk at off:
              seed = t*lba; seed += seed << 20                    (u32)
              a, b, c = H(be32(seed))                              0x3267a8 / hash 0x326360
              type = a % 9; s = be16(b) + be16(c)
              ks = PERM[type](key, s)                              9 routines, table at 0x59a1a8
              data[off:off+8] ^= ks; t = type + off; t *= each ks byte (u32)
  0x326360  H(bytes): three accumulators starting at P[0x100], P[0x200], P[0x300];
              for each signed byte c: acc = P[(acc * P[0x80 + c]) % 1024]. The table at 0x564088
              is P = the 1024 consecutive primes after 16384 (16411 .. 26681), generated here.
  0x326c58  key setup: password string if given (0x326888), else CVM header bytes (0x326830).
  0x326888  password -> key: t = 0; for i: t = (t + p[i]) * p[i]; t += sum(p[i+1:]) (signed chars);
              t += t << 20; w = be32(t); key = w0 w3 w1 w2 w2 w1 w3 w0; then 0x326720:
              each key byte pair k[i:i+2] = be16(H(k[i:i+2]).a).
  Password: a 16-char string at 0x4e0b40 (pointer at 0x56750c), passed with "CDV:PSUROFS.CVM"
  to the volume mount at 0x4840 (called from 0x20df84). AotI has its own (string 0x538570, pointer
  0x5cb15c). It is Sega key material, so it is not written here: it is read from the game module at
  run time (--erx; by default the psu and aoti modules under the work directory are tried, see
  workdir.py, and the password that descrambles the disc's volume descriptor is used).

Usage:
  python cvm.py list <iso-or-cvm> [--erx PSU_US.erx | --password PW] [--cvm NAME]
  python cvm.py x    <iso-or-cvm> <outdir> [--match SUBSTR] [--all] [--erx ... | --password PW] [--cvm NAME]
  python cvm.py sector <iso-or-cvm> <lba>          hexdump a descrambled ISO sector

<iso-or-cvm> is the whole disc image (PSUROFS.CVM is found through the disc's ISO9660 root;
--cvm picks another .CVM) or a standalone .cvm file. list prints LBA (ISO9660 sector inside the
CVM image), size and path. x needs --match (case-insensitive substring of the path, may repeat)
or --all. --password '' uses the header-derived key instead of the PSU password.
"""
import os
import struct
import sys

SECTOR = 2048
ISO_BASE = 0x1800
PASSWORD_PTRS = {"psu": 0x56750C, "aoti": 0x5CB15C}   # .data pointer to the CVM password string per PSU_US.erx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import workdir  # noqa: E402

workdir.work()


def password_from_erx(path):
    """The string the game passes to the ROFS volume mount, read from the unpacked game module: the first
    known pointer that leads to a 16-character password."""
    import erx
    im = erx.open_image(path)
    for va in PASSWORD_PTRS.values():
        o = im.va2off(va)
        if o is None:
            continue
        ptr, = struct.unpack_from("<I", im.d, o)
        o = im.va2off(ptr)
        if o is None or b"\0" not in im.d[o:o + 17]:
            continue
        pw = im.d[o:im.d.index(b"\0", o)]
        if len(pw) == 16 and pw.isalnum():
            return pw
    raise SystemExit(f"{path}: no CVM password at a known pointer")


def _primes_after(start, count):
    limit = start + 16 * count
    sieve = bytearray([1]) * limit
    sieve[0:2] = b"\0\0"
    for i in range(2, int(limit ** 0.5) + 1):
        if sieve[i]:
            sieve[i * i::i] = bytes(len(range(i * i, limit, i)))
    return [i for i in range(start + 1, limit) if sieve[i]][:count]


P = _primes_after(16384, 1024)

# The 9 keystream builders (0x326e80 + 0x68*n): output byte j = key[k] (op None),
# key[k] ^ s[i] ('x') or key[k] + s[i] ('a'). Transcribed from the routines' lbu/xor/addu/sb.
PERM = (
    ((3, 0, "x"), (0, 0, None), (7, 3, "x"), (4, 0, None), (1, 0, None), (6, 2, "x"), (2, 0, None), (5, 1, "x")),
    ((2, 1, "x"), (7, 0, None), (5, 0, None), (3, 2, "x"), (0, 0, "x"), (6, 0, None), (4, 0, None), (1, 3, "x")),
    ((1, 0, None), (7, 2, "x"), (6, 0, None), (2, 1, "x"), (5, 3, "x"), (3, 0, None), (0, 0, "x"), (4, 0, None)),
    ((3, 2, "a"), (6, 0, None), (0, 0, None), (2, 0, None), (4, 0, "a"), (1, 1, "a"), (7, 0, None), (5, 3, "a")),
    ((7, 0, None), (0, 3, "a"), (2, 0, "a"), (6, 1, "a"), (4, 0, None), (3, 0, None), (5, 0, None), (1, 2, "a")),
    ((2, 0, None), (3, 2, "a"), (6, 0, None), (7, 0, "a"), (0, 0, None), (1, 1, "a"), (4, 0, None), (5, 3, "a")),
    ((3, 0, "a"), (7, 0, None), (2, 1, "x"), (6, 0, None), (1, 0, None), (5, 2, "x"), (0, 0, None), (4, 3, "a")),
    ((7, 0, None), (4, 3, "x"), (3, 0, None), (1, 2, "a"), (0, 0, None), (2, 0, None), (5, 1, "a"), (6, 0, "x")),
    ((3, 0, None), (0, 1, "x"), (6, 0, None), (4, 0, "a"), (2, 3, "x"), (7, 0, None), (1, 0, None), (5, 2, "a")),
)


def _sch(c):
    return c - 256 if c > 127 else c


def rsu_hash(data):
    """0x326360: three 16-bit hashes of a byte string (bytes read as signed chars)."""
    idx = [0x80 + _sch(c) for c in data]
    out = []
    for start in (0x100, 0x200, 0x300):
        acc = P[start]
        for i in idx:
            acc = P[(acc * P[i]) % 1024]
        out.append(acc)
    return out


def _mix_pairs(k):
    """0x326720: replace each byte pair with the big-endian first hash of that pair."""
    for i in range(0, 8, 2):
        a = rsu_hash(k[i:i + 2])[0]
        k[i:i + 2] = struct.pack(">H", a & 0xFFFF)
    return bytes(k)


def key_from_password(pw):
    """0x326888."""
    c = [_sch(x) for x in pw]
    t = 0
    for i in range(len(c)):
        t = ((t + c[i]) * c[i]) & 0xFFFFFFFF
        t = (t + sum(c[i + 1:])) & 0xFFFFFFFF
    t = (t + (t << 20)) & 0xFFFFFFFF
    w = struct.pack(">I", t)
    k = bytearray(8)
    for i in range(4):
        k[2 * i], k[2 * i + 1] = w[i], w[3 - i]
    return _mix_pairs(k)


def key_from_header(hdr):
    """0x326830: used by ROFS when no password is given (not the case for PSU)."""
    if not struct.unpack_from(">I", hdr, 0x30)[0] & 0x10:
        return None
    k = bytearray(8)
    for i in range(4):
        k[2 * i], k[2 * i + 1] = hdr[0x25 + i], hdr[0x20 + i]
    return _mix_pairs(k)


def descramble(buf, lba, key):
    """0x3269e0 with blocksize 0x800: buf holds whole sectors starting at ISO LBA lba."""
    out = bytearray(buf)
    for blk in range(len(out) // SECTOR):
        sec = lba + blk
        base = blk * SECTOR
        t = key[5]
        for off in range(0, SECTOR, 8):
            seed = (t * sec) & 0xFFFFFFFF
            seed = (seed + (seed << 20)) & 0xFFFFFFFF
            a, b, c = rsu_hash(struct.pack(">I", seed))
            typ = a % 9
            s = struct.pack(">HH", b & 0xFFFF, c & 0xFFFF)
            t = typ + off
            p = base + off
            for j, (ki, si, op) in enumerate(PERM[typ]):
                v = key[ki]
                if op == "x":
                    v ^= s[si]
                elif op == "a":
                    v = (v + s[si]) & 0xFF
                out[p + j] ^= v
                t = (t * v) & 0xFFFFFFFF
    return bytes(out)


def iter_dir(d):
    """Yield (name, lba, size, is_dir) from ISO9660 directory extent bytes."""
    i = 0
    while i < len(d):
        n = d[i]
        if n == 0:
            i = (i // SECTOR + 1) * SECTOR
            continue
        r = d[i:i + n]
        i += n
        lba, = struct.unpack_from("<I", r, 2)
        size, = struct.unpack_from("<I", r, 10)
        flags, nl = r[25], r[32]
        name = r[33:33 + nl]
        if name in (b"\0", b"\1"):
            continue
        yield name.decode("latin1").split(";")[0], lba, size, bool(flags & 2)


def find_on_disc(f, want):
    """Locate a file in the outer disc's ISO9660 tree; returns (byte offset, size) or None."""
    f.seek(16 * SECTOR)
    pvd = f.read(SECTOR)
    if pvd[1:6] != b"CD001":
        return None
    root = pvd[156:190]
    stack = [(struct.unpack_from("<I", root, 2)[0], struct.unpack_from("<I", root, 10)[0])]
    while stack:
        lba, size = stack.pop()
        f.seek(lba * SECTOR)
        for name, elba, esz, is_dir in iter_dir(f.read(size)):
            if is_dir:
                stack.append((elba, esz))
            elif name.upper() == want.upper():
                return elba * SECTOR, esz
    return None


class Cvm:
    def __init__(self, path, password, cvm_name="PSUROFS.CVM"):
        self.f = open(path, "rb")
        head = self.f.read(4)
        if head == b"CVMH":
            self.base = 0
        else:
            hit = find_on_disc(self.f, cvm_name)
            if not hit:
                raise SystemExit(f"{path}: not a CVM and no {cvm_name} in its ISO9660 tree")
            self.base = hit[0]
        self.f.seek(self.base)
        self.hdr = self.f.read(SECTOR)
        if self.hdr[:4] != b"CVMH":
            raise SystemExit("no CVMH header")
        self.scrambled = bool(struct.unpack_from(">I", self.hdr, 0x30)[0] & 0x10)
        self.key = key_from_password(password) if password else key_from_header(self.hdr)
        self.iso = self.base + ISO_BASE
        pvd = self.meta(16, 1)
        if pvd[:6] != b"\x01CD001":
            raise SystemExit(f"sector 16 does not descramble to a PVD: {pvd[:16].hex()} (wrong password?)")
        self.pvd = pvd
        self.volume = pvd[40:72].decode("latin1").strip()

    def raw(self, lba, count):
        self.f.seek(self.iso + lba * SECTOR)
        return self.f.read(count * SECTOR)

    def meta(self, lba, count):
        """Descriptor / directory sectors: descrambled when the header says so."""
        d = self.raw(lba, count)
        if self.scrambled and self.key:
            d = descramble(d, lba, self.key)
        return d

    def walk(self):
        """Yield (path, lba, size, is_dir) depth first, in directory order."""
        root = self.pvd[156:190]
        todo = [("", struct.unpack_from("<I", root, 2)[0], struct.unpack_from("<I", root, 10)[0])]
        seen = set()
        while todo:
            path, lba, size = todo.pop()
            if lba in seen:
                continue
            seen.add(lba)
            subdirs = []
            for name, elba, esz, is_dir in iter_dir(self.meta(lba, (size + SECTOR - 1) // SECTOR)):
                full = f"{path}/{name}"
                yield full, elba, esz, is_dir
                if is_dir:
                    subdirs.append((full, elba, esz))
            todo.extend(reversed(subdirs))

    def read_file(self, lba, size, fh):
        self.f.seek(self.iso + lba * SECTOR)
        left = size
        while left:
            chunk = self.f.read(min(left, 1 << 20))
            if not chunk:
                raise IOError("short read")
            fh.write(chunk)
            left -= len(chunk)


def _opt(argv, name, default=None, many=False):
    vals = []
    while name in argv:
        i = argv.index(name)
        vals.append(argv[i + 1])
        del argv[i:i + 2]
    if many:
        return vals
    return vals[-1] if vals else default


def open_cvm(src, password=None, erx_path=None, cvm_name="PSUROFS.CVM"):
    """Cvm opened with an explicit password, a given module's password, or the first module under the work
    directory whose password opens this disc."""
    if password is not None:
        return Cvm(src, password.encode("latin1"), cvm_name)
    if erx_path:
        return Cvm(src, password_from_erx(erx_path), cvm_name)
    for game in workdir.GAMES:
        path = workdir.erx(game)
        if not os.path.exists(path):
            continue
        try:
            return Cvm(src, password_from_erx(path), cvm_name)
        except SystemExit:
            pass
    raise SystemExit(f"{src}: no game module under {workdir.work()} has a password that opens {cvm_name} "
                     "(unpack the disc's ERX first, or pass --erx)")


def main():
    argv = sys.argv[1:]
    pw = _opt(argv, "--password")
    erx_path = _opt(argv, "--erx")
    cvm_name = _opt(argv, "--cvm", "PSUROFS.CVM")
    matches = [m.lower() for m in _opt(argv, "--match", many=True)]
    take_all = "--all" in argv
    argv = [a for a in argv if a != "--all"]
    if len(argv) < 2:
        raise SystemExit(__doc__)
    cmd, src = argv[0], argv[1]
    c = open_cvm(src, pw, erx_path, cvm_name)
    if cmd == "list":
        print(f"# volume {c.volume!r} at disc offset {c.base:#x}, scrambled={c.scrambled}")
        nf = nd = total = 0
        for full, lba, size, is_dir in c.walk():
            if is_dir:
                nd += 1
                print(f"D lba={lba:7d} size={size:11d} {full}/")
            else:
                nf += 1
                total += size
                print(f"  lba={lba:7d} size={size:11d} {full}")
        print(f"# {nf} files, {nd} directories, {total} bytes")
    elif cmd == "x":
        if len(argv) < 3 or not (matches or take_all):
            raise SystemExit("x needs <outdir> and --match SUBSTR (or --all)")
        out = argv[2]
        n = 0
        for full, lba, size, is_dir in c.walk():
            if is_dir or (matches and not any(m in full.lower() for m in matches)):
                continue
            dst = os.path.join(out, *full.lstrip("/").split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as fh:
                c.read_file(lba, size, fh)
            n += 1
            print(f"{full} ({size} bytes)")
        print(f"# extracted {n} files to {out}")
    elif cmd == "sector":
        lba = int(argv[2], 0)
        d = c.meta(lba, 1)
        for i in range(0, SECTOR, 16):
            row = d[i:i + 16]
            txt = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
            print(f"{i:04x}  {row.hex(' ')}  {txt}")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
