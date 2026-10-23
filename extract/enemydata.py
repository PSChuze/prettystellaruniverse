"""Enemy tables for the server from the disc + PSU_US.erx.

  python enemydata.py [--game psu|aoti] [--iso "<disc>.iso"] [--out DIR]   -> DIR/enemies.json
                                                (default DIR <work>/<game>/gamedata)
  python enemydata.py zone <zone.nbl> <level> [--enemies <enemies.json>]               print a zone's gateways

Also imported by missiondata.py (zone_gateways) to put each gateway's enemy list into missions.json.
Addresses are PSU_US.erx image addresses (RAM = image + 0x248000).

Enemy identity (static reading, see the functions named):
  set object type 24 (also 23 / 25) = TEnemyGateway (ctor 0x1deda0). Its params (0x64 B): +0x5C s16,
    +0x5E s16, +0x60 s16 event flag ids (copied to the spawner at 0x1d1a1c), +0x62 u8 flag,
    +0x63 u8 = group id in the map's enemy file (0x1def60 -> 0x1e7ad0 matches group header s16 +0).
  enemy file = zone NBL member "enemy%02d_%04d_r%d.snr" (zone id, map id, layout; loader 0x228190).
    NSR; pointers are relative to the NBL data section (member offset + file offset).
    +8 root: {u32 groups ptr, u32 n}; group 0x18 B = 3 x {u32 ptr, u32 n}:
      [0] header: s16 group id, s16, s16, s16, s8          (0x1d1a3c..)
      [1] n x 0x10 info (one per wave): s16 +0 id, +2, +4, +6, +8, +0xA, s8 +0xC, s16 +0xE
      [2] n x {u32 entries ptr, s16 count} waves; entry 0x24 B (0x1d3890 client, 0x35c720 server):
          +0 s16 enemy type (EnemyLVTable row), +2 s16 sub (0x484070 -> enemy+0x10),
          +4..+8 u8 x5 variant bits 0..4 (0x35d9d4), +0xA s16, +0xC s16, +0xE s16, +0x10 s16 count,
          +0x12/+0x14/+0x16 s16, +0x18 s16 level offset, +0x1A s16, +0x1C s16
  enemy level = clamp(zone level + entry +0x18, 1, 200) (0x4038e0); zone level = quest.snr zone
    record (64 B) +0xA u8 (TQuestManager 0x169f30, room 0xd9130). Zone record +0x1C -> optional boost
    record (u32 category mask, u16 x /512 factors: +4 HP, +6 ATP, +8 DFP, +0xA ATA, +0xC EVP, +0xE STA,
    +0x10 S5, +0x12 TP, +0x14 MST, +0x18 EXP; applied at 0x282478 when mask bit (param +0x5C category,
    0x483f00) is set); null in every disc quest.
  variant (param index 0..93) = EnemyLVTable.snr row[type]: 4 x {s32 variant, s16 lo, s16 hi} (0x403a90).
  variant -> Param<Name>.snr: descriptor table 0x592a70[variant] +0x16C {NBL index into the name
    table 0x594050, file list, file index} (loader 0x197d20; registered in 0x630470 at 0x198060).
  target ids: 0x283c70 walks set_r*.rel (maps, groups, objects in file order) and gives each object
    whose type row in 0x53d9e0 (stride 0x1C, +0x14 u16 id count; gateways 23/24/25 = 10/30/50, boxes
    12 / switches 5 / ... = 1) a running index; actor id = 150 + index (0x102fe0). The server-side room
    numbers a gateway's enemies gateway id + 1 + i (0x35d81c), i running over waves, entries, count.

Stats (TServerActorEnemy 0x282040): row = EnemyLevelBaseParam row[level] (0x290d30),
  P = Param floats: HP +0, ATP +4, DFP +8, ATA +0xC, EVP +0x10, STA +0x14, S5 +0x18, TP +0x1C,
  MST +0x20, +0x24, EXP +0x28; category u8 +0x5C.
  max HP = u32(max(1.0, row.HP * P.HP) * boost.HP); stat = s16 clamp(row.stat * P.stat * boost.stat)
  (the client stores the TP result in the MST slot too, 0x282374); EXP = max(1, int(row.EXP * P.EXP
  * boost.EXP)) -> enemy+0x20.
Kill EXP (0x280c80 -> 0x280680): enemy EXP * product over variant bits i whose Param variant entry i
  (root[1], 5 x 0x48 B, floats +0xC..+0x2C) is not all 1.0 of EnemyLevelBaseParam root float[i]
  (1.5, 1.2, 1.2, 1.2, 1.2) * root float +0x14 (1.0). Variant bits: spawn entry +4..+8, plus bit 0 for
  variants 60..65 and bits 0+1 for 66 (0x403a00, the rare Rappies / Jaggos).
  Split (0x1ba680): killer 100 %, other hitters 70 % (enemy vfunc +0x44 = 0; 1 would mean 100 %).

AotI (SLUS-21631, --game aoti; addresses = AotI's PSU_US.erx image):
  Same formats: Param<Name>.snr, EnemyLevelBaseParam.snr (same 5 bit / global EXP multipliers),
  EnemyLVTable.snr (129 types, variants up to 136), enemy files and set objects. What moved:
  variant table 0x5f3910, 0x89 = 137 variants (0x1985d0 slti 0x89; PSU 0x198550 slti 0x5e), variant 68 null.
  NBL name table 0x5f4cb0, 0x134 names (0x1d0700 slti 0x134; PSU 0x1cf970 slti 0xdc).
  Descriptor: PSU keeps a 12-byte {nbl, file list, index} triple per file (+0x160 MotTbl, +0x16C Param,
    +0x178 ...); AotI shares one {nbl +0x160, file list +0x164} and keeps only the indexes (+0x168 MotTbl,
    +0x16C Param, +0x170 ...): loader 0x197cd0 calls 0x196f10(out, desc + 0x160, desc[+0x16C]) before the
    Param load 0x1974f0(mode -1) and hands the file to 0x293940 (PSU 0x290cc0, same code).
  Set object types: table 0x55d950 (same 0x1C rows), count 0x55df70 (0x285eb4; PSU 0x283c14 reads 0x53df58).
  Rare bits: 0x4089f0 (jump table 0x571840) sets bit 0 for variants 60..65 and 0+1 for 66, as PSU.
  The server stat code (TServerActorEnemy 0x283dc0, level row 0x2939b0) reads the same Param floats
  (+0..+0x28, category via +0x5C) as PSU's 0x282040.
"""
import glob
import json
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import erx  # noqa: E402
import nbl  # noqa: E402
import workdir  # noqa: E402

