"""Item drops and pickup (PS2 client; offsets are body offsets). From static reading of PSU_US.erx.

Data: $PSU_GAMEDATA/drops.json (extract/dropdata.py from GA_ITEMPARAM itemEnemyDrop/itemObjectDrop and
SU_ITEMAREADROP areaDropNN). Every roll is rand(50000) against cumulative thresholds (0x2081a0).
  enemy kill (0xb6b90): slot by the variant's t[3]: 0 nothing, 1 meseta (min + rand(max - min)),
      2 the area enemy table (row by level band), 3 rare (second roll > rare_t) else the common list
  box (0xb6870): record by drop type/index, chance, level band -> 3 ids + area object table
  level band: first i < 9 with level <= bands[i], else 9; the zone record gives area (6..21) and level
S>C 0x0A02 ground item (frame 0xA0, handler 0x9f600): +0x24 0x74-byte record: s16 map id, UTF-16 name
      (0x2E), u32 price, 20-byte constants, 0x20-byte item (var_rec from +4: UUID, id, qty, ...), f32 xyz.
      Meseta: id 12000000, amount in the quantity word. Sent to the whole zone; first picker wins.
S>C 0x0A03 whole ground list (joining a zone): +0x24 u32 count (<= 50), 0x74-byte records.
C>S 0x0106 event 6 pickup (+0x34 the 0x20-byte ground item) -> S>C 0x0106 event 6 to the zone (the
      picker's client adds the item with the sent UUID, everyone removes it from the ground; +0x2E = 3
      failure, +0x2F 1 inventory full / 2 stack full); meseta -> 0x0115 (level block) instead of the add.
Ground UUIDs come from their own range so they never collide with inventory UUIDs.

AotI (drops.json from dropdata.py --game aoti: enemy rows carry "rare_bands"; AotI ERX addresses):
  enemy kill 0xaf780: slots as PSU; meseta m = min + rand(max - min), amount m + m * level // 5 (0xaf8a0..0xaf8c8,
      no party factor); rare: h = band // 2, r2 = rand(50000) > rare_bands[h].t -> rare_bands[h].id, else the common
      list, where a threshold <= 0 never wins (0xaf974 blezl); a common index past the table drops nothing (0xaf810)
  band 0xaf9f0: bands 9 24 35 49 60 74 85 99 124 200, else 10; the client then reads past the row (level > 124:
      rare_t from the meseta min, kae t[10] from the next record), so band 10 is clamped to 9 here as in PSU
  area file 0xafb30: drops.json "zones" already hold the remapped entry (0x53f1b0 table by map thousands digit);
      "maps" {quest: {zone: {map: NN}}} overrides it for maps whose entry differs (zone_area map=)
"""
import json
import os
import random
import struct

from psu import games, items, progression
from psu.login import packet

R = 50000
MESETA = "12000000"
UUID_BASE = 0x40000000
ACC = bytes.fromhex("00011300")
_next_uuid = UUID_BASE
ground = {}                      # world zone key -> {uuid: ground item dict}


def tables():
    return games.json_data("drops.json") or None


def _band(t, level):
    return next((i for i in range(9) if level <= t["bands"][i]), 9)


def _pick(r, thresholds):
    return next((k for k, v in enumerate(thresholds) if r <= v), len(thresholds))


