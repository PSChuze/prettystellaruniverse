"""Shared game state: who is in which zone, and the packets that show players to each other.

Re-implemented from EGS (egs_zones.erl enter/leave/broadcast, egs_game.erl cast/info, egs_proto.erl
send_0111/010d/0205/0203/0201/0204/0233/0304). Every session registers a Player; the zone it stands
in (quest, zone) gives it a local id (LID, 0..1023) that its own packets and everyone else's use.

  enter zone  -> newcomer gets 0x0233 (the players already there, inside its load sequence);
                 the others get 0x0111 event 6, 0x010D, 0x0205, 0x0203, 0x0201 (EGS player_spawn)
  leave zone  -> the others get 0x0204
  cast        -> channel-1 commands (movement, animation) are rewritten with the sender's GID/LID and
                 the receiver's GID (EGS cast/info) and forwarded to the zone

PSU and AotI players share the world; these packets are built in the sender's task, so anything laid out
per disc keys on the recipient d.game (statically compared handlers, PSU_US.erx PSU / AotI):
  user record (0x0201 0x563a0 / 0x56c50, 0x010D 0x14efc0 / 0x14c540, 0x0233 0x546b0 / 0x54f30): same layout
    up to the status block (record +0x164), which is per disc (progression.status_block); PSU's is 4 bytes
    longer, so 0x0233 strides 0x2D0 on PSU and 0x2CC on AotI, with flag bytes after the block (PSU +0x2CC,
    AotI +0x2C8; zero here).
  Spaceport map 900 (PSU) / 901 (AotI): shown on the recipient's own copy (area.map_for).
  Same on both, relayed as is: 0x0111 (AotI also takes event 7), 0x0204, 0x0203, 0x0205 fields, channel-1
    casts 0x0102/0x0503/0x0514/0x050F/0x010F (same offsets; AotI's checks are wider: 0x0503/0x0514/0x0102
    accept +0x2C up to 0x18, 0x050F action up to 0x0E, and PSU's bound checks drop the extras), chat 0x0304
    both ways (C>S senders 0x125800 / 0x120670 and S>C handlers 0x1257b0 / 0x120620 use the same offsets).
"""
import asyncio
import struct

from psu import combat, drops, games, instance
from psu.area import map_for, user_bin
from psu.login import packet

ACC = bytes.fromhex("00011300")
PLAYER = bytes.fromhex("00001200")
CASTS = {0x0101, 0x0102, 0x0104, 0x0107, 0x010F, 0x050F, 0x0503, 0x0514}   # EGS cast whitelist


def u32(v):
    return struct.pack("<I", v)


class Player:
    def __init__(self, gid, raw, writer, send_raw):
        self.gid, self.raw, self.writer = gid, raw, writer
        self.send_raw = send_raw          # async (data, what): write under this player's lock
        self.uni = 1                      # universe (Uni-cube); zones are per universe
        self.zone = None                  # (universe, quest, zone) the player is registered in
        self.lid = 0
        self.area, self.entry = None, 0   # (quest, zone, map)
        self.pos = (0.0, 0.0, 0.0, 0.0)
        self.areanb = 0
        self.lock = asyncio.Lock()
        self.party = None                 # social.Party
        self.state = None                 # progression/inventory state (db characters.state)
        self.game = games.current()       # "psu" / "aoti": the disc this player runs (psu/games.py)

    def user(self, game):
        """This player's 712-byte user record for a recipient running `game`."""
        return user_bin(self.gid, self.lid, self.raw, self.area, self.entry, pos=self.pos, st=self.state,
                        game=game, owner=self.game)


