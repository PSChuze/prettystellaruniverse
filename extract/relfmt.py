"""Editable JSON for the NBL members a mission is made of, and the writers back to bytes (exact round trip).

  python relfmt.py decode <member file> [--kind K] [--ptrs nbl.json]   -> JSON on stdout
  python relfmt.py encode <member.json> <out file>                      (prints the pointer slots)
  Kinds (by member name when --kind is not given):
    quest      quest.snr                quest main block, zones, warps, rules, rank / reward sections (0x15 / 0x16)
    set        set_r<N>.rel             area, maps, object groups, objects (+ known object params as fields)
    enemy      enemy<ZZ>_<MMMM>_r<N>.snr  enemy groups: header, wave info, waves of spawn entries
    table      table.rel                counter table: groups, quest ids, city warp
    unit_title unit_title_table.rel     {zone, map} -> unit_title_<NN>.bin index
    rel        any other NSR chunk      blocks + pointers only
    text       text.bin, text_result.bin, unit_title_<NN>.bin: UTF-16 string table
nblpack.py works with these: `nblpack.py unpack --decode` writes <member>.json next to each member it knows and
pack encodes a member from its .json when there is one (the JSON wins over the raw file).

REL / SNR members ("NSR\\0" chunk): +0 magic, +4 u32 chunk size - 8, +8 u32 main block (file offset, not
relocated), +0xC u32 0, then the data; the member is the chunk padded to 16 bytes. Every pointer the loader
relocates is listed in the NBL pointer table (nblpack: member-relative "ptrs"). The JSON is a list of blocks in
file order: the file is cut at every pointer target and at the edges of the structures a format walk finds, so a
block is either typed ("type", "records": one dict per record, fields from STRUCTS) or raw ("hex", "ptrs":
{offset in block: target}). A pointer is written as the target block's label ("L01d8", "L01d8+4") or null, so
blocks can grow, shrink, move or be added and every pointer (and the NBL pointer table) follows. Labels are only
names: new blocks can use any. Fields named "_x" are padding: left out of the JSON while 0. Records are checked
on decode (re-encoded and compared): anything a STRUCTS layout does not reproduce exactly stays a raw block.

quest.snr main block (0x104 B, both versions; AotI 0x16 only moved data around it): see STRUCTS["quest_main"].
  conditions {u32 tokens ptr, u32 count}: RPN tokens (missiondata.quest_rules, the server's judge.py).
  zone record 0x40: +0 zone, +2 area, +0xA enemy level, +0xB season, +0x10 -> {sets ptr, count} (u32 set ids),
    +0x14 maps ptr (s16 map ids) / +0x18 count, +0x1C boost record (0x1C B) or null.
  warp entry 0x18: {s32 -1, zone, map, goal} -> {quest, zone, map, entry} (exitpatch.py).
  rank table 0x30 (+0x78 / +0x7C): sections (0x40 B), rewards {s32 id, condition ptr}, thresholds, type points.
set_r<N>.rel: +8 -> area {u16 area, u16 maps, ptr map table}; map 12 B {id, groups, ptr}; group 0x28 B (+0x22
  count, +0x24 objects); object 0x34 B (+0xE type, +0x14 position, +0x20 rotation, +0x2C params size, +0x30
  params). Params stay hex with known fields laid over them (PARAM_FIELDS: 24 gateway enemy group / flags, 27 goal
  id); a field edit is written into the hex.
enemy file: +8 -> {groups ptr, count}; group 0x18 = 3 x {ptr, u16 count, u16}: header 0xC (s16 group id, ..),
  info 0x10 per wave, waves {entries ptr, s16 count}; entry 0x24 (enemydata.py: +0 type, +2 sub, +4 5 variant
  bits, +0x10 count, +0x18 level offset).
text: +0 u32 size, +4 u32 8 -> {u32 12} -> string pointers (up to the first string), UTF-16LE strings with a
  0 terminator; written back in order. Characters below 0x20 other than newline, the private-use range
  U+E000..U+F8FF (the game's control codes, e.g. {f812} page break, {f820} a name) and unpaired surrogates are
  written as {xxxx} (hex), "{" itself as {007b}. An entry is a string, {"text", "pad": n zero units after the
  terminator} or {"same": i} (shares entry i's string).
"""
import json
import os
import re
import struct
import sys

# ---------------------------------------------------------------- record layouts

WARP = lambda p: [(p + ".quest", "s32"), (p + ".zone", "s16"), (p + ".map", "s16"), (p + ".entry", "s16"),
                  ("_" + p + "_pad", "u16")]

