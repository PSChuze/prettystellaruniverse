"""Reference model of the PS2 PSU client's character progression (PSU_US.erx 0x1f4990..0x1f4dd8).

Source table: GA_PLYCOMMON.NBL member PlyLevelParamData.snr (NSR). Pointers inside are
relative to the NBL data section: subtract the member's data offset (PSU 0xa1880, AotI 0x95680).
AotI (SLUS-21631) reads the same layout (GetTablePtr 0x1f5670, level row 0x1f56d0, EXP 0x1f58a0,
Corr::Calc 0x1f5970) with 30 type levels and levels populated to 200; its types 10..15 are AOTI_TYPES.
Usage: python plylevel.py <PlyLevelParamData.snr> [member base]   (prints EXP curve + a few stat samples)
"""
import struct
import sys

MEMBER_BASE = 0xA1880            # member data offset inside the NBL (pointer bias)
STATS = ("ATP", "DFP", "ATA", "EVP", "STA", "S5", "TP", "MST")   # u16 order in rows (s0..s7)
TYPES = ("Hunter", "Ranger", "Force", "Fighgunner", "Guntecher", "Wartecher",
         "Fortefighter", "Fortegunner", "Fortetecher", "Protranser")
AOTI_TYPES = ("Acrofighter", "Acrotecher", "Fighmaster", "Gunmaster", "Masterforce", "Acromaster")   # 10..15


class LevelParam:
    def __init__(self, data, base=MEMBER_BASE):
        self.d = data
        self.base = base
        root, = struct.unpack_from("<I", data, 8)          # NSR header: +4 ptrtab? +8 root
        (self.n_types, self.n_tlv, self.n_rg, self.n_lv, self.n_badge, self.n_misc,
         p_type, p_lv, p_req, p_badge, p_x1, p_x2, p_x3) = struct.unpack_from("<6H7I", data, root)
        self.type_tbl = [self.ptr(q) for q in struct.unpack_from(f"<{self.n_types}I", data, self.ptr(p_type))]
        self.lv_tbl = [self.ptr(q) for q in struct.unpack_from(f"<{self.n_rg}I", data, self.ptr(p_lv))]
        self.req = self.ptr(p_req)

    def ptr(self, p):
        return p - self.base

    def level_row(self, race, gender, level):
        """(exp_total, hp, [8 stats]) for level 1..200 (0x1f4a00)."""
        o = self.lv_tbl[race * 2 + gender] + (level - 1) * 24
        exp, hp = struct.unpack_from("<II", self.d, o)
        return exp, hp, list(struct.unpack_from("<8h", self.d, o + 8))

    def exp_for_level(self, race, gender, level):
        """0x1f4bd0: cumulative EXP needed to reach `level`; 100000000 past the table end."""
        if level <= 0 or level > 201:
            return 0xFFFFFFFF
        if level == 201:
            return 100000000
        exp = self.level_row(race, gender, level)[0]
        return 100000000 if (level >= 2 and exp == 0) else exp

    def type_row(self, cls, tlevel):
        """16 bytes: u16 type EXP total, u8 hp%, u8 0, u8 stat%[8], u8 race%[4]."""
        o = self.type_tbl[cls] + (tlevel - 1) * 16
        texp, hp_pct, _z = struct.unpack_from("<HBB", self.d, o)
        return texp, hp_pct, list(self.d[o + 4:o + 12]), list(self.d[o + 12:o + 16])

    def requirements(self, cls):
        """16 s8: minimum type level of each type needed to unlock `cls` (0x2c25e0)."""
        return list(struct.unpack_from("<16b", self.d, self.req + cls * 16))

    def stats(self, race, gender, level, cls, tlevel):
        """0x1f4ac0 with no extra-% array (every caller in the ERX passes NULL)."""
        exp, hp, st = self.level_row(race, gender, level)
        _texp, hp_pct, pct, race_pct = self.type_row(cls, tlevel)
        k = race_pct[race]
        hp = ((hp & 0xFFFF) * hp_pct * k) // 10000
        st = [((s & 0xFFFF) * p * k) // 10000 for s, p in zip(st, pct)]
        return exp, hp, st

    def level_from_exp(self, race, gender, level, exp, cap=200):
        """0x1e5a30: raise level while exp >= threshold of level+1; returns (level, next_total)."""
        while True:
            nxt = self.exp_for_level(race, gender, level + 1)
            if level >= min(cap, 200) or exp < nxt:
                return level, nxt
            level += 1


if __name__ == "__main__":
    lp = LevelParam(open(sys.argv[1], "rb").read(), int(sys.argv[2], 0) if len(sys.argv) > 2 else MEMBER_BASE)
    print("counts", lp.n_types, lp.n_tlv, lp.n_rg, lp.n_lv)
    same = all(lp.level_row(r, g, l)[0] == lp.level_row(0, 0, l)[0]
               for r in range(4) for g in range(2) for l in range(1, 201))
    print("EXP column identical for all race/gender tables:", same)
    last = max(l for l in range(1, 201) if lp.level_row(0, 0, l)[1])
    print("last populated level:", last)
    print("lv total_exp exp_to_next")
    for l in range(1, last + 1):
        e = lp.exp_for_level(0, 0, l)
        n = lp.exp_for_level(0, 0, l + 1)
        print(l, e, (n - e) if n != 100000000 else "-")
    for race in range(4):
        for cls in range(3):
            print("race", race, TYPES[cls], "lv1 tl1", lp.stats(race, 0, 1, cls, 1)[1:],
                  "| lv100 tl10", lp.stats(race, 0, 100, cls, 10)[1:])
    for c in range(16):
        print("type", c, (TYPES + AOTI_TYPES)[c], "req", lp.requirements(c),
              "texp", [lp.type_row(c, t)[0] for t in range(1, 11)])