workdir.work()


VARIANT_DESC = 0x592a70     # 94 x descriptor ptr
N_VARIANTS = 0x5e
NBL_NAMES = 0x594050        # enemy NBL name table
OBJ_TABLE = 0x53d9e0        # set object types: {type, client ctor, server ctor, 0, 0, u16 ids | u16 params size, 0}
OBJ_COUNT = 0x53df58
ID_BASE = 0x96
GAMES = {
    "psu": {"variants": (VARIANT_DESC, N_VARIANTS),
            "names": (NBL_NAMES, None), "param": (0x16C, 0x170, 0x174), "objects": (OBJ_TABLE, OBJ_COUNT)},
    "aoti": {"variants": (0x5f3910, 0x89),
             "names": (0x5f4cb0, 0x134), "param": (0x160, 0x164, 0x16C), "objects": (0x55d950, 0x55df70)},
}
RARE = {60: [0], 61: [0], 62: [0], 63: [0], 64: [0], 65: [0], 66: [0, 1]}
STATS = ("hp", "atp", "dfp", "ata", "evp", "sta", "s5", "tp", "mst", "x24", "exp")


class Erx:
    def __init__(self, path=None, game="psu"):
        path = path or workdir.erx(game)
        self.im = erx.open_image(path)
        self.g = GAMES[game]

    def u32(self, va):
        return struct.unpack_from("<I", self.im.d, self.im.va2off(va))[0]

    def cstr(self, va):
        o = self.im.va2off(va)
        return self.im.d[o:self.im.d.index(b"\0", o)].decode("ascii")

    def nbl_names(self):
        va, n = self.g["names"]
        if n is not None:
            return [self.cstr(self.u32(va + 4 * i)) for i in range(n)]
        out = []
        while True:
            p = self.u32(va)
            if not 0x500000 < p < 0x560000:
                return out
            out.append(self.cstr(p))
            va += 4

    def variants(self):
        """{variant: (nbl name, Param file name)}"""
        names, out = self.nbl_names(), {}
        table, count = self.g["variants"]
        o_nb, o_lst, o_idx = self.g["param"]
        for v in range(count):
            d = self.u32(table + 4 * v)
            if not d:
                continue
            nb, lst, idx = self.u32(d + o_nb), self.u32(d + o_lst), self.u32(d + o_idx)
            out[v] = (names[nb], self.cstr(self.u32(lst + 4 * idx)))
        return out

    def object_ids(self):
        """{set object type: target ids it reserves}"""
        table, count = self.g["objects"]
        n = self.u32(count)
        out = {}
        for i in range(n):
            t = self.u32(table + 0x1C * i)
            if t < 0x10000:
                out[t] = self.u32(table + 0x1C * i + 0x14) & 0xFFFF
        return out


