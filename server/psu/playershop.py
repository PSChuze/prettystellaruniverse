"""My Shop: a player's shop in their My Room, shop search, buying while the owner is away, the visitor log and the
Partner Machine's name (PS2 client, both discs; offsets are body offsets = packet offsets - 8).

Both discs carry the whole feature (MyRoomText "Prepare shop", "Open shop", "Switch items for sale", "Write ad",
"Change price", "Put in storage", "Close shop", "Buy", "Search for a shop", "Search by name / ad / item",
"Recommended shops", "View visitor log", "Recently visited rooms"; classes TMyRoomMenuShop, ShopStock, ShopList,
ShopAdvertise, ShopGuest, TermSearchShop, TermVisitShop, TermRecord). The senders are the same on both
(PSU 0x2a1ba0.., AotI 0x2a4c10..), and so are the room record, 0x1323 and 0x1320 handlers. The client keeps the
offline half of the room server (TMyRoomManager, PSU 0x5e74d0): its 0x130d / 0x130f dispatchers (0x23a670,
jump table 0x53a190; 0x23a180, table 0x53a130) and shop routines (0x237320 open, 0x237840 put, 0x2376a0 take back,
0x2375f0 price, 0x237490 take back part, 0x2392e0 buy) are the model for what the server does here.

Room record R (0xc40; 0x130a +0x2C, 0x1323 +0x24; copied field by field by 0x4858d0 / AotI 0x4633e0):
  +0x000 0x40 name (UTF-16), +0x040 8 bytes, +0x048 0xf8 ad text (UTF-16), +0x144 32 x 0x20 shop items (the
  0x20-byte item record with its first word = the shop slot, -1 = empty), +0x544 32 x u32 prices, +0x5c4 u32 open,
  +0x5c8 u32 visitor count (<= 20), +0x5cc 20 x 0x50 visitor log, +0xc0e s16 storage capacity.
  Visitor entry (0x1320, TermRecord 0x3bdc50): s16 year, u8 month, day, hour, minute, second, 0, u32 GID, u8 slot,
  3 zero, 0x40 name. Two 32 x 0x48 constant lists follow R (kind 1 / kind 2, 0x22d860): the shop items' constants.
C>S 0x130d (+0x24 GID, +0x28 sub, +0x2C A, +0x30 B):
  0x10 shop menu opened -> 130e type 2, a2 = customers in the room (the client waits for a2 >= 0, 0x22b510)
  0x11 take back: A = shop slot, B = count (the sender leaves 0: the whole item) -> room storage
  0x12 open (A != 0) / close -> R+0x5c4; 130e type 0xd a1 = open to the room (the client toggles its copy, 0x22b5d0,
       and a guest's buy menu closes on 0)
  0x0d B = 4 visitor log -> 0x1320 (+0x28 count, +0x2C entries); B = 3 recently visited shops -> 0x1321 (+0x28 0;
       its entries are not built yet, so the list shows "No shops visited yet.")
  0x0c B = 0..3 in someone else's room: that room's data (0x1327) / record (0x1323), never the guest's own
C>S 0x130f (+0x28 s16 sub, +0x2A s16 P, +0x2C A, +0x30 B, +0x40 0x20-byte item record):
  0x11 put: P = storage entry (its record word 0), A = price, B = count -> first free shop slot
  0x12 P = shop slot: A >= 0 new price; B > 0 count back to storage
  0x14 buy (guest, ShopGuest 0x3d6a60): P = shop slot, A = price, B = count, +0x40 the shop record as the guest saw
       it -> 130e type 0x13 a2 = 1 / 0 (0x3d8070); meseta goes from the buyer to the owner, online or not
After each change the owner gets 0x1323 + 0x1324 and everyone else in the room 0x1323 (per recipient disc).
C>S 0x1319 ad text (+0x28 0xf8); C>S 0x131a Partner Machine name (+0x28 0x40, TMyRoomMenuProboInputName 0x401d40):
  kept in the room data block at +0x2E5A (s16 1 = named) / +0x2E5C (0x40), which 0x1327 carries (myroom.c1327).
C>S 0x1313 search (+0x28 s16 mode: 0 name, 1 ad, 4 item, 5 recommended; +0x2C u32 1, +0x30 0x40 text)
  -> 0x1314 (+0x2A u16 mode, AotI keeps one result list per mode; +0x2C count, +0x30 30 x 0x148):
  entry +0x00 0x40 name, +0x40 u8 items for sale, +0x41 u8 open, +0x48 u32 owner GID, +0x4C u32 slot, +0x50 0xf8 ad.
  Visiting a result is the usual 0x1304.
Shared world: an item the recipient's disc does not have (xgame.item_ok) is shown as an empty slot, is not found by
a search and cannot be bought.
"""
import struct
import time