STRUCTS = {
    "quest_main": [("version", "u16"), ("u16_02", "u16"), ("time", "u32"), ("quest_id", "u32"),
                   ("zones", "ptr"), ("zone_count", "u32"), ("warps", "ptr"), ("warp_count", "u32"),
                   *WARP("enter"), *WARP("exit"), ("u8_34", "u8"), ("u8_35", "u8"), ("u8_36", "u8"), ("u8_37", "u8"),
                   ("u32_38", "u32"), ("u32_3c", "u32"), ("u32_40", "u32"), ("cond_44", "ptr"),
                   ("start", "ptr"), ("clear", "ptr"), ("fail", "ptr"), ("abort", "ptr"), ("u32_58", "u32"),
                   ("u32_5c", "u32"), ("party_min", "u8"), ("party_max", "u8"), ("cursor_x", "s16"),
                   ("cursor_y", "s16"), ("u16_66", "u16"), ("rewards", "ptr"), *WARP("clear_warp"),
                   ("rank", "ptr"), ("rank2", "ptr"), ("path", "ptr"), ("path_count", "u32"), ("cond_88", "ptr"),
                   ("flags_tmp", "ptr"), ("flags_val", "ptr"), ("flags_bool", "ptr"),
                   ("flag_counts", "u8[4]"), ("npcs", "ptr"), ("u32_a0", "u32"), ("u32_a4", "u32"),
                   ("npc_count", "u32"), ("_tail", "u32[22]")],
    "zone": [("zone", "u16"), ("area", "u16"), ("area32", "u32"), ("u16_08", "u16"), ("level", "u8"),
             ("season", "u8"), ("u8_0c", "u8[4]"), ("sets", "ptr"), ("maps", "ptr"), ("map_count", "u32"),
             ("boost", "ptr"), ("_tail", "u32[8]")],
    "list": [("items", "ptr"), ("count", "u32")],
    "u32": [("v", "u32")],
    "s32": [("v", "s32")],
    "s16": [("v", "s16")],
    "token": [("v", "ref")],
    "flagname": [("name", "str16")],
    "boost": [("mask", "u32"), ("hp", "u16"), ("atp", "u16"), ("dfp", "u16"), ("ata", "u16"), ("evp", "u16"),
              ("sta", "u16"), ("s5", "u16"), ("tp", "u16"), ("mst", "u16"), ("u16_16", "u16"), ("exp", "u16"),
              ("u16_1a", "u16")],
    "warp": [("from.quest", "s32"), ("from.zone", "s16"), ("from.map", "s16"), ("from.goal", "s16"),
             ("_from_pad", "u16"), *WARP("to")],
    "cond": [("tokens", "ptr"), ("count", "u32")],
    "reward": [("id", "s8"), ("item", "hex4"), ("_pad", "u16"), ("qty", "u8"), ("flags", "u32"), ("meseta", "u32")],
    "rank": [("sections", "ptr"), ("section_count", "u32"), ("rewards", "ptr"), ("reward_count", "u32"),
             ("thresholds", "s32[4]"), ("type_points", "s32[4]")],
    "rank_section": [("type", "s8"), ("kind", "s8"), ("arg", "s8"), ("_pad", "u8"), ("max", "s32"), ("min", "s32"),
                     ("par", "s32"), ("mult", "s32"), ("flag_count", "u8"), ("u8_15", "u8[3]"),
                     ("flag_events", "u8[8]"), ("flag_zones", "s16[8]"), ("flag_maps", "s16[8]")],
    "rank_reward": [("id", "s32"), ("cond", "ptr")],
    "point": [("x", "s16"), ("y", "s16")],
    # set_r<N>.rel
    "set_area": [("area", "u16"), ("map_count", "u16"), ("maps", "ptr")],
    "set_map": [("map", "u16"), ("group_count", "u16"), ("groups", "ptr"), ("_pad", "u32")],
    "set_group": [("u32_00", "u32"), ("hex_04", "hex18"), ("u16_16", "u16"), ("u32_18", "u32"), ("index", "u32"),
                  ("u16_20", "u16"), ("object_count", "u16"), ("objects", "ptr")],
    "set_object": [("u32_00", "u32"), ("class", "u32"), ("u32_08", "u32"), ("u16_0c", "u16"), ("type", "u16"),
                   ("u32_10", "u32"), ("pos", "f32[3]"), ("rot", "f32[3]"), ("params_size", "u32"),
                   ("params", "ptr")],
    # enemy<ZZ>_<MMMM>_r<N>.snr
    "enemy_group": [("header", "ptr"), ("header_count", "u16"), ("u16_06", "u16"), ("info", "ptr"),
                    ("info_count", "u16"), ("u16_0e", "u16"), ("waves", "ptr"), ("wave_count", "u16"),
                    ("u16_16", "u16")],
    "enemy_header": [("group", "s16"), ("s16_02", "s16"), ("s16_04", "s16"), ("s16_06", "s16"), ("s8_08", "s8"),
                     ("u8_09", "u8[3]")],
    "enemy_info": [("id", "s16"), ("s16_02", "s16"), ("s16_04", "s16"), ("s16_06", "s16"), ("s16_08", "s16"),
                   ("s16_0a", "s16"), ("s8_0c", "s8"), ("_pad", "u8"), ("s16_0e", "s16")],
    "enemy_wave": [("entries", "ptr"), ("entry_count", "s16"), ("_pad", "u16")],
    "enemy_entry": [("type", "s16"), ("sub", "s16"), ("bits", "u8[5]"), ("_pad", "u8"), ("s16_0a", "s16"),
                    ("s16_0c", "s16"), ("s16_0e", "s16"), ("count", "s16"), ("s16_12", "s16"), ("s16_14", "s16"),
                    ("s16_16", "s16"), ("level_ofs", "s16"), ("s16_1a", "s16"), ("s16_1c", "s16"),
                    ("s16_1e", "s16"), ("s16_20", "s16"), ("s16_22", "s16")],
    # table.rel
    "table_main": [("u32_00", "u32"), ("time", "u32"), ("groups", "ptr"), ("quests", "ptr"), ("group_count", "u16"),
                   ("quest_count", "u16"), ("name", "u16"), ("desc", "u16"), ("cursor_x", "s16"),
                   ("cursor_y", "s16"), ("u32_1c", "u32"), ("u16_20", "u16"), ("u16_22", "u16"), ("city", "ptr")],
    "table_group": [("first", "u16"), ("count", "u16"), ("name", "u16"), ("desc", "u16"), ("u16_08", "u16"),
                    ("u16_0a", "u16"), ("_tail", "u32[5]")],
    "city": [("quest", "s32"), ("zone", "s16"), ("map", "s16"), ("entry", "s16"), ("_pad", "u16")],
    # unit_title_table.rel
    "unit_title": [("zone", "u16"), ("map", "u16"), ("index", "u32")],
}

