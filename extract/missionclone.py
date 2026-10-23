"""Clone a mission into a new quest id, with edits, as server-ready archives.

  python missionclone.py --quest <quest nbl> --zone <zone id>=<zone nbl> [--zone ...] --id <new quest id>
                         --out <dir> [--game aoti|psu] [--text <entry>=<string>]...
                         [--wave <zone>:<enemy file>:<group>:<wave>:<entry>:<field>=<v>[,<field>=<v>]]...
                         [--counter <id> --table <counter table nbl>] [--missions <missions.json>]
                         [--enemies <enemies.json>] [--check-server <server dir>]

  e.g. AotI FM 150 (Scarred Planet, layout 0, rank A = 315002) as 400100:
  python missionclone.py --game aoti --quest .../XF_QE_OFF02_FM_150_0_2/quest_ae.nbl
         --zone 0=.../XF_QE_OFF02_FM_150_0_2/zone00_ae.nbl --id 400100 --out <dir>/aoti
         --text "0=Scarred Planet X" --wave "0:enemy00_0100_r0.snr:1:0:0:count=4"
         --counter 4 --table .../counter-table-4.nbl --missions .../aoti/missions.json --check-server server

What it does, all through the writers (nblpack.py, relfmt.py, missionedit.py):
  quest NBL  quest.snr main block quest id -> the new id; --text edits text.bin entries (entry 0 = the name the
             counter shows); written plain (key 0) as <out>/quest-<id>.nbl
  zone NBLs  --wave edits a spawn entry of an enemy file (group = the enemy group id a gateway's params +0x63
             names, wave and entry indexes from 0; fields as relfmt's enemy_entry: type, sub, bits, count,
             level_ofs, ...) -> <out>/zone-<id>-<zone>.nbl
  serving    missiondata.mission(): the goal-gate warps (exitpatch) and the missions.json entry (objects, enemy
             gateways from enemydata, rules), merged into a copy of --missions (or a new one) in <out>
  counter    --counter / --table: the counter table NBL with the clone appended to its last group (table.rel
             quest list and counts, and the quest NBL as the next "NNNN.nbl" member) -> <out>/counter-table-<id>.nbl,
             missions.json counters follow
  --json     also writes the editable intermediates (nblpack unpack --decode) to <out>/json/
  --check-server  loads the result with the server's own modules (PSU_NBL_DIR=<out>/..: missions.data/mission,
             area.available, missions.c1015 / c0c06 (AotI counter pack), instance.Instance, combat.roster,
             judge.evaluate) and prints what they see.
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import missiondata  # noqa: E402
import missionedit  # noqa: E402
import nblpack  # noqa: E402
import relfmt  # noqa: E402
import workdir  # noqa: E402


def _num(v):
    try:
        return int(v, 0)
    except ValueError:
        return json.loads(v)


def clone_quest(src, qid, texts):
    arch = nblpack.Archive(open(src, "rb").read())
    q = missionedit.Doc(arch, "quest.snr")
    old = q.main()["quest_id"]
    missionedit.set_quest_id(q, qid)
    q.save()
    if texts:
        t = missionedit.Doc(arch, "text.bin")
        for i, s in texts:
            e = t.j["strings"][i]
            t.j["strings"][i] = s if isinstance(e, str) else dict(e, text=s)
        t.save()
    return old, arch.build()


def clone_zone(src, waves):
    arch = nblpack.Archive(open(src, "rb").read())
    for fname, group, wave, entry, fields in waves:
        e = missionedit.Doc(arch, fname)
        rec = missionedit.wave_entries(e, group, wave)[entry]
        for k, v in fields.items():
            if k not in rec and k not in dict(relfmt.STRUCTS["enemy_entry"]):
                raise SystemExit(f"enemy_entry has no field {k}")
            print(f"  {fname} group {group} wave {wave} entry {entry}: {k} {rec.get(k)} -> {v}")
            rec[k] = v
        e.save()
    return arch.build()


def add_to_counter(table_nbl, qid, quest_nbl):
    """Counter table NBL with qid appended to the last group; -> (NBL bytes, [quest ids])."""
    arch = nblpack.Archive(open(table_nbl, "rb").read())
    t = missionedit.Doc(arch, "table.rel")
    m = t.main()
    quests = missionedit.records(t, m["quests"])
    groups = missionedit.records(t, m["groups"])
    n = m["quest_count"]
    assert len(quests) == n and groups[-1]["first"] + groups[-1]["count"] == n, "table layout"
    quests.append({"v": qid})
    groups[-1]["count"] += 1
    m["quest_count"] = n + 1
    t.save()
    if f"{n - 1:04d}.nbl" in arch.names():
        arch.add(f"{n:04d}.nbl", "STD", quest_nbl, after=f"{n - 1:04d}.nbl")
    return arch.build(), [r["v"] for r in quests]


def check_server(server, nbl_root, game, qid, zones, counter):
    os.environ["PSU_NBL_DIR"] = nbl_root
    sys.path.insert(0, os.path.join(server, "server"))
    from psu import area, combat, games, instance, judge, missions
    games.set_current(games.AOTI if game == "aoti" else games.PSU)
    m = missions.mission(qid)
    assert m, "missions.json has no entry"
    print(f"server: missions.mission({qid}): {m['name']!r}, enter {m['enter']}, zones {missions.maps(qid)}")
    for z in zones:
        print(f"server: area.available({qid}, {z}) = {area.available(qid, z)}  ({area.nbl_path(qid, z)})")
        inst = instance.Instance(qid, z)
        roster = combat.roster((0, qid, z))
        print(f"server: instance zone {z}: {len(inst.set)} set objects, {len(inst.boxes)} boxes; "
              f"combat roster {len(roster)} enemies")
    pkt = missions.c1015(1, qid)
    print(f"server: 0x1015 mission start packet {len(pkt)} B (quest NBL {os.path.getsize(area.nbl_path(qid))} B)")
    q = m["quest"]
    print(f"server: judge start rule {q['start']} -> {judge.evaluate(q['start'], lambda *a: False)}, "
          f"clear rule {q['clear']} -> {judge.evaluate(q['clear'], lambda *a: False)} (no flags set)")
    if counter is not None:
        opts = missions.options(counter)
        pk = missions.c0c06(1, counter)
        print(f"server: counter {counter}: {len(missions.data()['counters'][str(counter)]['quests'])} quests, "
              f"options {len(opts)} (new quest {opts[-1]}), 0x0C06 {len(pk)} B")
    return m


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quest", required=True)
    ap.add_argument("--zone", action="append", default=[])
    ap.add_argument("--id", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--game", default="aoti", choices=("aoti", "psu"))
    ap.add_argument("--text", action="append", default=[])
    ap.add_argument("--wave", action="append", default=[])
    ap.add_argument("--counter", type=int)
    ap.add_argument("--table")
    ap.add_argument("--missions")
    ap.add_argument("--enemies")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check-server")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    texts = [(int(k), v) for k, _, v in (t.partition("=") for t in a.text)]
    zsrc = {int(k): v for k, _, v in (z.partition("=") for z in a.zone)}
    waves = {}
    for w in a.wave:
        z, fname, g, wv, en, fields = w.split(":", 5)
        f = {k: _num(v) for k, _, v in (x.partition("=") for x in fields.split(","))}
        waves.setdefault(int(z), []).append((fname, int(g), int(wv), int(en), f))

    old, qb = clone_quest(a.quest, a.id, texts)
    print(f"quest {old} -> {a.id}")
    tmp = os.path.join(a.out, f"_src-{a.id}")
    os.makedirs(tmp, exist_ok=True)
    qsrc = os.path.join(tmp, "quest.nbl")
    open(qsrc, "wb").write(qb)
    for z, p in zsrc.items():
        open(os.path.join(tmp, f"zone{z:02d}.nbl"), "wb").write(clone_zone(p, waves.get(z, [])))

    # missions.json entry + goal warps, exactly as missiondata builds the disc's missions
    if a.enemies:
        gd = json.load(open(a.enemies, encoding="utf-8"))
    else:
        gd = json.load(open(workdir.path(a.game, "gamedata", "enemies.json"), encoding="utf-8"))
    obj_ids = {int(k): v for k, v in gd["object_ids"].items()}
    qid, entry = missiondata.mission(qsrc, lambda z: os.path.join(tmp, f"zone{z:02d}.nbl"), a.out, gd, obj_ids,
                                     a.game == "aoti", f"clone of {old}")
    shutil.rmtree(tmp)
    assert qid == a.id
    index = json.load(open(a.missions, encoding="utf-8")) if a.missions else {"counters": {}, "missions": {}}
    index["missions"][str(qid)] = entry
    if a.counter is not None:
        tb, quests = add_to_counter(a.table, qid, open(os.path.join(a.out, f"quest-{qid}.nbl"), "rb").read())
        open(os.path.join(a.out, f"counter-table-{a.counter}.nbl"), "wb").write(tb)
        c = index["counters"].setdefault(str(a.counter), {"groups": 1, "quests": []})
        c["quests"] = quests
        print(f"counter {a.counter}: {len(quests)} quests, last {quests[-1]}")
    json.dump(index, open(os.path.join(a.out, "missions.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    for z, edits in waves.items():
        for fname, group, wave, _entry, _f in edits:
            for g in entry["gateways"].get(z, []):
                if g["enemy_group"] == group and f"_{g['map']:04d}_" in fname:
                    es = [(e["name"] or e["type"], e["level"]) for e in g["enemies"] if e["wave"] == wave]
                    print(f"  missions.json zone {z} map {g['map']} gateway {g['id']} group {group} wave {wave}: "
                          f"{len(es)} enemies {es[:6]}")
    print(f"wrote {a.out}: quest-{qid}.nbl, " + ", ".join(f"zone-{qid}-{z}.nbl" for z in entry["zones"])
          + ", missions.json")
    if a.json:
        for name in [f"quest-{qid}"] + [f"zone-{qid}-{z}" for z in entry["zones"]]:
            man, files = nblpack.unpack(open(os.path.join(a.out, name + ".nbl"), "rb").read())
            relfmt.decode_members(man, files)
            nblpack.write_dir(man, files, os.path.join(a.out, "json", name))
    if a.check_server:
        root = os.path.dirname(os.path.abspath(a.out)) if a.game == "aoti" else os.path.abspath(a.out)
        check_server(a.check_server, root, a.game, qid, [int(z) for z in entry["zones"]], a.counter)


if __name__ == "__main__":
    main()
