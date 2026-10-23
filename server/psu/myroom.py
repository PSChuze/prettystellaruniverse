"""My Room (PS2 client; offsets are body offsets). From static reading of PSU_US.erx.

Data: the only room zone on the disc is the offline Extra lobby's zone 1 (area 22 = TAreaMyRoom, one map,
map 1 = scene XF_SN_CM0001, set 0); the server serves that quest/zone as quest 1120000 zone 1
(extract/native-lobby-zones.sh). The Colony elevators ask for 1120000/0/100; the room console's
"visit" warp asks for 1120000/-1/-1/-1; both land in the player's own room for now.

  S>C 0x1327 room data (frame 0x980, handler 0x2a4fe0; may arrive before or after the zone load):
      +0x24 u32 owner GID, +0x28 u8 owner byte (user_bin byte 0x15; the client compares both with the
      player to decide "own room"), +0x2C owner info (UTF-16 name 0x40 + 8), +0x74 / +0x78 u32,
      +0x7C 32 furniture slot bytes (goods id = item id byte 2, 0xff = empty), ..., +0x928 s16 Probo type
  C>S 0x1328 room ready (+0x24 GID): no reply
  C>S 0x130b set object (frame 0x38): +0x24 GID, +0x28 s16 slot, +0x2A s16, +0x2C u32 inventory UUID
      (a "0b 01 XX 00" goods item) or -1 = take the goods back
  S>C 0x130c (frame 0x38, handler 0x2a5f10): +0x28 s16 slot, +0x2A s16 goods id (0xff = remove)
The room's set has three furniture spots (TMyRoomSetter kind 0): slots 1, 2 and 3.
AotI (SLUS-21631): the same packet layouts for 0x1327 (but see c1327: +0x31), 0x130a, 0x1324, 0x130c (goods
ids up to 0x11f there, 0xfe in PSU), 0x130e (types 0..0x26; the ones sent here unchanged), 0x1310/12/18/1b/1c.
Its room is its own offline lobby's zone 1 (quest 200000, served as 1120000 from $PSU_NBL_DIR/aoti/).
Per character: state["room"] = 32 slot bytes (default all 0xff; 0 would show goods 0, the kadomatsu).
"""
import struct

from psu import games, items
from psu.login import packet

ACC = bytes.fromhex("00011300")
QUEST, ZONE, MAP = 1120000, 1, 1
SPOTS = (1, 2, 3)
ROOM_VAR = bytes.fromhex("06000000" "02000000") + bytes(8) + bytes.fromhex("40000000" "00010000" "00010000")


def hdr(d):
    return struct.pack("<H", d.lid) + bytes(18) + ACC + struct.pack("<I", d.gid) + bytes(8)


def slots(st):
    s = st.get("room") if st else None
    return bytes(s) if s and len(s) == 32 else b"\xff" * 32