from psu import combat, games, items, xgame
from psu.login import packet
from psu.myroom import ACC, QUEST, hdr, stock_records

SLOTS = 32
VISITORS = 20
RESULTS = 30
R_SIZE = 0xC40
ENTRY = 0x148
MESETA_MAX = 99_999_999
CAPACITY = 300


def shop(st):
    sh = st.setdefault("shop", {})
    sh.setdefault("open", 0)
    sh.setdefault("ad", "")
    sh.setdefault("items", [])
    return sh


def room_owner(p):
    """GID of the My Room p stands in (its world zone key is (QUEST, owner)), else None."""
    z = getattr(p, "zone", None)
    if z and z[1] == QUEST and z[2] not in (0, 1, 0xFFFF):
        return z[2]
    return None


def _utf16(hexstr, size):
    b = bytes.fromhex(hexstr or "")
    return b[:size].ljust(size, b"\0")


def _visible(entry, game):
    return entry and entry["item"]["id"] in items.db() and xgame.item_ok(entry["item"]["id"], game)


def slot_entry(st, slot):
    return next((e for e in shop(st)["items"] if e["slot"] == slot), None)


def _item_rec(entry, game):
    r = bytearray(items.var_rec(dict(entry["item"], active=0, slots=0), game)[4:])
    struct.pack_into("<I", r, 0, entry["slot"])
    return bytes(r)


def room_record(st, game=None, name=b""):
    """R (0xc40) for a `game` recipient. name: the shop name block (the owner's character name)."""
    game = game or games.current()
    sh = shop(st or {})
    r = bytearray(R_SIZE)
    r[0:0x40] = name[:0x40].ljust(0x40, b"\0")
    r[0x48:0x140] = _utf16(sh["ad"], 0xF8)
    for i in range(SLOTS):
        struct.pack_into("<i", r, 0x144 + i * 0x20, -1)
    for e in sh["items"]:
        if 0 <= e["slot"] < SLOTS and _visible(e, game):
            r[0x144 + e["slot"] * 0x20:0x164 + e["slot"] * 0x20] = _item_rec(e, game)
            struct.pack_into("<I", r, 0x544 + e["slot"] * 4, e["price"])
    struct.pack_into("<I", r, 0x5C4, 1 if sh["open"] else 0)
    log = (st or {}).get("room_visitors", [])[:VISITORS]
    struct.pack_into("<I", r, 0x5C8, len(log))
    for i, v in enumerate(log):
        r[0x5CC + i * 0x50:0x61C + i * 0x50] = visitor_rec(v)
    struct.pack_into("<h", r, 0xC0E, CAPACITY)
    return bytes(r)


def const_list(st, game=None):
    game = game or games.current()
    recs = [bytes(0x48)] * SLOTS
    for e in shop(st or {})["items"]:
        if 0 <= e["slot"] < SLOTS and _visible(e, game):
            recs[e["slot"]] = items.const_rec(e["item"])
    return b"".join(recs)


def c130a(d, st, name=b""):
    """The owner's full record: R + the two constant lists + the storage."""
    game = getattr(d, "game", games.current())
    recs = stock_records(st)
    lists = const_list(st, game)
    return packet(0x130A, hdr(d) + struct.pack("<I", d.gid) + bytes(4) + room_record(st, game, name) + lists + lists
                  + struct.pack("<I", len(recs)) + b"".join(recs))


