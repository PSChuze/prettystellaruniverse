"""Mission data for the server from the disc.

  python missiondata.py [--game psu|aoti] [--iso "<disc>.iso"] [--out DIR] [--enemies <json>] [--lang ae]
      default DIR <work>/<game>/nbl; --enemies: this disc's enemies.json (default <work>/<game>/gamedata/)

Extracts from PSUROFS.CVM (cvm.py) and unpacks (nbl.py):
  FA_QE_TABLE000N.AFS    the six counters' mission tables (table000N_<lang>.nbl: table.rel + text.bin +
                         one nested quest NBL per mission)  -> counter-table-N.nbl (sent raw in S>C 0x0C06)
  XF_QE_OFF_FM_nnn_0.AFS the free missions (quest id 110000 + 10 * nnn)
                         -> quest-<id>.nbl (0x1015 / 0x020E) and zone-<id>-<zone>.nbl (0x020F), the names
                            the server's area.nbl_path() looks up
and writes missions.json:
  counters: {N: {groups, quests: [ids]}}             (table.rel main: +0x10 groups, +0x12 quests, ids at +0xC)
  missions: {id: {name, enter: [zone, map, entry], exit: [...], zones: {zone: [maps]}, afs,
             objects: {zone: [[map index, group, object, type, params hex, target id, map object id, [x, y, z]], ...]},
             levels: {zone: enemy level}, gateways: {zone: [gateway, ...]}}}
             (set objects the server drives: 5 switch, 12 box, 22 key console, 24 enemy gateway, 31 key)
  gateway = {map, map_index, group, object, id (actor target id), capacity, enemy_group (params +0x63),
             flags [params +0x5C, +0x5E, +0x60, +0x62], enemies: [{id, wave, type, level, variant, name,
             bits}]} from enemydata.zone_gateways (needs the disc's enemies.json: enemydata.py).
  quest = {start, clear, fail, abort (condition tokens or null), clear_warp, rank, rank2, rewards} from
             quest_rules(): what the server judges online (mission clear/fail, rank, type points, rewards).
  Gateway condition id = id - 150 (0x0402 +0x24 when the gateway reports; flags +0x30 == 7 = all waves
  done); its enemies' condition ids = condition id + 1 + i.
quest.snr main block: +8 quest id, +0xC zones ptr, +0x10 count (64 B: zone id, area id, +0xA u8 enemy level,
+0x1C boost record ptr), +0x1C enter warp
(u32 quest, s16 zone, map, entry), +0x28 exit warp. Text: text.bin entry 0 = name.

AotI (SLUS-21631): python missiondata.py --game aoti [--tables 0,1,2,3,4,5 | C=T,...] [--enemies <json>] [--out ...]
  -> $PSU_NBL_DIR/aoti/ (the server reads $PSU_NBL_DIR/aoti/<name> first for an AotI session).
  Archives (<work>/aoti/cvm/missions): FA_QE_TABLE0000..0005 (free missions) + 0110..0139 (Ep. 2 story
  counters: HQ and the bases, see Story below) and XF_QE_OFF02_FM_nnn_d_v.AFS, quest id 300000 + 100 * nnn + 10 * d + v (v = rank C/B/A/S, 0..3, the zone
  level rising per v; d picks the layout: another entry map). Counter mapping: table N is counter N, as in
  PSU. Both clients build the name from the same "%stable%04d.afs" (PSU 0x53a0c0, AotI 0x55a0d0), and
  AotI tables 0..5 carry PSU's counter texts in PSU's order (0 Clyez Linear Line, 1 Clyez Space Dock, 2 Holtes Flyer Base, 3 Holtes Linear
  Line, 4 Ohtoku Flyer Base, 5 Dagora Flyer Base). --tables picks the tables (C=T serves disc table T as
  counter C); the default is 0..5 plus the story tables 115, 121, 123, .., 139.
  Story (Episode 2 online: 10 chapters x 2 acts x difficulty C/B/A, 1-player). The counter id is the
  goal id of the set object (type 27, params +0 s16) the player walks into: the Clyez lobby zone
  (1100000 zone 0, map 5, the GUARDIANS HQ) has counter 115 (C>S 0x0811 / 0x0C05 counter 115,
  from 0/5/2), table 0115 = the first acts, 30 quests 2nn100 + v (nn = chapter 13..22). Each
  chapter's base is the lobby quest 2nn000 (STORYnn_L_0) with base counter 121, 123, .., 139 (tables
  01xx = the second acts 2nn110 + v; the B acts' warp tables key their arrival on {2nn000, zone, map,
  that counter id}) and a second goal (counter 0 or 120, 122, ..) that warps to map 0, whose
  EVENT.entr_unit0000 is cwBack_To_City (offline lobby 200000 warps). Table 0110 (quests 200110..200113)
  has no archives and no counter object: not built. STORYnn_A_v / _B_v are built like the free missions
  (missions.json "story": {chapter, act, base}); zones without a set file (cutscene zones, e.g. 13_A zone
  0: opening movie, then change_unit) get their maps from the script's EVENT.entr_unit<map> routines.
  The bases go to missions.json "bases": {2nn000: {enter, zones, counters [[zone, map, id, table|null]]}}.
  quest.snr version 0x16 (PSU 0x15; main block +0 u16): the main block, zone record, warp table and rule
  layouts are PSU's. Every pointer slot in _ptrtable.bin sits at the same main block offset in both
  (+0xC zones, +0x14 warps, +0x44, +0x48 start, +0x4C clear, +0x68 rewards, +0x78 rank, +0x88, +0x9C)
  and the zone record keeps +0x10 maps / +0x14 / +0x1C boost (AotI fills the boost record). Only the
  file's data order moved (PSU starts with a 16-byte flag name at +0x10, AotI with the rank sections).
  Exit warps go to quest 13001 or are all -1 (FM 001, 020, 150, 260, 271, 331); the in-table goal warps
  go to quest 200000 (the offline lobby). exitpatch only for goals that are no warp's arrival entry
  (back gates stay as on disc), to the exit warp, or when that is -1, to the quest's own outward goal
  warp: FM 001 d 1/2 (the disc keys its final gate by zone 1 instead of 0), FM 103 (zone 1 map 100
  goal 1), FM 110 (map 112 goal 1).
  Enemies: AotI's own enemies.json (enemydata.py --game aoti: 129 types, 136 variants, AotI's object table
  and Params), from --enemies or <work>/aoti/gamedata/enemies.json. Without one: PSU's enemies.json with
  AotI's EnemyLVTable and object table (aoti_enemies; variants >= 94 then have no name or stats).
"""
import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import workdir  # noqa: E402

