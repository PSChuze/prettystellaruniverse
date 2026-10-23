"""Combat: the server owns hit results, enemy HP, player HP and EXP (PS2 client layouts; offsets are body offsets).

From static reading of PSU_US.erx: offline, the client runs an in-process room (TGameRoom, C>S dispatch
0x62570) that does what the official server did; online the client only reports hits and shows what it is
sent. Enemy AI runs on every client; enemies spawn client-side from the zone's set data once the server
echoes the spawn condition.

  C>S 0x0402 condition (+0x24 id, +0x28 4 x s16, +0x30 type, +0x34, +0x38) -> S>C 0x1201 same data, to the zone
  C>S 0x0403 / 0x0404 event flags -> S>C 0x1203 / 0x1205 (psu/instance.py keeps the flag sets)
  C>S 0x0E00 hits (channel 2): +0x24 count, +0x28 sender id, +0x2C sequence, then 0x50-byte hits:
          +0 f32 x y z, +0xC attacker id, +0x10 target id, +0x14/+0x18 opaque (echoed), +0x1C block B (16),
          +0x2C block C (16), +0x3C attack block D (20): +0 flags (0x200 damaging, 0xC tech, 0x10000 ignore
          defence, 0x40000 percent, 0x4000 sure hit, 0x80000 copy +0x13), +8 s16 accuracy, +0xA s16 bonus,
          +0xC s16 power, +0x10 u8 element, +0x11 u8 element %, +0x13 s8
  S>C 0x0E07 results (channel 3): +0x24 count, 0x68-byte entries from +0x28: +0 u8 1 (0 ends the batch),
          +1 flag2, +4 damage, +8 x y z, +0x14 attacker, +0x18 target, +0x1C/+0x20 echo, +0x24 attacker
          SE1 SE2 HP (+0x30 2 bytes), +0x34 target SE1 SE2 HP (+0x40 2 bytes), +0x44 block B, +0x54 block D.
          The client overwrites both actors' SE/HP with these, so the attacker's real HP is always sent.
          SE1: 0x1 damaged, 0x10 miss, 0x20000 dead, 0x400000 critical.
  S>C 0x0E08 enemy status: +0x24 id, +0x28 0x114-byte status image (+4 SE1, +8 SE2, +0x10 u8 level,
          +0x14 8 x s16 stats, +0x24 HP, +0x28 max HP); AotI: 0x110 bytes, every field 4 bytes earlier
  S>C 0x0115 EXP (frame 0xA8): +0x24 GID, +0x28 LID, +0x2C enemy id, +0x30 the 0x70-byte level block
  S>C 0x1022 party HUD HP: +0x24 HP, +0x28 s8 party slot

Player ids in a zone are LIDs (the offline room treats ids < 6 as players); anything else is an enemy.
Damage (client 0x1bfc30): base = power + bonus - (tech ? MST : DFP), nothing if <= 0; damage = base / 5,
critical (5 %) x1.5; without D flag 0x4000 an accuracy roll misses when rand(0..100) <= EVP * 10 / accuracy,
always with accuracy <= 0 (0x1bfdd8 -> 0x1c0060; techniques set 0x4000, 0xb5064; striking / bullet PAs only with
their record flag 2, 0x26303c; enemy blocks from their clients come without it).
Enemy stats: $PSU_GAMEDATA/enemylevel.json (disc EnemyLevelBaseParam.snr), per-enemy multipliers
not parsed yet (1.0). EXP on a kill: the killer 100 %, everyone else who hit it 70 % (0x1ba680).

Support techniques (Resta, Giresta, Reverser, Shifta, Deband, Zodial, Retier, Megistar; the same records and
rules on both discs, PSU / AotI addresses): targets are the client's (one hit per player its technique touched).
  D (tech builder 0x262600 / 0x267170, flags from the record 0xb4fd0 / 0xad430, GetTechParam 0xb51d0 / 0xad620):
    record per tier t = min(L / 10, 2) (L = tech slot level byte): power % = pow + inc * (L - 10 t); kind 1 of
    the light group: rec flag 0x40 -> 0x800 revive, 0x20 -> 0x2400 cure (bits 0x3c3ff, AotI 0x2003c3ff), power
    % > 0 -> 0x1400 heal; strength 1..5 -> 0x2000 with the effect's SE2 bits (table 0x53c9e0 / 0x55cd30),
    0x2400 when they are buffs (0xf80000), D+0x12 = strength - 1. Power = (TP + weapon ATP + grind) * % / 100,
    times the caster's 0x200000 buff (0x1bd560 slot 2).
  On the target (0x1c0350 / 0x1c0970): dead -> 0x800 revives (0x1bfb40 / 0x1c0170: HP = max(1, power), SE1
    0x40000); alive -> 0x1c01e0 / 0x1c0830: heal min(power, missing) with SE1 0x80000; cure of the bits' ailments
    and debuffs (0x1be790 non-cure mode) with SE1 0x80000; buffs (0x1bdc60 / 0x1be150; 0x800000 means all four)
    with SE1 0x1000000. Nothing happened -> no result.
  Buffs (GA_STATUSCHANGE_TABLE.NBL StatusChangeControlData, same file on both discs; level = D+0x12): SE2
    0x80000 ATP, 0x100000 DFP, 0x200000 TP/MST, 0x400000 ATA/EVP, each clears its debuff (bit >> 5); 0x1000 /
    0x2000 regeneration-type statuses. The server keeps them (Player.buffs) and multiplies the stats the client
    does (striking/bullet ATP and ATA 0x262db0, tech power 0x262600, defence DFP/MST/EVP 0x5ecb0). The client
    takes SE2 from every 0x0E07 / 0x0117 (0x1bc0f0, 0x14ecd0), so both carry the player's active bits; expiry
    is announced with 0x0117.

Status effects, elements, equipment (PSU / AotI; the same StatusChangeControlData on both discs):
  SE2 0x1 burn, 0x2 poison, 0x4 virus, 0x8 shock, 0x10 silence, 0x20 freeze, 0x40 sleep, 0x80 paralysis,
    0x100 confusion, 0x200 charm (effect file order at 0x598a30), 0x400 instant death; 0x4000 / 0x8000 / 0x10000 /
    0x20000 the debuffs of the buff slots. A block with 0x2000 (D+4 bits, D+0x12 level, D+0xe chance %, 0x8000
    sure) tries them after a landed hit or as a status-only block (0x1c0350 -> 0x1bf8f0 = 0x1bdee0 + 0x1bdac0;
    AotI 0x1c0970 -> 0x1bff40 = 0x1be560 + 0x1bdac0): chance rand(100) < D+0xe * (table chance - target STA) / 100
    (0x1bd900), blocked by the table's SE2 mask (0x1bd9a0) and by the target's immunities (status +0xc). Ticks
    (0x1bf040 damage, never below 1 HP; 0x1beac0 Giresta heal) and expiries go out as 0x0E07 entries with attacker
    0xFFFFFFFF, as the offline room sends them (0x1bb3a0 -> 0x1bb150). Freeze .. charm end after N landed hits
    (0x1be5b0). Frozen / asleep / paralysed targets are always hit (0x1c0060).
  Elements (0x1bd050): codes 1 fire, 9 ice, 2 lightning, 10 ground, 4 light, 12 dark (0x263260); weak 1 + a + d,
    same 1 - a - d on power + bonus (0x1bfc30), AotI 1 +- 0.66 (a + d) (0x1c0250). Weapons: constants +0xB / +0xC;
    techniques: school, 10 + level + 4 per further same-school link (0x262600, 0xb09f0); enemies: Param +0x5C
    and level row +0x14 x Param +0x24 (0x281030); players: line shield element / % (+ type-3 unit rec+8, 0x261ad0).
  Equipment (0x263db0 / AotI 0x268c70): line shield rec +4 ATP, +6 TP, +8 DFP, +0xA MST, +0xC ATA, +0xE EVP,
    +0x10 STA; units rec +4 / +6: type 1 TP / MST, 2 ATP / ATA, 3 DFP / EVP, 4 STA / S5; a type-3 unit's rec+9..+0xB
    bits are immunities (0x2632d0 / 0x2680f0). All of it counts in power, accuracy, defence and Resta's power.
"""
import json
import os
import random
import struct
import time

from psu import games, progression
from psu.login import packet

ACC = bytes.fromhex("00011300")
fights = {}                     # world zone key -> {enemy id: Enemy}
positions = {}                  # (world zone key, enemy id) -> 12-byte xyz from its last 0x0402 (drop spot)
_roster = {}                    # (quest, zone) -> {enemy id: enemy spec from missions.json gateways}
_gates = {}                     # (quest, zone) -> [gateway dicts]
STATS = ("hp", "atp", "dfp", "ata", "evp", "sta", "s5", "tp", "mst")


def u32(v):
    return struct.pack("<I", v & 0xFFFFFFFF)


def hdr(d, lid=None):
    return struct.pack("<H", d.lid if lid is None else lid) + bytes(18) + ACC + u32(d.gid) + bytes(8)


def enemy_rows():
    return games.json_data("enemylevel.json", required=True)["levels"]


def enemy_data():
    """$PSU_GAMEDATA/enemies.json (extract/enemydata.py): 93 variants' Param multipliers, level rows,
    EXP bit multipliers. Missing -> None (generic rows at the player's level)."""
    return games.json_data("enemies.json") or None


def enemy_stats(variant, level, bits=()):
    """TServerActorEnemy 0x282040: level row x Param multipliers; the MST slot gets the TP value
    (0x282374); kill EXP = base x the EXP bit multipliers of non-neutral variant bits (0x280680)."""
    gd = enemy_data()
    v = gd["variants"][str(variant)]
    row = gd["levels"][max(1, min(level, len(gd["levels"]) - 1))]
    hp = int(max(1.0, row[0] * v["hp"]))
    st = [max(0, min(0x7FFF, int(r * v[k]))) for r, k in zip(row[1:9], STATS[1:9])]
    st[7] = st[6]
    exp = max(1, int(row[10] * v["exp"]))
    mult = gd["exp_global"]
    for b in bits:
        if v["variant_bits"][b]["exp_bonus"]:
            mult *= gd["exp_bits"][b]
    return hp, st, int(exp * mult)


