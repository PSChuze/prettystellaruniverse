"""First area change: put a freshly loaded character into a lobby (game service).

Sequence and layouts from EGS (egs_game.erl area_change, egs_proto.erl, psu_characters.erl;
re-implemented from those descriptions). The quest and zone files (NBL archives) are built
from the disc and EGS's lobby sources by extract/ (docs/data.md) and read from $PSU_NBL_DIR; they are never
committed. BE words (16#...:32 in EGS) stay BE.

Archives are $PSU_NBL_DIR/quest-<quest>.nbl and zone-<quest>-<zone>.nbl. Entry into a quest + zone,
in EGS order (a zone change inside the same quest skips 0x0C00/0x020E; a map change is 0x0205 on):
  0x0C00 quest init, 0x020E quest NBL, 0x0A05, 0x0111 event 6, 0x010D character, 0x0200 zone init,
  0x020F zone NBL, 0x0205 load map (area nb 1), 0x100E location, 0x1212, 0x0201 character,
  0x0A06 item uuids, 0x0208 finish loading (area nb 2), 0x0236 start zone.
"""
import os
import struct

from psu import games, items, progression, xgame
from psu import season as seasons
from psu.charload import char_bin, g
from psu.login import packet

be = bytes.fromhex
LOBBY = {"quest": 1100000, "zone": 0, "map": int(os.environ.get("PSU_LOBBY_MAP", "4")),
         "entry": int(os.environ.get("PSU_LOBBY_ENTRY", "0")), "set": 0, "seasonal": 0, "season": 255}
NBL_DIR = os.environ.get("PSU_NBL_DIR", os.path.join(games.WORK, "lobby-nbl", "ps2"))
# Staged content laid over $PSU_NBL_DIR (os.pathsep-separated dirs, same layout: <dir>/<game>/<name>, PSU <dir>/<name>),
# for story content a host builds that is not on the disc: archives found there win, and a missions.json there
# extends the disc's (missions.data)
NBL_EXTRA = [p for p in os.environ.get("PSU_NBL_EXTRA", "").split(os.pathsep) if p]


def stats_bin(atp=1000, ata=2000, tp=3000, dfp=4000, evp=5000, mst=6000, sta=7000):
    return struct.pack("<8H", atp, dfp, ata, evp, sta, 0, tp, mst)   # EGS defaults, same order


def map_for(area, game, owner):
    """area of a player of disc `owner` as shown to a player of disc `game`: the shared Spaceport is map 900 for
    PSU and 901 for AotI (resolve), so each side sees the other on its own copy. Same disc: unchanged."""
    q, z, m = area
    if owner and game and owner != game and q == 1104000 and m in (900, 901):
        m = 901 if game == games.AOTI else 900
    return q, z, m


def user_bin(gid, lid, raw, area, entry, pos=(0.0, 0.0, 0.0, 0.0), st=None, game=None, owner=None):
    """EGS psu_characters:character_user_to_binary for a player (712 B): 80-byte location part, char_bin,
    then the PS2 status block (progression.status_block: EXP thresholds, max HP, stats, level, HP).
    game = the disc that reads it (another player's record: the recipient's, world.Player.user; owner = the
    record's player's disc, for map_for); default this session's. Both discs read the same layout up to the
    status block, whose layout is per disc (AotI = EGS/PC). PSU's block is 4 bytes longer (0x168: its 0x010D
    handler 0x14efc0 strides records 0x2B4, AotI's 0x14c540 0x2B0), so the 712 bytes hold all of AotI's and
    all but PSU's last 4 (zero) bytes. An AotI player shown to a PSU one: AotI-only parts, voice and type are
    replaced (psu/xgame.py)."""
    q, z, m = map_for(area, game, owner)
    x, y, zz, d = pos
    st = st if st is not None else progression.new_state(raw)
    out = (be("00001200") + struct.pack("<I", gid) + bytes(8) + struct.pack("<HHHH", lid, 0, 0, 0)
           + struct.pack("<IIIHH", q, z, m, entry, 0) + be("0100") + struct.pack("<H", int(d * 182.0416) & 0xFFFF)
           + struct.pack("<fff", x, y, zz) + bytes(8) + struct.pack("<IIII", 0, 0, 0, 0)
           + xgame.char_for(char_bin(raw, st, game), game, owner) + progression.status_block(raw, st, game))
    assert len(out) == 712, len(out)
    return out


