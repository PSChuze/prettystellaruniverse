"""What one disc's player data may show a player on the other disc (PSU SLUS-21194 <-> AotI SLUS-21631).

AotI ships everything PSU draws and more, so only an AotI player shown to a PSU recipient is translated;
PSU -> AotI, PSU -> PSU and AotI -> AotI bytes are never touched (tools/xgame_check.py).

Appearance (97-byte block at char_bin +0x43, raw 0x0D02 +0x6B): +0 voice type, +1 pitch, +5 six LE u32 part
words (slot << 24 | flags << 16 | number << 8 | variant; slots 0 upper/torso, 1 lower/legs, 2 shoes/arms,
3 ears, 4 face, 5 hair/head), +0x1D..+0x1F clothing colour bytes, then eyebrow/eyelash/eye bytes, colour XY
words and proportions.
  Part words: the client looks a word up in GA_PLYMAKECOMMON.NBL partsinfo.snr (root +8 -> {records, count};
    16-byte records {word, name, XF_PLYDRAWRESPARTS.AFS index, flags}, sorted by word) with a binary search
    (PSU 0x2c00a0 via 0xf1000) that returns NULL for an unknown word, and 10 of its 12 callers (0xe9b44,
    0xea020 ... 0xeb120) read the record's flags (+0xC) without a NULL check. PSU's table has 1025 words,
    AotI's 2059 (a superset: every PSU word, same file names; 1034 AotI-only: 489 hair, 165 per clothing
    slot, 33 faces, 17 ears). The sets are $PSU_GAMEDATA/parts.json (extract/partsdata.py); a word outside the
    recipient's set is replaced by its race/gender's first character-creation choice (selectable.snr), and
    ears/face/hair are replaced together (a cast's head type and its ears/face come as a set).
  Voice: PSU's player voices are SND_GB_{M,F}_* (types 1..50: flesh male 1..14, female 15..26, cast male
    27..38, cast female 39..50, the ranges EGS checks); AotI adds SND_IL_GB_* (76..101), absent from PSU's
    XA_SOUNDDATA.AFS. Mapped into the same race/gender group.
  Colour palettes (GA_PLYMAKECOMMONPAL.NBL, same members), eye/eyebrow/eyelash images and proportions are
    the same on both discs: passed through.
Classes: PSU's type names and rows stop at 9 Protranser; an AotI class (10..15) is shown as
  progression.visible_class's PSU stand-in (char_bin with the recipient's game; cards and 0x1516 here).
Items: an id outside PSU's items.json (or an AotI clothing id with a colour in byte 1's high nibble) is never
  given to a PSU player: hidden from its ground list, refused in a trade (item_ok).
Item values: AotI levels PAs to Lv50 (level byte 49) and keeps a weapon's grind-failure count in var_rec +0x1B's
  high nibble (0x299b4c). PSU's PA tables stop at Lv30 (29 thresholds per PA in its pa.json, GetSkillParam /
  GetBulletParam / GetTechParam index by the level byte) and +0x1B is its weapon's element byte, so every item
  record and PA list a PSU client reads is built for that client (items.var_rec / c0a0a / c0106_add with the
  recipient's game, growth.c0108): level bytes clamp to 29, the failure nibble is left out. Stored state keeps
  the real values (accounts are shared: an AotI-made character can be loaded on PSU, trades cross discs).
"""
import struct

from psu import games, progression

APPEARANCE = 0x43           # in char_bin: name[0x40] race gender class
PARTS = 5                   # part words in the appearance block
COLOURS = 0x1D              # clothing colour bytes for slots 0..2
NONE = 0xFFFFFFFF           # "no part" (a cast's ears with a head type)

# player voice groups (first, count) per (cast, gender): PSU ranges, AotI's additions
PSU_VOICES = {(0, 0): (1, 14), (0, 1): (15, 12), (1, 0): (27, 12), (1, 1): (39, 12)}
AOTI_VOICES = {(0, 0): (76, 8), (0, 1): (84, 5), (1, 0): (89, 8), (1, 1): (97, 5)}
CAST = 2


def visible_voice(voice, race, gender, recipient_game, owner_game=None):
    if recipient_game != games.PSU or owner_game == games.PSU:
        return voice
    first, n = PSU_VOICES[(1 if race == CAST else 0, gender & 1)]
    for afirst, an in AOTI_VOICES.values():
        if afirst <= voice < afirst + an:
            return first + (voice - afirst) % n
    return voice


