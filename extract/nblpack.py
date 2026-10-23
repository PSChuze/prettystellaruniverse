"""NBL packer: an NBL <-> an editable directory (nbl.json + one file per member), byte-exact both ways.

  python nblpack.py unpack <file.nbl> <dir> [--decode]   members + nbl.json (--decode: also <member>.json for the
                                                       kinds relfmt.py knows, which pack then prefers)
  python nblpack.py pack   <dir> <out.nbl> [--fresh]    rebuild (--fresh: never reuse the cached PRS stream)
  python nblpack.py check  <file.nbl>... [--fresh]      unpack + pack in memory, compare (exit 1 on a difference;
                                                       --fresh: also without the PRS cache, compare decompressed)

Layout as nbl.py documents it (NMLL header, member records, data section, pointer table, optional TMLL).
What the writer derives (nbl.json only stores a value where the disc file differs from the rule):
  header     "NMLL", u16 2, u16 name-buffer size = sum(len(name) + 1), header size 0x30 + sum(record sizes)
  records    tag, u32 0x60 (the last record may hold 0: "rsize"), name, offset, size, pointer-table offset/size,
             +0x40 info chunk: STD members 32 zero bytes, others "NSIF" 0x18 1 0x20 size size+0x20
             align16(ptrs)+0x10 1 ("nsif" overrides the 32 bytes)
  data       members in record order, each at the next multiple of "align" (0x80 on the discs; "off" overrides),
             zero filled; the section ends at a multiple of "end_align" (0x20 or 0x80); PRS when "compressed"
             (a member-sized archive below 0x800 B is stored raw on the discs), Blowfish (nbl.Blowfish) when "key"
  pointers   per member in record order (members without pointers take the running position). On disc a slot
             and the value in it are data-section offsets (member offset + file offset); here they are made
             member-relative: "ptrs" lists file offsets and the member file holds file offsets, so a member can
             move or be rebuilt on its own. "ptrs_abs" keeps both absolute where a slot lies outside its member
             (only seen in archives a buggy patch tool wrote).
  TMLL       texture section ("tmll"): records like the main ones (tags NNVR / NNPL, "head" = the record tail: the
             GBIX/PVRT texture header or a whole PVPL palette), data section PRS, never encrypted.
PRS: Sega's compressor is not reproduced (prsenc.py is greedy), so unpack stores the disc's compressed stream
(_data.prs / tmll/_data.prs) with the SHA-1 of what it decompresses to; pack reuses it while the data section is
unchanged, else compresses afresh. A rebuilt archive is byte-identical with the cache and has identical members,
records, pointer table and decompressed data without it (only the PRS stream and csize differ).
Keys: unpack records the archive's key (re-encrypted on pack for an exact rebuild); new archives use key 0
(plain), which both clients accept (the server already sends key-0 archives).
"""
import hashlib
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nbl  # noqa: E402
import prs  # noqa: E402
from prsenc import compress  # noqa: E402

REC = 0x60
ZERO_INFO = bytes(32)


def align(x, a):
    return (x + a - 1) // a * a


def nsif(size, psize):
    return struct.pack("<4s7I", b"NSIF", 0x18, 1, 0x20, size, size + 0x20, align(psize, 16) + 0x10, 1)


def default_info(typ, size, psize):
    return ZERO_INFO if typ == "STD" else nsif(size, psize)


def sha1(b):
    return hashlib.sha1(b).hexdigest()


def _bf(key):
    return nbl.Blowfish(key) if key and key != bytes(4) else None


# ---------------------------------------------------------------- unpack

def _rec(d, o, bf):
    body = d[o + 0x10:o + 0x40]
    if bf:
        body = bf.decrypt(body)
    rsize, = struct.unpack_from("<I", d, o + 4)
    off, size, poff, psize = struct.unpack_from("<4I", body, 0x20)
    return {"tag": d[o:o + 4], "rsize": rsize, "rec8": d[o + 8:o + 0x10], "name": body[:0x20],
            "off": off, "size": size, "poff": poff, "psize": psize,
            "tail": d[o + 0x40:o + (rsize or REC)]}


