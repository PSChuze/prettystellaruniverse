"""PA EXP thresholds from GA_ITEMPARAM.NBL -> <itemdb.OUT>/pa.json (the server's combat.py reads it).

Usage: python patables.py [--game psu|aoti] [out.json]

Follows the client:
  PSU_US.erx  GetSkillParam 0xb5fa0 (striking, cat 04), GetBulletParam 0xb58e0 (cat 05), GetTechParam 0xb51d0 (cat 06).
  AotI erx    GetSkillParam 0xae780, GetBulletParam 0xae030 (+0xae410, 0xae3d0, 0xae390), GetTechParam 0xad620
              (+0xad940, 0xad8e0, 0xad8a0). Same 0x24/0x20 records and cost fields as PSU; roots differ (pa_records).
tier = min(L//10, tiers-1), k = L - 10*tier (AotI 0xae8a4, PSU 0xb631c; = L%10 below level 30);
cost(level L) = base[tier] + 10*step[tier]*k; threshold(L) = sum cost(0..L) (cumulative PA EXP at which level byte L
becomes L+1). Level byte CAP-1 -> threshold -1 = no more EXP.
CAP from itemCommonParam root +8 skill / +9 bullet / +0xA tech (AotI 0xad400 -> mgr+6/+8/+4, checked by the getters
above: lh 6 at 0xae79c, lh 8 at 0xae064, lh 4 at 0xad650): PSU 30/30/30, AotI 50/50/50.
"""
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import itemdb  # noqa: E402

u16 = lambda d, o: struct.unpack_from("<H", d, o)[0]
u32 = lambda d, o: struct.unpack_from("<I", d, o)[0]
s16 = lambda d, o: struct.unpack_from("<h", d, o)[0]


def root(d, m, name):
    off = m[name]["off"]
    return off + u32(d, off + 8)


def caps(d, m):
    """(skill, bullet, tech) level caps: itemCommonParam root +8/+9/+0xA (AotI 0xad400)."""
    r = root(d, m, "itemCommonParam.snr")
    return d[r + 8], d[r + 9], d[r + 0xA]


