"""Grinding, Lumilass and Probo growth (PS2 client; offsets are body offsets). Static reading of PSU_US.erx.

Data ($PSU_GAMEDATA, built by extract/econdata.py): grind.json, lumilass.json, probo.json.

Grinding (the weapon shop's grind menu, TWeaponCustomShop):
  C>S 0x0A09 (+0x24 u8 1, +0x25 u8 grinder slot, +0x26 u16 weapon slot): rate quote -> the same packet back
      with +0x26 = chance in 1/100 % (0xffff = invalid pair)
  chance = 10000 - (100 - success[weapon rank][grind]) * (100 - 5 * grinder bonus); refused when grinder rank
      < weapon rank, grind >= 10 or grind >= max_grind
  C>S 0x0105 event 8 with a grinder (+0x2C grinder slot, +0x2F weapon slot, +0x30 grinder UUID) commits:
      success when rand(10000) < chance: max PP += pp_inc[grind], ATP bonus (var_rec +0x14) += atp_inc[grind],
      grind += 1. S>C 0x0106 event 0x0D: +0x2C grinder slot, +0x2E 1/0, +0x2F weapon slot, +0x30 grinder
      UUID, +0x34 the weapon's 0x20-byte record; then 0x0106 0x0C removes the grinder.
  AotI (grind.json from econdata.py --game aoti; pp_inc/atp_inc are None, weapons[id].refine instead): same quote
      (0x29c710) and chance; commit 0x299950: success when rand(10000) <= chance (0x299ac0: fails only when
      chance < r). Success (0xafc90): s = refine row of the weapon type (record id byte 1), f = f32 s.mult[stars-1]
      (record +0x17 & 0x3f); max PP += s.pp + d1, ATP bonus += s.atp + d2 with d = trunc(f * (rand(s.rand) + 1)),
      0 -> 1 (PP roll first), both clamped 0..9999; grind += 1 (no refine row: grind += 1 only). Failure
      (0x299aec): max PP and PP back to the item's base PP, ATP bonus 0, grind 0, failure count (+0x1B high
      nibble) += 1; the shown max grind is max_grind - failures (0x474c60), so the quote refuses from there.
      The reply carries the weapon record on failure too (the client copies the record it is sent, 0x299b58).
Lumilass (TBeautyParlor): C>S 0x1A01 event 2 -> S>C 0x1A03: +0x28 u32 count (<= 120), +0x2C 8 x u32 prices,
  +0x4C count x u32 hair words (slot 5 << 24 | id << 8 | colour). C>S 0x1A05 commit (no reply): +0x28 u32 list
  index, +0x2C menu entry, +0x30 9 x u32 = appearance +0x24..+0x44, +0x54 5 bytes = appearance +0x18, +0x19,
  +0x1A, +0x1C, +0x20; appearance offset X lives at raw[0x70 + X]; hair (slot 5) at +0x14.
  AotI's 0x1A03 differs (see c1a03); its 0x1A05 (builder 0x3dc800, 0x78 bytes) starts like PSU's and adds +0x59 2
  bytes, +0x5B appearance +0x1D, +0x1E, +0x5D a flag, +0x60 appearance +0x0C, +0x10, +0x54, +0x58 (not applied yet).
Probo (0x37e470 / 0x37f450, verified by emulation): gifts grow stats 0..3 by coef[stat][kind] x factor
  (f32, round toward zero), levels = stat // 100 (total <= 100); evolution by next[type] / req[n].
  AotI (probo.json --game aoti): coef/next/req are PSU's values and the growth code the same (0x37e450 -> 0x37d330,
  evolve 0x37e1d0). A PM device (category 0x0E, 0x23cd20) is not a growth gift: index = id byte 2, new type =
  device[index][type] (0x23ced0, index < 68, type 0..40) unless stat 4 < device_need[index] (0x23cdc0); a new
  type 0..40 is taken with no stat change, -1 changes nothing. PSU sessions keep PSU's path (no devices).
"""
import json
import os
import random
import struct

from psu import games, items, progression
from psu.login import packet

ACC = bytes.fromhex("00011300")
def data(name):
    return games.json_data(name + ".json")


def hdr(gid, lid):
    return struct.pack("<H", lid) + bytes(18) + ACC + struct.pack("<I", gid) + bytes(8)


# --- grinding -------------------------------------------------------------------------------------------

def grind_chance(grinder, weapon):
    """Chance in 1/100 % for grinder slot + weapon slot, or None when refused."""
    g = data("grind")
    gr, wp = g.get("grinders", {}).get(grinder["id"]), g.get("weapons", {}).get(weapon["id"])
    if gr is None or wp is None:
        return None
    level = weapon.get("grind", 0)
    top = wp["max_grind"] - (weapon.get("fails", 0) if games.current() == games.AOTI else 0)
    if gr["rank"] < wp["rank"] or level >= 10 or level >= top:
        return None
    return 10000 - (100 - g["success"][wp["rank"]][level]) * (100 - 5 * gr["bonus"])


