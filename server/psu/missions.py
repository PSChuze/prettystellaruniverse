"""Missions from the disc's offline free missions (PS2 client layouts; offsets are body offsets).

Data: $PSU_NBL_DIR/missions.json + counter-table-N.nbl + quest-<id>.nbl + zone-<id>-<zone>.nbl, built by
extract/missiondata.py from the disc (never committed). Free mission ids are 110000 + 10 * FM number;
disc quest NBLs are served as-is (the client takes the quest id from the quest.snr it receives).

  C>S 0x0C05 counter files -> S>C 0x0C06 (0x236190): +0x24 u8 1, +0x26 s16 N options, +0x28 u32 NBL size,
          +0x2C N option bytes (pad 4), then the counter table NBL (table.rel + text.bin + one quest NBL per
          mission). N = groups + quests; 0 hidden, 2 greyed, 3 selectable.
          AotI (0x238a70) takes EGS's (PC) pack instead: +0x24 u8 1, +0x28 s32 pos[128], +0x228 u32 size[128],
          +0x428 u32 file count, +0x42C the files back to back: 0 = the counter table NBL (table.rel, text.bin),
          1.. = one quest NBL per table quest (pos < 0: none, its slot stays empty). No option bytes: 0x0C10
          (0x238950, same layout as PSU's) brings them.
  C>S 0x0C0F -> S>C 0x0C10 (0x2360e0): +0x24 u8 1, +0x26 s16 N, +0x28 options.
  C>S 0x0C01 accept (+0x24 quest id) -> 0x1020 (clear), 0x1015 (+0x24 id, 01 01 00 00, id, size, quest NBL:
          "Mission start" banner), 0x0C02 (+0x24 s8 0 accepted / 4 prerequisites / 5 gather / other rejected).
  C>S 0x0807 quest = mission, zone/map/entry = ffff -> the quest's enter warp; the zone change uses the
          "mission" 0x0200 variant. After the client acks the load: S>C 0x0C09 (0x1696e0) event 0 = start.
  S>C 0x0C09: +0x24 s8 event (0 start, 1 clear, 2 fail), +0x25 results flag, +0x28 u32 time.
Mixed parties (PSU and AotI players): a mission belongs to the disc whose missions.json lists it (AotI ids
  300000.. have no PSU quest NBL; PSU's 110000.. quest NBLs are a quest.snr version AotI cannot parse), so
  only members on the same disc as the accepting leader take part (playable()). Members of the other disc
  at the counter get 0x0C02 4 (prerequisites) instead of 0x1020/0x1015/0x0C02 0, and their 0x0807 after
  the leader into the mission is not taken as a departure (it bounces back to entry 0 of where they are).
  Each disc's counters list only that disc's missions (data() per session), so the rule only bites when the
  leader departs.
  Leaving: an 0x0807 outside the mission (quest 0x7ffffffe...) or C>S 0x0C0E -> 0x1006 event 10 (end) /
          12 (cleared silently), then the zone change back to where the counter was entered from.
AotI Episode 2 story (extract/missiondata.py --game aoti; 10 chapters nn = 13..22, two acts, difficulty C/B/A):
  the counter id is the goal id of the set object (type 27, params +0) the player walks into, and counter N
  is disc table N. Counter 115 = the GUARDIANS HQ in the Clyez lobby (1100000 zone 0 map 5; the
  client sends 0x0811 / 0x1710 / 0x0C05 with 115 and hangs without a pack): table 0115, the first acts
  2nn100 + v. Each chapter's base is the lobby quest 2nn000 (missions.json "bases") whose base counter 121,
  123, .., 139 opens table 01xx, the second acts 2nn110 + v (their warp tables key the arrival on that base
  counter). The first acts exit to the offline HQ (200000/2/5/2) and nothing on the disc's online path leads
  to a base, so leaving a cleared first act goes to its chapter's base (story_next); leaving a second act goes
  back to the base it was taken from. In a base, the other goal (counter 0 or 120, 122, ..) warps to map 0,
  whose entr_unit0000 runs cwBack_To_City: its warps name the offline lobby 200000 (area.resolve maps it to
  the city); a 0x0811 for a base counter without a table goes to the HQ (STORY_HOME) as well.
  Progression: every chapter is offered (SAVE_QEST_ID = PSU_AOTI_CHAPTER, default 21); clears are recorded
  per character in state["story"] {quest id: best rank} (record_clear).
AotI Episode 3 (online-only, not on either disc; served only when a host stages it in $PSU_NBL_EXTRA): acts 2nn100 + v
  with nn = 23.. (chapter 23 = Ep3 Ch1, act 1 = 223100/223101/223102) and their bases 2nn000 (223000 Block A
  Refugee Camp) are story missions like Episode 2's (missions.json "story" and "bases", story_next, record_clear);
  the staged missions.json extends the disc's (data(): missions and bases added, counters replaced, so counter 115
  offers the staged table: Ep2's three groups, then one group per Ep3 difficulty holding each chapter's first act in
  order, then the side stories'). The extension is AotI only (it lives in <dir>/aoti/). Chapters 23..30 = Ep3 Ch1..8
  (base 2nn000 with goals 120 + 2 (nn - 13) the way back and + 1 the act 2 counter = table 01xx, 140/141 .. 154/155),
  side stories 41 The Magashi Plan (241100..) and 42 Innocent Girl (242100..), no base.
  Order per character (unlocked): Ch1 Act 1 from the start, Act 2 after Act 1, each chapter after the previous one's
  Act 2, Innocent Girl from the start, The Magashi Plan after Ch5 Act 2; a locked act is greyed in 0x0C10 (option 2)
  and its accept answered 0x0C02 4 (prerequisites). Story flags: the acts borrow Ep2's spare S13..S22_FLAG_20..38,
  kept per character (charload.story_flag); subplot gates are answered by the server
  (STORY_GATES) and per-run choices forgotten on accept (RUN_FLAGS, start_run).
"""
import json
import os
import struct

