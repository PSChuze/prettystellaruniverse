"""Player-to-player features: parties, partner cards, simple mail, blacklist, view stats.

Layouts from the PS2 client (PSU_US.erx receive table at 0x522970; handler addresses in the comments);
EGS only knew NPC parties. Body offsets are after the 8-byte frame. S>C bodies open with the usual
header: DestLID u16 | 18 zero | 00011300 | DestGID | 8 zero (0x24 bytes).

Party   C>S 100A invite (+0x24 target GID, +0x28 name) -> invitee S>C 100A (0x330710): +0x24 key
        00011200+inviter GID, +0x2C 8 zero, +0x34 inviter name. C>S 100C answer (+0x24 key, +0x2C 8,
        +0x34 u8 result 1 accept / 2 decline, +0x38 name) is forwarded to the inviter as S>C 100C.
        On accept: 1004 (one member, 0x331170) to the members, 1005 (whole party, 0x331580) to the
        newcomer. C>S 1002 leave / 1007 kick (+0x24 slot) -> 1006 events (0x32c7c0): 3 removed by
        leader, 4 left, 13 kicked (others), 7 new leader.
        Member record (0xC0): +0 00011200 GID, +8 0, +0x10 name, +0x50 u16 level, +0x52 ffff,
        +0x54 state (1 player), +0x55 1, +0x56 slot, +0x57 1, +0x58 npc -1, +0x9C quest, zone, map,
        entry, +0xAC LID, +0xB0 counter -1, +0xB4 -1, +0xB8 HP, +0xBC max HP. 1005: +0x24 my slot,
        +0x25 leader slot, records at +0x34 + slot*0xC0, tail from +0x4B4 as EGS.
Cards   C>S 1514 (+0x24 target) -> target S>C 1503 (0x287650, the sender's card record R at +0x24).
        C>S 1504 (+0x24 s16 slot or -1, +0x26 u8 1 accept) -> store, S>C 1509 (+0x24 s16 slot, R).
        C>S 1508 (+0x24 slot, R; R+0x1C8 = 0 deletes). 1501 at load: +0x24 count, (R + u32 slot)*n.
        C>S 1506 -> S>C 1507 (+0x24 200 status bytes, 1 = online). R (0x1D4): name 0x40, race,
        gender, class, voice type, pitch, 3 zero, owner GID, 0x1C zero, comment 0xB0, memo 0xB0,
        u8 1 valid, u8 per-card byte, u8 4, u8 char slot, 8 zero.
Mail    C>S 150B: +0x24 record (0x1AC: sender GID, serial, name at +8, text at +0x48, flags +0x19C),
        +0x1D0 count, +0x1D4 recipient GIDs -> S>C 150F (+0x24 count, records) to each recipient.
        C>S 150D mark read (+0x24 sender GID, +0x28 serial) -> S>C 150E same fields.
Black   C>S 1513: +0x24 u8 1 add / 0 remove, +0x28 GID, +0x2C name, +0x6C u16 u8 u8 -> stored;
        S>C 1512 at load: 80 entries of 0x48 at +0x24.
View    C>S 0A07 (+0x24 target) -> S>C 0A07 (0x396680, size 0x6BC): +0x24 GID, equipment block
        (zeros, which is enough to close the client's wait).
"""
import struct

from psu import db as dbm, games, progression, xgame
from psu.area import map_for
from psu.charload import APPEARANCE, CLASS, c1005_own_party
from psu.login import packet

ACC = bytes.fromhex("00011300")
PLAYER_ID = bytes.fromhex("00011200")
STATE_DIR = None                     # set by game.py (same as its PSU_STATE_DIR)
MAX_CARDS, MAX_MAIL, MAX_BLACK = 200, 100, 80


def u32(v):
    return struct.pack("<I", v & 0xFFFFFFFF)


def hdr(d):
    """S>C body header addressed to player d."""
    return struct.pack("<H", d.lid) + bytes(18) + ACC + u32(d.gid) + bytes(8)


def _db():
    return dbm.get(STATE_DIR)


# --- parties ---------------------------------------------------------------------------------------

class Party:
    def __init__(self, leader):
        self.slots = [leader] + [None] * 5
        self.leader = 0
        self.mission = None          # {"qid", "owner"} once the leader accepts: members depart into it

    def members(self):
        return [(i, p) for i, p in enumerate(self.slots) if p is not None]

    def slot_of(self, p):
        return self.slots.index(p)


PENDING_INVITE = {}                  # invitee gid -> inviter Player
PENDING_CARD = {}                    # receiver gid -> sender Player
_EMPTY = None                        # EGS's empty member record, cut from the solo 1005
_TAIL = None


