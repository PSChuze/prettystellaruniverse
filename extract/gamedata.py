"""Build the game tables the server needs from a disc.

  python gamedata.py [--game psu|aoti] [--iso "<disc>.iso"] [--out DIR]   (default DIR <work>/<game>/gamedata)
                     [--type-overrides psu_type_overrides.json|aoti_type_overrides.json [--type-fill linear|disc]]
                     [--plylevel-only]

--type-overrides replaces the type rows (HP/stat/race %), unlock requirements and type EXP curves of the types
the file names with published retail (US Network mode) values, with sources; see the file's _doc.
overlay/psu_type_overrides.json: types 0..9 as on Sega's servers before the Illuminus update (type level cap 10);
overlay/aoti_type_overrides.json: types 0..14 as after it (cap 20). The discs' own rows for 0..9 are the offline-mode
values (PSUPedia's "Offline" HU/RA/FO TL1 rows match them); online values were server side.
Type levels the file leaves null are interpolated between the published ones (--type-fill linear, default) or
keep the disc value (--type-fill disc); levels above a type's stat_cap (default: its cap) hold that row. Type EXP
totals above the cap continue the last step (the client table has more levels than retail allowed; nothing in
the table can stop levelling past the cap, so plylevel.json also gets type_caps for the server to clamp to).
The hidden S5 % stays as on the disc. --plylevel-only writes plylevel.json and nothing else.

--game aoti (SLUS-21631) reads <work>/aoti/cvm (cvm.py opens it with AotI's own module). Format differences
on the AotI disc:
  PlyLevelParamData.snr  same NSR layout (16 types x 16-byte rows, 8 race/gender tables x 24-byte rows);
                 pointers are biased by the member's own NBL data offset (PSU 0xa1880, AotI 0x95680);
                 30 type levels (PSU 10) and level rows populated to 200 (PSU 100). Types 10..15 (Acrofighter,
                 Acrotecher, Fighmaster, Gunmaster, Masterforce, Acromaster: PlayerText group 0 #48..53) are
                 placeholder rows on the disc (every % 100, type EXP 100/level to 10 then the base curve). The
                 client reads the same rows (AotI GetTablePtr 0x1f5670, Corr::Calc 0x1f5970: [table+0xc][cls],
                 row (tlevel-1)*16, +2 HP %, +4 8 stat %, +0xc race %).
  itemShopList.snr       root = {u32 46, ptr}: 46 x 2 bytes [list, category] (PSU: 16-byte records at +0x1C)
  XF_ITEMSHOPDATA.AFS    one NBL per category (_CC<Cat>.nbl) holding tiers as members NN.snr; each member
                 is PSU's itemShopDataNN_CC<Cat>.snr layout (root at +8 = 4 x {u32 count, u32 ptr}).

Extracts GA_PLYCOMMON.NBL, GA_ITEMPARAM.NBL, GA_TEXT_AE.NBL and XF_ITEMSHOPDATA.AFS from PSUROFS.CVM
(cvm.py), unpacks them (nbl.py) and writes:
  plylevel.json  level rows (per race*2+gender: [total EXP, HP, 8 stats] for levels 1..200), type rows
                 (per type: [type EXP total, HP %, 8 stat %, 4 race %] for type levels 1..10) and the
                 unlock requirements (16 x 16 s8), from PlyLevelParamData.snr (plylevel.py)
  items.json     id -> name, category, 20-byte constants block, price, ... (itemdb.py)
  weapons.json   per weapon type: melee hitbox entries, attack sound, max targets (itemWeaponParam_NN.snr)
  shops.json     NPC shops: npcs[id] = [list 0..3, category 0..12] (GA_ITEMPARAM itemShopList.snr, 46 x 16 B
                 at +0x1C) and tiers[NN][category] = 4 lists of 4-byte item ids (XF_ITEMSHOPDATA.AFS
                 itemShopDataNN_CC<Cat>.snr: root at +8 = 4 x {u32 count, u32 member-relative ptr})
  enemylevel.json  enemy base rows per level (SU_ENEMYLEVELBASEPARAM.NBL / EnemyLevelBaseParam.snr: NSR header,
                 0x18-byte rows at 0x10 + 0x18 * level, level 0 empty): [HP, ATP, DFP, ATA, EVP, STA, S5, TP,
                 MST, unknown, EXP]; per-enemy multipliers: enemydata.py (enemies.json)
"""
import glob
import json
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import itemdb  # noqa: E402
import plylevel  # noqa: E402
import workdir  # noqa: E402

NEEDED = ("GA_PLYCOMMON.NBL", "GA_ITEMPARAM.NBL", "GA_TEXT_AE.NBL", "XF_ITEMSHOPDATA.AFS", "SU_ENEMYLEVELBASEPARAM.NBL")


def run(*args):
    subprocess.run([sys.executable, os.path.join(HERE, args[0]), *args[1:]], check=True, stdout=subprocess.DEVNULL)