class World:
    def __init__(self):
        self.zones = {}                   # (universe, quest, zone) -> {gid: Player}
        self.online = {}                  # gid -> Player, everyone logged in with a character

    def population(self):
        """{universe id: players online with a character}."""
        out = {}
        for o in self.online.values():
            out[o.uni] = out.get(o.uni, 0) + 1
        return out

    def others(self, p):
        return [o for o in self.zones.get(p.zone, {}).values() if o is not p]

    def enter(self, p, quest, zone):
        """Register p in (quest, zone) with the lowest free LID; returns that LID."""
        members = self.zones.setdefault((p.uni, quest, zone), {})
        used = {o.lid for o in members.values()}
        p.zone, p.lid = (p.uni, quest, zone), next(i for i in range(1024) if i not in used)
        members[p.gid] = p
        return p.lid

    def leave(self, p):
        """Unregister p; returns the players still in that zone."""
        members = self.zones.get(p.zone, {})
        members.pop(p.gid, None)
        if not members:
            self.zones.pop(p.zone, None)
            combat.clear_zone(p.zone)
            drops.clear(p.zone)
            instance.clear(p.zone)
        p.zone = None
        return list(members.values())

    async def post(self, dest, data, what, quiet=False):
        try:
            await dest.send_raw(data, what, quiet)
        except (ConnectionError, OSError):
            pass

    def relay(self, dest, data, what, quiet=False):
        """Fire and forget: a receiver busy with a fragmented download must not stall the sender.
        asyncio.Lock is FIFO, so packets to one receiver keep their order."""
        asyncio.get_running_loop().create_task(self.post(dest, data, what, quiet))


WORLD = World()


def watch_buffs(p):
    """Each time one of p's statuses runs out or ticks (psu/combat.py: Player.buffs / .ail; burn, poison, virus,
    Giresta, Megistar), the tick / expiry entries (0x0E07, attacker 0xFFFFFFFF as the offline room sends them)
    and p's SE2 and HP (0x0117; the client keeps SE2 until told, 0x14ecd0) go to everyone in p's zone, the HP
    also to p's party (0x1022). One watcher per player."""
    old = getattr(p, "buff_task", None)
    if old is not None and not old.done():
        old.cancel()

    async def run():
        while True:
            wait = combat.next_event(p)
            if wait is None or p.zone is None:
                return
            await asyncio.sleep(wait + 0.05)
            if p.zone is None:
                return
            entries = combat.player_ticks(p)
            hp = combat.player_hp(p)
            for d in [p] + WORLD.others(p):
                if entries:
                    WORLD.relay(d, combat.c0e07(d, entries), f"0x0e07 status ({len(entries)})", quiet=True)
                WORLD.relay(d, combat.c0117(d, hp, p), f"0x0117 status {combat.se2(p):#x}", quiet=bool(entries))
            if entries and p.party is not None:
                k = p.party.slot_of(p)
                for _, m in p.party.members():
                    WORLD.relay(m, combat.c1022(m, hp, k), "0x1022 hp", quiet=True)

    p.buff_task = asyncio.get_running_loop().create_task(run())


def watch_enemy(zone, e):
    """The same for an enemy with a status: its ticks (never lethal) and expiries go to the zone as 0x0E07."""
    old = getattr(e, "status_task", None)
    if old is not None and not old.done():
        old.cancel()

    async def run():
        while not e.dead:
            wait = combat.next_event(e)
            if wait is None or zone not in WORLD.zones:
                return
            await asyncio.sleep(wait + 0.05)
            entries = combat.enemy_ticks(e)
            if entries:
                for d in list(WORLD.zones.get(zone, {}).values()):
                    WORLD.relay(d, combat.c0e07(d, entries), f"0x0e07 enemy {e.id} status", quiet=True)

    e.status_task = asyncio.get_running_loop().create_task(run())


# --- packets about player p, addressed to player d -------------------------------------------------

def c0111_spawn(d, p, event_id=6, param=0):
    """0x0111 player event (PS2 events 0..6: 3 = incapacitated red screen, 4 = healing aura, 6 = spawn)."""
    return packet(0x0111, struct.pack("<H", d.lid) + bytes(6) + u32(p.gid) + bytes(8) + ACC + u32(d.gid) + bytes(8)
                  + u32(p.gid) + u32(p.lid) + u32(event_id) + u32(param))