def lvtable(b):
    """EnemyLVTable.snr -> [[(variant, lo, hi), ...] per enemy type]"""
    end, = struct.unpack_from("<I", b, 8)
    rows = []
    for r in range(0x10, end, 0x20):
        rows.append([list(struct.unpack_from("<ihh", b, r + 8 * k)) for k in range(4)
                     if struct.unpack_from("<i", b, r + 8 * k)[0] >= 0])
    return rows


def variant_at(rows, etype, level):
    if not 0 <= etype < len(rows):
        return -1
    for v, lo, hi in rows[etype]:
        if lo <= level <= hi:
            return v
    return -1


def param(b, base):
    """Param<Name>.snr (pointers = base + file offset): root +0 base floats, +4 5 x 0x48 variant entries."""
    root, = struct.unpack_from("<I", b, 8)
    fp, vp = struct.unpack_from("<II", b, root)
    fo, vo = fp - base, vp - base
    fl = list(struct.unpack_from("<11f", b, fo))
    out = {k: round(x, 4) for k, x in zip(STATS, fl)}
    out["category"] = b[fo + 0x5C]
    bits = []
    for i in range(5):
        e = vo + 0x48 * i
        f = [round(x, 4) for x in struct.unpack_from("<9f", b, e + 0xC)]
        flags, = struct.unpack_from("<I", b, e + 0x40)
        bits.append({"factors": f, "flags": flags, "exp_bonus": any(abs(x - 1.0) > 1e-4 for x in f)})
    out["variant_bits"] = bits
    return out


def level_base(b):
    """EnemyLevelBaseParam.snr: root {rows ptr, f32 x5 bit EXP multipliers, f32 global EXP multiplier}"""
    root, = struct.unpack_from("<I", b, 8)
    return list(struct.unpack_from("<5f", b, root + 4)), struct.unpack_from("<f", b, root + 0x18)[0]


def nbl_members(path):
    """{member name: (bytes, base)} with base = the member's offset in the NBL data section."""
    d = open(path, "rb").read()
    nb = nbl.parse(d)
    data = nbl.nbl_data(d, nb)
    return {m["name"]: (data[m["off"]:m["off"] + m["size"]], m["off"]) for m in nb["members"]}


def enemy_groups(b, base):
    """enemy%02d_%04d_r%d.snr -> {group id: {"header": [...], "waves": [[entry dict, ...], ...]}}"""
    rel = lambda p: p - base
    root, = struct.unpack_from("<I", b, 8)
    gp, ng = struct.unpack_from("<II", b, root)
    out = {}
    for gi in range(ng):
        g = rel(gp) + 0x18 * gi
        hp, _hn, ip, inn, wp, wn = struct.unpack_from("<IIIIII", b, g)
        hdr = list(struct.unpack_from("<hhhhb", b, rel(hp)))
        infos = [list(struct.unpack_from("<6hbxh", b, rel(ip) + 0x10 * k)) for k in range(inn & 0xFFFF)]
        waves = []
        for w in range(wn & 0xFFFF):
            ep, en = struct.unpack_from("<Ih", b, rel(wp) + 8 * w)
            ents = []
            for k in range(en):
                e = rel(ep) + 0x24 * k
                t, sub = struct.unpack_from("<hh", b, e)
                flags = list(b[e + 4:e + 9])
                f = struct.unpack_from("<10h", b, e + 0xA)
                ents.append({"type": t, "sub": sub, "bits": [i for i in range(5) if flags[i]],
                             "count": f[3], "level_ofs": f[7], "raw": b[e:e + 0x24].hex()})
            waves.append(ents)
        out[hdr[0]] = {"header": hdr, "info": infos, "waves": waves}
    return out


