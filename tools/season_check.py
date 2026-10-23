"""Local check for psu/season.py and its use in psu/area.py.

  python tools/season_check.py      ($PSU_NBL_DIR or $PSU_WORK/lobby-nbl/ps2 for the lobby archives;
                                     the staged checks need seasonbuild's output, else they are skipped)

1. No season (PSU_SEASON unset / none, or a season with nothing staged): every lobby / zone / map sequence is
   byte-identical to the no-season one, on both discs.
2. Calendar and names: fixed dates map to the expected seasons (Easter computed), aliases resolve.
3. Event drops: only with a season on, only that season's items, never when PSU_SEASON_DROP=0.
4. Staged Christmas (if built): PSU gets the staged quest + zone NBL, 0x020F +0x2D = 13 and 0x0205 +0x43 = 1 on a
   quest change only; AotI gets the staged AotI zone NBL with 0x020F +0x2D = 13 and its usual quest NBL.
"""
import datetime
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("PSU_WORK") or os.path.join(HERE, "..", "..", "work")   # data built from the discs (docs/data.md)
sys.path.insert(0, os.path.join(HERE, "..", "server"))
sys.path.insert(0, HERE)
os.environ.setdefault("PSU_GAMEDATA", os.path.join(WORK, "psu", "gamedata"))
os.environ.setdefault("PSU_NBL_DIR", os.path.join(WORK, "lobby-nbl", "ps2"))
for k in ("PSU_SEASON", "PSU_SEASON_DIR", "PSU_SEASON_DROP"):
    os.environ.pop(k, None)

import xgame_check as X  # noqa: E402
from psu import area, games, progression, season  # noqa: E402

GID = 0x10000001
LOBBIES = (1100000, 1101000, 1102000, 1103000, 1104000)


def sequences():
    out = {}
    for g in (games.PSU, games.AOTI):
        games.set_current(g)
        raw = X.raw_char("Tester", 0, 1, 0)
        st = progression.new_state(raw)
        out[(g, "lobby")] = area.lobby_sequence(GID, raw, lid=3, st=st)
        for q in LOBBIES:
            m = (901 if g == games.AOTI else 900) if q == 1104000 else 1
            if not area.available(q, 0):
                continue
            out[(g, "zone", q)] = area.area_sequence(GID, raw, (q, 0, m), 0, nb=3, lid=2, st=st)
            out[(g, "zone_noquest", q)] = area.area_sequence(GID, raw, (q, 0, m), 0, nb=3, lid=2, st=st,
                                                             quest_change=False)
            out[(g, "map", q)] = area.location_sequence(GID, raw, (q, 0, m), 1, nb=5, lid=2, st=st)
    return out


def flat(seq):
    return b"".join(d for d, _ in seq)


def find(seq, prefix):
    return next((d for d, what in seq if what.startswith(prefix)), None)


