"""Build parts.json per disc: the appearance part words each client can draw (psu/xgame.py).

  python partsdata.py [--work DIR] [--game psu|aoti --out DIR]

Reads <work>/<game>/cvm/GA_PLYMAKECOMMON.NBL (econdata.py extracts it, or cvm.py x <iso> <work>/<game>/cvm
--match GA_PLYMAKECOMMON) and writes <work>/<game>/gamedata/parts.json for each disc that has it, or with
--game, DIR/parts.json for that disc.

partsinfo.snr: u32 at +8 = file offset of {u32 records (load address), u32 count}; the records are the
count x 16 bytes just before it: {u32 part word, u32 name (load address, "m_upper_00000.nbl"), u32 index into
XF_PLYDRAWRESPARTS.AFS, u32 flags}, sorted by word (the client's binary search, PSU 0x2c00a0).
selectable.snr: the character-creation lists, 8 race/gender groups x 6 slots of {u32 ptr, u32 count}
(as econdata.py lumilass_tables reads them); defaults[race*2+gender][slot] = the first choice
with a part number below 500 (a cast's normal ears/face/hair rather than a head type), else the first.
"""
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import itemdb  # noqa: E402
import workdir  # noqa: E402

u32 = lambda b, o: struct.unpack_from("<I", b, o)[0]


def members(game):
    d, m = itemdb.load_nbl(workdir.path(game, "cvm", "GA_PLYMAKECOMMON.NBL"))
    return {k: d[v["off"]:v["off"] + v["size"]] for k, v in m.items()}


def partsinfo(b):
    root = u32(b, 8)
    count = u32(b, root + 4)
    start = root - 16 * count
    words = [u32(b, start + 16 * i) for i in range(count)]
    assert words == sorted(words), "partsinfo not sorted"
    return words


def selectable(b):
    root = u32(b, 8)
    gptrs = []
    for g in range(root, len(b) - 3, 4):
        p = u32(b, g)
        if p == 0 or (gptrs and abs(p - gptrs[0]) >= len(b)):
            break
        gptrs.append(p)
    g0 = min(gptrs)
    base = next(u32(b, off) - 0x10 for off in range(0x10, root, 4)
                if 0 < u32(b, off + 4) < 0x100 and u32(b, off) - 0x10 + off == g0)
    out = []
    for go in (p - base for p in gptrs):
        out.append([[u32(b, u32(b, go + 8 * s) - base + 4 * i) for i in range(u32(b, go + 8 * s + 4))]
                    for s in range(6)])
    return out


def defaults(sel, words):
    allowed = set(words)
    out = []
    for slots in sel[:8]:
        row = []
        for lst in slots:
            ok = [w for w in lst if w in allowed]
            row.append(next((w for w in ok if (w >> 8) & 0xFFF < 500), ok[0] if ok else None))
        out.append(row)
    return out


def main():
    a = sys.argv[1:]
    games = [a[a.index("--game") + 1]] if "--game" in a else [
        g for g in workdir.GAMES if os.path.exists(workdir.path(g, "cvm", "GA_PLYMAKECOMMON.NBL"))]
    if not games:
        raise SystemExit(f"no GA_PLYMAKECOMMON.NBL under {workdir.work()} (see the docstring)")
    for game in games:
        out = a[a.index("--out") + 1] if "--out" in a and "--game" in a else workdir.path(game, "gamedata")
        m = members(game)
        words = partsinfo(m["partsinfo.snr"])
        data = {
            "notes": [f"{game}: GA_PLYMAKECOMMON.NBL partsinfo.snr part words (appearance +5, six LE u32, slot << 24); "
                      "defaults[race*2+gender][slot] from selectable.snr (extract/partsdata.py)"],
            "words": words,
            "defaults": defaults(selectable(m["selectable.snr"]), words),
        }
        path = os.path.join(out, "parts.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=0)
        print("wrote", path, len(words), "words")


if __name__ == "__main__":
    main()