# The native lobby zones (disc: offline Extra lobby) warp with their own zone numbers: 2 Colony, 3 Parum,
# 4 Neudaiz, 5 Moatoob, 10 PPT Spaceport. A request for one of those zones with a map that zone has is
# served from the online lobby quest's zone 0 (EGS numbering: counters and shops are zones 1..13 with
# maps 9000+ / 100+, so the map list keeps the two apart).
NATIVE_ZONES = {2: (1100000, {1, 2, 3, 4, 5, 100, 101, 102, 103, 301}),
                3: (1101000, {1, 2, 3, 4, 100, 200, 400}),
                4: (1102000, {1, 3, 100, 200}),
                5: (1103000, {1, 2, 100, 101, 102, 200}),
                10: (1104000, {900, 901})}           # AotI's Spaceport is map 901 (zone10 entr_unit0901)
LOBBY_QUESTS = {1100000, 1101000, 1102000, 1103000, 1104000}
OFFLINE_LOBBY = 200000      # the disc's offline lobby: AotI story bases warp home to it (cwBack_To_City), native zones


def resolve(q, z, m):
    """Online (quest, zone, map) for a request, translating native lobby zone numbers."""
    if q == 1104000 and m == 900 and games.current() == games.AOTI:
        m = 901      # the shared quest's warps say 900 (PSU); AotI's Spaceport zone only has map 901's entries
    if (q in LOBBY_QUESTS or q == OFFLINE_LOBBY) and z in NATIVE_ZONES and m in NATIVE_ZONES[z][1]:
        return NATIVE_ZONES[z][0], 0, m
    if q == 1120000 and z in NATIVE_ZONES and m in NATIVE_ZONES[z][1]:   # leaving My Room (native zone numbers)
        return NATIVE_ZONES[z][0], 0, m
    native = {v[0]: v[1] for v in NATIVE_ZONES.values()}
    if q in native and z in (11, 12, 13) and m in native[q]:   # shop/side doors: the native zone 0 holds these maps
        return q, 0, m
    return q, z, m


LOBBY_VAR = be("00040000") + bytes(20) + be("00140000")              # EGS send_0200 lobby "Var"
MISSION_VAR = (be("06000500") + be("01000000") + bytes(8) + be("00040000") + be("00010000")
               + be("00140000"))                                           # EGS send_0200 mission "Var"


def nbl_file(name, game=None):
    """An archive by name: the $PSU_NBL_EXTRA dirs first, then the session's game's own copy ($PSU_NBL_DIR/aoti/...),
    then the shared one."""
    for root in NBL_EXTRA:
        p = games.data_file(root, name, game)
        if os.path.exists(p):
            return p
    return games.data_file(NBL_DIR, name, game)


def nbl_path(q, z=None):
    """The session's game's own archive ($PSU_NBL_DIR/aoti/...) when there is one, else the shared one."""
    return nbl_file(f"quest-{q}.nbl" if z is None else f"zone-{q}-{z}.nbl")


def available(q, z):
    """True when both archives for quest q / zone z are in $PSU_NBL_DIR."""
    return os.path.exists(nbl_path(q)) and os.path.exists(nbl_path(q, z))


def with_inventory(seq, gid, st):
    """A fresh 0x0A0A just before 0x0A05: the client rebuilds the weapon palette from the 0x0A0A records on
    each zone load (0x0A05 resets the UUIDs, 0x0A06 re-creates the items) and never stores it back.
    Off by default (PSU_ZONE_INVENTORY=1 to try): a second 0x0A0A in the lobby entry right after the character
    load's freezes the client before the zone NBL (no 0x0B05 acks, no input after)."""
    if not st or os.environ.get("PSU_ZONE_INVENTORY") != "1":
        return seq
    i = next((n for n, (_, what) in enumerate(seq) if what == "0x0a05"), None)
    if i is None:
        return seq
    return seq[:i] + [(items.c0a0a_state(gid, st), "0x0a0a inventory (palette)")] + seq[i:]


def c020e_quest(quest_nbl):
    """0x020E quest NBL, laid out for the session's disc. Size at packet +0x2C on both; then
    PSU (handler 0x1e56d0) copies the NBL from +0x30, AotI (0x1e61e0) keeps a word from +0x30 and copies
    from +0x34, the EGS/PC layout. Sent the PSU way, AotI never unpacks its quest: area index 0, black world."""
    size = struct.pack("<H", 0xFFFF) + bytes(34) + struct.pack("<I", len(quest_nbl))
    if games.current() == games.AOTI:
        return packet(0x020E, size + bytes(4) + quest_nbl)
    return packet(0x020E, size + quest_nbl + bytes(4))