# set object params: known fields (offset, type) per object type, laid over the hex. 23/24/25 (enemy gateways) from
# enemydata.py, 27 (goal) from exitpatch.py; 4, 18, 20, 26 follow EGS's set builder (PC layouts, same on PS2 where
# checked: model / NPC / door / entrance ids).
PARAM_FIELDS = {
    4: {"model": (0, "u32"), "scale": (4, "f32")},
    18: {"model": (0, "u16"), "npc": (2, "u16"), "talk_radius": (4, "f32"), "walk_radius": (8, "f32")},
    20: {"model": (0, "u32"), "closed": (4, "u8")},
    23: {"enemy_group": (0x63, "u8")},
    24: {"flag_a": (0x5C, "s16"), "flag_b": (0x5E, "s16"), "flag_c": (0x60, "s16"), "flag_d": (0x62, "u8"),
         "enemy_group": (0x63, "u8")},
    25: {"enemy_group": (0x63, "u8")},
    26: {"entry": (0, "u16")},
    27: {"goal": (0, "s16")},
}

PRIM = {"u8": "B", "s8": "b", "u16": "H", "s16": "h", "u32": "I", "s32": "i", "f32": "f", "ptr": "I", "ref": "i"}


def _ftype(t):
    """-> (struct char or None, count, byte size) for a field type: u8 s8 u16 s16 u32 s32 f32 ptr ref, X[n],
    hexN (bytes as hex), strN (ASCII, 0 padded). ptr: a relocated pointer (label) or null; ref: a word that is a
    pointer where the file relocates it (label) and a plain s32 elsewhere (RPN tokens: 151 'flag name')."""
    if t.startswith("hex") or t.startswith("str"):
        return None, int(t[3:]), int(t[3:])
    base, _, arr = t.partition("[")
    c = PRIM[base]
    n = int(arr[:-1]) if arr else 1
    return c, n, struct.calcsize("<" + c) * n


def ssize(name):
    return sum(_ftype(t)[2] for _f, t in STRUCTS[name])


def fmt_f32(v):
    b = struct.pack("<f", v)
    for p in range(6, 10):
        s = f"{v:.{p}g}"
        if struct.pack("<f", float(s)) == b:
            return float(s)
    return v


class Bad(Exception):
    pass