def _layout_align(members, size_key="size"):
    """The member alignment that explains the most offsets (0x80 on the discs)."""
    best = None
    for a in (0x80, 0x20, 0x100, 0x800, 0x10, 4, 1):
        pos, hits = 0, 0
        for m in members:
            hits += m["off"] == align(pos, a)
            pos = max(pos, m["off"] + m[size_key])
        if best is None or hits > best[1]:
            best = (a, hits)
    return best[0]


def unpack(d):
    """-> (manifest, {relative path: bytes}). Exact inverse of pack()."""
    if d[:4] != b"NMLL":
        raise ValueError("not an NBL")
    ver, namebuf, hsize, count, dsize, csize, ptsize = struct.unpack_from("<HHIIIII", d, 4)
    key = d[0x1C:0x20]
    bf = _bf(key)
    recs, o = [], 0x30
    for _ in range(count):
        r = _rec(d, o, bf)
        recs.append(r)
        o += r["rsize"] or REC
    data_off = align(hsize, 0x800)
    raw = d[data_off:data_off + (csize or dsize)]
    if bf:
        raw = bf.decrypt(raw)
    data = prs.decompress(raw) if csize else raw
    ptr_off = align(data_off + (csize or dsize), 0x800)
    pt = d[ptr_off:ptr_off + ptsize]

    man = {"nbl": ver, "key": key.hex() if bf else None, "compressed": bool(csize)}
    files, used = {}, set()
    mlist = [{"name": nbl.cstr(r["name"]), "off": r["off"], "size": r["size"]} for r in recs]
    man["align"] = _layout_align(mlist)
    pos, members = 0, []
    names = [m["name"] for m in mlist]
    for i, r in enumerate(recs):
        name = names[i]
        m = {"name": name, "type": nbl.cstr(r["tag"])}
        if r["tag"] != m["type"].encode("ascii").ljust(4, b"\0"):
            m["tag"] = r["tag"].hex()
        if r["name"] != name.encode("ascii").ljust(0x20, b"\0"):
            m["name_raw"] = r["name"].hex()
        body = bytearray(data[r["off"]:r["off"] + r["size"]])
        slots = list(struct.unpack_from(f"<{r['psize'] // 4}I", pt, r["poff"])) if r["psize"] else []
        if all(r["off"] <= s <= r["off"] + r["size"] - 4 for s in slots):
            rel = [s - r["off"] for s in slots]
            for s in rel:
                v, = struct.unpack_from("<I", body, s)
                struct.pack_into("<I", body, s, (v - r["off"]) & 0xFFFFFFFF)
            if slots:
                m["ptrs"] = rel
        else:
            m["ptrs_abs"] = slots
        fn = nbl.safe_name(name, used)
        files[fn] = bytes(body)
        m["file"] = fn
        if r["off"] != align(pos, man["align"]):
            m["off"] = r["off"]
        pos = max(pos, r["off"] + r["size"])
        if r["tail"] != default_info(m["type"], r["size"], r["psize"]):
            m["info"] = r["tail"].hex()
        if r["rsize"] != REC:
            m["rsize"] = r["rsize"]
        if r["rec8"] != bytes(8):
            m["rec8"] = r["rec8"].hex()
        members.append(m)
    man["members"] = members
    for a in (0x20, 0x80, 0x10, 0x100, 0x800):
        if dsize == align(pos, a):
            man["end_align"] = a
            break
    else:
        man["dsize"] = dsize
    # pointer table: record order with running offsets, else explicit
    run = 0
    for m, r in zip(members, recs):
        if r["poff"] != run:
            man["ptr_layout"] = [[r["poff"], r["psize"]] for r in recs]
            break
        run += r["psize"]
    if run != ptsize and "ptr_layout" not in man:
        man["ptr_layout"] = [[r["poff"], r["psize"]] for r in recs]
    if "ptr_layout" in man:
        man["ptr_table"] = "_ptrtable.bin"
        files["_ptrtable.bin"] = pt
    if namebuf != sum(len(n) + 1 for n in names):
        man["namebuf"] = namebuf
    if hsize != 0x30 + sum(r["rsize"] or REC for r in recs) - (REC - 0 if recs and recs[-1]["rsize"] == 0 else 0) \
            and hsize != 0x30 + REC * count:
        man["hsize"] = hsize
    if csize:
        files["_data.prs"] = raw
        man["prs"] = {"file": "_data.prs", "sha1": sha1(data)}
    if any(d[hsize:data_off]):
        man["hdr_pad"] = d[hsize:data_off].hex()
    end = ptr_off + ptsize
    t_dsize, = struct.unpack_from("<I", d, 0x24)
    if t_dsize:
        t = align(end, 0x800) if ptsize else ptr_off
        man["tmll"], tfiles = _unpack_tmll(d, t, bf)
        files.update({"tmll/" + k: v for k, v in tfiles.items()})
        th = d[0x20:0x30]
        if th != struct.pack("<4I", *struct.unpack_from("<I", d, t + 8), t_dsize,
                             struct.unpack_from("<I", d, t + 0x14)[0], struct.unpack_from("<I", d, t + 0xC)[0]):
            man["hdr20"] = th.hex()
        end = _tmll_end(d, t)
    elif d[0x20:0x30] != bytes(16):
        man["hdr20"] = d[0x20:0x30].hex()
    # what follows the pointer table (or data) up to the file end
    if len(d) != align(end, 0x800) or any(d[end:]):
        man["trailer"] = "_trailer.bin"            # whatever follows the last section, when not 0x800 zero padding
        files["_trailer.bin"] = d[end:]
    return man, files