def parts(game):
    """$PSU_GAMEDATA/[<game>/]parts.json: {"words": [...], "defaults": [race*2+gender][slot] word}; {} if absent."""
    return games.json_data("parts.json", game=game)


_SETS = {}


def part_set(game):
    t = parts(game)
    if not t.get("words"):
        return None
    if game not in _SETS:
        _SETS[game] = frozenset(t["words"])
    return _SETS[game]


def appearance_for(block, race, gender, recipient_game, owner_game=None):
    """The 97-byte appearance block as a `recipient_game` player may be sent it (unchanged when allowed)."""
    if recipient_game != games.PSU or owner_game in (None, games.PSU):
        return block
    b = bytearray(block)
    b[0] = visible_voice(b[0], race, gender, recipient_game, owner_game)
    allowed = part_set(recipient_game)
    if allowed is not None:
        defaults = parts(recipient_game).get("defaults", [])
        rg = race * 2 + gender
        dflt = defaults[rg] if 0 <= rg < len(defaults) else None
        words = list(struct.unpack_from("<6I", b, PARTS))
        bad = [w != NONE and ((w >> 24) != s or w not in allowed) for s, w in enumerate(words)]
        if any(bad[3:]):
            bad[3:] = [True] * 3
        if dflt:
            for s in range(6):
                if bad[s] and words[s] != dflt[s]:
                    words[s] = dflt[s]
                    if s < 3:
                        b[COLOURS + s] = 0
            struct.pack_into("<6I", b, PARTS, *words)
    return bytes(b)


def char_for(cb, recipient_game, owner_game):
    """char_bin (276 B) of an `owner_game` player for a `recipient_game` player: the appearance block (the class
    byte is char_bin's, progression.visible_class). owner_game None (a player's own record) is never changed."""
    if recipient_game != games.PSU or owner_game in (None, games.PSU):
        return cb
    race, gender = cb[0x40], cb[0x41]
    return (cb[:APPEARANCE] + appearance_for(cb[APPEARANCE:APPEARANCE + 97], race, gender, recipient_game, owner_game)
            + cb[APPEARANCE + 97:])


def card_for(rec, recipient_game, owner_game=None):
    """A partner card record (name 0x40, race, gender, class, voice type, pitch ...) for a recipient."""
    if recipient_game != games.PSU or owner_game == games.PSU or len(rec) < 0x45:
        return rec
    b = bytearray(rec)
    b[0x42] = progression.visible_class(b[0x42], recipient_game)
    b[0x43] = visible_voice(b[0x43], b[0x40], b[0x41], recipient_game, owner_game)
    return bytes(b)


# --- items --------------------------------------------------------------------------------------------

PSU_PA_TOP = 29             # PSU's top PA level byte (Lv30)


def pa_level(level, recipient_game):
    """A PA level byte (PA list id byte 3, weapon var_rec +0x1E, a tech slot's level) as a recipient may see it."""
    return min(level, PSU_PA_TOP) if recipient_game == games.PSU else level


def pa_id(pid, recipient_game):
    """A PA list id (hex, level in byte 3) as a recipient may see it."""
    if recipient_game != games.PSU or int(pid[6:8], 16) <= PSU_PA_TOP:
        return pid
    return pid[:6] + f"{PSU_PA_TOP:02x}"


def item_ok(item_id, recipient_game):
    """True when a `recipient_game` client knows item `item_id` (hex id as stored in the inventory)."""
    if recipient_game != games.PSU or not item_id:
        return True
    if item_id[:2] == "12":                  # meseta
        return True
    if item_id[:2] == "09" and int(item_id[2:4], 16) >= 0x10:
        return False                         # AotI clothing: colour bits in byte 1's high nibble
    db = games.json_data("items.json", game=games.PSU)
    return not db or item_id in db or item_id[:6] + "00" in db


def ground_ok(g, recipient_game):
    return g.get("meseta") or item_ok(g.get("id"), recipient_game)


def trade_error(t, players):
    """Why trade t cannot complete (an item the receiving side's disc does not have), or None.
    players: gid -> world.Player."""
    for giver in (t.a, t.b):
        taker = players.get(t.other(giver))
        gp = players.get(giver)
        if taker is None or gp is None or gp.state is None:
            continue
        inv = {x["uuid"]: x["id"] for x in gp.state.get("inv", [])}
        for uuid in t.items[giver]:
            if not item_ok(inv.get(uuid), taker.game):
                return f"{taker.gid:#x} ({taker.game}) cannot hold {inv.get(uuid)}"
    return None