def shop_tiers(cvm, game):
    """tiers[NN][category] = 4 lists of 4-byte item ids, from the unpacked XF_ITEMSHOPDATA.AFS."""
    tiers = {}
    if game == "psu":
        files = [(f, os.path.basename(f)[:-4]) for f in glob.glob(os.path.join(cvm, "shop", "itemShopData*.nbl"))]
        members = [(f, int(base[12:14]), int(base[15:17]), base + ".snr") for f, base in files]   # itemShopDataNN_CC<Cat>
    else:
        members = []
        for f in glob.glob(os.path.join(cvm, "shop", "_[0-9][0-9]*.nbl")):
            cc = int(os.path.basename(f)[1:3])                                      # _CC<Cat>.nbl
            members += [(f, int(name[:2]), cc, name) for name in itemdb.load_nbl(f)[1] if name[:2].isdigit()]
    for f, nn, cc, name in sorted(members, key=lambda x: (x[0], x[1])):
        sd, sm = itemdb.load_nbl(f)
        off = sm[name]["off"]
        root, = struct.unpack_from("<I", sd, off + 8)       # member-relative; the list pointers are NBL-data
        lists = []                                          # absolute (PSU's single member sits at 0)
        for k in range(4):
            n, ptr = struct.unpack_from("<II", sd, off + root + 8 * k)
            lists.append([sd[ptr + 4 * i:ptr + 4 * i + 4].hex() for i in range(n)] if n < 500 else [])
        tiers.setdefault(nn, {})[cc] = lists
    return tiers


def shop_npcs(game):
    """npcs[i] = [list 0..3, category 0..12] from GA_ITEMPARAM itemShopList.snr."""
    d, m = itemdb.load_nbl(itemdb.ITEMPARAM)
    if game == "psu":
        sl = d[m["itemShopList.snr"]["off"]:]
        return [[sl[0x1C + 16 * i], sl[0x1D + 16 * i]] for i in range(46)]
    off = m["itemShopList.snr"]["off"]
    n, p = struct.unpack_from("<II", d, off + struct.unpack_from("<I", d, off + 8)[0])
    return [[d[p + 2 * i], d[p + 2 * i + 1]] for i in range(n)]


RACES = ("Human", "Newman", "Cast", "Beast")                     # race% column order (race*2+gender tables)
OV_TO_ROW = {"HP": 1, "ATP": 2, "DFP": 3, "ATA": 4, "EVP": 5, "STA": 6, "TP": 8, "MST": 9}   # row 7 = S5


def apply_type_overrides(types, reqs, names, path, fill="linear", caps=None):
    """Replace rows/requirements/type EXP of the types in an overrides file (format: its _doc).
    Returns {type: notes}; caps[c] = the type level cap of each overridden type."""
    ov = json.load(open(path, encoding="utf-8"))
    caps = {} if caps is None else caps
    cols = ov["columns"]
    notes = {}
    for name, t in ov["types"].items():
        c = names.index(name)
        rows = types[c]
        cap = min(t.get("cap", len(rows)), len(rows))
        stat_cap = min(t.get("stat_cap", cap), cap)
        known = {int(k): v for k, v in t["levels"].items()}
        filled = []
        for j, col in enumerate(cols):
            pts = sorted((lv, v[j]) for lv, v in known.items() if v[j] is not None)
            for tl in range(1, len(rows) + 1):
                lv = min(tl, stat_cap)
                exact = [v for k, v in pts if k == lv]
                if exact:
                    val = exact[0]
                else:
                    lo = [(k, v) for k, v in pts if k < lv]
                    hi = [(k, v) for k, v in pts if k > lv]
                    if fill == "linear" and lo and hi:
                        (l0, v0), (l1, v1) = lo[-1], hi[0]
                        val = int(v0 + (v1 - v0) * (lv - l0) / (l1 - l0) + 0.5)
                    else:
                        val = rows[tl - 1][OV_TO_ROW[col]]
                    if tl <= stat_cap:
                        filled.append((tl, col))
                rows[tl - 1][OV_TO_ROW[col]] = val
        race = t.get("race_pct", {})
        for tl in range(len(rows)):
            rows[tl][10:14] = [race.get(r, 100) for r in RACES]
        if "requirements" in t:
            unknown = set(t["requirements"]) - set(names)
            if unknown:
                raise SystemExit(f"{path}: {name} requires unknown types {sorted(unknown)}")
            reqs[c] = [t["requirements"].get(n, 0) for n in names]
        exp = None
        if "exp_curve" in t:
            exp = type_exp(ov["exp_curves"][t["exp_curve"]], cap, len(rows))
            for tl in range(len(rows)):
                rows[tl][0] = exp[tl]
        caps[c] = cap
        notes[name] = {"cap": cap, "stat_cap": stat_cap, "filled": filled, "exp": t.get("exp_curve")}
    return notes