GAMES = {
    "psu": {"fm": "XF_QE_OFF_FM_", "out": ("nbl",), "tables": None},
    "aoti": {"fm": "XF_QE_OFF02_FM_", "story": "XF_QE_OFF02_STORY", "out": ("nbl", "aoti"),
             "tables": {n: n for n in (*range(6), 115, *range(121, 140, 2))}},
}
workdir.work()
STORY_ACTS = {"A": 100, "B": 110}   # STORYnn_<act>_<v>: quest id nn * 1000 + 200000 + act base + v (v = difficulty C/B/A)
OBJ_TABLE_AOTI = 0x55d950    # AotI PSU_US.erx: set object type table (PSU 0x53d9e0, same 0x1C rows), count after it


def run(*args):
    subprocess.run([sys.executable, os.path.join(HERE, args[0]), *args[1:]], check=True, stdout=subprocess.DEVNULL)


def texts(b):
    """text.bin: u32 size, u32 8, u32 12, then u32 pointers to UTF-16 strings until the first string."""
    ptrs, o, first = [], 12, None
    while True:
        p, = struct.unpack_from("<I", b, o)
        first = p if first is None else first
        if o >= first:
            break
        ptrs.append(p)
        o += 4
    out = []
    for p in ptrs:
        e = p
        while b[e:e + 2] != b"\0\0":
            e += 2
        out.append(b[p:e].decode("utf-16-le"))
    return out