def decode_record(name, b, o, slots, label_of):
    """One record of STRUCTS[name] at b[o:] -> dict. Pointer fields must be relocation slots (or 0 / no slot)."""
    rec, p = {}, o
    for f, t in STRUCTS[name]:
        c, n, size = _ftype(t)
        if c is None and t.startswith("str"):
            raw = b[p:p + n]
            v = raw.split(b"\0", 1)[0]
            if v.ljust(n, b"\0") != raw or any(x < 0x20 or x > 0x7E for x in v):
                raise Bad(f"{name}.{f}: not a string")
            v = v.decode("ascii")
        elif c is None:
            v = b[p:p + n].hex()
        else:
            vals = list(struct.unpack_from(f"<{n}{c}", b, p))
            if t == "ref":
                v = label_of(struct.unpack_from("<I", b, p)[0]) if p in slots else vals[0]
            elif t == "ptr":
                v = vals[0]
                if p in slots:
                    v = label_of(v)
                elif v == 0:
                    v = None
                else:
                    raise Bad(f"{name}.{f}: pointer {v:#x} without a relocation")
            else:
                if c == "f":
                    if any(x != x or x in (float("inf"), float("-inf")) for x in vals):
                        raise Bad("f32 not finite")
                    vals = [fmt_f32(x) for x in vals]
                v = vals if n > 1 or "[" in t else vals[0]
        if f.startswith("_"):
            if (v if not isinstance(v, list) else any(v)):
                rec[f] = v
        else:
            rec[f] = v
        p += size
    for s in range(o, p):
        if s in slots and _field_at(name, s - o) is None:
            raise Bad(f"{name}: relocation at +{s - o:#x} is not a pointer field")
    return rec


def _field_at(name, off):
    p = 0
    for f, t in STRUCTS[name]:
        size = _ftype(t)[2]
        if p == off and t in ("ptr", "ref"):
            return f
        p += size
    return None


def encode_record(name, rec, resolve):
    """-> (bytes, [pointer field offsets that carry a relocation])."""
    out, slots = bytearray(), []
    for f, t in STRUCTS[name]:
        c, n, size = _ftype(t)
        v = rec.get(f, ("" if t.startswith("str") else "00" * n) if c is None else [0] * n if "[" in t
                    else None if t == "ptr" else 0)
        if c is None:
            bb = v.encode("ascii").ljust(n, b"\0") if t.startswith("str") else bytes.fromhex(v)
            if len(bb) != n:
                raise ValueError(f"{name}.{f}: {n} bytes expected")
            out += bb
        elif t == "ref" and isinstance(v, str):
            slots.append(len(out))
            out += struct.pack("<I", resolve(v) & 0xFFFFFFFF)
        elif t == "ptr":
            if v is None:
                out += bytes(4)
            else:
                slots.append(len(out))
                out += struct.pack("<I", resolve(v) & 0xFFFFFFFF)
        else:
            vals = v if isinstance(v, list) else [v]
            if len(vals) != n:
                raise ValueError(f"{name}.{f}: {n} values expected")
            out += struct.pack(f"<{n}{c}", *vals)
    return bytes(out), slots


# ---------------------------------------------------------------- NSR chunk <-> blocks

def label(off):
    return f"L{off:04x}"


class Walk:
    """Collects typed ranges {start: (struct, count)} and param annotations while following a format's pointers."""

    def __init__(self, b, slots):
        self.b, self.slots = b, set(slots)
        self.ranges = {}
        self.params = {}

    def u(self, o, f="<I"):
        return struct.unpack_from(f, self.b, o)[0]

    def ptr(self, o):
        """The target of the pointer at o, or None (no relocation there / out of the file)."""
        if o not in self.slots or o + 4 > len(self.b):
            return None
        v = self.u(o)
        return v if 0x10 <= v < len(self.b) else None

    def add(self, at, name, n=1):
        if at is None or n <= 0 or at + n * ssize(name) > len(self.b):
            return False
        if at in self.ranges:
            return self.ranges[at] == (name, n)
        self.ranges[at] = (name, n)
        return True

    def field(self, name, base, f):
        p = 0
        for fn, t in STRUCTS[name]:
            c, n, size = _ftype(t)
            if fn == f:
                return p
            p += size
        raise KeyError(f)


