"""Local check for packets built about one player for another (psu/world.py, psu/social.py, psu/area.py).

PSU -> PSU output must not change with the per-recipient (PSU / AotI) layouts: the packets below are built from
fixed players and compared with digests of the single-disc output.
Then the AotI-recipient variants are checked structurally (status block layout, 0x0233 stride, Spaceport map).
Types (psu/progression.py): PSU's 0x0113 table, 0x1A07 mask, own character record and level cap keep their
bytes; with AotI's plylevel.json ($PSU_GAMEDATA/aoti/, else $PSU_WORK/aoti/gamedata or gamedata-staging) the
AotI classes 10..15 are checked too (offered to AotI only, shown to PSU as a stand-in, stats from AotI's rows).

  python tools/xgame_check.py            (needs plylevel.json: $PSU_GAMEDATA or $PSU_WORK/psu/gamedata)
  python tools/xgame_check.py --print    print the digests instead of checking them
  python tools/xgame_check.py --print-aoti   the same for the all-AotI scenario (AOTI_DIGESTS)

An AotI player shown to a PSU one (psu/xgame.py): AotI-only part words, voice, type and items are replaced or
withheld; with $PSU_GAMEDATA/parts.json (tools/partsdata.py) the part words are checked against PSU's set.

Economy: a PSU session's grind, drop, PA, synthesis and Probo results with a fixed RNG seed must match ECON_DIGESTS
(PSU's rules; --print-econ prints them); AotI's rules are checked with AotI's grind/probo/synthesis tables
($PSU_GAMEDATA/aoti/, else $PSU_WORK/aoti/gamedata or gamedata-staging).
"""
import hashlib
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("PSU_WORK") or os.path.join(HERE, "..", "..", "work")   # data built from the discs (docs/data.md)
sys.path.insert(0, os.path.join(HERE, "..", "server"))
os.environ.setdefault("PSU_GAMEDATA", os.path.join(WORK, "psu", "gamedata"))

from psu import charload, combat, games, progression, social, world, xgame  # noqa: E402
from psu.area import c010d_char as own_c010d  # noqa: E402


def raw_char(name, race, gender, cls):
    b = bytearray(0x6B + 97 + 8)
    b[0x28:0x68] = name.encode("utf-16-le").ljust(0x40, b"\0")
    b[0x68], b[0x69], b[0x6A] = race, gender, cls
    for i in range(97):
        b[0x6B + i] = (i * 7 + race) & 0xFF
    # real PSU part words (the race/gender's first creation choices): a pattern there is not a valid part, and
    # every record a PSU client reads now has invalid words replaced (charload.char_bin, psu/xgame.py)
    dflt = xgame.parts(games.PSU).get("defaults", [])
    if race * 2 + gender < len(dflt):
        struct.pack_into("<6I", b, 0x6B + xgame.PARTS, *dflt[race * 2 + gender])
    return bytes(b)


def player(gid, lid, game, name, race, cls, area, entry=0, level=12):
    games.set_current(game)
    p = world.Player(gid, raw_char(name, race, 1, cls), None, None)
    p.lid, p.area, p.entry, p.pos = lid, area, entry, (10.0, 0.0, -20.0, 90.0)
    st = progression.new_state(p.raw)
    st["level"] = level
    st["hp"] = 50
    p.state = st
    return p


def scenario(game_of):
    """Packets keyed by name, players' games from game_of (name -> game)."""
    lobby, port = (1100000, 0, 4), (1104000, 0, 900)
    a = player(0x1111, 0, game_of["a"], "Alpha", 0, 1, lobby)
    b = player(0x2222, 1, game_of["b"], "Bravo", 2, 3, lobby, entry=2)
    c = player(0x3333, 2, game_of["c"], "Charl", 3, 0, port if game_of["c"] == games.PSU else (1104000, 0, 901))
    party = social.Party(a)
    party.slots[1] = b
    out = {
        "0111": world.c0111_spawn(b, a), "010d": world.c010d_char(b, a), "0205": world.c0205_map(b, a),
        "0205port": world.c0205_map(a, c), "0203": world.c0203(b, a), "0201": world.c0201_char(b, a),
        "0201port": world.c0201_char(a, c), "0204": world.c0204_unspawn(b, a),
        "0233x1": world.c0233_players(b, [a]), "0233x2": world.c0233_players(c, [a, b]),
        "0304": world.c0304_chat(b, a, bytes(0x34) + bytes([0, 0, 0, 3, 0, 0, 0, 0]) + "hi\0".encode("utf-16-le")),
        "1004": social.c1004_member(b, a, 0), "1005": social.c1005_party(b, party), "100e": social.c100e_location(b, c, 1),
        "100a": social.c100a_invite(b, a), "1503": social.c1503_card(b, a),
    }
    games.set_current(game_of["a"])     # a's task builds b's own 0x010D (level-up relay, game.py)
    out["010d_own_other"] = own_c010d(b.gid, b.raw, b.area, b.entry, lid=b.lid, st=b.state, game=b.game) \
        if "game" in own_c010d.__code__.co_varnames else own_c010d(b.gid, b.raw, b.area, b.entry, lid=b.lid, st=b.state)
    return out, (a, b, c)


# sha256[:16] of the PSU -> PSU packets; per-recipient layouts must leave them as they are.
PSU_DIGESTS = {
    "0111": "13024f4619427b46",
    "010d": "3074a2d085b4e0e6",
    "0205": "82f653fc6bbc83bd",
    "0205port": "7028b34fd32e7e8e",
    "0203": "f8808521d78fe175",
    "0201": "963f9a2f6d8ff108",
    "0201port": "84978f7a3a5a41a9",
    "0204": "6076b84b81bc9b42",
    "0233x1": "0902c71b2aaee2c8",
    "0233x2": "82d0420bcd383cb8",       # records 0x2D0 apart, as PSU reads them
    "0304": "52a014693d4b2852",
    "1004": "3945cd3c824a511a",
    "1005": "b09a39713caec298",
    "100e": "0f4a4bb0c49603d1",
    "100a": "cbbc5cd9e9c35f40",
    "1503": "faaddea1e9bd6ad6",
    "010d_own_other": "1563a8ab53d5bc78",
}
# types: PSU's type packets; AotI's classes must leave them as they are
TYPE_DIGESTS = {
    "0113": "b38da04cb5be51ea",
    "1a07": "3b0e4842b28d6b39",
    "charbin": "038c15b1170964f1",
    "levelcap": "bf34786738bf96ec",
}


def type_packets():
    """PSU session: a Ranger with Hunter 5 / Ranger 5 / Force 3 (Fighgunner and Guntecher open)."""
    games.set_current(games.PSU)
    raw = raw_char("Types", 1, 0, 1)
    st = progression.new_state(raw)
    st["type_level"][:3] = [5, 5, 3]
    st["type_exp"][:3] = [1000, 1000, 500]
    st["level"] = 30
    menu = progression.type_menu(st) if hasattr(progression, "type_menu") else \
        [1 if c < 10 and (c == st["cls"] or progression.can_change_type(st, c)) else 0 for c in range(16)]
    p = world.Player(0x4444, raw, None, None)
    p.state = dict(st, level=99, exp=0)
    combat.add_exp(p, 10 ** 9)
    return {"0113": progression.types_table(raw, st), "1a07": bytes(menu), "charbin": charload.char_bin(raw, st),
            "levelcap": struct.pack("<II", p.state["level"], p.state["exp"])}


def aoti_types():
    """Structural checks of AotI's classes; None when no AotI plylevel.json is found."""
    own = os.path.join(os.environ["PSU_GAMEDATA"], "aoti", "plylevel.json")
    for path in (own, os.path.join(WORK, "aoti", "gamedata", "plylevel.json"),
                 os.path.join(WORK, "aoti", "gamedata-staging", "plylevel.json")):
        if os.path.exists(path):
            break
    else:
        return None
    import json
    with open(path, encoding="utf-8") as f:
        games._JSON[(games.AOTI, "plylevel.json")] = json.load(f)
    raw = raw_char("Acro", 0, 0, 0)
    games.set_current(games.AOTI)
    st = progression.new_state(raw)
    fresh = progression.can_change_type(st, 10)       # False with the published requirements (Hunter 3 + Ranger 5)
    st["type_level"][0], st["type_level"][1] = 3, 5
    ok = [progression.class_count() == 15, not progression.can_change_type(st, 15), progression.can_change_type(st, 10),
          progression.type_menu(st)[10] == 1, progression.types_table(raw, st)[10 * 0x30:11 * 0x30] != bytes(0x30),
          progression.level_cap() == 200]
    st["cls"], st["type_level"][12] = 12, 25
    aoti_stats = progression.derived(raw, st)
    ok += [charload.char_bin(raw, st)[0x42] == 12, charload.char_bin(raw, st, games.PSU)[0x42] == 6]
    games.set_current(games.PSU)          # the same character from a PSU session
    ok += [progression.visible_class(12) == 6, charload.char_bin(raw, st)[0x42] == 6,
           not progression.can_change_type(dict(st, cls=0), 12), progression.type_menu(st)[12] == 0,
           progression.types_table(raw, st)[10 * 0x30:] == bytes(6 * 0x30),
           progression.type_rows(12) == games.json_data("plylevel.json", games.AOTI)["types"][12],
           progression.derived(raw, st)["max_hp"] > 0 and aoti_stats["max_hp"] > 0]
    return ok


