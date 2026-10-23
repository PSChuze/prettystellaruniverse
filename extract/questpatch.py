"""Set the quest id inside a quest NBL's quest.snr (main block +8), e.g. the offline Extra lobby quest
(id 13001) served online as My Room (1120000).

The client keeps the quest.snr id as the current quest (TQuestManager +0x3C, getter 0x169110). Each frame
TPlayer's fade (0x143650) asks vfunc +0x38 (0x14ea80) whether the player's area (+0x27C quest, +0x280 zone,
+0x284 map, from the server's location packets) equals the current quest / zone / map. If it does not, alpha
(+0x278) is forced to 0 and the player is hidden (task flag 0x20) and gets no control. With 13001 in the
file and 1120000 in the packets, the My Room player never appears.
Output: a plain (key 0) NBL, PRS data section, same members / pointer table.
usage: python questpatch.py <quest nbl> <out nbl> <quest id>"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nbl, prs
from prsenc import compress


def main():
    src, dst, qid = sys.argv[1], sys.argv[2], int(sys.argv[3], 0)
    d = open(src, "rb").read()
    nb = nbl.parse(d)
    data = bytearray(nbl.nbl_data(d, nb))
    snr = {m["name"]: m for m in nb["members"]}["quest.snr"]
    main_ptr, = struct.unpack_from("<I", data, snr["off"] + 8)
    at = snr["off"] + main_ptr + 8
    old, = struct.unpack_from("<I", data, at)
    struct.pack_into("<I", data, at, qid)
    print(f"quest.snr id {old} -> {qid} (quest.snr +{main_ptr + 8:#x})")

    pdata = compress(bytes(data))
    assert prs.decompress(pdata) == bytes(data)
    hdr = bytearray(d[:nb["hsize"]])
    struct.pack_into("<IIII", hdr, 0x10, len(data), len(pdata), nb["ptsize"], 0)   # dsize, csize, ptsize, key 0
    for m in nb["members"]:                                                        # records written decrypted
        o = m["rec"]
        body = d[o + nbl.REC_ENC_START:o + nbl.REC_ENC_END]
        if nb["bf"]:
            body = nb["bf"].decrypt(body)
        hdr[o + nbl.REC_ENC_START:o + nbl.REC_ENC_END] = body
    out = bytearray(hdr) + bytes(nbl.align(len(hdr)) - len(hdr))
    out += pdata + bytes(nbl.align(len(out) + len(pdata)) - len(out) - len(pdata))
    out += d[nb["ptr_off"]:nb["ptr_off"] + nb["ptsize"]]
    out += bytes(nbl.align(len(out)) - len(out))
    open(dst, "wb").write(out)
    print(f"{src}: {len(d)} B -> {dst}: {len(out)} B")


if __name__ == "__main__":
    main()