SERVER_OBJECTS = (5, 12, 22, 24, 31)


def set_objects(s, ids=None):
    """set_r0.rel: +8 -> area block (u16 area, u16 map count, u32 maps ptr); the 12-byte map table (u16 map
    id, u16 groups, u32 groups ptr) sits just before it. Group 40 B (+0x22 count, +0x24 objects ptr);
    object 52 B (+0xE type, +0x2C params size, +0x30 params ptr). Pointers are file offset + base."""
    ab, = struct.unpack_from("<I", s, 8)
    _area, nm, mp = struct.unpack_from("<HHI", s, ab)
    mt = ab - 12 * nm
    base = mp - mt
    ids = _object_ids() if ids is None else ids
    out, c = [], 0
    for mi in range(nm):
        _mid, ng, gp = struct.unpack_from("<HHI", s, mt + 12 * mi)
        oid = 0                                               # object id: running index within the map
        for gi in range(ng):
            g = gp - base + 40 * gi
            n, op = struct.unpack_from("<HI", s, g + 34)
            for oi in range(n):
                o = op - base + 52 * oi
                typ, = struct.unpack_from("<H", s, o + 14)
                psz, pp = struct.unpack_from("<II", s, o + 44)
                tid = 150 + c if ids.get(str(typ), 0) else -1     # 0x283c70: target id = 150 + running index
                c += ids.get(str(typ), 0)
                if typ in SERVER_OBJECTS:
                    pos = [round(v, 2) for v in struct.unpack_from("<3f", s, o + 20)]     # +0x14 x, y, z
                    out.append([mi, gi, oi, typ, s[pp - base:pp - base + psz].hex(), tid, oid, pos])
                oid += 1
    return out


def _object_ids():
    """Per set object type, how many target ids it reserves (enemies.json object_ids from enemydata.py)."""
    p = workdir.path("psu", "gamedata", "enemies.json")
    return json.load(open(p))["object_ids"] if os.path.exists(p) else {}


def quest_rules(q, mp):
    """quest.snr main block rules the server judges online (the client only judges offline: TQuestJudge).
    Conditions: +0x48 start, +0x4C clear, +0x50 fail, +0x54 abort (each -> {u32 tokens ptr, u32 count}; null =
    never), evaluated by TQuestContractConditionCalc 0x395f10: RPN, tokens 0..6 = AND OR EQ LT LE GT GE (left = second pop),
    100.. = commands with a fixed operand count (table 0x55ab50 +0xC): 100 n = push n, 120 zone map_id event
    = flag set (0x3997e0), 147 = result rank (reward selectors). +0x68 rewards {ptr, count} of 16 B
    (s8 id, +1 4 B item, +7 s8 qty, +8 flags (bit 2 = meseta), +0xC u32 meseta). +0x6C clear warp (used
    when its zone/map/entry are >= 0, else +0x1C). +0x78 rank table (+0x7C a second one): +0 sections ptr,
    +4 count (64 B: s8 type, s8 kind, s8 arg, +4 max, +8 min, +0xC par, +0x10 mult; score =
    clamp(max - mult * (par - value), min, max); type 0/1 elapsed s, 2 enemies killed, 3 deaths, 6 flags
    set of +0x14 n x (+0x20 s16 zone, +0x30 s16 map id, +0x18 u8 event), exported [zone, map, event]), +8 rewards {ptr, n} of
    {s32 reward id, cond ptr}, +0x10 4 x s32 thresholds (rank = first i with score <= thr[i], else 3),
    +0x20 4 x s32 type points per rank (0x3cbe70)."""
    u = lambda o: struct.unpack_from("<I", q, o)[0]

    def cond(p):
        if not p:
            return None
        tp, n = u(p), u(p + 4)
        return list(struct.unpack_from(f"<{n}i", q, tp))

    def rank(r):
        if not r:
            return None
        secs = []
        for k in range(u(r + 4)):
            s = u(r) + 64 * k
            t, kind, arg = struct.unpack_from("<bbb", q, s)
            mx, mn, par, mult = struct.unpack_from("<4i", q, s + 4)
            sec = {"type": t, "kind": kind, "arg": arg, "max": mx, "min": mn, "par": par, "mult": mult}
            if t == 6:
                n = q[s + 0x14]
                sec["flags"] = [[struct.unpack_from("<h", q, s + 0x20 + 2 * i)[0],
                                 struct.unpack_from("<h", q, s + 0x30 + 2 * i)[0], q[s + 0x18 + i]] for i in range(n)]
            secs.append(sec)
        rw = [[struct.unpack_from("<i", q, u(r + 8) + 8 * k)[0], cond(u(u(r + 8) + 8 * k + 4))]
              for k in range(u(r + 0xC))]
        return {"sections": secs, "thresholds": list(struct.unpack_from("<4i", q, r + 0x10)),
                "type_points": list(struct.unpack_from("<4i", q, r + 0x20)), "rewards": rw}

    rewards = {}
    if u(mp + 0x68):
        tp, n = u(u(mp + 0x68)), u(u(mp + 0x68) + 4)
        for k in range(n):
            e = tp + 16 * k
            rid, = struct.unpack_from("<b", q, e)
            rewards[rid] = {"item": q[e + 1:e + 5].hex(), "qty": q[e + 7], "flags": q[e + 8],
                            "meseta": u(e + 0xC)}
    return {"start": cond(u(mp + 0x48)), "clear": cond(u(mp + 0x4C)), "fail": cond(u(mp + 0x50)),
            "abort": cond(u(mp + 0x54)), "clear_warp": list(struct.unpack_from("<hhh", q, mp + 0x70)),
            "rank": rank(u(mp + 0x78)), "rank2": rank(u(mp + 0x7C)), "rewards": rewards}


