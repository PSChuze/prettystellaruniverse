"""Inventory and equipment (PS2 layouts; PSU_US.erx handler addresses in the comments).

Item table: $PSU_GAMEDATA/items.json (extract/gamedata.py from the disc; never committed):
id (4 bytes cat, type, index, manufacturer as hex) -> name, cat, 20-byte constants block, price, ...
Online the client shows names/prices/stats from the constants the server sends, not from the disc.

Persisted per character (characters.state["inv"]): up to 60 slots {uuid, id, qty, active, slots
(weapon palette bits), grind, pa}; state["pas"]: learned photon arts [[PA EXP, id], ...] (max 36; the
level is id byte 3, 0-based; psu/growth.py).

  0x0A0A  whole inventory at character load (0x980a0): +0x2C count, PA count, 6, 0, 2 f32, 6 ids,
          36 x 8 PAs, +0x170 60 x 0x24 variable records, +0x9E0 60 x 0x48 constants, ... size 0x23D8
  0x0109  C>S palette (set_palette); restored through 0x0A0A (record byte +1 and body+0x30 item palette)
  0x0A06  inventory UUIDs in slot order in each zone load (0x97fb0), 60 x u32, 0xffffffff pad
  0x0105  item event, C>S request / S>C confirmation (0x14fd90): +0x2C GID, +0x30 LID, +0x34 index,
          +0x35 event (1 equip, 2 unequip, 3 link, 4 unlink, 5 drop, 7 learn a disk (psu/growth.py), 8 use,
          9 trap), +0x36 category, +0x37 A, +0x38 UUID.
          The client only equips/uses when the confirmation arrives.
Variable record (0x24): active, palette bits, 0, uuid, id, qty, then per category (weapon: PP, max PP,
grind<<4|rank, rarity-1, element, %, style, hand, weapon type, PA type, PA level; stackable (stackable()):
qty, s16 max qty at +0x10, see stack_max).
A technique weapon (style 3, rods 0x18 / wands 0x19) has no weapon type there: +0x1C..+0x23 are 4 linked
technique slots (code, level byte), code = (PA id byte 1 - 1) * 10 + byte 2 + 1 (1..0x38, 0 = empty); the
client relinks every nonzero code at load (0xbe090), so a type byte at +0x1C read as a technique in slot 0.
Constants record (0x48): name UTF-16 (0x2E), rarity-1, category, u32 price, 20-byte block.
AotI (0x0A0A 0x99bb0, 0x0A06 0x99ab0, 0x0105 0x14d300, 0x0106 0x14d000): same offsets and record sizes, except
0x0A0A: +0x2298 (0x60) is not read and the nibble table is 8 x (25 bytes at +0x22F8 + 2 bytes at +0x23C8),
so the trailing 16 bytes count (0x44 like the rest). 0x0105/0x0106 events go up to 0x12.
"""
import json
import os
import struct

from psu.login import packet
from psu import games, xgame

ACC = bytes.fromhex("00011300")
MAX_ITEMS, MAX_PAS = 60, 36
def db():
    return games.json_data("items.json", required=True)


