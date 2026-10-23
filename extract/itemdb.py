"""PS2 PSU item database from disc data, following the client's own offline lookup (PSU_US.erx
0xb4090 build-constants, 0xb81f0 name, 0xb4660 price). Output: id -> name, category, 20-byte
constants block (the +0x34..+0x48 part of an 0x0A0A/0x010A item-constants record), price.
AotI (--game aoti) follows its own client: builder 0x474e70, name 0xb1110, price 0xac830, category
validity 0xab5d0 (jump table 0x53eed0), param manager loader 0xafe50 (file name table 0x612ff0).

Item id: 4 bytes, written big-endian in packets: cat, type, index, manuf (manuf & 0x3f; bit 0x40 =
gift wrapped, bit 0x80 also masked by the client).

Usage: python itemdb.py [--game psu|aoti] [--json out.json] [--merge other.json] [--show 01090000 ...]
"""
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nbl  # noqa: E402
import workdir  # noqa: E402

# disc files, extracted by gamedata.py under <work>/<game>/cvm (workdir.py); tables go to <work>/<game>/gamedata
# unless --out says otherwise. AotI's PSUROFS.CVM opens with AotI's own password (cvm.py).
GAMES = workdir.GAMES
GAME = "psu"


def use_game(game):
    """Point the module's disc paths (CVM, ITEMPARAM, TEXT_AE, VALUE, ERX) at <work>/<game>/."""
    global GAME, CVM, ITEMPARAM, TEXT_AE, VALUE, ERX, OUT
    if game not in GAMES:
        raise SystemExit(f"--game: one of {', '.join(GAMES)}")
    GAME = game
    CVM = workdir.path(game, "cvm")
    ITEMPARAM = os.path.join(CVM, "GA_ITEMPARAM.NBL")
    TEXT_AE = os.path.join(CVM, "GA_TEXT_AE.NBL")
    VALUE = os.path.join(CVM, "shop", "item00ValueData.nbl")
    ERX = workdir.erx(game)
    OUT = workdir.path(game, "gamedata")


def game_arg(argv):
    """Remove `--game <psu|aoti>` from argv (in place), switch the paths, return the game."""
    game = "psu"
    if "--game" in argv:
        i = argv.index("--game")
        game = argv[i + 1]
        del argv[i:i + 2]
    use_game(game)
    return game


def cvm_args(game=None):
    """Extra cvm.py arguments for this game's disc: its own module, which holds its CVM password."""
    return ["--erx", ERX]


use_game("psu")

u8 = lambda d, o: d[o]
u16 = lambda d, o: struct.unpack_from("<H", d, o)[0]
u32 = lambda d, o: struct.unpack_from("<I", d, o)[0]


def load_nbl(path):
    raw = open(path, "rb").read()
    nb = nbl.parse(raw)
    data = nbl.nbl_data(raw, nb)
    return data, {m["name"]: m for m in nb["members"]}


class Params:
    def __init__(self):
        self.d, self.m = load_nbl(ITEMPARAM)

    def root(self, name):
        off = self.m[name]["off"]
        return off + u32(self.d, off + 8)   # header root is member-relative; inner pointers are absolute

    def typed(self, name, typ):
        """root = array over type-1 of {u32 count, u32 ptr}"""
        e = self.root(name) + (typ - 1) * 8
        return u32(self.d, e), u32(self.d, e + 4)

    def records12(self, name, slot, recsize):
        """AotI typed root: array of 12-byte {u32 recs, u32 map, s16 n_map, s16 n_recs} (lookup e.g. consume
        0x475c90, shield 0x475eb0, unit 0x475370). With a map: index < n_map, r = map[index] (0xff = none);
        without: r = index; r < n_recs. Yields (index, record offset)."""
        e = self.root(name) + slot * 12
        recs, mp, n_map, n_recs = struct.unpack_from("<IIhh", self.d, e)
        if not recs:
            return
        for idx in range(n_map if mp else n_recs):
            r = self.d[mp + idx] if mp else idx
            if r != 0xFF and r < n_recs:
                yield idx, recs + r * recsize