def set_target_ids(s, obj_ids):
    """set_r*.rel -> {(map index, group, object): actor target id} as 0x283c70 + 0x102fe0 assign them."""
    ab, = struct.unpack_from("<I", s, 8)
    _area, nm, mp = struct.unpack_from("<HHI", s, ab)
    mt = ab - 12 * nm
    base = mp - mt
    run, out = 0, {}
    for mi in range(nm):
        _mid, ng, gp = struct.unpack_from("<HHI", s, mt + 12 * mi)
        for gi in range(ng):
            g = gp - base + 40 * gi
            n, op = struct.unpack_from("<HI", s, g + 34)
            for oi in range(n):
                typ, = struct.unpack_from("<H", s, op - base + 52 * oi + 14)
                k = obj_ids.get(typ, 0)
                if k:
                    out[(mi, gi, oi)] = ID_BASE + run
                    run += k
    return out


def zone_gateways(members, zone_id, level, gd, obj_ids=None):
    """Gateways of one zone NBL (members = nbl_members()) with their enemies: list of dicts."""
    obj_ids = obj_ids or {int(k): v for k, v in gd["object_ids"].items()}
    s = members["set_r0.rel"][0]
    ids = set_target_ids(s, obj_ids)
    ab, = struct.unpack_from("<I", s, 8)
    _area, nm, mp = struct.unpack_from("<HHI", s, ab)
    mt = ab - 12 * nm
    base = mp - mt
    out = []
    for mi in range(nm):
        mid, ng, gp = struct.unpack_from("<HHI", s, mt + 12 * mi)
        ef = members.get(f"enemy{zone_id:02d}_{mid:04d}_r0.snr")
        groups = enemy_groups(*ef) if ef else {}
        for gi in range(ng):
            g = gp - base + 40 * gi
            n, op = struct.unpack_from("<HI", s, g + 34)
            for oi in range(n):
                o = op - base + 52 * oi
                typ, = struct.unpack_from("<H", s, o + 14)
                if typ not in (23, 24, 25):
                    continue
                psz, pp = struct.unpack_from("<II", s, o + 44)
                prm = s[pp - base:pp - base + psz]
                f5c, f5e, f60 = struct.unpack_from("<hhh", prm, 0x5C)
                gid = prm[0x63]
                gw = {"map": mid, "map_index": mi, "group": gi, "object": oi, "id": ids.get((mi, gi, oi)),
                      "capacity": obj_ids.get(typ, 0), "enemy_group": gid, "flags": [f5c, f5e, f60, prm[0x62]],
                      "enemies": []}
                grp = groups.get(gid)
                i = 0
                for wi, wave in enumerate(grp["waves"] if grp else []):
                    for ent in wave:
                        lv = max(1, min(200, level + ent["level_ofs"]))
                        var = variant_at(gd["types"], ent["type"], lv)
                        bits = sorted(set(ent["bits"]) | set(RARE.get(var, [])))
                        v = gd["variants"].get(str(var))
                        for _ in range(max(ent["count"], 0)):
                            gw["enemies"].append({"id": gw["id"] + 1 + i if gw["id"] is not None else None,
                                                  "wave": wi, "type": ent["type"], "level": lv, "variant": var,
                                                  "name": v["name"] if v else None, "bits": bits})
                            i += 1
                out.append(gw)
    return out


def enemy_stats(gd, variant, level, bits=()):
    """Server-side stats for one enemy: {max_hp, stats[8] (ATP DFP ATA EVP STA S5 TP MST), exp, kill_exp}."""
    v = gd["variants"][str(variant)]
    row = gd["levels"][max(1, min(level, len(gd["levels"]) - 1))]
    hp = int(max(1.0, row[0] * v["hp"]))
    st = [max(0, min(0x7FFF, int(r * v[k]))) for r, k in zip(row[1:9], STATS[1:9])]
    st[7] = st[6]                                           # 0x282374: MST slot gets the TP value
    exp = max(1, int(row[10] * v["exp"]))
    mult = gd["exp_global"]
    for b in bits:
        if v["variant_bits"][b]["exp_bonus"]:
            mult *= gd["exp_bits"][b]
    return {"max_hp": hp, "stats": st, "exp": exp, "kill_exp": int(exp * mult)}


