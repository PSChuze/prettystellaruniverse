"""Economy tables for the server from the disc + PSU_US.erx.

  python econdata.py [--game psu|aoti] [--iso "<disc>.iso"] [--out DIR]   (default DIR <work>/<game>/gamedata;
                                                reads DIR/items.json from gamedata.py)

Writes grind.json, probo.json, synthesis.json, lumilass.json. Offsets below are PSU_US.erx image addresses
(RAM = image + 0x248000). Formulas are documented in each JSON's "notes"; they were read from the offline
room/server code (the PS2 client carries its own "server": TGamePlayer / TMyRoomManager / TItemProductManager).

grind.json      (TGamePlayer::UseItem 0x296d20, rate query TGamePlayerItem::RcvItemMisk 0x299ba0 / 0x299a70)
  success[rank][grind]  % base rate, itemCommonParam.snr root+0x12+rank*10 (rows 1..4 = weapon rank C..S)
  grinders[id]          {rank: b1&7, bonus: (b1>>3)&0x1f, stack: b0} from itemRefineParam.snr (2 bytes each)
  weapons[id]           {rank, max_grind, pp_inc[10], atp_inc[10]} from itemWeaponParam_* record +0x2C/+0x36
probo.json      (give handler 0x2399a0, growth 0x37f6e0 -> 0x37e470, evolve 0x37f450, device 0x239e80)
  coef[stat][kind]      Evo_param.snr (SS_PA_EVOLUTION.NBL), 5 x 19 f32
  next[type]            evolution candidates (ERX 0x558fc0, 2 x s8 per type)
  req[n]                [total level, lv0..lv4] needed to become type n (ERX 0x59cd70, 13 x 6 s16)
  device[idx][type]     new type for PM Device index idx on current type, -1 = refused (ERX 0x595a30, 37 x 41 s8)
synthesis.json  (TItemProductParamManager 0x415ec0 loads XF_BOARDPARAM.NBL; recipe lookup 0x4159b0)
  boards[id]            {cat, index, level, freq_max, variant_board, slot_types[4], outputs[8], materials[4][8]}
  failure[group]        weighted random items (FailureTbl.snr; roll 0x414af0)
  element[e]            [[percent, weight] x 5] (ElementTbl.snr; roll 0x414940)
lumilass.json   (TBeautyParlor 0x362c30.., GA_PLYMAKECOMMON.NBL selectable*.snr)
  selectable[race*2+gender][slot]  appearance part words (slot 5 = hair) offered at character creation;
  debug[...]            the larger selectableDeb.snr lists (all hair styles the client can draw)

AotI (--game aoti, <work>/aoti/erx/PSU_US.erx). Same schemas; changes:
grind.json      rate 0x29c710 (= PSU 0x299a70, same formula, still param manager +0xc = itemCommonParam root);
                use 0x299950 (no direct caller).
  success         same place and shape, new values
  grinders        itemRefineParam 40 entries (PSU 20), same 2-byte encoding
  weapons         itemdb.weapon_grind(P): pp_inc/atp_inc None (records are 0x26 bytes, no grind tables)
  refine[type]    itemWeaponRefineSetting.snr (XF_ITEMSHOPDATA.AFS; name 0x612fdc, loader 0x9a890): 0x48 bytes
                  per weapon type {s32 pp, s32 atp, s32 rand, f32 mult[15]}; success 0xafc90 (vtable 0x650f80+0xc)
                  failure resets the weapon (0x299aec), max grind shrinks per failure (0x474c60)
probo.json      growth 0x37e450 -> 0x37d330 (round constant 0.9 at 0x568a54), evolve 0x37e1d0, level 0x37de30,
                give handler 0x23c7c0, gift limit 0x23f090: same logic as PSU
  coef            Evo_param.snr identical
  next            ERX 0x5689f0 (same values); req ERX 0x5fe110 (same values)
  device          68 x 41 s8 at ERX 0x5f6fb0 (0x23ced0; PSU 37 x 41, rows 0..36 identical), index < 0x44
  device_need[i]  68 u16 at ERX 0x5f7aa0 (0x23ce60): refused when stat 4 (+0x2E80) < need (0x23cdc0)
  devices         {} (itemEvolutionParam.snr is gone; the give path only uses device/device_need)
synthesis.json  loader 0x41b990 (12 slots: Param/Output/Material name indexes at 0x604570/0x6045a0/0x6045d0),
                board type -> slot 0x41b790, row by id byte 2 0x41b870, recipe 0x41b3c0, variant 0x41b2b0,
                rate 0x4031e0, roll 0x41ae60, failure 0x41a550, element 0x41a380
  boards          same record layouts; cat = loader slot (AOTI_BOARD_CATS), board type = Param header u16;
                  level/board_b1 from itemBoardParam 12-byte typed root (board lookup 0x475850, init 0xac980)
  failure[g]      FailureTbl: u32 count, then flat 12-byte {4s id, u32 weight, u32 n} at 4 + (10g + k) * 12;
                  weights out of 1000 (PSU 6-byte entries, out of 100)
  element[e]      ElementTbl: 0xa4-byte rows {u32 ?, 20 x {u32 percent, u32 weight}}, weights out of 1000
lumilass.json   selectable.snr same format (no selectableDeb.snr: debug = []); TBeautyParlor 0x1A03 0x364040
"""
import json
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import erx  # noqa: E402
import itemdb  # noqa: E402
import workdir  # noqa: E402

