"""Synthesis (My Room production, the offline TItemProductManager; PS2 offsets are body offsets).

Data: $PSU_GAMEDATA/synthesis.json (extract/econdata.py): boards "0dTTII00" -> level, freq_max (uses),
variant_board, slot_types[4], wildcard, outputs[8] {id, rate, element, fail_group}, materials[4][8] {id, qty,
flag (1 = wildcard)}; failure groups (weighted ids); element tables (weighted percents). Production is instant
on the PS2 (the clock stubs return 0). Materials come from the My Room stock (state["room_stock"]).
Per character: state["synth"] = 32 entries {board, running, fail, fail_id, used, variant, slots[4], result}.
S>C 0x130E +0x28 type +0x2C a1 +0x30 a2 +0x34 a3 +0x38 a4; 0x1310 (0x54) +0x28 kind +0x2A slot +0x2C record
(0 result, 2 to stock, 4 material of slot); 0x131b (0x90) +0x28 kind, +0x2A E, +0x2C A, +0x2E B, +0x34
record of X, +0x54 D, +0x56 F, +0x5A name of X; 0x131c (0x78) +0x28 const_rec(result).
C>S 0x130f: +0x28 sub, +0x2A s16 P, +0x30 u32 Q, +0x3C UUID, +0x40 0x20-byte record; 0x130d: +0x28 sub.

AotI (synthesis.json from extract/econdata.py --game aoti: 264 boards in 12 loader slots, so "cat" is not a Probo stat;
failure/element weights out of 1000). Same records and packets; the rules differ (AotI ERX):
  rate 0x4031e0: r = outputs[v].rate; r < 2 or r == 100 -> r; else int(100 * (0.01 * min(100, 95 - 0.33 * (10 *
      level - skill))) * (0.01 * r)) in f32, clamped 0..100. skill (0x339d30 -> level 0x37de30, then 1..200) from
      outputs[v].id: armour (cat 2) Probo stat 3; weapon type < 14 stat 0, 14..23 and 27 stat 1, 24..26 stat 2,
      other weapons level(1) = 0; other categories 1
  roll 0x41ae60: success when outputs[0].rate == 100, the rate is 100 or rand(100) < rate (no +4); failure: failure
      [fail_group] by rand(1000) + 1 against cumulative weights (0x41a550; group 0, past the table or past the
      weights -> the normal output, 0x41b0cc); element percent (0x41a380) the same way, element 0 -> 0 %
  Goods boards (type 9) read WpnShoot2's tables (0x41b790 maps 9 -> slot 8): lookup()
"""
import json
import os
import random
import struct

from psu import games, items, progression
from psu.login import packet

ACC = bytes.fromhex("00011300")
N_ENTRIES = 32
def data():
    return games.json_data("synthesis.json")


def hdr(d):
    return struct.pack("<H", d.lid) + bytes(18) + ACC + struct.pack("<I", d.gid) + bytes(8)


def entries(st):
    e = st.setdefault("synth", [None] * N_ENTRIES)
    while len(e) < N_ENTRIES:
        e.append(None)
    return e


def lookup(bid):
    """Board record for a board item id. AotI's type -> loader slot map (0x41b790) sends type 9 (Goods) to slot 8
    (WpnShoot2), and the row is the first one there with the same id byte 2 (0x41b870): 0d09II00 runs 0d0bII00's
    recipe, uses and outputs. The level stays the Goods board's own (item +0x14, 0x4031e0); variant_board
    (0x41b2b0: board types 1 2 3 4 7 10..13, or several outputs) comes from the outputs alone for type 9."""
    bs = data().get("boards", {})
    b = bs.get(bid)
    if b is None or games.current() != games.AOTI or bid[2:4] != "09":
        return b
    src = bs.get("0d0b" + bid[4:6] + "00")
    if src is None:
        return None
    outs = src["outputs"]
    return {**src, "name": b["name"], "board_type": 9, "level": b["level"], "board_b1": b["board_b1"],
            "variant_board": outs[0]["a"] != 0 or outs[1]["id"] != "00000000"}


