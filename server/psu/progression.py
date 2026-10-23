"""Character progression the PS2 client expects the server to own (it shows what it is sent).

Tables: $PSU_GAMEDATA/plylevel.json, built from the disc (GA_PLYCOMMON.NBL / PlyLevelParamData.snr) by
extract/gamedata.py; never committed. Formula = client 0x1f4ac0:
  k = race% (type row) ; HP = level HP * hp% * k // 10000 ; stat[i] = level stat[i] * stat%[i] * k // 10000
Stat order everywhere: ATP, DFP, ATA, EVP, STA, S5 (hidden), TP, MST.

Persisted state (db characters.state JSON): cls (type index 0 Hunter .. 9 Protranser; AotI adds 10 Acrofighter,
11 Acrotecher, 12 Fighmaster, 13 Gunmaster, 14 Masterforce, 15 Acromaster), level, exp (running total), meseta,
blast, luck, playtime, type_level[16], type_exp[16], hp.

Types per disc: a PSU session offers classes 0..9 (creation, 0x1A07 menu, 0x0110 event 3 type change); an AotI
session 0..15 (its 0x0113 / 0x1A07 handlers read all 16 entries; type names PlayerText #48..53). A class's rows
(type EXP, HP/stat/race %, requirements) come from the disc that defines it: 0..9 from the session's own
plylevel.json, 10..15 always from AotI's ($PSU_GAMEDATA/aoti/plylevel.json, extract/gamedata.py --game aoti),
so an AotI-class character keeps its stats and type EXP when it logs in from PSU. On the AotI disc types
10..15 are placeholder rows (every % 100, 30 type levels); those are what the client would compute too.
Levels: the PSU disc has rows for 1..100, AotI's for 1..200 (level_cap()).
What a PSU client is shown for an AotI-only class: visible_class().

Retail type tables (extract/gamedata.py --type-overrides, then
retail_types.py build in extract/): both discs carry the AotI-era rows for 0..9 (30 rows each, as AotI's disc) and a
`type_caps` key (retail cap per type: 20, AotI's placeholder Acromaster 30). Type EXP never raises a type past its
cap (judge.add_type_exp, type_cap()); a level a character already had above it is kept. A PSU client is never sent
a type level above 10 (visible_type_level). Stored type EXP from the disc curves is converted once to the retail
curve keeping each type level (migrate_type_exp; migrate_db on game start; marker state["type_curve"]).

char_bin level block (char+0xA4, 112 B): u16 level, u16 0, u16 blast, s8 luck, 5 zero, u32 EXP, 4 zero,
u32 meseta, u32 playtime, 20 zero, 16 x u32 (type_level | type_exp << 16).
Play time is seconds (both discs' panels print it as h / 3600, h / 60 % 60, h % 60: PSU 0x3d34f0 and 0x37b740 with
"%02d:%02d:%02d"; EGS PlayTime:32 at the same place). The client never counts it online: only 0x0115 (0x144e10, AotI
0x1419f0) and 0x010D (0x144ad0, AotI 0x1416c0) write the player's copy (+0x5bc, AotI +0x6c0), so the server keeps
it: start_clock when a character is selected, every level_block banks the whole seconds since, stop_clock on
logout or reselect.
Status block (0x164 B after char_bin in user_bin): u32 EXP for next level, u32 EXP for this level,
u32 max HP, 8 x u16 stats, 16 zero, u8 level + 3, 8 x i16 stats, u32 HP, u32 max HP, 168 zero,
5 x {f32 1.0, 8 zero}, 20 zero, f32 1.0, 32 zero.
"""
import json
import os
import struct
import time

from psu import games