def c1327(d, owner_gid, owner_byte, owner_raw, owner_st, entrance=1):
    """AotI (handler 0x2a8040) also keeps +0x29 (packet +0x31) and its "own room" test (0x230ac0, used by the
    console, the Probo and 0x130e) wants it equal to the client's entrance: [0x6b6aac] + 1, set from 0x0216 +0x32
    (0 from this server) and sent back as 0x080E's Entrance (the client sends 1). With 0 there AotI treats its own room as a
    visit. PSU's handler (0x2a4fe0) reads only the GID and the owner byte; it gets 0 there as before.
    d is the recipient (Player), entrance the one its client sent in 0x080E."""
    name = owner_raw[0x28:0x68] if owner_raw else bytes(0x40)
    second = entrance & 0xFF if getattr(d, "game", games.current()) == games.AOTI else 0
    body = (struct.pack("<I", owner_gid) + bytes([owner_byte, second, 0, 0]) + name + bytes(8)
            + bytes(8) + slots(owner_st))
    body = bytearray(body + bytes(0x978 - 0x24 - len(body)))
    pr = (owner_st or {}).get("probo")
    if pr:                                   # room block +0x2E10.. is carried at body +0x904: rank, +0x2E34 type, +0x2E78 stats
        base = 0x904 - 0x2E10 - 0x24
        struct.pack_into("<h", body, base + 0x2E10, sum(max(0, x // 100) for x in pr["stats"][:4]))
        struct.pack_into("<h", body, base + 0x2E34, pr["type"])
        struct.pack_into("<5h", body, base + 0x2E78, *pr["stats"][:5])
        if pr.get("name"):                   # +0x2E5A s16 1 = named, +0x2E5C the name (C>S 0x131a, psu/playershop.py);
            struct.pack_into("<h", body, base + 0x2E5A, 1)     # 0x1327 carries the block up to +0x2E84 (0x2a5178),
            o = base + 0x2E5C                                  # so 20 of the name's 32 characters
            nm = bytes.fromhex(pr["name"])[:len(body) - o]
            body[o:o + len(nm)] = nm
    return packet(0x1327, hdr(d) + bytes(body))


def c130c(d, slot, goods):
    return packet(0x130C, hdr(d) + struct.pack("<I", d.gid) + struct.pack("<hh", slot, goods) + bytes(4))


def set_object(st, slot, uuid):
    """Apply a 0x130b to st. Returns (goods id for 0x130c, error, inventory change) where the change is
    ("add", slot, n) or ("remove", slot, n) for items.c0106_add / c0106_remove."""
    if slot not in SPOTS:
        return None, f"slot {slot} is not a furniture spot", None
    room = bytearray(slots(st))
    if uuid == 0xFFFFFFFF:                                  # take back
        g = room[slot]
        if g == 0xFF:
            return None, "empty", None
        added, err = items.add_item(st, f"0b01{g:02x}00", 1)
        if err:
            return None, err, None
        room[slot] = 0xFF
        st["room"] = list(room)
        return 0xFF, None, ("add", added, 1)
    item = next((x for x in st.get("inv", []) if x["uuid"] == uuid), None)
    if item is None or not item["id"].startswith("0b01"):
        return None, "not a room goods item", None
    if room[slot] != 0xFF:
        return None, "spot taken", None
    g = int(item["id"][4:6], 16)
    removed, err = items.remove_item(st, uuid, 1)
    if err:
        return None, err, None
    room[slot] = g
    st["room"] = list(room)
    return g, None, ("remove", removed, 1)


# --- console menus (C>S 0x130d: +0x24 GID, +0x28 u32 sub, +0x2C A, +0x30 B, +0x34 C, +0x38 D) ---------
# Most subs wait for S>C 0x130e "notify" (frame 0x44, handler 0x2a5920): +0x28 u32 type, +0x2C arg1, +0x30 arg2.
#   0x19 -> 130e type 21 (arg2 BGM id; 0x1a default)     0x1e -> 130e type 23 (arg2 room flag)
#   0x16 -> 130e type 18 (item stock menu ready)         0x15 -> 130e type 17 (arg1 1 = may give, arg2 count)
#   0x0c B=0 -> room data (0x1327); B=1..3 -> 0x130a room record + stock (empty: capacity 300, count 0);
#        B=4 -> 0x1318 (0x404-byte box, zeros)
#   0x1a / 0x1b / 0x01 / 0x07 (production helper) -> no reply
# C>S 0x130f sub 4 give item (+0x3C UUID, +0x40 item record) -> S>C 0x1312 (+0x28 s16 evolved, +0x2A s16
# Probo type, +0x2C 5 x (s16 stat, s16 delta)) to the giver, 130e type 22 (Probo type) to the room.
GIVE_LIMIT = 100
STOCK_CAPACITY = 300


def c130e(d, kind, a1=0, a2=0):
    return packet(0x130E, hdr(d) + struct.pack("<IIIIII", d.gid, kind, a1, a2, 0, 0))


def stock_records(st):
    """0x68-byte stock records: the 32-byte item (var_rec from +4) + the 0x48-byte constants record."""
    return [items.var_rec(x)[4:] + items.const_rec(x) for x in (st or {}).get("room_stock", [])]


def c130a(d, st):
    """Room record R (0xc40; shop, visitor log, R+0xc0e = stock capacity) + two 32 x 0x48 lists (the shop items'
    constants) + the item stock; built by psu/playershop.py."""
    from psu import playershop
    return playershop.c130a(d, st, d.raw[0x28:0x68] if getattr(d, "raw", None) else b"")


# --- My Shop, visitor log, Partner Machine name: psu/playershop.py (imported late, it imports this module) ---
SHOP_CMDS = (0x130D, 0x130F, 0x1313, 0x1319, 0x131A)


def shop_ctx(**kw):
    from psu import playershop
    return playershop.Ctx(**kw)


async def shop_handle(ctx, cmd, body):
    """True when psu/playershop.py answered cmd (game.py then skips its own branch)."""
    from psu import playershop
    return await playershop.handle(ctx, cmd, body)


def shop_visited(ctx, owner):
    from psu import playershop
    playershop.visited(ctx, owner)


def c1324(d, st):
    """The item stock list alone (handler sets beeb): +0x24 count, 0x68-byte records from +0x28."""
    recs = stock_records(st)
    return packet(0x1324, hdr(d) + struct.pack("<I", len(recs)) + b"".join(recs))


def deposit(st, uuid, n):
    """Inventory -> room stock (C>S 0x130f sub 0). Returns (removed slot, error)."""
    stock = st.setdefault("room_stock", [])
    item = next((x for x in st.get("inv", []) if x["uuid"] == uuid), None)
    if item is None:
        return None, "no such item"
    held = next((x for x in stock if x["id"] == item["id"]), None) if items.stackable(item["id"]) else None
    if held is None and len(stock) >= STOCK_CAPACITY:
        return None, "stock full"
    removed, err = items.remove_item(st, uuid, max(1, n))
    if err:
        return None, err
    if held is not None:
        held["qty"] += removed["qty"]
    else:
        stock.append(dict(removed, active=0, slots=0))
    return removed, None


def withdraw(st, index, n):
    """Room stock -> inventory (C>S 0x130d sub 0). Returns (added slot, error)."""
    stock = st.setdefault("room_stock", [])
    if not 0 <= index < len(stock):
        return None, "no such stock entry"
    entry = stock[index]
    stack = items.stackable(entry["id"])
    n = min(max(1, n), entry["qty"]) if stack else 1
    added, err = items.add_item(st, entry["id"], n, template=entry)
    if err:
        return None, err
    entry["qty"] -= n
    if entry["qty"] <= 0 or not stack:
        stock.remove(entry)
    return added, None


def c1318_empty(d):
    return packet(0x1318, hdr(d) + struct.pack("<I", d.gid) + bytes(4) + bytes(0x404))


def c1312(d, probo_type=0):
    return packet(0x1312, hdr(d) + struct.pack("<I", d.gid) + struct.pack("<hh", 0, probo_type) + bytes(20))


def menu_reply(d, st, sub, b):
    """[(packet, what)] for a C>S 0x130d sub (b = body+0x30)."""
    if sub == 0x19:
        return [(c130e(d, 21, 0, 0x1A), "0x130e bgm")]
    if sub == 0x1E:
        return [(c130e(d, 23, 0, 0), "0x130e room flag")]
    if sub == 0x16:
        return [(c130e(d, 18), "0x130e stock menu ready")]
    if sub == 0x15:
        n = (st or {}).get("room_gifts", 0)
        return [(c130e(d, 17, 1 if n < GIVE_LIMIT else 0, n), f"0x130e give ok ({n})")]
    if sub == 0x0C and b in (1, 2, 3):
        return [(c130a(d, st), f"0x130a room record ({len((st or {}).get('room_stock', []))} stocked)")]
    if sub == 0x0C and b == 4:
        return [(c1318_empty(d), "0x1318 box (empty)")]
    return []


def give_item(st, uuid):
    """C>S 0x130f sub 4: one of the item goes to the Probo. Returns the removed slot or None."""
    removed, err = items.remove_item(st, uuid, 1)
    if err:
        return None
    st["room_gifts"] = st.get("room_gifts", 0) + 1
    return removed