# the same scenario with every player on AotI; psu/xgame.py must leave it as it is
AOTI_DIGESTS = {
    "0111": "13024f4619427b46",
    "010d": "639684f4d2dcd21c",
    "0205": "82f653fc6bbc83bd",
    "0205port": "5be934303deb2a1d",
    "0203": "f8808521d78fe175",
    "0201": "64c16c8afd400786",
    "0201port": "3508b607728123cd",
    "0204": "6076b84b81bc9b42",
    "0233x1": "a2d59d60e837834b",
    "0233x2": "cfb91f03095fd26a",
    "0304": "52a014693d4b2852",
    "1004": "3945cd3c824a511a",
    "1005": "b09a39713caec298",
    "100e": "f696524e60aff327",
    "100a": "cbbc5cd9e9c35f40",
    "1503": "faaddea1e9bd6ad6",
    "010d_own_other": "790d0b945eb12476",
}


def digest(b):
    return hashlib.sha256(b).hexdigest()[:16]


AOTI_HAIR, AOTI_UPPER, AOTI_VOICE, AOTI_CLASS = 0x0513A900, 0x00001100, 90, 12   # f_hair_93700, m_upper_01700


def aoti_char(name):
    """A cast male (race 2) in AotI-only clothes, hair, voice and type Fighmaster (12)."""
    b = bytearray(raw_char(name, 2, 0, AOTI_CLASS))
    a = 0x6B
    b[a], b[a + 1] = AOTI_VOICE, 0x7F
    struct.pack_into("<6I", b, a + 5, AOTI_UPPER, 0x0101F400, 0x0201F400, 0x03001E00, 0x04000400, AOTI_HAIR)
    b[a + 0x1D:a + 0x20] = bytes([3, 3, 3])
    return bytes(b)


def xgame_checks():
    """An AotI player (x) seen by a PSU player (y) and by an AotI player (z)."""
    from psu import charload, drops, xgame
    lobby = (1100000, 0, 4)
    x = player(0x4444, 3, games.AOTI, "Xeno", 2, 0, lobby)
    x.raw = aoti_char("Xeno")
    try:                                              # class 12's rows are AotI's plylevel.json (aoti_types)
        progression.type_rows(AOTI_CLASS)
        x.state = dict(x.state, cls=AOTI_CLASS)
    except FileNotFoundError:
        print("note: no AotI plylevel.json, the AotI-class record is not checked")
    y = player(0x5555, 4, games.PSU, "Yuri", 0, 1, lobby)
    z = player(0x6666, 5, games.AOTI, "Zed", 1, 2, lobby)
    games.set_current(games.AOTI)
    own = charload.char_bin(x.raw, x.state)
    ok = {}

    def cb_of(rec):                                   # char_bin inside a 712-byte user record
        return rec[80:80 + 276]
    to_psu = cb_of(x.user(games.PSU))
    to_aoti = cb_of(x.user(games.AOTI))
    ok["aoti->aoti unchanged"] = to_aoti == own
    ok["psu record class"] = to_psu[0x42] == progression.visible_class(x.state["cls"], games.PSU) < 10
    ok["psu record rest"] = to_psu[:0x43] == own[:0x42] + to_psu[0x42:0x43] and to_psu[0x43 + 97:] == own[0x43 + 97:]
    ok["psu record voice"] = 27 <= to_psu[0x43] < 39
    words = struct.unpack_from("<6I", to_psu, 0x43 + 5)
    psu_parts = xgame.part_set(games.PSU)
    if psu_parts is not None:
        ok["psu record parts"] = all(w in psu_parts for w in words)
        ok["psu record kept PSU parts"] = words[1:3] == (0x0101F400, 0x0201F400)
        ok["psu record colour reset"] = to_psu[0x43 + 0x1D] == 0 and to_psu[0x43 + 0x1E] == 3
    else:
        print("note: no parts.json in $PSU_GAMEDATA, part words not checked")
    # every packet that carries x's record to y
    rec_0201 = world.c0201_char(y, x)[8 + 0x24:8 + 0x24 + 712]
    ok["0x0201"] = cb_of(rec_0201) == to_psu
    ok["0x010d"] = world.c010d_char(y, x)[8 + 0x60:8 + 0x60 + 276] == to_psu
    ok["0x0233"] = cb_of(world.c0233_players(y, [x])[8 + 0x28:]) == to_psu
    ok["0x0201 to aoti"] = cb_of(world.c0201_char(z, x)[8 + 0x24:]) == own
    card = social.c1503_card(y, x)[8 + 0x24:]
    ok["0x1503 class/voice"] = card[0x42] == progression.visible_class(AOTI_CLASS, games.PSU) and 27 <= card[0x43] < 39
    ok["0x1501 stored card"] = xgame.card_for(social.card_record(x), games.PSU)[0x42:0x44] == card[0x42:0x44]
    ok["0x1503 to aoti"] = social.c1503_card(z, x)[8 + 0x24:] == social.card_record(x)
    ok["card_for psu record"] = xgame.card_for(social.card_record(y), games.PSU) == social.card_record(y)
    # items: an id PSU's items.json lacks is withheld from a PSU recipient, shown to AotI
    ok["item_ok"] = (not xgame.item_ok("7f7f7f00", games.PSU) and xgame.item_ok("7f7f7f00", games.AOTI)
                     and xgame.item_ok("03010000", games.PSU) and not xgame.item_ok("09160100", games.PSU))
    zone = ("xgame-check",)
    drops.ground[zone] = {1: {"uuid": 1, "id": "03010000", "qty": 1, "map": 0, "pos": bytes(12), "meseta": False},
                          2: {"uuid": 2, "id": "7f7f7f00", "qty": 1, "map": 0, "pos": bytes(12), "meseta": False}}
    games.set_current(games.PSU)
    n_psu = struct.unpack_from("<I", drops.c0a03(y, zone)[8:], 0x24)[0]
    drops.ground[zone].pop(2)
    ok["0x0a03 withheld"] = n_psu == 1
    drops.clear(zone)
    ok.update(item_value_checks(x, y, z))
    bad = [k for k, v in ok.items() if not v]
    return len(ok), bad


def item_value_checks(x, y, z):
    """AotI-only item values (PA level > Lv30, grind-failure nibble) never reach a PSU client; stored state and
    AotI recipients keep them; PSU-made items read the same on both discs."""
    from psu import growth, items, social
    games.set_current(games.AOTI)
    c = bytes.fromhex(items.db()["01010000"]["consts"])
    rod = next(k for k, v in sorted(items.db().items())
               if v["cat"] == 1 and bytes.fromhex(v["consts"])[0xE] == 3 and k[2:4] == "18")
    w = {"uuid": 0x801, "id": "01010000", "qty": 1, "active": 0, "slots": 0, "grind": 0, "pa": [1, 40], "fails": 3}
    r = {"uuid": 0x802, "id": rod, "qty": 1, "active": 0, "slots": 0, "grind": 0, "pa": [0, 0],
         "techs": [[1, 45], [12, 3], [0, 0], [0, 0]]}
    pw = {"uuid": 0x803, "id": "01010000", "qty": 1, "active": 0, "slots": 0, "grind": 0, "pa": [1, 29]}
    st = {"inv": [w, r, pw], "pas": [[9000, "04010128"], [7000, "0601002d"], [10, "06020103"]]}
    before = repr(st)
    ok = {}
    for game in (games.AOTI, games.PSU):          # the record a client of `game` reads, from either session
        for sess in (games.AOTI, games.PSU):
            games.set_current(sess)
            vw, vr = items.var_rec(w, game), items.var_rec(r, game)
            if game == games.PSU:
                ok[f"psu record from {sess}"] = vw[0x1E] == 29 and vw[0x1B] == c[0xD] and \
                    vr[0x1C:0x20] == bytes([1, 29, 12, 3])
            else:
                ok[f"aoti record from {sess}"] = vw[0x1E] == 40 and vw[0x1B] == (c[0xD] + 0x30) & 0xFF and \
                    vr[0x1C:0x20] == bytes([1, 45, 12, 3])
            ok[f"psu-made record {game} from {sess}"] = items.var_rec(pw, game) == items.var_rec(pw, games.PSU)
    # its own 0x0A0A: an AotI-made character loaded on the PSU disc, and on AotI
    games.set_current(games.PSU)
    own = items.c0a0a_state(x.gid, st)
    pa_ids = [own[0x50 + 8 * i + 4:0x50 + 8 * i + 8].hex() for i in range(3)]
    recs = [own[0x170 + 0x24 * i:0x170 + 0x24 * (i + 1)] for i in range(3)]
    ok["0x0a0a on psu: PA list"] = pa_ids == ["0401011d", "0601001d", "06020103"]
    ok["0x0a0a on psu: records"] = recs == [items.var_rec(s, games.PSU) for s in st["inv"]] and recs[0][0x1E] == 29
    games.set_current(games.AOTI)
    own = items.c0a0a_state(x.gid, st)
    ok["0x0a0a on aoti unchanged"] = [own[0x50 + 8 * i + 4:0x50 + 8 * i + 8].hex() for i in range(3)] == \
        [e[1] for e in st["pas"]] and own[0x170 + 0x1E] == 40
    # trade: the partner's 0x1904 record and the completion 0x0106, built in the AotI giver's session
    rec_psu, rec_aoti = social.trade_record(w, 1, games.PSU), social.trade_record(w, 1, games.AOTI)
    ok["0x1904 record to psu"] = rec_psu[0x16] == 29 and rec_psu[0x13] == c[0xD]
    ok["0x1904 record to aoti"] = rec_aoti[0x16] == 40 and rec_aoti[0x13] == (c[0xD] + 0x30) & 0xFF
    add = items.c0106_add(y.gid, y.lid, w, game=y.game)[8:]
    ok["0x0106 trade add to psu"] = add[0x34 + 0x1A] == 29 and add[0x34 + 0x17] == c[0xD]
    ok["0x0106 trade add to aoti"] = items.c0106_add(z.gid, z.lid, w, game=z.game)[8 + 0x34 + 0x1A] == 40
    # 0x0108 about x's PA at Lv41 relayed to the zone
    to_psu = growth.c0108(y, x, [9001, "04010128"], True)[8:]
    to_aoti = growth.c0108(z, x, [9001, "04010128"], True)[8:]
    ok["0x0108 to psu"] = to_psu[0x2C] == 0 and to_psu[0x34:0x38].hex() == "0401011d"
    ok["0x0108 to aoti"] = to_aoti[0x2C] == 1 and to_aoti[0x34:0x38].hex() == "04010128"
    ok["0x0108 psu-made to psu"] = growth.c0108(y, x, [5, "0401011d"], True)[8 + 0x2C] == 1
    ok["stored state unchanged"] = repr(st) == before
    games.set_current(games.PSU)
    return ok