def starter(raw, next_uuid):
    """The offline new-game pack (DE_ITEMPACK playerItemPack.snr): weapon pair + shield by race
    (GRM human/cast, Yohmei newman, Tenora beast), Monomate x5, Goggles, Photon Eraser/Reflector,
    and the PA Rising Strike."""
    m = {0: 0, 1: 1, 2: 0, 3: 2}.get(raw[0x68], 0)
    ids = [f"010900{m:02x}", f"011400{m:02x}", f"020100{m:02x}", "03010000", "11010000", "11020000", "11020100"]
    inv = []
    for iid in ids:
        if iid in db():
            inv.append({"uuid": next_uuid, "id": iid, "qty": 5 if iid == "03010000" else 1,
                        "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]})
            next_uuid += 1
    return inv, [[0, "04090600"]], next_uuid          # [PA EXP, id with the 0-based level in byte 3]


def var_rec(slot, game=None):
    """The 0x24-byte variable record as a `game` client (default: this session's) may read it (psu/xgame.py)."""
    game = game or games.current()
    it = db()[slot["id"]]
    iid = bytes.fromhex(it["id"])
    c = bytes.fromhex(it["consts"])
    r = bytearray(0x24)
    r[0], r[1] = slot["active"], slot["slots"]
    struct.pack_into("<I", r, 4, slot["uuid"])
    r[8:12] = iid
    if it["cat"] == 1:                                      # PP, max PP (grinding raises it and the ATP bonus +0x14)
        struct.pack_into("<HHh", r, 0x10, cur_pp(slot), max_pp(slot), slot.get("atp_bonus", 0))
        r[0x16] = (slot["grind"] << 4) | (c[0xF] & 0xF)
        r[0x17] = it.get("rarity0", 0)
        r[0x18], r[0x19], r[0x1A], r[0x1B] = c[0xB], c[0xC], c[0xE], c[0xD]
        if slot.get("fails") and game == games.AOTI:              # AotI grind failures: +0x1B high nibble
            r[0x1B] = (c[0xD] + (min(slot["fails"], 15) << 4)) & 0xFF   # (0x299b4c adds 0x10 per failure)
        if c[0xE] == 3:                                     # technique weapon: 4 x (tech code, level) at +0x1C
            for i, (code, lv) in enumerate(slot.get("techs", [])[:4]):   # (0xbde00 link, 0x296350 hit)
                r[0x1C + 2 * i], r[0x1D + 2 * i] = code, xgame.pa_level(lv, game)
        else:
            r[0x1C] = iid[1]
            r[0x1D], r[0x1E] = slot["pa"][0], xgame.pa_level(slot["pa"][1], game)
    elif stackable(slot["id"]):                         # +0x10 s16: the stack's max (see stack_max)
        struct.pack_into("<II", r, 0xC, slot["qty"], stack_max(slot["id"]))
    else:
        struct.pack_into("<I", r, 0xC, slot["qty"])
    return bytes(r)


def const_rec(slot):
    it = db()[slot["id"]]
    name = (it["name"] or "").encode("utf-16-le")[:0x2C].ljust(0x2E, b"\0")
    return (name + bytes([it.get("rarity0", 0), it["cat"]]) + struct.pack("<I", it["price"] or 0)
            + bytes.fromhex(it["consts"]))


def paparam():
    """$PSU_GAMEDATA/paparam.json (extract/paparams.py): PA base params and the default bullets."""
    return games.json_data("paparam.json")


def pa_params(pas):
    """0x0A0A +0x1AC0 (mgr+0x98): 36 x 0x30 per learned PA in list order: s16 ATP%, s16 ATA%, u32 PA EXP, base
    param. Zeros leave a linked PA's step count 0, so triangle never starts it (0xba890)."""
    return b"".join(pa_param(exp, pid) for exp, pid in pas[:36]).ljust(0x6C0, b"\0")


def pa_param(exp, pid):
    """One learned PA's 0x30-byte entry (0x0A0A +0x1AC0; a learned disk's 0x0105 event 7 reply +0x34)."""
    e = paparam().get("pa", {}).get(pid[:6] + "00")
    if not e:
        return bytes(0x30)
    L = min(bytes.fromhex(pid)[3], len(e["pct"]) - 1)
    base = bytes.fromhex(e["base"][min(L // 10, len(e["base"]) - 1)])
    return (struct.pack("<hhI", *e["pct"][L], exp) + base).ljust(0x30, b"\0")


def bullet_defaults():
    """0x0A0A +0x2180 (mgr+0x9c): 10 x 0x1C default bullet per weapon type 0x0E..0x17. Zeros give bullets speed
    and radius 0, so gun shots never collide and never report 0x0E00."""
    return b"".join(bytes.fromhex(h) if h else bytes(0x1C)
                    for h in (paparam().get("bullet_default") or [None] * 10)).ljust(0x118, b"\0")


def c0a0a(gid, inv, pas, apal=None, game=None):
    """apal: the item palette (6 x 4-byte item type ids, 0 = empty) as the client last sent it in 0x0109.
    game: the client's disc (default: this session's); a PSU client gets PSU's PA levels (psu/xgame.py)."""
    game = game or games.current()
    pas = [[exp, xgame.pa_id(pid, game)] for exp, pid in pas]
    body = bytearray()
    body += bytes([len(inv), len(pas), 6, 0]) + struct.pack("<ff", 0.0, 0.0)
    body += bytes.fromhex(apal) if apal else bytes(24)
    body += b"".join(struct.pack("<I", lv) + bytes.fromhex(pid) for lv, pid in pas).ljust(0x120, b"\0")
    body += b"".join(var_rec(s, game) for s in inv).ljust(0x870, b"\0")
    body += b"".join(const_rec(s) for s in inv).ljust(0x10E0, b"\0")
    body += pa_params(pas) + bullet_defaults() + bytes(0x60) + b"\x44" * 200
    body += struct.pack("<HHHh", 10000, 10000, 10000, 1000) + b"\x44" * 16
    hdr = struct.pack("<H", 0xFFFF) + bytes(18) + ACC + struct.pack("<I", gid) + bytes(8)
    p = packet(0x0A0A, hdr + bytes(body))
    assert len(p) == 0x23D8, hex(len(p))
    return p


def c0a0a_state(gid, st):
    return c0a0a(gid, st.get("inv", []), st.get("pas", []), st.get("apal"))


def set_palette(st, body):
    """C>S 0x0109 (builder 0x15cd90): +0x2C 6 x u32 right-hand weapon UUID per palette slot, +0x44 6 x u32
    left-hand, +0x5C 6 x 4-byte item type ids (item palette); 0xffffffff / 0 = empty. The client rebuilds
    its weapon palette on every zone load from variable record byte +1 (bit s = palette slot s) and never
    writes it back, so the server keeps it and re-sends it in 0x0A0A."""
    right = struct.unpack_from("<6I", body, 0x2C)
    left = struct.unpack_from("<6I", body, 0x44)
    for slot in st.get("inv", []):
        slot["slots"] = sum(1 << s for s in range(6) if slot["uuid"] in (right[s], left[s]))
    st["apal"] = body[0x5C:0x74].hex()
    return right, left


def uuids(inv):
    return b"".join(struct.pack("<I", s["uuid"]) for s in inv).ljust(240, b"\xff")


def weapons():
    return games.json_data("weapons.json")


def equip_block(slot):
    """The 16 bytes an equip confirmation of a weapon carries at +0x3C (0x146bb0 -> 0xb9e90, EGS striking
    weapons): u32 attack sound, 8-byte hitbox (s16 range x10, height, angle 1, angle 2), u8 max targets
    (low nibble; high nibble = sound type), 0, effect, model (the weapon type). Without it the weapon has
    range 0 / 0 targets and the client never reports a player hit."""
    cat = db().get(slot["id"], {}).get("cat") if slot else None
    if cat in (2, 8):
        # line shield / unit (TItemShield 0xbe520, TItemLineUnit 0xbed20): 12 bytes copied to [player+0x390],
        # 6 x (s8 level, u8 rate / 10); byte 3 is the melee reach multiplier the weapon range is scaled by
        # (0 = no reach at all). The client's own default (image 0x5213a0): x1.0 everywhere.
        return bytes.fromhex("000a000a000a000000000000")
    if cat != 1:
        return b""
    wtype = bytes.fromhex(slot["id"])[1]
    w = weapons().get(str(wtype))
    if not w:
        return b""
    return (struct.pack("<I", w["sound"]) + bytes.fromhex(w["hitboxes"][0])
            + bytes([max(1, w["targets"]) & 0x0F, 0, 0, wtype]))


def c0105_reply(gid, lid, req, index=None, extra=b""):
    """Confirm a C>S 0x0105 (EGS layout): target GID/LID, index, event, category, A, UUID; extra = the weapon
    block on an equip (equip_block)."""
    tgt_gid, tgt_lid, idx, ev, cat, a, uuid = struct.unpack_from("<IIBBBBI", req, 0x24)
    return packet(0x0105, struct.pack("<H", lid) + bytes(6) + struct.pack("<I", tgt_gid) + bytes(8) + ACC
                  + struct.pack("<I", gid) + bytes(8)
                  + struct.pack("<IIBBBBI", tgt_gid, tgt_lid, idx if index is None else index, ev, cat, a, uuid)
                  + extra)


def apply_event(st, req):
    """Update the saved inventory for an 0x0105 request; returns (event, item name or None)."""
    _g, _l, idx, ev, _cat, _a, uuid = struct.unpack_from("<IIBBBBI", req, 0x24)
    inv = st.get("inv", [])
    slot = next((s for s in inv if s["uuid"] == uuid), inv[idx] if idx < len(inv) else None)
    if slot is None:
        return ev, None
    name = db()[slot["id"]]["name"]
    if ev == 1:
        slot["active"] = 1
    elif ev == 2:
        slot["active"] = 0
    elif ev == 3:                                   # link PA list entry +0x2E (0x5bbc0) to this weapon
        from psu import growth                      # (technique weapons: into tech slot +0x2F, 0xbe020)
        if growth.tech_weapon(slot):
            growth.link_tech(st, slot, req[0x2E], req[0x2F])
        else:
            slot["pa"] = growth.link_level(st, slot, req[0x2E])     # +0x1E = the learned PA's level byte
    elif ev == 4:                                   # unlink (technique weapons: tech slot +0x2F, 0xbda50)
        from psu import growth
        if growth.tech_weapon(slot):
            if req[0x2F] < 4 and slot.get("techs"):
                slot["techs"][req[0x2F]] = [0, 0]
        else:
            slot["pa"] = [0, 0]
    elif ev == 8 and slot["qty"] > 0:
        slot["qty"] -= 1
        if slot["qty"] == 0:
            inv.remove(slot)
    return ev, name


# --- mid-session inventory changes: S>C 0x0106 (handler 0x14fa50; the offline room's own path) ----------
# +0x24 GID, +0x28 LID, +0x2C 0, +0x2D event, +0x2E 0, +0x2F equip flag, +0x30 ffffffff, +0x34 the 0x20-byte
# item (var_rec from +4: UUID, id, qty, ...); event 0x0B add (+0x54 const_rec; the client merges a stackable
# into the first slot with the same id, clamped to its max, else appends with the sent UUID), 0x0C remove N
# (by UUID; N = qty for stackables, 1 otherwise; the slot goes at >= held). The client appends at the end and
# closes gaps, like a Python list, so the server's list order stays in step. Send only after 0x0236.
def _hdr(gid, lid):
    return struct.pack("<H", lid) + bytes(18) + ACC + struct.pack("<I", gid) + bytes(8)


def c0106_add(gid, lid, slot, equip=0, game=None):
    """game: the receiving client's disc (default: this session's), for an item given across sessions (trade)."""
    return packet(0x0106, _hdr(gid, lid) + struct.pack("<II", gid, lid) + bytes([0, 0x0B, 0, equip]) + b"\xff" * 4
                  + var_rec(slot, game)[4:] + const_rec(slot))


def c0106_remove(gid, lid, slot, n, game=None):
    return packet(0x0106, _hdr(gid, lid) + struct.pack("<II", gid, lid) + bytes([0, 0x0C, 0, 0]) + b"\xff" * 4
                  + var_rec(dict(slot, qty=n), game)[4:])


STACK_CATS = (3, 7, 12, 15)       # consumables, materials, traps, grinders


def stackable(key):
    """The client's rule (0xafb70 -> 0xafbb0, AotI 0xaafb0): categories 3, 7, 12 and 15 stack unless id byte 3
    has 0x40. A picked-up / added / bought stackable merges into the first held one with the same id (0xf5f70)."""
    it = db().get(key)
    return bool(it) and it["cat"] in STACK_CATS and not bytes.fromhex(key)[3] & 0x40


def stack_max(key):
    """Max per stack, what the offline client puts at item +0xC (variable record +0x10, 0xb0690, clamped to 99):
    consumable table byte 1 (0xb49d0), trap table byte 2 (0xb4810), grinder table byte 0 (0xb4730), materials 99
    (0xb4c60). Online the client takes it from the server's record as is, and a merge clamps the stack to it
    (0xb0870 -> 0xb0780 reads it at 0xb0630), so a 0 there empties the stack."""
    if not stackable(key):
        return 1
    it = db()[key]
    c = bytes.fromhex(it["consts"])
    if it["cat"] == 3:
        m = c[1]
    elif it["cat"] == 12:
        m = bytes.fromhex(it["rec"])[2] if it.get("rec") else c[7]
    elif it["cat"] == 15:
        m = c[0]
    else:
        m = 99
    return max(1, min(m, 99))


def add_item(st, key, n=1, template=None, uuid=None):
    """Add n of item `key` to st (mirrors the client's 0x0106 add). Returns (slot to send, error).
    uuid: keep this UUID for a new slot (a picked-up ground item keeps its ground UUID)."""
    inv = st.setdefault("inv", [])
    stack = stackable(key)
    held = next((x for x in inv if x["id"] == key), None) if stack else None
    if held is not None:
        held["qty"] = min(held["qty"] + n, stack_max(key))
        return dict(held, qty=n), None
    if len(inv) >= MAX_ITEMS:
        return None, "inventory full"
    if uuid is None:
        uuid = max([st.get("next_uuid", 1)] + [x["uuid"] + 1 for x in inv if x["uuid"] < 0x40000000])
        st["next_uuid"] = uuid + 1
    slot = dict(template or {"grind": 0, "pa": [0, 0]}, uuid=uuid, id=key, qty=n if stack else 1, active=0, slots=0)
    inv.append(slot)
    return slot, None


def remove_item(st, uuid, n=1):
    """Remove n of the item with `uuid` (not if equipped). Returns (the slot as it was, error)."""
    inv = st.get("inv", [])
    slot = next((x for x in inv if x["uuid"] == uuid), None)
    if slot is None:
        return None, "no such item"
    if slot.get("active"):
        return None, "equipped"
    before = dict(slot)
    stack = stackable(slot["id"])
    n = min(n, slot["qty"]) if stack else 1
    slot["qty"] -= n
    if slot["qty"] <= 0 or not stack:
        inv.remove(slot)
    return dict(before, qty=n), None


# --- weapon PP (Photon Points) ------------------------------------------------------------------------------
# Online the client never spends PP itself: the offline room (TServerActorPlayer, PSU / AotI addresses) does, and
# the server has to. Each change reaches the client as S>C 0x0105 event 0x0A (0x14fd90 -> 0x145db0 -> 0xf6910;
# AotI 0x14d300 -> 0x142940 -> 0xf0600; size 0x40): +0x36 must be 0, +0x37 flags (1 every weapon, 2 amount is a %
# of max PP, 4 both equipped hands, 8 set PP to the amount; else add it), +0x38 weapon UUID, +0x3C s32 amount;
# the client clamps to 0..max PP (0xb0270). The server keeps the current PP in the slot ("pp", absent = full) and
# always sends flag 8 with the value it holds, so the client cannot drift from it. 0x0A0A / 0x0106 records carry it
# at var_rec +0x10 (max PP at +0x12).
PP_SET = 8


def max_pp(slot):
    it = db().get(slot["id"]) or {}
    if it.get("cat") != 1:
        return 0
    return max(0, struct.unpack_from("<H", bytes.fromhex(it["consts"]), 0)[0] + slot.get("pp_bonus", 0))


def cur_pp(slot):
    mx = max_pp(slot)
    return max(0, min(slot.get("pp", mx), mx))


def set_pp(slot, value):
    """Store PP clamped to 0..max (full drops the key). Returns True when it changed."""
    mx = max_pp(slot)
    old, new = cur_pp(slot), max(0, min(int(value), mx))
    if new >= mx:
        slot.pop("pp", None)
    else:
        slot["pp"] = new
    return new != old


def equipped_weapons(st):
    return [x for x in st.get("inv", []) if x.get("active") and (db().get(x["id"]) or {}).get("cat") == 1]


def c0105_pp(gid, lid, slot):
    """S>C 0x0105 event 0x0A: set the weapon's PP to what the server holds (flag 8)."""
    return packet(0x0105, struct.pack("<H", lid) + bytes(6) + struct.pack("<I", gid) + bytes(8) + ACC
                  + struct.pack("<I", gid) + bytes(8)
                  + struct.pack("<IIBBBBIi", gid, lid, 0, 0x0A, 0, PP_SET, slot["uuid"], cur_pp(slot)))


def pp_item(st, used, a):
    """A PP item used with C>S 0x0105 event 8 (the room's 0x296a30, AotI 0x299410), from its constants
    (itemConsumeParam, 0xb2500): +0 kind 1, +2 target (4: the weapon at inventory index A, A = 0x3C both equipped
    hands, halved when two; 5: every weapon), +4 s16 amount, +7 bit 0x10 amount is a % of max PP (Photon Charge:
    100 %). `used` is the item's slot as it was before the use. Returns the weapon slots whose PP changed, or None
    when the item restores no PP."""
    it = db().get(used["id"]) or {}
    if it.get("cat") != 3:
        return None
    c = bytes.fromhex(it["consts"])
    if c[0] != 1 or c[2] not in (4, 5):
        return None
    amount, = struct.unpack_from("<h", c, 4)
    inv = st.get("inv", [])
    if c[2] == 5:
        targets = [x for x in inv if (db().get(x["id"]) or {}).get("cat") == 1]
    elif a == 0x3C:
        targets = equipped_weapons(st)
        if len(targets) > 1:
            amount = int(amount / 2)
    else:
        targets = [inv[a]] if a < len(inv) and (db().get(inv[a]["id"]) or {}).get("cat") == 1 else []
    changed = []
    for w in targets:
        gain = int(max_pp(w) * amount / 100) if c[7] & 0x10 else amount
        if set_pp(w, cur_pp(w) + gain):
            changed.append(w)
    return changed