def type_exp(curve, cap, n):
    """Cumulative type EXP per type level 1..n from a published curve (TL1 = 0; null entries interpolated
    linearly); levels past the cap continue the last step, capped at the u16 the client and save use."""
    known = [(i, v) for i, v in enumerate(curve) if v is not None]
    out = []
    for i in range(cap):
        lo = [(k, v) for k, v in known if k <= i][-1]
        hi = next(((k, v) for k, v in known if k >= i), lo)
        out.append(lo[1] if hi[0] == lo[0] else int(lo[1] + (hi[1] - lo[1]) * (i - lo[0]) / (hi[0] - lo[0]) + 0.5))
    step = out[-1] - out[-2] if cap > 1 else 1
    for i in range(cap, n):
        out.append(min(out[-1] + step, 0xFFFF))
    return out


def main():
    a = sys.argv[1:]
    game = itemdb.game_arg(a)
    cvm = itemdb.CVM
    iso = workdir.iso_arg(a)
    out = a[a.index("--out") + 1] if "--out" in a else itemdb.OUT
    overrides = a[a.index("--type-overrides") + 1] if "--type-overrides" in a else None
    fill = a[a.index("--type-fill") + 1] if "--type-fill" in a else "linear"
    if fill not in ("linear", "disc"):
        raise SystemExit("--type-fill: linear or disc")
    os.makedirs(out, exist_ok=True)
    missing = [n for n in NEEDED if not os.path.exists(os.path.join(cvm, n))]
    if missing:
        run("cvm.py", "x", workdir.need_iso(iso), cvm, *[x for n in missing for x in ("--match", n)], *itemdb.cvm_args())
    ply_dir = os.path.join(cvm, "plycommon")
    if not os.path.exists(os.path.join(ply_dir, "PlyLevelParamData.snr")):
        run("nbl.py", "x", os.path.join(cvm, "GA_PLYCOMMON.NBL"), ply_dir)
    shop_dir = os.path.join(cvm, "shop")
    if not os.path.exists(os.path.join(shop_dir, "item00ValueData.nbl")):
        run("nbl.py", "afs", os.path.join(cvm, "XF_ITEMSHOPDATA.AFS"), shop_dir)

    _d, pm = itemdb.load_nbl(os.path.join(cvm, "GA_PLYCOMMON.NBL"))
    lp = plylevel.LevelParam(open(os.path.join(ply_dir, "PlyLevelParamData.snr"), "rb").read(),
                             pm["PlyLevelParamData.snr"]["off"])
    levels = [[list(lp.level_row(rg // 2, rg % 2, lv)) for lv in range(1, lp.n_lv + 1)] for rg in range(lp.n_rg)]
    levels = [[[e, h, *st] for e, h, st in rows] for rows in levels]
    types = [[[t[0], t[1], *t[2], *t[3]] for t in (lp.type_row(c, tl) for tl in range(1, lp.n_tlv + 1))]
             for c in range(lp.n_types)]
    reqs = [lp.requirements(c) for c in range(lp.n_types)]
    names = list(plylevel.TYPES) + (list(plylevel.AOTI_TYPES) if game == "aoti" else [])
    caps = [len(rows) for rows in types]
    if overrides:
        path = overrides if os.path.exists(overrides) else os.path.join(HERE, "overlay", overrides)
        for name, n in apply_type_overrides(types, reqs, names, path, fill, caps).items():
            print(f"{name}: cap {n['cap']}, stats to type level {n['stat_cap']}, {len(n['filled'])} values "
                  f"{'interpolated' if fill == 'linear' else 'kept from the disc'}, type EXP "
                  f"{n['exp'] or 'from the disc'}")
    json.dump({"levels": levels, "types": types, "requirements": reqs, "type_caps": caps,
               "stat_order": list(plylevel.STATS), "type_names": names},
              open(os.path.join(out, "plylevel.json"), "w", encoding="utf-8"))
    if "--plylevel-only" in a:
        print(f"wrote {out}/plylevel.json")
        return
    run("itemdb.py", "--game", game, "--json", os.path.join(out, "items.json"))
    en_dir = os.path.join(cvm, "enemylevel")
    if not os.path.exists(os.path.join(en_dir, "EnemyLevelBaseParam.snr")):
        run("nbl.py", "x", os.path.join(cvm, "SU_ENEMYLEVELBASEPARAM.NBL"), en_dir)
    eb = open(os.path.join(en_dir, "EnemyLevelBaseParam.snr"), "rb").read()
    end, = struct.unpack_from("<I", eb, 8)
    rows = [list(struct.unpack_from("<I9hH", eb, o)) for o in range(0x10, end - 0x18 + 1, 0x18)]
    while rows and not any(rows[-1]):
        rows.pop()
    json.dump({"levels": rows}, open(os.path.join(out, "enemylevel.json"), "w", encoding="utf-8"))
    json.dump({"npcs": shop_npcs(game), "tiers": shop_tiers(cvm, game)},
              open(os.path.join(out, "shops.json"), "w", encoding="utf-8"))
    json.dump(itemdb.weapons_table(itemdb.Params()), open(os.path.join(out, "weapons.json"), "w", encoding="utf-8"),
              indent=0)
    print(f"wrote {out}/plylevel.json ({len(levels)} race/gender tables, {len(types)} types) and items.json")


if __name__ == "__main__":
    main()