def walk_quest(w, main):
    S = lambda f: w.field("quest_main", main, f)
    if not w.add(main, "quest_main"):
        return
    u = lambda o, f="<I": w.u(o, f)
    z = w.ptr(main + S("zones"))
    nz = u(main + S("zone_count"))
    if z is not None and nz < 256 and w.add(z, "zone", nz):
        for i in range(nz):
            r = z + 0x40 * i
            sp = w.ptr(r + 0x10)
            if sp is not None and w.add(sp, "list"):
                w.add(w.ptr(sp), "u32", u(sp + 4))
            mp = w.ptr(r + 0x14)
            w.add(mp, "s16", u(r + 0x18))
            w.add(w.ptr(r + 0x1C), "boost")
    wp = w.ptr(main + S("warps"))
    w.add(wp, "warp", u(main + S("warp_count")))
    for f in ("cond_44", "start", "clear", "fail", "abort", "cond_88"):
        walk_cond(w, w.ptr(main + S(f)))
    rp = w.ptr(main + S("rewards"))
    if rp is not None and w.add(rp, "list"):
        w.add(w.ptr(rp), "reward", u(rp + 4))
    for f in ("rank", "rank2"):
        r = w.ptr(main + S(f))
        if r is not None and w.add(r, "rank"):
            w.add(w.ptr(r), "rank_section", u(r + 4))
            rr = w.ptr(r + 8)
            n = u(r + 0xC)
            if rr is not None and w.add(rr, "rank_reward", n):
                for k in range(n):
                    walk_cond(w, w.ptr(rr + 8 * k + 4))
    w.add(w.ptr(main + S("path")), "point", u(main + S("path_count")))


def walk_cond(w, c):
    if c is not None and w.add(c, "cond"):
        n = w.u(c + 4)
        t = w.ptr(c)
        if n < 4096 and t is not None and w.add(t, "token", n):
            for k in range(n):
                w.add(w.ptr(t + 4 * k), "flagname")


def walk_set(w, main):
    if not w.add(main, "set_area"):
        return
    nm = w.u(main + 2, "<H")
    mt = w.ptr(main + 4)
    if mt is None or not w.add(mt, "set_map", nm):
        return
    for mi in range(nm):
        ng = w.u(mt + 12 * mi + 2, "<H")
        gp = w.ptr(mt + 12 * mi + 4)
        if gp is None or not w.add(gp, "set_group", ng):
            continue
        for gi in range(ng):
            g = gp + 40 * gi
            n = w.u(g + 0x22, "<H")
            op = w.ptr(g + 0x24)
            if op is None or not w.add(op, "set_object", n):
                continue
            for oi in range(n):
                o = op + 52 * oi
                pp = w.ptr(o + 0x30)
                if pp is not None:
                    w.params[pp] = (w.u(o + 0xE, "<H"), w.u(o + 0x2C))


def walk_enemy(w, main):
    if not w.add(main, "list"):
        return
    gp, ng = w.ptr(main), w.u(main + 4)
    if gp is None or ng > 4096 or not w.add(gp, "enemy_group", ng):
        return
    for gi in range(ng):
        g = gp + 0x18 * gi
        w.add(w.ptr(g), "enemy_header", w.u(g + 4, "<H"))
        w.add(w.ptr(g + 8), "enemy_info", w.u(g + 0xC, "<H"))
        wp, wn = w.ptr(g + 0x10), w.u(g + 0x14, "<H")
        if wp is not None and w.add(wp, "enemy_wave", wn):
            for k in range(wn):
                w.add(w.ptr(wp + 8 * k), "enemy_entry", w.u(wp + 8 * k + 4, "<h"))


def walk_table(w, main):
    if not w.add(main, "table_main"):
        return
    w.add(w.ptr(main + 8), "table_group", w.u(main + 0x10, "<H"))
    w.add(w.ptr(main + 0xC), "u32", w.u(main + 0x12, "<H"))
    w.add(w.ptr(main + 0x24), "city")


def walk_unit_title(w, main):
    if w.add(main, "list"):
        w.add(w.ptr(main), "unit_title", w.u(main + 4))


WALKS = {"quest": walk_quest, "set": walk_set, "enemy": walk_enemy, "table": walk_table,
         "unit_title": walk_unit_title, "rel": None}


def kind_of(name):
    n = name.lower()
    if n == "quest.snr":
        return "quest"
    if re.match(r"set_r\d+\.rel$", n):
        return "set"
    if re.match(r"enemy\d+_\d+_r\d+\.snr$", n):
        return "enemy"
    if n == "table.rel":
        return "table"
    if n == "unit_title_table.rel":
        return "unit_title"
    if n in ("text.bin", "text_result.bin") or re.match(r"unit_title_\d+\.bin$", n):
        return "text"
    return None


