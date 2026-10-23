"""python -m extract --psu ISO [--aoti ISO] --out DATA [--work CACHE]: build the server's game data from your discs."""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build  # noqa: E402
import disc  # noqa: E402


def main():
    ap = argparse.ArgumentParser(prog="python -m extract", description=__doc__.split(": ", 1)[1])
    ap.add_argument("--psu", metavar="ISO", help="Phantasy Star Universe (USA), SLUS-21194")
    ap.add_argument("--aoti", metavar="ISO", help="Phantasy Star Universe: Ambition of the Illuminus (USA), SLUS-21631")
    ap.add_argument("--out", required=True, metavar="DATA", help="the data directory the server reads")
    ap.add_argument("--work", default=os.environ.get("PSU_EXTRACT_WORK") or "extract-cache", metavar="CACHE",
                    help="where the discs are unpacked (under 1 GB; reused by the next run)")
    ap.add_argument("--egs", metavar="DIR", help="a local EGS checkout at the pinned commit instead of downloading it")
    ap.add_argument("--rebuild", action="store_true", help="run every step again (after a tool changed)")
    ap.add_argument("--sha1", action="store_true", help="also hash the whole image and compare it with the known dumps")
    a = ap.parse_args()
    if not (a.psu or a.aoti):
        ap.error("give at least one disc: --psu ISO and/or --aoti ISO")
    discs = {}
    for want, path in (("psu", a.psu), ("aoti", a.aoti)):
        if not path:
            continue
        try:
            info = disc.identify(path, os.path.join(a.work, "discs"), verify=a.sha1)
        except (disc.DiscError, OSError) as e:
            sys.exit(f"extract: {e}")
        if info["game"] != want:
            sys.exit(f"extract: {path} is {info['title']} ({info['serial']}); pass it as --{info['game']}")
        print(f"{want}: {info['title']} ({info['serial']}{', DNAS-patched copy' if info['patched'] else ''})")
        discs[want] = (os.path.abspath(path), info)
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(a.work, exist_ok=True)
    build.Build(discs, a.out, a.work, a.egs, a.rebuild).run()
    print(f"done: {os.path.abspath(a.out)} (nbl/, gamedata/, captures/)")


main()
