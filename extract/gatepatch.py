"""Native Colony zone NBL (Extra lobby zone02) with the 4F PPT gate chapter checks disabled.

EVENT.entr_unit0004 compares cwGet_Ex_Chapter_Number (always -1 online: seq.is_online) with 12 / 6 / 5 and
closes the Spaceport gate / blocks Moatoob / blocks Neudaiz. Patching those immediates to -1 makes every
compare false. AotI's Colony (XF_QE_OFF_LOBBY_ILLUMINUS zone02) gates on story flags instead:
"pushs 'S13_FLAG_CLR'; call work.chrflag_get; push 0; eq; jz" blocks Neudaiz (S14: Moatoob) while the
flag is clear; the "push 0" becomes "push -1" so the block never runs (Palm and the Spaceport always pass).
Output: a plain (key 0) NBL, PRS data section, same members / pointer table.
usage: python gatepatch.py <native zone nbl> <out nbl>"""
import struct, sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nbl, prs
from prsenc import compress

src, dst = sys.argv[1], sys.argv[2]
d = open(src, "rb").read()
nb = nbl.parse(d)
data = bytearray(nbl.nbl_data(d, nb))
mem = {m["name"]: m for m in nb["members"]}
sm = mem["script.bin"]
assert sm["off"] + sm["size"] == max(m["off"] + m["size"] for m in nb["members"]), "script.bin must be last"
sb = data[sm["off"]:sm["off"] + sm["size"]]
unpacked, = struct.unpack_from("<I", sb, 0)
tsb = bytearray(prs.decompress(sb, 0x1C))
assert len(tsb) == unpacked and tsb[:4] == b"TSB2"

# find EVENT.entr_unit0004 and patch "push 12/6/5" (op 2) that precede "lt" (0x17)
R, pos, done = 12, 0, 0
while True:
    a = R + pos
    nxt, = struct.unpack_from("<I", tsb, a)
    name = tsb[a + 4:a + 36].split(b"\0")[0]
    if name == b"EVENT.entr_unit0004":
        isz, = struct.unpack_from("<I", tsb, a + 36)
        code = a + 48
        for k in range(0, isz - 12, 4):
            op, arg, nx = struct.unpack_from("<3I", tsb, code + k)
            if op == 2 and arg in (12, 6, 5) and nx == 0x17:
                struct.pack_into("<i", tsb, code + k + 4, -1)
                print(f"patched push {arg} at script+{code + k + 4:#x}")
                done += 1
            elif (op, arg, nx) == (2, 0, 0x12) and k >= 8 and struct.unpack_from("<I", tsb, code + k - 8)[0] == 0x4C:
                struct.pack_into("<i", tsb, code + k + 4, -1)          # AotI: chrflag_get(S1x_FLAG_CLR) == 0
                print(f"patched flag test push 0 at script+{code + k + 4:#x}")
                done += 1
    if nxt == 0:
        break
    pos = nxt
assert done in (2, 3), done          # PSU: 3 chapter compares, AotI: 2 flag tests

packed = compress(bytes(tsb))
assert prs.decompress(packed) == bytes(tsb)
newsb = struct.pack("<II", len(tsb), len(packed)) + bytes(0x14) + packed
# keep the original 0x1C header words except the two sizes
newsb = bytes(sb[:0]) + newsb
data = bytes(data[:sm["off"]]) + newsb
data += bytes(-len(data) % 0x20)
dsize = len(data)
pdata = compress(data)
assert prs.decompress(pdata) == data

hdr = bytearray(d[:nb["hsize"]])
struct.pack_into("<IIII", hdr, 0x10, dsize, len(pdata), nb["ptsize"], 0)   # dsize, csize, ptsize, key 0
for m in nb["members"]:                                                    # write the records decrypted
    o = 0x30 + (m["rec"] - 0x30) if False else m["rec"]
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