def decode_rel(b, slots, kind="rel"):
    """NSR chunk bytes + member-relative relocation slots -> JSON-able dict (see the module doc)."""
    if len(b) < 0x10:
        raise Bad("short")
    magic = b[:4]
    size, main, h0c = struct.unpack_from("<3I", b, 4)
    end = size + 8
    bias = 0
    if end > len(b) and 0x10 <= size <= len(b):        # EGS-built chunks count the whole file
        end, bias = size, 8
    if not 0x10 <= end <= len(b):
        raise Bad("chunk size")
    slots = sorted(slots)
    if any(s < 0x10 or s + 4 > end for s in slots):
        raise Bad("relocation outside the chunk")
    body = b[:end]
    w = Walk(body, slots)
    walk = WALKS.get(kind)
    if walk and 0x10 <= main < end:
        walk(w, main)
    cuts = {0x10, end}
    targets = {}
    for s in slots:
        v, = struct.unpack_from("<I", body, s)
        targets[s] = v
        if 0x10 <= v < end:
            cuts.add(v)
    if 0x10 <= main < end:
        cuts.add(main)
    for at, (name, n) in w.ranges.items():
        cuts.add(at)
        cuts.add(min(end, at + n * ssize(name)))
    for at in w.params:
        cuts.add(at)
    cuts = sorted(c for c in cuts if 0x10 <= c <= end)
    # a typed range may only be used when no cut splits a pointer word
    slotset = set(slots)

    def label_of(v):
        if 0x10 <= v < end:
            return label(v)
        return v                                              # outside the chunk: kept as a number

    blocks = []
    ranges = sorted((at, at + n * ssize(name), name) for at, (name, n) in w.ranges.items())
    for s, e in zip(cuts, cuts[1:]):
        blk = None
        for rs, re_, name in ranges:
            sz = ssize(name)
            if rs <= s and e <= re_ and (s - rs) % sz == 0 and (e - s) % sz == 0:
                try:
                    recs = [decode_record(name, body, p, slotset, label_of) for p in range(s, e, sz)]
                    enc = encode_rel_records(name, recs, lambda x: _resolve_static(x, end))
                    if enc[0] != body[s:e] or [p + s for p in enc[1]] != [x for x in slots if s <= x < e]:
                        raise Bad("re-encode differs")
                    blk = {"label": label(s), "type": name, "records": recs}
                    break
                except Bad:
                    blk = None
        if blk is None:
            blk = {"label": label(s), "hex": body[s:e].hex()}
            ps = {f"{x - s:#x}": label_of(targets[x]) for x in slots if s <= x < e}
            if any(x + 4 > e for x in slots if s <= x < e):
                raise Bad("relocation crosses a block edge")
            if ps:
                blk["ptrs"] = ps
            if s in w.params:
                typ, psz = w.params[s]
                blk["object_type"] = typ
                pf = PARAM_FIELDS.get(typ)
                if pf:
                    fl = {}
                    for f, (o, t) in pf.items():
                        if o + _ftype(t)[2] <= e - s:
                            v = struct.unpack_from("<" + PRIM[t], body, s + o)[0]
                            if t == "f32":
                                if v != v or v in (float("inf"), float("-inf")):
                                    continue
                                v = fmt_f32(v)
                            fl[f] = v
                    blk["fields"] = fl
        blocks.append(blk)
    out = {"format": kind, "magic": magic.decode("latin-1"), "main": label_of(main) if main else None}
    if h0c:
        out["u32_0c"] = h0c
    if bias:
        out["size_bias"] = bias
    if len(b) != (end + 15) // 16 * 16 or any(b[end:]):
        out["trailer"] = b[end:].hex()
    out["blocks"] = blocks
    return out


def _resolve_static(x, end):
    """For the decode-time self check: a label resolves to its own offset."""
    if isinstance(x, str):
        lab, _, add = x.partition("+")
        return int(lab[1:], 16) + int(add or 0)
    return x


def encode_rel_records(name, recs, resolve):
    out, slots = bytearray(), []
    for r in recs:
        bb, ss = encode_record(name, r, resolve)
        slots += [len(out) + s for s in ss]
        out += bb
    return bytes(out), slots


def _block_bytes(blk, resolve):
    if "records" in blk:
        return encode_rel_records(blk["type"], blk["records"], resolve)
    b = bytearray(bytes.fromhex(blk["hex"]))
    for f, (o, t) in PARAM_FIELDS.get(blk.get("object_type"), {}).items():
        if f in blk.get("fields", {}):
            struct.pack_into("<" + PRIM[t], b, o, blk["fields"][f])
    slots = []
    for k, v in sorted(blk.get("ptrs", {}).items(), key=lambda kv: int(kv[0], 0)):
        o = int(k, 0)
        struct.pack_into("<I", b, o, resolve(v) & 0xFFFFFFFF)
        slots.append(o)
    return bytes(b), slots