def board(entry):
    return lookup(entry["board"]) if entry else None


# --- records ----------------------------------------------------------------------------------------------

def rec20(item_id, qty=1, uuid=0, element=None, pct=None):
    if not item_id or item_id in ("00000000", "ffffffff") or item_id not in items.db():
        return bytes(0x20)
    slot = {"uuid": uuid, "id": item_id, "qty": qty, "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]}
    r = bytearray(items.var_rec(slot))
    if items.db()[item_id]["cat"] == 1 and element is not None:
        r[0x18], r[0x19] = element & 0xFF, (pct or 0) & 0xFF
    return bytes(r[4:])


def name_of(item_id):
    it = items.db().get(item_id or "", {})
    return (it.get("name") or "").encode("utf-16-le")[:0x2C].ljust(0x2E, b"\0")


def c130e(d, kind, a1=0, a2=0, a3=0, a4=0):
    return packet(0x130E, hdr(d) + struct.pack("<Iiiiii", d.gid, kind, a1, a2, a3, a4))


def c1310(d, kind, slot, rec):
    return packet(0x1310, hdr(d) + struct.pack("<Ihh", d.gid, kind, slot) + rec)


def c131b(d, kind, E, A, B, item_id, D=0, F=0, rec=None):
    return packet(0x131B, hdr(d) + struct.pack("<Ihhhh", d.gid, kind, E, A, B) + bytes(4)
                  + (rec if rec is not None else rec20(item_id)) + struct.pack("<hhh", D, F, 0) + name_of(item_id))


def c131c(d, item_id):
    slot = {"uuid": 0, "id": item_id, "qty": 1, "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]}
    body = items.const_rec(slot) if item_id in items.db() else bytes(0x48)
    return packet(0x131C, hdr(d) + struct.pack("<I", d.gid) + body[:0x48])


# --- rules ------------------------------------------------------------------------------------------------

def stackable(item_id):
    return items.stackable(item_id)


def stock_count(st, item_id):
    stock = st.get("room_stock", [])
    if stackable(item_id):
        return sum(x["qty"] for x in stock if x["id"] == item_id)
    return sum(1 for x in stock if x["id"] == item_id)


def material(entry, b, slot, variant):
    """(item id, need) for a slot: the entry's own slot item for slot type 1, else the variant's recipe item."""
    v = max(0, variant)
    m = b["materials"][slot][v]
    if b["slot_types"][slot] == 1 and entry["slots"][slot]:
        return entry["slots"][slot], m["qty"]
    return (m["id"] if m["id"] not in ("ffffffff", "00000000") else None), m["qty"]


def recipe(b, variant):
    return [b["materials"][k][max(0, variant)]["id"] for k in range(4)]


def output_max(st, entry, b):
    if b["variant_board"]:
        return 1
    n = b["freq_max"] - entry["used"]
    for k in range(4):
        mid, need = material(entry, b, k, 0)
        if mid and need > 0:
            n = min(n, stock_count(st, mid) // need)
    return max(n, -10 if n <= 0 else n)


def _skill_aoti(st, item_id):
    from psu.econ import _lv
    stats = st.get("probo", {}).get("stats", [0] * 5)
    iid = bytes.fromhex(item_id or "00000000")
    if iid[0] == 2:
        lv = _lv(stats[3])
    elif iid[0] == 1:
        t = iid[1]
        k = 0 if t < 14 else 1 if t < 24 or t == 27 else 2 if t <= 26 else None
        lv = _lv(stats[k]) if k is not None else _lv(1)
    else:
        lv = 1
    return max(1, min(lv, 200))


def _rate_aoti(st, b, variant):
    from psu.econ import _f32, _rz
    out = b["outputs"][max(0, variant)]
    r = out["rate"]
    if r >= 2 and r != 100:
        skill = _skill_aoti(st, out["id"])
        m = _rz(_f32(95.0) + _rz(_f32(-0.33) * _f32(float(10 * b["level"] - skill))))
        m = min(m, 100.0)
        f = _rz(_rz(_f32(0.01) * m) * _rz(_f32(0.01) * _f32(float(r))))
        r = int(_rz(_f32(100.0) * f))
    return max(0, min(r, 100))


def _cum1000(rows, weight):
    """rand(1000) + 1 against cumulative weights (AotI 0x41a550 / 0x41a380); None past the last."""
    x, acc = random.randrange(1000) + 1, 0
    for row in rows:
        acc += weight(row)
        if x <= acc:
            return row
    return None


def rate(st, entry, b, variant):
    if games.current() == games.AOTI:
        return _rate_aoti(st, b, variant)
    out = b["outputs"][max(0, variant)]
    if out["rate"] >= 100:
        return 100
    stats = st.get("probo", {}).get("stats", [0] * 5)
    skill = max(0, stats[b["cat"]] // 100) if b["cat"] < 4 else 1
    skill = max(1, min(skill, 200))
    return int(out["rate"] * min(100.0, 95 + 0.33 * (skill - 10 * b["level"])) / 100)


def _weighted(rows, key="weight"):
    total = sum(r[key] if isinstance(r, dict) else r[1] for r in rows)
    if total <= 0:
        return None
    x = random.randrange(total)
    for r in rows:
        w = r[key] if isinstance(r, dict) else r[1]
        if x < w:
            return r
        x -= w
    return rows[-1]


def roll(st, entry, b):
    """Result for the entry (sticky once rolled): {id, element, pct, fail}."""
    if entry.get("result"):
        return entry["result"]
    v = max(0, entry["variant"])
    out = b["outputs"][v]
    if games.current() == games.AOTI:
        r = rate(st, entry, b, v)
        ok = b["outputs"][0]["rate"] == 100 or r == 100 or random.randrange(100) < r
        pick = None if ok else _cum1000(data().get("failure", {}).get(str(out["fail_group"]), []),
                                        lambda x: x["weight"])
        if pick:
            res = {"id": pick["id"], "element": 0, "pct": 0, "fail": 1}
        else:
            rows = data().get("element", {}).get(str(out["element"]), []) if out["element"] else []
            el = _cum1000(rows, lambda x: x[1])
            res = {"id": out["id"], "element": out["element"], "pct": el[0] if el else 0, "fail": 0}
        entry["result"] = res
        return res
    ok = random.randrange(100) < rate(st, entry, b, v) + 4 or b["outputs"][0]["rate"] >= 100
    res = {"id": out["id"], "element": out["element"], "pct": 0, "fail": 0}
    if ok:
        el = _weighted(data().get("element", {}).get(str(out["element"]), [[0, 100]]))
        res["pct"] = el[0] if el else 0
    else:
        grp = data().get("failure", {}).get(str(out["fail_group"]), [])
        pick = _weighted(grp) if grp else None
        if pick:
            res = {"id": pick["id"], "element": 0, "pct": 0, "fail": 1}
    entry["result"] = res
    return res


def consume(st, entry, b, variant, n):
    stock = st.setdefault("room_stock", [])
    for k in range(4):
        mid, need = material(entry, b, k, variant)
        left = need * n
        if not mid or left <= 0:
            continue
        for x in list(stock):
            if left <= 0:
                break
            if x["id"] != mid:
                continue
            take = min(left, x["qty"]) if stackable(mid) else 1
            x["qty"] -= take
            left -= take
            if x["qty"] <= 0 or not stackable(mid):
                stock.remove(x)


# --- the protocol ----------------------------------------------------------------------------------------

def _rooms(st, item_id):
    if not item_id or not stackable(item_id):
        return 1, 1
    mx = items.stack_max(item_id)
    in_stock = stock_count(st, item_id)
    held = sum(x["qty"] for x in st.get("inv", []) if x["id"] == item_id)
    return max(0, mx - in_stock), max(0, mx - held)


def entry_rows(d, st, i):
    """The four packets per entry of the board list (130d 7) and the material rows."""
    e = entries(st)[i]
    b = board(e)
    if e is None or b is None:
        return [(c131b(d, 0, i, 0, 0, None), None), (c131b(d, 2, i, 0, 0, None), None),
                (c130e(d, 0xB, i, 0, 0, 0), None)] + [(c131b(d, 1, i, k, 0, None), None) for k in range(4)]
    v = e["variant"]
    out = b["outputs"][max(0, v)]
    sr, ir = _rooms(st, out["id"])
    rows = [(c131b(d, 0, i, e["running"] + (10 if b["wildcard"] else 0), 0 if b["variant_board"] else 1,
                   e["board"], e["used"], b["freq_max"]), None),
            (c131b(d, 2, i, rate(st, e, b, v), 0, out["id"], v, output_max(st, e, b),
                   rec=rec20(out["id"], element=out["element"])), None),
            (c130e(d, 0xB, i, sr, 0, ir), None)]
    for k in range(4):
        mid, need = material(e, b, k, v)
        rows.append((c131b(d, 1, i, k, b["slot_types"][k], mid, need, stock_count(st, mid) if mid else 0), None))
    return rows


def handle(d, st, cur, cmd, body):
    """One production C>S (0x130d subs 1/4/6/7/8, 0x130f subs 2/3/5/6/7/0xB..0x10). cur = session dict
    {"entry", "variant"}. Returns ([(packet, what)], inventory changes [("add"|"remove", slot, n)])."""
    sub = struct.unpack_from("<I" if cmd == 0x130D else "<h", body, 0x28)[0]
    out, inv = [], []
    ents = entries(st)
    if cmd == 0x130D:
        if sub == 7:
            for i in range(N_ENTRIES):
                out += [(p, f"synth list {i}") for p, _ in entry_rows(d, st, i)]
        elif sub in (8, 4) and cur.get("entry") is not None:
            e = ents[cur["entry"]]
            b = board(e)
            if e is None or b is None:
                return out, inv
            if sub == 4:
                e["running"], e["variant"], e["result"] = 0, -1, None
            cur["variant"] = -1
            e["slots"] = recipe(b, 0)
            if sub == 8:
                out.append((c130e(d, 7, -1), "0x130e synth variant"))
                o = b["outputs"][0]
                out.append((c131b(d, 2, cur["entry"], rate(st, e, b, 0), 0, o["id"], -1, output_max(st, e, b)),
                            "0x131b synth output"))
                for k in range(4):
                    mid, need = material(e, b, k, 0)
                    out.append((c1310(d, 4, k, rec20(mid)), "0x1310 synth material"))
                    out.append((c131b(d, 1, cur["entry"], k, b["slot_types"][k], mid, need,
                                      stock_count(st, mid) if mid else 0), "0x131b synth material"))
        elif sub == 6 and cur.get("entry") is not None:
            e = ents[cur["entry"]]
            b = board(e)
            if e is None or b is None:
                return out, inv
            r = roll(st, e, b)
            sr, ir = _rooms(st, r["id"])
            out += [(c1310(d, 0, 0, rec20(r["id"], element=r["element"], pct=r["pct"])), "0x1310 synth result"),
                    (c130e(d, 0xB, cur["entry"], sr, 0, ir), "0x130e synth result"),
                    (c131b(d, 3, r["fail"], sr, ir, r["id"]), "0x131b synth result"),
                    (c131c(d, r["id"]), "0x131c synth result")]
        return out, inv

    P, = struct.unpack_from("<h", body, 0x2A)
    Q, = struct.unpack_from("<I", body, 0x30)
    rid = body[0x44:0x48].hex() if len(body) >= 0x48 else ""
    ruuid, = struct.unpack_from("<I", body, 0x40) if len(body) >= 0x44 else (0,)
    if sub in (2, 5):                                          # register a board from the inventory
        b = lookup(rid)
        if b is None:
            return out, inv
        i = P if (sub == 5 and 0 <= P < N_ENTRIES) else next((k for k, x in enumerate(ents) if x is None), None)
        if i is None:
            return out, inv
        removed, err = items.remove_item(st, ruuid, 1)
        if err:
            return out, inv
        inv.append(("remove", removed, 1))
        ents[i] = {"board": rid, "running": 0, "fail": 0, "fail_id": -1, "used": 0, "variant": -1,
                   "slots": recipe(b, 0), "result": None}
    elif sub == 6 and 0 <= Q < N_ENTRIES and ents[Q] is not None:
        cur["entry"] = Q
        b = board(ents[Q])
        cur["variant"] = -1 if b and b["variant_board"] else 0
        out += [(c130e(d, 3, 0, 0), "0x130e synth board"), (c130e(d, 4, ents[Q]["running"], 0), "0x130e synth running")]
    elif sub == 7 and 0 <= Q < N_ENTRIES:
        cur["entry"] = Q
        cur["variant"] = ents[Q]["variant"] if ents[Q] else -1
    elif sub == 0x10 and 0 <= Q < N_ENTRIES and ents[Q] is not None:
        e, b = ents[Q], board(ents[Q])
        for k in range(4):
            for v in range(8):
                m = b["materials"][k][v]
                mid = m["id"] if m["id"] not in ("ffffffff", "00000000") else None
                out.append((c131b(d, 4, Q, k * 10 + v, m["flag"], mid, m["qty"],
                                  stock_count(st, mid) if mid else 0), "0x131b synth candidate"))
    elif sub == 3 and cur.get("entry") is not None and 0 <= P < 4:
        e = ents[cur["entry"]]
        b = board(e)
        if e is None or b is None:
            return out, inv
        v = next((k for k in range(8) if b["materials"][P][k]["id"] == rid), None)
        if v is None:
            v = 7 if any(b["materials"][P][k]["flag"] == 1 for k in range(8)) else 0
        e["variant"] = cur["variant"] = v
        e["slots"] = recipe(b, v)
        e["slots"][P] = rid
        out.append((c130e(d, 7, v), "0x130e synth variant"))
        o = b["outputs"][v]
        out.append((c131b(d, 2, cur["entry"], rate(st, e, b, v), 0, o["id"], v, output_max(st, e, b)),
                    "0x131b synth output"))
        for k in range(4):
            mid, need = material(e, b, k, v)
            out.append((c1310(d, 4, k, rec20(mid)), "0x1310 synth material"))
            out.append((c131b(d, 1, cur["entry"], k, b["slot_types"][k], mid, need,
                              stock_count(st, mid) if mid else 0), "0x131b synth material"))
    elif sub == 0xB and cur.get("entry") is not None:            # start (variant boards: one product)
        e = ents[cur["entry"]]
        b = board(e)
        if e is None or b is None:
            return out, inv
        e["running"], e["variant"], e["result"] = 1, max(0, P), None
        consume(st, e, b, e["variant"], 1)
        out.append((c130e(d, 0xA, 0), "0x130e synth started"))
    elif sub == 0xC:
        if cur.get("entry") is not None and ents[cur["entry"]] is not None:
            e = ents[cur["entry"]]
            e["running"], e["result"] = 1, None
            e["variant"] = max(0, e["variant"])
        out.append((c130e(d, 0xC, 0), "0x130e synth started"))
    elif sub in (0xD, 0xE, 0xF) and cur.get("entry") is not None:  # take out: inventory / stock / discard
        e = ents[cur["entry"]]
        b = board(e)
        if e is None or b is None or not e.get("result"):
            return out, inv
        n = max(1, Q) if not b["variant_board"] else 1
        r = e["result"]
        if not b["variant_board"]:
            consume(st, e, b, 0, n)
        if sub == 0xD and r["id"] in items.db():
            tmpl = {"grind": 0, "pa": [0, 0]}
            added, err = items.add_item(st, r["id"], n, template=tmpl)
            if not err:
                inv.append(("add", added, n))
        elif sub == 0xE and r["id"] in items.db():
            st.setdefault("room_stock", []).append({"uuid": 0x30000000 + random.randrange(0x0FFFFFFF), "id": r["id"],
                                                    "qty": n, "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]})
            out.append((c1310(d, 2, 0, rec20(r["id"], n)), "0x1310 synth to stock"))
        e["used"] += n
        e["running"], e["result"] = 0, None
        if e["used"] >= b["freq_max"]:
            ents[cur["entry"]] = None
    return out, inv