def c0a09_quote(gid, lid, body, chance):
    return packet(0x0A09, hdr(gid, lid) + body[0x24:0x26] + struct.pack("<H", 0xFFFF if chance is None
                                                                          else max(0, min(chance, 10000))))


def grind(st, grinder_uuid, weapon_index):
    """Commit a grind. Returns (grinder slot, weapon slot, success, grinder index, error)."""
    inv = st.get("inv", [])
    gi = next((i for i, x in enumerate(inv) if x["uuid"] == grinder_uuid), None)
    if gi is None or not 0 <= weapon_index < len(inv):
        return None, None, False, None, "no such item"
    grinder, weapon = inv[gi], inv[weapon_index]
    chance = grind_chance(grinder, weapon)
    if chance is None:
        return None, None, False, None, "refused"
    if games.current() == games.AOTI:
        return grinder, weapon, refine(weapon, chance), gi, None
    ok = random.randrange(10000) < chance
    if ok:
        wp = data("grind")["weapons"][weapon["id"]]
        level = weapon.get("grind", 0)
        weapon["pp_bonus"] = min(9999, weapon.get("pp_bonus", 0) + wp["pp_inc"][level])
        weapon["atp_bonus"] = min(9999, weapon.get("atp_bonus", 0) + wp["atp_inc"][level])
        weapon["grind"] = level + 1
    return grinder, weapon, ok, gi, None


def refine(weapon, chance, rnd=random.randrange):
    """AotI commit (0x299950): grow the weapon on success, reset it on failure. Returns success."""
    wp = data("grind")["weapons"][weapon["id"]]
    if rnd(10000) > chance:
        weapon["pp_bonus"] = weapon["atp_bonus"] = weapon["grind"] = 0
        weapon["fails"] = min(15, weapon.get("fails", 0) + 1)
        return False
    s = wp.get("refine") or data("grind").get("refine", {}).get(str(bytes.fromhex(weapon["id"])[1]))
    if s:
        stars = items.db().get(weapon["id"], {}).get("rarity0", 0) & 0x3F
        f = _f32(s["mult"][min(stars, len(s["mult"]) - 1)])
        d = [max(1, int(_rz(f * _f32(float(rnd(s["rand"]) + 1 if s["rand"] > 0 else 1))))) for _ in range(2)]
        base, = struct.unpack_from("<H", bytes.fromhex(items.db()[weapon["id"]]["consts"]), 0)
        pp = max(0, min(9999, base + weapon.get("pp_bonus", 0) + s["pp"] + d[0]))
        weapon["pp_bonus"] = pp - base
        weapon["atp_bonus"] = max(0, min(9999, weapon.get("atp_bonus", 0) + s["atp"] + d[1]))
    weapon["grind"] = weapon.get("grind", 0) + 1
    return True


def c0106_grind(gid, lid, grinder_index, weapon_index, grinder_uuid, weapon, ok):
    aoti = games.current() == games.AOTI          # AotI: the reset record on failure too
    rec = items.var_rec(weapon)[4:] if ok or aoti else struct.pack("<I", weapon["uuid"]) + bytes(0x1C)
    return packet(0x0106, hdr(gid, lid) + struct.pack("<II", gid, lid)
                  + bytes([grinder_index & 0xFF, 0x0D, 1 if ok else 0, weapon_index & 0xFF])
                  + struct.pack("<I", grinder_uuid) + rec)


# --- Lumilass ---------------------------------------------------------------------------------------------

LUMILASS_PRICES = [0] * 8               # menu entry prices (unknown online; free for now)


def hair_list(raw):
    groups = data("lumilass").get("selectable", [])
    g = raw[0x68] * 2 + raw[0x69]
    if g >= len(groups):
        return []
    return [w for lst in groups[g] for w in lst if (w >> 24) == 5][:120]


AOTI_LUMILASS_PRICES = [0] * 13


def c1a03(gid, lid, raw):
    """PSU (0x3daa80 -> 0x364b10): +0x30 count, +0x34 8 prices, +0x54 words. AotI (0x3dc390 -> 0x364040): +0x30
    count A, +0x34 count B, +0x38 13 prices, +0x6C A + B words (at most 400); sent the PSU way AotI took the
    first price as a second count and the hair words from 0x18 bytes too early."""
    words = hair_list(raw)
    tail = b"".join(struct.pack("<I", w) for w in words)
    if games.current() == games.AOTI:
        return packet(0x1A03, hdr(gid, lid) + struct.pack("<III", 0, len(words), 0)
                      + struct.pack("<13I", *AOTI_LUMILASS_PRICES) + tail)
    return packet(0x1A03, hdr(gid, lid) + struct.pack("<II", 0, len(words)) + struct.pack("<8I", *LUMILASS_PRICES)
                  + tail)