def zone_ids(zone_key):
    """(quest, zone) of a world zone key (universe, quest, zone | (zone, owner))."""
    q, z = zone_key[-2], zone_key[-1]
    return q, (z[0] if isinstance(z, tuple) else z)


def roster(zone_key):
    """{enemy id: spec} for a mission zone (missions.json gateways: id, wave, type, level, variant, bits)."""
    from psu import missions
    qz = zone_ids(zone_key)
    if qz not in _roster:
        m = missions.mission(qz[0]) or {}
        gates = m.get("gateways", {}).get(str(qz[1]), [])
        _gates[qz] = gates
        _roster[qz] = {e["id"]: dict(e, gateway=g["id"], map=g["map"]) for g in gates for e in g["enemies"]}
    return _roster[qz]


class Enemy:
    def __init__(self, eid, level, spec=None):
        self.id, self.spec = eid, spec
        if spec is not None and enemy_data():
            self.level = spec["level"]
            self.max_hp, self.stats, self.exp = enemy_stats(spec["variant"], spec["level"], spec.get("bits", ()))
            self.name = spec.get("name", "")
        else:                                # not in the set data: the generic row at the player's level
            rows = enemy_rows()
            row = rows[max(1, min(level, len(rows) - 1))]
            self.level = level
            self.max_hp = max(1, row[0])
            self.stats = list(row[1:9])      # ATP DFP ATA EVP STA S5 TP MST
            self.exp = row[10]
            self.name = ""
        self.hp = self.max_hp
        self.hitters = set()                 # GIDs of players that hit it
        self.dead = False
        self.element, self.element_pct = 0, 0
        if spec is not None and enemy_data():   # Param +0x5C (category) is the element code (0x35d534 -> actor
            gd = enemy_data()                   # +0x3c, 0x281410); % = level row +0x14 x Param +0x24 (0x281420)
            v = gd["variants"].get(str(spec["variant"]), {})
            row = gd["levels"][max(1, min(self.level, len(gd["levels"]) - 1))]
            self.element = v.get("category", 0) if v.get("category", 0) & 7 else 0
            self.element_pct = max(0, min(127, int(row[9] * v.get("x24", 1.0)))) if len(row) > 9 else 0


def new_enemy(zone_key, eid, level):
    return Enemy(eid, level, roster(zone_key).get(eid))


def cleared_gateways(zone_key, enemies):
    """Gateways whose enemies are all dead now: [(gateway, flag id = map index * 256 + clear event)],
    each once (the offline room sets params +0x5C's event when a gateway's waves are done, 0x35c920)."""
    qz = zone_ids(zone_key)
    roster(zone_key)
    out = []
    for g in _gates.get(qz, []):
        if g.get("cleared_sent") is None:
            g["cleared_sent"] = set()
        key = zone_key
        if key in g["cleared_sent"] or not g["enemies"] or g["flags"][0] < 0:
            continue
        if all(enemies.get(e["id"]) is not None and enemies[e["id"]].dead for e in g["enemies"]):
            g["cleared_sent"].add(key)
            out.append((g, g["map_index"] * 256 + g["flags"][0]))
    return out


def zone_enemies(zone):
    return fights.setdefault(zone, {})


def clear_zone(zone):
    fights.pop(zone, None)


# --- conditions ---------------------------------------------------------------------

def c1201(d, body):
    return packet(0x1201, hdr(d, 0xFFFF) + body[0x24:0x3C])


# --- hits --------------------------------------------------------------------------------------------

