"""Put a disc's offline lobby zones in the server's NBL directory under the online quest / zone names it looks up.

  python lobbies.py psu|aoti <lobby dir> <out dir>
<lobby dir>: the disc's offline lobby AFS unpacked (nbl.py afs): PSU XF_QE_OFF_EXTRA_LOBBY01.AFS, AotI
XF_QE_OFF_LOBBY_ILLUMINUS.AFS (same zone numbering, English "ae" archives). <out dir>: PSU nbl/, AotI nbl/aoti/,
which the server reads first for an AotI session (psu/games.py). python -m extract runs this.
"""
import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def tool(*args):
    subprocess.run([sys.executable, os.path.join(HERE, args[0]), *map(str, args[1:])], check=True,
                   stdout=subprocess.DEVNULL)


def build(g, src, out):
    os.makedirs(out, exist_ok=True)
    s = lambda n: os.path.join(src, f"{n}_ae.nbl")          # noqa: E731
    o = lambda n: os.path.join(out, f"{n}.nbl")             # noqa: E731
    shutil.copyfile(s("zone10"), o("zone-1104000-0"))      # PPT Spaceport (map 900)
    # Colony: the native zone (maps 1-5, shops 100-103, 301, its own Job Counter) as zone 0, with the 4F
    # Spaceport gate's chapter checks off (online the chapter is -1); AotI: its story-flag gates
    tool("gatepatch.py", s("zone02"), o("zone-1100000-0"))
    for z in (0, 11, 12, 13):
        shutil.copyfile(s("zone03"), o(f"zone-1101000-{z}"))     # Parum, maps 1-4 100 200 400
        shutil.copyfile(s("zone04"), o(f"zone-1102000-{z}"))     # Neudaiz, maps 1 3 100 200
        shutil.copyfile(s("zone05"), o(f"zone-1103000-{z}"))     # Moatoob, maps 1 2 100-102 200
    # My Room: the offline lobby quest (13001; zone 1 = area 22, map 1) as quest 1120000, its quest.snr id
    # rewritten to match (a mismatch hides the player: area check in the fade)
    tool("questpatch.py", s("quest"), o("quest-1120000"), 1120000)
    shutil.copyfile(s("zone01"), o("zone-1120000-1"))
    # every native zone: no Extra-mode intro, no endless chapter-count loops (-1 online)
    zones = [o("zone-1100000-0"), *sorted(glob.glob(os.path.join(out, "zone-110[123]000-*.nbl"))),
             o("zone-1104000-0"), o("zone-1120000-1")]
    for f in zones:
        try:
            tool("roompatch.py", f, f + ".tmp")
            os.replace(f + ".tmp", f)
        except subprocess.CalledProcessError:
            if os.path.exists(f + ".tmp"):
                os.remove(f + ".tmp")
            print(f"roompatch: {os.path.basename(f)} left as is")
    if g == "aoti":
        # no Illuminus work-flag reset and no tutorial offer on every My Room entry
        f = o("zone-1120000-1")
        tool("aotiroompatch.py", f, f + ".tmp")
        os.replace(f + ".tmp", f)
    print(f"{g}: lobby zones -> {out}")


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("psu", "aoti"):
        raise SystemExit(__doc__)
    build(*sys.argv[1:])