WEAPON_FILES = ["01DKSword", "02DKKnuckle", "03DKSpear", "04DK_DSaber", "05DKAxe", "06DK2Saber",
                "07DK2Dagger", "08DK2Claw", "09MKSaver", "10MKDagger", "11MKClaw", "12MKWhip",
                "13MKSlicer", "14DGRifle", "15DGShotgun", "16DGBow", "17DGGrenade", "18DG_LCan",
                "19DG2HGun", "20SGHGun", "21SG_Cbow", "22SGCard", "23SGMGun", "24DTRod", "25MTWand"]
MANUF = ["GRM", "Yohmei", "Tenora", "Kubara"]
CATS = {1: "weapon", 2: "armor(line shield)", 3: "consumable", 4: "PA disc (striking)",
        5: "PA disc (bullet)", 6: "PA disc (technique)", 7: "material", 8: "unit",
        9: "clothing", 10: "parts (CAST)", 11: "room goods", 12: "trap", 13: "synth board",
        14: "PM device", 15: "grinder", 17: "special (goggles/sonar)"}
# client table 0x5789d0: category -> (GA_TEXT member index, group base); weapons/armor special-cased
TEXT_MAP = {3: (4, 16), 4: (2, 0), 5: (2, 13), 6: (2, 23), 7: (4, 0), 8: (5, 3), 9: (3, 0),
            10: (3, 6), 11: (4, 22), 12: (4, 20), 13: (4, 24), 14: (4, 33), 15: (4, 34), 17: (4, 35)}
TEXT_FILES = ["BossText", "EnemyText", "ItemAbilityText", "ItemCostumeText", "ItemGeneralText",
              "ItemShieldText", "ItemWeapon01GRM_Text", "ItemWeapon02CON_Text", "ItemWeapon03TNR_Text",
              "MemoryCard", "ObjectText", "PlayerText", "QuestText", "WindowText", "WItemWeapon04KBR_Text"]
# AotI: GA_TEXT_AE gains DungeonLobbyText (file 1), every later file index shifts by one. Name lookup 0xb1110:
# table 0x5d3f90 = category -> (file, group); weapons file 7 + manuf (15 for manuf >= 3), group type-1;
# armor (6, manuf); others group + type-1; then 0x103a60(file, group, index).
AOTI_TEXT_FILES = TEXT_FILES[:1] + ["DungeonLobbyText"] + TEXT_FILES[1:]
AOTI_TEXT_MAP = {3: (5, 16), 4: (3, 0), 5: (3, 13), 6: (3, 23), 7: (5, 0), 8: (6, 3), 9: (4, 0),
                 10: (4, 6), 11: (5, 22), 12: (5, 20), 13: (5, 26), 14: (5, 44), 15: (5, 45), 17: (5, 46)}
AOTI_WEAPON_FILES = WEAPON_FILES + ["26STMag", "27SSMag"]


