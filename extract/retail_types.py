"""Retail type tables for both discs and the one-time type EXP conversion (psu/progression.py).

  python retail_types.py build --psu PSU.json --aoti AOTI.json --out DIR
      PSU.json / AOTI.json: each disc's plylevel.json from gamedata.py --plylevel-only --type-overrides
      (overlay/psu_type_overrides.json, overlay/aoti_type_overrides.json). Writes DIR/aoti/plylevel.json (AotI's
      as is) and DIR/plylevel.json: the PSU disc's own levels, type names and rows for 10..15 (never read: the
      server takes 10..15 from AotI's table), with AotI's era for 0..9: its 30 type rows (stats, race %, type
      EXP), requirements and type_caps. The PSU client never reads this file; what it is sent is capped to its
      10 rows (visible_type_level).
  python retail_types.py migrate <psu.db> [--gamedata DIR] [--apply]
      What progression.migrate_db would do to that database with the given tables ($PSU_GAMEDATA), per
      character: type, level, EXP before / after. --apply runs it (backup + audit in the db's directory);
      only ever on a copy, never on the database a running server uses.
"""
import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def build(psu, aoti, dest):
    a = json.load(open(aoti, encoding="utf-8"))
    p = json.load(open(psu, encoding="utf-8"))
    out = dict(p)
    out["types"] = [a["types"][c] if c < 10 else p["types"][c] for c in range(16)]
    out["requirements"] = [a["requirements"][c] if c < 10 else p["requirements"][c] + [0] * (16 - len(p["requirements"][c]))
                           for c in range(16)]
    out["type_caps"] = [a["type_caps"][c] if c < 10 else len(p["types"][c]) for c in range(16)]
    for d, t in ((dest, out), (os.path.join(dest, "aoti"), a)):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "plylevel.json"), "w", encoding="utf-8") as f:
            json.dump(t, f)
        print(f"{os.path.join(d, 'plylevel.json')} caps {t['type_caps']}")


def migrate(db_path, gamedata, apply):
    if gamedata:
        os.environ["PSU_GAMEDATA"] = gamedata
    sys.path.insert(0, os.path.join(HERE, "..", "server"))
    from psu import db as dbm, progression
    if not progression.retail_curve():
        sys.exit("tables are not the retail ones (type_caps on both discs): nothing would be converted")
    if apply:
        d = dbm.DB(os.path.dirname(os.path.abspath(db_path)))
        n = progression.migrate_db(d, d.state_dir)
        print(f"converted {n}")
        return
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    for gid, slot, name, raw in c.execute("SELECT gid, slot, name, state FROM characters"):
        st = json.loads(raw or "{}")
        before = (list(st.get("type_level", [])), list(st.get("type_exp", [])))
        changed = progression.migrate_type_exp(st)
        if changed is None:
            print(f"{gid:#010x}/{slot} {name}: skipped ({'marked' if st.get('type_curve') else 'no state'})")
            continue
        tl = [f"{cc}:TL{before[0][cc]} {before[1][cc]}->{st['type_exp'][cc]}"
              for cc in range(16) if before[0][cc] > 1 or before[1][cc]]
        print(f"{gid:#010x}/{slot} {name}: cls {st.get('cls')} " + (", ".join(tl) or "all TL1, EXP 0 (unchanged)"))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--psu", required=True)
    b.add_argument("--aoti", required=True)
    b.add_argument("--out", required=True)
    m = sub.add_parser("migrate")
    m.add_argument("db")
    m.add_argument("--gamedata")
    m.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.cmd == "build":
        build(a.psu, a.aoti, a.out)
    else:
        migrate(a.db, a.gamedata, a.apply)


if __name__ == "__main__":
    main()