CVM, ERX = itemdb.CVM, itemdb.ERX           # set per game by main()

NEEDED = ("GA_ITEMPARAM.NBL", "XF_BOARDPARAM.NBL", "SS_PA_EVOLUTION.NBL", "GA_PLYMAKECOMMON.NBL")
u16 = lambda d, o: struct.unpack_from("<H", d, o)[0]
u32 = lambda d, o: struct.unpack_from("<I", d, o)[0]
BOARD_CATS = ["WpnKnock", "WpnShoot", "WpnTech", "Shield", "Consume", "Material", "Disk", "EvoParts", "Goods"]
# AotI loader slot order (0x41b990); board types 1 2 3 4 5 6 9 10 11 12 13 18
AOTI_BOARD_CATS = ["WpnKnock", "WpnShoot", "WpnTech", "Shield", "Consume", "Material", "Goods",
                   "WpnKnock2", "WpnShoot2", "WpnTech2", "Shield2", "Goods2"]


def members(name):
    d, m = itemdb.load_nbl(os.path.join(CVM, name))
    return {k: d[v["off"]:v["off"] + v["size"]] for k, v in m.items()}, d, m


class Erx:
    def __init__(self):
        self.im = erx.Image(ERX)

    def read(self, va, n):
        o = self.im.va2off(va)
        return self.im.d[o:o + n]


def grind_tables(P):
    d = P.d
    r = P.root("itemCommonParam.snr")
    success = [list(struct.unpack_from("<10b", d, r + 0x12 + rank * 10)) for rank in range(5)]
    n, p = u32(d, P.root("itemRefineParam.snr")), u32(d, P.root("itemRefineParam.snr") + 4)
    grinders = {}
    for i in range(n):
        b0, b1 = d[p + 2 * i], d[p + 2 * i + 1]
        grinders[f"0f01{i:02x}00"] = {"rank": b1 & 7, "bonus": (b1 >> 3) & 0x1F, "stack": b0}
    weapons = {}
    for typ in range(1, 26):
        for iid, c, ex in itemdb.weapon_consts(P, typ):
            rec = bytes.fromhex(ex["rec"])
            weapons[iid.hex()] = {"rank": c[0xF], "max_grind": c[0x11], "pp_inc": list(rec[0x2C:0x36]),
                                  "atp_inc": list(rec[0x36:0x40])}
    return {
        "notes": [
            "C>S 0x0A09 sub 1 (body +0x24 u8 1, +0x25 u8 grinder slot, +0x26 u16 weapon slot) asks the rate; reply is the "
            "same packet with body +0x26 u16 = chance in 1/100 % (0xffff = invalid)",
            "chance = 10000 - (100 - success[weapon rank][grind]) * (100 - 5 * grinder.bonus)  (may be <= 0)",
            "C>S 0x0105 event 8 (use) with +0x2C = grinder slot, +0x2F = weapon slot, +0x30 = grinder UUID commits",
            "refused (no reply in the reference) when grinder rank < weapon rank, grind >= 10 or grind >= max_grind",
            "success when rand(10000) < chance: max PP += pp_inc[grind], ATP bonus (var_rec+0x14 s16) += atp_inc[grind]"
            " (both clamped 0..9999), grind += 1; failure changes nothing on the weapon",
            "result: S>C 0x0106 event 0x0D (see report); the grinder is consumed either way",
        ],
        "success": success, "grinders": grinders, "weapons": weapons,
    }


