"""Print every change of the online-flow state machines in a running PSU (disc build) over PINE.

Objects (ERX va; the disc build loads the ERX at 0x248000):
  *0x6371d4  TDnasNet      state +0x80 (1 err, 3 loading, 4 ready, 5/7/9 op running, 6/8/10 op ok), err +0x84
  *0x6388c4  TPatchClient  state +0x48 (1 connected, 2 up to date, 3 downloading, 4-6 got file,
                                        7 error, 8 complete after download), err +0xac
Prints one line per change, so it can run under a monitor.

  python statewatch.py [--slot 28014] [--base 0x248000] [--hz 50]
"""
import argparse
import time

import pine

WATCH = [
    ("dnas", 0x6371D4, [("state", 0x80), ("err", 0x84)]),
    ("patch", 0x6388C4, [("state", 0x48), ("err", 0xAC)]),
    # field/scene info: 0x104a20(obj+0x20, [obj+0x24]) picks the area code; [obj+0x28] (s16) the scene id
    ("field", 0x6218B4, [("w20", 0x20), ("area_idx", 0x24), ("scene", 0x28), ("w2c", 0x2C)]),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=28014)
    ap.add_argument("--base", type=lambda s: int(s, 0), default=0x248000)
    ap.add_argument("--hz", type=float, default=50)
    a = ap.parse_args()
    p = pine.Pine(slot=a.slot)
    last = {}
    while True:
        try:
            for name, gva, fields in WATCH:
                obj = p.read(gva + a.base, 4)
                cur = {"obj": obj}
                if obj:
                    for f, off in fields:
                        cur[f] = p.read(obj + off, 4)
                if last.get(name) != cur:
                    desc = " ".join(f"{k}={v:#x}" if k == "obj" else f"{k}={v}" for k, v in cur.items())
                    print(f"{time.strftime('%H:%M:%S')} {name}: {desc}", flush=True)
                    last[name] = cur
        except Exception as e:  # emulator paused/closed: report once, keep trying
            if last.get("_err") != str(e):
                print(f"{time.strftime('%H:%M:%S')} pine: {e}", flush=True)
                last["_err"] = str(e)
            time.sleep(1)
        time.sleep(1 / a.hz)


if __name__ == "__main__":
    main()