def unpack(nbl_path):
    d = nbl_path[:-4] + "_nbl"
    if not os.path.exists(d):
        run("nbl.py", "x", nbl_path, d)
    return d


def aoti_object_ids(erx_path):
    """{set object type: target ids it reserves} from AotI's object table (same rows as enemydata.Erx.object_ids;
    found by PSU's first rows: AotI keeps PSU's types, drops 15, adds 65..71 (66/67/68/70/71 reserve 1 id))."""
    import erx
    im = erx.open_image(erx_path)
    u = lambda va: struct.unpack_from("<I", im.d, im.va2off(va))[0]
    n = next(k for k in range(40, 120) if u(OBJ_TABLE_AOTI + 0x1C * k) == k)      # the count follows the rows
    return {u(OBJ_TABLE_AOTI + 0x1C * i): u(OBJ_TABLE_AOTI + 0x1C * i + 0x14) & 0xFFFF for i in range(n)
            if u(OBJ_TABLE_AOTI + 0x1C * i) < 0x10000}


def aoti_enemies(gd, iso, erx_path):
    """PSU's enemies.json with AotI's own EnemyLVTable rows (variant per type and level) and object id counts."""
    import enemydata
    cvm = workdir.path("aoti", "cvm", "enemytables")
    lv = os.path.join(cvm, "SU_ENEMYLVTABLEPS2.NBL")
    if not os.path.exists(lv):
        os.makedirs(cvm, exist_ok=True)
        run("cvm.py", "x", workdir.need_iso(iso), cvm, "--match", "SU_ENEMYLVTABLEPS2", "--erx", erx_path)
    out = dict(gd)
    out["types"] = enemydata.lvtable(enemydata.nbl_members(lv)["EnemyLVTable.snr"][0])
    out["object_ids"] = {str(k): v for k, v in sorted(aoti_object_ids(erx_path).items())}
    return out


