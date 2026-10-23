"""Sega PRS decompression (the LZ77 variant NBL archives use for their data section).

Bitstream of control bits, LSB first, refilled a byte at a time, interleaved with data bytes:
  1                 literal byte
  0 0 b1 b0 off8    short copy: length (b1 b0) + 2, offset = off8 - 256
  0 1 lo hi [n]     long copy:  w = lo | hi << 8; w == 0 ends the stream;
                    offset = (w >> 3) - 0x2000; length = (w & 7) + 2, or n + 1 when (w & 7) == 0

  python prs.py <in> <out> [--skip N]      (NBL data sections start at 0x800)
"""
import sys


def decompress(src, start=0):
    out = bytearray()
    i = start
    ctrl = 0
    nbits = 0

    def bit():
        nonlocal ctrl, nbits, i
        if nbits == 0:
            ctrl = src[i]
            i += 1
            nbits = 8
        b = ctrl & 1
        ctrl >>= 1
        nbits -= 1
        return b

    while i < len(src):
        if bit():
            out.append(src[i])
            i += 1
            continue
        if bit():
            w = src[i] | (src[i + 1] << 8)
            i += 2
            if w == 0:
                break
            offset = (w >> 3) - 0x2000
            size = w & 7
            if size == 0:
                size = src[i] + 1
                i += 1
            else:
                size += 2
        else:
            size = ((bit() << 1) | bit()) + 2
            offset = src[i] - 256
            i += 1
        for _ in range(size):
            out.append(out[len(out) + offset])
    return bytes(out)


def main():
    a = sys.argv[1:]
    if len(a) < 2:
        raise SystemExit(__doc__)
    skip = int(a[a.index("--skip") + 1], 0) if "--skip" in a else 0
    data = open(a[0], "rb").read()
    out = decompress(data, skip)
    open(a[1], "wb").write(out)
    print(f"{len(data) - skip} -> {len(out)} bytes")


if __name__ == "__main__":
    main()