def probo_tables(P, X):
    evo, _, _ = members("SS_PA_EVOLUTION.NBL")
    raw = evo["Evo_param.snr"][0x10:0x10 + 0x17C]
    coef = [list(struct.unpack_from("<19f", raw, 0x4C * s)) for s in range(5)]
    nxt = [list(struct.unpack_from("<2b", X.read(0x558FC0, 32), 2 * t)) for t in range(16)]
    req = [list(struct.unpack_from("<6h", X.read(0x59CD70, 13 * 12), 12 * i)) for i in range(13)]
    dev = [list(struct.unpack("<41b", X.read(0x595A30 + 41 * i, 41))) for i in range(37)]
    d = P.d
    r = P.root("itemEvolutionParam.snr")
    n, p = u32(d, r), u32(d, r + 4)
    devices = {}
    for i in range(n):
        a, b, c, dd, e, f = struct.unpack_from("<hhBBBB", d, p + 8 * i)
        devices[f"0e01{i:02x}00"] = {"add": [a, b], "req": [c, dd], "add_stats": [e & 15, e >> 4],
                                     "req_stats": [f & 15, f >> 4]}
    kinds = {"0": "category 0", **{str(c - 1): f"item category {c}" for c in range(2, 14)},
             "13": "category 14 (PM device; unused by the give path)", "14": "category 15", "15": "category 16",
             "16": "weapon style 0 (melee)", "17": "weapon style 1/2 (ranged)", "18": "weapon style 3 (technique)"}
    return {
        "notes": [
            "stats: room block +0x2E78..+0x2E80 (5 s16); level = max(0, stat // 100) (0x37f080); "
            "rank +0x2E10 = sum of the levels of stats 0..3; type +0x2E34",
            "gift of a non-device item (0x37f6e0): kind from category (weapons by style byte, record +0x16); "
            "factor = stars+1 for non-weapons, stars for weapons (stars = (record+0x13 & 0x3f) + 1); "
            "for stat i in 0..3: f = f32(factor * coef[i][kind]) with round-toward-zero f32 arithmetic; "
            "f > 0: d = trunc(f + 0.9); if level(s+d) + other levels <= 100: s += d else s = level(s)*100 + 99; "
            "f < 0: d = trunc(f - 0.9); if level(s+d) < level(s): s = level(s)*100; elif s <= 0: s = 0; "
            "elif s + d < 0: s = 0; else s += d. stat 4 never changes. deltas are the s8 changes",
            "then evolve (0x37f450): types 8..12 and 16..40 are final; for n in next[type] (n >= 0): if total "
            "level >= req[n][0] and level(stat k) >= req[n][k+1] for all 5: type = n (first match wins)",
            "PM device (category 14): new = device[index][type]; -1 = no change (evolved flag 0); index 0x25 resets",
            "gift limit (0x23aed0): 100 gifts; the count resets when >= 43200 s (12 h) passed since the last gift",
        ],
        "coef": coef, "kinds": kinds, "next": nxt, "req": req, "device": dev, "devices": devices,
        "gift": {"limit": 100, "window_s": 43200},
    }