GAMEDATA = games.GAMEDATA
LEVEL_CAP = 100                 # the PSU disc table has data for levels 1..100 (AotI: level_cap())
TYPE_EXP_STEP = 250
# Classes a session of that disc may pick. AotI's table has 16 rows, but Acromaster (15) was never a retail type
# (an empty placeholder row anyone could change to): offered only with PSU_AOTI_ACROMASTER=1.
# The PSU client's PlyLevelParamData has 10 type-level rows per type (header +2) and reads them with no bound:
# Corr::Calc 0x1f4ca0 (AotI 0x1f5970) takes the row at types[type] + (TL - 1) * 16, TL = the u16 at char +0xD4 +
# type * 4 (stat recompute 0x5e610, run by 0x5ded0 / 0x5fb30 after equip and level events), and the types are
# packed 0xA0 apart, so TL 11..20 would read the next type's rows (Protranser: the placeholder rows of type 10).
# Its own type EXP add (0x5d6a0, offline judge 0x3cbe70 / event 0x5fbc0) clamps to row 10 itself.
PSU_CLIENT_TYPE_LEVELS = 10
TYPE_CURVE = "retail-1"         # state["type_curve"] once its type EXP is on the retail curve (migrate_type_exp)
# Type EXP totals per type level the stored EXP was earned on before the retail tables (both discs, identical
# where they overlap; PSU's 10 rows are AotI's first 10): 0..9 every 250 to TL10, then 300 / 500; 10..15
# (always AotI's rows) every 100 to TL10, then the same tail.
_DISC_TAIL = [2500, 2800, 3100, 3400, 3700, 4000, 4300, 4600, 4900, 5200, 5500, 6000, 6500, 7000, 7500, 8000,
              8500, 9000, 9500, 10000]
DISC_TYPE_EXP = [list(range(0, 2251, 250)) + _DISC_TAIL] * 10 + [list(range(0, 901, 100)) + _DISC_TAIL] * 6
CLASSES = {games.PSU: 10, games.AOTI: 16 if os.environ.get("PSU_AOTI_ACROMASTER") == "1" else 15}
# What a PSU client is shown for an AotI-only class (visible_class): the PSU type nearest in role.
PSU_STANDIN = {10: 0, 11: 5, 12: 6, 13: 7, 14: 8, 15: 3}


def tables(game=None):
    return games.json_data("plylevel.json", game=game, required=True)


def class_count(game=None):
    """Classes 0..n-1 exist for this disc (PSU 10, AotI 16)."""
    return CLASSES.get(game or games.current(), 10)


def type_table(cls, game=None):
    """The plylevel.json that defines class cls: AotI's for 10..15, else the session's."""
    return tables(games.AOTI if cls >= CLASSES[games.PSU] else game)


def type_rows(cls, game=None):
    """Type rows [type EXP total, HP %, 8 stat %, 4 race %] per type level for class cls. Type EXP code should
    read these (not tables()["types"]) so an AotI class played from a PSU session keeps AotI's 30 levels."""
    return type_table(cls, game)["types"][cls]


def type_cap(cls, game=None):
    """Highest type level type EXP can bring class cls to: the table's type_caps (retail: 20), else its rows."""
    t = type_table(cls, game)
    caps = t.get("type_caps")
    rows = len(t["types"][cls])
    return min(caps[cls], rows) if caps and cls < len(caps) else rows


def visible_type_level(tl, recipient_game=None):
    """The type level to put on the wire for a client of recipient_game: a PSU client gets at most 10 (its rows,
    PSU_CLIENT_TYPE_LEVELS); the stored level, stats and type EXP stay the real ones."""
    if (recipient_game or games.current()) == games.PSU:
        return min(tl, PSU_CLIENT_TYPE_LEVELS)
    return tl


def retail_curve():
    """True when both discs' tables are the retail ones (type_caps) with the same type EXP for 0..9: the
    condition for converting stored type EXP (migrate_type_exp)."""
    try:
        p, a = tables(games.PSU), tables(games.AOTI)
    except FileNotFoundError:
        return False
    if "type_caps" not in p or "type_caps" not in a:
        return False
    return all([r[0] for r in p["types"][c]] == [r[0] for r in a["types"][c]] for c in range(CLASSES[games.PSU]))


def level_for_exp(curve, exp):
    return 1 + sum(1 for k in range(1, len(curve)) if curve[k] <= exp)


