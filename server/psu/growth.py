"""Photon Art growth (PS2 client; offsets are body offsets). From static reading of PSU_US.erx.

Learned PAs (state["pas"]): [u32 PA EXP (running total), "cc b1 b2 LL" id] where byte 3 is the 0-based
level (shown +1). cat 04 striking (b1 weapon type, b2 PA index), 05 bullet, 06 technique.
Each damaging hit that lands on an enemy adds 1 PA EXP to the PA the weapon uses (0x1ba830 -> 0x296640 ->
0x296180): hit +0x14 word = weapon UUID (bits 0..28) + PA flag / tech slot (bits 29..31); the weapon's
style byte (var_rec +0x1A) picks striking (0: only with a PA flag), bullet (1/2: only with var_rec +0x1D PA
index != 0) or technique (3: the code in tech slot t = bits 29..31 at var_rec +0x1C + 2t, PA 06 (code-1)/10+1
(code-1)%10, 0x296350; AotI 0x299080 -> 0x298da0, GetTechParam 0xad620, the same rule). exp >= threshold(level)
-> level + 1 (one per hit; the cap is level byte 29 = Lv30). The weapon's var_rec +0x1E (a technique weapon's
slot level byte +0x1D + 2t) carries the same level byte.
What counts (0x1ba5a0): a hit from a player whose attack block D is damaging (0x200) and whose result has SE
0x1 / 0x800000, or a non-damaging one (support / healing techniques) whose result has 0x40000 / 0x80000 /
0x1000000; the server grows PAs on landed damaging hits on enemies and on every support technique result on a
player (revive, heal, cure, buff; one per target, combat.resolve), the disc's cap applying the same way.
Thresholds: $PSU_GAMEDATA/pa.json (extract/patables.py): id -> thr[level byte 0..cap-2].
The cap comes from the disc: one threshold per level-up, so level byte len(thr) is the top (PSU 29 = Lv30; AotI's
pa.json has 49 = Lv50, itemCommonParam root +8/+9/+0xA = 50, checked by GetSkillParam 0xae780 / GetBulletParam
0xae030 / GetTechParam 0xad620). A PA without thresholds uses the longest list of its disc's table.
  S>C 0x0108 (frame 0x40, handler 0x1457c0): +0x24 GID, +0x28 LID, +0x2C u8 level-up flag (shows
  "<PA> Lv n" and the effect), +0x2D 0, +0x30 u32 PA EXP, +0x34 the 4-byte id (level in byte 3).
"""
import json
import os
import struct

from psu import games, items, progression, xgame
from psu.login import packet

ACC = bytes.fromhex("00011300")
CAP_BYTE = 29
_CAPS = {}


def thresholds():
    return games.json_data("pa.json")


def cap_byte(thr=None):
    """Top level byte: len(thr) for a PA with thresholds, else the disc's longest (PSU 29, AotI 49)."""
    if thr:
        return len(thr)
    game = games.current()
    if game not in _CAPS:
        _CAPS[game] = max((len(v.get("thr", [])) for v in thresholds().values() if isinstance(v, dict)),
                          default=0) or CAP_BYTE
    return _CAPS[game]


def weapon_key(slot, w):
    """The 3-byte PA key the weapon's hit feeds, or None."""
    it = items.db().get(slot["id"])
    if not it or it["cat"] != 1:
        return None
    c = bytes.fromhex(it["consts"])
    style, wtype, idx = c[0xE], bytes.fromhex(slot["id"])[1], slot.get("pa", [0, 0])[0]
    if style == 0 and w >> 29:
        return f"04{wtype:02x}{idx:02x}"
    if style in (1, 2) and idx:
        return f"05{wtype:02x}{idx:02x}"
    if style == 3:                                    # technique: the tech in slot w >> 29 (0x296350)
        techs = slot.get("techs") or []
        t = w >> 29
        if t < len(techs) and 1 <= techs[t][0] <= 0x38:
            return tech_key(techs[t][0])
    return None