def c1323(d, st, name=b""):
    """R + the two constant lists without the storage (handler 0x2a4dd0: R at +0x24, lists at +0xC64 / +0x1564)."""
    game = getattr(d, "game", games.current())
    lists = const_list(st, game)
    return packet(0x1323, hdr(d) + room_record(st, game, name) + lists + lists)


def c130e(d, kind, a1=0, a2=0):
    return packet(0x130E, hdr(d) + struct.pack("<IIIIII", d.gid, kind, a1 & 0xFFFFFFFF, a2 & 0xFFFFFFFF, 0, 0))


def visitor_rec(v):
    t = time.gmtime(v.get("t", 0))
    return (struct.pack("<hBBBBBB", t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, 0)
            + struct.pack("<IB", v["gid"], v.get("slot", 0)) + bytes(3) + _utf16(v.get("name"), 0x40))


def note_visit(owner_st, gid, raw, slot=0, now=None):
    """Put a visitor first in the owner's log (one entry per visitor, at most 20)."""
    log = [v for v in owner_st.get("room_visitors", []) if v["gid"] != gid]
    name = raw[0x28:0x68].hex() if raw else ""
    owner_st["room_visitors"] = [{"gid": gid, "slot": slot, "name": name, "t": int(now or time.time())}] + log[:VISITORS - 1]


def c1320(d, st):
    log = (st or {}).get("room_visitors", [])[:VISITORS]
    return packet(0x1320, hdr(d) + struct.pack("<II", 0, len(log)) + b"".join(visitor_rec(v) for v in log))


def c1321_empty(d):
    return packet(0x1321, hdr(d) + struct.pack("<II", 0, 0))


# --- owner operations (the TMyRoomManager routines) ---------------------------------------------------------

def _stack(item_id):
    return items.stackable(item_id)


def _to_storage(st, item, n):
    """n of `item` (an inventory-like dict) into the room storage. Returns an error or None."""
    stock = st.setdefault("room_stock", [])
    held = next((x for x in stock if x["id"] == item["id"]), None) if _stack(item["id"]) else None
    if held is None and len(stock) >= CAPACITY:
        return "storage full"
    if held is not None:
        held["qty"] += n
    else:
        uuid = max([st.get("next_uuid", 1)] + [x["uuid"] + 1 for x in st.get("inv", []) + stock if x["uuid"] < 0x40000000])
        st["next_uuid"] = uuid + 1
        stock.append(dict(item, uuid=uuid, qty=n, active=0, slots=0))
    return None


def put(st, key, price, count):
    """130f 0x11: storage entry `key` (its record word 0: the UUID, low 16 bits; or its index) into the first free
    shop slot. Returns (slot, error)."""
    sh = shop(st)
    stock = st.setdefault("room_stock", [])
    entry = next((x for x in stock if x["uuid"] & 0xFFFF == key & 0xFFFF), None)
    if entry is None and 0 <= key < len(stock):
        entry = stock[key]
    if entry is None:
        return None, "no such storage entry"
    if price < 0 or price > MESETA_MAX:
        return None, "bad price"
    used = {e["slot"] for e in sh["items"]}
    slot = next((i for i in range(SLOTS) if i not in used), None)
    if slot is None:
        return None, "shop full"
    stack = _stack(entry["id"])
    n = min(max(1, count), entry["qty"]) if stack else 1
    item = dict(entry, qty=n, active=0, slots=0)
    entry["qty"] -= n
    if entry["qty"] <= 0 or not stack:
        stock.remove(entry)
    sh["items"].append({"slot": slot, "item": item, "price": price})
    return slot, None


def take_back(st, slot, count):
    """130d 0x11 / 130f 0x12 B: count (< 0 = all) of shop slot `slot` back to storage. Returns an error or None."""
    e = slot_entry(st, slot)
    if e is None:
        return "empty slot"
    qty = e["item"].get("qty", 1)
    n = qty if count < 0 or count >= qty or not _stack(e["item"]["id"]) else count
    err = _to_storage(st, e["item"], n)
    if err:
        return err
    if n >= qty:
        shop(st)["items"].remove(e)
    else:
        e["item"]["qty"] = qty - n
    return None