def c010d_char(d, p):
    return packet(0x010D, struct.pack("<H", d.lid) + bytes(18) + ACC + u32(d.gid) + bytes(8) + u32(1) + bytes(4)
                  + bytes.fromhex("00000300") + bytes.fromhex("ffff0000") + bytes(4) + u32(p.gid) + bytes(24)
                  + u32(p.gid) + u32(p.lid) + b"\xff" * 4 + p.user(d.game)[80:])


def c0205_map(d, p):
    q, z, m = map_for(p.area, d.game, p.game)
    return packet(0x0205, struct.pack("<H", d.lid) + bytes(18) + ACC + u32(d.gid) + bytes(8) + b"\xff" * 4
                  + struct.pack("<IIII", z, m, p.entry, d.areanb) + struct.pack("<H", p.lid) + bytes(2))


def c0203(d, p):
    return packet(0x0203, struct.pack("<H", d.lid) + bytes(18) + ACC + u32(d.gid) + bytes(8) + u32(p.gid) + u32(p.lid))


def c0201_char(d, p):
    return packet(0x0201, struct.pack("<H", d.lid) + bytes(2) + PLAYER + u32(p.gid) + bytes(8) + ACC + u32(d.gid)
                  + bytes(8) + p.user(d.game) + bytes(4) + bytes(76))


def c0204_unspawn(d, p):
    return packet(0x0204, struct.pack("<H", d.lid) + bytes(2) + PLAYER + u32(p.gid) + bytes(8) + ACC + u32(d.gid)
                  + bytes(8) + u32(p.gid) + u32(p.lid) + u32(100))


def c0233_players(d, players):
    """Records 0x2D0 apart for PSU (handler 0x546b0: the 712-byte record, PSU's last 4 status bytes, 4 flag
    bytes), 0x2CC for AotI (0x54f30: no extra status bytes). 80 bytes follow the last 712-byte record on both,
    so a single record is sent as before."""
    pad = 8 if d.game == games.PSU else 4
    recs = b"".join(o.user(d.game) + bytes(pad) for o in players)
    return packet(0x0233, struct.pack("<H", d.lid) + bytes(2) + PLAYER + u32(d.gid) + bytes(8) + ACC + u32(d.gid)
                  + bytes(8) + u32(len(players)) + recs + bytes(80 - pad))


def spawn_packets(d, p):
    return [(c0111_spawn(d, p), "0x0111 spawn"), (c010d_char(d, p), "0x010d other"), (c0205_map(d, p), "0x0205 other"),
            (c0203(d, p), "0x0203"), (c0201_char(d, p), "0x0201 other")]


def cast_to(d, p, cmd, body):
    """Forward a channel-1 command as the sender wrote it, with its GID/LID stamped at +0x24/+0x28.
    EGS (PC) also rewrites +4..+0xC and +0x14..+0x1C as source/destination ids, but on PS2 those bytes
    carry data (0x0503 has movement floats there), so they are left alone."""
    out = body[:0x24] + u32(p.gid) + u32(p.lid) + body[0x2C:]
    return packet(cmd, out, channel=1)


def c0304_chat(d, p, body):
    """PS2 chat relay (handler 0x1257b0): the C>S body from +0x34 as sent (+0x34 bubble style, +0x35/+0x36
    cut-in face, +0x37 length in UTF-16 units incl. the NUL, +0x38 channel 0 public / 1 party, +0x3C text),
    with the sender id forced to 00001200 + GID at +0x24 and +0x2C cleared. No name field on PS2 (EGS/PC
    has one; inserting it made receivers show the sender's name as the message)."""
    return packet(0x0304, struct.pack("<H", d.lid) + bytes(2) + ACC + u32(p.gid) + bytes(8) + ACC + u32(d.gid) + bytes(8)
                  + PLAYER + u32(p.gid) + bytes(8) + body[0x34:])


AREA_NAMES = {1100000: "Colony", 1101000: "Parum", 1102000: "Neudaiz", 1103000: "Moatoob",
              1104000: "PPT Spaceport", 1120000: "My Room"}


def location_text(p):
    q = p.area[0] if p.area else None
    return AREA_NAMES.get(q, "On a mission" if q else "")
