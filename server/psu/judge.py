"""Mission judge: start / clear rules, rank, rewards (the offline TQuestJudge, run by the server online).

Online the client only keeps a status struct; the server evaluates the quest.snr rules (missions.json
"quest", from extract/missiondata.py):
  start / clear / fail: RPN token lists (evaluator 0x395f10): 0..6 = AND OR EQ LT LE GT GE (left operand =
      second popped), 100 n = push n, 120 zone map_id event = flag set (map id -> the zone's map index,
      flag = index * 256 + event), 134 / 135 quest zone map_id = the player is there (below), 147 = the result
      rank. null = never (fail/abort are null on the disc). Other commands (ARGC) evaluate as never.
  134 (AotI FM 020 "Electronic Brain" start rules: 134 <own id> 0 <enter map> 100 1 EQ): the PMF table
      (PSU 0x55ab50, AotI 0x56a490: 16 B per command 100.., {this_delta, vtbl_offset, -}, +0xC operand
      count) is filled at boot, so it is read from EE RAM (AotI savestate, module base 0x23ce00): 134 and 135
      -> vtable +0x84 / +0x88 of TQuestContractConditionCalcClient (AotI 0x668240: 0x3972f0, 0x396dd0 = a
      thunk to +0x84; PSU 0x5f1c60: 0x3994b0, same code). It reads the current quest id, zone and map id
      (vfuncs 0x8c/0x90/0x94 of the 0x55350 singleton) and calls 0x393b10 (PSU 0x3960d0): true when quest
      != -1, arg quest != -1, quest == arg, and zone == arg (arg < 0: any), map == arg (arg < 0: any).
      Online: the session's area (quest, zone, map). Not a disc difference: PSU has it too, its rules never use it.
  rank (0x3cbe70): sections {type, max, min, par, mult}: score = clamp(max - mult * (par - value), min, max);
      value by type: 0/1 elapsed seconds, 2 enemies killed, 3 party deaths, 4 times APC <arg> was incapacitated
      (the story escorts: Ep2 Flash of Ruin 2 arg 21 Tomrain, Illuminus Ambition 2 arg 7 Lou, Ep3 Despair and
      Hope 1 arg 18 Lumia; run()["apc_downs"][arg], which no client packet feeds yet: the server does not see an
      APC fall, so the escort counts as unhurt), 6 listed flags set.
      rank = first i with total <= thresholds[i], else 3 (index 3 = best).
  rewards: [reward id, condition (147 rank test)] -> quest rewards {item, qty, flags (bit 2 meseta), meseta}
  type_points[rank]: type EXP for the current type (0x5d6a0: clamp to the last row, level = 1 + rows <= exp)
S>C 0x0C09 clear with results (0x1696e0): +0x24 1, +0x25 1, +0x28 elapsed, +0x2C the 0x628-byte result
  block (0x481b00): +0 rank, +1 participation %, +2 sections, +4 total, +8 adjusted, +0xC 16-byte sections
  {type, kind, par, value, score}, +0x8C meseta, +0x90 flags, +0x91 item count, items from +0x98 (0x44 each:
  s8 -1, s8 qty, u8, 64-byte item at +4), +0x624 flags (1 rank, 4 items). 0x65C bytes: fragmented.
  The parser also takes +0x624 flags 2 (sub-block at +0x6C), 8 (+0x340) and 0x10 (+0x5F4, two records of u16
  values, likely old/new stats; unverified); none is sent. The "New record" banner (WindowText 15 #8) cannot show:
  the parser forces its byte (0x4821b0).
No in-game leaderboard: TWindowResultRanking (PSU vtable 0x5f0d30, setter 0x382260;
  AotI setter 0x381100) is a per-mission score ranking (top 30 + own row, 0x84C-byte block) opened by TResultDisp
  state 4 only when +0x28 is set, and nothing sets it; its network packet 0x1A06 has an empty handler on both discs
  (PSU 0x3daa30, AotI 0x3dc340). Only a client patch could show it, so rankings live on the board (psu/board.py).
"""
import struct
import time

from psu import instance, items, missions, progression
from psu.login import packet

ACC = bytes.fromhex("00011300")
runs = {}               # (universe, quest, owner) -> {"t0", "kills", "deaths", "started", "cleared"}


def run(uni, qid, owner):
    return runs.setdefault((uni, qid, owner), {"t0": None, "kills": 0, "deaths": 0, "apc_downs": {},
                                                "started": False, "cleared": False})


def flag_set(uni, qid, owner, zone, map_id, ev):
    m = missions.mission(qid) or {}
    maps = m.get("zones", {}).get(str(zone), [])
    if map_id not in maps:
        return False
    fid = maps.index(map_id) * 256 + ev
    inst = instance.instances.get((uni, qid, (zone, owner)))
    if inst is None:
        return False
    i, b = fid >> 3, 1 << (fid & 7)
    return i < len(inst.flags_a) and bool(inst.flags_a[i] & b)