def convert_exp(old, new, level, exp):
    """Type EXP on curve new for a type kept at level whose exp was on curve old: the same fraction of the way
    to the next level (none at the last row of either curve)."""
    level = max(1, min(level, len(new)))
    base = new[level - 1]
    if level >= len(new) or level >= len(old):
        return base
    lo, hi = old[level - 1], old[level]
    frac = min(max(exp - lo, 0), hi - lo - 1) / (hi - lo) if hi > lo else 0
    return base + int(frac * (new[level] - base))


def migrate_type_exp(st):
    """One-time: convert st's type EXP from the disc curves (DISC_TYPE_EXP) to the retail ones (AotI's
    type_rows; 0..9 are the same in both discs' tables), keeping every stored type level, even one above the
    retail cap. Marks st["type_curve"]; a marked state, or tables that are not the retail ones (retail_curve),
    are left alone. Returns [(cls, level, old exp, new exp)] for the types whose EXP changed (None: not run)."""
    if st.get("type_curve") == TYPE_CURVE or "type_level" not in st or not retail_curve():
        return None
    changed = []
    for c in range(16):
        new = [r[0] for r in type_rows(c, games.AOTI)]
        old = DISC_TYPE_EXP[c]
        tl, exp = st["type_level"][c], st["type_exp"][c]
        if tl < 1:
            tl = level_for_exp(old, exp)
        x = convert_exp(old, new, tl, exp)
        if x != exp:
            changed.append((c, tl, exp, x))
        st["type_level"][c], st["type_exp"][c] = tl, x
    st["type_curve"] = TYPE_CURVE
    return changed


def migrate_db(db, state_dir, log=print):
    """migrate_type_exp for every saved character, once, in one IMMEDIATE transaction, after a copy of psu.db
    (<state>/backups/psu-pre-type-curve-<time>.db, SQLite backup API), with one audit line per converted
    character in <state>/account-audit.log (db.audit, op type_curve). Idempotent: marked states are skipped, so
    a restart (or the per-load check in game.char_state) converts nothing twice. Returns the count converted."""
    if not retail_curve():
        return 0
    todo = []
    for gid, slot, raw_state in db.c.execute("SELECT gid, slot, state FROM characters").fetchall():
        try:
            st = json.loads(raw_state or "{}")
        except ValueError:
            continue
        if "type_level" in st and st.get("type_curve") != TYPE_CURVE:
            todo.append((gid, slot))
    if not todo:
        return 0
    import sqlite3
    import time
    bdir = os.path.join(state_dir, "backups")
    os.makedirs(bdir, exist_ok=True)
    bpath = os.path.join(bdir, time.strftime("psu-pre-type-curve-%Y%m%d-%H%M%S.db"))
    dst = sqlite3.connect(bpath)
    with dst:
        db.c.backup(dst)
    dst.close()
    done = []
    db.c.execute("BEGIN IMMEDIATE")
    try:
        for gid, slot in todo:
            name, raw_state = db.c.execute("SELECT name, state FROM characters WHERE gid = ? AND slot = ?",
                                           (gid, slot)).fetchone()
            st = json.loads(raw_state or "{}")
            changed = migrate_type_exp(st)
            if changed is None:
                continue
            db.c.execute("UPDATE characters SET state = ? WHERE gid = ? AND slot = ?", (json.dumps(st), gid, slot))
            done.append((gid, slot, name, changed))
        db.c.execute("COMMIT")
    except BaseException:
        db.c.execute("ROLLBACK")
        raise
    for gid, slot, name, changed in done:
        db.audit("type_curve", db.username(gid) or "", gid, char=name, slot=slot, curve=TYPE_CURVE,
                 types=[{"cls": c, "level": tl, "exp": [a, b]} for c, tl, a, b in changed])
    log(f"type curve {TYPE_CURVE}: {len(done)} characters converted, backup {bpath}")
    return len(done)


def requirements(cls, game=None):
    return type_table(cls, game)["requirements"][cls]


