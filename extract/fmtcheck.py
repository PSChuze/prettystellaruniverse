"""Round-trip proof for nblpack.py and relfmt.py over every NBL under the given paths.

  python fmtcheck.py [--fresh] [--jobs N] [--list files.txt] [<dir or .nbl>...]
      default path: the work directory (workdir.py; "*_nbl" unpack dirs and "*staging*" dirs skipped)

Per NBL: unpack -> pack must give the same bytes (nblpack.check); --fresh also packs without the cached PRS
streams and compares records, pointer table and decompressed data. Members that are NBLs themselves (counter
tables carry one quest NBL per mission) are checked the same way. Per member of a kind relfmt knows (quest.snr,
set_rN.rel, enemy*.snr, table.rel, unit_title_table.rel, text.bin / text_result.bin / unit_title_NN.bin):
decode -> JSON -> encode must give the same bytes and the same relocation slots. Typed coverage = the share of
chunk bytes in typed blocks (the rest stays hex). The disc is "aoti" for paths with /aoti, else "psu".
"""
import collections
import os
import sys
import time
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nblpack  # noqa: E402
import relfmt  # noqa: E402
import workdir  # noqa: E402

DEFAULT = [workdir.work()]


def find(paths):
    out = []

    def walk(p):
        try:
            ents = list(os.scandir(p))
        except OSError:
            return
        for e in ents:
            if e.is_dir(follow_symlinks=False):
                if not e.name.endswith("_nbl") and "staging" not in e.name:
                    walk(e.path)
            elif e.name.lower().endswith(".nbl"):
                out.append(e.path.replace("\\", "/"))
    for p in paths:
        if os.path.isdir(p):
            walk(p)
        else:
            out.append(p)
    return out


def check_nbl(d, fresh, st, disc, nested=False):
    tag = "nbl-nested" if nested else "nbl"
    exact, fr = nblpack.check(d, fresh)
    st[(tag, disc, "files")] += 1
    st[(tag, disc, "exact")] += exact
    if fr is not None:
        st[(tag, disc, "prs")] += 1
        st[(tag, disc, "fresh_ok")] += fr
    man, files = nblpack.unpack(d)
    for m in man["members"]:
        b = files[m["file"]]
        if b[:4] == b"NMLL":
            check_nbl(b, fresh, st, disc, True)
            continue
        kind = relfmt.kind_of(m["name"])
        if kind is None:
            continue
        key = (kind, disc)
        st[key + ("files",)] += 1
        if "ptrs_abs" in m:
            st[key + ("skipped_abs",)] += 1
            continue
        slots = m.get("ptrs", [])
        try:
            j = relfmt.decode(m["name"], b, slots)
        except relfmt.Bad as e:
            st[key + ("bad:" + str(e).split(":")[0][:40],)] += 1
            continue
        if relfmt.encode(j) == (b, slots):
            st[key + ("exact",)] += 1
        else:
            st[key + ("DIFF",)] += 1
        if kind == "quest":
            main = next(x for x in j["blocks"] if x["label"] == j["main"])
            if "records" in main:
                st[key + (f"version_{main['records'][0]['version']:#x}",)] += 1
        if kind != "text":
            tb = sum(len(relfmt._block_bytes(x, lambda v: 0)[0]) for x in j["blocks"] if "records" in x)
            allb = sum(len(relfmt._block_bytes(x, lambda v: 0)[0]) for x in j["blocks"])
            st[key + ("bytes",)] += allb
            st[key + ("typed_bytes",)] += tb
        else:
            st[key + ("strings",)] += len(j["strings"])


def one(args):
    p, fresh = args
    st = collections.Counter()
    disc = "aoti" if "/aoti" in p else "psu"
    try:
        check_nbl(open(p, "rb").read(), fresh, st, disc)
    except Exception as e:                       # report, keep going
        st[("error", disc, type(e).__name__ + ": " + str(e)[:60])] += 1
        return st, p
    return st, None


def main():
    a = sys.argv[1:]
    fresh = "--fresh" in a
    jobs = int(a[a.index("--jobs") + 1]) if "--jobs" in a else os.cpu_count()
    if "--list" in a:
        paths = [x.strip() for x in open(a[a.index("--list") + 1]) if x.strip()]
    else:
        args = [x for i, x in enumerate(a) if not x.startswith("--") and (i == 0 or a[i - 1] not in ("--jobs",))]
        paths = find(args or DEFAULT)
    t = time.time()
    tot, errs = collections.Counter(), []
    with Pool(jobs) as pool:
        for st, err in pool.imap_unordered(one, [(p, fresh) for p in paths], chunksize=4):
            tot.update(st)
            if err:
                errs.append(err)
    print(f"{len(paths)} NBL files, {time.time() - t:.0f} s")
    kinds = sorted({k[0] for k in tot})
    for kind in kinds:
        for disc in ("psu", "aoti"):
            row = {k[2]: v for k, v in tot.items() if k[0] == kind and k[1] == disc}
            if not row:
                continue
            files = row.pop("files", 0)
            exact = row.pop("exact", 0)
            extra = ""
            if "bytes" in row:
                extra = f"  typed {100 * row.pop('typed_bytes', 0) / max(1, row.pop('bytes')):.1f} % of bytes"
            rest = "  ".join(f"{k} {v}" for k, v in sorted(row.items()))
            print(f"  {kind:12} {disc:5} {files:6} files  {exact:6} exact{extra}  {rest}")
    for e in errs[:20]:
        print("  error:", e)
    sys.exit(1 if errs or any(k[2] in ("DIFF",) for k in tot) else 0)


if __name__ == "__main__":
    main()