def pa_records(d, m, game):
    """Yield (cat, id hex, nt, [tier record addr], member, extra) per PA (extra: root for cat 04/05, stats entry for
    cat 06), plus ("bdef", wt, nt, [records[0]], member, root) per bullet weapon type (default bullet).

    Skill (cat 04) root: +0 u32 nt, +4 range table, then
      PSU  +8 u32 ptr[7] per PA index (0 = none) -> nt x 0x24 records (0xb605c..0xb6064).
      AotI +8 u32 records, +0xC s8 slot[7] = first record index (-1 = none) -> recs + slot*0x24 (0xae838..0xae854).
    Bullet (cat 05, types 0x0E..0x17: AotI 0xae0b0) root: +0 nt, +4 s16 count, +8 range table, +0xC records;
      index 0 = default bullet = records[0]; index i>0 -> records + (g*nt + tier + 1)*0x24, g = i-1 (PSU 0xb5d54),
      AotI: g = u8 map[i-1] when root+0x10 map ptr != 0 (0xae0f4..0xae154; no shipped table has one).
    Tech (cat 06) root: +0 nt, +4 u32 count,
      PSU  +8 stats (0x14 each), +0xC records (0x20 each), group = index (0xb52a4..0xb52c4).
      AotI +8 u8 map[count] (0 = identity; 0xff = no tech), +0xC stats, +0x10 records, group = map[index]
           for both stats and records (0xad6e4..0xad758, GetTechBaseParam 0xadb74..0xadbe8).
    """
    for name in sorted(m):
        if name.startswith("itemSkillParam_"):
            wt = int(name[15:17])
            r = root(d, m, name)
            nt = u32(d, r)
            for idx in range(7):
                if game == "psu":
                    p = u32(d, r + 8 + idx * 4)
                else:
                    slot = struct.unpack_from("<b", d, r + 0xC + idx)[0]
                    p = u32(d, r + 8) + slot * 0x24 if slot >= 0 else 0
                if p:
                    yield 4, "04%02x%02x00" % (wt, idx), nt, [p + t * 0x24 for t in range(nt)], name, r
        elif name.startswith("itemBulletParam_"):
            wt = int(name[16:18])
            if not 14 <= wt <= 23:     # AotI 27MagAt: no bullet root; GetBulletParam rejects type 0x1B (0xae0b0)
                continue
            r = root(d, m, name)
            nt, cnt, recs = u32(d, r), s16(d, r + 4), u32(d, r + 0xC)
            mp = u32(d, r + 0x10) if game != "psu" else 0
            yield "bdef", wt, nt, [recs], name, r
            for idx in range(1, cnt):
                g = d[mp + idx - 1] if mp else idx - 1
                yield 5, "05%02x%02x00" % (wt, idx), nt, [recs + (g * nt + t + 1) * 0x24 for t in range(nt)], name, r
        elif name.startswith("itemTechParam_0"):
            el = int(name[14:16])
            r = root(d, m, name)
            nt = u32(d, r)
            if game == "psu":
                sts, recs = u32(d, r + 8), u32(d, r + 0xC)
                for idx in range(10):
                    rec0 = recs + (idx * nt) * 0x20
                    if rec0 + nt * 0x20 > m[name]["off"] + m[name]["size"]:
                        break
                    yield 6, "06%02x%02x00" % (el, idx), nt, [rec0 + t * 0x20 for t in range(nt)], name, \
                        sts + idx * 0x14
            else:
                cnt, mp, sts, recs = u32(d, r + 4), u32(d, r + 8), u32(d, r + 0xC), u32(d, r + 0x10)
                for idx in range(cnt):
                    g = d[mp + idx] if mp else idx
                    if g == 0xFF:
                        continue
                    rec0 = recs + g * nt * 0x20
                    yield 6, "06%02x%02x00" % (el, idx), nt, [rec0 + t * 0x20 for t in range(nt)], name, \
                        sts + g * 0x14


def tier_k(L, nt):
    t = min(L // 10, nt - 1)
    return t, L - 10 * t


def thresholds(tiers, cap):
    """tiers: list of (base, step). Returns cumulative thresholds for level bytes 0..cap-2."""
    out, tot = [], 0
    for L in range(cap - 1):
        t, k = tier_k(L, len(tiers))
        b, s = tiers[t]
        tot += b + 10 * s * k
        out.append(tot)
    return out


def main(argv):
    game = itemdb.game_arg(argv)
    d, m = itemdb.load_nbl(itemdb.ITEMPARAM)
    try:
        T = itemdb.Texts()
    except Exception:
        T = None

    def name(iid):
        try:
            return T.name(bytes.fromhex(iid)) if T else None
        except Exception:
            return None

    cap = dict(zip((4, 5, 6), caps(d, m)))
    # cost fields: skill +0xC u32 base / +0x10 u16 step (AotI 0xae910), bullet +0x14/+0x18 (0xae3d0),
    # tech +0x10/+0x14 (0xad8e0)
    cf = {4: 0xC, 5: 0x14, 6: 0x10}
    res = {}
    for cat, iid, nt, recs, *_ in pa_records(d, m, game):
        if cat == "bdef":
            continue
        tiers = [(u32(d, p + cf[cat]), u16(d, p + cf[cat] + 4)) for p in recs]
        res[iid] = {"name": name(iid), "tiers": tiers, "thr": thresholds(tiers, cap[cat])}

    out = argv[1] if len(argv) > 1 else os.path.join(itemdb.OUT, "pa.json")
    json.dump(res, open(out, "w"), indent=0)
    for k, v in res.items():
        print(k, v["name"], v["tiers"], "thr L0..:", v["thr"][:3], "... L9", v["thr"][9], "L19", v["thr"][19],
              "L28", v["thr"][28], "last", v["thr"][-1])
    print(len(res), "PAs, caps", cap)


if __name__ == "__main__":
    main(sys.argv)