def synthesis_tables(P, items):
    bp, _, _ = members("XF_BOARDPARAM.NBL")
    d = P.d
    boards = {}
    for c, cat in enumerate(BOARD_CATS):
        par, out = bp[cat + "Param.snr"], bp[cat + "Output.snr"]
        mats = [bp[f"{cat}Material{k}.snr"] for k in (1, 2, 3, 4)]
        n, typ = struct.unpack_from("<HH", par, 0x10)
        bn, bptr = P.typed("itemBoardParam.snr", c + 1)
        for i in range(n):
            bid = par[0x14 + 5 * i:0x18 + 5 * i]
            if bid[0] != 0x0D:
                continue
            o = 0x14 + i * 0xA4
            outputs = []
            for k in range(8):
                iid, a, b, e, fg = struct.unpack_from("<4sIIII", out, o + 4 + 20 * k)
                outputs.append({"id": iid.hex(), "a": a, "rate": b, "element": e, "fail_group": fg})
            materials, slot_types = [], []
            for k in range(4):
                mo = 0x14 + i * 0x44
                slot_types.append(u32(mats[k], mo))
                materials.append([{"id": mm.hex(), "qty": q, "flag": fl}
                                  for mm, q, fl in (struct.unpack_from("<4sHH", mats[k], mo + 4 + 8 * v)
                                                    for v in range(8))])
            idx = bid[2]
            level, extra = (d[bptr + 2 * idx], d[bptr + 2 * idx + 1]) if idx < bn else (0, 0)
            variant_board = bid[1] in (1, 2, 3, 4, 7) or outputs[0]["a"] != 0 or outputs[1]["id"] != "00000000"
            boards[bid.hex()] = {"name": items.get(bid.hex(), {}).get("name"), "cat": c, "cat_name": cat,
                                 "index": i, "level": level, "board_b1": extra, "freq_max": par[0x18 + 5 * i], "param": par[0x18 + 5 * i],
                                 "variant_board": variant_board, "slot_types": slot_types,
                                 "wildcard": any(m["flag"] == 1 for sl in materials for m in sl),
                                 "outputs": outputs, "materials": materials}
    ft = bp["FailureTbl.snr"][0x10:]
    failure = {}
    for g in range(1, u32(ft, 0) + 1):
        ents = []
        for k in range(10):
            e = ft[g * 0x3C + 4 + 6 * k:g * 0x3C + 10 + 6 * k]
            if e[4]:
                ents.append({"id": e[:4].hex(), "weight": e[4], "n": e[5]})
        failure[str(g)] = ents
    et = bp["ElementTbl.snr"][0x10:]
    element = {}
    for o in range(0xB, len(et) - 10, 11):
        e = et[o]
        pairs = [[et[o + 1 + 2 * k], et[o + 2 + 2 * k]] for k in range(5)]
        if any(w for _, w in pairs):
            element[str(e)] = pairs
    return {
        "notes": [
            "boards: board id 0d TT II 00, cat = TT-1 (WpnKnock..Goods), index = row in <cat>Param.snr with id byte "
            "2 == II; level/board_b1 = itemBoardParam.snr bytes (board item record +0x14/+0x15, init 0xb47c0)",
            "4 material slots (Material1..4.snr); slot k of variant v = materials[k][v] (qty per product); "
            "slot_types[k] = u32 at the record start (1: the entry's own slot item is the material, i.e. the "
            "player's choice; else the recipe item); a SetSlotItem of an item matching materials[slot][v] (or "
            "an entry with flag 1 = wildcard) selects variant v (0x3fd190)",
            "freq_max = uses per registered board (Param byte, GetFreqMax 0x4151a0); variant_board (0x4158d0) "
            "= board type 1/2/3/4/7 or several outputs: 1 product per start, materials consumed at StartProduct; "
            "otherwise materials x n are consumed at take-out (DecrementMaterialImm 0x3ff9b0)",
            "time: CalcForcastDateByMinute exists but the PS2 build's minutes (0x3fc4d0) and clock (0x3fc6f0) "
            "return 0: production is instant",
            "rate (GetOutputCompRatio 0x3fe970): if outputs[v].rate == 100 -> 100 else "
            "int(rate_v * min(100, 95 + 0.33 * (skill - 10 * level)) / 100) with skill = Probo level of stat "
            "0/1/2/3 for board type 1/2/3/4, else 1 (0x33cb10), clamped 1..200",
            "roll (0x415450): success when rand(100) < rate + 4 or outputs[0].rate == 100: item = outputs[v].id, "
            "element = outputs[v].element with percent rolled from element[e]; failure: item from "
            "failure[fail_group] (weighted, rand(100)+1 against cumulative weights); empty/invalid group -> "
            "the normal output",
            "materials are taken from the My Room stock (DecrementMaterial 0x3ffc00 -> room stock 0x238a90)",
        ],
        "boards": boards, "failure": failure, "element": element,
    }