def _solo_parts():
    global _EMPTY, _TAIL
    if _EMPTY is None:
        body = c1005_own_party(0, bytes(0x200))[8:]
        _EMPTY, _TAIL = body[0xF4:0xF4 + 0xC0], body[0x4B4:]
    return _EMPTY, _TAIL


def member_record(p, slot, game=None):
    """p's record as shown to a player running `game` (the Spaceport map is per disc, area.map_for).
    Same layout on both discs (1004 0x331170 / 0x32d020, 1005 0x331580 / 0x32d430), except +0x52: PSU reads
    a u16, AotI two s8 (NPC fields); ffff reads as "none" on both."""
    q, z, m = map_for(p.area, game, p.game) if p.area else (0xFFFFFFFF, 0, 0)
    level, hp, max_hp = 1, 100, 100
    if p.state is not None:
        max_hp = progression.derived(p.raw, p.state)["max_hp"]
        level, hp = p.state["level"], min(p.state.get("hp", max_hp), max_hp)
    return (PLAYER_ID + u32(p.gid) + bytes(8) + p.raw[0x28:0x68]
            + struct.pack("<HH", level, 0xFFFF) + bytes([1, 1, slot, 1]) + struct.pack("<hH", -1, 0) + loc(p)
            + struct.pack("<IIII", q, z, m, p.entry) + u32(p.lid) + u32(0xFFFFFFFF) + bytes([0xFF, 0, 0, 0])
            + struct.pack("<II", hp, max_hp))


def loc(p):
    """Record +0x5C: the member's location text (0x40 bytes UTF-16; the client printf's it, so no '%')."""
    from psu.world import location_text
    return location_text(p).replace("%", "").encode("utf-16-le")[:0x3E].ljust(0x40, b"\0")


def c100e_location(d, p, slot):
    """S>C 0x100E (0x330990) member location: +0x25 s8 slot, +0x26 s16 zone, +0x28 s16 map, +0x2A s16 entry,
    +0x2C u32 quest, +0x30 location text, +0x70 u32 LID, +0x74 u32, +0x78 s8."""
    q, z, m = map_for(p.area, d.game, p.game) if p.area else (0xFFFFFFFF, 0, 0)
    return packet(0x100E, hdr(d) + struct.pack("<bbhhhI", 0, slot, z, m, p.entry, q) + loc(p)
                  + struct.pack("<IIb", p.lid, 0, -1) + bytes(3))


def c1022_hp(d, hp, slot):
    """S>C 0x1022 (0x330530): +0x24 u32 HP, +0x28 s8 party slot; to every member (solo: slot 0)."""
    return packet(0x1022, hdr(d) + u32(hp) + bytes([slot & 0xFF, 0, 0, 0]))


def c101d_ack(d, value):
    return packet(0x101D, hdr(d) + bytes([value, 0, 0, 0]))


def c1516_card_info(d, p, universe_name):
    """S>C 0x1516 (0x39b6c0), the partner card holder's status window: +0x24 owner GID, +0x28 name,
    +0x68 race, sex, type, +0x70 u16 level, +0x72 u16 type level, +0x74 UTF-16 (0x20), +0x94 UTF-16 line."""
    st = p.state or {}
    cls = st.get("cls", p.raw[0x6A])
    tl = st.get("type_level", [1] * 16)[cls] if cls < 16 else 1
    from psu.world import location_text
    shown = progression.visible_class(cls, d.game)    # an AotI class: its PSU stand-in for a PSU recipient
    body = (u32(p.gid) + p.raw[0x28:0x68] + bytes([p.raw[0x68], p.raw[0x69], shown & 0xFF]) + bytes(5)
            + struct.pack("<HH", st.get("level", 1), tl) + universe_name.encode("utf-16-le")[:0x1E].ljust(0x20, b"\0")
            + location_text(p).encode("utf-16-le")[:0xFE].ljust(0x100, b"\0"))
    return packet(0x1516, hdr(d) + body)


def c0303_shortcuts(gid, shortcuts):
    """S>C 0x0303 (0x404560): 12 chat shortcuts x 0xA4 (0xA0 UTF-16 text + 4 modifier bytes)."""
    out = b""
    for i in range(12):
        out += bytes.fromhex(shortcuts.get(str(i), "")).ljust(0xA4, b"\0")[:0xA4]
    return packet(0x0303, load_hdr(gid) + out)


def c1004_member(d, p, slot):
    return packet(0x1004, hdr(d) + member_record(p, slot, d.game) + bytes(0x130 - 0x24 - 0xC0))


def c1005_party(d, party):
    empty, tail = _solo_parts()
    recs = b"".join(member_record(p, i, d.game) if p is not None else empty for i, p in enumerate(party.slots))
    return packet(0x1005, hdr(d) + bytes([party.slot_of(d), party.leader, 1, 0]) + bytes(4) + b"\xff" * 4 + bytes(4)
                  + recs + tail)