def _tmll_end(d, t):
    h, c, ds, cs = struct.unpack_from("<4I", d, t + 8)
    return t + align(h, 0x800) + (cs or ds)


def _unpack_tmll(d, t, bf):
    ver, namebuf, h, c, ds, cs = struct.unpack_from("<HHIIII", d, t + 4)
    recs, o = [], t + 0x20
    for _ in range(c):
        r = _rec(d, o, bf)
        recs.append(r)
        o += r["rsize"] or REC
    raw = d[t + align(h, 0x800):t + align(h, 0x800) + (cs or ds)]
    data = prs.decompress(raw) if cs else raw
    man = {"compressed": bool(cs), "ver": ver}
    files, used = {}, set()
    owners = {}
    for r in recs:                       # the first record at an offset owns the bytes (NNPL point at 0, size 0x40)
        if r["tag"][:4] == b"NNVR":
            owners.setdefault(r["off"], r)
    ml = [r for r in recs if owners.get(r["off"]) is r]
    a = _layout_align([{"off": r["off"], "size": r["size"]} for r in ml])
    man["align"] = a
    pos, members = 0, []
    for r in recs:
        name = nbl.cstr(r["name"])
        m = {"name": name, "type": nbl.cstr(r["tag"])}
        if r["name"] != name.encode("ascii").ljust(0x20, b"\0"):
            m["name_raw"] = r["name"].hex()
        fn = nbl.safe_name(name, used)
        files[fn + ".head"] = r["tail"]
        m["head"] = fn + ".head"
        if owners.get(r["off"]) is r:
            files[fn] = data[r["off"]:r["off"] + r["size"]]
            m["file"] = fn
            if r["off"] != align(pos, a):
                m["off"] = r["off"]
            pos = max(pos, r["off"] + r["size"])
        else:
            m["off"], m["size"] = r["off"], r["size"]
        if r["poff"] or r["psize"]:
            m["ptr"] = [r["poff"], r["psize"]]
        if r["rsize"] != REC + len(r["tail"]) - 0x20:
            m["rsize"] = r["rsize"]
        if r["rec8"] != bytes(8):
            m["rec8"] = r["rec8"].hex()
        members.append(m)
    man["members"] = members
    if ds != align(pos, 0x20):
        man["dsize"] = ds
    if namebuf:
        man["namebuf"] = namebuf
    if d[t + 0x18:t + 0x20] != bytes(8):
        man["hdr18"] = d[t + 0x18:t + 0x20].hex()
    if h != 0x20 + sum(0x40 + len(r["tail"]) for r in recs):
        man["hsize"] = h
    if any(d[t + h:t + align(h, 0x800)]):
        man["hdr_pad"] = d[t + h:t + align(h, 0x800)].hex()
    if cs:
        files["_data.prs"] = raw
        man["prs"] = {"file": "_data.prs", "sha1": sha1(data)}
    return man, files


