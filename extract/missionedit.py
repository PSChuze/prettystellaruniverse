"""Mission edits on the relfmt.py JSON of NBL members (library for exitpatch.py, missionclone.py and new tools).

  arch = nblpack.Archive(open("quest.nbl", "rb").read())
  q = Doc(arch, "quest.snr")              # decoded JSON (q.j), edits, q.save() puts it back into the archive
  m = q.main()                            # the main record (quest_main / set_area / table_main / ...)
  set_quest_id(q, 400100); add_warps(q, [warp(...)]); q.save()
  t = Doc(arch, "text.bin"); t.j["strings"][0] = "New name"; t.save()
  open("out.nbl", "wb").write(arch.build())       # key 0, PRS, pointers relocated
Blocks are addressed by label; records(doc, ref) gives the record list a pointer field points at (the block must
be typed: decode keeps a structure raw only when it cannot reproduce it, which does not happen for the main
mission structures on either disc, see fmtcheck.py).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relfmt  # noqa: E402


class Doc:
    def __init__(self, arch, name):
        self.arch, self.name = arch, name
        b, slots = arch.get(name)
        self.j = relfmt.decode(name, b, slots)

    def block(self, ref):
        lab = ref.partition("+")[0]
        return next(b for b in self.j["blocks"] if b["label"] == lab)

    def main(self):
        b = self.block(self.j["main"])
        return b["records"][0]

    def save(self):
        self.arch.put(self.name, *relfmt.encode(self.j))

    def new_label(self, stem):
        have = {b["label"] for b in self.j["blocks"]}
        n = 0
        while f"{stem}{n}" in have:
            n += 1
        return f"{stem}{n}"


def records(doc, ref):
    """The typed record list a pointer field (label) points at."""
    b = doc.block(ref)
    if "records" not in b:
        raise ValueError(f"{doc.name}: block {b['label']} is not typed")
    return b["records"]


# ---------------------------------------------------------------- quest.snr

def warp(src, dst):
    """A warp-table record: src (quest, zone, map, goal) -> dst (quest, zone, map, entry)."""
    return {"from.quest": src[0], "from.zone": src[1], "from.map": src[2], "from.goal": src[3],
            "to.quest": dst[0], "to.zone": dst[1], "to.map": dst[2], "to.entry": dst[3]}


def warps(q):
    m = q.main()
    if not isinstance(m["warps"], str) or not m["warp_count"]:
        return []                      # null, or (count 0) a pointer at the chunk end
    return records(q, m["warps"])


def add_warps(q, recs):
    """Append warp-table records (the table grows in place; everything after it moves)."""
    m = q.main()
    if isinstance(m["warps"], str) and m["warp_count"]:
        records(q, m["warps"]).extend(recs)
    else:
        lab = q.new_label("warps")
        i = next(k for k, b in enumerate(q.j["blocks"]) if b["label"] == q.j["main"])
        q.j["blocks"].insert(i, {"label": lab, "type": "warp", "records": list(recs)})
        m["warps"] = lab
    m["warp_count"] += len(recs)


def set_quest_id(q, qid):
    q.main()["quest_id"] = qid


def zones(q):
    m = q.main()
    return records(q, m["zones"]) if m["zones"] else []


# ---------------------------------------------------------------- enemy files

def enemy_group(e, group_id):
    """The enemy_group record whose header names group_id (set object params +0x63 of a gateway)."""
    for g in records(e, e.main()["items"]):
        if g["header"] and records(e, g["header"])[0]["group"] == group_id:
            return g
    raise KeyError(group_id)


def wave_entries(e, group_id, wave):
    return records(e, records(e, enemy_group(e, group_id)["waves"])[wave]["entries"])


# ---------------------------------------------------------------- set files

def set_objects(s):
    """[(map id, group index, object index, object record, params block or None)] in file order."""
    out = []
    area = s.main()
    for mp in records(s, area["maps"]):
        if not mp["groups"]:
            continue
        for gi, g in enumerate(records(s, mp["groups"])[:mp["group_count"]]):
            if not g["objects"]:
                continue
            for oi, o in enumerate(records(s, g["objects"])[:g["object_count"]]):
                out.append((mp["map"], gi, oi, o, s.block(o["params"]) if o["params"] else None))
    return out
