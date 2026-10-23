"""Item drop tables for the server from the disc.

  python dropdata.py [--game psu|aoti] [--iso "<disc>.iso"] [--out DIR]   -> DIR/drops.json
                                                (default DIR <work>/<game>/gamedata, see workdir.py)
  python dropdata.py roll enemy <variant> <level> [<area>] [-n N]        sample the offline roll
  python dropdata.py roll object <type> <index> <level> [<area>] [-n N]

Sources: GA_ITEMPARAM.NBL itemEnemyDrop.snr / itemObjectDrop.snr, SU_ITEMAREADROP.AFS (areaDrop06..21.nbl,
members kae.snr = area enemy table, yok.snr = area object table). Addresses: PSU_US.erx image (RAM + 0x248000).

The offline room (TItemParamManagerClient, vtable 0x5db480, global 0x61a9fc) rolls every drop itself;
rand(n) = 0x2081a0, uniform 0..n-1; every table uses n = 50000 and picks the first slot whose threshold
t satisfies r <= t (thresholds are cumulative; a slot whose threshold equals the previous one never wins).

Level band (0xb6a60 / 0xb6870): bands = itemObjectDrop root u16[10] (5 10 15 20 25 30 40 60 80 100);
  band = first i < 9 with level <= bands[i], else 9.

Enemy kill (0xb6b90, called from TGameRoomClient 0xd9470 <- TServerActorEnemy 0x280d30):
  key = the enemy's param variant (0..93, TServerActorEnemy +0x18; enemies.json "types" maps spawn type
  + level to it), level = enemy status +0x10. Entry (0x18 B): u16 variant, u16 common index, u16 t[3],
  u16 rare, u32 meseta min, u32 meseta max, 4-byte rare item id.
    r = rand(50000); slot = first k in 0..2 with r <= t[k], else 3
    0 nothing | 1 meseta: min + rand(max - min) (offline also x (0x392d30() + 1), the party count)
    2 area table (kae.snr of the mission's area drop file, below)
    3 r2 = rand(50000); r2 > rare -> the rare item id; else the common table
      common (0x10 B): 3 item ids, u16 t[2]; r3 = rand(50000); first k in 0..1 with r3 <= t[k], else 2
Area enemy table (kae.snr, 0xb6a60): {u32 n, ptr} -> n x 0x18 B {4-byte id, u16 t[10] per band};
  r = rand(50000); first j < n - 1 with r <= t[band], else the last entry.
Breakable object (0xb6870, called from TGameRoomClient 0xd9350 <- TServerActorObjBreak 0x3b0080):
  set type 12 params +2 s8 = drop type (0..4; < 0 = no drop), +3 u8 = index (0..5), +4 s8 = hits needed;
  level = the room's mission level byte (room +0x9e). Root +0x14: 5 x {u32 count, ptr} -> count x 0xD0 B:
  u8 type (must equal the drop type), u8, u16 chance, u32, then 10 bands x 0x14 B {3 ids, u16 t[3], u16}.
    r = rand(50000); r > chance -> nothing
    r2 = rand(50000); slot = first k in 0..2 with r2 <= band.t[k] -> band id k; else 3 -> area object table
Area object table (yok.snr, 0xb67b0): root = 5 x 0x10 B {3 ids, u16 t[2]} indexed by drop type;
  r = rand(50000); first k in 0..1 with r <= t[k], else 2.
Area drop file: the current zone's quest.snr record +2 s16 (TQuestManager vfunc +0xF8 = 0x227ce0) gives NN;
  0xb6ee0 loads su_ItemAreaDrop.afs entry NN - 6 only for 6..21 (offline only, 0x94700); any other value (23 =
  boss zones) keeps the previously loaded file. drops.json "zones" = {quest: {zone: [NN, zone level]}}; the
  zone level is also the breakable-object roll level (room +0x9e, same as TGameRoomClient 0xd9130).
Item id 00000000 in any slot = nothing (the client's 0xb4cb0 rejects it). Meseta is item id 12000000 with
the amount in the item's quantity word.

AotI (--game aoti, AotI's PSU_US.erx; <work>/aoti/cvm GA_ITEMPARAM.NBL + SU_ITEMAREADROP.AFS = areaDrop06..33):
  Same rand(50000), roots, area kae/yok tables and common lists (0x10 B). What changed:
  Band (0xaf9f0, root = itemObjectDrop, bands 9 24 35 49 60 74 85 99 124 200): first i < 9 with level <=
    bands[i], else 10 (PSU 9). Band 10 (level > 124) indexes past the 10 kae thresholds and the 5 rare
    slots below (the client then reads the next row); drops.json keeps the bands and the rolls here clamp to 9.
  Enemy kill (0xaf780): entry 0x2C B: u16 variant, u16 common, u16 t[3], u16 rare_t[5] (+0xA), u16 meseta
    min (+0x14), u16 max (+0x16), 5 x 4-byte rare ids (+0x18). Rare slot h = band // 2 (half bands):
    r2 > rare_t[h] -> rare id h, else the common list. Meseta: m = min + rand(max - min), amount =
    m + m * level / 5 (no party factor). Common list: a threshold <= 0 never wins (PSU: r <= t).
    drops.json: rare_t / rare = slot 0 (what a PSU reader uses), rare_bands = the 5 [t, id] pairs.
  Box (0xaf4b0, params type < 5, index, level): record 0x64 B = 5 half-band blocks of 0x14 (3 ids,
    u16 t[3], u16); chance = record +0x12 (the first block's last u16); no type byte check. drops.json
    writes PSU's shape: bands[b] = half-band block b // 2, type = the drop type.
  Area file (0xafb30 <- 0x9d09c, area = the zone record +2, map = TQuestManager +0x44 via vfunc +0x94):
    entry area - 6 for 6..21 as PSU, but a 12-row {s8 area, s8 k, s8 entry} table (0x53f1b0) gives
    another entry when area matches and k == (map % 10000) // 1000. drops.json zones carry the entry + 6
    per zone; "maps" {quest: {zone: {map: NN}}} lists maps whose NN differs from their zone's.
"""
import json
import os
import random
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import workdir  # noqa: E402