def pa_hit(st, w):
    """One landed hit with hit-word w. Returns (entry, leveled_up) for the PA that grew, or None."""
    uuid = w & 0x1FFFFFFF
    slot = next((x for x in st.get("inv", []) if x["uuid"] == uuid), None)
    key = weapon_key(slot, w) if slot else None
    if key is None:
        return None
    entry = next((e for e in st.get("pas", []) if e[1][:6] == key), None)
    if entry is None:
        return None
    level = int(entry[1][6:8], 16)
    thr = thresholds().get(key + "00", {}).get("thr", [])
    if level >= cap_byte(thr):
        return None
    entry[0] += 1
    up = level < len(thr) and entry[0] >= thr[level]
    if up:
        level += 1
        entry[1] = key + f"{level:02x}"
        for x in st.get("inv", []):                   # every weapon linked to this PA shows the new level
            if key[:2] == "06":
                for t in x.get("techs") or []:
                    if t[0] and tech_key(t[0]) == key:
                        t[1] = level
            elif x.get("pa") and weapon_key(x, 1 << 29) == key:
                x["pa"] = [x["pa"][0], level]
    return entry, up


def c0108(d, p, entry, up):
    """To player d: a PSU client sees PSU's top level (Lv30) at most, and no level-up above it (psu/xgame.py)."""
    pid = xgame.pa_id(entry[1], getattr(d, "game", None) or games.current())
    up = up and pid == entry[1]
    return packet(0x0108, struct.pack("<H", d.lid) + bytes(18) + ACC + struct.pack("<I", d.gid) + bytes(8)
                  + struct.pack("<II", p.gid, p.lid) + bytes([1 if up else 0, 0, 0, 0])
                  + struct.pack("<I", entry[0]) + bytes.fromhex(pid))


def link_level(st, slot, idx):
    """PA index + level byte for linking PA list entry `idx` (0x0105 event 3 +0x2E; the client takes list[idx],
    0x5bbc0, and copies its byte 2 and level, 0xbb600) to weapon slot: the learned PA's level."""
    wtype = bytes.fromhex(slot["id"])[1]
    pas = st.get("pas", [])
    if 0 <= idx < len(pas):
        cat, b1, b2, lv = bytes.fromhex(pas[idx][1])
        if cat in (4, 5) and b1 == wtype:
            return [b2, lv]
    return [0, 0]


def tech_weapon(slot):
    """True for a technique weapon (constants style byte 3: rods, wands)."""
    it = items.db().get(slot["id"]) if slot else None
    return bool(it) and it["cat"] == 1 and bytes.fromhex(it["consts"])[0xE] == 3


def tech_code(pid):
    """Technique code (var_rec tech slot byte) of a cat-06 PA id: (byte 1 - 1) * 10 + byte 2 + 1 (0xbe020)."""
    return (int(pid[2:4], 16) - 1) * 10 + int(pid[4:6], 16) + 1


def tech_key(code):
    """The 3-byte PA key of a technique code: 06, (code - 1) / 10 + 1, (code - 1) % 10 (0x296350)."""
    return f"06{(code - 1) // 10 + 1:02x}{(code - 1) % 10:02x}"


def link_tech(st, slot, idx, tslot):
    """Link PA list entry `idx` (a technique) into tech slot `tslot` (0..3) of a technique weapon (0xbde00)."""
    pas = st.get("pas", [])
    if not (0 <= idx < len(pas) and 0 <= tslot < 4) or pas[idx][1][:2] != "06":
        return
    code = tech_code(pas[idx][1])
    if not 1 <= code <= 0x38:
        return
    techs = slot.setdefault("techs", [[0, 0] for _ in range(4)])
    techs[tslot] = [code, int(pas[idx][1][6:8], 16)]


# --- learning a PA from a disk (cat 4 striking, 5 bullet, 6 technique) -------------------------------------
# The item menu (PSU 0x1f159c, AotI the same flow) refuses a disk whose PA is already in the list at any level
# (0x5bb80 compares id bytes 0..2, message 0x47); with room (count +0x615 < max +0x617 = 36, built by 0x5b0a0)
# it sends C>S 0x0105 event 7 (0x15b780 -> 0x15d5b0; AotI 0x15b7f0 -> 0x15da70): +0x2C inventory index, +0x2E 0x24
# (append), +0x2F 0, +0x30 disk UUID; with the list full it first asks which PA to forget (0x1f07d0 -> 0x1f06f0) and
# sends that list position in +0x2E. The disk carries no level: the PA starts at Lv1 (0x21f070 clears byte 3).
# S>C 0x0105 event 7 (PSU 0x1460a0, AotI 0x142c80) is dropped unless +0x2E < 0x24 and 4 <= +0x2F < 7; it removes the
# disk's whole slot (0xf62a0 n=99), shows "<disk> learned", writes list entry +0x2E and bumps the count while < 36.
# PSU builds the entry itself from the disk id (0x21f070) with PA EXP = +0x38; AotI takes it from the packet: u32
# PA EXP at +0x64, the 4-byte id at +0x68. Online both copy the 0x30-byte PA param entry at +0x34 into the
# param table when list entry +0x2E is that PA (0x97ca0 / 0x99790), as 0x0A0A +0x1AC0 does at load.
LEARN = 7