def lumilass_tables(debug=True):
    pm, d, m = members("GA_PLYMAKECOMMON.NBL")

    def parse(name):
        """root (+8) -> group pointers; group = 6 x {u32 ptr, u32 count}; pointers are load-address based
        (the first list sits right after the 0x10-byte NSR header, which gives the base)."""
        b = pm[name]
        root = u32(b, 8)
        gptrs = []
        for g in range(root, len(b) - 3, 4):
            p = u32(b, g)
            if not gptrs or abs(p - gptrs[0]) < len(b):
                if p == 0:
                    break
                gptrs.append(p)
            else:
                break
        g0 = min(gptrs)
        # the lowest group's first entry points at the first list (file +0x10)
        base = None
        for off in range(0x10, root, 4):
            p, c = u32(b, off), u32(b, off + 4)
            if 0 < c < 0x100 and p - 0x10 + off == g0:
                base = p - 0x10
                break
        groups = [p - base for p in gptrs]
        out = []
        for go in groups:
            slots = []
            for s in range(6):
                ptr, cnt = u32(b, go + 8 * s), u32(b, go + 8 * s + 4)
                slots.append([u32(b, ptr - base + 4 * i) for i in range(cnt)])
            out.append(slots)
        return out

    return {
        "notes": [
            "part word = slot << 24 | variant/gender flags << 16 | id << 8 | colour (appearance struct u32s +0x00..+0x14,"
            " stored in the character raw record at raw[0x70 + off]); slot 5 = hair (appearance +0x14)",
            "selectable[race*2+gender] (race 0 human, 1 newman, 2 cast, 3 beast) = character-creation choices; "
            "debug = selectableDeb.snr (debug[0..7] per race/gender with every drawable part, debug[8] a test set)",
            "S>C 0x1A03 body: +0x28 u32 count (<= 120), +0x2C 8 x u32 prices (menu entries 0..7), +0x4C count x u32 "
            "hair words; the client appends the player's current hair if it is missing",
        ],
        "selectable": parse("selectable.snr"),
        "debug": parse("selectableDeb.snr") if debug else [],
    }


# --- AotI -------------------------------------------------------------------------------------------------

