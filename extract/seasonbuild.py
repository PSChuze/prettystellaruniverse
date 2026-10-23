"""Build the seasonal lobby archives the server's psu/season.py serves (disc + EGS data).

  python seasonbuild.py [--work DIR] [--nbl NBLDIR] [--season NAME ...] [--out DIR]

Inputs:
  <work>/<game>/cvm/season/XF_OB_<NAME>.NBL   the disc's 16 decoration archives (identical on both discs; psu
                                    is used when both are there):
                                    python cvm.py x <iso> <work>/<game>/cvm/season --match XF_OB_
  NBLDIR/zone-1100000-0.egs.nbl     EGS's Colony zone (egs-build.sh): its set file has one group per season
                                    and map (static models 500..575)
  NBLDIR/quest-1100000.nbl, zone-1100000-0.nbl, aoti/zone-1100000-0.nbl   the archives served now
NBLDIR defaults to $PSU_NBL_DIR. Output: DIR/<season>/quest-1100000.nbl, zone-1100000-0.nbl,
aoti/zone-1100000-0.nbl (default DIR NBLDIR/season-staging, which is where season.py looks unless
PSU_SEASON_DIR says otherwise). Nothing is served before PSU_SEASON is set.

What changes:
  quest NBL  quest.snr zone 0 record +0xB = season id (PSU's zone entry loads xf_ob_<name>.nbl from it).
  zone NBL   set_r0.rel: for each map EGS decorates (Colony 1-4, 103), the map's group table is copied to the end of
             the member with the season's EGS groups appended (their objects and params after it), the map
             entry points at the new table; the new pointer slots join the member's pointer table. The native
             groups and every other byte stay; the NSR size (+4) and the record / NSIF sizes follow.
  zone NBL   script.bin: the Colony's city BGM. The disc scripts play sound.play_bgm 40 (GUARDIANS_CITY:
             "push 0; push 40; syscall 253", PSU in cwBgm_Area_All, AotI there and in cwSound_Area_08); the 40
             becomes the season's track (SEASON_BGM, the ids EGS's Colony script picks with obj.get_eventflag;
             client table PSU 0x53b7b0, AotI 0x55bae0). Only that operand changes: no fix-up, routine or symbol.
A group is the season's when all its static models (id = model + 3000) are in that season's obj_param.snr id
list, so the assignment comes from the disc. Seasons without EGS groups (the planet festivals, fireworks) get
nothing. Output NBLs are plain (key 0) with a PRS data section, as exitpatch.py writes them.
"""
import argparse
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nbl  # noqa: E402
import prs  # noqa: E402
import workdir  # noqa: E402
from prsenc import compress  # noqa: E402

# The client's season ids (quest.snr zone +0xB) in order; server/psu/season.py keeps the same list.
SEASONS = ("party", "newyear", "valentine", "whiteday", "cherryblossoms", "easter", "parumfestival", "sonic",
           "neudaizfestival", "fireworks", "redleaves", "halloween", "moatoobfestival", "christmas", "snow",
           "wedding")

ALIGN = 0x80                    # member data offsets in these NBLs
QUEST, ZONE = 1100000, 0
CITY_BGM = 40                   # GUARDIANS_CITY, what the disc's Colony plays
SEASON_BGM = {"wedding": 44, "party": 46, "newyear": 47, "easter": 47, "valentine": 48, "whiteday": 49,
              "cherryblossoms": 50, "redleaves": 50, "halloween": 51, "christmas": 52, "snow": 52,
              "sonic": 121}     # CITY_MARRIAGE, CITY_CARNIVAL, CITY_NEWYEAR, CITY_VALENTINE (x2), CITY_BLOSSOM,
                                # CITY_HALLOWEEN, CITY_CHRISTMAS, xa_bgm014 (EGS: Sonic); the festivals and
                                # fireworks keep 40, as on EGS
BGM_SITES = {"": 1, "aoti": 2}  # play_bgm 40 sites in the Colony script: PSU, AotI