# ---------------------------------------------------------------- pack

def _record(tag, name, off, size, poff, psize, info, rsize, rec8, bf):
    body = bytearray(name.ljust(0x20, b"\0")[:0x20] + struct.pack("<4I", off, size, poff, psize))
    if bf:
        body = bytearray(bf.encrypt(bytes(body)))
    out = tag + struct.pack("<I", rsize) + rec8 + bytes(body) + info
    return out


def _section(data, compressed, cache, files, fresh, bf):
    if compressed:
        if cache and not fresh and cache.get("sha1") == sha1(data) and cache["file"] in files:
            stream = files[cache["file"]]
        else:
            stream = compress(data)
        csize = len(stream)
    else:
        stream, csize = data, 0
    if bf:
        stream = bf.encrypt(stream)
    return stream, csize


def member_bytes(m, files):
    """(bytes, member-relative pointer slots) for a manifest member: a <file>.json (relfmt) wins over the file."""
    js = m.get("json")
    if js and js in files:
        import relfmt
        return relfmt.encode(json.loads(files[js]))
    return files[m["file"]], m.get("ptrs", [])


def pack(man, files, fresh=False):
    key = bytes.fromhex(man["key"]) if man.get("key") else bytes(4)
    bf = _bf(key)
    a = man.get("align", 0x80)
    data, layout, pos = bytearray(), [], 0
    ptrs = bytearray()
    for m in man["members"]:
        body, rel = member_bytes(m, files)
        off = m.get("off", align(pos, a))
        if off < pos:
            raise ValueError(f"{m['name']}: offset {off:#x} overlaps the member before (ends {pos:#x})")
        if len(data) < off:
            data += bytes(off - len(data))
        body = bytearray(body)
        if "ptrs_abs" in m:
            slots = m["ptrs_abs"]
        else:
            for s in rel:
                v, = struct.unpack_from("<I", body, s)
                struct.pack_into("<I", body, s, (v + off) & 0xFFFFFFFF)
            slots = [s + off for s in rel]
        data[off:off + len(body)] = body
        pos = max(pos, off + len(body))
        layout.append((off, len(body), slots))
    dsize = man.get("dsize", align(pos, man.get("end_align", 0x80)))
    data += bytes(dsize - len(data))
    data = bytes(data[:dsize])
    if "ptr_layout" in man:
        pt = files[man["ptr_table"]]
        playout = man["ptr_layout"]
    else:
        playout = []
        for off, size, slots in layout:
            playout.append([len(ptrs), 4 * len(slots)])
            ptrs += struct.pack(f"<{len(slots)}I", *slots)
        pt = bytes(ptrs)
    stream, csize = _section(data, man.get("compressed", True), man.get("prs"), files, fresh, bf)

    recs = bytearray()
    names = []
    for i, (m, (off, size, slots)) in enumerate(zip(man["members"], layout)):
        name = bytes.fromhex(m["name_raw"]) if "name_raw" in m else m["name"].encode("ascii")
        names.append(m["name"])
        tag = bytes.fromhex(m["tag"]) if "tag" in m else m["type"].encode("ascii").ljust(4, b"\0")
        poff, psize = playout[i]
        info = bytes.fromhex(m["info"]) if "info" in m else default_info(m["type"], size, psize)
        rsize = m.get("rsize", REC)
        recs += _record(tag, name, off, size, poff, psize, info, rsize,
                        bytes.fromhex(m["rec8"]) if "rec8" in m else bytes(8), bf)
    hsize = man.get("hsize", 0x30 + len(recs))
    namebuf = man.get("namebuf", sum(len(n) + 1 for n in names))
    tm = man.get("tmll")
    tsec = _pack_tmll(tm, files, fresh, bf) if tm else None
    hdr20 = bytes.fromhex(man["hdr20"]) if "hdr20" in man else (
        struct.pack("<4I", tsec[1], tsec[2], tsec[3], tsec[4]) if tsec else bytes(16))
    hdr = (b"NMLL" + struct.pack("<HHIIIII", man.get("nbl", 2), namebuf, hsize, len(man["members"]), dsize, csize,
                                 len(pt)) + key + hdr20 + bytes(recs))
    out = bytearray(hdr)
    out += bytes.fromhex(man["hdr_pad"]) if "hdr_pad" in man else bytes(align(len(out), 0x800) - len(out))
    out += stream
    if pt:
        out += bytes(align(len(out), 0x800) - len(out))
        out += pt
    if tsec:
        out += bytes(align(len(out), 0x800) - len(out))
        out += tsec[0]
    if "trailer" in man:
        out += files[man["trailer"]]
    else:
        out += bytes(align(len(out), 0x800) - len(out))
    return bytes(out)