def parse_text(d):
    gt = u32(d, 4)
    first = u32(d, gt)
    groups = [u32(d, gt + 4 * i) for i in range((first - gt) // 4)]
    strstart = u32(d, groups[0])
    out = []
    for i, g in enumerate(groups):
        end = groups[i + 1] if i + 1 < len(groups) else strstart
        grp = []
        for k in range((end - g) // 4):
            o = u32(d, g + 4 * k)
            e = o
            while d[e:e + 2] != b"\0\0":
                e += 2
            grp.append(d[o:e].decode("utf-16-le", "replace"))
        out.append(grp)
    return out


class Texts:
    def __init__(self):
        d, m = load_nbl(TEXT_AE)
        self.files = {}
        self.aoti = GAME == "aoti"
        self.map = AOTI_TEXT_MAP if self.aoti else TEXT_MAP
        for i, base in enumerate(AOTI_TEXT_FILES if self.aoti else TEXT_FILES):
            mm = m[base + "_AE.bin"]
            self.files[i] = parse_text(d[mm["off"]:mm["off"] + mm["size"]])

    def get(self, f, g, i):
        try:
            return self.files[f][g][i]
        except IndexError:
            return None

    def name(self, iid):
        cat, typ, idx, man = iid[0], iid[1], iid[2], iid[3] & 0x3F
        w = 1 if self.aoti else 0   # AotI file index shift (0xb1110)
        if cat == 1:
            return self.get(6 + w + man if man < 3 else 14 + w, typ - 1, idx)
        if cat == 2:
            return self.get(5 + w, man, idx)
        if cat in self.map:
            f, g = self.map[cat]
            return self.get(f, g + typ - 1, idx)
        return None


class Values:
    """item00ValueData.snr (XF_ITEMSHOPDATA.AFS): root[cat-1] = {u32 n_manuf, ptr};
    entry[manuf] = {u16 n, u16 rate, ptr}; sorted {u32 key = id BE with manuf&0x3f, u32 base}."""
    def __init__(self):
        self.d, m = load_nbl(VALUE)
        off = m["item00ValueData.snr"]["off"]
        self.root = off + u32(self.d, off + 8)
        self.table = {}
        for cat in range(1, 20):
            e = self.root + (cat - 1) * 8
            if e + 8 > len(self.d):
                break
            n, p = u32(self.d, e), u32(self.d, e + 4)
            if not p or n > 64:
                continue
            for man in range(n):
                cnt, rate, lst = u16(self.d, p + 8 * man), u16(self.d, p + 8 * man + 2), u32(self.d, p + 8 * man + 4)
                for k in range(cnt):
                    key, base = u32(self.d, lst + 8 * k), u32(self.d, lst + 8 * k + 4)
                    self.table[key] = (base, rate)

    def price(self, iid):
        key = (iid[0] << 24) | (iid[1] << 16) | (iid[2] << 8) | (iid[3] & 0x3F)
        if key not in self.table:
            return None, None
        base, rate = self.table[key]
        if GAME == "aoti":
            # AotI 0xac830: integer math, (base / 10000) * rate when base % 10000 == 0, else
            # s32(base * rate) / 10000 (truncating); then abs, <= 0 -> 1. Same table walk (0xac630/0xac710).
            if base % 10000 == 0:
                v = (base // 10000) * rate
            else:
                p = (base * rate) & 0xFFFFFFFF   # s32 product (mult, mflo)
                v = abs(p - (1 << 32) if p >= 1 << 31 else p) // 10000
            v = abs(v)
            return base, (v if v > 0 else 1)
        v = int(base * rate / 10000.0)
        return base, (v if v > 0 else 1)


def weapon_consts(P, typ):
    """yield (id bytes, 20-byte constants, extra dict) for weapon type typ (1..25)"""
    name = "itemWeaponParam_%s.snr" % WEAPON_FILES[typ - 1]
    d = P.d
    r = P.root(name)
    sound = u32(d, r + 4)
    nhit = d[r + 0x50]
    b51, b52 = d[r + 0x51], d[r + 0x52]
    for man in range(4):
        recs = u32(d, r + 8 + 4 * man)
        if not recs:
            continue
        for idx in range(14):
            n = d[r + 0x18 + man * 14 + idx]
            if n >= 0x0F:
                continue
            rec = recs + n * 0x40
            if d[rec + 3] >= nhit:
                continue
            grind = 0
            for k in range(10):
                if d[rec + 0x2C + k] == 0 and d[rec + 0x36 + k] == 0:
                    break
                grind += 1
            c = bytearray(20)
            struct.pack_into("<HHHHH", c, 0, u16(d, rec + 0x18), u16(d, rec + 8), u16(d, rec + 0xA),
                             u16(d, rec + 0xE), u16(d, rec + 0x10))
            c[0xA], c[0xB], c[0xC] = d[rec + 0x12], d[rec + 0x13], d[rec + 0x14]
            c[0xD], c[0xE], c[0xF] = b51, b52, d[rec + 0x16] & 0xF
            c[0x11], c[0x12] = grind, d[rec + 0]
            yield bytes([1, typ, idx, man]), bytes(c), {"sound": sound, "rec": d[rec:rec + 0x40].hex()}


def weapons_table(P):
    """weapons.json: per weapon type (item id byte 1) the melee hitbox block the S>C 0x0105 equip reply carries
    at +0x3C (0xb9e90): itemWeaponParam_NN*.snr root: +0 -> 5 x 8-byte hitbox entries (s16 range x10, height,
    angle 1, angle 2), +4 u32 attack sound, +0x50 u8 max targets."""
    weapons = {}
    for name in P.m:
        if not name.startswith("itemWeaponParam_"):
            continue
        r = P.root(name)
        hb_ptr, sound = struct.unpack_from("<II", P.d, r)
        hb = [P.d[hb_ptr + 8 * k:hb_ptr + 8 * k + 8].hex() for k in range(5)]
        if GAME == "aoti":
            # same root layout (0x476120); AotI files hold only root+0x50 hitboxes (records need rec+3 below
            # it), so entries past that are zero instead of the following bytes
            hb = [h if k < P.d[r + 0x50] else "00" * 8 for k, h in enumerate(hb)]
        weapons[int(name[16:18])] = {"file": name, "sound": sound, "targets": P.d[r + 0x50], "hitboxes": hb}
    return weapons


def weapon_consts_aoti(P, typ):
    """AotI: yield (id bytes, 20-byte constants, extra dict) for weapon type typ (1..27; validity 0xab570).
    Fetch 0x476120: root +0 hitbox ptr, +4 sound, +8 + 4*manuf record array, +0x18 + manuf*14 + index s8
    record number (0..0x0e), +0x50 hitbox count (record +3 must be below it), +0x51/+0x52 hand/style.
    Records are 0x26 bytes (PSU 0x40; no grind tables, AotI grinds from itemWeaponRefineSetting, see
    weapon_grind). Builder 0x474e70 case 1: c+0 PP = rec+0x14, c+2 ATP = rec+6, c+4 ATA = rec+8,
    c+6 req = rec+0xe, c+8 = 0xffff, c+0xa = rec+0xd, c+0xb element = rec+0x10 >> 4,
    c+0xc element % = rec+0x11, c+0xd/+0xe = root+0x51/+0x52, c+0xf rank = rec+0x10 & 0xf, c+0x10 = 0,
    c+0x11 max grind = 10 when the record exists (0x476020), c+0x12 = rec+0."""
    name = "itemWeaponParam_%s.snr" % AOTI_WEAPON_FILES[typ - 1]
    d = P.d
    r = P.root(name)
    sound = u32(d, r + 4)
    nhit, b51, b52 = d[r + 0x50], d[r + 0x51], d[r + 0x52]
    for man in range(4):
        recs = u32(d, r + 8 + 4 * man)
        if not recs:
            continue
        for idx in range(14):
            n = struct.unpack_from("<b", d, r + 0x18 + man * 14 + idx)[0]
            if n < 0 or n >= 0x0F:
                continue
            rec = recs + n * 0x26
            if d[rec + 3] >= nhit:
                continue
            c = bytearray(20)
            struct.pack_into("<HHHHH", c, 0, u16(d, rec + 0x14), u16(d, rec + 6), u16(d, rec + 8),
                             u16(d, rec + 0xE), 0xFFFF)
            c[0xA] = d[rec + 0xD]
            c[0xB] = (struct.unpack_from("<b", d, rec + 0x10)[0] >> 4) & 0xFF
            c[0xC], c[0xD], c[0xE], c[0xF] = d[rec + 0x11], b51, b52, d[rec + 0x10] & 0xF
            c[0x11], c[0x12] = 10, d[rec]
            yield bytes([1, typ, idx, man]), bytes(c), {"sound": sound, "rec": d[rec:rec + 0x26].hex()}


def refine_settings():
    """AotI itemWeaponRefineSetting.snr (XF_ITEMSHOPDATA.AFS, name 0x612fdc, loaded by 0x9a890):
    per weapon type 0x48 bytes {s32 pp, s32 atp, s32 rand, f32 mult[15]}."""
    d, m = load_nbl(os.path.join(CVM, "shop", "itemWeaponRefineSetting.nbl"))
    off = m["itemWeaponRefineSetting.snr"]["off"]
    r = off + u32(d, off + 8)
    out = {}
    for t in range(1, len(AOTI_WEAPON_FILES) + 1):
        pp, atp, rnd, *mult = struct.unpack_from("<3i15f", d, r + (t - 1) * 0x48)
        out[t] = {"pp": pp, "atp": atp, "rand": rnd, "mult": [round(x, 4) for x in mult]}
    return out


def weapon_grind(P):
    """{id: {rank, max_grind, pp_inc[10], atp_inc[10]}} per weapon.
    PSU: itemWeaponParam_* record +0x2C / +0x36 (fetch 0xb2a50 copies them to +0x38 / +0x42), the same
    values econdata.grind_tables uses.
    AotI: no per-grind tables; pp_inc/atp_inc are None and "refine" carries the type's
    itemWeaponRefineSetting entry. Success (manager vtable 0x650f80 slot 3 = 0xafc90): with
    m = mult[item +0x13 & 0x3f (0xabec0)], max PP += pp + max(1, int(m * (rand(n) + 1))),
    ATP bonus += atp + max(1, int(m * (rand(n) + 1))) (rand 0xac620, both clamped 0..9999), grind += 1
    (0xab8a0). Failure (0x299950 at 0x299aec): max PP = record PP, ATP bonus 0, grind 0, failure count
    (item +0x17 high nibble) += 1; 0x474c60 shows max grind - failures. Chance is PSU's formula
    (0x299a70: 10000 - (100 - success[rank][grind]) * (100 - 5 * grinder bonus))."""
    out = {}
    if GAME == "aoti":
        rs = refine_settings()
        for typ in range(1, len(AOTI_WEAPON_FILES) + 1):
            for iid, c, ex in weapon_consts_aoti(P, typ):
                out[iid.hex()] = {"rank": c[0xF], "max_grind": c[0x11], "pp_inc": None, "atp_inc": None,
                                  "refine": rs[typ]}
        return out
    for typ in range(1, 26):
        for iid, c, ex in weapon_consts(P, typ):
            rec = bytes.fromhex(ex["rec"])
            out[iid.hex()] = {"rank": c[0xF], "max_grind": c[0x11], "pp_inc": list(rec[0x2C:0x36]),
                              "atp_inc": list(rec[0x36:0x40])}
    return out


def valid_aoti(iid):
    """AotI item id validity 0xab5d0 (jump table 0x53eed0; manuf = byte 3 & ~0xc0). Only the category and type
    are checked (and byte 2 for 17); no table says whether an item "exists"."""
    cat, typ, idx, man = iid[0], iid[1], iid[2], iid[3] & 0x3F
    rng = {3: 4, 7: 16, 8: 4, 9: 6, 10: 6, 12: 2, 13: 18}   # 0xab4f0 0xab4c0 0xab490 0xab460 0xab430 0xab400
    if cat == 1:
        return 1 <= typ < 0x1C and man < 4                 # 0xab570
    if cat == 2:
        return typ == 1 and man < 4                        # 0xab520
    if cat in rng:
        return 1 <= typ <= rng[cat]
    if cat == 17:
        return (typ == 1 and idx < 2) or (typ == 2 and idx < 5)   # 0xab3a0
    return cat in (4, 5, 6, 11, 14, 15, 18)


# builder 0x474e70 categories that need a parameter record (a missing record makes it return 0); every other
# valid category is built from the id alone (zero constants), with the name lookup returning NULL past its group
AOTI_RECORD_CATS = (1, 2, 3, 8, 9, 10, 12, 15)


def board_refs():
    """Item ids AotI's synthesis tables (XF_BOARDPARAM.NBL, when extracted) name: board ids of rows with outputs,
    outputs, materials and failure items (FailureTbl 12-byte {id, weight, n} entries)."""
    path = os.path.join(CVM, "XF_BOARDPARAM.NBL")
    if not os.path.exists(path):
        return set()
    d, m = load_nbl(path)
    mem = {k: d[v["off"]:v["off"] + v["size"]] for k, v in m.items()}
    ids = set()
    for name, b in mem.items():
        if not name.endswith("Param.snr"):
            continue
        cat = name[:-9]
        out = mem[cat + "Output.snr"]
        mats = [mem["%sMaterial%d.snr" % (cat, k)] for k in (1, 2, 3, 4)]
        for i in range(u16(b, 0x10)):
            outs = [out[0x18 + i * 0xA4 + 20 * k:0x1C + i * 0xA4 + 20 * k] for k in range(8)]
            if any(any(o) for o in outs):
                ids.add(b[0x14 + 5 * i:0x18 + 5 * i].hex())
            ids.update(o.hex() for o in outs)
            for mt in mats:
                ids.update(mt[0x18 + i * 0x44 + 8 * v:0x1C + i * 0x44 + 8 * v].hex() for v in range(8))
    f = mem["FailureTbl.snr"][0x10:]
    for g in range(1, u32(f, 0) + 1):
        for k in range(10):
            o = 4 + (10 * g + k) * 12
            if u32(f, o + 4):
                ids.add(f[o:o + 4].hex())
    return {i for i in ids if i not in ("00000000", "ffffffff")}


def drop_refs(path):
    """Item ids in a drops.json (enemy rares, common/area/object lists; meseta_id excluded)."""
    if not path or not os.path.exists(path):
        return set()
    import re
    t = json.load(open(path, encoding="utf-8"))
    ids = set(re.findall(r'"([0-9a-f]{8})"', json.dumps({k: v for k, v in t.items() if k != "meseta_id"})))
    return ids - {"00000000", t.get("meseta_id", "12000000")}


# AotI grinders 20..39 (itemRefineParam: bonus 11, 13, 14, 15, 16 per rank) have no text (ItemGeneralText group
# 45 holds 20 names, so 0x3b3830 returns NULL) and no item00ValueData price (0xac710 -> 0xfa0a1f00, unsellable).
# The server sends names in the constants record, so they get the disc's naming pattern continued (C+1..C+5 are
# bonus 1, 3, 5, 7, 9): "Grinder <rank>+6".."+10", marked name_derived.
GRINDER_RANKS = "?CBAS"


def all_items_aoti(P, T, V, refs=()):
    """AotI builder 0x474e70 (category switch; price 0xac830 first). Handled: 1 weapon (0x476120),
    2 armor (0x475eb0 + pack 0x474180), 3 consumable (0x475c90), 8 unit (0x475dc0), 9/10 clothing/parts
    (0x475ae0), 12 trap (0x475940), 15 grinder (0x475750). Everything else is name + price only.
    refs: ids the disc's own tables hand out (board_refs, drop_refs); a valid text-only id without a name is
    added with name None ("unnamed": the client builds it, shows no name and finds no recipe for it)."""
    d = P.d
    items = {}

    def add(iid, consts, extra=None):
        base, price = V.price(iid)
        items[iid.hex()] = {"id": iid.hex(), "cat": iid[0], "cat_name": CATS.get(iid[0], "?"),
                            "name": T.name(iid), "consts": consts.hex(), "value_base": base,
                            "price": price, **(extra or {})}

    for typ in range(1, len(AOTI_WEAPON_FILES) + 1):
        for iid, c, ex in weapon_consts_aoti(P, typ):
            add(iid, c, {"manuf": MANUF[iid[3]], "pp": u16(c, 0), "atp": u16(c, 2), "ata": u16(c, 4),
                         "req": u16(c, 6), "ele": c[0xB], "ele_pct": c[0xC], "rank": c[0xF],
                         "max_grind": c[0x11], "sound": ex["sound"],
                         "rarity0": int(ex["rec"][4:6], 16), "hand": c[0xD], "style": c[0xE]})
    # armor: one itemShieldParam.snr, typed root slot = manuf (validity 0xab520: type 1, manuf < 4),
    # 0x18-byte records; pack 0x474180: c+0 = rec+0x14, c+2 = rec+8, c+4 = rec+0xa, c+6 = rec+0xe,
    # c+8 = rec+0x10, c+0xa = rec+0x16, c+0xc = rec+0 << 4 | rec+1, c+0xd = rec+2, c+0xe = rec+0x12,
    # c+0xf = rec+0x13
    for man in range(4):
        for idx, rec in P.records12("itemShieldParam.snr", man, 0x18):
            c = bytearray(20)
            struct.pack_into("<HHHHHH", c, 0, u16(d, rec + 0x14), u16(d, rec + 8), u16(d, rec + 0xA),
                             u16(d, rec + 0xE), u16(d, rec + 0x10), u16(d, rec + 0x16))
            c[0xC] = ((d[rec] << 4) | d[rec + 1]) & 0xFF
            c[0xD], c[0xE], c[0xF] = d[rec + 2], d[rec + 0x12], d[rec + 0x13]
            add(bytes([2, 1, idx, man]), bytes(c), {"manuf": MANUF[man], "rec": d[rec:rec + 0x18].hex()})

    def typed_cat(cat, snr, ntypes, recsize, pack):
        for typ in range(1, ntypes + 1):
            rs = recsize(typ) if callable(recsize) else recsize
            for idx, rec in P.records12(snr, typ - 1, rs):
                add(bytes([cat, typ, idx, 0]), pack(rec), {"rec": d[rec:rec + rs].hex()})

    # consumable 0x475c90: types 1..4 (0xab4f0), 8-byte records copied to c+0..7
    typed_cat(3, "itemConsumeParam.snr", 4, 8, lambda r: d[r:r + 8] + bytes(12))
    # unit 0x475dc0 / 0x475370: types 1..4 (0xab490), 12-byte records (16 for type 2); c+0..0xf =
    # rec+0..0xf (the client copies 4 halfwords from rec+8 even for 12-byte records)
    typed_cat(8, "itemLineUnitParam.snr", 4, lambda t: 16 if t == 2 else 12, lambda r: d[r:r + 16] + bytes(4))
    # clothing / parts 0x475ae0: types 1..6 (0xab460), 16-byte records copied to c+0..0xf
    typed_cat(9, "itemSuitParam.snr", 6, 16, lambda r: d[r:r + 16] + bytes(4))
    typed_cat(10, "itemPartsParam.snr", 6, 16, lambda r: d[r:r + 16] + bytes(4))

    def trap(r):   # 0x475940 + builder 0x475094: c+4 = rec+0x12, c+6 = rec+0x11, c+7 = rec+2
        c = bytearray(20)
        struct.pack_into("<H", c, 4, u16(d, r + 0x12))
        c[6], c[7] = d[r + 0x11], d[r + 2]
        return bytes(c)
    typed_cat(12, "itemTrapParam.snr", 2, 0x14, trap)   # types 1..2 (0xab430), 0x14-byte records
    # grinder 0x475750: itemRefineParam {u32 n, u32 ptr}, 2-byte records -> c+0, c+1
    r = P.root("itemRefineParam.snr")
    n, p = u32(d, r), u32(d, r + 4)
    for idx in range(n):
        iid = bytes([15, 1, idx, 0])
        add(iid, d[p + idx * 2:p + idx * 2 + 2] + bytes(18))
        it = items[iid.hex()]
        if not it["name"] and idx >= 20:
            b1 = d[p + idx * 2 + 1]
            it["name"] = "Grinder %s+%d" % (GRINDER_RANKS[b1 & 7], 6 + (idx - 20) % 5)
            it["name_derived"] = True
    # text-only categories (no case in 0x474e70): type ranges from the text groups (0x5d3f90) and
    # validity 0xab5d0 (7: 1..16, 13: 1..18, 17: 1..2; 4/5/6/11/14 any type)
    ntypes = {4: 13, 5: 10, 6: 7, 7: 16, 11: 4, 13: 18, 14: 1, 17: 2}
    for cat, nt in ntypes.items():
        f, g = AOTI_TEXT_MAP[cat]
        for typ in range(1, nt + 1):
            for idx, s in enumerate(T.files[f][g + typ - 1]):
                if s:
                    add(bytes([cat, typ, idx, 0]), bytes(20))
    for k in sorted(refs):
        iid = bytes.fromhex(k)
        if k not in items and valid_aoti(iid) and iid[0] not in AOTI_RECORD_CATS:
            add(iid, bytes(20), {"unnamed": True})
    return items


def all_items(refs=()):
    P, T, V = Params(), Texts(), Values()
    if GAME == "aoti":
        return all_items_aoti(P, T, V, refs)
    d = P.d
    items = {}

    def add(iid, consts, extra=None):
        base, price = V.price(iid)
        items[iid.hex()] = {"id": iid.hex(), "cat": iid[0], "cat_name": CATS.get(iid[0], "?"),
                            "name": T.name(iid), "consts": consts.hex(), "value_base": base,
                            "price": price, **(extra or {})}

    for typ in range(1, 26):
        for iid, c, ex in weapon_consts(P, typ):
            add(iid, c, {"manuf": MANUF[iid[3]], "pp": u16(c, 0), "atp": u16(c, 2), "ata": u16(c, 4),
                         "req": u16(c, 6), "ele": c[0xB], "ele_pct": c[0xC], "rank": c[0xF],
                         "max_grind": c[0x11], "sound": ex["sound"],
                         "rarity0": int(ex["rec"][4:6], 16), "hand": c[0xD], "style": c[0xE]})
    for man in range(3):   # armor: itemShieldParam_0{1,2,3}, {count, ptr}, 0x18-byte records
        name = ["itemShieldParam_01Parum.snr", "itemShieldParam_02ND.snr", "itemShieldParam_03Motwob.snr"][man]
        r = P.root(name)
        n, p = u32(d, r), u32(d, r + 4)
        for idx in range(n):
            rec = p + idx * 0x18
            c = bytearray(20)
            struct.pack_into("<HHHHHH", c, 0, u16(d, rec + 0x14), u16(d, rec + 8), u16(d, rec + 0xA),
                             u16(d, rec + 0xE), u16(d, rec + 0x10), u16(d, rec + 0x16))
            c[0xC], c[0xD], c[0xE] = d[rec + 1], d[rec + 2], d[rec + 0x12]
            add(bytes([2, 1, idx, man]), bytes(c), {"manuf": MANUF[man], "rec": d[rec:rec + 0x18].hex()})

    def typed_cat(cat, snr, recsize, pack, ntypes=16):
        for typ in range(1, ntypes + 1):
            try:
                n, p = P.typed(snr, typ)
            except struct.error:
                break
            if not p or n > 255:
                continue
            rs = recsize(typ) if callable(recsize) else recsize
            for idx in range(n):
                rec = p + idx * rs
                add(bytes([cat, typ, idx, 0]), pack(d, rec), {"rec": d[rec:rec + rs].hex()})

    root_types = lambda snr: (P.m[snr]["size"] - (P.root(snr) - P.m[snr]["off"])) // 8
    typed_cat(3, "itemConsumeParam.snr", 8, lambda d, r: d[r:r + 8] + bytes(12), 4)
    typed_cat(8, "itemLineUnitParam.snr", lambda t: 16 if t == 2 else 12, lambda d, r: d[r:r + 16] + bytes(4), 4)
    typed_cat(9, "itemSuitParam.snr", 10, lambda d, r: d[r:r + 10] + bytes(10), 6)
    typed_cat(10, "itemPartsParam.snr", 10, lambda d, r: d[r:r + 10] + bytes(10), 6)

    def trap(d, r):
        c = bytearray(20)
        struct.pack_into("<HHH", c, 0, u16(d, r + 0xE), u16(d, r + 0x12), u16(d, r + 0x14))
        c[6], c[7], c[8] = (d[r + 1] >> 2) & 7, d[r + 2], d[r + 3]
        return bytes(c)
    typed_cat(12, "itemTrapParam.snr", 0x16, trap, 2)
    for cat, snr, rs in ((14, "itemEvolutionParam.snr", 8), (15, "itemRefineParam.snr", 2)):
        r = P.root(snr)
        n, p = u32(d, r), u32(d, r + 4)
        for idx in range(n):
            add(bytes([cat, 1, idx, 0]), d[p + idx * rs:p + idx * rs + rs] + bytes(20 - rs))
    # text-only categories (no per-item constants in the client's switch: 4-7, 11, 13, 17)
    for cat in (4, 5, 6, 7, 11, 13, 17):
        f, g = TEXT_MAP[cat]
        for typ in range(1, 40):
            gi = g + typ - 1
            if gi >= len(T.files[f]) or (cat in (4,) and gi >= 13) or (cat == 5 and gi >= 23) \
                    or (cat == 7 and gi >= 16) or (cat == 11 and gi >= 23) or (cat == 13 and gi >= 33) \
                    or (cat == 17 and gi >= 37):
                break
            for idx, s in enumerate(T.files[f][gi]):
                if s:
                    add(bytes([cat, typ, idx, 0]), bytes(20))
    return items


def main():
    game_arg(sys.argv)
    out = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
    refs = set()
    if GAME == "aoti":
        # drops.json from the same output dir (enemydata/dropdata --game aoti), when it is there
        drops = os.path.join(os.path.dirname(os.path.abspath(out)) if out else OUT, "drops.json")
        refs = board_refs() | drop_refs(drops)
    items = all_items(refs)
    if "--merge" in sys.argv:
        # --merge <other items.json>: ids only the other disc has, appended with "other_disc": true (for
        # characters shared across discs; this disc's client cannot build or name them)
        other = json.load(open(sys.argv[sys.argv.index("--merge") + 1], encoding="utf-8"))
        items.update({k: {**v, "other_disc": True} for k, v in other.items() if k not in items})
    if out:
        json.dump(items, open(out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    cnt = {}
    for it in items.values():
        cnt[it["cat_name"]] = cnt.get(it["cat_name"], 0) + 1
    print("total", len(items))
    for k, v in sorted(cnt.items()):
        print("  %-26s %d" % (k, v))
    show = [a for a in sys.argv[1:] if len(a) == 8 and all(c in "0123456789abcdefABCDEF" for c in a)]
    for s in show:
        it = items.get(s.lower())
        print(json.dumps(it, ensure_ascii=False) if it else "%s: not found" % s)


if __name__ == "__main__":
    main()
