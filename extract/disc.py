"""Which disc an ISO is, and whether it is one the tools can read.

A disc is recognised by its size, the boot ELF SYSTEM.CNF names, and the four DNAS call sites in its game
module (ERX/PSU_US.GZ; the same sites patches/bypass.json patches). A DNAS-patched copy (bypass.py iso, the
"[ONLINE]" images) has the patched words there instead: its tables are the retail ones, so it is accepted.
The sha1 values are the retail dumps and bypass.py's output from them; a mismatch is only reported.
Anything else (another region, a PAL or Japanese disc, a PC or Xbox 360 image) is refused.
"""
import gzip
import hashlib
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import erx  # noqa: E402
import iso  # noqa: E402

DISCS = {
    "psu": {"serial": "SLUS-21194", "title": "Phantasy Star Universe (USA)", "boot": "SLUS_211.94",
            "size": 4251156480, "sha1": "92e54e8f0e6ab0f00aa245bab3e67e19a935473e",
            "patched_sha1": "176f6f0c542307d410d2a62decf4e8536756b2b2",
            "sites": ((0x352314, 0x0C122D8D, 0x10000017), (0x352590, 0x0C122D8B, 0x0000102D),
                      (0x3524E0, 0x0C122D87, 0x0000102D), (0x352438, 0x0C122D89, 0x0000102D))},
    "aoti": {"serial": "SLUS-21631", "title": "Phantasy Star Universe: Ambition of the Illuminus (USA)",
             "boot": "SLUS_216.31", "size": 3703570432, "sha1": "b3dd9016bb03f7c4ca6f245d364369142783a04b",
             "patched_sha1": "275cc78d54117d59d28488e2511f77674b7aaa26",
             "sites": ((0x350584, 0x0C138BF6, 0x1000001E), (0x350808, 0x0C138BF4, 0x0000102D),
                       (0x350758, 0x0C138BF0, 0x0000102D), (0x3506B0, 0x0C138BF2, 0x0000102D))},
}


class DiscError(Exception):
    pass


def files(path):
    """{path on the disc: (lba, size)} from the ISO9660 tree."""
    out = {}
    with open(path, "rb") as f:
        f.seek(16 * iso.SECTOR)
        pvd = f.read(iso.SECTOR)
        if pvd[1:6] != b"CD001":
            raise DiscError(f"{path}: not an ISO9660 image")
        root = pvd[156:190]
        rlba, = struct.unpack("<I", root[2:6])
        rsz, = struct.unpack("<I", root[10:14])
        for full, lba, sz, is_dir in iso.walk(f, rlba, rsz):
            if not is_dir:
                out[full.lstrip("/").upper()] = (lba, sz)
    return out


def read(path, entry):
    with open(path, "rb") as f:
        f.seek(entry[0] * iso.SECTOR)
        return f.read(entry[1])


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def identify(path, cache=None, verify=False):
    """{game, serial, title, patched, sha1} for a supported disc, or DiscError saying what it is not.
    verify: also hash the whole image (once per file: the result is kept in the cache dir)."""
    if not os.path.isfile(path):
        raise DiscError(f"{path}: no such file")
    size = os.path.getsize(path)
    fs = files(path)
    cnf = read(path, fs["SYSTEM.CNF"]).decode("latin1") if "SYSTEM.CNF" in fs else ""
    game = next((g for g, d in DISCS.items() if d["boot"] in cnf), None)
    if game is None and not cnf:
        raise DiscError(f"{path}: not a PlayStation 2 disc (no SYSTEM.CNF); use the US PlayStation 2 discs "
                        f"{DISCS['psu']['serial']} or {DISCS['aoti']['serial']}")
    if game is None:
        boot = next((ln.split("\\")[-1].split(";")[0] for ln in cnf.splitlines() if "BOOT2" in ln), None)
        raise DiscError(f"{path}: not a supported disc (boot ELF {boot or 'unknown'}); "
                        f"use the US PlayStation 2 discs {DISCS['psu']['serial']} or {DISCS['aoti']['serial']}")
    d = DISCS[game]
    if size != d["size"]:
        raise DiscError(f"{path}: {d['serial']} but {size} bytes, not {d['size']}: a trimmed or altered image")
    if "ERX/PSU_US.GZ" not in fs:
        raise DiscError(f"{path}: no ERX/PSU_US.GZ")
    tmp = os.path.join(cache or ".", f".{game}-module.erx")
    os.makedirs(os.path.dirname(os.path.abspath(tmp)), exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(gzip.decompress(read(path, fs["ERX/PSU_US.GZ"])))
    try:
        im = erx.open_image(tmp)
        words = [struct.unpack_from("<I", im.d, im.va2off(va))[0] for va, _old, _new in d["sites"]]
    finally:
        os.remove(tmp)
    if words == [old for _va, old, _new in d["sites"]]:
        patched = False
    elif words == [new for _va, _old, new in d["sites"]]:
        patched = True
    else:
        raise DiscError(f"{path}: {d['serial']}, but its game module is not the retail one "
                        "(another revision, or patched by something other than bypass.py)")
    info = {"game": game, "serial": d["serial"], "title": d["title"], "patched": patched, "sha1": None}
    if verify:
        info["sha1"] = cached_sha1(path, cache)
        if info["sha1"] != d["patched_sha1" if patched else "sha1"]:
            print(f"note: {path} is not the known dump ({info['sha1']}); its game module matches, continuing")
    return info


def cached_sha1(path, cache):
    st = os.stat(path)
    key = f"{st.st_size}-{int(st.st_mtime)}"
    db = os.path.join(cache, "sha1.json") if cache else None
    known = {}
    if db and os.path.exists(db):
        with open(db, encoding="utf-8") as f:
            known = json.load(f)
    if key not in known:
        known[key] = sha1(path)
        if db:
            with open(db, "w", encoding="utf-8") as f:
                json.dump(known, f, indent=1)
    return known[key]


if __name__ == "__main__":
    for p in sys.argv[1:]:
        try:
            print(p, identify(p, verify="--sha1" in sys.argv))
        except DiscError as e:
            print(e)