def disk_pa(item_id):
    """The 3-byte PA key a disk teaches (PSU 0x21f070 = AotI 0x21f830): a bullet disk 05 b1 b2 is PA 05 (b1 + 0x0D)
    (b2 + 1); striking (04) and technique (06) disks are their PA."""
    b = bytes.fromhex(item_id)
    if b[0] == 5:
        return f"05{b[1] + 0x0D:02x}{b[2] + 1:02x}"
    return item_id[:6]


def _disk_problem(slot, pas):
    """Why the inventory slot cannot be learned (None = it can): not a disk, no such PA, already learned."""
    it = items.db().get(slot["id"]) if slot else None
    if not it or it["cat"] not in (4, 5, 6):
        return "not a disk"
    key = disk_pa(slot["id"])
    if key + "00" not in thresholds() and key + "00" not in items.paparam().get("pa", {}):
        return f"no PA {key} on this disc"
    if any(e[1][:6] == key for e in pas):
        return "already learned"
    return None


def pick_disk(inv, idx, uuid, pas):
    """The saved inventory slot an event 7 request means: the slot with the request's UUID, else the one at its
    index, when that is a learnable disk. The client's pack can disagree with the saved one (after the zone
    loads' 0x0A06 hand out UUIDs by position, a disk's index and UUID in the client can name another item in the
    saved pack), and the client only sends a disk it may learn (it checks
    the item at that index itself, 0x15b780, and refuses a known PA, 0x5bb80), so otherwise the learnable disk
    nearest that index is meant. Returns (slot, error): on an error the slot asked for (or None)."""
    asked = [s for s in (next((s for s in inv if s["uuid"] == uuid), None),
                         inv[idx] if idx < len(inv) else None) if s is not None]
    for s in asked:
        if _disk_problem(s, pas) is None:
            return s, None
    near = [(abs(i - idx), i) for i, s in enumerate(inv) if _disk_problem(s, pas) is None]
    if near:
        return inv[min(near)[1]], None
    return (asked[0], _disk_problem(asked[0], pas)) if asked else (None, "not a disk")


def learn_disk(st, req):
    """C>S 0x0105 event 7: learn the disk with UUID +0x30 / index +0x2C (pick_disk). Returns (list position,
    entry, disk category, disk name, error); on success the disk's slot is gone from st["inv"] and the PA is in
    st["pas"] at the position (appended while the list has room, else the entry the player chose at +0x2E is
    replaced)."""
    idx, _ev, pos, _a, uuid = struct.unpack_from("<BBBBI", req, 0x2C)
    inv = st.get("inv", [])
    pas = st.setdefault("pas", [])
    slot, err = pick_disk(inv, idx, uuid, pas)
    it = items.db().get(slot["id"]) if slot else None
    if err:
        return None, None, it["cat"] if it else 0, it and it["name"], err
    key = disk_pa(slot["id"])
    entry = [0, key + "00"]
    if len(pas) < items.MAX_PAS:
        pos = len(pas)
        pas.append(entry)
    elif pos < items.MAX_PAS:
        pas[pos] = entry
    else:
        return None, None, it["cat"], it["name"], "PA list full"
    inv.remove(slot)
    return pos, entry, it["cat"], it["name"], None


def c0105_learn(gid, lid, req, pos, cat, entry, game=None):
    """The S>C 0x0105 event 7 confirmation of learn_disk: list position, disk category, the PA's param entry,
    then AotI's PA EXP + id (PSU stops reading at +0x64)."""
    pid = xgame.pa_id(entry[1], game or games.current())
    return items.c0105_reply(gid, lid, req[:0x2E] + bytes([pos, cat]) + req[0x30:0x34],
                             extra=items.pa_param(entry[0], entry[1]) + struct.pack("<I", entry[0])
                             + bytes.fromhex(pid))
