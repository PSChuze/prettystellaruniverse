"""Character records and the character-load sequence (game service).

Layouts from EGS (github.com/essen/egs, apps/egs_net/src/egs_net.erl and apps/egs/src/egs_proto.erl;
protocol docs public domain, code AGPL; re-implemented from those descriptions). Words EGS writes as
`16#xxxxxxxx:32` are BIG-endian on the wire; `?l32` fields are little-endian.

A character is kept as the raw 0x0D02 body the client sent (game.py stores it). The creation
packet's appearance block has the same 97-byte layout as the one inside a character record, so
it is copied straight across rather than re-encoded field by field.
"""
import os
import re
import struct
import time

from psu import games, items, progression, xgame

from psu.login import packet

ACC = bytes.fromhex("00011300")          # BE marker before a GID in 0x0Dxx / 0x1xxx replies
CLASS_BY_RACE = {0: 15, 1: 14, 2: 13, 3: 12}   # EGS/PC numbering (human acro .. beast hunter): unused on PS2
CLASS = 0x6A                                    # the class the PS2 client chose at creation (0x0D02 body)
APPEARANCE = slice(0x6B, 0x6B + 97)      # in the 0x0D02 body: after slot, name, race, gender, 1 byte
DEFAULT_OPTIONS = bytes([0] * 16 + [4, 0, 0, 0])   # EGS egs_char:new, brightness 4


def g(gid):
    return ACC + struct.pack("<I", gid)


def target_hdr(target=0xFFFF):
    """DestTargetID l16 + 18 zero bytes: the common opening of these commands."""
    return struct.pack("<H", target) + bytes(18)


def char_bin(raw, st=None, game=None):
    """276-byte character: name[64] race gender class appearance[97] levels[112] (EGS character_to_binary);
    class and the level block come from the saved progression state (psu/progression.py) when given. game =
    the disc that reads it (default this session's): a PSU client is shown an AotI-only class as its PSU
    stand-in (progression.visible_class); records built for another player's client should pass theirs."""
    race, gender = raw[0x68], raw[0x69]
    if st is not None:
        levels, cls = progression.level_block(st, game), progression.visible_class(st["cls"], game)
    else:
        levels = (struct.pack("<IH", 1, 0) + bytes([3]) + bytes(5)          # level 1, blast bar 0, luck 3
                  + struct.pack("<I", 0) + bytes(4) + struct.pack("<II", 0, 0)  # exp, -, money, playtime
                  + bytes(20) + struct.pack("<12I", *([1] * 12)) + struct.pack("<4I", 1, 1, 1, 1))
        cls = progression.visible_class(raw[CLASS], game)
    appearance = raw[APPEARANCE]
    if (game or games.current()) == games.PSU:
        # Accounts are shared by both discs: an AotI-made character loaded on the PSU disc (its own 0x0D03 / 0x0D01 /
        # 0x0201) must not carry AotI-only parts or voices either (PSU's part lookup has no NULL check). The check
        # only replaces words PSU's disc lacks, so a PSU-made appearance is unchanged.
        appearance = xgame.appearance_for(appearance, race, gender, games.PSU, games.AOTI)
    out = raw[0x28:0x68] + bytes([race, gender, cls]) + appearance + levels
    assert len(out) == 276, len(out)
    return out


def slot_record(raw, st=None):
    """348-byte 0x0D03 slot (EGS account_character_to_binary): 00 01 00*6 | character | 00*64."""
    return b"\x00\x01" + bytes(6) + char_bin(raw, st) + bytes(64) if raw else bytes(348)


CHRWORK = 0x1C0 - 0x140   # 0x0D01: flag bits at packet +0x140, 222 u32 character-work values at +0x1C0
SAVE_LC = 170             # chrwork index of SAVE_LC00 (disc DE_SAVEWORKFLAG.NBL, CharaSaveWorkFlag.snr)


def lobby_counter_work(origin_quest, written=None):
    """SAVE_LC00..05 for the Spaceport's lobby-counter script (native zone 10, read with work.chrwork_get):
    LC00..03 offer Colony/Parum/Neudaiz/Moatoob, LC04 = where the player came from (0/1/2 = the Colony's
    Parum/Neudaiz/Moatoob gate, 3/4/5 = Parum/Neudaiz/Moatoob; picks the default planet, the cancel target
    and the preloaded shuttle movie), LC05 != 0 skips the district submenu. The native Colony gate goals
    write them with 0x0D04 just before the 0x0807 (`written`, index -> value); they are kept (all planets
    offered). The script resets all six to -1 on every visit, so they are sent before each entry."""
    if games.current() == games.AOTI and origin_quest == 1100000 and written and all(i in written for i in range(6)):
        # AotI's Spaceport (zone10 cw_Lobbycounter_Load_RTM) preloads only the shuttle movie of the gate's planet
        # (LC04 0/1/2 -> Parum/Neudaiz/Moatoob) and movie.play_rtm waits forever on any other, so offering
        # every planet hangs the client (Moatoob gate -> Neudaiz). Keep what its own gate wrote, except
        # LC05 = 1 as for PSU: with 0 the district submenu (cw_LC_Select_Dagora etc.) returns -1 and the
        # counter cancels to the Colony after the shuttle movie.
        return {SAVE_LC + i: (1 if i == 5 else written[i]) for i in range(6)}
    gate = {1101000: 3, 1102000: 4, 1103000: 5}.get(origin_quest)
    if gate is None:
        gate = (written or {}).get(4, 0)
        gate = gate if gate in (0, 1, 2) else 0
    return {SAVE_LC + i: v for i, v in enumerate((1, 1, 1, 1, gate, 1))}