def encode_rel(j):
    """JSON dict -> (bytes, member-relative relocation slots)."""
    sizes = []
    for blk in j["blocks"]:
        bb, _ = _block_bytes(blk, lambda v: 0)
        sizes.append(len(bb))
    offs, pos = {}, 0x10
    for blk, n in zip(j["blocks"], sizes):
        if blk.get("align"):
            pos = (pos + blk["align"] - 1) // blk["align"] * blk["align"]
        offs[blk["label"]] = pos
        pos += n
    end = pos

    def resolve(v):
        if v is None:
            return 0
        if isinstance(v, int):
            return v
        lab, _, add = v.partition("+")
        if lab not in offs:
            raise KeyError(f"unknown label {lab}")
        return offs[lab] + int(add or 0)

    out = bytearray()
    out += j["magic"].encode("latin-1") + struct.pack("<3I", end - 8 + j.get("size_bias", 0), resolve(j["main"]),
                                                                  j.get("u32_0c", 0))
    slots = []
    for blk in j["blocks"]:
        at = offs[blk["label"]]
        out += bytes(at - len(out))
        bb, ss = _block_bytes(blk, resolve)
        slots += [at + s for s in ss]
        out += bb
    if "trailer" in j:
        out += bytes.fromhex(j["trailer"])
    else:
        out += bytes(-len(out) % 16)
    return bytes(out), slots


# ---------------------------------------------------------------- text.bin

ESC = re.compile(r"\{([0-9a-fA-F]{4})\}")


def _u16_to_str(units):
    out, i = [], 0
    while i < len(units):
        c = units[i]
        if 0xD800 <= c < 0xDC00 and i + 1 < len(units) and 0xDC00 <= units[i + 1] < 0xE000:
            out.append(chr(0x10000 + ((c - 0xD800) << 10) + units[i + 1] - 0xDC00))
            i += 2
            continue
        if (c < 0x20 and c != 10) or 0xD800 <= c < 0xF900 or c == 0x7B:
            out.append(f"{{{c:04x}}}")
        else:
            out.append(chr(c))
        i += 1
    return "".join(out)


def _str_to_u16(s):
    out = bytearray()
    pos = 0
    for m in ESC.finditer(s):
        out += s[pos:m.start()].encode("utf-16-le")
        out += struct.pack("<H", int(m[1], 16))
        pos = m.end()
    out += s[pos:].encode("utf-16-le")
    return bytes(out)


def decode_text(b):
    """-> {"format": "text", "strings": [...]}: an entry is a string, {"text": s, "pad": n} (n zero UTF-16 units
    after the terminator, as some disc files have), {"same": i} (points at entry i's string) or
    {"unused": s, "pad": n} (a string no pointer refers to: kept in place, takes no table slot)."""
    if len(b) < 12:
        raise Bad("text header")
    size, root, table = struct.unpack_from("<3I", b, 0)
    if size != len(b) or root != 8 or table != 12:
        raise Bad("text header")
    ptrs, o, first = [], 12, None
    while o + 4 <= len(b) and (first is None or o < first):
        p, = struct.unpack_from("<I", b, o)
        if not 12 < p <= len(b) or p & 1:
            raise Bad("text pointer")
        first = p if first is None else min(first, p)
        ptrs.append(p)
        o += 4
    if first is None:
        first = 12
    if o != first:
        raise Bad("text table")

    def units_at(p):
        units, e = [], p
        while True:
            if e + 2 > len(b):
                raise Bad("text unterminated")
            c, = struct.unpack_from("<H", b, e)
            if c == 0:
                return units, e + 2
            units.append(c)
            e += 2

    def fill(entries, exp, p):
        """Bytes exp..p no entry points at: zero units pad the item before, anything else is an unused string."""
        if (p - exp) & 1:
            raise Bad("text gap")
        while exp < p:
            c, = struct.unpack_from("<H", b, exp)
            if c == 0:
                if not entries or _is_same(entries[-1]):
                    raise Bad("text gap")
                _pad(entries, 1)
                exp += 2
            else:
                units, e = units_at(exp)
                if e > p:
                    raise Bad("text overlap")
                entries.append({"unused": _u16_to_str(units)})
                exp = e

    entries, starts, exp = [], {}, first
    for p in ptrs:
        if p < exp:
            if p not in starts:
                raise Bad("text pointer into a string")
            entries.append({"same": starts[p]})
            continue
        fill(entries, exp, p)
        units, exp = units_at(p)
        starts[p] = sum(1 for x in entries if not _is_unused(x))
        entries.append(_u16_to_str(units))
    fill(entries, exp, len(b))
    j = {"format": "text", "strings": entries}
    if encode_text(j) != b:
        raise Bad("text re-encode differs")
    return j