def area_sequence(gid, raw, area, entry, nb, quest_change=True, lid=0, set_id=0, season=255, seasonal=0, st=None,
                  mission=False, var=None, host=0):
    """EGS area_change for a zone change: [0x0C00 + 0x020E if the quest changes], 0x0A05, 0x0111 event 6,
    0x010D, 0x0200 zone init, 0x020F zone NBL, then location_sequence (with 0x1212 / 0x0A06)."""
    q, zone, mp = area
    # The active season's archives for this zone (season.py): AotI takes the id from 0x020F, PSU from the quest
    # NBL's zone record, so PSU gets the seasonal zone only together with its seasonal quest NBL (else the set
    # file would place objects whose models were never loaded).
    sea = seasons.for_zone(NBL_DIR, q, zone, gid=gid)
    aoti = games.current() == games.AOTI
    if sea and not aoti and not (quest_change and sea[1]):
        sea = None
    if sea:
        season, seasonal = sea[0], 1
    with open(sea[2] if sea else nbl_path(q, zone), "rb") as f:
        zone_nbl = f.read()
    ub = user_bin(gid, lid, raw, area, entry, st=st)
    lidh = struct.pack("<H", lid) + bytes(18)          # DestLID l16 + 0:144
    seq = []
    if quest_change:
        with open(sea[1] if sea and not aoti else nbl_path(q), "rb") as f:
            quest_nbl = f.read()
        seq += [
            (packet(0x0C00, lidh + g(gid) + bytes(8) + struct.pack("<I", q) + b"\xff" * 128), f"0x0c00 quest init {q}"),
            (c020e_quest(quest_nbl), f"0x020e quest nbl ({len(quest_nbl)} B)"),
        ]
    seq += [
        (packet(0x0A05, lidh + g(gid) + bytes(8)), "0x0a05"),
        (packet(0x0111, struct.pack("<H", lid) + bytes(6) + struct.pack("<I", gid) + bytes(8) + g(gid) + bytes(8)
                + struct.pack("<IIII", gid, lid, 6, 0)), "0x0111 event 6"),
        (packet(0x010D, lidh + g(gid) + bytes(8) + struct.pack("<I", 1) + bytes(4) + be("00000300") + be("ffff0000")
                + bytes(4) + struct.pack("<I", gid) + bytes(24) + struct.pack("<II", gid, lid) + be("ffffffff")
                + ub[80:] + bytes(4)), "0x010d character"),
        (packet(0x0200, lidh + g(gid) + bytes(8) + struct.pack("<HHI", lid, zone, 1) + be("ffffffff")
                + (var or (MISSION_VAR if mission else LOBBY_VAR)) + be("ffffffff") + be("ffffffff")),
         f"0x0200 zone init {zone}" + (" (mission)" if mission else "")),
        # PS2 0x1e55f0: set nb (s8) at +0x2C picks the zone member "set_r<nb>.rel"; size +0x30, NBL +0x34.
        # +0x2D: season id (0..15, 0xFF none), read by AotI (0x1e60f0); PSU takes it from the quest NBL (season.py)
        (packet(0x020F, struct.pack("<H", 0xFFFF) + bytes(34) + bytes([set_id & 0xFF, season]) + bytes(2)
                + struct.pack("<I", len(zone_nbl)) + zone_nbl), f"0x020f zone nbl ({len(zone_nbl)} B)"),
    ]
    loc = location_sequence(gid, raw, area, entry, nb=nb, lid=lid, seasonal=seasonal, zone_change=True, st=st)
    if mission:          # S>C 0x0215 host (0xda180 sets the session's host id; counter loads send it too, EGS)
        i = next((n for n, (_, what) in enumerate(loc) if what.startswith("0x0208")), len(loc))
        loc = loc[:i] + [(packet(0x0215, lidh + g(gid) + bytes(8) + struct.pack("<I", host)), f"0x0215 host {host}")] + loc[i:]
    return with_inventory(seq, gid, st) + loc


NO_SOURCE = 0xFFFFFFFF          # 0x010D record +0x34: no EXP source, the client shows nothing
NO_ENEMY = 0xFFFFFFFE           # an EXP source that is no enemy (GM !level): effect and window, no EXP figure