def visible_class(cls, recipient_game=None):
    """The class byte to put in a record for a client of recipient_game (default: this session's disc).
    AotI clients see every class as is. A PSU client has names and rows for 0..9 only (its PlayerText #47..52
    say "no data" for 10..15 and its type rows there are placeholders), so an AotI-only class (10..15) is shown
    as the PSU type nearest in role: Acrofighter -> Hunter, Acrotecher -> Wartecher, Fighmaster -> Fortefighter,
    Gunmaster -> Fortegunner, Masterforce -> Fortetecher, Acromaster -> Fighgunner (PSU_STANDIN). Only the byte
    sent changes: stats, HP and type levels stay the real class's, and nothing is saved.
    Hook for the cross-game record builders (world/social/area: the class byte after race and sex in the
    character record, 0x1516 +0x6A); charload uses it for the player's own record."""
    if (recipient_game or games.current()) == games.AOTI or cls < CLASSES[games.PSU]:
        return cls
    return PSU_STANDIN.get(cls, 0)


def level_cap(game=None):
    """Highest level with a row in this disc's table (PSU 100, AotI 200; the client's own limit is 200,
    AotI 0x1f58a0)."""
    if (game or games.current()) == games.PSU:
        return LEVEL_CAP
    t = tables(game)
    return max((i + 1 for i, row in enumerate(t["levels"][0]) if row[1]), default=LEVEL_CAP)


def new_state(raw):
    """Level 1 in the type the client chose at creation (0x0D02 body +0x6A); a class this disc does not
    offer starts as Hunter."""
    cls = raw[0x6A] if raw[0x6A] < class_count() else 0
    st = {"cls": cls, "level": 1, "exp": 0, "meseta": 1000, "blast": 0, "luck": 3, "playtime": 0,
          "type_level": [1] * 16, "type_exp": [0] * 16}
    if retail_curve():
        st["type_curve"] = TYPE_CURVE
    st["hp"] = derived(raw, st)["max_hp"]
    return st


def exp_for_level(raw, level):
    """Cumulative EXP to reach level (client 0x1f4bd0, AotI 0x1f58a0)."""
    if level > level_cap():
        return 100_000_000
    row = tables()["levels"][raw[0x68] * 2 + raw[0x69]][level - 1]
    return 100_000_000 if (level >= 2 and row[0] == 0) else row[0]