def warp_fix(q, zone_goals):
    """AotI: which goals exitpatch may give a warp, and to where. Goals that are some warp's arrival entry are the
    way back (left as on disc); the rest get the exit warp (+0x28), or when that is all -1, the quest's own warp
    out of the mission from the table (quest not -1 / itself). Returns (zone_goals, 12-byte target or None)."""
    mp, = struct.unpack_from("<I", q, 8)
    qid, = struct.unpack_from("<I", q, mp + 8)
    wp, wn = struct.unpack_from("<II", q, mp + 0x14)
    arrive, out = set(), []
    for i in range(wn):
        tq, tz, tm, te = struct.unpack_from("<ihhh", q, wp + 0x18 * i + 0xC)
        if tq == -1:
            arrive.add((tz, tm, te))
        elif tq != qid:
            out.append(q[wp + 0x18 * i + 0xC:wp + 0x18 * i + 0x18])
    target = q[mp + 0x28:mp + 0x34]
    if struct.unpack_from("<i", target)[0] == -1:
        target = out[0] if out else None
    return {z: [(m, g) for m, g in gl if (z, m, g) not in arrive] for z, gl in zone_goals.items()}, target


def story_of(afs):
    """XF_QE_OFF02_STORYnn_<A|B|L>_v.AFS -> (chapter nn, act, v); anything else -> (None, None, None)."""
    m = re.match(r"XF_QE_OFF02_STORY(\d+)_([ABL])_(\d+)\.AFS$", os.path.basename(afs))
    return (int(m[1]), m[2], int(m[3])) if m else (None, None, None)


def script_maps(znbl):
    """Map ids a zone script has entry events for (EVENT.entr_unit<map>), for zones without a set file."""
    import tsbdis
    names = [r[0] for r in tsbdis.routines(tsbdis.load(znbl))]
    return sorted({int(m[1]) for n in names for m in [re.match(r"EVENT\.entr_unit(\d+)$", n)] if m})


def base_lobby(afs, out, lang, tables):
    """A story chapter's base (STORYnn_L_0, quest 200000 + 1000 * nn): the lobby quest whose base counter (set
    object type 27, params +0 = counter id) opens disc table FA_QE_TABLE01xx with the chapter's second act.
    Copies quest-<id>.nbl and zone-<id>-<zone>.nbl as they are; returns (quest id, {enter [zone, map, entry],
    zones {zone: [maps]}, counters [[zone, map, counter id, table or None]], afs})."""
    import exitpatch
    d = afs[:-4]
    if not os.path.exists(d):
        run("nbl.py", "afs", afs, d)
    qnbl = os.path.join(d, f"quest_{lang}.nbl")
    q = open(os.path.join(unpack(qnbl), "quest.snr"), "rb").read()
    mp, = struct.unpack_from("<I", q, 8)
    qid, zp, nz = struct.unpack_from("<III", q, mp + 8)
    zones, counters = {}, []
    for zi in range(nz):
        zid, = struct.unpack_from("<H", q, zp + 64 * zi)
        znbl = os.path.join(d, f"zone{zid:02d}_{lang}.nbl")
        s = open(os.path.join(unpack(znbl), "set_r0.rel"), "rb").read()
        ab, = struct.unpack_from("<I", s, 8)
        _area, nmaps, _mptr = struct.unpack_from("<HHI", s, ab)
        zones[zid] = [struct.unpack_from("<H", s, ab - 12 * nmaps + 12 * i)[0] for i in range(nmaps)]
        counters += [[zid, m, c, c if c in tables else None] for m, c in exitpatch.goals(s)]
        shutil.copyfile(znbl, os.path.join(out, f"zone-{qid}-{zid}.nbl"))
    shutil.copyfile(qnbl, os.path.join(out, f"quest-{qid}.nbl"))
    return qid, {"enter": list(struct.unpack_from("<hhh", q, mp + 0x20)), "zones": zones, "counters": counters,
                 "afs": os.path.basename(afs)}


def nbl_members(path):
    """{member name: bytes} of an NBL (nblpack: pointers member-relative; the parsers here do not mind)."""
    import nblpack
    man, files = nblpack.unpack(open(path, "rb").read())
    return {m["name"]: files[m["file"]] for m in man["members"]}