def roll_enemy(variant, level, area, rnd=random.randrange):
    """-> None, ("meseta", amount) or ("item", id)."""
    t = tables()
    e = t and t["enemy"].get(str(variant))
    if not e:
        return None
    aoti = "rare_bands" in e
    slot = _pick(rnd(R), e["t"])
    if slot == 0:
        return None
    if slot == 1:
        lo, hi = e["meseta"]
        m = lo + (rnd(hi - lo) if hi > lo else 0)
        return ("meseta", m + m * level // 5 if aoti else m)
    if slot == 2:
        a = t["area"].get(str(area))
        if not a:
            return None
        rows, b = a["enemy"], _band(t, level)
        r = rnd(R)
        out = rows[next((j for j in range(len(rows) - 1) if r <= rows[j]["t"][b]), len(rows) - 1)]["id"]
    elif aoti:
        rt, rare = e["rare_bands"][min(_band(t, level) // 2, len(e["rare_bands"]) - 1)]
        if rnd(R) > rt:
            out = rare
        elif not 0 <= e["common"] < len(t["common"]):
            return None
        else:
            c = t["common"][e["common"]]
            r = rnd(R)
            out = c["ids"][next((k for k, v in enumerate(c["t"]) if v > 0 and r <= v), len(c["t"]))]
    elif rnd(R) > e["rare_t"]:
        out = e["rare"]
    else:
        c = t["common"][e["common"]]
        out = c["ids"][_pick(rnd(R), c["t"])]
    return ("item", out) if out != "00000000" else None


def roll_object(dtype, index, level, area, rnd=random.randrange):
    t = tables()
    if not t or not 0 <= dtype < 5 or not 0 <= index < len(t["object"][dtype]):
        return None
    rec = t["object"][dtype][index]
    if rec["type"] != dtype or rnd(R) > rec["chance"]:
        return None
    bd = rec["bands"][_band(t, level)]
    k = _pick(rnd(R), bd["t"])
    if k < 3:
        out = bd["ids"][k]
    else:
        a = t["area"].get(str(area))
        if not a:
            return None
        y = a["object"][dtype]
        out = y["ids"][_pick(rnd(R), y["t"])]
    return ("item", out) if out != "00000000" else None


def zone_area(quest, zone, map_id=None):
    """(area file 6..21, box level) of a mission zone, or (None, 1). AotI: a map whose drop file differs from
    its zone's (drops.json "maps", from the 0x53f1b0 remap) gives its own file."""
    t = tables()
    za = (t or {}).get("zones", {}).get(str(quest), {}).get(str(zone))
    if not za:
        return None, 1
    nn = (t.get("maps") or {}).get(str(quest), {}).get(str(zone), {}).get(str(map_id)) if map_id is not None else None
    return (nn if nn is not None else za[0]), za[1]


def make(zone_key, result, map_id, pos):
    """A ground item from a roll result at pos (12 bytes xyz), stored in the zone's ground list."""
    global _next_uuid
    kind, value = result
    if kind == "meseta":
        key = MESETA
    else:
        db = items.db()
        key = value if value in db else (value[:6] + "00" if value[:6] + "00" in db else None)
        if key is None:
            return None
    _next_uuid += 1
    g = {"uuid": _next_uuid, "id": key, "qty": value if kind == "meseta" else 1, "map": map_id, "pos": pos,
         "meseta": kind == "meseta"}
    lst = ground.setdefault(zone_key, {})
    if len(lst) >= 50:                                          # the client recycles the oldest too
        lst.pop(next(iter(lst)))
    lst[g["uuid"]] = g
    return g


def item20(g):
    """The 0x20-byte item as the client carries it (var_rec from +4)."""
    if g["meseta"]:
        return struct.pack("<I", g["uuid"]) + bytes.fromhex(MESETA) + struct.pack("<I", g["qty"]) + bytes(20)
    slot = {"uuid": g["uuid"], "id": g["id"], "qty": g["qty"], "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]}
    return items.var_rec(slot)[4:]


def record(g):
    if g["meseta"]:
        consts = bytes(0x2E + 4 + 20)
    else:
        slot = {"uuid": g["uuid"], "id": g["id"], "qty": 1, "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]}
        c = items.const_rec(slot)
        consts = c[:0x2E] + c[0x30:0x48]                 # name, price, 20-byte block (no rarity/category)
    return struct.pack("<h", g["map"]) + consts + item20(g) + g["pos"]


def hdr(d):
    return struct.pack("<H", d.lid) + bytes(18) + ACC + struct.pack("<I", d.gid) + bytes(8)


def c0a02(d, g):
    return packet(0x0A02, hdr(d) + record(g))


def c0a03(d, zone_key):
    from psu import xgame
    recs = [record(g) for g in list(ground.get(zone_key, {}).values())[-50:]
            if xgame.ground_ok(g, getattr(d, "game", None))]      # AotI-only items are not shown to PSU
    return packet(0x0A03, hdr(d) + struct.pack("<I", len(recs)) + b"".join(recs))


def c0106_pickup(d, picker, g, fail=0):
    """S>C 0x0106 event 6 about `picker` to player d: add (picker's own client) + remove from the ground.
    fail: 0 ok, 1 inventory full, 2 stack full (sent to the picker only)."""
    if g["meseta"] or fail:
        tail = bytes(0x48)
    else:
        slot = {"uuid": g["uuid"], "id": g["id"], "qty": g["qty"], "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]}
        tail = items.const_rec(slot)
    return packet(0x0106, hdr(d) + struct.pack("<II", picker.gid, picker.lid)
                  + bytes([0, 6, 3 if fail else 0, fail]) + b"\xff" * 4 + item20(g) + tail)


def clear(zone_key):
    ground.pop(zone_key, None)