def c0d04_ack(gid, body):
    """Reply to a client 0x0D04 (work.chrwork_set / chrflag_set and friends): its body echoed with the status
    byte (body +0x36, after the 16-byte name and two type bytes) set to 1, as EGS answers the PC client.
    AotI's handler (ERX 0x1b5cf0, group 0x0D table 0x54ee90) matches it to the pending request by the type
    byte (+0x34) and completes the script's wait object with that status. Without it AotI waits 9000 frames
    (0x233910: 150 s) and then pushes -1 onto the script stack; PSU's timeout is 450 frames."""
    if len(body) < 0x40:
        return None
    return packet(0x0D04, bytes(20) + g(gid) + bytes(8) + body[0x24:0x36] + b"\x01" + body[0x37:0x40])


STORY_FLAG = re.compile(r"S(1[3-9]|2[0-9])_FLAG_([0-9]{2}|CLR)$")


def story_flag(state, body, gate=None):
    """Online story flags: work.on_chrflag_set / on_chrflag_get (syscalls 287 / 286, both wait for the 0x0D04
    answer) name a CharaSaveWorkFlag.snr flag (16-byte name at +0x24, value at +0x3C). Writes of S13..S29 story
    flags with a value are kept per character in state["chrflags"]; a request with value 0 (a get: Ep2's "skip to
    just before the trial?" prompt, cw_Is_Opening_Skip, or the Ep3 acts' flags) is answered with the kept value, or
    with gate(name) when that is not None (the Ep3 subplot gates the server decides, missions.story_gate). Returns
    (body to acknowledge, True when state changed)."""
    if state is None:
        return body, False
    name = body[0x24:0x34].split(b"\0")[0].decode("latin1", "replace")
    if not STORY_FLAG.match(name):
        return body, False
    v, = struct.unpack_from("<i", body, 0x3C)
    flags = state.setdefault("chrflags", {})
    g = gate(name) if gate and not v else None
    if g is not None:
        return body[:0x3C] + struct.pack("<i", g) + body[0x40:], False
    if v:
        if flags.get(name) == v:
            return body, False
        flags[name] = v
        return body, True
    if name in flags:
        return body[:0x3C] + struct.pack("<i", flags[name]) + body[0x40:], False
    return body, False


SAVE_QEST_ID = 176        # chrwork index (both discs' CharaSaveWorkFlag.snr): AotI's story chapter, 13..21
AOTI_CHAPTER = int(os.environ.get("PSU_AOTI_CHAPTER", "21"))


def c0d01_character(gid, raw, options=None, chrwork=None, st=None):
    if games.current() == games.AOTI:
        # AotI's lobby NPCs (zone02 cwIlluminusNpcTalk) pick their lines by SAVE_QEST_ID = the last offline story
        # chapter (13..21); online it is 0, no case matches and they say nothing.
        chrwork = {SAVE_QEST_ID: AOTI_CHAPTER, **(chrwork or {})}
    block = bytearray(1016)
    for index, value in (chrwork or {}).items():
        struct.pack_into("<i", block, CHRWORK + 4 * index, value)
    return packet(0x0D01, target_hdr() + g(gid) + bytes(8) + char_bin(raw, st) + bytes(block)
                  + (options or DEFAULT_OPTIONS))


def c1006_party_event(gid, event_id, party_pos=0):
    return packet(0x1006, target_hdr() + g(gid) + bytes(8) + bytes([event_id, party_pos, 0, 0]))


def c1005_own_party(gid, raw, level=1, hp=100, max_hp=100):
    be = bytes.fromhex
    member = be("0100ffff") + be("0000ff00") + be("ffff0000") + bytes(80) + be("ffffffff")
    body = (target_hdr() + g(gid) + bytes(8)
            + be("00000100") + bytes(4) + be("ffffffff") + bytes(4) + be("00011200") + struct.pack("<I", gid) + bytes(8)
            + raw[0x28:0x68] + bytes([level]) + bytes(2) + bytes([1]) + be("01010000") + bytes(4) + bytes(64)
            + be("ffffffff") + bytes(12) + be("ffffffff") + bytes(8) + struct.pack("<II", hp, max_hp) + bytes(80)
            + (member + bytes(96)) * 4 + member + bytes(56)
            + be("ffffffff") + bytes(4) + be("ff020000") + be("ffff0000") * 6 + bytes(460))
    return packet(0x1005, body)


def c0210_time(gid, lid=0):
    return packet(0x0210, struct.pack("<H", lid) + bytes(18) + g(gid) + bytes(12) + struct.pack("<I", int(time.time())))