def mission(qnbl, zone_nbl, out, gd, obj_ids, aoti, afs=""):
    """One mission: copy quest-<id>.nbl and zone-<id>-<zone>.nbl (zone_nbl(zone id) -> source path) to out, give
    the goal gates their warps (exitpatch), return (quest id, missions.json entry)."""
    import enemydata
    import exitpatch
    qm = nbl_members(qnbl)
    q = qm["quest.snr"]
    mp, = struct.unpack_from("<I", q, 8)
    if aoti:
        assert struct.unpack_from("<H", q, mp)[0] == 0x16, (afs, "quest.snr version")
    qid, zp, nz = struct.unpack_from("<III", q, mp + 8)
    enter = list(struct.unpack_from("<hhh", q, mp + 0x20))
    exit_ = list(struct.unpack_from("<hhh", q, mp + 0x2C))
    name = texts(qm["text.bin"])[0] if "text.bin" in qm else ""
    zones, objects, levels, gateways = {}, {}, {}, {}
    for zi in range(nz):
        zid, = struct.unpack_from("<H", q, zp + 64 * zi)
        level = q[zp + 64 * zi + 0xA]
        znbl = zone_nbl(zid)
        if not znbl or not os.path.exists(znbl):
            continue
        zm = nbl_members(znbl)
        if "set_r0.rel" not in zm:
            # AotI story cutscene zones (e.g. STORY13_A zone 0: the opening, then change_unit into the
            # mission) have only script.bin and text.bin: their maps are the script's entr_unit<map> events
            zones[zid] = script_maps(znbl)
            objects[zid], levels[zid], gateways[zid] = [], level, []
            shutil.copyfile(znbl, os.path.join(out, f"zone-{qid}-{zid}.nbl"))
            continue
        s = zm["set_r0.rel"]
        ab, = struct.unpack_from("<I", s, 8)
        _area, nmaps, _mptr = struct.unpack_from("<HHI", s, ab)
        zones[zid] = [struct.unpack_from("<H", s, ab - 12 * nmaps + 12 * i)[0] for i in range(nmaps)]
        objects[zid] = set_objects(s, gd["object_ids"])
        levels[zid] = level
        gateways[zid] = enemydata.zone_gateways(enemydata.nbl_members(znbl), zid, level, gd, obj_ids)
        shutil.copyfile(znbl, os.path.join(out, f"zone-{qid}-{zid}.nbl"))
    qout = os.path.join(out, f"quest-{qid}.nbl")
    if os.path.abspath(qnbl) != os.path.abspath(qout):
        shutil.copyfile(qnbl, qout)
    # online, the final goal gate needs a warp-table entry to leave (exitpatch.py)
    goals = exitpatch.zone_goals_from([os.path.join(out, f"zone-{qid}-{z}.nbl") for z in zones])
    if aoti:
        goals, target = warp_fix(q, goals)
        if target is not None:
            exitpatch.patch(qout, qout, goals, target)
    else:
        exitpatch.patch(qout, qout, goals)
    return qid, {"name": name, "enter": enter, "exit": exit_, "zones": zones, "objects": objects, "levels": levels,
                 "gateways": gateways, "quest": quest_rules(q, mp), "afs": afs}


def tables_arg(a, default):
    """--tables 0,1,2 or C=T,... -> {counter: disc table}; 'all' = every table on the disc (None)."""
    if "--tables" not in a:
        return default
    v = a[a.index("--tables") + 1]
    if v == "all":
        return None
    out = {}
    for x in v.split(","):
        c, _, t = x.partition("=")
        out[int(c)] = int(t or c)
    return out