def bgm_sites(tsb):
    """[(byte offset of the 40, routine)] for each "push 0; push 40; syscall 253" in a TSB2 script, walking whole
    instructions (string operands skipped) and leaving out operands a fix-up names."""
    import tsbdis
    out = []
    for name, kind, code, words, fix in tsbdis.routines(tsb):
        ins, i = [], 0
        while i < len(words):                          # (index, op, operand index or None)
            op = words[i]
            n = tsbdis.OPS.get(op, ("", 0))[1]
            if n == -1:
                n = 1 + words[i + 1]
            ins.append((i, op, i + 1 if n == 1 else None))
            i += 1 + n
        for (_a, o1, x1), (_b, o2, x2), (_c, o3, x3) in zip(ins, ins[1:], ins[2:]):
            if (o1, o2, o3) == (0x02, 0x02, 0x61) and None not in (x1, x2, x3) and words[x1] == 0                     and words[x2] == CITY_BGM and words[x3] == 253 and x2 not in fix:
                out.append((code + 4 * x2, name))
    return out


def patch_bgm(prs, compress, script, bgm, sites):
    """script.bin (0x1C header: u32 unpacked size, u32 packed size, 0x14 zero; PRS) with the Colony's play_bgm 40
    set to `bgm`; exactly `sites` of them must be found."""
    unpacked, packed = struct.unpack_from("<II", script, 0)
    assert script[8:0x1C] == bytes(0x14), "script.bin header is not the usual one"
    tsb = bytearray(prs.decompress(script, 0x1C))
    assert len(tsb) == unpacked and tsb[:4] == b"TSB2"
    found = bgm_sites(bytes(tsb))
    if len(found) != sites:
        raise SystemExit(f"script.bin: {len(found)} play_bgm {CITY_BGM} sites, expected {sites}: {found}")
    for at, _name in found:
        struct.pack_into("<I", tsb, at, bgm)
    out = compress(bytes(tsb))
    assert prs.decompress(out) == bytes(tsb) and len(tsb) == unpacked
    return struct.pack("<II", len(tsb), len(out)) + bytes(0x14) + out


def load(nbl, path):
    d = open(path, "rb").read()
    nb = nbl.parse(d)
    return d, nb, bytes(nbl.nbl_data(d, nb))


def member(nb, name):
    return next(m for m in nb["members"] if m["name"] == name)


def season_ids(nbl, cvm):
    """{season: set of extra object ids} from each XF_OB_*.NBL's obj_param.snr ({u32 id, u32 ptr} x n before
    the root {u32 n, u32 ptr})."""
    out = {}
    for name in SEASONS:
        p = os.path.join(cvm, f"XF_OB_{name.upper()}.NBL")
        if not os.path.exists(p):
            continue
        _d, nb, data = load(nbl, p)
        m = member(nb, "obj_param.snr")
        s = data[m["off"]:m["off"] + m["size"]]
        r, = struct.unpack_from("<I", s, 8)
        n, = struct.unpack_from("<I", s, r)
        out[name] = {struct.unpack_from("<I", s, r - 8 * n + 8 * k)[0] for k in range(n)}
    return out


def set_maps(data, m):
    """[(map id, [(group header 40 B, [(object 52 B, params)])])] of a set_r*.rel member (data-relative pointers)."""
    b = m["off"]
    ab = b + struct.unpack_from("<I", data, b + 8)[0]
    _area, nm, mp = struct.unpack_from("<HHI", data, ab)
    out = []
    for mi in range(nm):
        mid, ng, gp = struct.unpack_from("<HHI", data, mp + 12 * mi)
        groups = []
        for gi in range(ng):
            g = gp + 40 * gi
            n, op = struct.unpack_from("<HI", data, g + 34)
            objs = []
            for oi in range(n):
                o = op + 52 * oi
                psz, pp = struct.unpack_from("<II", data, o + 44)
                objs.append((data[o:o + 52], data[pp:pp + psz]))
            groups.append((data[g:g + 40], objs))
        out.append((mid, groups))
    return out


def season_groups(egs_data, egs_nb, ids):
    """{season: {map id: [groups]}} from EGS's Colony set file."""
    out = {}
    for mid, groups in set_maps(egs_data, member(egs_nb, "set_r0.rel")):
        for hdr, objs in groups:
            models = {3000 + struct.unpack_from("<I", p, 0)[0] for o, p in objs
                      if struct.unpack_from("<H", o, 14)[0] == 4 and len(p) >= 4}
            who = [s for s, v in ids.items() if models and models <= v]
            if len(who) == 1:
                out.setdefault(who[0], {}).setdefault(mid, []).append((hdr, objs))
    return out