def main():
    ok = {}
    base = sequences()
    if not base:
        raise SystemExit(f"no lobby archives in {os.environ['PSU_NBL_DIR']}")
    for mode in ("none", "christmas", "auto"):
        os.environ["PSU_SEASON"] = mode
        os.environ["PSU_SEASON_DIR"] = tempfile.mkdtemp()           # nothing staged
        again = sequences()
        ok[f"unchanged with PSU_SEASON={mode}, nothing staged"] = all(flat(again[k]) == flat(v) for k, v in base.items())
    os.environ.pop("PSU_SEASON_DIR")
    os.environ.pop("PSU_SEASON")
    ok["unchanged with PSU_SEASON unset"] = all(flat(sequences()[k]) == flat(v) for k, v in base.items())

    d = datetime.date
    cal = {d(2026, 12, 24): "christmas", d(2026, 10, 31): "halloween", d(2027, 3, 28): "easter",
           d(2026, 4, 2): "easter", d(2026, 4, 15): "cherryblossoms", d(2026, 7, 4): None, d(2027, 1, 1): "newyear",
           d(2026, 12, 31): "newyear", d(2026, 2, 14): "valentine", d(2026, 10, 24): "party", d(2026, 6, 23): "sonic"}
    ok["calendar"] = all(season.by_date(k) == v for k, v in cal.items())
    ok["easter dates"] = season.easter(2026) == d(2026, 4, 5) and season.easter(2027) == d(2027, 3, 28)
    ok["names"] = (season.name_of("Winter") == "snow" and season.name_of("bogus") is None
                   and season.SEASONS.index("christmas") == 13 and len(season.SEASONS) == 16)
    os.environ["PSU_SEASON"] = "auto"
    ok["auto"] = season.current(d(2026, 12, 24)) == "christmas" and season.current(d(2026, 7, 4)) is None

    games.set_current(games.PSU)
    os.environ["PSU_SEASON"] = "christmas"
    ok["event drop"] = season.event_drop(rnd=lambda n: 0) == ("item", "0b010b00")
    os.environ["PSU_SEASON_DROP"] = "0"
    ok["event drop off"] = season.event_drop(rnd=lambda n: 0) is None
    os.environ.pop("PSU_SEASON_DROP")
    os.environ["PSU_SEASON"] = "none"
    ok["no event drop without a season"] = season.event_drop(rnd=lambda n: 0) is None

    staging = os.path.join(os.environ["PSU_NBL_DIR"], "season-staging")
    staged = os.path.exists(os.path.join(staging, "christmas", "zone-1100000-0.nbl"))
    if staged:
        os.environ["PSU_SEASON"] = "christmas"
        os.environ["PSU_SEASON_DIR"] = staging
        sea = sequences()
        for g in (games.PSU, games.AOTI):
            s = sea[(g, "zone", 1100000)]
            f = find(s, "0x020f")
            zp = os.path.join(staging, "christmas", "aoti" if g == games.AOTI else "", "zone-1100000-0.nbl")
            zb = open(zp, "rb").read()
            ok[f"{g} staged zone NBL"] = f[0x34:0x34 + len(zb)] == zb          # 0x020F: the NBL from +0x34
            ok[f"{g} 0x020F season 13"] = f[0x2D] == 13
            ok[f"{g} 0x0205 seasonal"] = find(s, "0x0205")[-1] == 1
            q = find(s, "0x020e")
            qs = open(os.path.join(staging, "christmas", "quest-1100000.nbl"), "rb").read()
            ok[f"{g} quest NBL"] = (qs in q) == (g == games.PSU)
            ok[f"{g} other lobbies unchanged"] = all(flat(sea[k]) == flat(v) for k, v in base.items()
                                                     if k[0] == g and len(k) > 2 and k[2] != 1100000)
        ok["psu no season without a quest change"] = flat(sea[(games.PSU, "zone_noquest", 1100000)]) == \
            flat(base[(games.PSU, "zone_noquest", 1100000)])

        # the city BGM: the staged script is the served one with only the play_bgm 40 operands set to 52
        sys.path.insert(0, os.path.join(HERE, "..", "extract"))
        import seasonbuild
        nbl, prs = seasonbuild.nbl, seasonbuild.prs
        for g, sub in ((games.PSU, ""), (games.AOTI, "aoti")):
            def script(p):
                _d, nb, data = seasonbuild.load(nbl, p)
                m = seasonbuild.member(nb, "script.bin")
                return prs.decompress(data[m["off"]:m["off"] + m["size"]], 0x1C)
            native = script(os.path.join(os.environ["PSU_NBL_DIR"], sub, "zone-1100000-0.nbl"))
            staged_tsb = script(os.path.join(staging, "christmas", sub, "zone-1100000-0.nbl"))
            sites = [at for at, _ in seasonbuild.bgm_sites(native)]
            diff = [i for i in range(0, len(native), 4) if native[i:i + 4] != staged_tsb[i:i + 4]]
            ok[f"{g} christmas BGM 52 at the {seasonbuild.BGM_SITES[sub]} play_bgm site(s), nothing else"] = (
                len(native) == len(staged_tsb) and len(sites) == seasonbuild.BGM_SITES[sub] and diff == sites
                and all(struct.unpack_from("<I", staged_tsb, at)[0] == 52 for at in sites))

        # a preview: one character gets the staged season with the server's season off, nobody else does
        os.environ["PSU_SEASON"] = "none"
        season.PREVIEW[GID] = "christmas"
        prev = sequences()
        ok["preview: that character gets the staged Colony"] = all(
            find(prev[(g, "zone", 1100000)], "0x020f")[0x2D] == 13 for g in (games.PSU, games.AOTI))
        ok["preview: other characters do not"] = season.for_zone(os.environ["PSU_NBL_DIR"], 1100000, 0,
                                                                 gid=GID + 1) is None
        season.PREVIEW[GID] = None
        os.environ["PSU_SEASON"] = "christmas"
        ok["preview none: no season for that character while the server runs one"] = all(
            flat(v) == flat(base[k]) for k, v in sequences().items())
        season.PREVIEW.clear()
        os.environ.pop("PSU_SEASON_DIR")
    os.environ.pop("PSU_SEASON", None)
    bad = [k for k, v in ok.items() if not v]
    for k in bad:
        print("FAIL", k)
    if bad:
        sys.exit(1)
    print(f"ok: {len(ok)} season checks" + ("" if staged else " (staged checks skipped: run extract/seasonbuild.py)"))


if __name__ == "__main__":
    main()
