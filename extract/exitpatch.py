"""Give every mission goal gate a warp: add quest.snr warp-table entries for goals that have none, pointing at
the quest's own exit warp (e.g. free mission 110000: map 1132 goal 1 -> 0x7ffffffe/2/4/6, back to the lobby).

Walking into a goal (set object type 27, TObjColliGoalWithEvent; params +0 s16 goal id) makes the client look
up {-1, zone, map, goal} in quest.snr's warp table (main block +0x14 ptr, +0x18 count; 0x18-byte entries:
key {u32 -1, u16 zone, u16 map, u16 goal, 0} -> target {u32 quest, u16 zone, u16 map, u16 entry, 0};
TQuestManager 0x16a380). Online it then sends C>S 0x0807 with the target (0x482dc0). The disc's missions have
no entry for their final gate (offline the zone script warps home instead), so online the player is stuck.
The exit warp is main block +0x28 (same 12-byte target layout; +0x1C is the enter warp).

The warp table grows in place (relfmt.py / missionedit.py: quest.snr re-laid out, every pointer, the NSR size and
the member's NSIF follow) and the members after quest.snr move (nblpack.py relocates their pointers and
pointer-table slots: these are data-section offsets, so moving a member without rewriting them, as the first
version of this tool did, left unit_title_table.rel pointing into the wrong member). Output: plain (key 0) NBL,
PRS data section, like questpatch.py.
usage: python exitpatch.py <quest nbl> <out nbl> <zone nbl>...   (the quest's zones, set_r0.rel inside)
Also used by missiondata.py (patch()). AotI's quest.snr (version 0x16) has the same main block, warp table and
goal objects; missiondata picks its goals and target (warp_fix: AotI exit warps can be all -1).
Idempotent: goals that already have an entry are left alone."""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import missionedit  # noqa: E402
import nbl  # noqa: E402
import nblpack  # noqa: E402


def goals(set_rel):
    """[(map id, goal id)] for the type-27 goals in a zone's set_r0.rel (layout as missiondata.set_objects)."""
    s = set_rel
    ab, = struct.unpack_from("<I", s, 8)
    _area, nm, mp = struct.unpack_from("<HHI", s, ab)
    mt = ab - 12 * nm
    base = mp - mt
    out = []
    for mi in range(nm):
        mid, ng, gp = struct.unpack_from("<HHI", s, mt + 12 * mi)
        for gi in range(ng):
            g = gp - base + 40 * gi
            n, op = struct.unpack_from("<HI", s, g + 34)
            for oi in range(n):
                o = op - base + 52 * oi
                typ, = struct.unpack_from("<H", s, o + 14)
                psz, pp = struct.unpack_from("<II", s, o + 44)
                if typ == 27 and psz >= 2:
                    out.append((mid, struct.unpack_from("<h", s, pp - base)[0]))
    return out


def patch(src, dst, zone_goals, target=None):
    """zone_goals: {zone id: [(map id, goal id)]}. target: the 12-byte warp target for the new entries (default
    the quest's exit warp, main block +0x28). Returns the number of entries added (0: dst not written)."""
    arch = nblpack.Archive(open(src, "rb").read())
    q = missionedit.Doc(arch, "quest.snr")
    m = q.main()
    have = {(w["from.zone"], w["from.map"], w["from.goal"]) for w in missionedit.warps(q)}
    if target is None:
        to = (m["exit.quest"], m["exit.zone"], m["exit.map"], m["exit.entry"])
    else:
        to = struct.unpack_from("<ihhh", bytes(target))
    add = []
    for z, gl in sorted(zone_goals.items()):
        for mid, goal in gl:
            if goal >= 0 and (z, mid, goal) not in have:
                have.add((z, mid, goal))
                add.append(missionedit.warp((-1, z, mid, goal), to))
    if not add:
        return 0
    missionedit.add_warps(q, add)
    q.save()
    with open(dst, "wb") as f:
        f.write(arch.build())
    return len(add)


def zone_goals_from(paths):
    """{zone id: goals} from zone NBL paths named zone-<quest>-<zone>.nbl."""
    out = {}
    for p in paths:
        zid = int(os.path.basename(p).rsplit("-", 1)[1].split(".")[0])
        d = open(p, "rb").read()
        nb = nbl.parse(d)
        data = nbl.nbl_data(d, nb)
        m = next((m for m in nb["members"] if m["name"] == "set_r0.rel"), None)
        if m is not None:
            out[zid] = goals(data[m["off"]:m["off"] + m["size"]])
    return out


def main():
    src, dst, zones = sys.argv[1], sys.argv[2], sys.argv[3:]
    n = patch(src, dst, zone_goals_from(zones))
    print(f"{src}: {n} goal warp(s) added" + (f" -> {dst}" if n else " (nothing to do)"))


if __name__ == "__main__":
    main()