def _is_same(e):
    return isinstance(e, dict) and "same" in e


def _is_unused(e):
    return isinstance(e, dict) and "unused" in e


def _pad(entries, n):
    e = entries[-1]
    entries[-1] = {"text": e, "pad": n} if isinstance(e, str) else dict(e, pad=e.get("pad", 0) + n)


def encode_text(j):
    """Strings in entry order, each UTF-16LE + 0 (+ "pad" zero units); {"same": i} reuses table entry i's string;
    {"unused": s} is written without a table slot."""
    ents = j["strings"]
    n = sum(1 for e in ents if not _is_unused(e))
    o = 12 + 4 * n
    ptrs, body = [], bytearray()
    for e in ents:
        if _is_same(e):
            ptrs.append(ptrs[e["same"]])
            continue
        txt, pad = (e, 0) if isinstance(e, str) else (e.get("text", e.get("unused")), e.get("pad", 0))
        if not _is_unused(e):
            ptrs.append(o + len(body))
        body += _str_to_u16(txt) + b"\0\0" + bytes(2 * pad)
    return struct.pack("<3I", o + len(body), 8, 12) + struct.pack(f"<{n}I", *ptrs) + bytes(body)


def text_strings(j):
    """Plain list of the entry strings (pads dropped, "same" resolved)."""
    out = []
    for e in j["strings"]:
        if _is_unused(e):
            continue
        out.append(out[e["same"]] if _is_same(e) else e if isinstance(e, str) else e["text"])
    return out


# ---------------------------------------------------------------- front door

def decode(name, b, slots=(), kind=None):
    """-> JSON-able dict for member `name` (kind from the name unless given). Raises Bad when it cannot."""
    kind = kind or kind_of(name) or "rel"
    if kind == "text":
        return decode_text(b)
    return decode_rel(b, slots, kind)


def encode(j):
    """-> (bytes, member-relative relocation slots)."""
    if j["format"] == "text":
        return encode_text(j), []
    return encode_rel(j)


def dumps(j):
    """JSON with short lists and records on one line (diff-friendly, still plain JSON)."""
    def enc(v, ind):
        pad = " " * ind
        if isinstance(v, dict):
            flat = json.dumps(v, ensure_ascii=False)
            if len(flat) <= 160 and not any(isinstance(x, (dict, list)) and len(json.dumps(x)) > 60
                                            for x in v.values()):
                return flat
            items = [f'{pad} {json.dumps(k)}: {enc(x, ind + 1)}' for k, x in v.items()]
            return "{\n" + ",\n".join(items) + "\n" + pad + "}"
        if isinstance(v, list):
            flat = json.dumps(v, ensure_ascii=False)
            if all(not isinstance(x, (dict, list)) for x in v):
                return flat
            items = [pad + " " + enc(x, ind + 1) for x in v]
            return "[\n" + ",\n".join(items) + "\n" + pad + "]"
        return json.dumps(v, ensure_ascii=False)
    return enc(j, 0) + "\n"


def decode_members(man, files):
    """nblpack hook: add <member>.json for every member relfmt can decode exactly; returns the count."""
    n = 0
    for m in man["members"]:
        if "ptrs_abs" in m or kind_of(m["name"]) is None:
            continue
        try:
            j = decode(m["name"], files[m["file"]], m.get("ptrs", []))
        except Bad:
            continue
        if encode(j) != (files[m["file"]], m.get("ptrs", [])):
            continue
        files[m["file"] + ".json"] = dumps(j).encode("utf-8")
        m["json"] = m["file"] + ".json"
        n += 1
    return n


def main():
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "decode":
        kind = a[a.index("--kind") + 1] if "--kind" in a else None
        b = open(a[1], "rb").read()
        slots = []
        if "--ptrs" in a:
            man = json.load(open(a[a.index("--ptrs") + 1], encoding="utf-8"))
            fn = os.path.basename(a[1])
            slots = next((m.get("ptrs", []) for m in man["members"] if m["file"] == fn), [])
        sys.stdout.write(dumps(decode(os.path.basename(a[1]), b, slots, kind)))
    elif len(a) >= 3 and a[0] == "encode":
        b, slots = encode(json.load(open(a[1], encoding="utf-8")))
        open(a[2], "wb").write(b)
        print(json.dumps(slots))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