def set_price(st, slot, price):
    e = slot_entry(st, slot)
    if e is None or not 0 <= price <= MESETA_MAX:
        return "bad slot or price"
    e["price"] = price
    return None


def set_open(st, flag):
    """Returns True when the flag changed."""
    sh = shop(st)
    new = 1 if flag else 0
    changed = sh["open"] != new
    sh["open"] = new
    return changed


def buy(owner_st, buyer_st, slot, price, count, seen, buyer_game):
    """130f 0x14. seen: the 0x20-byte record the buyer's client showed. Moves the item and the meseta.
    Returns (inventory slot added for 0x0106, units, total, error)."""
    sh = shop(owner_st)
    if not sh["open"]:
        return None, 0, 0, "shop closed"
    e = slot_entry(owner_st, slot)
    if e is None or not _visible(e, buyer_game):
        return None, 0, 0, "no such item"
    if e["price"] != price:
        return None, 0, 0, "price changed"
    if seen is not None and seen[4:8] != bytes.fromhex(e["item"]["id"]):
        return None, 0, 0, "item changed"
    stack = _stack(e["item"]["id"])
    qty = e["item"].get("qty", 1)
    n = min(max(1, count), qty) if stack else 1
    total = price * n
    if buyer_st.get("meseta", 0) < total:
        return None, 0, 0, "meseta"
    template = {k: v for k, v in e["item"].items() if k not in ("uuid", "id", "qty", "active", "slots")}
    added, err = items.add_item(buyer_st, e["item"]["id"], n, template=template)
    if err:
        return None, 0, 0, err
    buyer_st["meseta"] -= total
    owner_st["meseta"] = min(owner_st.get("meseta", 0) + total, MESETA_MAX)
    if n >= qty:
        sh["items"].remove(e)
    else:
        e["item"]["qty"] = qty - n
    return added, n, total, None


# --- search ---------------------------------------------------------------------------------------------------

def _text(hexstr):
    return bytes.fromhex(hexstr or "").decode("utf-16-le", "replace").split("\0")[0]


def search(rows, mode, text, game, limit=RESULTS):
    """rows: (gid, slot, name raw 0x40, state) of characters. Returns 0x1314 entries (bytes) for open shops with
    something the searcher's disc can show, matching mode 0 name / 1 ad / 4 item name / 5 (any)."""
    want = (text or "").strip().lower()
    hits = []
    for gid, slot, name, st in rows:
        sh = (st or {}).get("shop")
        if not sh or not sh.get("open"):
            continue
        shown = [e for e in sh.get("items", []) if _visible(e, game)]
        if not shown:
            continue
        nm = name.decode("utf-16-le", "replace").split("\0")[0].lower()
        if want and mode == 0 and want not in nm:
            continue
        if want and mode == 1 and want not in _text(sh.get("ad")).lower():
            continue
        if want and mode == 4 and not any(want in (items.db()[e["item"]["id"]].get("name") or "").lower() for e in shown):
            continue
        hits.append((len(shown), gid, slot, name, sh))
    hits.sort(key=lambda h: -h[0])
    out = []
    for n, gid, slot, name, sh in hits[:limit]:
        out.append(name[:0x40].ljust(0x40, b"\0") + bytes([min(n, 255), 1]) + bytes(6)
                   + struct.pack("<II", gid, slot) + _utf16(sh.get("ad"), 0xF8))
    return out


def c1314(d, mode, entries):
    body = b"".join(entries[:RESULTS])
    return packet(0x1314, hdr(d) + struct.pack("<IhHI", 0, 0, mode & 0xFFFF, len(entries[:RESULTS]))
                  + body.ljust(RESULTS * ENTRY, b"\0"))


def parse_search(body):
    mode, = struct.unpack_from("<h", body, 0x28)
    text = body[0x30:0x70].decode("utf-16-le", "replace").split("\0")[0] if len(body) >= 0x70 else ""
    return mode, text


# --- Partner Machine name -------------------------------------------------------------------------------------

def set_probo_name(st, name):
    pr = st.setdefault("probo", {"type": 0, "stats": [0, 0, 0, 0, 0]})
    pr["name"] = bytes(name[:0x40]).hex()


