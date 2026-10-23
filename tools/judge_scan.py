"""Scan every quest rule in a missions.json against psu/judge.py (dev check, needs the built mission data).

Per rule (start, clear, fail, abort, rank reward conditions): every token known (0..6 or judge.ARGC), the
RPN stack ends at depth 1 (the client's "result stack depth != 1" check), and commands the server does not
judge are listed (exit 1 only for malformed rules). Known: PSU's 111 (1 operand) and 151 n (random 0..n-1,
AotI 0x393570) appear only in PSU S-rank item reward conditions and stay never (PSU judging unchanged).
Then each start rule is evaluated at the mission's enter location with no flags set (false there: it
waits for a flag; listed).

  python tools/judge_scan.py [missions.json ...]     default: $PSU_NBL_DIR (or $PSU_WORK/lobby-nbl/ps2) and its aoti/
"""
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("PSU_WORK") or os.path.join(HERE, "..", "..", "work")   # data built from the discs (docs/data.md)
sys.path.insert(0, os.path.join(HERE, "..", "server"))
os.environ.setdefault("PSU_GAMEDATA", os.path.join(WORK, "psu", "gamedata"))

from psu import judge  # noqa: E402

JUDGED = {100, 120, 134, 135, 147}


def rules(q):
    for k in ("start", "clear", "fail", "abort"):
        yield k, q.get(k)
    for rk in ("rank", "rank2"):
        for i, (rid, cond) in enumerate((q.get(rk) or {}).get("rewards", [])):
            yield f"{rk}.reward{rid}", cond


def check(tokens):
    """-> (ok, commands used, problem)"""
    depth, i, used = 0, 0, set()
    while i < len(tokens):
        t = tokens[i]
        if 0 <= t <= 6:
            if depth < 2:
                return False, used, "too few operands"
            depth -= 1
            i += 1
        elif t in judge.ARGC or t == 147:
            used.add(t)
            depth += 1
            i += 1 + judge.ARGC.get(t, 0)
        else:
            return False, used, f"unknown command {t}"
    return depth == 1 and i == len(tokens), used, None if depth == 1 else f"depth {depth}"


def scan(path):
    d = json.load(open(path, encoding="utf-8"))
    n_rules = n_null = 0
    bad, unjudged, cmds = [], collections.Counter(), collections.Counter()
    start_true = start_wait = 0
    for qid, m in sorted(d["missions"].items()):
        q = m.get("quest") or {}
        for name, t in rules(q):
            if not t:
                n_null += 1
                continue
            n_rules += 1
            ok, used, why = check(t)
            for c in used:
                cmds[c] += 1
                if c not in JUDGED:
                    unjudged[(c, qid, name)] += 1
            if not ok:
                bad.append((qid, name, why, t))
        st = q.get("start")
        if st:
            ez, em, _ = m["enter"]
            if judge.evaluate(st, lambda *a: False, at=(int(qid), ez, em)):
                start_true += 1
            else:
                start_wait += 1
                print(f"  {qid} {m['name']}: start false at the enter location {ez}/{em}: {st}")
    print(f"{path}: {len(d['missions'])} missions, {n_rules} rules ({n_null} null = never), "
          f"commands {dict(sorted(cmds.items()))}")
    print(f"  malformed: {len(bad)}; not judged by the server: {len(unjudged)}; "
          f"start true on entry: {start_true}, waits for a flag: {start_wait}")
    for b in bad:
        print("  malformed", b)
    for k in unjudged:
        print("  not judged", k)
    return not bad


def main():
    paths = sys.argv[1:]
    if not paths:
        root = os.environ.get("PSU_NBL_DIR", os.path.join(WORK, "lobby-nbl", "ps2"))
        paths = [p for p in (os.path.join(root, "missions.json"), os.path.join(root, "aoti", "missions.json"))
                 if os.path.exists(p)]
    ok = all([scan(p) for p in paths])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
