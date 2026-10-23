"""NPC item shops (PS2 client; offsets are body offsets). From static reading of PSU_US.erx.

Talking to a shop NPC: C>S 0x1A01 event 0 (+0x28 NPC id) -> any S>C 0x1A02 lets the dialog go on; the
shop NPC then asks for its list online (offline it reads the disc):
  C>S 0x010A (0x3C): +0x24 GID, +0x28 LID, +0x2C u8 event, +0x2E u8 A, +0x2F u8 B, +0x30 u32 param
      1 enter (param = NPC id), 2 buy (param = index into the list sent, A = quantity; clothing: colour),
      3 sell (A = quantity, param = UUID), 4 gift-wrap (not handled), 5 leave
  S>C 0x010A (handler 0x14f7c0): +0x24 GID, +0x28 LID (must be the local player), +0x2C event,
      +0x2D bit 0 = failed
      1: +0x2E u8 count, +0x2F u8 category, +0x30 0, +0x34 count x 0x4C: UTF-16 name (0x2E), rarity0, 0,
         4-byte item id, u32 buy price (<= 0 hides the entry), 20-byte constants
      2: +0x30 the new item's 0x24-byte variable record, +0x54 its 0x48-byte constants record
      3: +0x2E u8 quantity, +0x30 UUID (the client removes it)
  Meseta is not touched by the client online: S>C 0x0115 (level block, enemy id -1) carries it.
Data: $PSU_GAMEDATA/shops.json (extract/gamedata.py): npcs[id] = [list 0..3, category], tiers[NN][cat] =
4 lists of item ids. Buy price = items.json value_base, sell price = items.json price (the constants record).
The tier (file NN 01..12) grows with the story; the server picks it from the player's level. A list can be
empty in a tier (PSU tier 01: the Colony disc shop, NPC 32 = list 3 category 3; PSU tiers 01..03: every
Material list): the client cannot hold 0 entries (0x999e0: the allocator 0xae10 returns NULL for size 0, so
0x9abf0 calls 0x9a0a0 and the window closes at once), so an empty list is taken from the nearest tier that
has one (later tiers first).
Sell price: online the client shows u32 +0x30 of the item's constants record (0x99690 -> 0xb80f0 reads
mgr+0x94[item+0x22] +0x30; offline it computes base * rate / 10000 at 0xb4660, which is items.json price) times
the quantity, and the server pays the same: sell_price(). A bought item takes the character's next UUID below
0x40000000 like items.add_item; 0x40000000 and up are ground UUIDs (drops.UUID_BASE), and a bought item there
collides with a later drop of that UUID once picked up.
AotI (0x14cd90; list 0x9b6b0, bought 0x141fd0, sold 0x142240) reads the same offsets and 0x4C entries; only a
clothing id (category 9) differs: AotI's colour mask is (id byte 1 >> 4) << 8 | byte 3, PSU's is byte 3, the same
for ids with a type below 0x10. Events 1..6 (PSU 1..5).
"""
import json
import os
import struct

from psu import games, items, progression
from psu.login import packet

ACC = bytes.fromhex("00011300")
CATEGORIES = ("Weapon", "Shield", "Medicine", "Disk", "ButickM", "ButickF", "PartsM", "PartsF", "Material",
              "Board", "Misc", "Unit", "Dungeon")
def data():
    return games.json_data("shops.json", required=True)