def write_nbl(nbl, prs, compress, d, nb, members_data, ptr_table):
    """Rebuild an NBL: members_data {name: bytes} in their original order and places (pushed to the next ALIGN
    when the member before grew; only members without pointers may move, as pointer values are data-relative),
    ptr_table {name: [data-relative slots]} for members whose slots changed (the others keep theirs)."""
    members = sorted(nb["members"], key=lambda m: m["off"])
    old_pt = d[nb["ptr_off"]:nb["ptr_off"] + nb["ptsize"]]
    out, offs = bytearray(), {}
    for m in members:                  # a member keeps its offset unless the one before grew past it
        at = m["off"] if len(out) <= m["off"] else len(out) + (-len(out) % ALIGN)
        if at != m["off"] and m["ptr_size"]:
            raise SystemExit(f"{m['name']} would move but holds pointers")
        out += bytes(at - len(out))
        offs[m["name"]] = at
        out += members_data[m["name"]]
    out += bytes(-len(out) % 0x20)
    if len(out) < nb["dsize"]:         # the original tail padding (data size) when nothing grew past it
        out += bytes(nb["dsize"] - len(out))
    pt, ptr = bytearray(), {}
    for m in sorted(nb["members"], key=lambda m: m["ptr_off"]):
        if m["name"] in ptr_table:
            slots = sorted(ptr_table[m["name"]])
        else:
            n = m["ptr_size"] // 4
            slots = [v - m["off"] + offs[m["name"]]
                     for v in struct.unpack_from(f"<{n}I", old_pt, m["ptr_off"])] if n else []
        ptr[m["name"]] = (len(pt), 4 * len(slots))
        pt += b"".join(struct.pack("<I", v) for v in slots)
    pdata = compress(bytes(out))
    assert prs.decompress(pdata) == bytes(out)
    hdr = bytearray(d[:nb["hsize"]])
    struct.pack_into("<IIII", hdr, 0x10, len(out), len(pdata), len(pt), 0)       # dsize, csize, ptsize, key 0
    for m in nb["members"]:
        o = m["rec"]
        body = bytearray(d[o + nbl.REC_ENC_START:o + nbl.REC_ENC_END])
        if nb["bf"]:
            body = bytearray(nb["bf"].decrypt(bytes(body)))
        size = len(members_data[m["name"]])
        struct.pack_into("<IIII", body, 0x20, offs[m["name"]], size, *ptr[m["name"]])
        hdr[o + nbl.REC_ENC_START:o + nbl.REC_ENC_END] = body
        if hdr[o + 0x40:o + 0x44] == b"NSIF":          # +0x10 size, +0x14 size + 0x20, +0x18 align8(ptrs) + 0x18
            struct.pack_into("<II", hdr, o + 0x50, size, size + 0x20)
            if ptr[m["name"]][1] != m["ptr_size"]:
                struct.pack_into("<I", hdr, o + 0x58, (ptr[m["name"]][1] + 7 & ~7) + 0x18)
    res = bytearray(hdr) + bytes(nbl.align(len(hdr)) - len(hdr))
    res += pdata + bytes(nbl.align(len(res) + len(pdata)) - len(res) - len(pdata))
    res += pt
    res += bytes(nbl.align(len(res)) - len(res))
    if nb["tmll_off"] is not None:
        res += d[nb["tmll_off"]:]
    return bytes(res)


def add_groups(d, nb, data, per_map):
    """(members_data, ptr_table) with per_map {map id: [groups]} appended to set_r0.rel's maps."""
    m = member(nb, "set_r0.rel")
    base = m["off"]
    s = bytearray(data[base:base + m["size"]])
    s += bytes(-len(s) % 16)
    slots = [v for v in struct.unpack_from(f"<{m['ptr_size'] // 4}I", d, nb["ptr_off"] + m["ptr_off"])]
    ab, = struct.unpack_from("<I", s, 8)
    _area, nm, mp = struct.unpack_from("<HHI", s, ab)
    native = set_maps(data, m)
    template = None
    added = 0
    for mi in range(nm):
        mid, ng, gp = struct.unpack_from("<HHI", s, mp - base + 12 * mi)
        extra = per_map.get(mid)
        if not extra:
            continue
        groups = native[mi][1]
        template = groups[-1][0]                       # a plain native group header (kind 0) for the new groups
        table = len(s)
        s += b"".join(h for h, _ in groups)
        slots += [base + table + 40 * k + 0x24 for k in range(len(groups))]
        heads = len(s)
        s += bytes(40 * len(extra))
        for k, (_h, objs) in enumerate(extra):
            objs_at = len(s)
            s += bytes(52 * len(objs))
            for j, (o, p) in enumerate(objs):
                at = len(s)
                s += p + bytes(-len(p) % 4)
                ob = bytearray(o)
                struct.pack_into("<II", ob, 44, len(p), base + at)
                s[objs_at + 52 * j:objs_at + 52 * j + 52] = ob
                slots.append(base + objs_at + 52 * j + 48)
            h = bytearray(template)
            struct.pack_into("<HI", h, 34, len(objs), base + objs_at)
            s[heads + 40 * k:heads + 40 * k + 40] = h
            slots.append(base + heads + 40 * k + 0x24)
            added += len(objs)
        struct.pack_into("<HHI", s, mp - base + 12 * mi, mid, ng + len(extra), base + table)
    s += bytes(-len(s) % 16)
    struct.pack_into("<I", s, 4, len(s) - 8)            # NSR chunk size
    after = [x for x in nb["members"] if x["off"] > base]
    assert all(x["ptr_size"] == 0 for x in after), "a member after set_r0.rel has pointers"
    md = {x["name"]: data[x["off"]:x["off"] + x["size"]] for x in nb["members"]}
    md["set_r0.rel"] = bytes(s)
    return md, {"set_r0.rel": slots}, added