def apply_lumilass(raw, body):
    """C>S 0x1A05 -> the new raw character record (bytes), or None."""
    words = hair_list(raw)
    index, = struct.unpack_from("<I", body, 0x28)
    r = bytearray(raw)
    if index < len(words):
        struct.pack_into("<I", r, 0x70 + 0x14, words[index])
    r[0x70 + 0x24:0x70 + 0x48] = body[0x30:0x54]
    for off, b in zip((0x18, 0x19, 0x1A, 0x1C, 0x20), body[0x54:0x59]):
        r[0x70 + off] = b
    return bytes(r)


# --- Probo growth -------------------------------------------------------------------------------------------

def _f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _rz(x):
    """float -> float32 rounded toward zero (the R5900 FPU truncates)."""
    y = _f32(x)
    if abs(y) > abs(x):
        i, = struct.unpack("<I", struct.pack("<f", y))
        y = struct.unpack("<f", struct.pack("<I", i - 1))[0]
    return y


def _lv(x):
    return max(0, int(x / 100)) if x >= 0 else 0


def _s8(x):
    return ((x + 128) & 0xFF) - 128


def gift_kind(item_id, style):
    cat = bytes.fromhex(item_id)[0]
    if cat == 1:
        return {0: 16, 1: 17, 2: 17, 3: 18}.get(style, 16)
    if 2 <= cat <= 13:
        return cat - 1
    return {15: 14, 16: 15}.get(cat)


def probo_gift(st, slot):
    """Grow the Probo from a gifted item slot. Returns (evolved, type, stats, deltas)."""
    p = data("probo")
    pr = st.setdefault("probo", {"type": 0, "stats": [0, 0, 0, 0, 0]})
    if not p:
        return False, pr["type"], pr["stats"], [0] * 5
    if games.current() == games.AOTI and bytes.fromhex(slot["id"])[0] == 0x0E:
        return probo_device(p, pr, bytes.fromhex(slot["id"])[2])
    it = items.db().get(slot["id"], {})
    rec = items.var_rec(slot)
    kind = gift_kind(slot["id"], rec[0x1A])
    stats, deltas = list(pr["stats"]), [0] * 5
    if kind is not None:
        factor = (it.get("rarity0", 0) + 1) if bytes.fromhex(slot["id"])[0] == 1 else (it.get("rarity0", 0) + 2)
        for i in range(4):
            c = p["coef"][i][kind]
            if not c:
                continue
            f = _rz(_f32(float(factor)) * _f32(c))
            if f > 0:
                d = int(_rz(f + _f32(0.9)))
                new = stats[i] + d
                if _lv(new) + sum(_lv(stats[j]) for j in range(4) if j != i) <= 100:
                    stats[i], deltas[i] = new, _s8(d)
                else:
                    L = _lv(stats[i])
                    deltas[i], stats[i] = _s8(L * 100 + 99 - stats[i]), L * 100 + 99
            elif f < 0:
                d = int(_rz(f - _f32(0.9)))
                if _lv(stats[i] + d) < _lv(stats[i]):
                    L = _lv(stats[i])
                    deltas[i], stats[i] = _s8(L * 100 - stats[i]), L * 100
                elif stats[i] <= 0:
                    stats[i] = 0
                elif stats[i] + d < 0:
                    deltas[i], stats[i] = _s8(-stats[i]), 0
                else:
                    stats[i], deltas[i] = stats[i] + d, _s8(d)
    evolved, t = False, pr["type"]
    if not (8 <= t <= 12 or 16 <= t <= 40) and t < len(p["next"]):
        L = [_lv(x) for x in stats]
        for n in p["next"][t]:
            if n < 0 or n >= len(p["req"]):
                continue
            r = p["req"][n]
            if sum(L[:4]) >= r[0] and all(L[k] >= r[k + 1] for k in range(5)):
                t, evolved = n, True
                break
    pr["type"], pr["stats"] = t, stats
    return evolved, t, stats, deltas


def probo_device(p, pr, idx):
    """AotI PM device (0x23cd20 -> 0x23cdc0): (evolved, type, stats, deltas). Index 0x25 (PM Device ZERO, give
    handler 0x23ca10) zeroes stats 0..4 and sets type 0 unless the type is 0 or 1; evolved only on a change."""
    dev, need, t = p.get("device", []), p.get("device_need", []), pr["type"]
    if idx == 0x25:
        pr["stats"] = [0] * 5
        if t not in (0, 1):
            pr["type"] = 0
            return True, 0, pr["stats"], [0] * 5
        return False, t, pr["stats"], [0] * 5
    new = dev[idx][t] if idx < min(len(dev), 0x44) and 0 <= t < min(len(dev[idx]), 41) else -1
    if idx < len(need) and (pr["stats"][4] & 0xFFFF) < need[idx]:
        new = -1
    if 0 <= new < 41:
        pr["type"] = new
        return True, new, pr["stats"], [0] * 5
    return False, t, pr["stats"], [0] * 5


def c1312(gid, lid, evolved, ptype, stats, deltas):
    body = struct.pack("<I", gid) + struct.pack("<hh", 1 if evolved else 0, ptype)
    body += b"".join(struct.pack("<hh", s, d) for s, d in zip(stats[:5], deltas[:5]))
    return packet(0x1312, hdr(gid, lid) + body)
