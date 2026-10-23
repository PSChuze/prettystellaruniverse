"""0x1A01 object/NPC dialogs (EGS egs_net parse 16#1a01 + egs_proto send_1a0x; re-implemented).

C>S 0x1A01 body: DestLID l16 | 0:16 | 0:256 | LID l32 (+0x24) | ShopID l32 (+0x28) | EventID l32 (+0x2C) | A | B
  EventID 0 NPC shop/talk   -> 0x1A02 (a, b, c, d) by ShopID
          2 Lumilass        -> 0x1A03 (needs the per-race head/hair lists: not yet)
          3 PP cube / Photon Charger -> 0x1A04 + a 196-byte rate table captured from the official
            server (EGS p/ppcube.bin, fetched at build time), read from $PSU_CAPTURE_DIR, never committed
          4/5 recharge all/one, 6/7 put on/remove outfit -> no reply known
          9 types menu      -> 0x1A07 with all 16 types enabled
AotI (group 0x1A table 0x56d888): 0x1A02 (+0x36) and 0x1A07 (+0x30, +0x34, 16 bytes at +0x38) read as PSU's.
0x1A04 +0x30 f32, then PSU (0x413400) 12 + 26 f32 at +0x34 / +0x64, AotI (0x418dd0) 20 + 28 at +0x34 / +0x84:
the 196-byte capture is that AotI (PC) layout. 0x1A09 is AotI-only (+0x30 data, +0x434 count; not sent).
"""
import os
import struct

from psu import games
from psu.charload import g
from psu.login import packet

CAPTURE_DIR = os.environ.get("PSU_CAPTURE_DIR", os.path.join(games.WORK, "ref", "egs", "p"))
SHOPS = {80: (17, 17, 3, 9), 90: (5, 1, 4, 5), 91: (5, 5, 4, 7), 92: (5, 8, 4, 0), 93: (5, 18, 4, 0)}
EVENTS = {0: "npc", 2: "lumilass", 3: "ppcube", 4: "ppcube_charge_all", 5: "ppcube_charge_one",
          6: "outfit_on", 7: "outfit_off", 9: "types"}


def head(gid, lid, extra_zero):
    return struct.pack("<H", lid) + bytes(18) + g(gid) + bytes(extra_zero)


def c1a02_shop(gid, a, b, c, d, lid=0):
    return packet(0x1A02, head(gid, lid, 12) + struct.pack("<HHHH", a, b, c, d))


def c1a04_ppcube(gid, table):
    return packet(0x1A04, struct.pack("<H", 0xFFFF) + bytes(18) + g(gid) + bytes(8) + bytes(4) + table)


def c1a07_types(gid, lid=0, enabled=None, fee=0):
    """Types menu (client handler: +0x30 fee in meseta, +0x34 nonzero = confirmable, +0x38 16 enabled bytes).
    PSU has 10 types (0 Hunter .. 9 Protranser), slots 10..15 stay off; AotI enables its 10..15 too (Acrofighter ..
    Acromaster, EGS 1a07.txt order). game.py passes progression.type_menu(state); the default is PSU's."""
    en = bytes(enabled) if enabled is not None else b"\x01" * 10 + bytes(6)
    return packet(0x1A07, head(gid, lid, 12) + struct.pack("<II", fee, 0) + en)   # +0x34 = 1 left the menu waiting


def reply(gid, body, lid=0, enabled=None):
    """(event name, shop id, [(packet, what)]) for one 0x1A01 body. `enabled`: 16 type flags for the types menu."""
    shop, ev = struct.unpack_from("<II", body, 0x28)
    name = EVENTS.get(ev, f"event{ev}")
    out = []
    if ev == 0:
        out.append((c1a02_shop(gid, *SHOPS.get(shop, (0, 1, 0, 0)), lid=lid), f"0x1a02 shop {shop}"))
    elif ev == 3:
        path = os.path.join(CAPTURE_DIR, "ppcube.bin")
        if os.path.exists(path):
            with open(path, "rb") as f:
                out.append((c1a04_ppcube(gid, f.read()), "0x1a04 pp cube"))
    elif ev == 9:
        out.append((c1a07_types(gid, lid, enabled), "0x1a07 types"))
    return name, shop, out


def c1211_object(gid, a, b, c, d):
    return packet(0x1211, struct.pack("<H", 0xFFFF) + bytes(18) + g(gid) + bytes(8) + struct.pack("<IIII", a, b, c, d))


def object_event(gid, body):
    """C>S 0x0F0A object event (EGS parse 16#0f0a): block/group/object/map at +0x24, object id +0x2C,
    object target id +0x30, object type +0x34, and again object type l16 + event id u8 at +0x60.
    Chairs (type 56): event 25 sit / 26 stand -> 0x1211(target, 0, 8, 0 | 2).
    Returns (description, [(packet, what)])."""
    target, = struct.unpack_from("<I", body, 0x30)
    otype, = struct.unpack_from("<H", body, 0x34)
    ev = body[0x62] if len(body) > 0x62 else -1
    out = []
    if otype == 56 and ev in (25, 26):
        out.append((c1211_object(gid, target, 0, 8, 0 if ev == 25 else 2), f"0x1211 chair {'sit' if ev == 25 else 'stand'}"))
    return f"type {otype} event {ev} target {target:#x}", out