def damage(D, target_stats, target_hp, attacker_stats=None, target=None):
    """(damage, se1 bits) for attack block D against a target's 8 stats. Enemy attacks arrive with power 0
    (the client fills D from the attacker's local status, blank online), so attacker_stats (the server's
    enemy) stand in for ATP / MST and ATA. target (Player or Enemy) brings its SE2 (frozen, asleep or paralysed
    targets are always hit, 0x1c0060), its barrier charges (0x1bd320: a striking / bullet hit uses a Dizas
    charge, a technique a Rentis one, and does nothing) and its element (0x1bd050, D+0x10 / +0x11 against the
    defence block +0xf / +0xe: weak 1 + a + d, resistant 1 - a - d, AotI 0.66 times that, on power + bonus
    before defence). A damaging block without 0x1000 is status only (0x1c0350)."""
    flags, _, acc, bonus, power = struct.unpack_from("<IIhhh", D, 0)
    if not flags & 0x200 or not flags & 0x1000:
        return 0, 0
    if attacker_stats is not None and power <= 0:
        power = attacker_stats[7] if flags & 0x8 else attacker_stats[0]
        acc = acc or attacker_stats[2]
    if not flags & 0x4000 and not se2(target) & 0xE0 and \
            (acc <= 0 or random.randint(0, 100) <= target_stats[3] * 10 // acc):
        return 0, 0x10
    barrier = getattr(target, "barrier", None)
    if barrier:
        k = 1 if flags & 0xC == 0xC else 0 if flags & 0xC else None
        if k is not None and barrier[k] > 0:
            barrier[k] -= 1
            return 0, 0
    mult = 1.0
    if target is not None and D[0x10]:
        dcode, dpct = defence_element(target)
        mult = element_mult(D[0x10], D[0x11], dcode, dpct)
    if flags & 0x40000:
        dmg = target_hp * bonus // 100
    elif mult != 1.0:
        base = f32(f32(float(power + bonus)) * mult)
        if not flags & 0x10000:
            base = f32(base - (target_stats[7] if flags & 0x8 else target_stats[1]))
        dmg = int(f32(base / 5.0)) if base > 0 else 0
    else:
        base = power + bonus
        if not flags & 0x10000:
            base -= target_stats[7] if flags & 0x8 else target_stats[1]
        dmg = base // 5 if base > 0 else 0
    se = 0x1
    if dmg and random.random() < 0.05:
        dmg, se = dmg * 3 // 2, se | 0x400000
    return min(dmg, 0xFFFF), se


def revive_amount(reviver, attack_id, target):
    """HP a dead player gets back from `reviver`'s hit (attack id low 29 bits = the item UUID; items with
    the revive flag 0x80 in constants byte 7: Moon Atomizer 25 %, Moon Atomizer X 100 %, flag 0x10 =
    percent of max HP), or None. The item was usually just consumed (0x0105 use), so recent uses count."""
    uuid = attack_id & 0x1FFFFFFF
    slot = next((x for x in (reviver.state or {}).get("inv", []) if x["uuid"] == uuid), None)
    slot = slot or getattr(reviver, "recent_use", {}).get(uuid)
    if slot is None:
        return None
    from psu import items
    it = items.db().get(slot["id"])
    if not it:
        return None
    c = bytes.fromhex(it["consts"])
    if not c[7] & 0x80:
        return None
    amount, = struct.unpack_from("<H", c, 4)
    mx = progression.derived(target.raw, target.state)["max_hp"]
    return max(1, mx * amount // 100 if c[7] & 0x10 else min(amount, mx))


def heal_amount(user, attack_id, target):
    """HP from a healing item (constants byte 7 flag 0x20, amount u16 at +4, flag 0x10 = percent of max HP;
    Monomate 300, Dimate / Trimate ...) used by `user` (attack id = the just-used item's UUID), or None."""
    uuid = attack_id & 0x1FFFFFFF
    slot = next((x for x in (user.state or {}).get("inv", []) if x["uuid"] == uuid), None)
    slot = slot or getattr(user, "recent_use", {}).get(uuid)
    if slot is None:
        return None
    from psu import items
    it = items.db().get(slot["id"])
    if not it or it["cat"] != 3:
        return None
    c = bytes.fromhex(it["consts"])
    if not c[7] & 0x20 or c[7] & 0x80:
        return None
    amount, = struct.unpack_from("<H", c, 4)
    if amount == 0 and c[7] & 0x40:                     # Antimate / Sol Atomizer: a cure (item_status)
        return None
    mx = progression.derived(target.raw, target.state)["max_hp"]
    return max(1, mx * amount // 100 if c[7] & 0x10 else amount)


# --- support techniques and buffs ---------------------------------------------------------------------

# GetTechParam records (PSU 0xb51d0, AotI 0xad620; GA_ITEMPARAM itemTechParam_0N, identical on both discs) of
# the techniques with a non-damaging block: PA key -> (kind = stats byte 1 & 0xF, per tier (pow %, % per level,
# rec flags +0xF, effect +0x18, strength +0x19)).
SUPPORT = {
    "060105": (2, ((500, 10, 0, 0x11, 1), (700, 10, 0, 0x11, 2), (900, 10, 0, 0x11, 3))),      # Shifta
    "060205": (2, ((500, 20, 0, 0x13, 1), (700, 20, 0, 0x13, 2), (900, 10, 0, 0x13, 3))),      # Deband
    "060305": (2, ((500, 20, 0, 0x17, 1), (700, 20, 0, 0x17, 2), (900, 10, 0, 0x17, 3))),      # Zodial
    "060500": (1, ((101, 1, 0, 0, 0), (126, 1, 0, 0, 0), (151, 1, 0, 0, 0))),                  # Resta
    "060501": (1, ((151, 1, 0x40, 0x10, 1), (176, 1, 0x40, 0x10, 2), (201, 1, 0x40, 0x10, 3))),  # Giresta
    "060502": (1, ((0, 0, 0x20, 0, 0), (0, 0, 0x20, 0, 0), (0, 0, 0x20, 0, 0))),               # Reverser
    "060506": (2, ((500, 10, 0, 0x15, 1), (700, 10, 0, 0x15, 2), (900, 10, 0, 0x15, 3))),      # Retier
    "060605": (2, ((500, 10, 0, 0x1A, 1), (700, 10, 0, 0x1A, 2), (900, 10, 0, 0x1A, 3))),      # Megistar
}
EFFECT_BITS = {0x10: 0x1000, 0x11: 0x80000, 0x13: 0x100000, 0x15: 0x200000, 0x17: 0x400000, 0x1A: 0x802000}
CURE_BITS = {games.PSU: 0x3C3FF, games.AOTI: 0x2003C3FF}       # 0xb5174 / 0xad4e0
DAMAGING_BITS = {games.PSU: 0x1807C3FF, games.AOTI: 0x3807C3FF}  # 0x264690/a0/d0, AotI 0x269530/40/70
# StatusChangeControlData buff k (SE2 1 << (19 + k)), row 0 per level: (seconds, f32 multiplier bits)
BUFFS = (((120, 0x3F9EB852), (180, 0x3FA147AE), (240, 0x3FA3D70A), (240, 0x3FA3D70A), (240, 0x3FA3D70A)),
         ((120, 0x3FF0A3D7), (180, 0x3FF5C28F), (240, 0x3FFAE148), (240, 0x3FFAE148), (240, 0x3FFAE148)),
         ((120, 0x3F9AE148), (180, 0x3F9D70A4), (240, 0x3FA00000), (240, 0x3FA00000), (240, 0x3FA00000)),
         ((120, 0x40200000), (180, 0x40400000), (240, 0x40600000), (240, 0x40600000), (240, 0x40600000)))
# statuses 0xc / 0xd (SE2 0x1000 / 0x2000, applied by 0x1bdc60 through 0x1bd1e0): seconds per level
TIMED = {0x1000: (20, 24, 28, 28, 28), 0x2000: (20, 35, 50, 50, 50)}
# row 1 of the same buff slots: the debuffs (SE2 1 << (14 + k), 0x1bdac0): (seconds, f32 multiplier bits)
DEBUFFS = (((30, 0x3F547AE1), (45, 0x3F4F5C29), (60, 0x3F4A3D71), (60, 0x3F4A3D71), (60, 0x3F4A3D71)),
           ((30, 0x3F19999A), (45, 0x3F000000), (60, 0x3ECCCCCD), (60, 0x3ECCCCCD), (60, 0x3ECCCCCD)),
           ((30, 0x3F4CCCCD), (45, 0x3F19999A), (60, 0x3ECCCCCD), (60, 0x3ECCCCCD), (60, 0x3ECCCCCD)),
           ((30, 0x3F000000), (45, 0x3ECCCCCD), (60, 0x3E99999A), (60, 0x3E99999A), (60, 0x3E99999A)))
# StatusChangeControlData status i (SE2 1 << i; 0x1bd1e0), per level: (seconds, tick period s, value, chance %,
# hits that end it, value mode 1 = % of max HP). Both discs ship the same GA_STATUSCHANGE_TABLE.NBL.
AILMENTS = (
    ((11, 2, 2, 24, 0, 1), (11, 2, 3, 29, 0, 1), (11, 2, 4, 35, 0, 1), (11, 2, 5, 42, 0, 1), (11, 2, 6, 50, 0, 1)),
    ((15, 3, 2, 24, 0, 1), (21, 3, 2, 29, 0, 1), (27, 3, 2, 35, 0, 1), (33, 3, 2, 42, 0, 1), (39, 3, 2, 50, 0, 1)),
    ((20, 5, 6, 24, 0, 1), (25, 5, 6, 29, 0, 1), (30, 5, 6, 35, 0, 1), (35, 5, 6, 42, 0, 1), (40, 5, 6, 50, 0, 1)),
    ((5, 0, 0, 24, 0, 1), (5, 0, 0, 29, 0, 1), (6, 0, 0, 35, 0, 1), (6, 0, 0, 42, 0, 1), (7, 0, 0, 50, 0, 1)),
    ((5, 0, 0, 24, 0, 1), (5, 0, 0, 29, 0, 1), (6, 0, 0, 35, 0, 1), (6, 0, 0, 42, 0, 1), (7, 0, 0, 50, 0, 1)),
    ((3, 0, 0, 24, 3, 2), (4, 0, 0, 29, 4, 2), (5, 0, 0, 35, 5, 2), (6, 0, 0, 42, 6, 2), (7, 0, 0, 50, 7, 2)),
    ((4, 0, 40, 24, 2, 2), (5, 0, 50, 29, 3, 2), (6, 0, 55, 35, 4, 2), (8, 0, 60, 42, 5, 2), (10, 0, 60, 50, 6, 2)),
    ((3, 0, 0, 24, 100, 2), (3, 0, 0, 29, 100, 2), (3, 0, 0, 35, 100, 2), (3, 0, 0, 42, 100, 2), (3, 0, 0, 50, 100, 2)),
    ((5, 0, 0, 24, 3, 2), (5, 0, 0, 29, 4, 2), (6, 0, 0, 35, 5, 2), (6, 0, 0, 42, 6, 2), (7, 0, 0, 50, 7, 2)),
    ((5, 0, 0, 24, 1, 2), (5, 0, 0, 29, 1, 2), (6, 0, 0, 35, 1, 2), (8, 0, 0, 42, 1, 2), (10, 0, 0, 50, 1, 2)),
    ((0, 0, 0, 13, 0, 2), (0, 0, 0, 16, 0, 2), (0, 0, 0, 19, 0, 2), (0, 0, 0, 25, 0, 2), (0, 0, 0, 30, 0, 2)),
    ((0, 0, 0, 15, 0, 2), (0, 0, 0, 20, 0, 2), (0, 0, 0, 25, 0, 2), (0, 0, 0, 30, 0, 2), (0, 0, 0, 35, 0, 2)),
    ((20, 4, 8, 100, 0, 1), (24, 4, 9, 100, 0, 1), (28, 4, 10, 100, 0, 1), (28, 4, 10, 100, 0, 1),
     (28, 4, 10, 100, 0, 1)),
    ((20, 5, 7, 100, 0, 1), (35, 7, 7, 100, 0, 1), (50, 9, 7, 100, 0, 1), (50, 9, 7, 100, 0, 1),
     (50, 9, 7, 100, 0, 1)))
# status i can only start while none of these SE2 bits is set (table +4, 0x1bd9a0)
AIL_BLOCK = (0x426, 0x404, 0x400, 0x408, 0x410, 0x421, 0x440, 0x480, 0x700, 0x700, 0x400, 0xC00, 0, 0)
# GA effect id -> SE2 bits (0x53c9e0 / AotI 0x55cd30, the same 29 words)
EFFECT_SE2 = (0, 0x1, 0x2, 0x4, 0x8, 0x10, 0x20, 0x40, 0x80, 0x100, 0x200, 0x400, 0x1000000, 0x2000000, 0x4000000,
              0x2000, 0x1000, 0x80000, 0x4000, 0x100000, 0x8000, 0x200000, 0x10000, 0x400000, 0x20000, 0x2000000,
              0x802000, 0x8000000, 0x10000000)
ELEMENT_CODE = (0, 1, 9, 2, 10, 4, 12)    # item / school element 1..6 -> attack and defence code (0x263260)
ELEMENT_SCALE = {games.PSU: 1.0, games.AOTI: 0.66}   # 0x1bfc30: 1 +- (a + d); AotI 0x1c0250: 1 +- 0.66 (a + d)
# attack techniques (kind 0) and enemy debuffs (kind 3): GetTechParam tier records +0x18 effect, +0x19 strength
# (0xb51d0 / 0xad620, the same on both discs); kind 2 Dizas / Rentis give barrier charges (effects 27 / 28)
TECH_FX = {
    "060100": ((1, 2), (1, 3), (1, 4)), "060101": ((1, 1), (1, 2), (1, 3)), "060102": ((1, 3), (1, 3), (1, 3)),
    "060103": ((1, 2), (1, 3), (1, 4)), "060200": ((6, 2), (6, 2), (6, 2)), "060201": ((6, 2), (6, 3), (6, 4)),
    "060202": ((6, 3), (6, 3), (6, 3)), "060203": ((6, 2), (6, 3), (6, 4)), "060300": ((4, 2), (4, 2), (4, 2)),
    "060301": ((4, 2), (4, 3), (4, 4)), "060302": ((4, 2), (4, 3), (4, 4)), "060303": ((4, 2), (4, 3), (4, 4)),
    "060400": ((5, 2), (5, 3), (5, 4)), "060402": ((2, 3), (2, 3), (2, 3)), "060404": ((8, 2), (8, 2), (8, 2)),
    "060600": ((11, 2), (11, 3), (11, 4)), "060601": ((9, 2), (9, 3), (9, 4)), "060602": ((3, 2), (3, 2), (3, 2)),
    "060106": ((18, 1), (18, 2), (18, 3)), "060206": ((20, 1), (20, 2), (20, 3)), "060306": ((24, 1), (24, 2), (24, 3)),
    "060405": ((27, 2), (27, 3), (27, 4)), "060505": ((28, 2), (28, 3), (28, 4)),
}
DEBUFF_TECHS = ("060106", "060206", "060306")      # Jellen, Zalure, Zoldeel: kind 3, status only, on enemies
BARRIER_TECHS = ("060405", "060505")               # Dizas, Rentis: kind 2, on players


def f32(v):
    return struct.unpack("<f", struct.pack("<f", v))[0]


def _mult(bits):
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def se2(p):
    """p's active SE2 bits (buffs the server applied); expired ones are dropped."""
    buffs = getattr(p, "buffs", None)
    if not buffs:
        return 0
    now = time.monotonic()
    for b in [b for b, (end, _) in buffs.items() if end <= now]:
        del buffs[b]
    out = 0
    for b in buffs:
        out |= b
    return out


def buff_mult(p, k):
    """Multiplier of buff slot k (0 ATP, 1 DFP, 2 TP/MST, 3 ATA/EVP) on p, 1.0 without it (0x1bd560). The slot
    holds the buff (1 << (19 + k)) or the debuff (1 << (14 + k)), never both (0x1bdc60 / 0x1bdac0)."""
    bits = se2(p)
    for b in (1 << (19 + k), 1 << (14 + k)):
        if bits & b:
            return getattr(p, "buffs", {}).get(b, (0, 1.0))[1]
    return 1.0


def _scaled(v, m):
    return v if m == 1.0 else int(f32(f32(float(v)) * m))


def buffed_stats(p, stats):
    """Defender stats with p's buffs (0x5ecb0: DFP x slot 1, MST x slot 2, EVP x slot 3)."""
    if not se2(p):
        return stats
    out = list(stats)
    for i, k in ((1, 1), (7, 2), (3, 3)):
        out[i] = _scaled(out[i], buff_mult(p, k))
    return out


def clear_buffs(p):
    """Death clears every buff (0x1bd610 cures 0x187bf3ff)."""
    if getattr(p, "buffs", None):
        p.buffs.clear()


def next_expiry(p):
    """Seconds until p's next buff runs out, or None."""
    buffs = getattr(p, "buffs", None)
    if not buffs:
        return None
    return max(0.0, min(end for end, _ in buffs.values()) - time.monotonic())


def apply_buffs(p, bits, level):
    """0x1bdc60: 0x800000 means all four buffs; each sets its slot (duration, multiplier) and clears its debuff;
    0x1000 / 0x2000 their timed statuses. True when anything was applied (SE1 0x1000000)."""
    if bits & 0x800000:
        bits |= 0x780000
    if not hasattr(p, "buffs") or p.buffs is None:
        p.buffs = {}
    now, done = time.monotonic(), False
    for k in range(4):
        b = 1 << (19 + k)
        if bits & b and level < len(BUFFS[k]):
            secs, m = BUFFS[k][level]
            p.buffs[b] = (now + secs, _mult(m))
            p.buffs.pop(1 << (14 + k), None)
            done = True
    for b, secs in TIMED.items():
        if bits & b and level < len(secs):
            p.buffs[b] = (now + secs[level], 1.0)
            _start_ticks(p, b, AILMENTS[b.bit_length() - 1][level], now)
            done = True
    return done


def cure(p, bits):
    """0x1be790 with cure flag 0: the bits' ailments (0x3ff, AotI 0x20000000) and debuffs (0x3c000) go."""
    cured = se2(p) & bits & 0x2003C3FF
    for b in list(getattr(p, "buffs", {}) or {}):
        if b & cured:
            del p.buffs[b]
            (getattr(p, "ail", None) or {}).pop(b, None)
    return bool(cured)


# --- ailments, debuffs, barriers, ticks (players and enemies alike) ------------------------------------
# Actor state: .buffs {SE2 bit: (end, slot multiplier)} (SE2 = its keys), .ail {bit: [next tick, period, value,
# mode, hits left]} for ticking / hit-ended statuses, .barrier [striking/bullet charges, technique charges].
# Groups (0x1bdee0 / AotI 0x1be560): bits 0..2 one at a time (burn, poison, virus: ticking damage), 3 and 4 each
# (shock, silence), 5..9 one at a time (freeze, sleep, paralysis, confusion, charm: end after `hits` hits,
# 0x1be5b0), 0x400 instant death, 0x1000 / 0x2000 timed (Giresta heal / Megistar drain), 0x8000000 /
# 0x10000000 barrier charges (Dizas / Rentis, slot 1 / 2 +0xdf through 0x531210, 0x1bd4a0).

def _status_init(a):
    if getattr(a, "buffs", None) is None:
        a.buffs = {}
    if getattr(a, "ail", None) is None:
        a.ail = {}
    if getattr(a, "barrier", None) is None:
        a.barrier = [0, 0]


def _start_ticks(a, bit, rec, now):
    """The status slot of a ticking / hit-ended status: first tick one period after it starts (0x1bf040)."""
    secs, period, value, _, hits, mode = rec
    if getattr(a, "ail", None) is None:
        a.ail = {}
    a.ail[bit] = [now + period if period else None, period, value, mode, hits]


def chance_roll(base, mult, sta, sure):
    """0x1bd900: rand(100) < mult * (base - target STA) / 100, nothing when STA >= base; D flag 0x8000 skips it."""
    if sure:
        return True
    r = random.randrange(100)
    if sta >= base:
        return False
    return r < ((mult & 0xFFFF) * (base - sta) // 100) & 0xFFFF


def inflict(t, bits, level, mult, sta, sure=False, immune=0):
    """Attack block D's status (flags 0x2000, D+4 bits, D+0x12 level, D+0xe chance %, flag 0x8000 sure) on actor
    t: 0x1bf8f0 = 0x1bdee0 (ailments) then 0x1bdac0 (debuffs). Returns the SE2 bits that started."""
    _status_init(t)
    bits &= ~immune
    now, cur, got = time.monotonic(), se2(t), 0

    def able(i):
        return not cur & AIL_BLOCK[i] and 0 <= level < len(AILMENTS[i])

    def put(i, clear=0):
        nonlocal cur
        rec = AILMENTS[i][level]
        for b in [b for b in t.buffs if b & clear]:
            del t.buffs[b]
            t.ail.pop(b, None)
        b = 1 << i
        t.buffs[b] = (now + rec[0], 1.0) if rec[0] else (float("inf"), 1.0)
        if rec[1] or rec[4]:
            _start_ticks(t, b, rec, now)
        cur = (cur & ~clear) | b
        return b

    if bits & 0x400 and able(10) and chance_roll(AILMENTS[10][level][3], mult, sta, sure):
        return 0x400                                    # instant death (0x1bf8f0 then kills, 0x1bfa30 / 0x1bd610)
    for group, clear in (((0, 1, 2), 0x7), ((3,), 0), ((4,), 0), ((5, 6, 7, 8, 9), 0x3E0), ((12,), 0), ((13,), 0)):
        for i in group:
            if bits & (1 << i) and able(i) and chance_roll(AILMENTS[i][level][3], mult, sta, sure):
                got |= put(i, clear)
                break
    for k, b in ((0, 0x8000000), (1, 0x10000000)):    # barrier charges: count = level when higher (0x1bd4a0)
        if bits & b and t.barrier[k] < level < 5:
            t.barrier[k] = level
            got |= b
    if bits & 0x40000:
        bits |= 0x3C000
    for k in range(4):                                  # debuffs: row 1 of the buff slots, clear the buff
        b = 1 << (14 + k)
        if bits & b and 0 <= level < len(DEBUFFS[k]) and chance_roll(100, mult, sta, sure):
            secs, m = DEBUFFS[k][level]
            t.buffs[b] = (now + secs, _mult(m))
            t.buffs.pop(1 << (19 + k), None)
            got |= b
    return got


def break_on_hit(t):
    """0x1be5b0: a landed damaging hit counts down every hit-ended status; at 0 its group ends."""
    for b, slot in list((getattr(t, "ail", None) or {}).items()):
        if slot[4] and b in getattr(t, "buffs", {}):
            slot[4] -= 1
            if slot[4] <= 0:
                del t.buffs[b]
                del t.ail[b]


def clear_status(a):
    """Death (0x1bd610 cures 0x187bf3ff / AotI 0x387bf3ff) ends every status and barrier."""
    clear_buffs(a)
    if getattr(a, "ail", None):
        a.ail.clear()
    if getattr(a, "barrier", None):
        a.barrier[:] = [0, 0]


def next_event(a):
    """Seconds until a's next status expiry or tick, or None."""
    times = [end for end, _ in (getattr(a, "buffs", None) or {}).values()]
    times += [s[0] for b, s in (getattr(a, "ail", None) or {}).items() if s[0] is not None]
    times = [x for x in times if x != float("inf")]
    if not times:
        return None
    return max(0.0, min(times) - time.monotonic())


def status_step(a, hp, max_hp, now=None):
    """Due ticks and expiries of actor a (HP hp of max_hp). Returns (events, hp): ("dmg", amount) for the
    ticking ailments and Megistar's drain (0x1bf040: % of max HP or flat, at least 1, never below 1 HP),
    ("heal", amount) for Giresta's (0x1beac0, at least 1, up to max HP), ("end", bits) for statuses that ran out;
    each event also carries the HP after it."""
    now = time.monotonic() if now is None else now
    before = 0
    for b in getattr(a, "buffs", None) or {}:
        before |= b
    events = []
    for b, s in sorted((getattr(a, "ail", None) or {}).items()):
        if s[0] is None or s[0] > now or b not in a.buffs or a.buffs[b][0] < s[0]:
            continue
        s[0] += s[1]
        amount = max_hp * s[2] // 100 if s[3] == 1 else s[2]
        amount = max(1, min(amount, 0xFFFF))
        if b == 0x1000:
            amount = min(amount, max_hp - hp)
            if amount > 0 and hp > 0:
                hp += amount
                events.append(("heal", amount, hp))
        elif hp > 1:
            amount = min(amount, hp - 1)
            hp -= amount
            events.append(("dmg", amount, hp))
    for b in [b for b, (end, _) in (getattr(a, "buffs", None) or {}).items() if end <= now]:
        del a.buffs[b]
    after = se2(a)
    for b in [b for b in (getattr(a, "ail", None) or {}) if b not in a.buffs]:
        del a.ail[b]
    if before & ~after:
        events.append(("end", before & ~after, hp))
    return events, hp


def _tick_entries(a, actor_id, events):
    se1 = {"dmg": 0x1, "heal": 0x80000, "end": 0}
    return [tick_entry(actor_id, se1[k], se2(a), hp, amount if k != "end" else 0,
                       tick_block(k, amount, amount if k == "end" else 0), getattr(a, "barrier", None) or (0, 0))
            for k, amount, hp in events]


def player_ticks(p):
    """Due status ticks / expiries of player p: its HP updated, 0x0E07 entries for the zone."""
    if not p.state:
        return []
    mx = progression.derived(p.raw, p.state)["max_hp"]
    events, hp = status_step(p, player_hp(p), mx)
    p.state["hp"] = hp
    return _tick_entries(p, p.lid, events)


def enemy_ticks(e):
    """Due status ticks / expiries of enemy e (never killed by them, 0x1bf310): 0x0E07 entries for the zone."""
    if e.dead:
        return []
    events, e.hp = status_step(e, e.hp, e.max_hp)
    return _tick_entries(e, e.id, events)


def tick_entry(actor_id, se1, se2_bits, hp, amount, D, barrier=(0, 0)):
    """A 0x0E07 entry for a status tick, as the offline room builds one (0x1bb3a0 -> 0x1bb150): attacker
    0xFFFFFFFF, both actor blocks the actor's own SE1 / SE2 / HP, D = the room's block."""
    blk = u32(se1) + u32(se2_bits) + u32(hp) + bytes(barrier) + bytes(2)
    return (bytes([1, 0, 0, 0]) + u32(amount) + bytes(12) + u32(0xFFFFFFFF) + u32(actor_id) + bytes(8)
            + blk + blk + bytes(16) + D)


def tick_block(kind, amount, bits=0):
    """D of a tick (0x1bb3a0): damage 0x115210 with the amount as bonus, heal 0x1400 | 0x400, an expiry
    0x2400 | 0x100000 with the bits that ended (D+4)."""
    D = bytearray(20)
    if kind == "dmg":
        struct.pack_into("<IIhhh", D, 0, 0x115210, 0, 0, min(amount, 0x7FFF), 0)
    elif kind == "heal":
        struct.pack_into("<IIhhh", D, 0, 0x1400, 0, 0, min(amount, 0x7FFF), 0)
    else:
        struct.pack_into("<II", D, 0, 0x102400, bits)
    D[0xE] = 100
    return bytes(D)


def element_mult(acode, apct, dcode, dpct, game=None):
    """0x1bd050: same pair (bits 0..2) with the other polarity (bit 3) = weak, same element = resists; the
    attack % (D+0x11) and the defence % (block +0xe) each count up to 100 (0x1c05a0 / 0x1bf520)."""
    if not acode & dcode & 7:
        return 1.0
    s = ELEMENT_SCALE.get(game or games.current(), 1.0)
    a = f32(f32(float(min(apct, 100))) / 100.0) + f32(f32(float(min(dpct, 100))) / 100.0)
    return f32(1.0 + s * a) if (acode ^ dcode) & 8 else f32(1.0 - s * a)


# --- equipment (line shield + 4 units, 0x263db0 / AotI 0x268c70) -----------------------------------------
UNIT_STATS = {1: (6, 7), 2: (0, 2), 3: (1, 3), 4: (4, 5)}   # unit type -> stat slots of rec +4 / +6
ARMOR_STATS = ((4, 0), (6, 6), (8, 1), (0xA, 7), (0xC, 2), (0xE, 3), (0x10, 4))   # line shield rec offset -> stat
IMMUNE_BYTES = ((0x9, (0x1, 0x2, 0x4, 0x8, 0x10)), (0xA, (0x20, 0x40, 0x80, 0x100)), (0xB, (0x400, 0xF80000, 0x7C000)))


def equipment(p):
    """What p's equipped line shield and units add (8 stats ATP DFP ATA EVP STA S5 TP MST), the shield's element
    code and % (0x261ad0: rec[1] low nibble on PSU, rec[1] on AotI (0xaccf0), % rec[2], plus a type-3 unit's
    rec+8, at most 127) and the type-3 unit's status immunities (0x2632d0 / 0x2680f0: rec+9..+0xB bit masks)."""
    st = getattr(p, "state", None) or {}
    game = getattr(p, "game", None) or games.current()
    out = {"stats": [0] * 8, "element": 0, "pct": 0, "immune": 0}
    db = games.json_data("items.json", game=game) or {}
    unit3 = None
    for slot in st.get("inv", []):
        if not slot.get("active"):
            continue
        it = db.get(slot["id"])
        if not it or it.get("cat") not in (2, 8) or not it.get("rec"):
            continue
        r = bytes.fromhex(it["rec"])
        if it["cat"] == 2 and len(r) >= 0x12:
            for o, k in ARMOR_STATS:
                out["stats"][k] += struct.unpack_from("<h", r, o)[0]
            e = r[1] if game == games.AOTI else r[1] & 0xF
            if 1 <= e <= 6:
                out["element"], out["pct"] = ELEMENT_CODE[e], r[2]
        elif it["cat"] == 8 and len(r) >= 0xC:
            k1, k2 = UNIT_STATS.get(bytes.fromhex(slot["id"])[1], (None, None))
            if k1 is None:
                continue
            out["stats"][k1] += struct.unpack_from("<h", r, 4)[0]
            out["stats"][k2] += struct.unpack_from("<h", r, 6)[0]
            if bytes.fromhex(slot["id"])[1] == 3:
                unit3 = r
    if unit3 is not None:
        if out["element"]:
            out["pct"] = max(0, min(127, out["pct"] + struct.unpack_from("<b", unit3, 8)[0]))
        for o, masks in IMMUNE_BYTES:
            for i, m in enumerate(masks):
                if unit3[o] & (1 << i):
                    out["immune"] |= m
    return out


def total_stats(p, base=None):
    """Base stats (progression.derived) plus equipment (0x263ad0 mode -1 without the weapons)."""
    base = base if base is not None else progression.derived(p.raw, p.state)["stats"]
    eq = equipment(p)["stats"]
    return [max(0, min(0x7FFF, b + e)) for b, e in zip(base, eq)] if any(eq) else base


def defence_element(t):
    """(element code, %) of the defence block: an enemy's category and level row % (0x281030), a player's line
    shield (0x261ad0)."""
    if isinstance(t, Enemy):
        return t.element, t.element_pct
    if getattr(t, "state", None):
        eq = equipment(t)
        return eq["element"], eq["pct"]
    return 0, 0


def enemy_stats_now(e):
    """An enemy's defence stats with its slot multipliers (0x281030: DFP x slot 1, MST x slot 2, EVP x slot 3)."""
    return buffed_stats(e, e.stats)


def sta(t, stats=None):
    """The STA a status chance is measured against (stat 4; 0x1bd900 asks the target's status for it)."""
    if isinstance(t, Enemy):
        return t.stats[4]
    return (stats or total_stats(t))[4]


def support_tech(p, w):
    """(PA key, level byte) when hit word w is a technique weapon's slot holding a support technique, else None."""
    st = p.state or {}
    slot = next((x for x in st.get("inv", []) if x["uuid"] == w & 0x1FFFFFFF), None)
    from psu import growth, items
    it = items.db().get(slot["id"]) if slot else None
    if not it or it["cat"] != 1 or bytes.fromhex(it["consts"])[0xE] != 3:
        return None
    techs, t = slot.get("techs") or [], w >> 29
    if t >= len(techs) or not 1 <= techs[t][0] <= 0x38:
        return None
    key = growth.tech_key(techs[t][0])[:6]
    return (key, techs[t][1]) if key in SUPPORT else None


def support_attack(p, w, D, game=None):
    """Attack block D for a support technique hit (0x262600 + 0xb4fd0), or None when the technique's block is
    not a support one. Returns (D, flags, SE2 bits, power, level index)."""
    tech = support_tech(p, w)
    if tech is None:
        return None
    game = game or games.current()
    key, level = tech
    kind, tiers = SUPPORT[key]
    tier = min(level // 10, len(tiers) - 1)
    pw, inc, rflags, effect, strength = tiers[tier]
    pct = max(0, min(0x7FFF, pw + inc * (level - 10 * tier)))
    flags, bits, lvl = 0xC, 0, 0
    if kind == 1 and key[2:4] == "05":
        if rflags & 0x40:
            flags |= 0x800
        if rflags & 0x20:
            flags |= 0x2400
            bits = CURE_BITS.get(game, CURE_BITS[games.PSU])
        if pct > 0:
            flags |= 0x1400
            if rflags & 4:
                flags |= 0x20000
    elif kind != 1:
        pct = 0
    if 1 <= strength <= 5:                               # 0x2646e0 / 0x269590
        flags |= 0x2000
        bits = EFFECT_BITS.get(effect, 0)
        if bits & 0xF80000:
            flags |= 0x2400
        elif bits & DAMAGING_BITS.get(game, DAMAGING_BITS[games.PSU]):
            flags |= 0x200
        lvl = strength - 1
    flags |= 0x4000 | (0x10000 if rflags & 2 else 0) | (0x8000 if rflags & 1 else 0)
    if not flags & 0x400 or flags & 0x200:
        return None
    power = 0
    if pct:
        st = p.state
        slot = next(x for x in st.get("inv", []) if x["uuid"] == w & 0x1FFFFFFF)
        from psu import items
        it = items.db().get(slot["id"], {})
        base = total_stats(p)[6] + (it.get("atp") or 0) + slot.get("atp_bonus", 0)   # TP with equipment (0x263ad0)
        power = _scaled(int(f32(f32(float(base * pct)) / 100.0)), buff_mult(p, 2))
        power = max(0, min(0x7FFF, power))
    out = bytearray(D)
    struct.pack_into("<II", out, 0, flags, bits)
    struct.pack_into("<hh", out, 0xA, 0, power)
    out[0x12] = lvl
    return bytes(out), flags, bits, power, lvl


def apply_support(target, flags, bits, power, lvl):
    """The support block on a player (0x1c0350): (amount for the result's damage field, SE1) or None."""
    mx = progression.derived(target.raw, target.state)["max_hp"]
    hp = player_hp(target)
    if hp == 0:
        if not flags & 0x800:
            return None
        hp = min(mx, mx if flags & 0x20000 else max(1, power))
        target.state["hp"] = hp
        return hp, 0x40000
    amount, se = 0, 0
    if flags & 0x1000:
        healed = min(mx - hp if flags & 0x20000 else power, mx - hp)
        if healed > 0:
            target.state["hp"] = hp + healed
            amount, se = healed, se | 0x80000
    if flags & 0x2000:
        if cure(target, bits) and not flags & 0x100000:
            se |= 0x80000
        if apply_buffs(target, bits, lvl):
            se |= 0x1000000
    return (amount, se) if se else None


def tech_link(p, w):
    """(PA key, level byte, slot list) of the technique in slot w >> 29 of the technique weapon w names, or None."""
    st = p.state or {}
    slot = next((x for x in st.get("inv", []) if x["uuid"] == w & 0x1FFFFFFF), None)
    from psu import growth, items
    it = items.db().get(slot["id"]) if slot else None
    if not it or it["cat"] != 1 or bytes.fromhex(it["consts"])[0xE] != 3:
        return None
    techs, t = slot.get("techs") or [], w >> 29
    if t >= len(techs) or not 1 <= techs[t][0] <= 0x38:
        return None
    return growth.tech_key(techs[t][0])[:6], techs[t][1], techs


def school_bonus(techs):
    """0xb09f0: +4 element % for each further linked technique of the first one's school, 0 when they mix."""
    schools = [(c - 1) // 10 + 1 for c, _ in techs[:4] if c]
    return 4 * (len(schools) - 1) if schools and len(set(schools)) == 1 else 0


def attack_status(p, w, slot, it, game=None):
    """(SE2 bits, level, strength byte) a player's hit carries (0x2646e0: strength 1..5 -> flag 0x2000, D+4 the
    effect's bits, D+0x12 strength - 1): a technique's tier record (TECH_FX), a bullet PA's base param
    +0xA effect / +0xB strength (paparam.json), else nothing."""
    game = game or games.current()
    if bytes.fromhex(it["consts"])[0xE] == 3:
        link = tech_link(p, w)
        if link is None or link[0] not in TECH_FX:
            return 0, 0
        key, level, _ = link
        tiers = TECH_FX[key]
        eff, strength = tiers[min(level // 10, len(tiers) - 1)]
    else:
        from psu import growth, items
        key = growth.weapon_key(slot, w)
        if not key or key[:2] != "05":
            return 0, 0
        e = (items.paparam() or {}).get("pa", {}).get(key + "00")
        if not e:
            return 0, 0
        base = bytes.fromhex(e["base"][min(slot.get("pa", [0, 0])[1] // 10, len(e["base"]) - 1)])
        eff, strength = base[0xA], base[0xB]
    bits = EFFECT_SE2[eff] if eff < len(EFFECT_SE2) else 0
    if not 1 <= strength <= 5 or not bits & DAMAGING_BITS.get(game, DAMAGING_BITS[games.PSU]):
        return 0, 0
    return bits, strength - 1


def player_attack(p, attack_id, D):
    """Attack block D for a player's hit (the server builds it from the attack id, 0x5f120): the weapon with
    UUID attack_id & 0x1FFFFFFF -> power = player ATP + weapon ATP + grind ATP bonus (TP for techniques, 0x262600;
    MST is the defence), accuracy = player ATA + weapon ATA, each with what the line shield and units add
    (0x263ad0 / 0x263db0); damaging flags (striking 0x1204, technique 0x120c with the sure-hit 0x4000), the
    client's own flag bits kept. Element: the weapon's
    (constants +0xB code, +0xC %; 0x262db0) or a technique's school with 10 + level + school bonus % (0x262600,
    0xb09f0); a technique's or bullet PA's status (attack_status) sets 0x2000, D+4, D+0x12 and D+0xe = the
    attacker's S5 (stat 5 with equipment, the chance multiplier of 0x1bd900)."""
    st = p.state or {}
    uuid = attack_id & 0x1FFFFFFF
    slot = next((x for x in st.get("inv", []) if x["uuid"] == uuid), None)
    from psu import items
    it = items.db().get(slot["id"], {}) if slot else {}
    if it.get("cat") != 1:
        return D
    stats = total_stats(p)
    c = bytes.fromhex(it["consts"])
    tech = c[0xE] == 3
    power = (stats[6] if tech else stats[0]) + (it.get("atp") or 0) + slot.get("atp_bonus", 0)   # tech: TP (stat 6), 0x262600
    acc = stats[2] + (it.get("ata") or 0)
    if se2(p):                         # buffs: ATP / TP slot on power, ATA slot (0x262db0, 0x262600)
        power = _scaled(power, buff_mult(p, 2 if tech else 0))
        acc = acc if tech else _scaled(acc, buff_mult(p, 3))
    flags = struct.unpack_from("<I", D, 0)[0] | 0x200 | 0x1000 | (0x4008 if tech else 0x4)   # 0x262db0: 0x1204
    out = bytearray(D)
    struct.pack_into("<hhh", out, 8, min(acc, 0x7FFF), 0, min(power, 0x7FFF))
    if tech:
        link = tech_link(p, attack_id)
        if link is not None:
            out[0x10] = ELEMENT_CODE[int(link[0][2:4], 16)] if 1 <= int(link[0][2:4], 16) <= 6 else 0
            out[0x11] = min(100, link[1] + 10 + school_bonus(link[2]))
    elif 1 <= c[0xB] <= 6:
        out[0x10], out[0x11] = ELEMENT_CODE[c[0xB]], min(c[0xC], 127)
    bits, level = attack_status(p, attack_id, slot, it)
    if bits:
        flags |= 0x2000
        struct.pack_into("<I", out, 4, bits)
        struct.pack_into("<h", out, 0xE, min(stats[5], 0x7FFF))
        out[0x12] = level
    struct.pack_into("<I", out, 0, flags)
    return bytes(out)


def _barrier(a):
    """The 2 bytes after an actor block's HP: its Dizas / Rentis charges (0x1bb150 via 0x1bd3a0), then 2 zero."""
    b = getattr(a, "barrier", None) or (0, 0)
    return bytes([b[0] & 0xFF, b[1] & 0xFF, 0, 0])


def immunity(t):
    """SE2 bits t cannot get (status +0xc): a player's type-3 unit (SetStatusInvalidFlagByItem 0x297750)."""
    return 0 if isinstance(t, Enemy) or not getattr(t, "state", None) else equipment(t)["immune"]


def growth_hit(p, w):
    """(Player, PA entry, level up) for a counted non-damaging technique result (0x1ba5a0), or None."""
    from psu import growth
    g = growth.pa_hit(p.state, w) if p is not None and p.state else None
    return (p, g[0], g[1]) if g is not None else None


def status_attack(p, tl, D):
    """D of a kind 3 (Jellen, Zalure, Zoldeel) or Dizas / Rentis hit (0x262600 + 0xb4fd0): no power, flags
    0x200 | 0x2000 | 0x4000 | 0xC, D+4 the effect's bits, D+0x12 strength - 1, D+0xe the caster's S5."""
    key, level, _ = tl
    tiers = TECH_FX[key]
    eff, strength = tiers[min(level // 10, len(tiers) - 1)]
    out = bytearray(D)
    struct.pack_into("<IIhhhh", out, 0, struct.unpack_from("<I", D, 0)[0] | 0x200 | 0x2000 | 0x4000 | 0xC,
                     EFFECT_SE2[eff], 0, 0, 0, min(total_stats(p)[5], 0x7FFF))
    out[0x12] = max(0, strength - 1)
    return bytes(out)


def debuffed_attack(e, D):
    """An enemy's own debuffs on the block its client built: power x slot 0 (slot 2 for techniques), accuracy x
    slot 3 (the offline enemy builds D from its status, 0x1bd560; online its client does not know the slots)."""
    flags, _, acc, bonus, power = struct.unpack_from("<IIhhh", D, 0)
    out = bytearray(D)
    struct.pack_into("<h", out, 8, _scaled(acc, buff_mult(e, 3)))
    struct.pack_into("<h", out, 0xC, _scaled(power, buff_mult(e, 2 if flags & 0xC == 0xC else 0)))
    return bytes(out)


def after_hit(t, D, se, hp, dmg, stats=None, statused=None):
    """After the damage (0x1c0350): a landed damaging hit (SE1 0x1 with damage) counts down t's hit-ended
    statuses unless D has 0x100000 (0x1bf7c0 -> 0x1be5b0); then a block with 0x2000 tries its status
    (0x1bf8f0; SE1 0x800000 when one starts; instant death takes the rest of the HP). Returns (se, hp, dmg)."""
    flags, bits = struct.unpack_from("<II", D, 0)
    landed = dmg > 0 and se & 0x1
    if landed and not flags & 0x100000:
        break_on_hit(t)
    if flags & 0x2000 and bits and hp > 0 and (landed or (flags & 0x200 and not flags & 0x1000)):
        mult, = struct.unpack_from("<h", D, 0xE)
        got = inflict(t, bits, D[0x12], mult, sta(t, stats), bool(flags & 0x8000), immunity(t))
        if got:
            se |= 0x800000
            if statused is not None:
                statused.append(t)
            if got & 0x400:
                dmg, hp = dmg + hp, 0
    return se, hp, dmg


def item_status(user, attack_id, target):
    """A cure or status item used on target (0x2623c0): constants +7 flag 0x40 -> cure of 0x3c3ff (AotI
    0x2003c3ff), D 0x2400 (Antimate, Sol Atomizer, Bruce's Ointment); else a low nibble (strength) with the
    effect id at +6 -> its bits through 0x2646e0 (rides: buffs; gums). Returns (SE1, D) or None (no result)."""
    uuid = attack_id & 0x1FFFFFFF
    slot = next((x for x in (user.state or {}).get("inv", []) if x["uuid"] == uuid), None)
    slot = slot or getattr(user, "recent_use", {}).get(uuid)
    if slot is None:
        return None
    from psu import items
    it = items.db().get(slot["id"])
    if not it or it["cat"] != 3:
        return None
    c = bytes.fromhex(it["consts"])
    game = games.current()
    if c[7] & 0x80 or (c[7] & 0x20 and struct.unpack_from("<h", c, 4)[0]):
        return None
    D = bytearray(20)
    D[0xE] = 100
    if c[7] & 0x40:
        bits, lvl, flags = CURE_BITS.get(game, CURE_BITS[games.PSU]), 0, 0x2400
    elif 1 <= c[7] & 0xF <= 5 and c[6] < len(EFFECT_SE2) and EFFECT_SE2[c[6]]:
        bits, lvl = EFFECT_SE2[c[6]], (c[7] & 0xF) - 1
        flags = 0x2400 | 0x2000 if bits & 0xF83000 else 0x2200
    else:
        return None
    struct.pack_into("<II", D, 0, flags, bits)
    D[0x12] = lvl
    if player_hp(target) == 0:
        return None
    se = 0
    if flags & 0x400:
        if c[7] & 0x40 and cure(target, bits):
            se |= 0x80000
        if not c[7] & 0x40 and apply_buffs(target, bits, lvl):
            se |= 0x1000000
    else:
        st = total_stats(target)
        if inflict(target, bits, lvl, 100, st[4], False, immunity(target)):
            se |= 0x800000
    return (se, bytes(D)) if se else None


def player_hp(p):
    st = p.state
    return st.get("hp", progression.derived(p.raw, st)["max_hp"]) if st else 0


def resolve(me, players, body, level):
    """One C>S 0x0E00 from `me`. Returns (entries, kills, hurt, grown): 0x68-byte 0x0E07 entries, [Enemy]
    killed by this batch (attacker GID in .killer), [Player] whose HP changed, [(Player, PA entry, level up)]."""
    count, = struct.unpack_from("<I", body, 0x24)
    enemies = zone_enemies(me.zone)
    by_lid = {p.lid: p for p in players}
    entries, kills, hurt, grown = [], [], [], []
    buffed = []                                         # players a support technique changed (expiry watch)
    statused = []                                       # enemies with a new status (tick / expiry watch)
    broken = []                                         # (box target id, [map index, params, hits], hit pos)
    for i in range(min(count, 78)):
        o = 0x30 + 0x50 * i
        if o + 0x50 > len(body):
            break
        h = body[o:o + 0x50]
        pos, (src, dst) = h[0:0xC], struct.unpack_from("<II", h, 0xC)
        B, D = h[0x1C:0x2C], h[0x3C:0x50]
        attacker_p = by_lid.get(src)
        target_p = by_lid.get(dst)
        sup = support_attack(attacker_p, struct.unpack_from("<I", h, 0x14)[0], D)             if attacker_p is not None and attacker_p.state else None
        if sup is not None:                                 # support technique: players only, no damage
            if target_p is None or not target_p.state:
                continue
            D, flags, bits, power, lvl = sup
            res = apply_support(target_p, flags, bits, power, lvl)
            if res is None:
                continue
            amount, se = res
            hurt.append(target_p)
            buffed.append(target_p)
            if se & 0x10C0000:                              # PA EXP for a non-damaging result (0x1ba5a0)
                from psu import growth
                g = growth.pa_hit(attacker_p.state, struct.unpack_from("<I", h, 0x14)[0])
                if g is not None:
                    grown.append((attacker_p, g[0], g[1]))
            a_se = se if attacker_p is target_p else 0
            entries.append(bytes([1, 0, 0, 0]) + u32(amount) + pos + u32(src) + u32(dst) + h[0x14:0x1C]
                           + u32(a_se) + u32(se2(attacker_p)) + u32(player_hp(attacker_p)) + bytes(4)
                           + u32(se) + u32(se2(target_p)) + u32(player_hp(target_p)) + bytes(4) + B + D)
            continue
        if target_p is not None and attacker_p is not None and target_p.state and player_hp(target_p) == 0:
            heal = revive_amount(attacker_p, struct.unpack_from("<I", h, 0x14)[0], target_p)
            if heal is None:
                continue
            target_p.state["hp"] = heal
            hurt.append(target_p)
            entries.append(bytes([1, 0, 0, 0]) + u32(heal) + pos + u32(src) + u32(dst) + h[0x14:0x1C]
                           + u32(0) + u32(se2(attacker_p)) + u32(player_hp(attacker_p)) + bytes(4)
                           + u32(0x40000) + u32(se2(target_p)) + u32(heal) + bytes(4) + B + D)
            continue
        if target_p is not None and attacker_p is not None and target_p.state:   # a healing item on a player
            heal = heal_amount(attacker_p, struct.unpack_from("<I", h, 0x14)[0], target_p)
            if heal is not None:
                mx = progression.derived(target_p.raw, target_p.state)["max_hp"]
                hp = min(mx, player_hp(target_p) + heal)
                target_p.state["hp"] = hp
                hurt.append(target_p)
                entries.append(bytes([1, 0, 0, 0]) + u32(heal) + pos + u32(src) + u32(dst) + h[0x14:0x1C]
                               + u32(0) + u32(se2(attacker_p)) + u32(player_hp(attacker_p)) + bytes(4)
                               + u32(0x80000) + u32(se2(target_p)) + u32(hp) + bytes(4) + B + D)
                continue
        if target_p is not None and attacker_p is not None and target_p.state:   # cure / status item (0x2623c0)
            res = item_status(attacker_p, struct.unpack_from("<I", h, 0x14)[0], target_p)
            if res is not None:
                se, D2 = res
                hurt.append(target_p)
                buffed.append(target_p)
                entries.append(bytes([1, 0, 0, 0]) + u32(0) + pos + u32(src) + u32(dst) + h[0x14:0x1C]
                               + u32(se if attacker_p is target_p else 0) + u32(se2(attacker_p))
                               + u32(player_hp(attacker_p)) + _barrier(attacker_p)
                               + u32(se) + u32(se2(target_p)) + u32(player_hp(target_p)) + _barrier(target_p)
                               + B + (D2 or D))
                continue
        w, = struct.unpack_from("<I", h, 0x14)
        tl = tech_link(attacker_p, w) if attacker_p is not None and attacker_p.state else None
        if tl is not None and tl[0] in DEBUFF_TECHS + BARRIER_TECHS:   # kind 3 on enemies, kind 2 on players
            if (tl[0] in BARRIER_TECHS) != (target_p is not None):
                continue
            t = target_p if target_p is not None else enemies.get(dst)
            if t is None and target_p is None:
                from psu import instance as _inst
                if dst in _inst.get(me.zone).boxes:
                    continue
                t = enemies[dst] = new_enemy(me.zone, dst, level)
            if t is None or getattr(t, "dead", False) or (target_p is not None and not target_p.state):
                continue
            D = status_attack(attacker_p, tl, D)
            got = inflict(t, struct.unpack_from("<I", D, 4)[0], D[0x12], struct.unpack_from("<h", D, 0xE)[0],
                          sta(t), bool(struct.unpack_from("<I", D, 0)[0] & 0x8000), immunity(t))
            if not got:
                continue
            (buffed if target_p is not None else statused).append(t)
            if target_p is None:
                t.hitters.add(attacker_p.gid)
            gr = growth_hit(attacker_p, w)
            if gr is not None:
                grown.append(gr)
            se, a_se = 0x800000, 0
            t_hp = player_hp(t) if target_p is not None else t.hp
            entries.append(bytes([1, 0, 0, 0]) + u32(0) + pos + u32(src) + u32(dst) + h[0x14:0x1C]
                           + u32(se if attacker_p is t else 0) + u32(se2(attacker_p)) + u32(player_hp(attacker_p))
                           + _barrier(attacker_p) + u32(se) + u32(se2(t)) + u32(t_hp) + _barrier(t) + B + D)
            continue
        if target_p is not None:                           # an enemy (or PvP) hitting a player
            if not target_p.state:
                continue
            e = enemies.get(src)
            if e is None and attacker_p is None:
                e = enemies[src] = new_enemy(me.zone, src, level)
            if e is not None and se2(e) & 0x7C000:        # its debuffs: ATP / TP slot on power, ATA slot (0x5ecb0)
                D = debuffed_attack(e, D)
            stats = total_stats(target_p)
            dmg, se = damage(D, buffed_stats(target_p, stats), player_hp(target_p),
                             e.stats if e is not None else None, target_p)
            hp = max(0, player_hp(target_p) - dmg)
            se, hp, dmg = after_hit(target_p, D, se, hp, dmg, stats, statused=buffed)
            target_p.state["hp"] = hp
            if hp == 0:
                se |= 0x20000
                clear_status(target_p)
            hurt.append(target_p)
            a_se, a_hp = 0, e.hp if e else 0
            t_se, t_hp = se, hp
        else:                                              # a player hitting an enemy
            e = enemies.get(dst)
            if e is None:
                from psu import instance as _inst
                box = _inst.get(me.zone).boxes.get(dst)
                if box is not None:                    # a breakable box (TServerActorObjBreak 0x3b0080)
                    box[2] += 1
                    need = max(1, box[1][4]) if len(box[1]) > 4 else 1
                    if box[2] == need:
                        broken.append((dst, box, pos))
                    continue
                e = enemies[dst] = new_enemy(me.zone, dst, level)
            if e.dead:
                continue
            if attacker_p is not None:                     # the client leaves D almost empty: build it
                D = player_attack(attacker_p, w, D)
            dmg, se = damage(D, enemy_stats_now(e), e.hp, None, e)
            hp = max(0, e.hp - dmg)
            se, hp, dmg = after_hit(e, D, se, hp, dmg, statused=statused)
            e.hp = hp
            if attacker_p is not None:
                e.hitters.add(attacker_p.gid)
                if dmg > 0 and attacker_p.state:                # PA EXP: +1 per landed hit (0x296180)
                    from psu import growth
                    g = growth.pa_hit(attacker_p.state, w)
                    if g is not None:
                        grown.append((attacker_p, g[0], g[1]))
            if e.hp == 0:
                se |= 0x20000
                e.dead = True
                clear_status(e)
                e.killer = attacker_p.gid if attacker_p is not None else me.gid
                e.killpos = pos
                kills.append(e)
            a_se, a_hp = 0, player_hp(attacker_p) if attacker_p is not None else 0
            t_se, t_hp = se, e.hp
        flags, = struct.unpack_from("<I", D, 0)
        flag2 = D[0x13] if flags & 0x80000 else 0
        attacker = attacker_p if attacker_p is not None else e if target_p is not None else None
        target = target_p if target_p is not None else e
        a_se2 = se2(attacker)                           # the client takes SE2 as sent (0x1bc0f0)
        t_se2 = se2(target)
        entries.append(bytes([1, flag2, 0, 0]) + u32(dmg) + pos + u32(src) + u32(dst) + h[0x14:0x1C]
                       + u32(a_se) + u32(a_se2) + u32(a_hp) + _barrier(attacker)
                       + u32(t_se) + u32(t_se2) + u32(t_hp) + _barrier(target) + B + D)
    resolve.broken = broken
    resolve.buffed = buffed
    resolve.statused = [e for e in dict.fromkeys(statused) if not e.dead]
    return entries, kills, hurt, grown


def c0e07(d, entries):
    return packet(0x0E07, hdr(d) + u32(len(entries)) + b"".join(entries))


def c0e08(d, e):
    """Laid out for the recipient's disc (d.game; relayed from the sender's task). PSU (0x1bbf10) copies a
    0x114-byte image from +0x24 whose first word it skips; AotI (0x1bbac0) copies 0x110 bytes, the same fields
    4 bytes earlier (SE1 +0, SE2 +4, level +0xC, stats +0x10, HP +0x20, max HP +0x24): the EGS/PC status
    layout its 0x010D character block uses too (progression.status_block)."""
    img = (bytes(4) + u32(0) + u32(0) + u32(0) + bytes([e.level & 0xFF, 0, 0, 0])
           + struct.pack("<8h", *e.stats) + u32(e.hp) + u32(e.max_hp))
    img += bytes(0x114 - len(img))
    if getattr(d, "game", games.PSU) == games.AOTI:
        img = img[4:]
    return packet(0x0E08, hdr(d) + u32(e.id) + img)


def c0117(d, hp, p=None):
    """Player HP/status refresh (0x14ecd0, frame 0x44): GID, LID, SE1, SE2, HP, 2 bytes; about player p (default
    the recipient itself). The client replaces SE2 with it, so p's active buff bits go along."""
    p = d if p is None else p
    return packet(0x0117, hdr(d) + u32(p.gid) + u32(p.lid) + u32(0) + u32(se2(p)) + u32(hp) + bytes(4))


def c1022(d, hp, slot):
    return packet(0x1022, hdr(d) + u32(hp) + bytes([slot & 0xFF, 0, 0, 0]))


def c0115(d, enemy_id):
    return packet(0x0115, hdr(d) + u32(d.gid) + u32(d.lid) + u32(enemy_id) + progression.level_block(d.state, getattr(d, "game", None)))


def add_exp(p, amount):
    """Add EXP to p.state; returns True when the level rose (HP refilled then)."""
    st = p.state
    top = progression.level_cap()
    cap = progression.exp_for_level(p.raw, top)
    st["exp"] = min(st["exp"] + amount, cap)
    old = st["level"]
    while st["level"] < top and st["exp"] >= progression.exp_for_level(p.raw, st["level"] + 1):
        st["level"] += 1
    if st["level"] != old:
        st["hp"] = progression.derived(p.raw, st)["max_hp"]
        return True
    return False


def kill_exp(e):
    """{gid: EXP} for a dead enemy: killer 100 %, other hitters 70 %."""
    out = {e.killer: e.exp}
    for gid in e.hitters:
        if gid != e.killer:
            out[gid] = e.exp * 7 // 10
    return out


# --- PP: what an attack costs, natural regeneration (the room, PSU / AotI) -----------------------------------
# The client reports what it does as casts the server relays; the room charges PP from them (TServerActorPlayer):
#   0x0102 (handler 0x604e0, AotI 0x60c10): +0x34 action, +0x38 u32 step word, +0x3C position; action 2 / 3 ->
#     0x295e70 (AotI 0x298910) with kind = action; 0x010F (0x60130, AotI 0x60770): +0x34 action 1 -> kind 2, step 0
#     (a left-hand shot); 0x0104 (0x60400, AotI 0x60af0): +0x34 u32 bit 2 -> the left-hand / second-pair mode (actor
#     +0x388 bit 8), kept until the next 0x0104.
# 0x295810 (AotI 0x2982a0) picks the weapon and its kind of cost, from the right-hand weapon's style (constants
# +0xE: 0 striking, 1 / 2 bullets, 3 technique) and hand (+0xD: 0 two-handed, 1 one-handed, 2 left-hand):
#   no right-hand weapon, or a one-handed one in left-hand mode -> the left-hand weapon (style 1 / 2) shoots;
#   technique -> tech slot kind - 2 (kind itself for a two-handed rod in that mode), 0x295b50: GetTechParam +0x26
#     (paparam base +0x26), nothing when base +0 has 0x40 without low nibble 5;
#   two-handed gun, kind 2 -> 0x295c60: GetBulletParam (linked bullet PA, else the weapon type's default bullet)
#     base +4, a % of max PP when base +3 bit 1;
#   striking, kind 3 (a PA; kind 2 is the plain combo) -> 0x295d60: GetSkillParam step (word >> 4) - 2 (0..2):
#     base +4 + step, a % of max PP when base +3 bit 1 (0xb5fa0; the client's own check 0xba890 reads the same);
# times the line shield / unit factor of 0x2640c0 / 0x264120 / 0x264180 (1.0 with the equip block the server sends,
# items.equip_block); the room then sends 0x0105 event 0x0A with -cost (0x295a30, AotI 0x2984f0).
# Regeneration (0x295f50, AotI 0x2989e0): every 5 s (0x264210, period 5 x frame rate from 0x264590) each equipped
# weapon below max gains base PP (itemWeaponParam +0x18, AotI +0x14) * 5 / the next s16, + a unit's bonus (0x2641e0,
# none here), except in room types 0 and 2 (0x61b90): the server regenerates in missions only.
PP_TICK = 5.0
# fallback "PP per tick" per weapon type 1..25 when $PSU_GAMEDATA/ppregen.json (extract/ppdata.py) lacks the weapon:
# the median of base PP * 5 / divisor over each type's weapons on the PSU disc (AotI's for the types PSU lacks)
PP_TICK_BY_TYPE = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 3, 5, 4, 5, 5, 5, 5, 3, 8, 9, 13, 10, 4, 3]


def _consts(slot):
    from psu import items
    it = items.db().get(slot["id"]) if slot else None
    return bytes.fromhex(it["consts"]) if it and it.get("cat") == 1 else None


def _pp_percent(base, k, mx):
    return mx * base[4 + k] // 100 if base[3] & 1 else base[4 + k]


def _tier(e, level):
    return bytes.fromhex(e["base"][min(level // 10, len(e["base"]) - 1)])


def pp_cost(slot, what, arg=0):
    """PP a weapon's attack costs: what = "tech" (arg = tech slot), "bullet", "striking" (arg = PA step)."""
    from psu import growth, items
    pa = (items.paparam() or {}).get("pa", {})
    mx = items.max_pp(slot)
    wtype = bytes.fromhex(slot["id"])[1]
    idx, level = (slot.get("pa") or [0, 0])[:2]
    if what == "tech":
        techs = slot.get("techs") or []
        if not 0 <= arg < len(techs) or not 1 <= techs[arg][0] <= 0x38:
            return 0
        e = pa.get(growth.tech_key(techs[arg][0]) + "00")
        if not e:
            return 0
        base = _tier(e, techs[arg][1])
        if base[0] & 0x40 and base[0] & 0xF != 5:
            return 0
        return max(0, struct.unpack_from("<h", base, 0x26)[0])
    if what == "bullet":
        if idx:
            e = pa.get(f"05{wtype:02x}{idx:02x}00")
            base = _tier(e, level) if e else None
        else:
            d = (items.paparam() or {}).get("bullet_default") or []
            h = d[wtype - 0x0E] if 0 <= wtype - 0x0E < len(d) else None
            base = bytes.fromhex(h)[4:] if h else None
        return _pp_percent(base, 0, mx) if base else 0
    if what == "striking" and 0 <= arg <= 2:
        e = pa.get(f"04{wtype:02x}{idx:02x}00")
        return _pp_percent(_tier(e, level), arg, mx) if e else 0
    return 0


def pp_attack(p, kind, word, left):
    """(weapon slot, PP cost) of attack action `kind` (2 / 3) with step word `word` (0x295810), or None."""
    if kind not in (2, 3) or not p.state:
        return None
    from psu import items
    eq = items.equipped_weapons(p.state)
    right = next((w for w in eq if _consts(w)[0xD] != 2), None)
    lefty = next((w for w in eq if _consts(w)[0xD] == 2), None)
    if right is None or (_consts(right)[0xD] != 0 and left):
        if left and lefty is not None and _consts(lefty)[0xE] in (1, 2):
            return lefty, pp_cost(lefty, "bullet")
        return None
    c = _consts(right)
    style, hand = c[0xE], c[0xD]
    if style == 3:
        return right, pp_cost(right, "tech", kind if left else kind - 2)
    if style == 1 and hand == 0 and kind == 2:
        return right, pp_cost(right, "bullet")
    if style == 0 and kind == 3:
        return right, pp_cost(right, "striking", (word >> 4) - 2)
    return None


def pp_cast(p, cmd, body):
    """A cast from p (relayed as before): the weapon whose PP an attack spent (stored), or None. Keeps the
    left-hand mode of 0x0104 in p.pp_left."""
    from psu import items
    if cmd == 0x0104 and len(body) >= 0x30:
        p.pp_left = bool(struct.unpack_from("<I", body, 0x2C)[0] & 2)
        return None
    if cmd == 0x0102 and len(body) >= 0x34 and body[0x2C] in (2, 3):
        kind, word = body[0x2C], struct.unpack_from("<I", body, 0x30)[0]
    elif cmd == 0x010F and len(body) >= 0x30 and body[0x2C] == 1:
        kind, word = 2, 0
    else:
        return None
    use = pp_attack(p, kind, word, getattr(p, "pp_left", False))
    if use is None or use[1] <= 0:
        return None
    slot, cost = use
    return slot if items.set_pp(slot, items.cur_pp(slot) - cost) else None


def pp_tick_amount(slot):
    """PP one regeneration tick gives (base PP * 5 / itemWeaponParam +0x1A)."""
    c = _consts(slot)
    if c is None:
        return 0
    div = (games.json_data("ppregen.json") or {}).get(slot["id"])
    if div:
        return struct.unpack_from("<H", c, 0)[0] * 5 // div
    wtype = bytes.fromhex(slot["id"])[1]
    return PP_TICK_BY_TYPE[wtype - 1] if 1 <= wtype <= len(PP_TICK_BY_TYPE) else 1


def pp_regen(st):
    """One regeneration tick for the equipped weapons; returns the slots that changed."""
    from psu import items
    out = []
    for w in items.equipped_weapons(st):
        if items.cur_pp(w) < items.max_pp(w) and items.set_pp(w, items.cur_pp(w) + pp_tick_amount(w)):
            out.append(w)
    return out