def c010d_char(gid, raw, area, entry, lid=0, st=None, game=None, source=NO_SOURCE):
    """0x010D character refresh (EGS send_010d): the client takes class and status from it, so type changes are
    sent this way, and it is the only level-up presentation online. The apply (PSU 0x144ad0, AotI 0x1416c0) keeps
    the old level block, copies the new one and returns unless EXP rose (old < new) and record +0x34 (`source`)
    is not -1; then the EXP figure over enemy `source` (0x144770, nothing when no enemy has that id), and if the
    level rose ef_pl_levelup + sound 0x702a9 (0x144720 / AotI 0x141280) and, for the player's own character, the
    stat-gain window from old and new stats (0x144660 / 0x1411c0). So a level-up is this packet alone, with the
    enemy id, and no 0x0115 with the new EXP before it. game = the receiving player's disc when another
    session's task builds it (party EXP, mission rewards)."""
    ub = user_bin(gid, lid, raw, area, entry, st=st, game=game)
    lidh = struct.pack("<H", lid) + bytes(18)
    return packet(0x010D, lidh + g(gid) + bytes(8) + struct.pack("<I", 1) + bytes(4) + be("00000300") + be("ffff0000")
                  + bytes(4) + struct.pack("<I", gid) + bytes(24) + struct.pack("<III", gid, lid, source & 0xFFFFFFFF)
                  + ub[80:] + bytes(4))


def lobby_sequence(gid, raw, lid=0, a=LOBBY, st=None):
    return area_sequence(gid, raw, (a["quest"], a["zone"], a["map"]), a["entry"], nb=1, quest_change=True,
                         lid=lid, set_id=a["set"], season=a["season"], seasonal=a["seasonal"], st=st)


def location_sequence(gid, raw, area, entry, nb, lid=0, seasonal=None, zone_change=False, st=None):
    """EGS area_change from 'load the player location' on: 0x0205 (area nb), 0x100E, [0x1212], 0x0201,
    [0x0A06], 0x0208 (area nb + 1), 0x0236. A map change inside the current zone (0x0807) is only this.
    seasonal (0x0205 +0x43, the obj.get_eventflag switch): None = 1 while this zone has a season (season.py)."""
    q, zone, mp = area
    if seasonal is None:
        seasonal = 1 if seasons.for_zone(NBL_DIR, q, zone, gid=gid) else 0
    ub = user_bin(gid, lid, raw, area, entry, st=st)
    lidh = struct.pack("<H", lid) + bytes(18)
    seq = [
        (packet(0x0205, lidh + g(gid) + bytes(8) + be("ffffffff") + struct.pack("<IIIIH", zone, mp, entry, nb, lid)
                + bytes([0, seasonal])), f"0x0205 load map {zone}/{mp}/{entry} (nb {nb})"),
        (packet(0x100E, be("ffffffbf") + bytes(16) + be("00011300") + struct.pack(">I", gid) + bytes(8)
                + bytes([1, 0]) + struct.pack("<HHHI", zone, mp, entry, q)
                + "dammy".encode("utf-16-le").ljust(64, b"\0") + bytes(4) + be("ffffffff") + bytes(4)), "0x100e location"),
    ]
    if zone_change:
        seq.append((packet(0x1212, struct.pack("<H", 0xFFFF) + bytes(18) + g(gid) + bytes(8) + bytes(2400)), "0x1212"))
    seq.append((packet(0x0201, struct.pack("<H", lid) + bytes(2) + be("00001200") + struct.pack("<I", gid) + bytes(8) + g(gid)
                       + bytes(8) + ub + bytes([0, 0, 0, 0]) + bytes(76)), "0x0201 character"))
    if zone_change:
        seq.append((packet(0x0A06, struct.pack("<H", lid) + bytes(6) + struct.pack("<I", gid) + bytes(8) + g(gid) + bytes(8)
                           + items.uuids(st.get("inv", []) if st else [])), "0x0a06 item uuids"))
    seq += [
        (packet(0x0208, lidh + g(gid) + bytes(8) + struct.pack("<I", nb + 1)), f"0x0208 finish loading (nb {nb + 1})"),
        (packet(0x0236, lidh + g(gid) + bytes(8)), "0x0236 start zone"),
    ]
    return seq


COUNTER_QUEST = 0x7FFFFFFF      # EGS priv/quests/0: the quest every counter loads (built as quest-0.nbl)