def patch_quest(d, nb, data, zone, sid):
    m = member(nb, "quest.snr")
    q = bytearray(data[m["off"]:m["off"] + m["size"]])
    mp, = struct.unpack_from("<I", q, 8)
    zp, zn = struct.unpack_from("<II", q, mp + 0xC)
    for i in range(zn):
        if struct.unpack_from("<h", q, zp + 0x40 * i)[0] == zone:
            q[zp + 0x40 * i + 0xB] = sid
            md = {x["name"]: data[x["off"]:x["off"] + x["size"]] for x in nb["members"]}
            md["quest.snr"] = bytes(q)
            return md
    raise SystemExit(f"quest.snr has no zone {zone}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nbl", default=os.environ.get("PSU_NBL_DIR"))
    ap.add_argument("--season", action="append", default=[])
    ap.add_argument("--out")
    a = ap.parse_args()
    if not a.nbl:
        raise SystemExit("--nbl DIR (or $PSU_NBL_DIR): the directory with the lobby archives")
    nd = a.nbl
    out = a.out or os.path.join(nd, "season-staging")
    cvm = next((workdir.path(g, "cvm", "season") for g in workdir.GAMES
                if os.path.exists(workdir.path(g, "cvm", "season"))), workdir.path("psu", "cvm", "season"))
    ids = season_ids(nbl, cvm)
    if not ids:
        raise SystemExit(f"no XF_OB_*.NBL in {cvm} (see the docstring)")
    egs_d, egs_nb, egs_data = load(nbl, os.path.join(nd, f"zone-{QUEST}-{ZONE}.egs.nbl"))
    groups = season_groups(egs_data, egs_nb, ids)
    want = [x.strip().lower() for x in a.season] or sorted(groups)
    for name in want:
        if name not in groups:
            print(f"{name}: no EGS groups for the Colony, skipped")
            continue
        sid = SEASONS.index(name)
        dst = os.path.join(out, name)
        os.makedirs(os.path.join(dst, "aoti"), exist_ok=True)
        d, nb, data = load(nbl, os.path.join(nd, f"quest-{QUEST}.nbl"))
        md = patch_quest(d, nb, data, ZONE, sid)
        open(os.path.join(dst, f"quest-{QUEST}.nbl"), "wb").write(write_nbl(nbl, prs, compress, d, nb, md, {}))
        for sub in ("", "aoti"):
            src = os.path.join(nd, sub, f"zone-{QUEST}-{ZONE}.nbl")
            if not os.path.exists(src):
                continue
            d, nb, data = load(nbl, src)
            md, pt, n = add_groups(d, nb, data, groups[name])
            if name in SEASON_BGM:
                md["script.bin"] = patch_bgm(prs, compress, md["script.bin"], SEASON_BGM[name], BGM_SITES[sub])
            res = write_nbl(nbl, prs, compress, d, nb, md, pt)
            if len(res) > 0x19000:                     # the 0x020F handlers take at most 0x19000 bytes
                raise SystemExit(f"{name} {sub or 'psu'}: zone NBL {len(res)} B is over 0x19000")
            open(os.path.join(dst, sub, f"zone-{QUEST}-{ZONE}.nbl"), "wb").write(res)
            print(f"{name} (id {sid}) {sub or 'psu'}: {n} objects on maps {sorted(groups[name])}, "
                  f"BGM {SEASON_BGM.get(name, CITY_BGM)}, {len(res)} B")


if __name__ == "__main__":
    main()
