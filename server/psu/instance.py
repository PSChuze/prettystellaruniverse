"""Mission instance state the server owns: event flags and object states (PS2 client; offsets are body offsets).

From static reading of PSU_US.erx's offline room (TGameRoom): on every zone load the client builds a fresh
flag manager (all clear) and condition manager ("not ready"); the room then sends a join snapshot. Enemy
gateways spawn nothing until the condition manager is ready, and only S>C 0x1202 makes it ready online.

  S>C 0x1202 conditions: +0x24 u32 first index, +0x28 u32 byte length (16-byte entries); (0, 0) = ready
  S>C 0x1204 flag set B: +0x24 u32 word offset 0, +0x28 u32 32, 256 bits
  S>C 0x1206 flag set A: +0x24 u32 word offset 0, +0x28 u32 640, 20 maps x 256 bits
  C>S 0x0404 / S>C 0x1205 flag A change: +0x24 u32 flag id (map index * 256 + event), +0x28 u32 mode
          (0 set, 1 clear, 2 toggle); 0x0403 / 0x1203 the same for flag set B
  C>S 0x0F0A object event: +0x24 s16 block, group, object, map id; +0x2C s16 object id; +0x34 type;
          +0x62 u8 event. Key console (type 22, keyset k = params +2): event 23 sets flag 201 + k,
          event 24 sets 243 + k; event 12 (any type) sets object state bit 0 -> S>C 0x1213
  S>C 0x1213 object state: +0x24 s16 object id, +0x26 0, +0x28 u32 state
Map index = the map's position in the zone's set file (missions.json "zones"). Set objects come from
missions.json "objects" (extract/missiondata.py). Enemy gateway (type 24) params: +0x5C s16 event set
when the wave is cleared, +0x5E required event, +0x60 trigger event.
"""
import struct

from psu import missions
from psu.login import packet

ACC = bytes.fromhex("00011300")
instances = {}                      # (quest, zone) -> Instance


def u32(v):
    return struct.pack("<I", v & 0xFFFFFFFF)


def hdr(d):
    return struct.pack("<H", d.lid) + bytes(18) + ACC + u32(d.gid) + bytes(8)


class Instance:
    def __init__(self, quest, zone):
        if isinstance(zone, tuple):                  # (zone, owner): one mission instance per party / player
            zone = zone[0]
        self.quest, self.zone = quest, zone
        self.flags_a = bytearray(640)
        self.flags_b = bytearray(32)
        self.objects = {}           # object id -> state word
        m = missions.mission(quest) or {}
        self.maps = m.get("zones", {}).get(str(zone), [])
        objs = m.get("objects", {}).get(str(zone), [])
        self.set = {(o[0], o[1], o[2]): (o[3], bytes.fromhex(o[4])) for o in objs}
        # breakable boxes (type 12) by target id (150 + running set index): [map index, params, hits so far]
        # [map index, params, hits so far, object id (running index within the map: 0x1213 / 0x0F0A ids)]
        # + [x, y, z] from the set record (+0x14) where the drop lands
        self.boxes = {o[5]: [o[0], bytes.fromhex(o[4]), 0, o[6] if len(o) > 6 else o[5], o[7] if len(o) > 7 else None]
                      for o in objs if o[3] == 12 and len(o) > 5 and o[5] >= 0}

    def map_index(self, map_id):
        return self.maps.index(map_id) if map_id in self.maps else 0


def get(zone):
    inst = instances.get(zone)
    if inst is None:
        inst = instances[zone] = Instance(*zone[-2:])     # (universe, quest, zone)
    return inst


def clear(zone):
    instances.pop(zone, None)


def set_flag(bits, fid, mode):
    i, b = fid >> 3, 1 << (fid & 7)
    if 0 <= i < len(bits):
        if mode == 0:
            bits[i] |= b
        elif mode == 1:
            bits[i] &= ~b & 0xFF
        elif mode == 2:
            bits[i] ^= b


def c1202(d):
    return packet(0x1202, hdr(d) + u32(0) + u32(0))


def c1204(d, inst):
    return packet(0x1204, hdr(d) + u32(0) + u32(len(inst.flags_b)) + bytes(inst.flags_b))


def c1206(d, inst):
    return packet(0x1206, hdr(d) + u32(0) + u32(len(inst.flags_a)) + bytes(inst.flags_a))


def c1205(d, fid, mode):
    return packet(0x1205, hdr(d) + u32(fid) + u32(mode))


def c1203(d, fid, mode):
    return packet(0x1203, hdr(d) + u32(fid) + u32(mode))


def c1213(d, oid, state):
    return packet(0x1213, hdr(d) + struct.pack("<hH", oid, 0) + u32(state))


def snapshot(d, inst):
    return [(c1202(d), "0x1202 conditions ready"), (c1204(d, inst), "0x1204 flags B"), (c1206(d, inst), "0x1206 flags A")]


def flag(inst, fid, mode, b=False):
    """Apply a flag change; returns the builder for the broadcast (d -> packet)."""
    set_flag(inst.flags_b if b else inst.flags_a, fid, mode)
    return (lambda d: c1203(d, fid, mode)) if b else (lambda d: c1205(d, fid, mode))


def object_event(inst, body):
    """C>S 0x0F0A in a mission. Returns (description, [(builder d -> packet, what)])."""
    _block, group, obj, map_id, oid = struct.unpack_from("<hhhHh", body, 0x24)
    otype, = struct.unpack_from("<H", body, 0x34)
    ev = body[0x62]
    mi = inst.map_index(map_id)
    kind, params = inst.set.get((mi, group, obj), (None, b""))
    out = []
    if otype == 22 and ev == 23 and kind == 22 and len(params) > 2:
        fid = mi * 256 + 201 + params[2]
        out.append((flag(inst, fid, 0), f"0x1205 key console init flag {fid}"))
    elif otype == 22 and ev == 24 and kind == 22:
        # open gate: the console's own params name the events it sets (+0x1C: the door's event, +0x1E: e.g. the
        # map's end event 31); the 243 + keyset table value alone leaves doors such as Evacuation's (event 52) shut
        evs = [struct.unpack_from("<h", params, o)[0] for o in (0x1C, 0x1E) if len(params) >= o + 2]
        evs = [e for e in evs if e >= 0] or ([243 + params[2]] if len(params) > 2 else [])
        for e in evs:
            fid = mi * 256 + e
            out.append((flag(inst, fid, 0), f"0x1205 key console open gate flag {fid}"))
    if otype == 31 and ev == 12 and kind == 31 and len(params) >= 8:   # a key picked up: its key event flag
        fid = mi * 256 + struct.unpack_from("<h", params, 6)[0]      # (params +6; the console checks it)
        out.append((flag(inst, fid, 0), f"0x1205 key flag {fid}"))
    if ev == 12 or (otype, ev) == (49, 3):         # 12: activate (any type); 49/3: crystal (EGS: 0x1213 state 1)
        st = inst.objects.get(oid, 0) | 1
        inst.objects[oid] = st
        out.append((lambda d, oid=oid, st=st: c1213(d, oid, st), f"0x1213 object {oid} state {st}"))
    return f"map {map_id} (#{mi}) group {group} object {obj} id {oid} type {otype} event {ev} set type {kind}", out