def _pack_tmll(tm, files, fresh, bf):
    a = tm.get("align", 0x80)
    data, pos, lay = bytearray(), 0, []
    for m in tm["members"]:
        if "file" in m:
            body = files["tmll/" + m["file"]]
            off = m.get("off", align(pos, a))
            if len(data) < off:
                data += bytes(off - len(data))
            data[off:off + len(body)] = body
            pos = max(pos, off + len(body))
            lay.append((off, len(body)))
        else:
            lay.append((m["off"], m["size"]))
    dsize = tm.get("dsize", align(pos, 0x20))
    data = bytes(data) + bytes(dsize - len(data))
    stream, csize = _section(data[:dsize], tm.get("compressed", True), tm.get("prs"),
                             {k[5:]: v for k, v in files.items() if k.startswith("tmll/")}, fresh, None)
    recs = bytearray()
    for m, (off, size) in zip(tm["members"], lay):
        head = files["tmll/" + m["head"]]
        name = bytes.fromhex(m["name_raw"]) if "name_raw" in m else m["name"].encode("ascii")
        poff, psize = m.get("ptr", [0, 0])
        rsize = m.get("rsize", REC + len(head) - 0x20)
        recs += _record(m["type"].encode("ascii").ljust(4, b"\0"), name, off, size, poff, psize, head, rsize,
                        bytes.fromhex(m["rec8"]) if "rec8" in m else bytes(8), bf)
    h = tm.get("hsize", 0x20 + len(recs))
    out = bytearray(b"TMLL" + struct.pack("<HHIIII", tm.get("ver", 2), tm.get("namebuf", 0), h, len(tm["members"]),
                                          dsize, csize))
    out += bytes.fromhex(tm["hdr18"]) if "hdr18" in tm else bytes(8)
    out += recs
    out += bytes.fromhex(tm["hdr_pad"]) if "hdr_pad" in tm else bytes(align(len(out), 0x800) - len(out))
    out += stream
    return bytes(out), h, dsize, csize, len(tm["members"])


# ---------------------------------------------------------------- helpers for other tools