def grind_tables_aoti(P):
    d = P.d
    r = P.root("itemCommonParam.snr")
    success = [list(struct.unpack_from("<10b", d, r + 0x12 + rank * 10)) for rank in range(5)]
    n, p = u32(d, P.root("itemRefineParam.snr")), u32(d, P.root("itemRefineParam.snr") + 4)
    grinders = {}
    for i in range(n):
        b0, b1 = d[p + 2 * i], d[p + 2 * i + 1]
        grinders[f"0f01{i:02x}00"] = {"rank": b1 & 7, "bonus": (b1 >> 3) & 0x1F, "stack": b0}
    weapons = itemdb.weapon_grind(P) if hasattr(itemdb, "weapon_grind") else {}
    refine = {}
    if hasattr(itemdb, "refine_settings"):
        refine = {str(t): v for t, v in itemdb.refine_settings().items()}
    return {
        "notes": [
            "AotI. rate quote (0x29c840 -> 0x29c710) and chance are PSU's: chance = 10000 - (100 - "
            "success[weapon rank][grind]) * (100 - 5 * grinder.bonus); success when rand(10000) < chance (0x299950)",
            "weapon rank = item +0x12 & 0xf (0xabe10), grind = item +0x12 >> 4 (0xab890), stars-1 = item +0x13 & 0x3f "
            "(0xabec0), failures = item +0x17 >> 4; grindable (0xac340): grind <= 10, max PP < 10000, PP <= max PP, "
            "ATP bonus < 10000",
            "success (0xafc90): s = refine[weapon type]; f = s.mult[stars-1]; max PP += s.pp + max(1, int(f * "
            "(rand(s.rand) + 1))); ATP bonus (+0x10) += s.atp + max(1, int(f * (rand(s.rand) + 1))) (f32, both "
            "clamped 0..9999); grind += 1",
            "failure (0x299aec): max PP = record PP, PP = min(PP, max PP), ATP bonus = 0, grind = 0, item +0x17 += "
            "0x10 (failure count; shown max grind = max_grind - failures, 0x474c60)",
            "S>C 0x0106 event 0x0D as in PSU (0x299950 builds it; +0x36 = 1 on success)",
            "weapons[id].pp_inc/atp_inc are None in AotI (no per-grind tables); refine[type] replaces them",
        ],
        "success": success, "grinders": grinders, "weapons": weapons, "refine": refine,
    }


def probo_tables_aoti(P, X):
    evo, _, _ = members("SS_PA_EVOLUTION.NBL")
    raw = evo["Evo_param.snr"][0x10:0x10 + 0x17C]
    coef = [list(struct.unpack_from("<19f", raw, 0x4C * s)) for s in range(5)]
    nxt = [list(struct.unpack_from("<2b", X.read(0x5689F0, 32), 2 * t)) for t in range(16)]
    req = [list(struct.unpack_from("<6h", X.read(0x5FE110, 13 * 12), 12 * i)) for i in range(13)]
    dev = [list(struct.unpack("<41b", X.read(0x5F6FB0 + 41 * i, 41))) for i in range(68)]
    need = list(struct.unpack("<68H", X.read(0x5F7AA0, 136)))
    kinds = {"0": "category 0", **{str(c - 1): f"item category {c}" for c in range(2, 14)},
             "13": "category 14 (PM device; unused by the give path)", "14": "category 15", "15": "category 16",
             "16": "weapon style 0 (melee)", "17": "weapon style 1/2 (ranged)", "18": "weapon style 3 (technique)"}
    return {
        "notes": [
            "AotI: stats, levels (0x37de30), growth (0x37e450 -> 0x37d330, round constant 0.9), evolve (0x37e1d0) "
            "and the gift limit (0x23f090) are PSU's; see the PSU notes",
            "PM device (category 14, give handler 0x23ca10): index 0x25 (PM Device ZERO) zeroes stats 0..4 "
            "(+0x2E78..+0x2E80) and the rank (+0x2E10) and sets type 0 unless the type is 0 or 1 (type 1 is kept); "
            "the evolved flag is set only when the type changed; deltas 0. Other indexes (0x23cd20): new = "
            "device[index][type] (index < 68) unless stat 4 (+0x2E80) < device_need[index] (0x23cdc0); -1 = "
            "no change. Gifts never change stat 4",
            "give handler 0x23c7c0 extras (no growth): category 0x0b type 3 index < 0x80 sets bit index of room "
            "+0xc20; category 3 type 4 index 0x1e adds 50 to room +0xc0e (max 500)",
        ],
        "coef": coef, "kinds": kinds, "next": nxt, "req": req, "device": dev, "device_need": need, "devices": {},
        "gift": {"limit": 100, "window_s": 43200},
    }


