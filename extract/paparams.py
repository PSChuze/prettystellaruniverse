"""PA / bullet parameter records for the tail of S>C 0x0A0A, from GA_ITEMPARAM.NBL -> <itemdb.OUT>/paparam.json
(the server's items.c0a0a reads it).

Usage: python paparams.py [--game psu|aoti] [out.json]

Recipes follow the client:
  PSU_US.erx  GetSkillBaseParam 0xb63f0 (cat 04), bullet 0xb5c70 (cat 05), GetTechBaseParam 0xb57b0 -> 0xb55e0 (cat 06).
  AotI erx    GetSkillBaseParam 0xaeb90, GetBulletBaseParam 0xae4a0 (and 0xae660), GetTechBaseParam 0xadad0: same
              packing as PSU (skill 0xaec84..0xaed18, bullet 0xae59c..0xae644, tech 0xadbcc..0xadc74 incl.
              memcpy 0x285130 of rec+2 x 0xa); record lookup per patables.pa_records.
  Per-level ATP%/ATA%: s16 base + u8 inc * k, k = L - 10*tier, clamped to 0..0x7fff:
              skill +0x12/+0x16, +0x14/+0x17 (AotI 0xae9c8, 0xaea9c; PSU 0xb6384, 0xb6200),
              bullet +0x1A/+0x1E, +0x1C/+0x1F (AotI 0xae220, 0xae258). Levels 0..CAP-1 (CAP per patables.caps).

0x0A0A pkt+0x1AC0 (mgr+0x98): 36 x 0x30, one per learned PA in list order: +0 s16 ATP%, +2 s16 ATA%, +4 u32 (EXP-like,
  not read by any gate), +8 base param (0x18 skill/bullet, 0x28 tech). Without it a linked PA never starts
  (0xba890: step < base[0xa] fails on zeros).
0x0A0A pkt+0x2180 (mgr+0x9c): 10 x 0x1C default bullet for weapon types 0x0E..0x17: s16 ATP%, s16 ATA%, base param.
  Without it bullets have speed/radius 0 and never collide, so guns never report 0x0E00.
  AotI bullet getters take the same types 0x0E..0x17 (0xae0b0, 0xae4f4), so 10 defaults; 27MagAt (0x1B) has no
  bullet root and is not reachable through them.

Output: {"bullet_default": [10 hex], "pa": {id: {"base": [hex per tier], "pct": [[atp, ata] per level 0..CAP-1]}}}
"""
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import itemdb  # noqa: E402
import patables  # noqa: E402

u32 = lambda d, o: struct.unpack_from("<I", d, o)[0]
s16 = lambda d, o: struct.unpack_from("<h", d, o)[0]
s8 = lambda d, o: struct.unpack_from("<b", d, o)[0]


def main(argv):
    game = itemdb.game_arg(argv)
    d, m = itemdb.load_nbl(itemdb.ITEMPARAM)
    cap = dict(zip((4, 5, 6), patables.caps(d, m)))

    def rng(tab, i):
        return d[tab + 4 * i:tab + 4 * i + 4]

    def pct(recs, cap, o):
        """o = (atp, ata, atp inc, ata inc) field offsets."""
        out = []
        for L in range(cap):
            t, k = patables.tier_k(L, len(recs))
            s = recs[t]
            out.append([min(max(s16(d, s + o[0]) + d[s + o[2]] * k, 0), 0x7FFF),
                        min(max(s16(d, s + o[1]) + d[s + o[3]] * k, 0), 0x7FFF)])
        return out

    pa, bdef = {}, [None] * 10
    for cat, iid, nt, srcs, name, extra in patables.pa_records(d, m, game):
        if cat == 4:
            tab = u32(d, extra + 4)
            base = [(d[s:s + 0xC] + b"".join(rng(tab, s8(d, s + 0x18 + k)) for k in range(3))).hex() for s in srcs]
            pa[iid] = {"base": base, "pct": pct(srcs, cap[4], (0x12, 0x14, 0x16, 0x17))}
        elif cat in ("bdef", 5):
            tab = u32(d, extra + 8)
            packb = lambda rec: d[rec:rec + 0x14] + rng(tab, s8(d, rec + 0x20))
            if cat == "bdef":
                bdef[iid - 0x0E] = (struct.pack("<hh", 100, 100) + packb(srcs[0])).hex()
            else:
                pa[iid] = {"base": [packb(s).hex() for s in srcs], "pct": pct(srcs, cap[5], (0x1A, 0x1C, 0x1E, 0x1F))}
        elif cat == 6:
            st = d[extra:extra + 0x14]
            base = [(st + d[rc + 0x1C:rc + 0x20] + bytes([d[rc + 0xF], d[rc + 0x18], d[rc], d[rc + 1]])
                     + d[rc + 2:rc + 0xC] + d[rc + 0x16:rc + 0x18]).hex() for rc in srcs]
            pa[iid] = {"base": base, "pct": [[100, 100]] * cap[6]}

    out = argv[1] if len(argv) > 1 else os.path.join(itemdb.OUT, "paparam.json")
    json.dump({"bullet_default": bdef, "pa": pa}, open(out, "w"), indent=0)
    for i, b in enumerate(bdef):
        print("bullet type %#x" % (0x0E + i), b)
    for k in ("04090600", "05140100", "06010000"):
        if k in pa:
            print(k, pa[k]["base"][0], pa[k]["pct"][:2])
    print(len(pa), "PAs, caps", cap)


if __name__ == "__main__":
    main(sys.argv)