def build(iso, game="psu"):
    g = GAMES[game]
    cvm, en_dir = workdir.path(game, "cvm"), workdir.path(game, "cvm", "enemies")
    cvm_py = [sys.executable, os.path.join(HERE, "cvm.py"), "x"]
    erx_arg = ["--erx", workdir.erx(game)]
    need = [n for n in ("SU_ENEMYLVTABLEPS2.NBL", "SU_ENEMYLEVELBASEPARAM.NBL") if not os.path.exists(os.path.join(cvm, n))]
    if need:
        subprocess.run([*cvm_py, workdir.need_iso(iso), cvm, *[x for n in need for x in ("--match", n)], *erx_arg],
                       check=True, stdout=subprocess.DEVNULL)
    if not glob.glob(os.path.join(en_dir, "SU_EN_*.NBL")):
        os.makedirs(en_dir, exist_ok=True)
        subprocess.run([*cvm_py, workdir.need_iso(iso), en_dir, "--match", "SU_EN_", *erx_arg],
                       check=True, stdout=subprocess.DEVNULL)
    ex = Erx(workdir.erx(game), game)
    rows = lvtable(nbl_members(os.path.join(cvm, "SU_ENEMYLVTABLEPS2.NBL"))["EnemyLVTable.snr"][0])
    lb = nbl_members(os.path.join(cvm, "SU_ENEMYLEVELBASEPARAM.NBL"))["EnemyLevelBaseParam.snr"][0]
    bits_mult, glob_mult = level_base(lb)
    end, = struct.unpack_from("<I", lb, 8)
    levels = [list(struct.unpack_from("<I9hH", lb, o)) for o in range(0x10, end - 0x18 + 1, 0x18)]
    params = {}
    for f in sorted(glob.glob(os.path.join(en_dir, "SU_EN_*.NBL"))):
        for name, (b, base) in nbl_members(f).items():
            if name.startswith("Param") and name.endswith(".snr"):
                params[name] = (b, base, os.path.basename(f))
    variants = {}
    for v, (nb, pf) in sorted(ex.variants().items()):
        if pf not in params:
            print(f"variant {v}: {pf} not found", file=sys.stderr)
            continue
        b, base, src = params[pf]
        variants[str(v)] = {"name": pf[5:-4], "param": pf, "nbl": src, "nbl_ref": nb, **param(b, base)}
    return {"types": rows, "variants": variants, "levels": levels, "exp_bits": [round(x, 4) for x in bits_mult],
            "exp_global": round(glob_mult, 4), "rare_bits": {str(k): v for k, v in RARE.items()},
            "object_ids": {str(k): v for k, v in sorted(ex.object_ids().items())}, "id_base": ID_BASE}


def main():
    a = sys.argv[1:]
    if a and a[0] == "zone":
        ej = a[a.index("--enemies") + 1] if "--enemies" in a else workdir.path("psu", "gamedata", "enemies.json")
        gd = json.load(open(ej, encoding="utf-8"))
        zid = int(os.path.basename(a[1])[4:6])
        for gw in zone_gateways(nbl_members(a[1]), zid, int(a[2]), gd):
            print(f"map {gw['map']} gateway id {gw['id']} group {gw['enemy_group']} flags {gw['flags']}")
            for e in gw["enemies"]:
                s = enemy_stats(gd, e["variant"], e["level"], e["bits"]) if e["variant"] >= 0 else {}
                print(f"   {e['id']} wave {e['wave']} {e['name']} lv {e['level']} bits {e['bits']} {s}")
        return
    game = a[a.index("--game") + 1] if "--game" in a else "psu"
    iso = workdir.iso_arg(a)
    out = a[a.index("--out") + 1] if "--out" in a else workdir.path(game, "gamedata")
    os.makedirs(out, exist_ok=True)
    gd = build(iso, game)
    json.dump(gd, open(os.path.join(out, "enemies.json"), "w", encoding="utf-8"), indent=1)
    print(f"{len(gd['variants'])} enemy variants, {len(gd['types'])} types -> {out}/enemies.json")


if __name__ == "__main__":
    main()
