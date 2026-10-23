"""AotI My Room zone NBL (XF_QE_OFF_LOBBY_ILLUMINUS zone01, served as nbl/aoti/zone-1120000-1.nbl) made
quiet online: no offline story-save reset, no first-visit tutorial offer (roompatch.py does the PSU / Extra
mode equivalent; this reuses its NBL rewrite).

EVENT.entr_unit0100 (room entry, pad off) calls lwInitIlluminusWorkFlag: "if chrwork S02_EVENT09 == 0" it
  clears S13..S22_FLAG_00..38/CLR, ON_TUTORIAL*, then sets SAVE_QEST_ID = 13 and S02_EVENT09 = 1: about 490
  chrflag/chrwork writes, each a 0x0D04 round trip, on every visit (online chrwork comes from the server's
  0x0D01 with S02_EVENT09 = 0), and SAVE_QEST_ID 13 overrides the server's 21 (lobby NPC talk keys on it).
  -> its "push 0" becomes "push -1": the reset never runs.
Then "if accountwork ON_TUTORIAL_ACC == 0": the first-visit Illuminus tutorial offer (windows 518/519; yes and
  solo -> syscall 270 to quest 200100, a tutorial mission the server does not serve; the account work is not
  kept online, so it came back every visit). -> "push 0" becomes "push -1": never offered.
usage: python aotiroompatch.py <aoti zone-1120000-1.nbl> <out nbl>"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import roompatch  # noqa: E402

PUSH, CALLX, EQ, JZ, PUSHS = 2, 0x4C, 0x12, 0x2D, 0x46
TARGETS = {"lwInitIlluminusWorkFlag": b"S02_EVENT09", "EVENT.entr_unit0100": b"ON_TUTORIAL_ACC"}


def patch(tsb):
    done = []
    for name, code, size in roompatch.routines(tsb):
        if name not in TARGETS:
            continue
        w = roompatch.words(tsb, code, size)
        for i in range(2, len(w) - 5):
            if w[i] == CALLX and w[i + 2] == PUSH and w[i + 3] == 0 and w[i + 4] == EQ and w[i + 5] == JZ:
                # the pushs just before must name the work variable this test reads
                j = max(0, i - 8)
                if TARGETS[name] in bytes(tsb[code + 4 * j:code + 4 * i]):
                    struct.pack_into("<i", tsb, code + 4 * (i + 3), -1)
                    done.append(f"{name}: {TARGETS[name].decode()} == 0 test push 0 -> -1 at script+{code + 4 * (i + 3):#x}")
                    break
    assert len(done) == 2, done
    return done


roompatch.patch = patch

if __name__ == "__main__":
    roompatch.main()