def main():
    a = sys.argv[1:]
    game = a[a.index("--game") + 1] if "--game" in a else "psu"
    cfg = GAMES[game]
    aoti = game == "aoti"
    iso = workdir.iso_arg(a)
    out = a[a.index("--out") + 1] if "--out" in a else workdir.path(game, *cfg["out"])
    lang = a[a.index("--lang") + 1] if "--lang" in a else "ae"
    tables = tables_arg(a, cfg["tables"])
    cvm_out = workdir.path(game, "cvm", "missions")
    erx_path = workdir.erx(game)
    os.makedirs(cvm_out, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    sys.path.insert(0, HERE)
    given = a[a.index("--enemies") + 1] if "--enemies" in a else None
    ej = given if given and not aoti else workdir.path("psu", "gamedata", "enemies.json")
    aj = given or workdir.path("aoti", "gamedata", "enemies.json")
    if aoti and os.path.exists(aj):
        gd = json.load(open(aj, encoding="utf-8"))
        print(f"enemies: {aj}")
    else:
        if not os.path.exists(ej):
            run("enemydata.py", *([] if aoti or not iso else ["--iso", iso]))   # PSU's tables (AotI mode: from the PSU disc)
        gd = json.load(open(ej, encoding="utf-8"))
        if aoti:
            gd = aoti_enemies(gd, iso, erx_path)
    obj_ids = {int(k): v for k, v in gd["object_ids"].items()}
    if not glob.glob(os.path.join(cvm_out, "FA_QE_TABLE*.AFS")):
        run("cvm.py", "x", workdir.need_iso(iso), cvm_out, "--match", "FA_QE_TABLE", "--match", cfg["fm"],
            "--erx", erx_path)
    index = {"counters": {}, "missions": {}}

    disc = {int(os.path.basename(f)[11:15]): f for f in glob.glob(os.path.join(cvm_out, "FA_QE_TABLE*.AFS"))}
    for c, n in sorted((tables or {n: n for n in disc}).items()):
        afs = disc[n]
        d = afs[:-4]
        if not os.path.exists(d):
            run("nbl.py", "afs", afs, d)
        nbl = os.path.join(d, f"table{n:04d}_{lang}.nbl")
        rel = open(os.path.join(unpack(nbl), "table.rel"), "rb").read()
        main_, = struct.unpack_from("<I", rel, 8)
        _v, _t, gp, qp, ng, nq = struct.unpack_from("<IIIIHH", rel, main_)
        index["counters"][c] = {"groups": ng, "quests": list(struct.unpack_from(f"<{nq}I", rel, qp))}
        shutil.copyfile(nbl, os.path.join(out, f"counter-table-{c}.nbl"))

    archives = sorted(glob.glob(os.path.join(cvm_out, cfg["fm"] + "*.AFS")))
    if cfg.get("story"):
        if not glob.glob(os.path.join(cvm_out, cfg["story"] + "*.AFS")):
            run("cvm.py", "x", workdir.need_iso(iso), cvm_out, "--match", cfg["story"], "--erx", erx_path)
        story = sorted(glob.glob(os.path.join(cvm_out, cfg["story"] + "*.AFS")))
        archives += [f for f in story if story_of(f)[1] in STORY_ACTS]
    for afs in archives:
        d = afs[:-4]
        if not os.path.exists(d):
            run("nbl.py", "afs", afs, d)
        qnbl = os.path.join(d, f"quest_{lang}.nbl")
        if not os.path.exists(qnbl):
            continue
        qid, entry = mission(qnbl, lambda z: os.path.join(d, f"zone{z:02d}_{lang}.nbl"), out, gd, obj_ids, aoti,
                             os.path.basename(afs))
        index["missions"][qid] = entry
        chapter, act, _v = story_of(afs)
        if act in STORY_ACTS:
            index["missions"][qid]["story"] = {"chapter": chapter, "act": act, "base": 200000 + 1000 * chapter}
    if cfg.get("story"):
        index["bases"] = {}
        for afs in story:
            if story_of(afs)[1] == "L":
                # counter 0 and the even ids in a base are its way back to the city (goal -> map 0, whose
                # entr_unit0000 event runs cwBack_To_City), not the Linear Line's free missions
                qid, base = base_lobby(afs, out, lang, {c for c in index["counters"] if c >= 100})
                index["bases"][qid] = base
    json.dump(index, open(os.path.join(out, "missions.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(f"{len(index['counters'])} counters, {len(index['missions'])} missions"
          + (f", {len(index['bases'])} story bases" if "bases" in index else "") + f" -> {out}")


if __name__ == "__main__":
    main()