def tier_for(level):
    t = os.environ.get("PSU_SHOP_TIER")
    return int(t) if t else max(1, min(12, 1 + (level - 1) // 8))


def item_key(iid):
    """items.json key for a shop id (clothing carries a colour mask in the 4th byte)."""
    db = items.db()
    return iid if iid in db else (iid[:6] + "00" if iid[:6] + "00" in db else None)


def listing(npc, level):
    """[(items.json key, disc id bytes)] and the category for shop NPC `npc`, or (None, None)."""
    npcs = data()["npcs"]
    if not 0 <= npc < len(npcs):
        return None, None
    lst, cat = npcs[npc]
    tier = tier_for(level)
    for t in [tier] + list(range(tier + 1, 13)) + list(range(tier - 1, 0, -1)):
        out = entries(t, cat, lst)
        if out:
            return out, cat
    return [], cat


def entries(tier, cat, lst):
    """[(items.json key, disc id bytes)] of list `lst` of category `cat` in tier file NN = tier (buyable only)."""
    ids = data()["tiers"].get(str(tier), {}).get(str(cat), [[], [], [], []])[lst]
    out = [(item_key(i), bytes.fromhex(i)) for i in ids]
    return [(k, b) for k, b in out if k and (items.db()[k].get("value_base") or 0) > 0]


def sell_price(slot):
    """Meseta for one of `slot` sold to an NPC shop: the constants record's price (items.const_rec +0x30)."""
    return items.db()[slot["id"]].get("price") or 0


def head(d, event, flag=0):
    return (struct.pack("<H", d.lid) + bytes(18) + ACC + struct.pack("<I", d.gid) + bytes(8)
            + struct.pack("<II", d.gid, d.lid) + bytes([event, flag]))


def c010a_list(d, entries, cat):
    body = b""
    for key, disc_id in entries[:255]:
        it = items.db()[key]
        name = (it["name"] or "").encode("utf-16-le")[:0x2C].ljust(0x2E, b"\0")
        body += (name + bytes([it.get("rarity0", 0), 0]) + disc_id + struct.pack("<I", it["value_base"])
                 + bytes.fromhex(it["consts"]))
    return packet(0x010A, head(d, 1) + bytes([len(entries[:255]), cat]) + bytes(4) + body)


def c010a_bought(d, slot):
    return packet(0x010A, head(d, 2) + bytes(2) + items.var_rec(slot) + items.const_rec(slot))


def c010a_sold(d, qty, uuid):
    return packet(0x010A, head(d, 3) + bytes([qty & 0xFF, 0]) + struct.pack("<I", uuid))


def c010a_fail(d, event):
    return packet(0x010A, head(d, event, 1) + bytes(2) + bytes(4))


def buy(st, key, qty):
    """Add qty of item `key` to state st, paying value_base each. Returns (slot for the reply, error)."""
    it = items.db()[key]
    qty = max(1, qty)
    stack = items.stackable(key)
    if not stack:
        qty = 1
    cost = it["value_base"] * qty
    if st.get("meseta", 0) < cost:
        return None, "meseta"
    inv = st.setdefault("inv", [])
    nxt = max([st.get("next_uuid", 1)] + [x["uuid"] + 1 for x in inv if x["uuid"] < 0x40000000])
    held = next((x for x in inv if x["id"] == key), None) if stack else None
    if held is None and len(inv) >= items.MAX_ITEMS:
        return None, "inventory full"
    st["meseta"] -= cost
    st["next_uuid"] = nxt + 1
    new = {"uuid": nxt, "id": key, "qty": qty, "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]}
    if held is not None:
        held["qty"] = min(held["qty"] + qty, items.stack_max(key))   # the client merges the reply's stack into
                                                                    # the one it holds, clamped (0xf5f70)
    else:
        inv.append(new)
    return new, None


def sell(st, uuid, qty):
    """Sell qty of the item with `uuid`; returns (quantity sold, meseta gained) or (0, 0)."""
    inv = st.get("inv", [])
    slot = next((x for x in inv if x["uuid"] == uuid), None)
    if slot is None or slot.get("active"):
        return 0, 0
    qty = max(1, min(qty, slot["qty"]))
    gain = sell_price(slot) * qty
    slot["qty"] -= qty
    if slot["qty"] <= 0:
        inv.remove(slot)
    st["meseta"] = st.get("meseta", 0) + gain
    return qty, gain