def synthesis_tables_aoti(P, items):
    bp, _, _ = members("XF_BOARDPARAM.NBL")
    boards = {}
    for c, cat in enumerate(AOTI_BOARD_CATS):
        par, out = bp[cat + "Param.snr"], bp[cat + "Output.snr"]
        mats = [bp[f"{cat}Material{k}.snr"] for k in (1, 2, 3, 4)]
        n, typ = struct.unpack_from("<HH", par, 0x10)
        lv = {idx: (P.d[o], P.d[o + 1]) for idx, o in P.records12("itemBoardParam.snr", typ - 1, 2)}
        for i in range(n):
            bid = par[0x14 + 5 * i:0x18 + 5 * i]
            if bid[0] != 0x0D or bid.hex() in boards:
                continue   # 0x41b870 takes the first row whose id byte 2 matches
            o = 0x14 + i * 0xA4
            outputs = []
            for k in range(8):
                iid, a, b, e, fg = struct.unpack_from("<4sIIII", out, o + 4 + 20 * k)
                outputs.append({"id": iid.hex(), "a": a, "rate": b, "element": e, "fail_group": fg})
            if all(x["id"] == "00000000" for x in outputs):
                continue   # padding row (Consume 22 = 0d050000 again, WpnShoot2 27..31 = 0d0b0100, Shield2 9..19)
            materials, slot_types = [], []
            for k in range(4):
                mo = 0x14 + i * 0x44
                slot_types.append(u32(mats[k], mo))
                materials.append([{"id": mm.hex(), "qty": q, "flag": fl}
                                  for mm, q, fl in (struct.unpack_from("<4sHH", mats[k], mo + 4 + 8 * v)
                                                    for v in range(8))])
            level, extra = lv.get(bid[2], (0, 0))
            variant_board = bid[1] in (1, 2, 3, 4, 7, 10, 11, 12, 13) or outputs[0]["a"] != 0 \
                or outputs[1]["id"] != "00000000"
            boards[bid.hex()] = {"name": items.get(bid.hex(), {}).get("name"), "cat": c, "cat_name": cat,
                                 "board_type": typ, "index": i, "level": level, "board_b1": extra,
                                 "freq_max": par[0x18 + 5 * i], "param": par[0x18 + 5 * i],
                                 "variant_board": variant_board, "slot_types": slot_types,
                                 "wildcard": any(m["flag"] == 1 for sl in materials for m in sl),
                                 "outputs": outputs, "materials": materials}
    ft = bp["FailureTbl.snr"][0x10:]
    failure = {}
    for g in range(1, u32(ft, 0) + 1):
        ents = []
        for k in range(10):
            iid, w, nn = struct.unpack_from("<4sII", ft, 4 + (10 * g + k) * 12)
            if w:
                ents.append({"id": iid.hex(), "weight": w, "n": nn & 0xFF})
        failure[str(g)] = ents
    et = bp["ElementTbl.snr"][0x10:]
    element = {}
    for e in range(1, len(et) // 0xA4):
        row = et[e * 0xA4:(e + 1) * 0xA4]
        if not u32(row, 0) and not row[4]:
            break   # end marker (0x41a380 counts rows from 1 until +0 and +4 are both 0)
        pairs = [list(struct.unpack_from("<II", row, 4 + 8 * k)) for k in range(20)]
        element[str(e)] = [pr for pr in pairs if pr[1]]
    return {
        "notes": [
            "AotI. boards: cat = loader slot (0x41b990) = AOTI_BOARD_CATS index; board_type = Param header u16 "
            "(board id 0d TT II 00, TT = board_type); row = the first Param entry with id byte 2 == II (0x41b870); "
            "rows without outputs are padding and left out (Consume row 22 repeats 0d050000, WpnShoot2 rows 27..31 "
            "are 0d0b0100, Shield2 rows 9..19 are 0d0d0900..0d0d1300; none has a name)",
            "client quirk: board type -> slot (0x41b790) maps 1..6 -> 0..5, 10..13 -> 7..10, 18 -> 11 and 9 -> 8 "
            "(WpnShoot2), so Goods (type 9) boards look up the WpnShoot2 tables (0d09II00 uses the recipe, uses and "
            "outputs of WpnShoot2's 0d0bII00, keeping its own level from the item, 0x4031e0 reads item +0x14); "
            "GoodsParam holds one board (0d090000 -> 0d0b0000's recipe); types 7, 8 and 14..17 have no slot",
            "level/board_b1: itemBoardParam 12-byte typed root {u32 recs, u32 map, s16 n_map, s16 n_recs}, slot = "
            "board_type - 1, 2-byte records (0x475850); init 0xac980 copies them to item +0x14/+0x15",
            "materials/outputs/freq_max/variant_board as in PSU; variant_board (0x41b2b0) board types 1 2 3 4 7 "
            "10 11 12 13 or several outputs",
            "rate (0x4031e0): r = outputs[v].rate; r < 2 or r == 100 -> r; else int(r * 0.01 * min(100, 95 - 0.33 * "
            "(10 * level - skill))) clamped 0..100; skill (0x339d30, clamped 1..200) from the first output id: "
            "armor -> Probo level of stat 3; weapon type 1..13 -> stat 0, 14..23 and 27 -> stat 1, 24..26 -> "
            "stat 2, other weapons 0; other categories 1",
            "roll (0x41ae60): success when outputs[0].rate == 100, rate == 100 or rand(100) < rate (no +4); "
            "failure: failure[fail_group] by rand(1000) + 1 against cumulative weights (0x41a550), group "
            "0 or > count -> none",
            "element[e] (0x41a380): [percent, weight] pairs, rand(1000) + 1 against cumulative weights; e = 0 -> 0 %",
            "production time not checked (CalcForcastDateByMinute string is gone)",
        ],
        "boards": boards, "failure": failure, "element": element,
    }


def lumilass_tables_aoti():
    t = lumilass_tables(debug=False)
    t["notes"] = t["notes"][:2] + [
        "AotI has no selectableDeb.snr: debug = []",
        "AotI S>C 0x1A03 (0x3dc390 -> 0x364040): +0x30 u32 count A, +0x34 u32 count B (A + B <= 400), +0x38 13 x "
        "s32 prices (negative hides the menu entry, 0x363aa4), +0x6C A + B hair words; A words are grouped into "
        "styles where the next word is this + 0x100 (up to 10, 0x363f50), B words are used one by one in menu "
        "mode 5 (0x6b5798); the player's current hair is appended to A when missing (0x362d78)",
        "AotI C>S 0x1A05 (builder 0x3dc800, 0x78 bytes) as PSU plus +0x59 2 bytes, +0x5B appearance +0x1D/+0x1E, "
        "+0x5D flag, +0x60 appearance +0x0C/+0x10/+0x54/+0x58 (from psu-server econ.py)",
    ]
    return t


def main():
    global CVM, ERX
    game = itemdb.game_arg(sys.argv)
    CVM, ERX = itemdb.CVM, itemdb.ERX
    a = sys.argv[1:]
    iso = workdir.iso_arg(a)
    out = a[a.index("--out") + 1] if "--out" in a else itemdb.OUT
    os.makedirs(out, exist_ok=True)
    missing = [n for n in NEEDED if not os.path.exists(os.path.join(CVM, n))]
    if missing:
        subprocess.run([sys.executable, os.path.join(HERE, "cvm.py"), "x", workdir.need_iso(iso), CVM,
                        *[x for n in missing for x in ("--match", n)], *itemdb.cvm_args()],
                       check=True, stdout=subprocess.DEVNULL)
    items = {}
    ip = os.path.join(out, "items.json")
    if os.path.exists(ip):
        items = json.load(open(ip, encoding="utf-8"))
    P, X = itemdb.Params(), Erx()
    if game == "aoti":
        tables = (("grind.json", grind_tables_aoti(P)), ("probo.json", probo_tables_aoti(P, X)),
                  ("synthesis.json", synthesis_tables_aoti(P, items)), ("lumilass.json", lumilass_tables_aoti()))
    else:
        tables = (("grind.json", grind_tables(P)), ("probo.json", probo_tables(P, X)),
                  ("synthesis.json", synthesis_tables(P, items)), ("lumilass.json", lumilass_tables()))
    for name, data in tables:
        json.dump(data, open(os.path.join(out, name), "w", encoding="utf-8"), indent=1)
        print("wrote", os.path.join(out, name))


if __name__ == "__main__":
    main()