def c0222_universe(gid, uni_id=1, name="Universe 1", players=1, max_players=1000, level_cap=100):
    nm = name.encode("utf-16-le")[:44].ljust(44, b"\0")
    return packet(0x0222, struct.pack("<H", 0xFFFF) + bytes(2) + bytes.fromhex("00001200") + struct.pack("<I", gid)
                  + bytes(8) + g(gid) + bytes(8) + struct.pack("<IHH", uni_id, players, max_players) + nm
                  + struct.pack("<I", level_cap))


def _universes():
    """PSU_UNIVERSES="1:Universe 1,2:Universe 2" -> [(1, "Universe 1"), (2, "Universe 2")]."""
    spec = os.environ.get("PSU_UNIVERSES", "1:Universe 1,2:Universe 2,3:Universe 3")
    out = []
    for part in spec.split(","):
        uid, _, name = part.partition(":")
        if uid.strip().isdigit():
            out.append((int(uid), name.strip() or f"Universe {uid.strip()}"))
    return out or [(1, "Universe 1")]


UNIVERSES = _universes()               # (id, name); the Uni-cube list and 0x0222
UNIVERSE_NAMES = dict(UNIVERSES)


def c021e_universe_list(unis=None, counts=None):
    """Uni-cube list (EGS send_021e): per universe id l32, players l16, style l16 (0x283 = left aligned
    with the population on the right), UTF-16 name in 32 bytes, "%5d" population in UTF-16 + 0:16.
    counts: {universe id: players online}."""
    unis, counts = unis or UNIVERSES, counts or {}
    out = b""
    for uni_id, name in unis:
        n = counts.get(uni_id, 0)
        out += (struct.pack("<IHH", uni_id, n, 0x283) + name.encode("utf-16-le")[:32].ljust(32, b"\0")
                + f"{n:5d}".encode("utf-16-le") + bytes(2))
    return packet(0x021E, bytes(36) + struct.pack("<I", len(unis)) + out)


def c0230_leave_area(gid):
    """EGS send_0230: sent on a universe change, before the character is loaded again."""
    return packet(0x0230, struct.pack("<H", 0xFFFF) + bytes(2) + g(gid) + bytes(8) + g(gid) + bytes(8))


def c1500_own_card(gid, raw, slot, comment=""):
    race, gender = raw[0x68], raw[0x69]
    voice_type, voice_pitch = raw[APPEARANCE][0], raw[APPEARANCE][1]
    cm = comment.encode("utf-16-le")[:352].ljust(352, b"\0")
    return packet(0x1500, struct.pack("<H", 0xFFFF) + bytes(18) + g(gid) + bytes(8) + raw[0x28:0x68]
                  + bytes([race, gender, raw[CLASS], voice_type, voice_pitch]) + bytes(3)
                  + struct.pack("<I", gid) + bytes(28) + cm + bytes([1, 4, 1, slot]) + bytes(8))


def c1501_partner_cards(gid):
    return packet(0x1501, target_hdr() + g(gid) + bytes(12))


def c1512_blacklist(gid):
    return packet(0x1512, target_hdr() + g(gid) + bytes(5768))


def c1602_npcs(gid, pm_name="My PM"):
    pm = pm_name.encode("utf-16-le")[:64].ljust(64, b"\0")
    return packet(0x1602, target_hdr() + g(gid) + bytes(12) + bytes(344) + bytes([0]) + bytes(3) + pm + bytes(4))


def c021b_load_end(gid, lid=0):
    return packet(0x021B, struct.pack("<H", lid) + bytes(18) + g(gid) + bytes(8))


def char_load_sequence(gid, raw, slot, options=None, st=None, uni=None, players=1):
    """EGS egs_game:char_load order: character, inventory (from the saved state), party, time, ..."""
    seq = [(c0d01_character(gid, raw, options, st=st), "0x0d01 character")]
    hp, max_hp, level = 100, 100, 1
    if st is not None:
        seq.append((items.c0a0a_state(gid, st), f"0x0a0a inventory ({len(st.get('inv', []))})"))
        max_hp = progression.derived(raw, st)["max_hp"]
        hp, level = min(st.get("hp", max_hp), max_hp), st["level"]
    return seq + [
        (c1006_party_event(gid, 5), "0x1006 party event 5"),
        (c1005_own_party(gid, raw, level=level, hp=hp, max_hp=max_hp), "0x1005 own party info"),
        (c1006_party_event(gid, 12), "0x1006 party event 12"),
        (c0210_time(gid), "0x0210 time"),
        (c0222_universe(gid, uni or UNIVERSES[0][0], UNIVERSE_NAMES.get(uni or UNIVERSES[0][0], "Universe"),
                        players), "0x0222 universe"),
        (c1500_own_card(gid, raw, slot), "0x1500 own card"),
        (c1501_partner_cards(gid), "0x1501 partner cards"),
        (c1512_blacklist(gid), "0x1512 blacklist"),
        (c1602_npcs(gid), "0x1602 npcs"),
        (c021b_load_end(gid), "0x021b load end"),
    ]