def c1005_solo(d):
    return packet(0x1005, hdr(d) + c1005_own_party(d.gid, d.raw)[8 + 0x24:])


def c1006_event(d, event, slot):
    return packet(0x1006, hdr(d) + bytes([event, slot & 0xFF, 0, 0]))


def c100a_invite(d, inviter):
    return packet(0x100A, hdr(d) + PLAYER_ID + u32(inviter.gid) + bytes(8) + inviter.raw[0x28:0x68])


def c100c_result(d, body):
    """Forward the invitee's 100C (key, 8 bytes, result, name) to the inviter."""
    return packet(0x100C, hdr(d) + body[0x24:0x78])


def c100c_error(d, key_gid, result, name):
    return packet(0x100C, hdr(d) + PLAYER_ID + u32(key_gid) + bytes(8) + bytes([result, 0, 0, 0]) + name)


# --- partner cards ---------------------------------------------------------------------------------

def card_record(p, comment="", char_slot=0, game=None):
    """game: the disc of the player the card is for (an AotI type / voice is shown as PSU's, psu/xgame.py)."""
    raw = p.raw
    cm = comment.encode("utf-16-le")[:0xB0].ljust(0xB0, b"\0")
    rec = (raw[0x28:0x68] + bytes([raw[0x68], raw[0x69], raw[CLASS], raw[APPEARANCE][0], raw[APPEARANCE][1]]) + bytes(3)
           + u32(p.gid) + bytes(0x1C) + cm + bytes(0xB0) + bytes([1, 0, 4, char_slot]) + bytes(8))
    return xgame.card_for(rec, game, p.game) if game else rec


def cards(gid):
    """{slot: card record bytes} for this account."""
    return _db().cards(gid)


def store_card(gid, slot, rec):
    _db().store_card(gid, slot, None if rec is None or rec[0x1C8] == 0 else rec)


def free_card_slot(gid):
    used = cards(gid)
    return next((i for i in range(MAX_CARDS) if i not in used), None)


def c1503_card(d, sender):
    return packet(0x1503, hdr(d) + card_record(sender, game=d.game))


def c1509_slot(d, slot, rec):
    return packet(0x1509, hdr(d) + struct.pack("<hH", slot, 0) + rec)


def load_hdr(gid):
    """Header for packets in the character load (before a zone LID exists): DestLID ffff."""
    return struct.pack("<H", 0xFFFF) + bytes(18) + ACC + u32(gid) + bytes(8)


def c1501_cards(gid, game=None):
    """Stored cards at load, for this session's disc (game): an AotI type / voice in a stored card shows as PSU's."""
    c, game = cards(gid), game or games.current()
    return packet(0x1501, load_hdr(gid) + u32(len(c))
                  + b"".join(xgame.card_for(rec, game) + u32(slot) for slot, rec in sorted(c.items())))


def c1512_blacklist(gid):
    return packet(0x1512, load_hdr(gid) + blacklist_body(gid))


def c1507_status(d, online):
    c = cards(d.gid)
    status = bytearray(MAX_CARDS)
    for slot, rec in c.items():
        owner, = struct.unpack_from("<I", rec, 0x48)
        status[slot] = 1 if owner in online else 0
    return packet(0x1507, hdr(d) + bytes(status))


# --- mail ------------------------------------------------------------------------------------------

def store_mail(gid, rec):
    _db().store_mail(gid, rec)


def mailbox(gid):
    return _db().mailbox(gid, MAX_MAIL)


def mark_read(gid, sender, serial):
    def read(r):
        r = bytearray(r)
        r[0x19C] |= 1
        return bytes(r)
    _db().update_mail(gid, lambda r: struct.unpack_from("<II", r, 0) == (sender, serial), read)


def c150f_mail(d, records):
    return packet(0x150F, hdr(d) + u32(len(records)) + b"".join(records))


def c150e_read(d, sender, serial):
    return packet(0x150E, hdr(d) + u32(sender) + u32(serial))


# --- blacklist -------------------------------------------------------------------------------------

def blacklist_update(gid, body):
    op, = struct.unpack_from("<B", body, 0x24)
    who, = struct.unpack_from("<I", body, 0x28)
    _db().set_blacklist(gid, who, body[0x28:0x70] if op == 1 else None)
    return op, who


def blacklist_body(gid):
    return b"".join(_db().blacklist(gid)[:MAX_BLACK]).ljust(MAX_BLACK * 0x48, b"\0")


# --- view stats ------------------------------------------------------------------------------------

def c0a07_view(d, target_gid):
    return packet(0x0A07, hdr(d) + u32(target_gid) + bytes(0x6BC - 8 - 0x24 - 4))


