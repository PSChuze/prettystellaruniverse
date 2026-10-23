"""Native lobby zone NBL (offline Extra lobby) made safe online: no Extra-mode intro, no endless chapter loops.

Online, cwGet_Ex_Chapter_Number is -1 (seq.is_online). Two loops count a chapter number down and only
stop at == 0, so starting at -1 they never end, and the entry event never finishes: no character, no control.
  EVENT.entr_zone        (EXTRA_MODE_FLAG == 1: nChapter_Count = cwGet_Ex_Chapter_Number, loop)
  cw_Fm_Set_Appare_Flag  (called from EVENT.entr_zone, same loop)
  -> the loop test "get n; push 0; eq; jz" becomes "lteq" (stops at <= 0; offline counts are >= 1).
My Room (zone01) EVENT.entr_unit0001: "if EXTRA_MODE_FLAG == 0" runs the "Welcome to Extra Mode"
intro (cwSave_Quest_Id, pad_off, cinema, mes.custom_win text 249), sets EXTRA_MODE_FLAG = 1, then calls
EVENT.entr_zone. -> the compare's "push 0" becomes "push -1", so the intro never runs.
Output: a plain (key 0) NBL, PRS data section, same members / pointer table (script.bin must be last).
usage: python roompatch.py <native zone nbl> <out nbl>"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nbl, prs
from prsenc import compress

PUSH, GET, CALLX, EQ, LTEQ, JZ = 2, 0x3C, 0x4C, 0x12, 0x16, 0x2D


def routines(tsb):
    """(name, code offset, instruction bytes) for every routine of a TSB2 script."""
    pos = 0
    while True:
        a = 12 + pos
        nxt, = struct.unpack_from("<I", tsb, a)
        isz, kind = struct.unpack_from("<II", tsb, a + 36)
        if kind == 0x4C:
            yield tsb[a + 4:a + 36].split(b"\0")[0].decode("latin1"), a + 48, isz - 4
        if nxt == 0:
            return
        pos = nxt


def words(tsb, code, size):
    return list(struct.unpack_from(f"<{size // 4}I", tsb, code))


def patch(tsb):
    done = []
    for name, code, size in routines(tsb):
        w = words(tsb, code, size)
        if name in ("EVENT.entr_zone", "cw_Fm_Set_Appare_Flag"):
            # first "get <v>; push 0; eq; jz" = the loop's exit test
            for i in range(len(w) - 5):
                if w[i] == GET and w[i + 2] == PUSH and w[i + 3] == 0 and w[i + 4] == EQ and w[i + 5] == JZ:
                    struct.pack_into("<I", tsb, code + 4 * (i + 4), LTEQ)
                    done.append(f"{name}: loop test eq -> lteq at script+{code + 4 * (i + 4):#x}")
                    break
        elif name == "EVENT.entr_unit0001":
            # first "callx <chrflag_get>; push 0; eq; jz" = "if EXTRA_MODE_FLAG == 0" (intro)
            for i in range(len(w) - 4):
                if w[i] == CALLX and w[i + 2] == PUSH and w[i + 3] == 0 and w[i + 4] == EQ and w[i + 5] == JZ:
                    struct.pack_into("<i", tsb, code + 4 * (i + 3), -1)
                    done.append(f"{name}: intro test push 0 -> -1 at script+{code + 4 * (i + 3):#x}")
                    break
    return done


def main():
    src, dst = sys.argv[1], sys.argv[2]
    d = open(src, "rb").read()
    nb = nbl.parse(d)
    data = bytearray(nbl.nbl_data(d, nb))
    sm = {m["name"]: m for m in nb["members"]}["script.bin"]
    assert sm["off"] + sm["size"] == max(m["off"] + m["size"] for m in nb["members"]), "script.bin must be last"
    sb = data[sm["off"]:sm["off"] + sm["size"]]
    unpacked, = struct.unpack_from("<I", sb, 0)
    tsb = bytearray(prs.decompress(sb, 0x1C))
    assert len(tsb) == unpacked and tsb[:4] == b"TSB2"
    done = patch(tsb)
    print("\n".join(done))
    assert done, "nothing to patch"

    packed = compress(bytes(tsb))
    assert prs.decompress(packed) == bytes(tsb)
    newsb = struct.pack("<II", len(tsb), len(packed)) + bytes(0x14) + packed
    data = bytes(data[:sm["off"]]) + newsb
    data += bytes(-len(data) % 0x20)
    pdata = compress(data)
    assert prs.decompress(pdata) == data

    hdr = bytearray(d[:nb["hsize"]])
    struct.pack_into("<IIII", hdr, 0x10, len(data), len(pdata), nb["ptsize"], 0)   # dsize, csize, ptsize, key 0
    for m in nb["members"]:                                                        # records written decrypted
        o = m["rec"]
        body = bytearray(d[o + nbl.REC_ENC_START:o + nbl.REC_ENC_END])
        if nb["bf"]:
            body = bytearray(nb["bf"].decrypt(bytes(body)))
        if m is sm:
            struct.pack_into("<I", body, 0x24, len(newsb))
        hdr[o + nbl.REC_ENC_START:o + nbl.REC_ENC_END] = body
    out = bytearray(hdr) + bytes(nbl.align(len(hdr)) - len(hdr))
    out += pdata + bytes(nbl.align(len(out) + len(pdata)) - len(out) - len(pdata))
    out += d[nb["ptr_off"]:nb["ptr_off"] + nb["ptsize"]]
    out += bytes(nbl.align(len(out)) - len(out))
    open(dst, "wb").write(out)
    print(f"{src}: {len(d)} B -> {dst}: {len(out)} B (script {len(sb)} -> {len(newsb)} B)")


if __name__ == "__main__":
    main()