def probo_name(st):
    pr = (st or {}).get("probo") or {}
    return bytes.fromhex(pr["name"]) if pr.get("name") else None


def meseta_packet(p):
    """0x0115 with p's level block (meseta included), as the NPC shop sends after a sale."""
    return combat.c0115(p, 0xFFFFFFFF)


# --- session glue (game.py passes a Ctx) -----------------------------------------------------------------------

class Ctx:
    """What a game session lends this module: me (world.Player), state (my character state), send / send_paced
    (async, to me), save_state(), event(**kw), sid, db (psu.db.DB), world (world.World), chars(gid) -> {slot: raw}."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _owner(ctx, owner):
    """(state, Player or None, slot, raw) of a room owner, online or not (offline: slot 0, as 0x1304 visits)."""
    me = ctx.me
    if owner == me.gid:
        return ctx.state, me, getattr(me, "slot", 0), me.raw
    p = ctx.world.online.get(owner)
    if p is not None and p.state is not None:
        return p.state, p, getattr(p, "slot", 0), p.raw
    raw = ctx.chars(owner).get(0)
    return ctx.db.char_state(owner, 0) or {}, None, 0, raw


def _save_owner(ctx, owner, st, p, slot):
    if owner == ctx.me.gid:
        ctx.save_state()
    else:
        ctx.db.save_char_state(owner, slot, st)


def _name(raw):
    return raw[0x28:0x68] if raw else b""


async def _refresh(ctx, owner, st, raw, stock=False):
    """The room record to everyone in the owner's room (the owner also gets the storage when asked)."""
    from psu import myroom
    me = ctx.me
    room = [me] + ctx.world.others(me) if room_owner(me) == owner else [me]
    for d in room:
        data = c1323(d, st, _name(raw))
        if d is me:
            await ctx.send(data, "0x1323 room record")
            if stock and owner == me.gid:
                await ctx.send_paced(myroom.c1324(d, st), "0x1324 storage")
        else:
            ctx.world.relay(d, data, "0x1323 room record")