class Archive:
    """Edit an NBL by member: a = Archive(bytes); a.get(name) -> (bytes, member-relative slots);
    a.put(name, bytes, slots); a.add(name, type, bytes, slots); a.build() -> NBL bytes (key 0, plain, by default:
    what the other patch tools write and both clients load). Members after a changed one move; their pointers
    and pointer-table slots follow (they are member-relative here)."""

    def __init__(self, d):
        self.man, self.files = unpack(d)

    def member(self, name):
        return next(m for m in self.man["members"] if m["name"] == name)

    def names(self):
        return [m["name"] for m in self.man["members"]]

    def get(self, name):
        return member_bytes(self.member(name), self.files)

    def _changed(self, m):
        ms = self.man["members"]
        for x in ms[ms.index(m):]:
            if "ptrs_abs" in x:
                raise ValueError(f"{x['name']}: pointer slots outside the member, cannot move it")
            x.pop("off", None)
            if x.get("info") and x["type"] != "STD":
                x.pop("info")                         # back to the derived NSIF (sizes may differ now)
        for k in ("ptr_layout", "ptr_table", "dsize", "trailer"):
            self.man.pop(k, None)

    def put(self, name, body, slots=None):
        m = self.member(name)
        m.pop("json", None)
        self.files[m["file"]] = bytes(body)
        if slots is not None:
            m.pop("ptrs", None)
            if slots:
                m["ptrs"] = sorted(slots)
        self._changed(m)

    def add(self, name, typ, body, slots=(), after=None):
        """New member (at the end, or after member `after`)."""
        used = {k.lower() for k in self.files}
        fn = nbl.safe_name(name, used)
        m = {"name": name, "type": typ, "file": fn}
        if slots:
            m["ptrs"] = sorted(slots)
        self.files[fn] = bytes(body)
        ms = self.man["members"]
        ms.insert(ms.index(self.member(after)) + 1 if after else len(ms), m)
        self.man.pop("namebuf", None)
        self.man.pop("hsize", None)
        self._changed(m)

    def build(self, key0=True, fresh=False):
        man = dict(self.man)
        if key0:
            man["key"] = None
        return pack(man, self.files, fresh=fresh)


def write_dir(man, files, out):
    import relfmt
    os.makedirs(out, exist_ok=True)
    for k, v in files.items():
        p = os.path.join(out, k)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(v)
    with open(os.path.join(out, "nbl.json"), "w", encoding="utf-8") as f:
        f.write(relfmt.dumps(man))


def read_dir(path):
    man = json.load(open(os.path.join(path, "nbl.json"), encoding="utf-8"))
    files = {}
    for root, _dirs, fns in os.walk(path):
        for fn in fns:
            if fn == "nbl.json":
                continue
            p = os.path.join(root, fn)
            files[os.path.relpath(p, path).replace(os.sep, "/")] = open(p, "rb").read()
    return man, files


def sections(d):
    """(decompressed data section, pointer table, records, decompressed TMLL data) of an NBL: what must match
    when only the PRS streams differ."""
    nb = nbl.parse(d)
    plain = [(m["name"], m["type"], m["off"], m["size"], m["ptr_off"], m["ptr_size"], m["tail"])
             for m in nb["members"] + nb["tmll"]]
    return (nbl.nbl_data(d, nb), d[nb["ptr_off"]:nb["ptr_off"] + nb["ptsize"]], plain, nbl.tmll_data(d, nb))


def check(d, fresh=False):
    """-> (exact, fresh_ok). exact: unpack + pack gives the same bytes. fresh_ok (when asked and the archive has a
    PRS section): packing without the cached streams gives the same records, pointer table and decompressed data."""
    man, files = unpack(d)
    exact = pack(man, files) == d
    if not fresh or not (man.get("compressed") or (man.get("tmll") or {}).get("compressed")):
        return exact, None
    return exact, sections(pack(man, files, fresh=True)) == sections(d)


def main():
    a = sys.argv[1:]
    if len(a) >= 3 and a[0] == "unpack":
        man, files = unpack(open(a[1], "rb").read())
        if "--decode" in a:
            import relfmt
            relfmt.decode_members(man, files)
        write_dir(man, files, a[2])
        print(f"{a[1]}: {len(man['members'])} members -> {a[2]}")
    elif len(a) >= 3 and a[0] == "pack":
        man, files = read_dir(a[1])
        out = pack(man, files, fresh="--fresh" in a)
        open(a[2], "wb").write(out)
        print(f"{a[1]} -> {a[2]} ({len(out)} B)")
    elif len(a) >= 2 and a[0] == "check":
        bad = 0
        for p in [x for x in a[1:] if not x.startswith("--")]:
            ok, fresh = check(open(p, "rb").read(), fresh="--fresh" in a)
            bad += not ok or fresh is False
            print(f"{p}: {'identical' if ok else 'DIFFERS'}" + ("" if fresh is None else
                  f", fresh PRS: {'same members and data' if fresh else 'DIFFERS'}"))
        sys.exit(1 if bad else 0)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