GAMES = {
    "psu": {"areas": range(6, 22)},
    "aoti": {"areas": range(6, 34), "remap": (0x53f1b0, 12)},
}
workdir.work()

R = 50000
MESETA = "12000000"
u16 = lambda d, o: struct.unpack_from("<H", d, o)[0]
u32 = lambda d, o: struct.unpack_from("<I", d, o)[0]
iid = lambda d, o: d[o:o + 4].hex()


def run(*args):
    subprocess.run([sys.executable, os.path.join(HERE, args[0]), *args[1:]], check=True, stdout=subprocess.DEVNULL)


def member(d, m, name):
    """(member offset, root offset); the header root (+8) is member-relative, inner pointers are
    relative to the NBL data section."""
    off = m[name]["off"]
    return off, off + u32(d, off + 8)


def enemy_tables(d, m, game="psu"):
    _, r = member(d, m, "itemEnemyDrop.snr")
    nsub, nent, psub, pent = struct.unpack_from("<HHII", d, r)
    common = [{"ids": [iid(d, psub + 16 * i + 4 * k) for k in range(3)],
               "t": list(struct.unpack_from("<2H", d, psub + 16 * i + 12))} for i in range(nsub)]
    enemy = {}
    if game == "aoti":
        for i in range(nent):
            o = pent + 0x2C * i
            var, sub, t0, t1, t2 = struct.unpack_from("<5H", d, o)
            rt = struct.unpack_from("<5H", d, o + 0xA)
            mmin, mmax = struct.unpack_from("<2H", d, o + 0x14)
            rb = [[rt[h], iid(d, o + 0x18 + 4 * h)] for h in range(5)]
            enemy[str(var)] = {"common": sub, "t": [t0, t1, t2], "rare_t": rb[0][0], "rare": rb[0][1],
                               "rare_bands": rb, "meseta": [mmin, mmax]}
        return enemy, common
    for i in range(nent):
        o = pent + 0x18 * i
        var, sub, t0, t1, t2, rare, mmin, mmax = struct.unpack_from("<6H2I", d, o)
        enemy[str(var)] = {"common": sub, "t": [t0, t1, t2], "rare_t": rare, "rare": iid(d, o + 0x14),
                           "meseta": [mmin, mmax]}
    return enemy, common