def counter_sequence(gid, raw, counter_id, nb, lid=0, st=None):
    """EGS counter_enter (0x0811): quest 0 + an empty zone, the counter as the location (0x100E with the
    counter id), 0x0215 x2, 0x020C, 0x1202/0x1204/0x1206/0x1207, 0x1212, 0x0201, 0x0A06, 0x0208, 0x0236."""
    area = (COUNTER_QUEST, 0, 0)
    with open(nbl_path(0), "rb") as f:
        quest_nbl = f.read()
    zone_data = bytes(2000)          # EGS: "doing like official just in case"
    ub = user_bin(gid, lid, raw, area, 0, st=st)
    lidh = struct.pack("<H", lid) + bytes(18)
    ffff = struct.pack("<H", 0xFFFF) + bytes(18)
    head = ffff + g(gid) + bytes(8)
    chunk = b"\xff" * 20 + bytes(28) + be("0000ffff") + be("ff000000") + be("64000a00")
    ctype = 2 if counter_id == 0xFFFFFFFF else 1
    name = "Counter".encode("utf-16-le").ljust(64, b"\0")
    return with_inventory([
        (packet(0x0C00, lidh + g(gid) + bytes(8) + struct.pack("<I", COUNTER_QUEST) + b"\xff" * 128), "0x0c00 quest init counter"),
        (c020e_quest(quest_nbl), f"0x020e counter quest nbl ({len(quest_nbl)} B)"),
        (packet(0x0A05, lidh + g(gid) + bytes(8)), "0x0a05"),
        (packet(0x010D, lidh + g(gid) + bytes(8) + struct.pack("<I", 1) + bytes(4) + be("00000300") + be("ffff0000")
                + bytes(4) + struct.pack("<I", gid) + bytes(24) + struct.pack("<II", gid, lid) + be("ffffffff")
                + ub[80:] + bytes(4)), "0x010d character"),
        (packet(0x0200, lidh + g(gid) + bytes(8) + struct.pack("<HHI", lid, 0, 1) + be("ffffffff")
                + be("06000500") + be("01000000") + bytes(8) + be("00040000") + be("00010000") + be("00140000")
                + be("ffffffff") + be("ffffffff")), "0x0200 zone init (mission)"),
        (packet(0x020F, struct.pack("<H", 0xFFFF) + bytes(34) + bytes([0, 255]) + bytes(2)
                + struct.pack("<I", len(zone_data)) + zone_data), "0x020f empty zone"),
        (packet(0x0205, lidh + g(gid) + bytes(8) + be("ffffffff") + struct.pack("<IIIIH", 0, 0, 0, nb, lid)
                + bytes([0, 0])), f"0x0205 load counter (nb {nb})"),
        (packet(0x100E, be("ffffffbf") + bytes(16) + be("00011300") + struct.pack(">I", gid) + bytes(8)
                + bytes([1, 0]) + bytes(6) + be("ffffff7f") + name + bytes(4) + struct.pack("<II", counter_id, ctype)),
         f"0x100e counter {counter_id:#x}"),
        (packet(0x0215, lidh + g(gid) + bytes(8) + struct.pack("<I", 0)), "0x0215"),
        (packet(0x0215, lidh + g(gid) + bytes(8) + struct.pack("<I", 0)), "0x0215"),
        (packet(0x020C, be("ffff0000") + bytes(32), channel=2), "0x020c"),
        (packet(0x1202, head + bytes(4) + be("10000000") + bytes(8) + be("14000000") + bytes(4)), "0x1202"),
        (packet(0x1204, lidh + g(gid) + bytes(12) + be("20000000") + bytes(32)), "0x1204"),
        (packet(0x1206, head + bytes(4) + be("80020000") + bytes(640)), "0x1206"),
        (packet(0x1207, head + chunk * 6), "0x1207"),
        (packet(0x1212, struct.pack("<H", 0xFFFF) + bytes(18) + g(gid) + bytes(8) + bytes(2400)), "0x1212"),
        (packet(0x0201, struct.pack("<H", lid) + bytes(2) + be("00001200") + struct.pack("<I", gid) + bytes(8) + g(gid)
                + bytes(8) + ub + bytes([0, 0, 0, 0]) + bytes(76)), "0x0201 character"),
        (packet(0x0A06, struct.pack("<H", lid) + bytes(6) + struct.pack("<I", gid) + bytes(8) + g(gid) + bytes(8)
                + items.uuids(st.get("inv", []) if st else [])), "0x0a06 item uuids"),
        (packet(0x0208, lidh + g(gid) + bytes(8) + struct.pack("<I", nb + 1)), f"0x0208 finish loading (nb {nb + 1})"),
        (packet(0x0236, lidh + g(gid) + bytes(8)), "0x0236 start zone"),
    ], gid, st)
