"""Diagnostic for a PCSX2 dev rig: hold the field-info area index / scene id at chosen values.

It tells whether a lobby that stays black is the client resolving area 0 / scene 0
(xf_sn_990000_00.nbl) instead of the Colony. While the field object (*0x6218B4 + 0x248000 on the
disc build) exists, keep [obj+0x24] = area index and the low half of [obj+0x28] = scene id, so the
client's `xf_sn_%s%04d` + `_00.nbl` land request names e.g. xf_sn_cg0001_00.nbl. Then the RAM scan
(ramnames.py) and the screen say whether that file exists and whether the world appears.

  python areapoke.py [--area 2] [--scene 1] [--slot 28014]
"""
import argparse
import time

import pine

FIELD_PTR = 0x6218B4
BASE = 0x248000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", type=int, default=2)      # 2 = "cg" (0x5240d0 table)
    ap.add_argument("--scene", type=int, default=1)
    ap.add_argument("--slot", type=int, default=28014)
    a = ap.parse_args()
    p = pine.Pine(slot=a.slot)
    last = None
    while True:
        try:
            obj = p.read(FIELD_PTR + BASE, 4)
            if obj:
                if p.read(obj + 0x24, 4) != a.area:
                    p.write(obj + 0x24, 4, a.area)
                if p.read(obj + 0x28, 2) != a.scene:
                    p.write(obj + 0x28, 2, a.scene)
            state = (obj, p.read(obj + 0x24, 4) if obj else None, p.read(obj + 0x28, 2) if obj else None)
            if state != last:
                print(f"{time.strftime('%H:%M:%S')} field obj={state[0]:#x} area={state[1]} scene={state[2]}", flush=True)
                last = state
        except pine.PineError as e:
            print(f"pine: {e}", flush=True)
            time.sleep(1)
        time.sleep(0.005)


if __name__ == "__main__":
    main()