def object_tables(d, m, game="psu"):
    _, r = member(d, m, "itemObjectDrop.snr")
    bands = list(struct.unpack_from("<10H", d, r))
    types = []
    for t in range(5):
        n, p = struct.unpack_from("<II", d, r + 0x14 + 8 * t)
        recs = []
        if game == "aoti":
            for k in range(n):
                o = p + 0x64 * k
                half = [{"ids": [iid(d, o + 0x14 * h + 4 * j) for j in range(3)],
                         "t": list(struct.unpack_from("<3H", d, o + 0x14 * h + 12))} for h in range(5)]
                recs.append({"type": t, "chance": u16(d, o + 0x12), "bands": [half[b // 2] for b in range(10)]})
            types.append(recs)
            continue
        for k in range(n):
            o = p + 0xD0 * k
            rec = {"type": d[o], "b1": d[o + 1], "chance": u16(d, o + 2), "x4": u32(d, o + 4), "bands": []}
            for b in range(10):
                q = o + 8 + 0x14 * b
                rec["bands"].append({"ids": [iid(d, q + 4 * j) for j in range(3)],
                                     "t": list(struct.unpack_from("<3H", d, q + 12))})
            recs.append(rec)
        types.append(recs)
    return bands, types


def area_tables(path):
    import itemdb
    d, m = itemdb.load_nbl(path)
    _, r = member(d, m, "kae.snr")
    n, p = struct.unpack_from("<II", d, r)
    kae = [{"id": iid(d, p + 0x18 * j), "t": list(struct.unpack_from("<10H", d, p + 0x18 * j + 4))} for j in range(n)]
    _, r = member(d, m, "yok.snr")
    yok = [{"ids": [iid(d, r + 16 * t + 4 * k) for k in range(3)], "t": list(struct.unpack_from("<2H", d, r + 16 * t + 12))}
           for t in range(5)]
    return {"enemy": kae, "object": yok}


def mission_zones(cvm, remap=None, lang="ae"):
    """{quest id: {zone: [area drop NN, zone level]}} from the free missions' quest.snr in the archives
    missiondata.py extracted (<cvm>/missions/*_FM_*/quest_<lang>.nbl; zone record 64 B: s16 zone, s16 area
    (= areaDropNN; 23 = boss area, outside 6..21), +0xA u8 level). Story missions are not listed.
    remap (AotI): {(area, map thousands digit): entry}; returns (zones, {quest: {zone: {map: NN}}})."""
    import glob
    import itemdb
    out, maps = {}, {}
    for f in sorted(glob.glob(os.path.join(cvm, "missions", "*_FM_*", f"quest_{lang}.nbl"))):
        d, m = itemdb.load_nbl(f)
        q = d[m["quest.snr"]["off"]:m["quest.snr"]["off"] + m["quest.snr"]["size"]]
        mp, = struct.unpack_from("<I", q, 8)
        qid, zp, nz = struct.unpack_from("<III", q, mp + 8)
        out[str(qid)] = {str(u16(q, zp + 64 * i)): [u16(q, zp + 64 * i + 2), q[zp + 64 * i + 0xA]] for i in range(nz)}
        if remap is None:
            continue
        for z, za in out[str(qid)].items():
            area = za[0]
            if not 6 <= area < 22:
                continue
            zf = os.path.join(os.path.dirname(f), f"zone{int(z):02d}_{lang}.nbl")
            if not os.path.exists(zf):
                continue
            zd, zm = itemdb.load_nbl(zf)
            if "set_r0.rel" not in zm:
                continue
            s = zd[zm["set_r0.rel"]["off"]:zm["set_r0.rel"]["off"] + zm["set_r0.rel"]["size"]]
            ab, = struct.unpack_from("<I", s, 8)
            nm = u16(s, ab + 2)
            nn = {u16(s, ab - 12 * nm + 12 * k): remap.get((area, u16(s, ab - 12 * nm + 12 * k) % 10000 // 1000), area - 6) + 6
                  for k in range(nm)}
            first = nn[u16(s, ab - 12 * nm)]
            za[0] = first
            diff = {str(k): v for k, v in nn.items() if v != first}
            if diff:
                maps.setdefault(str(qid), {})[z] = diff
    return (out, maps) if remap is not None else out


def area_remap(erx_path, va, n):
    """AotI 0x53f1b0: n x {s8 area, s8 k, s8 AFS entry}."""
    import erx
    im = erx.open_image(erx_path)
    o = im.va2off(va)
    return {(a, k): e for a, k, e in (struct.unpack_from("<3b", im.d, o + 3 * i) for i in range(n))}


def build(iso, out, game="psu"):
    import itemdb
    g = GAMES[game]
    cvm, area_dir = workdir.path(game, "cvm"), workdir.path(game, "cvm", "areadrop")
    if not os.path.exists(os.path.join(cvm, "GA_ITEMPARAM.NBL")) or not os.path.exists(os.path.join(cvm, "SU_ITEMAREADROP.AFS")):
        run("cvm.py", "x", workdir.need_iso(iso), cvm, "--match", "GA_ITEMPARAM.NBL", "--match", "SU_ITEMAREADROP.AFS",
            "--erx", workdir.erx(game))
    if not os.path.exists(os.path.join(area_dir, "areaDrop06.nbl")):
        run("nbl.py", "afs", os.path.join(cvm, "SU_ITEMAREADROP.AFS"), area_dir)
    d, m = itemdb.load_nbl(os.path.join(cvm, "GA_ITEMPARAM.NBL"))
    enemy, common = enemy_tables(d, m, game)
    bands, objects = object_tables(d, m, game)
    area = {}
    for nn in g["areas"]:
        f = os.path.join(area_dir, f"areaDrop{nn:02d}.nbl")
        if os.path.exists(f):
            area[str(nn)] = area_tables(f)
    os.makedirs(out, exist_ok=True)
    extra = {}
    if game == "psu":
        zones = mission_zones(cvm)
    else:
        zones, maps = mission_zones(cvm, area_remap(workdir.erx(game), *g["remap"]))
        extra = {"game": game, "maps": maps}
    json.dump({"rand": R, "bands": bands, "enemy": enemy, "common": common, "object": objects, "area": area,
               "zones": zones, "meseta_id": MESETA, **extra}, open(os.path.join(out, "drops.json"), "w", encoding="utf-8"))
    print(f"wrote {out}/drops.json: {len(enemy)} enemy variants, {len(common)} common lists, "
          f"{sum(map(len, objects))} object records, {len(area)} area files, {len(zones)} missions")


# --- reference roll (what the server should do; mirrors the offline room) -------------------------------

def band(t, level):
    return next((i for i in range(9) if level <= t["bands"][i]), 9)


def pick(r, thresholds, skip0=False):
    return next((k for k, v in enumerate(thresholds) if r <= v and not (skip0 and v <= 0)), len(thresholds))


def roll_enemy(t, variant, level, area=None, rnd=random.randrange):
    """-> None, ("meseta", amount) or ("item", id)."""
    e = t["enemy"].get(str(variant))
    if e is None:
        return None
    slot = pick(rnd(R), e["t"])
    if slot == 0:
        return None
    if slot == 1:
        lo, hi = e["meseta"]
        amount = lo + (rnd(hi - lo) if hi > lo else 0)
        return ("meseta", amount + amount * level // 5 if "rare_bands" in e else amount)
    if slot == 2:
        a = t["area"].get(str(area))
        if not a:
            return None
        rows, b = a["enemy"], band(t, level)
        r = rnd(R)
        j = next((j for j in range(len(rows) - 1) if r <= rows[j]["t"][b]), len(rows) - 1)
        out = rows[j]["id"]
    else:
        rt, rare = e["rare_bands"][band(t, level) // 2] if "rare_bands" in e else (e["rare_t"], e["rare"])
        if rnd(R) > rt:
            out = rare
        else:
            c = t["common"][e["common"]]
            out = c["ids"][pick(rnd(R), c["t"], "rare_bands" in e)]
    return ("item", out) if out != "00000000" else None


def roll_object(t, dtype, index, level, area=None, rnd=random.randrange):
    if not 0 <= dtype < 5 or not 0 <= index < len(t["object"][dtype]):
        return None
    rec = t["object"][dtype][index]
    if rec["type"] != dtype or rnd(R) > rec["chance"]:
        return None
    bd = rec["bands"][band(t, level)]
    k = pick(rnd(R), bd["t"])
    if k < 3:
        out = bd["ids"][k]
    else:
        a = t["area"].get(str(area))
        if not a:
            return None
        y = a["object"][dtype]
        out = y["ids"][pick(rnd(R), y["t"])]
    return ("item", out) if out != "00000000" else None


def main():
    a = sys.argv[1:]
    game = a[a.index("--game") + 1] if "--game" in a else "psu"
    out = a[a.index("--out") + 1] if "--out" in a else workdir.path(game, "gamedata")
    if a and a[0] == "roll":
        from collections import Counter
        t = json.load(open(os.path.join(out, "drops.json"), encoding="utf-8"))
        n = int(a[a.index("-n") + 1]) if "-n" in a else 10000
        args = [int(x) for x in a[2:] if x.lstrip("-").isdigit()][:4 if a[1] == "object" else 3]
        f = roll_enemy if a[1] == "enemy" else roll_object
        c = Counter()
        for _ in range(n):
            res = f(t, *args)
            c["nothing" if res is None else ("meseta" if res[0] == "meseta" else res[1])] += 1
        for k, v in c.most_common():
            print(f"{k:10} {v / n:8.2%}")
        return
    iso = workdir.iso_arg(a)
    build(iso, out, game)


if __name__ == "__main__":
    main()