from psu import games
from psu.area import NBL_DIR, NBL_EXTRA, nbl_file
from psu.charload import g
from psu.login import packet

_M = {}                             # game -> missions.json ($PSU_NBL_DIR/<game>/ first)
COUNTER_TABLES = {}                 # client counter id -> disc table where they differ (0 Linear Line, 1 Space Docks)


def data(game=None):
    """The session's disc's mission index: AotI's quest ids (300000 + 100 * FM + 10 * difficulty + variant)
    and counter tables are its own, so an aoti/missions.json wins over the shared (PSU-built) one. A missions.json
    in a $PSU_NBL_EXTRA dir (for that game) extends it: its missions and bases are added, its counters replace
    the disc's (the staged counter table lists the disc's quests too)."""
    game = game or games.current()
    if game not in _M:
        p = games.data_file(NBL_DIR, "missions.json", game)
        d = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"counters": {}, "missions": {}}
        for root in NBL_EXTRA:
            ext = games.data_file(root, "missions.json", game)
            if os.path.exists(ext):
                with open(ext, encoding="utf-8") as f:
                    x = json.load(f)
                d["missions"].update(x.get("missions", {}))
                d["counters"].update(x.get("counters", {}))
                if x.get("bases"):
                    d.setdefault("bases", {}).update(x["bases"])
        _M[game] = d
    return _M[game]


def mission(qid, game=None):
    return data(game)["missions"].get(str(qid))


OTHER_DISC = 4                      # 0x0C02 result for party members whose disc lacks the mission
PREREQUISITES = 4                   # 0x0C02 result for a story act this character has not opened (unlocked)


def playable(qid, game):
    """The quest is on that disc (its own missions.json lists it): see Mixed parties above."""
    return mission(qid, game) is not None


def table_for(counter):
    return COUNTER_TABLES.get(counter, counter if str(counter) in data()["counters"] else None)


AOTI_LAYOUTS = {int(x) for x in os.environ.get("PSU_AOTI_LAYOUTS", "0").split(",") if x.strip()}