# --- trade (TItemExchangeManager, singleton [0x6387fc]; static reading of the ERX) ----------------------
# C>S 1901 (+0x24 partner GID, +0x28 u8 action, +0x29 u8 param): 1 request, 2 accept (the asked side opens
# its window by itself; the requester's own 2 comes after "partner accepted"), 4 items confirmed, 6 final OK,
# 8 decline/busy, 9 cancel (param 6 = "no" at the final dialog: back to the window, not a cancel).
# S>C 1901 (0x3977d0) incoming request: +0x24 requester, +0x28 nonzero, +0x2C 2.
# S>C 1902 (0x397990): +0x24 nonzero, +0x28 partner GID (must equal T+0x80 for actions 2..6), +0x2C action,
# +0x2D param (nonzero). (1, 1) partner accepted / (1, 8) declined (requester, state 0); (2, 1) open the
# window (requester, state 2) - not (2, 3), which means "partner cancelled" and resets T, so the window never
# opens; (4, 5) partner confirmed, (4, 1) both confirmed -> final dialog, (4, 3) un-confirm,
# (4, 6 / 7) failed; (5, 5) partner final OK, (5, 3) back to the window; (6, 1) complete; (0, 10) cancelled.
# C>S 1903 / S>C 1904 (0x397240): +0x24 sender, +0x28 receiver, +0x2C UUID (-1 for meseta), +0x30 count,
# +0x34 op (1 add/count, 2 remove, 3 meseta), +0x35 1 = new entry; the partner's copy carries a 0x50-byte
# item record at +0x38 (item data from the id on, UTF-16 name at +0x20, u16 count at +0x4E).
# The client moves nothing on completion: the server swaps and re-sends 0x0A0A / 0x0A06 / 0x0115.
TRADE_ASKED = {}                     # asked player's gid -> requester gid (until accepted / declined)
TRADES = {}                          # gid -> Trade, for both sides


class Trade:
    def __init__(self, a, b):
        self.a, self.b = a, b        # requester, asked (gids)
        self.items = {a: {}, b: {}}  # gid -> {uuid: count}
        self.meseta = {a: 0, b: 0}
        self.confirmed, self.final = set(), set()

    def other(self, gid):
        return self.b if gid == self.a else self.a


def trade_end(t):
    for g in (t.a, t.b):
        if TRADES.get(g) is t:
            del TRADES[g]


def c1901_request(d, requester_gid):
    return packet(0x1901, hdr(d) + u32(requester_gid) + u32(1) + bytes([2, 0, 0, 0]) + bytes(8))


def c1902_state(d, partner_gid, action, param):
    return packet(0x1902, hdr(d) + u32(1) + u32(partner_gid) + bytes([action, param, 0, 0]) + bytes(8))


def c1904_item(d, body, new=False, record=b""):
    b = bytearray(body[0x24:0x38])
    b[0x11] = 1 if new else 0
    return packet(0x1904, hdr(d) + bytes(b) + record)


def trade_record(slot, count, game=None):
    """game: the partner's disc (an AotI weapon's PA level / failure count is not shown to PSU, psu/xgame.py)."""
    from psu import items
    it = items.db()[slot["id"]]
    name = (it["name"] or "").encode("utf-16-le")[:0x2C].ljust(0x2E, b"\0")
    return items.var_rec(slot, game)[8:0x24].ljust(0x20, b"\0") + name + struct.pack("<H", count)


def trade_swap(t, sa, sb, changes=None):
    """Move the offered items and meseta between states sa (t.a) and sb (t.b). Returns an error or None.
    changes (list) receives (gid, "remove" | "add", slot, n) for the 0x0106 updates."""
    from psu import items as _items
    changes = [] if changes is None else changes
    states = {t.a: sa, t.b: sb}
    for g, st in states.items():
        inv = {x["uuid"]: x for x in st.get("inv", [])}
        for uuid, n in t.items[g].items():
            if uuid not in inv or inv[uuid]["qty"] < n or inv[uuid].get("active"):
                return f"{g:#x} no longer has {uuid:#x} x{n}"
        if st.get("meseta", 0) < t.meseta[g]:
            return f"{g:#x} short of meseta"
    for g in (t.a, t.b):
        o = t.other(g)
        if len(states[o].get("inv", [])) + len(t.items[g]) > 60:
            return f"{o:#x} inventory full"
    for g in (t.a, t.b):
        o = t.other(g)
        giver, taker = states[g], states[o]
        for uuid, n in t.items[g].items():
            removed, _err = _items.remove_item(giver, uuid, n)
            changes.append((g, "remove", removed, removed["qty"]))
            added, _err = _items.add_item(taker, removed["id"], removed["qty"], template=removed)
            changes.append((o, "add", added, removed["qty"]))
        giver["meseta"] -= t.meseta[g]
        taker["meseta"] = taker.get("meseta", 0) + t.meseta[g]
    return None