async def handle(ctx, cmd, body):
    """A My Shop / visitor log / PM name packet. Returns True when it was one (nothing else should answer)."""
    from psu import myroom
    from psu.area import user_bin
    me, st = ctx.me, ctx.state
    if me is None or st is None:
        return False
    here = room_owner(me)
    if cmd == 0x130D and len(body) >= 0x34:
        sub, a, b = struct.unpack_from("<III", body, 0x28)
        if sub == 0x10:
            customers = len(ctx.world.others(me)) if here == me.gid else 0
            await ctx.send(c130e(me, 2, 0, customers), f"0x130e shop menu ({customers} customers)")
            return True
        if sub == 0x11:
            err = take_back(st, a, b if 0 < b < 0x80000000 else -1)
            ctx.event(ev="myshop", sid=ctx.sid, op="take_back", slot=a, n=b, error=err)
            if err is None:
                ctx.save_state()
            await _refresh(ctx, me.gid, st, me.raw, stock=True)
            return True
        if sub == 0x12:
            changed = set_open(st, a)
            ctx.event(ev="myshop", sid=ctx.sid, op="open" if a else "close", changed=changed)
            if changed:
                ctx.save_state()
                flag = shop(st)["open"]
                room = [me] + ctx.world.others(me) if here == me.gid else [me]
                for d in room:
                    data = c130e(d, 0xD, flag)
                    if d is me:
                        await ctx.send(data, f"0x130e shop open {flag}")
                    else:
                        ctx.world.relay(d, data, "0x130e shop open")
            await _refresh(ctx, me.gid, st, me.raw)
            return True
        if sub == 0x0D and b in (3, 4):
            if b == 4:
                await ctx.send(c1320(me, st), f"0x1320 visitor log ({len(st.get('room_visitors', []))})")
            else:
                await ctx.send(c1321_empty(me), "0x1321 visited shops (none)")
            return True
        if sub == 0x0C and b in (0, 1, 2, 3) and here is not None and here != me.gid:
            o_st, o_p, _, o_raw = _owner(ctx, here)
            if o_raw is None:
                return False
            if b == 0:
                o_byte = user_bin(here, 0, o_raw, me.area, 0, st=o_st)[0x15]
                await ctx.send(myroom.c1327(me, here, o_byte, o_raw, o_st, 0), "0x1327 room data (visit)")
            else:
                await ctx.send(c1323(me, o_st, _name(o_raw)), "0x1323 room record (visit)")
            return True
        return False
    if cmd == 0x130F and len(body) >= 0x60:
        sub, p_ = struct.unpack_from("<hh", body, 0x28)
        a, b = struct.unpack_from("<ii", body, 0x2C)
        if sub == 0x11:
            slot, err = put(st, p_ & 0xFFFF, a, b)
            ctx.event(ev="myshop", sid=ctx.sid, op="put", key=p_ & 0xFFFF, price=a, n=b, slot=slot, error=err)
            if err is None:
                ctx.save_state()
            await _refresh(ctx, me.gid, st, me.raw, stock=True)
            return True
        if sub == 0x12:
            errs = []
            if a >= 0:
                errs.append(set_price(st, p_, a))
            if b > 0:
                errs.append(take_back(st, p_, b))
            ctx.event(ev="myshop", sid=ctx.sid, op="edit", slot=p_, price=a, back=b, errors=[e for e in errs if e])
            if any(e is None for e in errs):
                ctx.save_state()
            await _refresh(ctx, me.gid, st, me.raw, stock=b > 0)
            return True
        if sub == 0x14:
            owner = here
            if owner is None or owner == me.gid:
                await ctx.send(c130e(me, 0x13, 0, 0), "0x130e buy refused")
                return True
            o_st, o_p, o_slot, o_raw = _owner(ctx, owner)
            added, n, total, err = buy(o_st, st, p_, a, b, bytes(body[0x40:0x60]), me.game)
            ctx.event(ev="myshop", sid=ctx.sid, op="buy", owner=f"{owner:#x}", slot=p_, price=a, n=n, total=total,
                      online=o_p is not None, error=err)
            if err is None:
                ctx.save_state()
                _save_owner(ctx, owner, o_st, o_p, o_slot)
                await ctx.send(items.c0106_add(me.gid, me.lid, added), f"0x0106 add {added['id']} (shop)")
                await ctx.send(meseta_packet(me), "0x0115 meseta")
                if o_p is not None:
                    ctx.world.relay(o_p, meseta_packet(o_p), "0x0115 meseta (shop sale)")
            await ctx.send(c130e(me, 0x13, 0, 1 if err is None else 0), "0x130e buy result")
            await _refresh(ctx, owner, o_st, o_raw)
            return True
        return False
    if cmd == 0x1319 and len(body) >= 0x120:
        shop(st)["ad"] = bytes(body[0x28:0x120]).hex()
        ctx.save_state()
        ctx.event(ev="myshop", sid=ctx.sid, op="ad", text=_text(shop(st)["ad"]))
        return True
    if cmd == 0x131A and len(body) >= 0x68:
        set_probo_name(st, body[0x28:0x68])
        ctx.save_state()
        ctx.event(ev="probo_name", sid=ctx.sid, name=_text(st["probo"]["name"]))
        return True
    if cmd == 0x1313 and len(body) >= 0x30:
        mode, text = parse_search(body)
        rows = []
        for gid, slot, name, s in ctx.db.shop_rows():
            p = ctx.world.online.get(gid)
            if p is not None and p.state is not None and getattr(p, "slot", 0) == slot:
                s = p.state
            rows.append((gid, slot, name, s))
        found = search(rows, mode, text, me.game)
        ctx.event(ev="myshop", sid=ctx.sid, op="search", mode=mode, text=text, found=len(found))
        await ctx.send(c1314(me, mode, found), f"0x1314 shops ({len(found)})")
        return True
    return False


def visited(ctx, owner):
    """0x1304: note the visitor in the owner's log."""
    if owner == ctx.me.gid:
        return
    o_st, o_p, o_slot, o_raw = _owner(ctx, owner)
    if o_raw is None:
        return
    note_visit(o_st, ctx.me.gid, ctx.me.raw, getattr(ctx.me, "slot", 0))
    _save_owner(ctx, owner, o_st, o_p, o_slot)
