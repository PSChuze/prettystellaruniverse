"""List file names the running game built at runtime: scan a savestate's EE RAM for name-like strings.

Takes a PCSX2 savestate (pine.py savestate N, or F1), reads eeMemory.bin (zstd, via p2s.py) and
prints every printable string ending in a data-file extension that does NOT also sit in the game
module's static strings. What remains was assembled at runtime (sprintf into a buffer), so it names
what the client just tried to open (for example which land / area file a lobby asked for).

  python ramnames.py <state.p2s> <PSU_US.erx> [--all]     (--all: include static names too)
"""
import re
import sys

import p2s

EXT = rb"\.(?:nbl|rel|snr|snj|snt|snm|dat|bin|xnr|adx|sfd|afs|cvm)"
PAT = re.compile(rb"[A-Za-z0-9_./:\\%-]{3,48}" + EXT + rb"(?=\0)")


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    ee = p2s.member(sys.argv[1], "eeMemory.bin")
    erx = open(sys.argv[2], "rb").read()
    static = set(m.group() for m in PAT.finditer(erx))
    seen = {}
    for m in PAT.finditer(ee):
        name = m.group()
        if b"%" in name or ("--all" not in sys.argv and name in static):
            continue
        seen.setdefault(name, []).append(m.start())
    for name, offs in sorted(seen.items(), key=lambda kv: kv[1][0]):
        print(f"{offs[0]:#010x} x{len(offs):<3d} {name.decode('latin1')}")
    print(f"-- {len(seen)} runtime names", file=sys.stderr)


if __name__ == "__main__":
    main()