def derived(raw, st):
    """max HP and the 8 stats for this character's level, type and type level."""
    t = tables()
    race, gender = raw[0x68], raw[0x69]
    lv = t["levels"][race * 2 + gender]
    row = lv[min(st["level"], len(lv)) - 1]
    rows = type_rows(st["cls"])
    trow = rows[min(st["type_level"][st["cls"]], len(rows)) - 1]
    hp_pct, pct, race_pct = trow[1], trow[2:10], trow[10:14]
    k = race_pct[race]
    max_hp = (row[1] & 0xFFFF) * hp_pct * k // 10000
    stats = [(s & 0xFFFF) * p * k // 10000 for s, p in zip(row[2:10], pct)]
    return {"max_hp": max_hp, "stats": stats}


_CLOCKS = {}                    # id(state) -> [state, monotonic time play time was last banked]
PLAYTIME_MAX = 0xFFFFFFFF


def start_clock(st, now=None):
    """Play time of st counts from now (the session selected this character)."""
    if st is not None:
        _CLOCKS[id(st)] = [st, time.monotonic() if now is None else now]


def tick_clock(st, now=None):
    """Bank the whole seconds since the last tick into st["playtime"]; a state no session plays is left alone."""
    c = _CLOCKS.get(id(st)) if st is not None else None
    if c is None or c[0] is not st:
        return
    now = time.monotonic() if now is None else now
    secs = int(now - c[1])
    if secs > 0:
        st["playtime"] = min(st.get("playtime", 0) + secs, PLAYTIME_MAX)
        c[1] += secs


def stop_clock(st, now=None):
    """Bank what is left and stop counting (logout, another character selected)."""
    tick_clock(st, now)
    c = _CLOCKS.get(id(st)) if st is not None else None
    if c is not None and c[0] is st:
        del _CLOCKS[id(st)]


def level_block(st, game=None):
    """game = the disc that reads it (default this session's): a PSU client gets type levels <= 10. Play time is
    brought up to date first (tick_clock)."""
    tick_clock(st)
    words = [(visible_type_level(st["type_level"][i], game) & 0xFFFF) | ((st["type_exp"][i] & 0xFFFF) << 16) for i in range(16)]
    return (struct.pack("<HHHb", st["level"], 0, st["blast"], st["luck"]) + bytes(5)
            + struct.pack("<I", st["exp"]) + bytes(4) + struct.pack("<II", st["meseta"], st.get("playtime", 0) & PLAYTIME_MAX) + bytes(20)
            + struct.pack("<16I", *words))


def status_block(raw, st, game=None):
    """The block for the disc that will read it: game = the recipient's (default: this session's)."""
    d = derived(raw, st)
    stats = struct.pack("<8H", *[min(s, 0xFFFF) for s in d["stats"]])
    hp = min(st.get("hp", d["max_hp"]), d["max_hp"])
    one = struct.pack("<f", 1.0)
    head = struct.pack("<III", exp_for_level(raw, st["level"] + 1), exp_for_level(raw, st["level"]), d["max_hp"]) + stats
    if (game or games.current()) == games.AOTI:
        # AotI reads EGS's (PC) layout: 12 zero, u32 level, stats, HP +0x3C, max HP +0x40, 168 zero, then the
        # floats 4 bytes earlier than PSU's. Sent the PSU way, AotI takes the end of the stats block
        # (+0x3C, 0x0007001C) as current HP and its HUD shows 9999 (clamped).
        out = (head + bytes(12) + struct.pack("<I", st["level"] & 0xFF) + stats + struct.pack("<II", hp, d["max_hp"])
               + bytes(168) + (one + bytes(8)) * 4 + one + bytes(20) + one + bytes(44))
    else:
        out = (head + bytes(16) + bytes([st["level"] & 0xFF, 0, 0, 0]) + stats
               + struct.pack("<II", hp, d["max_hp"]) + bytes(168) + (one + bytes(8)) * 5 + bytes(20) + one + bytes(32))
    assert len(out) == 0x164, hex(len(out))
    return out


def types_table(raw, st):
    """0x0113 payload: 16 entries of 0x30 (client copies 0x300 bytes); entries past this disc's classes are 0."""
    t = tables()
    out = b""
    for c in range(16):
        if c >= len(t["types"]) or c >= class_count():
            out += bytes(0x30)
            continue
        req = requirements(c)
        ok = can_change_type(st, c)
        probe = dict(st, cls=c)
        d = derived(raw, probe)
        rows = type_rows(c)
        tl = min(st["type_level"][c], len(rows))
        this_exp = rows[tl - 1][0]
        next_exp = rows[tl][0] if tl < min(len(rows), type_cap(c)) else 0
        out += (struct.pack("<I", 1 if ok else 0) + struct.pack("<16b", *req)
                + struct.pack("<II", exp_for_level(raw, st["level"]), d["max_hp"])
                + struct.pack("<8H", *[min(s, 0xFFFF) for s in d["stats"]]) + struct.pack("<HH", this_exp, next_exp))
    assert len(out) == 0x300
    return out


def can_change_type(st, cls):
    """0x0110 event 3: a class this session's disc offers, not the current one, requirements met."""
    if not 0 <= cls < class_count() or cls == st["cls"]:
        return False
    req = requirements(cls)
    return all(st["type_level"][i] >= r for i, r in enumerate(req) if r > 0)


def type_menu(st):
    """0x1A07 +0x38: 16 enabled bytes, the current type and every type it may change to."""
    return [1 if c < class_count() and (c == st["cls"] or can_change_type(st, c)) else 0 for c in range(16)]
