"""Greedy PRS compressor (inverse of prs.py decompress). Control bits LSB first; a control
byte is reserved in the output the moment its first bit is needed, exactly where the decoder reads it."""


class _W:
    def __init__(self):
        self.out = bytearray()
        self.cpos = -1
        self.nbits = 8

    def bit(self, b):
        if self.nbits == 8:
            self.cpos = len(self.out)
            self.out.append(0)
            self.nbits = 0
        if b:
            self.out[self.cpos] |= 1 << self.nbits
        self.nbits += 1

    def byte(self, v):
        self.out.append(v & 0xFF)


def compress(src, window=0x1FFF, max_len=256, chain=64):
    w = _W()
    n = len(src)
    heads = {}
    i = 0

    def insert(p):
        if p + 3 <= n:
            heads.setdefault(src[p:p + 3], []).append(p)

    while i < n:
        best_len, best_off = 0, 0
        if i + 3 <= n:
            cands = heads.get(src[i:i + 3], [])
            for p in reversed(cands[-chain:]):
                if i - p > window:
                    break
                L = 0
                while L < max_len and i + L < n and src[p + L] == src[i + L]:
                    L += 1
                if L > best_len:
                    best_len, best_off = L, i - p
                    if L == max_len:
                        break
        # short copy: len 2..5, offset 1..256
        if best_len >= 2 and best_len <= 5 and best_off <= 256:
            w.bit(0); w.bit(0)
            k = best_len - 2
            w.bit(k >> 1); w.bit(k & 1)
            w.byte(256 - best_off)
        elif best_len >= 3:
            L = best_len
            off = (-best_off) & 0x1FFF
            if L <= 9:
                v = (off << 3) | (L - 2)
                w.bit(0); w.bit(1); w.byte(v); w.byte(v >> 8)
            else:
                v = off << 3
                w.bit(0); w.bit(1); w.byte(v); w.byte(v >> 8); w.byte(L - 1)
        else:
            best_len = 1
            w.bit(1); w.byte(src[i])
        for p in range(i, i + best_len):
            insert(p)
        i += best_len
    w.bit(0); w.bit(1); w.byte(0); w.byte(0)
    return bytes(w.out)
