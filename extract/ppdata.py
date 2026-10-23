"""Build ppregen.json per disc: each weapon's PP regeneration divisor (psu/combat.py pp_tick_amount).

  python ppdata.py [--work DIR] [--game psu|aoti] [--gamedata DIR] [--out DIR]

Reads <work>/<game>/cvm/GA_ITEMPARAM.NBL and <gamedata>/items.json (default <work>/<game>/gamedata) and writes
ppregen.json = {item id: divisor} next to it, or into --out. Without --game, every disc under the work
directory that has GA_ITEMPARAM.NBL. The room regenerates
base PP * 5 / divisor every 5 s (PSU 0x295f50, AotI 0x2989e0). itemWeaponParam_<type>.snr: root +8 4 record
pointers (one per manufacturer), +0x18 14 index bytes per manufacturer (0x0F and up = none); PSU records are 0x40
bytes with base PP at +0x18 and the divisor at +0x1A (the room's copy +0x28 / +0x2A, 0x2635c0 -> 0xb2a50), AotI's
0x26 bytes with them at +0x14 / +0x16 (its copy +0x24 / +0x26, 0x2683f0). Each disc's layout is the one whose base
PP matches items.json.
"""
import argparse
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import itemdb  # noqa: E402
import workdir  # noqa: E402

u16 = lambda b, o: struct.unpack_from("<H", b, o)[0]
u32 = lambda b, o: struct.unpack_from("<I", b, o)[0]
LAYOUTS = ((0x40, 0x18), (0x26, 0x14))          # (record size, base PP offset; divisor right after)


def weapons(game, items):
    d, m = itemdb.load_nbl(workdir.path(game, "cvm", "GA_ITEMPARAM.NBL"))
    best = None
    for size, off in LAYOUTS:
        out, ok = {}, 0
        for typ, f in enumerate(itemdb.WEAPON_FILES, 1):
            name = "itemWeaponParam_%s.snr" % f
            if name not in m:
                continue
            r = m[name]["off"] + u32(d, m[name]["off"] + 8)
            for man in range(4):
                recs = u32(d, r + 8 + 4 * man)
                if not recs:
                    continue
                for idx in range(14):
                    n = d[r + 0x18 + man * 14 + idx]
                    if n >= 0x0F:
                        continue
                    rec = recs + n * size
                    key = "01%02x%02x%02x" % (typ, idx, man)
                    if key in items and items[key].get("pp") == u16(d, rec + off):
                        ok += 1
                        if u16(d, rec + off + 2):
                            out[key] = u16(d, rec + off + 2)
        if best is None or ok > best[0]:
            best = (ok, out)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", choices=workdir.GAMES)
    ap.add_argument("--gamedata", help="the directory with this disc's items.json (default <work>/<game>/gamedata)")
    ap.add_argument("--out", help="write ppregen.json here instead of next to items.json")
    opt = ap.parse_args()
    for game in [opt.game] if opt.game else workdir.GAMES:
        gd = opt.gamedata if opt.game and opt.gamedata else workdir.path(game, "gamedata")
        if not os.path.exists(workdir.path(game, "cvm", "GA_ITEMPARAM.NBL")):
            print(f"{game}: no GA_ITEMPARAM.NBL, skipped")
            continue
        with open(os.path.join(gd, "items.json"), encoding="utf-8") as f:
            items = json.load(f)
        ok, out = weapons(game, items)
        dst = opt.out if opt.game and opt.out else gd
        os.makedirs(dst, exist_ok=True)
        with open(os.path.join(dst, "ppregen.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, indent=0, sort_keys=True)
        print(f"{game}: {len(out)} weapons ({ok} matched items.json base PP) -> {dst}/ppregen.json")


if __name__ == "__main__":
    main()