# --- economy: grinding, drops, PA growth, synthesis, Probo ---------------------------------------------------

def econ_scenario():
    """PSU session, fixed RNG seed: grind quotes/commits/replies, enemy/box drop rolls, PA hits, synthesis rates,
    rolls and board rows, Probo gifts. Returns {part: bytes}."""
    import random
    import types
    from psu import drops, econ, growth, items, synthesis
    games.set_current(games.PSU)
    random.seed(20261001)
    db = items.db()
    out = {}

    def slot(iid, uuid, qty=1):
        return {"uuid": uuid, "id": iid, "qty": qty, "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]}
    g = econ.data("grind")
    wids = [w for w in sorted(g.get("weapons", {})) if w in db][::7][:24]
    gids = sorted(g.get("grinders", {}))
    b = []
    for n, wid in enumerate(wids):
        weapon, grinder = slot(wid, 0x100 + n), slot(gids[n % len(gids)] if gids else "0f010000", 0x200 + n, 99)
        st = {"inv": [grinder, weapon]}
        for _ in range(12):
            q = econ.grind_chance(grinder, weapon) if grinder["id"] in g.get("grinders", {}) else None
            b.append(econ.c0a09_quote(1, 0, bytes(0x24) + bytes([1, 0]), q))
            gr, wp, ok, gi, err = econ.grind(st, grinder["uuid"], 1)
            b.append(repr((ok, gi, err, weapon.get("grind"), weapon.get("pp_bonus"), weapon.get("atp_bonus"))).encode())
            if wp is not None:
                b.append(econ.c0106_grind(1, 0, gi, 1, grinder["uuid"], wp, ok))
    out["grind"] = b"".join(b)
    t = drops.tables() or {}
    b = []
    for var in sorted(t.get("enemy", {}), key=int):
        for level in (1, 5, 12, 33, 61, 99, 130, 210):
            for area in (6, 9, 14, 21, 25):
                for _ in range(4):
                    b.append(repr(drops.roll_enemy(int(var), level, area)).encode())
    for dtype in range(5):
        for idx in range(6):
            for level in (1, 20, 50, 101, 150):
                for _ in range(6):
                    b.append(repr(drops.roll_object(dtype, idx, level, 6 + (idx * 3 + dtype) % 16)).encode())
    for q, zs in sorted(t.get("zones", {}).items())[:40]:
        for z in sorted(zs):
            b.append(repr(drops.zone_area(int(q), int(z))).encode())
    out["drops"] = b"".join(b)
    b = []
    w = slot("01010000", 0x300)
    w["pa"] = [1, 0]
    st = {"inv": [w], "pas": [[0, "04010100"], [2200, "04010200"]]}
    for i in range(2400):
        b.append(repr(growth.pa_hit(st, 0x300 | (1 << 29))).encode())
    st["pas"][0][1] = "04010105"
    b.append(repr(growth.link_level(st, w, 1)).encode())
    out["pa"] = b"".join(b)
    sy = synthesis.data().get("boards", {})
    d = types.SimpleNamespace(gid=0x7777, lid=1)
    b = []
    for n, bid in enumerate(sorted(sy)[::9][:60]):
        bd = sy[bid]
        st = {"probo": {"type": 0, "stats": [n * 37 % 900, n * 53 % 900, n * 71 % 900, n * 13 % 900, 0]},
              "room_stock": [slot(m["id"], 0x500 + k, 5) for k, m in enumerate(bd["materials"][0][:2])
                             if m["id"] in db], "inv": []}
        e = {"board": bid, "running": 0, "fail": 0, "fail_id": -1, "used": 0, "variant": n % 3 - 1,
             "slots": synthesis.recipe(bd, 0), "result": None}
        synthesis.entries(st)[0] = e
        for v in range(8):
            b.append(repr(synthesis.rate(st, e, bd, v)).encode())
        for _ in range(3):
            e["result"] = None
            b.append(repr(synthesis.roll(st, e, bd)).encode())
        b += [p for p, _ in synthesis.entry_rows(d, st, 0)]
    out["synthesis"] = b"".join(b)
    b = []
    st = {}
    gifts = [i for i in sorted(db) if bytes.fromhex(i)[0] in (1, 2, 3, 5, 9, 11, 14, 15, 16)][::23][:120]
    for n, iid in enumerate(gifts):
        r = econ.probo_gift(st, slot(iid, 0x600 + n))
        b.append(repr(r).encode() + econ.c1312(1, 0, *r))
    out["probo"] = b"".join(b)
    return out


# sha256[:16] of econ_scenario(): PSU's economy rules, which AotI's must leave as they are
ECON_DIGESTS = {
    "grind": "36d5bc021dc38b52",        # a wand's +0x1C type byte (0x19) is not a tech slot
    "drops": "ae9c41627c911b66",
    "pa": "c0e75b29d79ed79e",           # link_level takes a PA list entry index, not a PA index
    "synthesis": "8d38420ddb260758",    # materials stack (items.stackable, client 0xafb70), so stock counts
                                        # sum quantities and material records carry max 99
    "probo": "219fed1199ab32d9",
}


def aoti_econ():
    """Structural checks of AotI's rules with AotI's grind/probo/synthesis tables ($PSU_GAMEDATA/aoti/, else
    $PSU_WORK/aoti/gamedata or gamedata-staging); None when they are not found."""
    import json
    from psu import drops, econ, growth, items, synthesis
    roots = [os.path.join(os.environ["PSU_GAMEDATA"], "aoti")] + [os.path.join(WORK, "aoti", d)
                                                                     for d in ("gamedata", "gamedata-staging")]
    for name in ("grind.json", "probo.json", "synthesis.json", "pa.json", "drops.json", "items.json"):
        path = next((os.path.join(r, name) for r in roots if os.path.exists(os.path.join(r, name))), None)
        if path is None:
            return None
        with open(path, encoding="utf-8") as f:
            games._JSON[(games.AOTI, name)] = json.load(f)
    games.set_current(games.AOTI)
    ok = {}
    db = items.db()
    gw = econ.data("grind")["weapons"]
    wid = next(w for w, v in sorted(gw.items()) if w in db and v.get("refine") and v["max_grind"] >= 3)
    w = {"uuid": 9, "id": wid, "qty": 1, "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]}
    s = gw[wid]["refine"]
    f = econ._f32(s["mult"][db[wid].get("rarity0", 0)])
    ok["grind success"] = econ.refine(w, 5000, rnd=lambda n: 0) and w["grind"] == 1 and \
        w["pp_bonus"] == s["pp"] + max(1, int(econ._rz(f))) and w["atp_bonus"] == s["atp"] + max(1, int(econ._rz(f)))
    p1 = w["pp_bonus"]
    econ.refine(w, 5000, rnd=lambda n: 0 if n == 10000 else n - 1)        # commit roll, then the top PP/ATP rolls
    ok["grind success max roll"] = w["grind"] == 2 and \
        w["pp_bonus"] == p1 + s["pp"] + max(1, int(econ._rz(f * econ._f32(float(s["rand"])))))
    ok["grind failure resets"] = not econ.refine(w, 5000, rnd=lambda n: 5001) and w["grind"] == 0 and \
        w["pp_bonus"] == 0 and w["atp_bonus"] == 0 and w["fails"] == 1
    c = bytes.fromhex(db[wid]["consts"])
    ok["failure count in the record"] = items.var_rec(w)[0x1B] == (c[0xD] + 0x10) & 0xFF
    rank = gw[wid]["rank"]
    gid = next(k for k, v in sorted(econ.data("grind")["grinders"].items()) if v["rank"] >= rank)
    gr = {"uuid": 8, "id": gid, "qty": 1, "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]}
    top = min(10, gw[wid]["max_grind"])
    ok["quote stops at max_grind - failures"] = econ.grind_chance(gr, dict(w, grind=top - 1)) is None and \
        econ.grind_chance(gr, dict(w, grind=top - 2)) is not None
    ok["grind reply carries the reset record"] = econ.c0106_grind(1, 0, 0, 1, 8, w, False)[8 + 0x34:] == \
        items.var_rec(w)[4:]
    games.set_current(games.PSU)
    ok["PSU record ignores failures"] = items.var_rec(dict(w, id="01010000"))[0x1B] == \
        bytes.fromhex(items.db()["01010000"]["consts"])[0xD]
    ok["PSU PA cap"] = growth.cap_byte() == 29
    games.set_current(games.AOTI)
    ok["AotI PA cap"] = growth.cap_byte() == 49 and growth.cap_byte(growth.thresholds()["050e0100"]["thr"]) == 49
    pw = {"uuid": 0x301, "id": "01010000", "qty": 1, "active": 0, "slots": 0, "grind": 0, "pa": [1, 40]}
    st = {"inv": [pw], "pas": [[10 ** 6, "04010128"]]}
    r = growth.pa_hit(st, 0x301 | (1 << 29))
    ok["PA grows past 30"] = r is not None and r[1] and st["pas"][0][1] == "04010129" and pw["pa"] == [1, 0x29]
    st["pas"][0][1] = "04010131"
    ok["PA stops at 50"] = growth.pa_hit(st, 0x301 | (1 << 29)) is None
    t = drops.tables()
    var = next(v for v, e in sorted(t["enemy"].items(), key=lambda x: int(x[0]))
               if e["meseta"][1] > e["meseta"][0] and e["t"][0] < e["t"][1])
    seq = iter([t["enemy"][var]["t"][0] + 1, 7])
    lo = t["enemy"][var]["meseta"][0]
    ok["meseta m + m*level/5"] = drops.roll_enemy(int(var), 33, None, rnd=lambda n: next(seq)) == \
        ("meseta", lo + 7 + (lo + 7) * 33 // 5)
    var = next(v for v, e in sorted(t["enemy"].items(), key=lambda x: int(x[0]))
               if e["t"][2] < 50000 and e["rare_bands"][4][1] != "00000000" and e["rare_bands"][4][0] < 49999)
    seq = iter([49999, 49999])
    ok["rare by half band, level > 124 clamped"] = drops.roll_enemy(int(var), 300, None, rnd=lambda n: next(seq)) == \
        ("item", t["enemy"][var]["rare_bands"][4][1])
    sb = synthesis.data()["boards"]
    bid, bd = next((k, v) for k, v in sorted(sb.items()) if 2 <= v["outputs"][0]["rate"] < 100 and v["level"] >= 3)
    st = {"probo": {"type": 0, "stats": [0, 0, 0, 0, 0]}}
    e = {"board": bid, "running": 0, "fail": 0, "fail_id": -1, "used": 0, "variant": 0, "slots": [], "result": None}
    r0 = synthesis.rate(st, e, bd, 0)
    st["probo"]["stats"] = [5000, 5000, 5000, 5000, 0]
    ok["synthesis rate from the output's Probo stat"] = 0 <= r0 < synthesis.rate(st, e, bd, 0) <= 100
    res = [synthesis.roll(st, dict(e, result=None), bd) for _ in range(200)]
    ok["synthesis roll"] = all(x["id"] for x in res) and any(not x["fail"] for x in res)
    dev, need = econ.data("probo")["device"], econ.data("probo")["device_need"]
    idx, typ = next((i, t) for i in range(len(dev)) for t in range(len(dev[i])) if dev[i][t] >= 0 and need[i] == 0)
    gift = {"uuid": 1, "id": f"0e01{idx:02x}00", "qty": 1, "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]}
    ok["PM device"] = econ.probo_gift({"probo": {"type": typ, "stats": [0] * 5}}, gift)[:2] == (True, dev[idx][typ])
    hi = next(i for i in range(len(need)) if need[i] > 0 and any(x >= 0 for x in dev[i]))
    gift["id"] = f"0e01{hi:02x}00"
    ok["PM device needs stat 4"] = not econ.probo_gift(
        {"probo": {"type": next(t for t, x in enumerate(dev[hi]) if x >= 0), "stats": [0] * 5}}, gift)[0]
    games.set_current(games.PSU)
    return ok


def tech_checks():
    """Technique PAs on both discs: link into a rod's tech slot (0x0105 event 3), the record's tech slots, growth
    per landed hit, the rod following a level-up, the disc's cap (PSU Lv30, AotI Lv50 when AotI's pa.json loads)."""
    from psu import growth, items
    ok = {}
    for game, top in ((games.PSU, 29), (games.AOTI, 49)):
        games.set_current(game)
        if game == games.AOTI and growth.cap_byte() != 49:
            print("note: no AotI pa.json, AotI techniques not checked")
            continue
        rod = next(k for k, v in sorted(items.db().items())
                   if v["cat"] == 1 and bytes.fromhex(v["consts"])[0xE] == 3 and k[2:4] == "18")
        w = {"uuid": 0x700, "id": rod, "qty": 1, "active": 1, "slots": 1, "grind": 0, "pa": [0, 0]}
        thr = growth.thresholds()["06010000"]["thr"]
        st = {"inv": [w], "pas": [[0, "04090600"], [thr[0] - 1, "06010000"], [0, "06020100"]]}
        req = bytes(0x24) + struct.pack("<IIBBBBI", 1, 0, 0, 3, 1, 2, 0x700)       # list entry 1 -> tech slot 2
        items.apply_event(st, req)
        items.apply_event(st, req[:0x2E] + bytes([2, 0]) + req[0x30:])              # list entry 2 -> slot 0
        r = items.var_rec(w)
        ok[f"{game} rod tech slots"] = r[0x1C:0x24] == bytes([12, 0, 0, 0, 1, 0, 0, 0])
        ok[f"{game} wrong slot no growth"] = growth.pa_hit(st, 0x700 | (1 << 29)) is None
        g = growth.pa_hit(st, 0x700 | (2 << 29))
        ok[f"{game} tech level-up"] = g is not None and g[1] and st["pas"][1] == [thr[0], "06010001"] and             w["techs"][2] == [1, 1] and items.var_rec(w)[0x21] == 1
        ok[f"{game} tech 0x0108"] = growth.c0108(types_ns(), types_ns(), g[0], g[1])[8 + 0x2C:] ==             bytes([1, 0, 0, 0]) + struct.pack("<I", thr[0]) + bytes.fromhex("06010001") if g else False
        g = growth.pa_hit(st, 0x700)
        ok[f"{game} slot 0 grows"] = g is not None and not g[1] and st["pas"][2][0] == 1
        st["pas"][1][1] = f"060100{top:02x}"
        ok[f"{game} tech cap"] = growth.pa_hit(st, 0x700 | (2 << 29)) is None
        items.apply_event(st, bytes(0x24) + struct.pack("<IIBBBBI", 1, 0, 0, 4, 0, 2, 0x700))
        ok[f"{game} unlink"] = items.var_rec(w)[0x20:0x22] == bytes(2)
    games.set_current(games.PSU)
    return ok


def learn_checks():
    """PA disks on both discs (C>S 0x0105 event 7, psu/growth.py learn_disk): append while the list has room, the
    reply's position / category / param entry / AotI id, already-known and full-list refusals, replacing the
    entry the player picked when full, the disk leaving the inventory, linking the new technique afterwards."""
    from psu import growth, items
    ok = {}
    for game in (games.PSU, games.AOTI):
        games.set_current(game)
        rod = next(k for k, v in sorted(items.db().items())
                   if v["cat"] == 1 and bytes.fromhex(v["consts"])[0xE] == 3 and k[2:4] == "18")
        w = {"uuid": 0x700, "id": rod, "qty": 1, "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]}
        disk = lambda u, iid: {"uuid": u, "id": iid, "qty": 1, "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]}
        st = {"inv": [w, disk(0x701, "06010000"), disk(0x702, "05010000"), disk(0x703, "06010000"),
                      disk(0x704, "01010000")], "pas": [[0, "04090600"]]}
        req = lambda i, u, pos=0x24: bytes(0x24) + struct.pack("<IIBBBBI", 1, 0, i, 7, pos, 0, u)
        pos, e, cat, _n, err = growth.learn_disk(st, req(1, 0x701))
        r = growth.c0105_learn(1, 0, req(1, 0x701), pos, cat, e)[8:] if not err else b""
        ok[f"{game} learn Foie"] = (pos, e, cat, err) == (1, [0, "06010000"], 6, None) and \
            [x["uuid"] for x in st["inv"]] == [0x700, 0x702, 0x703, 0x704]
        ok[f"{game} learn reply"] = r[0x2C:0x34] == bytes([1, 7, 1, 6]) + struct.pack("<I", 0x701) and \
            r[0x34:0x64] == items.pa_param(0, "06010000") and r[0x64:0x6C] == bytes(4) + bytes.fromhex("06010000")
        pos, e, cat, _n, err = growth.learn_disk(st, req(1, 0x702))
        ok[f"{game} bullet disk PA"] = e == [0, "050e0100"] and pos == 2 and cat == 5
        ok[f"{game} known refused"] = growth.learn_disk(st, req(1, 0x703))[4] == "already learned" and \
            len(st["inv"]) == 3 and len(st["pas"]) == 3
        ok[f"{game} not a disk"] = growth.learn_disk(st, req(2, 0x704))[4] == "not a disk"
        items.apply_event(st, bytes(0x24) + struct.pack("<IIBBBBI", 1, 0, 0, 3, 1, 0, 0x700))
        ok[f"{game} link learned tech"] = w.get("techs", [[0, 0]])[0] == [1, 0] and items.var_rec(w)[0x1C] == 1
        st["pas"] += [[0, f"0409{i:02x}00"] for i in range(33)]
        st["inv"].append(disk(0x705, "06020000"))
        ok[f"{game} full list refused"] = growth.learn_disk(st, req(3, 0x705))[4] == "PA list full"
        pos, e, _c, _n, err = growth.learn_disk(st, req(3, 0x705, pos=3))
        ok[f"{game} full list replaces the pick"] = err is None and pos == 3 and st["pas"][3] == [0, "06020000"] \
            and len(st["pas"]) == 36
    games.set_current(games.PSU)
    mayalee = next((k for k, v in items_db_psu().items() if v["cat"] == 5 and k[4:6] == "07"), None)
    if mayalee:
        st = {"inv": [{"uuid": 1, "id": mayalee, "qty": 1, "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]}]}
        ok["psu disk without a PSU PA refused"] = growth.learn_disk(
            st, bytes(0x24) + struct.pack("<IIBBBBI", 1, 0, 0, 7, 0x24, 0, 1))[4] is not None and len(st["inv"]) == 1
    return ok


def pp_checks():
    """Weapon PP on both discs (psu/combat.py pp_attack / pp_regen, psu/items.py): what each attack costs as the
    room charges it (0x295810), the left-hand mode, plain swings free, Photon Charge, regeneration, and the S>C
    0x0105 event 0x0A set packet / var_rec PP fields."""
    from psu import combat, items
    ok = {}
    for game in (games.PSU, games.AOTI):
        games.set_current(game)
        pa = items.paparam()["pa"]
        p = player(0x3333, 2, game, "Charlie", 0, 0, (1100000, 0, 4))
        saber = _slot(0x800, "01090000", active=1, pa=[6, 0])        # Rising Strike linked, Lv1
        gun = _slot(0x801, "01140000", active=1)                      # left hand, default bullet
        rod = _slot(0x802, "01180201", techs=[[1, 5], [41, 9], [6, 15], [0, 0]])
        charge = _slot(0x803, "03020000", 2)
        p.state["inv"] = [saber, gun, rod, charge]
        mx = items.max_pp(saber)
        rs = bytes.fromhex(pa["04090600"]["base"][0])
        bd = bytes.fromhex(items.paparam()["bullet_default"][0x14 - 0x0E])[4:]
        ok[f"{game} plain swing free"] = combat.pp_attack(p, 2, 0x11, False) is None
        ok[f"{game} striking PA step 0"] = combat.pp_attack(p, 3, 0x23, False) == (saber, rs[4])
        ok[f"{game} striking PA step 9 free"] = combat.pp_attack(p, 3, 0xB3, False) == (saber, 0)
        ok[f"{game} left-hand shot"] = combat.pp_attack(p, 2, 0, True) == (gun, bd[4])
        body = bytes(0x24) + bytes(8) + bytes([3, 0, 0, 0]) + struct.pack("<I", 0x23) + bytes(0x24)
        ok[f"{game} 0x0102 PA spends"] = combat.pp_cast(p, 0x0102, body) is saber and \
            items.cur_pp(saber) == mx - rs[4]
        r = items.c0105_pp(p.gid, p.lid, saber)
        ok[f"{game} 0x0105 0x0A set"] = len(r) == 0x40 and r[4:6] == bytes([1, 5]) and r[6] == 3 and \
            struct.unpack_from("<IIBBBBIi", r, 0x2C) == (p.gid, p.lid, 0, 0x0A, 0, 8, 0x800, mx - rs[4])
        vr = items.var_rec(saber, game)
        ok[f"{game} var_rec PP / max"] = struct.unpack_from("<HH", vr, 0x10) == (mx - rs[4], mx)
        p.state["inv"] = [saber, gun, rod, charge]
        saber["active"], rod["active"] = 0, 1                         # a two-handed rod alone
        foie = bytes.fromhex(pa["06010000"]["base"][0])
        ok[f"{game} technique slot 0"] = combat.pp_attack(p, 2, 0, False) == (
            rod, struct.unpack_from("<h", foie, 0x26)[0])
        ok[f"{game} technique slot 2 in left mode"] = combat.pp_attack(p, 2, 0, True)[0] is rod
        saber["active"], rod["active"] = 1, 0
        items.set_pp(gun, 10)
        ch = items.pp_item(p.state, charge, 0x3C)                   # both hands: 100 % halved
        ok[f"{game} Photon Charge both hands"] = ch == [saber, gun] and items.cur_pp(saber) == mx and \
            items.cur_pp(gun) == min(items.max_pp(gun), 10 + items.max_pp(gun) * 50 // 100)
        items.set_pp(gun, 0)
        ok[f"{game} regen tick"] = combat.pp_regen(p.state) == [gun] and \
            items.cur_pp(gun) == combat.pp_tick_amount(gun) > 0
    games.set_current(games.PSU)
    return ok


def items_db_psu():
    from psu import items
    games.set_current(games.PSU)
    return items.db()


# --- combat results: hits that are not support techniques keep their bytes; support techniques per disc -------

# sha256[:16] of 0x0E07 / 0x0117 (fixed RNG seed), the same on both discs unless a "<name> aoti" key holds AotI's own
COMBAT_DIGESTS = {
    # the player's striking block is 0x1204 like the client's (0x262db0), no sure-hit 0x4000: accuracy is rolled
    "0e07 melee": "9b34bcc9579b7587",
    # techniques are sure hits (0x4000, 0xb5064), so no accuracy roll; Foie's D carries fire 15 % (10 + Lv6 + 0
    # for mixed schools), burn (0x2000, D+4 1, D+0x12 1) and the caster's S5 at D+0xe (PSU 100, AotI 120, the only
    # AotI difference); the second hit's burn roll lands (SE1 0x800001, the enemy's SE2 0x1)
    "0e07 foie": "d87696097bbb59fe",
    "0e07 foie aoti": "6157150be550ef54",
    # two enemy hits (0x4000, no roll); the enemy's attacker block shows its HP after Foie and its burn (SE2 0x1)
    "0e07 enemy": "a8bb98c47c136ba8",
    "0e07 monomate": "e2686493f9863da3",
    "0e07 moon": "ccf45e8c2d86678b",
    "0117": "dcc7b77646aad643",
}


def _slot(uuid, iid, qty=1, **kw):
    return dict({"uuid": uuid, "id": iid, "qty": qty, "active": 0, "slots": 0, "grind": 0, "pa": [0, 0]}, **kw)


def _hits(sender, hits):
    """C>S 0x0E00 body: (src, dst, hit word, D) per 0x50-byte hit."""
    b = bytes(0x24) + struct.pack("<III", len(hits), sender.lid, 1)
    for src, dst, w, D in hits:
        b += struct.pack("<fffIIII", 1.0, 2.0, 3.0, src, dst, w, 0x55) + bytes(range(32)) + D.ljust(20, b"\0")
    return b


def _fighters(game):
    """A Force with a Rayharod (Foie Lv6, Resta Lv10, Shifta Lv16, Deband Lv1) and a melee weapon, and a
    partner holding a Monomate and a Moon Atomizer, in a mission zone of their own."""
    from psu import combat
    games.set_current(game)
    a = player(0x1111, 0, game, "Alpha", 0, 2, (1100000, 0, 4), level=20)
    b = player(0x2222, 1, game, "Bravo", 1, 0, (1100000, 0, 4), level=20)
    a.zone = b.zone = (1, 7777, 1)
    combat.clear_zone(a.zone)
    a.state["inv"] = [_slot(0x700, "01180201", active=1, techs=[[1, 5], [41, 9], [6, 15], [16, 0]]),
                      _slot(0x701, "01010000", active=1)]
    a.state["pas"] = [[0, "06010005"], [0, "06050009"]]
    b.state["inv"] = [_slot(0x702, "03010000", 5), _slot(0x703, "03010700", 2)]
    return a, b


def combat_packets(game):
    import random
    a, b = _fighters(game)
    out = {}
    enemy = struct.pack("<IIhhhhBBBb", 0x5200, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    seq = [("melee", a, [(0, 0x100, 0x701, b"")]),
           ("foie", a, [(0, 0x100, 0x700, b""), (0, 0x100, 0x700 | (0 << 29), b"")]),
           ("enemy", a, [(0x100, 0, 0, enemy), (0x100, 1, 0, enemy)]),
           ("monomate", b, [(1, 0, 0x702, b"")])]
    for name, me, hs in seq:
        random.seed(7)
        out["0e07 " + name] = combat.c0e07(me, combat.resolve(me, [a, b], _hits(me, hs), 20)[0])
    a.state["hp"] = 0
    random.seed(7)
    out["0e07 moon"] = combat.c0e07(b, combat.resolve(b, [a, b], _hits(b, [(1, 0, 0x703, b"")]), 20)[0])
    out["0117"] = combat.c0117(a, 123)
    combat.clear_zone(a.zone)
    games.set_current(games.PSU)
    return out


def support_checks():
    """Support techniques on each disc: Resta's heal by level (TP + weapon ATP) x (pow + inc x (L - 10 tier)) / 100,
    the 0x0E07 entry (SE1 0x80000 + healed amount + new HP; 0x1000000 + SE2 buff bits for Shifta / Deband /
    Megistar), Giresta's revive (SE1 0x40000), Reverser's cure (nothing to cure -> no result; AotI's mask),
    buffs on later hits / 0x0117, Shifta's ATP multiplier, and PA EXP for the non-damaging results."""
    import json
    import random
    from psu import items
    ok = {}
    roots = [os.path.join(os.environ["PSU_GAMEDATA"], "aoti")] + [os.path.join(WORK, "aoti", d)
                                                                     for d in ("gamedata", "gamedata-staging")]
    for game in (games.PSU, games.AOTI):
        if game == games.AOTI:
            paths = {n: next((os.path.join(r, n) for r in roots if os.path.exists(os.path.join(r, n))), None)
                     for n in ("items.json", "pa.json")}
            if None in paths.values():
                print("note: no AotI items/pa tables, AotI support techniques not checked")
                continue
            for n, p in paths.items():
                with open(p, encoding="utf-8") as f:
                    games._JSON[(games.AOTI, n)] = json.load(f)
        a, b = _fighters(game)
        b.state["level"] = 100                              # enough max HP that no heal below is capped
        rod = a.state["inv"][0]
        tp = progression.derived(a.raw, a.state)["stats"][6] + items.db()[rod["id"]]["atp"]
        mx = progression.derived(b.raw, b.state)["max_hp"]

        def cast(slot_no, target, me=a):
            return combat.resolve(me, [a, b], _hits(me, [(me.lid, target.lid, 0x700 | (slot_no << 29), b"")]), 20)

        top = 49 if game == games.AOTI else 29
        for lv, pct in ((0, 101), (9, 110), (10, 126), (25, 156), (top, 151 + top - 20)):
            rod["techs"][1] = [41, lv]
            b.state["hp"] = 1
            ent, _, hurt, grown = cast(1, b)
            heal = tp * pct // 100
            e = ent[0] if ent else bytes(0x68)
            ok[f"{game} Resta Lv{lv + 1} heals {heal}"] = len(ent) == 1 and struct.unpack_from("<I", e, 4)[0] == heal \
                and struct.unpack_from("<III", e, 0x34) == (0x80000, 0, 1 + heal) and b.state["hp"] == 1 + heal \
                and hurt == [b] and struct.unpack_from("<I", e, 0x54)[0] & 0x1400 == 0x1400
        b.state["hp"] = mx
        ok[f"{game} Resta at full HP: no result"] = cast(1, b)[0] == []
        a.state["pas"][1] = [0, "06050009"]
        b.state["hp"] = 1
        g = cast(1, b)[3]
        ok[f"{game} Resta grows Resta"] = len(g) == 1 and g[0][1] is a.state["pas"][1] and a.state["pas"][1][0] == 1
        # Shifta Lv16 (tier 1: strength 2 -> level index 1) on the partner and on self
        ent, _, _, g = cast(2, b)
        e = ent[0] if ent else bytes(0x68)
        ok[f"{game} Shifta SE1 0x1000000 SE2 0x80000"] = struct.unpack_from("<II", e, 0x34) == (0x1000000, 0x80000) \
            and e[0x54 + 0x12] == 1 and struct.unpack_from("<I", e, 0x58)[0] == 0x80000 and g == []
        ok[f"{game} Shifta x1.26 for 180 s"] = abs(b.buffs[0x80000][1] - 1.26) < 1e-6 and \
            175 < combat.next_expiry(b) <= 180
        rod["techs"][3] = [16, 25]                          # Deband Lv26: tier 2, strength 3 -> level index 2
        e = cast(3, a)[0][0]
        ok[f"{game} Deband on self SE2 0x100000"] = struct.unpack_from("<II", e, 0x34) == (0x1000000, 0x100000) \
            and struct.unpack_from("<II", e, 0x24) == (0x1000000, 0x100000) and abs(a.buffs[0x100000][1] - 1.96) < 1e-6
        ok[f"{game} 0x0117 carries SE2"] = struct.unpack_from("<I", combat.c0117(b, 5)[8:], 0x30)[0] == 0x80000
        random.seed(7)
        e = combat.resolve(a, [a, b], _hits(a, [(0, 0x100, 0x701, b"")]), 20)[0][0]
        a.buffs.pop(0x100000)
        a.buffs[0x80000] = (10 ** 12, combat._mult(0x3FA3D70A))
        random.seed(7)
        e2 = combat.resolve(a, [a, b], _hits(a, [(0, 0x101, 0x701, b"")]), 20)[0][0]
        p1, p2 = struct.unpack_from("<h", e, 0x54 + 0xC)[0], struct.unpack_from("<h", e2, 0x54 + 0xC)[0]
        ok[f"{game} Shifta ATP x1.28 on hits, SE2 in the attacker block"] = \
            p2 == int(combat.f32(p1 * combat.f32(1.28))) and struct.unpack_from("<I", e2, 0x28)[0] == 0x80000
        a.buffs.clear()
        # Megistar Lv1: all four buffs + 0x2000
        rod["techs"][3] = [56, 0]
        e = cast(3, b)[0][0]
        ok[f"{game} Megistar SE2 0x782000"] = struct.unpack_from("<I", e, 0x38)[0] == 0x782000 | 0x80000
        # Giresta Lv1 on a downed player: revive with HP = power, SE1 0x40000
        rod["techs"][3] = [42, 0]
        b.state["hp"] = 0
        b.buffs.clear()
        ent, _, _, g = cast(3, b)
        e = ent[0] if ent else bytes(0x68)
        ok[f"{game} Giresta revives"] = struct.unpack_from("<III", e, 0x34) == (0x40000, 0, min(mx, tp * 151 // 100)) \
            and b.state["hp"] == min(mx, tp * 151 // 100) and bool(struct.unpack_from("<I", e, 0x54)[0] & 0x800)
        # Reverser: nothing to cure -> nothing; a debuff (Jellen 0x4000, AotI also 0x20000000) is cured
        rod["techs"][3] = [43, 0]
        ok[f"{game} Reverser without ailments: no result"] = cast(3, b)[0] == []
        b.buffs.update({0x4000: (10 ** 12, 0.83), 0x20000000: (10 ** 12, 1.0)})
        e = cast(3, b)[0][0]
        left = 0x20000000 if game == games.PSU else 0
        ok[f"{game} Reverser cures 0x4000"] = struct.unpack_from("<II", e, 0x34) == (0x80000, left) and \
            struct.unpack_from("<I", e, 0x58)[0] == combat.CURE_BITS[game]
        # a support technique on an enemy does nothing (no damage)
        ok[f"{game} Resta on an enemy: no result"] = combat.resolve(
            a, [a, b], _hits(a, [(0, 0x100, 0x700 | (1 << 29), b"")]), 20)[0] == []
        combat.clear_zone(a.zone)
    games.set_current(games.PSU)
    return ok


def _aoti_tables():
    """Load the AotI items / pa / paparam tables the way support_checks does; False when they are missing."""
    import json
    roots = [os.path.join(os.environ["PSU_GAMEDATA"], "aoti")] + [os.path.join(WORK, "aoti", d)
                                                                     for d in ("gamedata", "gamedata-staging")]
    for n in ("items.json", "pa.json", "paparam.json", "enemies.json", "enemylevel.json", "plylevel.json"):
        p = next((os.path.join(r, n) for r in roots if os.path.exists(os.path.join(r, n))), None)
        if p is None:
            if n in ("items.json", "pa.json"):
                return False
            continue
        with open(p, encoding="utf-8") as f:
            games._JSON[(games.AOTI, n)] = json.load(f)
    return True


def status_checks():
    """Status effects, elements and equipment on each disc (psu/combat.py): StatusChangeControlData ticks (burn
    % of max HP every 2 s, never lethal; Giresta heal, Megistar drain), hit-ended freeze, instant death, Jellen /
    Zalure on an enemy (0x0E07 SE1 0x800000, the slot multipliers on its attack and defence), Dizas charges
    blocking a striking hit, Reverser / Antimate cures, element weakness (PSU 1 + a + d, AotI 1 + 0.66 (a + d)),
    a technique's element and burn in its D, a line shield's DFP / element, a type-3 unit's immunities and a
    type-1 unit's TP in Resta's power, the tick / expiry 0x0E07 entries and the enemy's element from enemies.json."""
    import random
    import time as _t
    from psu import items
    ok = {}
    for game in (games.PSU, games.AOTI):
        if game == games.AOTI and not _aoti_tables():
            print("note: no AotI items/pa tables, AotI status effects not checked")
            continue
        a, b = _fighters(game)
        b.state["level"] = 100
        mx = progression.derived(b.raw, b.state)["max_hp"]
        b.state["hp"] = mx
        rod = a.state["inv"][0]
        now = _t.monotonic()

        def cast(slot_no, target, me=a):
            return combat.resolve(me, [a, b], _hits(me, [(me.lid, target.lid, 0x700 | (slot_no << 29), b"")]), 20)

        # burn (status 0, level 1: 11 s, every 2 s, 3 % of max HP) on a player: ticks, never lethal, expiry
        got = combat.inflict(b, 0x1, 1, 100, 0, sure=True)
        ev, hp = combat.status_step(b, mx, mx, now + 2.05)
        ok[f"{game} burn ticks 3% every 2 s"] = got == 0x1 and ev == [("dmg", mx * 3 // 100, mx - mx * 3 // 100)]
        ev, hp = combat.status_step(b, 2, mx, now + 4.1)
        ok[f"{game} burn never kills"] = ev == [("dmg", 1, 1)] and hp == 1
        ent = combat._tick_entries(b, b.lid, ev)[0]
        ok[f"{game} tick 0x0E07 entry"] = len(ent) == 0x68 and struct.unpack_from("<IIIIII", ent, 4)[0] == 1 and \
            struct.unpack_from("<II", ent, 0x14) == (0xFFFFFFFF, b.lid) and \
            struct.unpack_from("<III", ent, 0x34) == (1, 0x1, 1) and struct.unpack_from("<Ihhh", ent, 0x54)[0] == 0x115210
        b.buffs[0x1] = (now + 1, 1.0)
        ev, _ = combat.status_step(b, 2, mx, now + 1.5)
        ok[f"{game} burn expiry"] = ("end", 0x1, 2) in ev and not combat.se2(b) & 0x1
        # burn blocks freeze (table +4: 0x421); without it freeze (status 5, level 0: 3 hits) ends after 3 landed hits
        combat.inflict(b, 0x1, 0, 100, 0, sure=True)
        ok[f"{game} burn blocks freeze"] = combat.inflict(b, 0x20, 0, 100, 0, sure=True) == 0
        b.buffs.clear()
        combat.inflict(b, 0x20, 0, 100, 0, sure=True)
        for _ in range(2):
            combat.break_on_hit(b)
        mid = combat.se2(b) & 0x20
        combat.break_on_hit(b)
        ok[f"{game} freeze ends after 3 hits"] = bool(mid) and not combat.se2(b) & 0x20
        ok[f"{game} STA at or above the chance: no roll"] = combat.chance_roll(24, 100, 24, False) is False
        # Reverser and Antimate cure a burn; a type-3 unit's immunities block one
        combat.inflict(b, 0x1, 0, 100, 0, sure=True)
        rod["techs"][3] = [43, 0]
        e = cast(3, b)[0][0]
        ok[f"{game} Reverser cures burn"] = struct.unpack_from("<I", e, 0x34)[0] == 0x80000 and not combat.se2(b) & 1
        combat.inflict(b, 0x2, 0, 100, 0, sure=True)
        b.state["inv"].append(_slot(0x704, "03010500"))
        ent = combat.resolve(b, [a, b], _hits(b, [(1, 1, 0x704, b"")]), 20)[0]
        ok[f"{game} Antimate cures poison"] = len(ent) == 1 and struct.unpack_from("<II", ent[0], 0x34) == (0x80000, 0) \
            and struct.unpack_from("<II", ent[0], 0x54) == (0x2400, combat.CURE_BITS[game])
        b.state["inv"].append(_slot(0x705, "08030c00", active=1))           # Cati / Maintenance
        imm = combat.immunity(b)
        ok[f"{game} Cati / Maintenance immunities"] = imm & 0x3FF == 0x1FF and \
            combat.inflict(b, 0x1, 0, 100, 0, True, imm) == 0
        b.state["inv"].pop()
        # line shield: Perpaline adds DFP 33 / EVP 58 and fire 8 % (PSU rec[1] low nibble, AotI rec[1])
        base = progression.derived(b.raw, b.state)["stats"]
        b.state["inv"].append(_slot(0x706, "02010100", active=1))
        tot = combat.total_stats(b)
        ok[f"{game} Perpaline DFP / EVP / fire 8%"] = tot[1] == base[1] + 33 and tot[3] == base[3] + 58 and \
            combat.defence_element(b) == (1, 8)
        b.state["inv"].pop()
        # element: Gigush (ice 16 %) on a fire enemy with 5 %: weak
        gd = items.db()["01010100"]
        a.state["inv"].append(_slot(0x707, "01010100", active=1))
        en = combat.zone_enemies(a.zone).setdefault(0x120, combat.new_enemy(a.zone, 0x120, 20))
        en.element, en.element_pct = 1, 5
        D = combat.player_attack(a, 0x707, bytes(20))
        m = combat.element_mult(D[0x10], D[0x11], 1, 5, game)
        want = combat.f32(1.0 + (1.0 if game == games.PSU else 0.66) * (combat.f32(0.16) + combat.f32(0.05)))
        random.seed(3)
        dmg, se = combat.damage(D, en.stats, en.hp, None, en)
        power = struct.unpack_from("<h", D, 0xC)[0]
        exp = int(combat.f32(combat.f32(combat.f32(combat.f32(float(power)) * m) - en.stats[1]) / 5.0))
        exp = exp * 3 // 2 if se & 0x400000 else exp
        ok[f"{game} Gigush ice vs fire x{want:.4f}"] = (D[0x10], D[0x11]) == (9, gd["ele_pct"]) and \
            abs(m - want) < 1e-6 and dmg == exp
        en.element = 9
        ok[f"{game} same element resists"] = combat.element_mult(9, 16, 9, 5, game) < 1.0
        # Foie Lv6 carries fire 10 + 5 + 0 % (mixed schools) and burn strength 2 (level 1) with the caster's S5
        D = combat.player_attack(a, 0x700, bytes(20))
        s5 = combat.total_stats(a)[5]
        ok[f"{game} Foie D: fire 15 %, burn level 1, S5"] = (D[0x10], D[0x11], D[0x12]) == (1, 15, 1) and \
            struct.unpack_from("<I", D, 4)[0] == 1 and struct.unpack_from("<I", D, 0)[0] & 0x2000 and \
            struct.unpack_from("<h", D, 0xE)[0] == s5
        # accuracy: a block without 0x4000 with accuracy 1 misses a normal target, never a frozen one (0x1c0060)
        Da = struct.pack("<IIhhhhBBBb", 0x1204, 0, 1, 0, 500, 100, 0, 0, 0, 0)
        en4 = combat.new_enemy(a.zone, 0x123, 20)
        miss = combat.damage(Da, en4.stats, en4.hp, None, en4)[1]
        combat.inflict(en4, 0x20, 0, 100, 0, sure=True)
        ok[f"{game} accuracy roll, frozen always hit"] = miss == 0x10 and             all(combat.damage(Da, en4.stats, en4.hp, None, en4)[1] & 0x1 for _ in range(20))
        # instant death through after_hit
        en2 = combat.new_enemy(a.zone, 0x121, 20)
        Dd = bytearray(20)
        struct.pack_into("<IIhhh", Dd, 0, 0x3200 | 0x8000, 0x400, 0, 0, 0)
        se, hp, dmg = combat.after_hit(en2, bytes(Dd), 0x1, en2.hp - 5, 5)
        ok[f"{game} instant death"] = hp == 0 and dmg == en2.hp and se & 0x800000
        # Jellen Lv1 on an enemy: status only (no damage), SE1 0x800000, its attack x0.83; Zalure: DFP x0.6
        rod["techs"][3] = [7, 0]
        random.seed(1)
        en3 = combat.zone_enemies(a.zone).setdefault(0x122, combat.new_enemy(a.zone, 0x122, 20))
        res = []
        for _ in range(5):
            res = combat.resolve(a, [a, b], _hits(a, [(0, 0x122, 0x700 | (3 << 29), b"")]), 20)[0]
            if res:
                break
        e = res[0] if res else bytes(0x68)
        ok[f"{game} Jellen: SE1 0x800000, SE2 0x4000, no damage"] = bool(res) and \
            struct.unpack_from("<IIII", e, 0x34)[:2] == (0x800000, 0x4000) and struct.unpack_from("<I", e, 4)[0] == 0 \
            and en3.hp == en3.max_hp
        Dh = struct.pack("<IIhhhhBBBb", 0x5204, 0, 100, 0, 1000, 100, 0, 0, 0, 0)
        e = combat.resolve(a, [a, b], _hits(a, [(0x122, 1, 0, Dh)]), 20)[0][0]
        ok[f"{game} Jellen: its attack power x0.83"] = struct.unpack_from("<h", e, 0x54 + 0xC)[0] == \
            int(combat.f32(combat.f32(1000.0) * combat.f32(0.83)))
        combat.inflict(en3, 0x8000, 0, 100, 0, sure=True)
        ok[f"{game} Zalure: DFP x0.6"] = combat.enemy_stats_now(en3)[1] == \
            int(combat.f32(combat.f32(float(en3.stats[1])) * combat.f32(0.6)))
        # Dizas Lv1 (strength 2): one charge on the partner; a striking hit then does nothing and uses it
        rod["techs"][3] = [36, 0]
        res = cast(3, b)[0]
        e = res[0] if res else bytes(0x68)
        ok[f"{game} Dizas gives 1 charge (+0x40)"] = bool(res) and e[0x40] == 1 and b.barrier[0] == 1
        hp0 = combat.player_hp(b)
        e = combat.resolve(a, [a, b], _hits(a, [(0x122, 1, 0, Dh)]), 20)[0][0]
        ok[f"{game} Dizas charge blocks a striking hit"] = struct.unpack_from("<I", e, 4)[0] == 0 and \
            combat.player_hp(b) == hp0 and b.barrier[0] == 0
        # Giresta (regen 0x1000 level 0: 8 % every 4 s) and Megistar (drain 0x2000 level 0: 7 % every 5 s)
        b.buffs.clear()
        b.ail.clear()
        b.state["hp"] = mx // 2
        rod["techs"][3] = [42, 0]
        cast(3, b)
        t0 = _t.monotonic()
        ev, hp = combat.status_step(b, mx // 2, mx, t0 + 4.05)
        ok[f"{game} Giresta heals 8% every 4 s"] = combat.se2(b) & 0x1000 and ev and ev[0][:2] == ("heal", mx * 8 // 100)
        b.buffs.clear()
        b.ail.clear()
        rod["techs"][3] = [56, 0]
        cast(3, b)
        t0 = _t.monotonic()
        ev, hp = combat.status_step(b, mx // 2, mx, t0 + 5.05)
        ok[f"{game} Megistar drains 7% every 5 s"] = ev and ev[0][:2] == ("dmg", mx * 7 // 100)
        # a type-1 unit's TP (Cara / Force: rec+4) adds to Resta's power
        b.buffs.clear()
        a.state["inv"].append(_slot(0x708, "08010000", active=1))
        rec = bytes.fromhex(items.db()["08010000"]["rec"])
        tp = progression.derived(a.raw, a.state)["stats"][6] + struct.unpack_from("<h", rec, 4)[0] + \
            items.db()[rod["id"]]["atp"]
        rod["techs"][1] = [41, 0]
        b.state["hp"] = 1
        e = cast(1, b)[0][0]
        ok[f"{game} Cara / Force TP in Resta"] = struct.unpack_from("<I", e, 4)[0] == tp * 101 // 100
        a.state["inv"].pop()
        # an enemy's element and % from enemies.json (Rappy: category 4, light; row +0x14 x Param +0x24)
        gdat = combat.enemy_data()
        if gdat:
            r = combat.Enemy(0x130, 10, {"level": 10, "variant": 0})
            ok[f"{game} Rappy is light with the row %"] = r.element == gdat["variants"]["0"]["category"] == 4 and \
                r.element_pct == int(gdat["levels"][10][9] * gdat["variants"]["0"].get("x24", 1.0))
        combat.clear_zone(a.zone)
    games.set_current(games.PSU)
    return ok


def types_ns():
    import types
    return types.SimpleNamespace(gid=0x1234, lid=1)


def shop_checks():
    """My Shop (psu/playershop.py): an AotI owner's shop as PSU and AotI visitors see, search and buy from it."""
    from psu import myroom, playershop as ps
    lobby = (1100000, 0, 4)
    owner = player(0x7777, 1, games.AOTI, "Owen", 0, 0, lobby)
    y = player(0x5555, 2, games.PSU, "Yuri", 0, 1, lobby)
    z = player(0x6666, 3, games.AOTI, "Zed", 1, 2, lobby)
    st = owner.state
    aoti_only = "01010b00"
    st["meseta"] = 100
    st["room_stock"] = [_slot(0x101, aoti_only), _slot(0x102, "03010000", qty=5)]
    games.set_current(games.AOTI)
    ok = {}
    s0, e0 = ps.put(st, 0x101, 500, 1)
    s1, e1 = ps.put(st, 0x102, 40, 5)
    ok["put"] = (s0, e0, s1, e1) == (0, None, 1, None) and st["room_stock"] == []
    ps.set_open(st, 1)
    games.set_current(games.PSU)
    r_psu = ps.room_record(st, games.PSU)
    games.set_current(games.AOTI)
    r_aoti = ps.room_record(st, games.AOTI)
    word = lambda r, i: struct.unpack_from("<i", r, 0x144 + i * 0x20)[0]
    ok["psu sees no aoti item"] = word(r_psu, 0) == -1 and word(r_psu, 1) == 1 and len(r_psu) == 0xC40
    ok["aoti sees both"] = word(r_aoti, 0) == 0 and struct.unpack_from("<I", r_aoti, 0x544)[0] == 500
    ok["open flag"] = struct.unpack_from("<I", r_psu, 0x5C4)[0] == 1
    rows = [(owner.gid, 0, owner.raw[0x28:0x68], st)]
    games.set_current(games.PSU)
    hp = ps.search(rows, 4, "monomate", games.PSU) + ps.search(rows, 4, "sword", games.PSU)
    games.set_current(games.AOTI)
    ha = ps.search(rows, 0, "owe", games.AOTI)
    ok["search"] = len(hp) == 1 and hp[0][0x40] == 1 and len(ha) == 1 and ha[0][0x40] == 2
    games.set_current(games.PSU)
    y.state["meseta"] = 1000
    seen = ps._item_rec(ps.slot_entry(st, 1), games.PSU)
    _, _, _, err0 = ps.buy(st, y.state, 0, 500, 1, None, games.PSU)
    added, n, total, err1 = ps.buy(st, y.state, 1, 40, 2, seen, games.PSU)
    ok["buy"] = (err0 == "no such item" and err1 is None and n == 2 and total == 80 and y.state["meseta"] == 920
                 and st["meseta"] == 180 and ps.slot_entry(st, 1)["item"]["qty"] == 3 and added["id"] == "03010000")
    games.set_current(games.AOTI)
    ok["take back"] = ps.take_back(st, 0, -1) is None and ps.slot_entry(st, 0) is None and len(st["room_stock"]) == 1
    ok["sizes"] = (len(ps.c1323(z, st)) == 8 + 0x24 + 0xC40 + 0x1200 and len(ps.c1314(z, 0, ha)) == 8 + 0x30 + 30 * 0x148
                   and len(myroom.c130a(z, st)) == 8 + 0x2C + 0xC40 + 0x1200 + 4 + 0x68)
    ps.note_visit(st, y.gid, y.raw, 0, now=0)
    log = ps.c1320(owner, st)[8:]
    ok["visitor log"] = (struct.unpack_from("<I", log, 0x28)[0] == 1 and struct.unpack_from("<I", log, 0x34)[0] == y.gid
                         and log[0x3C:0x7C] == y.raw[0x28:0x68])
    ps.set_probo_name(st, "Pochi".encode("utf-16-le"))
    room = myroom.c1327(owner, owner.gid, 0, owner.raw, st)[8:]
    ok["probo name"] = room[0x94E:0x950] == b"\1\0" and room[0x950:0x95A] == "Pochi".encode("utf-16-le")
    games.set_current(games.PSU)
    return ok


def main():
    psu = {"a": games.PSU, "b": games.PSU, "c": games.PSU}
    if "--print-aoti" in sys.argv:
        for k, v in scenario({"a": games.AOTI, "b": games.AOTI, "c": games.AOTI})[0].items():
            print(f'    "{k}": "{digest(v)}",')
        return
    pk, _ = scenario(psu)
    if "--print" in sys.argv:
        for k, v in {**pk, **type_packets()}.items():
            print(f'    "{k}": "{digest(v)}",')
        return
    if "--print-econ" in sys.argv:
        for k, v in econ_scenario().items():
            print(f'    "{k}": "{digest(v)}",')
        return
    tp = type_packets()
    ec = econ_scenario()
    bad = [k for k, v in pk.items() if PSU_DIGESTS.get(k) != digest(v)]
    bad += [k for k, v in tp.items() if TYPE_DIGESTS.get(k) != digest(v)]
    for k in bad:
        print(f"PSU->PSU {k}: {digest({**pk, **tp}[k])} != {PSU_DIGESTS.get(k, TYPE_DIGESTS.get(k))}")

    # AotI recipient b of a PSU player a, and the reverse
    mix, (a, b, c) = scenario({"a": games.PSU, "b": games.AOTI, "c": games.PSU})
    ub = mix["0201"][8 + 0x24:8 + 0x24 + 712]  # 0x0201: user record after the 0x24-byte header
    status = ub[0x164:]
    lvl, = struct.unpack_from("<I", status, 0x28)
    hp, mhp = struct.unpack_from("<II", status, 0x3C)
    ok = [lvl == 12, hp == 50, mhp == progression.derived(a.raw, a.state)["max_hp"]]
    # 0x0233 to an AotI recipient: records 0x2CC apart
    games.set_current(games.PSU)
    two = world.c0233_players(b, [a, c])[8:]
    n, = struct.unpack_from("<I", two, 0x24)
    second = two[0x28 + 0x2CC:]
    ok += [n == 2, struct.unpack_from("<I", second, 4)[0] == c.gid]
    # PSU recipient: 0x2D0 apart
    two = world.c0233_players(a, [b, c])[8:]
    ok += [struct.unpack_from("<I", two, 0x28 + 0x2D0 + 4)[0] == c.gid]
    # Spaceport: a PSU player (map 900) is shown to an AotI recipient on map 901, and the reverse
    m_aoti = struct.unpack_from("<I", world.c0205_map(b, c)[8:], 0x2C)[0]
    rec = world.c0201_char(b, c)[8 + 0x24:]
    ok += [m_aoti == 901, struct.unpack_from("<I", rec, 0x20)[0] == 901]
    rev, (a2, b2, c2) = scenario({"a": games.PSU, "b": games.PSU, "c": games.AOTI})
    ok += [struct.unpack_from("<I", rev["0205port"][8:], 0x2C)[0] == 900]
    # another player's own 0x010D built in a PSU task for an AotI player: AotI status layout
    own = mix["010d_own_other"][8:]
    st2 = own[0x60 + 0x114:]
    ok += [struct.unpack_from("<I", st2, 0x28)[0] == 12, struct.unpack_from("<I", st2, 0x3C)[0] == 50]
    at = aoti_types()
    ak, _ = scenario({"a": games.AOTI, "b": games.AOTI, "c": games.AOTI})
    abad = [k for k, v in ak.items() if AOTI_DIGESTS.get(k) != digest(v)]
    for k in abad:
        print(f"AotI->AotI {k}: {digest(ak[k])} != {AOTI_DIGESTS.get(k)}")
    nx, xbad = xgame_checks()
    for k in xbad:
        print("AotI->PSU", k, "failed")
    ebad = [k for k, v in ec.items() if ECON_DIGESTS.get(k) != digest(v)]
    for k in ebad:
        print(f"PSU economy {k}: {digest(ec[k])} != {ECON_DIGESTS.get(k)}")
    ae = aoti_econ()
    tc = tech_checks()
    lc = learn_checks()
    for k, v in lc.items():
        if not v:
            print("PA disk", k, "failed")
            ebad.append(k)
    for k, v in tc.items():
        if not v:
            print("technique", k, "failed")
            ebad.append(k)
    cp = {(g, k): v for g in (games.PSU, games.AOTI) for k, v in combat_packets(g).items()}
    for (g, k), v in cp.items():
        want = COMBAT_DIGESTS.get(f"{k} {g}", COMBAT_DIGESTS.get(k))
        if want != digest(v):
            print(f"combat {g} {k}: {digest(v)} != {want}")
            ebad.append(k)
    sc = support_checks()
    for k, v in sc.items():
        if not v:
            print("support", k, "failed")
            ebad.append(k)
    stc = status_checks()
    for k, v in stc.items():
        if not v:
            print("status", k, "failed")
            ebad.append(k)
    for k, v in (ae or {}).items():
        if not v:
            print("AotI economy", k, "failed")
            ebad.append(k)
    ppc = pp_checks()
    for k, v in ppc.items():
        if not v:
            print("PP", k, "failed")
            ebad.append(k)
    shc = shop_checks()
    for k, v in shc.items():
        if not v:
            print("my shop", k, "failed")
            ebad.append(k)
    if bad or abad or xbad or ebad or not all(ok) or (at is not None and not all(at)):
        print("FAIL", "structural:", ok, "AotI types:", at)
        sys.exit(1)
    print(f"ok: {len(pk) + len(tp)} PSU->PSU and {len(ak)} AotI->AotI packets match, {len(ok)} cross-game checks, "
          + (f"{len(at)} AotI type checks, " if at is not None else "AotI types skipped (no AotI plylevel.json), ")
          + f"{nx} AotI->PSU translation checks, {len(ec)} PSU economy digests, "
          + (f"{len(ae)} AotI economy checks" if ae is not None else "AotI economy skipped (no AotI tables)")
          + f", {len(tc)} technique checks, {len(lc)} PA disk checks, {len(cp)} combat digests, {len(sc)} support technique checks"
          + f", {len(shc)} my shop checks, {len(ppc)} PP checks"
          + f", {len(stc)} status / element / equipment checks")


if __name__ == "__main__":
    main()