# operand count per command (table +0xC, same on both discs for 100..151; AotI adds 152, 153 = 0 operands)
ARGC = {100: 1, 101: 1, 102: 1, 103: 1, 104: 1, 105: 1, 106: 1, 107: 1, 108: 1, 109: 1, 110: 1, 111: 1, 112: 1,
        113: 1, 118: 1, 119: 1, 120: 3, 124: 2, 125: 2, 126: 2, 127: 1, 128: 1, 129: 1, 133: 1, 134: 3, 135: 3,
        137: 1, 144: 1, 151: 1}


def at_place(at, quest, zone, map_id):
    """134/135 (0x393b10): the player's (quest, zone, map) matches; zone/map < 0 match any."""
    if at is None or quest == -1 or at[0] == -1 or at[0] != quest:
        return False
    return (zone < 0 or at[1] == zone) and (map_id < 0 or at[2] == map_id)


def evaluate(tokens, flag, rank=None, at=None):
    """Run an RPN rule. flag(zone, map_id, event) -> bool; at = the player's (quest, zone, map) for 134/135.
    None tokens = never (False)."""
    if not tokens:
        return False
    st, i = [], 0
    ops = {0: lambda a, b: a and b, 1: lambda a, b: a or b, 2: lambda a, b: a == b, 3: lambda a, b: a < b,
           4: lambda a, b: a <= b, 5: lambda a, b: a > b, 6: lambda a, b: a >= b}
    while i < len(tokens):
        t = tokens[i]
        if t in ops:
            b, a = st.pop(), st.pop()
            st.append(int(bool(ops[t](a, b))) if t > 1 else int(bool(ops[t](a, b))))
            i += 1
        elif t == 100:
            st.append(tokens[i + 1])
            i += 2
        elif t == 120:
            st.append(int(flag(tokens[i + 1], tokens[i + 2], tokens[i + 3])))
            i += 4
        elif t in (134, 135):
            st.append(int(at_place(at, *tokens[i + 1:i + 4])))
            i += 4
        elif t == 147:
            st.append(rank if rank is not None else -1)
            i += 1
        else:
            return False                                  # unknown token: never
    return bool(st and st[-1])


def rank_of(q, elapsed, r, flag):
    rk = q.get("rank") or {}
    secs = []
    for s in rk.get("sections", []):
        if s["type"] in (0, 1):
            value = elapsed
        elif s["type"] == 2:
            value = r["kills"]
        elif s["type"] == 3:
            value = r["deaths"]
        elif s["type"] == 4:
            value = r.get("apc_downs", {}).get(s["arg"], 0)
        elif s["type"] == 6:
            value = sum(1 for f in s.get("flags", []) if flag(*f))
        else:
            value = 0
        score = max(s["min"], min(s["max"], s["max"] - s["mult"] * (s["par"] - value)))
        secs.append((s, value, score))
    total = sum(x[2] for x in secs)
    th = rk.get("thresholds", [399, 699, 999, 1000])
    rank = next((i for i, t in enumerate(th) if total <= t), 3)
    return rank, total, secs


def rewards(q, rank, flag):
    out = []
    for rid, cond in (q.get("rank") or {}).get("rewards", []):
        if evaluate(cond, flag, rank):
            rw = q.get("rewards", {}).get(str(rid))
            if rw:
                out.append(rw)
    return out


def add_type_exp(raw, st, amount):
    """0x5d6a0: type EXP of the current type, clamped to the type level cap (progression.type_cap: retail 20,
    the disc tables' last row); returns (old level, new level). Rows from the disc that defines the class
    (progression.type_rows: AotI's 10..15). A type already past its cap (kept by the curve migration) gains no
    EXP and keeps its level."""
    cls = st["cls"]
    rows = progression.type_rows(cls)
    cap = progression.type_cap(cls)
    old = st["type_level"][cls]
    top = max(rows[cap - 1][0], st["type_exp"][cls])
    exp = max(0, min(st["type_exp"][cls] + amount, top))
    st["type_exp"][cls] = exp
    level = min(1 + sum(1 for k in range(1, len(rows)) if rows[k][0] <= exp), cap)
    st["type_level"][cls] = old if old > cap else level
    return old, st["type_level"][cls]


def result_block(rank, total, secs, meseta, got_items):
    b = bytearray(0x628)
    b[0], b[1], b[2] = rank, 100, len(secs)
    struct.pack_into("<ii", b, 4, total, total)
    for i, (s, value, score) in enumerate(secs[:6]):
        struct.pack_into("<bbxxIii", b, 0xC + 16 * i, s["type"], s["kind"], s["par"], value, score)
    struct.pack_into("<I", b, 0x8C, meseta)
    b[0x91] = min(len(got_items), 10)
    for i, slot in enumerate(got_items[:10]):
        o = 0x98 + 0x44 * i
        b[o], b[o + 1] = 0xFF, slot.get("qty", 1) & 0xFF
        rec = items.var_rec(slot)[4:] + items.const_rec(slot)[:0x20]
        b[o + 4:o + 0x44] = rec[:0x40]
    b[0x624] = 1 | (4 if got_items else 0)
    return bytes(b)


def c0c09_results(gid, lid, elapsed, block):
    return packet(0x0C09, struct.pack("<H", lid) + bytes(18) + ACC + struct.pack("<I", gid) + bytes(8)
                  + bytes([1, 1, 0, 0]) + struct.pack("<I", elapsed) + block)


def now():
    return time.time()