def shown(q):
    """AotI's disc tables list every map layout of a mission (quest id 300000 + 100 * FM + 10 * layout + rank:
    layouts 0..2 start on different maps), so each mission would show three times. Only the
    PSU_AOTI_LAYOUTS layouts (default 0) are offered; PSU ids are not affected."""
    return games.current() != games.AOTI or q < 300000 or (q // 10) % 10 in AOTI_LAYOUTS


STORY_HOME = ((1100000, 0, 5), 2)  # the Clyez lobby's HQ map: the first acts' exit 200000/2/5/2 in online numbers


def story(qid):
    """{"chapter", "act" ("A" first / "B" second), "base" (lobby quest 2nn000)} for an Episode 2 / 3 story mission."""
    return (mission(qid) or {}).get("story")


def base(qid):
    """The story base lobby quest 2nn000 (missions.json "bases": enter [zone, map, entry], zones, counters)."""
    return data().get("bases", {}).get(str(qid))


def base_counter(qid, counter):
    """In base qid: the disc table counter `counter` opens, or None for its way back (or an unknown counter)."""
    b = base(qid)
    return next((c[3] for c in (b or {}).get("counters", []) if c[2] == counter), None)


def story_next(qid, cleared):
    """Where leaving story mission qid goes instead of its origin: a cleared first act -> its chapter's base
    entry ((quest, zone, map), entry); otherwise None (the origin)."""
    st = story(qid)
    if not st or st["act"] != "A" or not cleared:
        return None
    b = base(st["base"])
    if not b:
        return None
    z, m, e = b["enter"]
    return (st["base"], z, m), e


def record_clear(state, qid, rank):
    """Per-character story progress: state["story"][quest id] = the best rank (0..3) cleared."""
    if state is None or not story(qid):
        return False
    done = state.setdefault("story", {})
    done[str(qid)] = max(rank, done.get(str(qid), -1))
    return True


_ACTS = {}                          # game -> {(chapter, act): [quest id str]}


def story_acts(chapter, act):
    """The quest ids (C/B/A) of story chapter `chapter` act `act` on the session's disc."""
    game = games.current()
    if game not in _ACTS:
        idx = {}
        for q, m in data()["missions"].items():
            st = m.get("story")
            if st:
                idx.setdefault((st["chapter"], st["act"]), []).append(q)
        _ACTS[game] = idx
    return _ACTS[game].get((chapter, act), [])


def story_done(state, chapter, act, rank=0):
    """Has this character cleared story chapter `chapter` act `act` (any difficulty) at `rank` or better?"""
    done = (state or {}).get("story", {})
    return any(done.get(q, -1) >= rank for q in story_acts(chapter, act))


EP3_FIRST = 23                      # Ep3 Ch1 (story chapter 23); Ep2 is 13..22 and stays open (PSU_AOTI_CHAPTER)
SIDE_AFTER = {41: (27, "B")}        # side story chapter -> the story act that opens it (The Magashi Plan after Ch5
                                    # Act 2 "Ambition's End 2"; Innocent Girl, 42, from the start)


def unlocked(state, qid):
    """Episode 3 order per character (state["story"], record_clear): Ch1 Act 1 from the start, each Act 2 after its
    Act 1 (it is offered at the base that Act 1's clear leads to), each Act 1 after the previous chapter's Act 2,
    side stories by SIDE_AFTER. Free missions and Episode 2 are always open. No state (fixtures, tools): open."""
    st = story(qid)
    if not st or st["chapter"] < EP3_FIRST or state is None:
        return True
    ch, act = st["chapter"], st["act"]
    if ch in SIDE_AFTER or ch > 40:
        need = SIDE_AFTER.get(ch)
        return need is None or story_done(state, *need)
    if act == "B":
        return story_done(state, ch, "A")
    return ch == EP3_FIRST or story_done(state, ch - 1, "B")


# Ep3 story flags: the acts borrow Ep2's spare S13..S22_FLAG_20..38 (CharaSaveWorkFlag.snr has
# no S23..S30 names; the disc scripts use only _00.._09 and _CLR of each outside the debug menu and the one-time
# local reset lwInitIlluminusWorkFlag). Subplot gates are never set by a script: the server answers their get.
STORY_GATES = {
    # Ch1 Act 1 Lou subplot: all of Episode 2, Ch4 (Farewell to Mother) and this act cleared at S
    "S13_FLAG_23": lambda st: (all(story_done(st, c, "B") for c in range(13, 23)) and story_done(st, 26, "B")
                               and story_done(st, 23, "A", 3)),
    # Ch3 Act 2 Ethan / Lumia talk: Ch5 (Ambition's End) cleared and this act cleared at S
    "S15_FLAG_25": lambda st: story_done(st, 27, "B") and story_done(st, 25, "B", 3),
    # Ch4 Act 2 "Take Lou": the Ch1 Act 1 Lou event seen (S13_FLAG_22) and Ch4 Act 1 cleared at S
    "S16_FLAG_25": lambda st: bool(st.get("chrflags", {}).get("S13_FLAG_22")) and story_done(st, 26, "A", 3),
    # Ch6 Act 1 subplot 1 ("Stay up listening"): the Ch3 Act 2 talk seen (S15_FLAG_24) and Ch6 Act 1 cleared at S
    "S18_FLAG_25": lambda st: bool(st.get("chrflags", {}).get("S15_FLAG_24")) and story_done(st, 28, "A", 3),
    # Ch6 Act 2 subplot 2 (the third answer): subplot 1 watched (S18_FLAG_22) and Ch7 (Road to Rykros) cleared
    "S18_FLAG_26": lambda st: bool(st.get("chrflags", {}).get("S18_FLAG_22")) and story_done(st, 29, "B"),
}
RUN_FLAGS = {                       # per-run choices: forgotten when the act is accepted (a write of 0 is a get)
    (26, "B"): ("S16_FLAG_24",),    # Ch4 Act 2: Lou taken instead of Curtz
    (28, "B"): ("S18_FLAG_27",),    # Ch6 Act 2: Hyuga and Maya instead of Ethan and Karen
}


def story_gate(state, name):
    """The server's answer to a get of a gate flag (1 / 0), or None for a flag the character keeps itself."""
    g = STORY_GATES.get(name)
    return None if g is None or state is None else int(bool(g(state)))


def start_run(state, qid):
    """On accepting a story act: forget its per-run flags. Returns the names dropped."""
    st = story(qid)
    names = RUN_FLAGS.get((st["chapter"], st["act"]), ()) if st and state is not None else ()
    flags = (state or {}).get("chrflags", {})
    return [n for n in names if flags.pop(n, None) is not None]


def options(table, state=None):
    """0x0C10 option bytes: groups 3, quests 3 selectable / 2 greyed (an Ep3 act this character has not opened yet,
    unlocked()) / 0 hidden (not on this disc, other AotI layouts)."""
    c = data()["counters"][str(table)]
    return bytes([3] * c["groups"] + [(3 if unlocked(state, q) else 2) if mission(q) and shown(q) else 0
                                      for q in c["quests"]])


def hdr(gid, lid=0xFFFF):
    return struct.pack("<H", lid) + bytes(18) + g(gid) + bytes(8)


def aoti_pack(table, nbl):
    """EGS counter pack (egs_files load_counter_pack layout) for AotI's 0x0C06: the table NBL as file 0, then
    each quest of the table from its own quest-<id>.nbl (missing or not in missions.json: pos -1, size 0)."""
    pos, sizes, files, off = [0], [len(nbl)], [nbl], len(nbl)
    for q in data()["counters"][str(table)]["quests"][:127]:
        p = nbl_file(f"quest-{q}.nbl")
        if mission(q) and os.path.exists(p):
            with open(p, "rb") as f:
                files.append(f.read())
            pos.append(off)
            sizes.append(len(files[-1]))
            off += sizes[-1]
        else:
            pos.append(-1)
            sizes.append(0)
    n = len(pos)
    return (struct.pack(f"<{n}i", *pos) + bytes(4 * (128 - n)) + struct.pack(f"<{n}I", *sizes) + bytes(4 * (128 - n))
            + struct.pack("<I", n) + b"".join(files))


def c0c06(gid, table):
    with open(nbl_file(f"counter-table-{table}.nbl"), "rb") as f:
        nbl = f.read()
    if games.current() == games.AOTI:
        return packet(0x0C06, hdr(gid) + struct.pack("<I", 1) + aoti_pack(table, nbl))
    opts = options(table)
    pad = opts + bytes((-len(opts)) % 4)
    return packet(0x0C06, hdr(gid) + struct.pack("<BBhI", 1, 0, len(opts), len(nbl)) + pad + nbl)


def c0c10(gid, table, state=None):
    opts = options(table, state)
    return packet(0x0C10, hdr(gid) + struct.pack("<BBh", 1, 0, len(opts)) + opts)


def c1020(gid):
    return packet(0x1020, hdr(gid))


def c1015(gid, qid, game=None):
    """Same layout on both discs (PSU 0x330a40, AotI 0x32c960: +0x28 u8 1, +0x2C id, +0x30 size, NBL +0x34).
    game: the recipient's disc when relayed to a party member (the quest NBL is that disc's)."""
    with open(nbl_file(f"quest-{qid}.nbl", game), "rb") as f:
        nbl = f.read()
    return packet(0x1015, hdr(gid) + struct.pack("<I", qid) + bytes([1, 1, 0, 0]) + struct.pack("<II", qid, len(nbl)) + nbl)


def c0c02(gid, result=0):
    return packet(0x0C02, hdr(gid) + bytes([result & 0xFF, 0, 0, 0]))


def c0c09(gid, event, time_=0, lid=0xFFFF):
    return packet(0x0C09, hdr(gid, lid) + bytes([event, 0, 0, 0]) + struct.pack("<I", time_))


def maps(qid):
    m = mission(qid)
    return {int(z): set(ms) for z, ms in m["zones"].items()} if m else {}
