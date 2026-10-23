"""The public board: a JSON snapshot for an outside board service (for example an OpenLobby board) to show on a
web page or a chat channel. The game service writes it; the board reads it and never touches psu.db.

  $PSU_BOARD_FILE (default $PSU_LOG_DIR/board/board.json), rewritten every BOARD_SECS (atomic replace). Mount only
  the board/ directory into the board service (read-only), so session logs stay out of it.

What it holds (version 1). Names are character names only, never account names; characters on a banned account
are left out of every list.
  updated, uptime_s     epoch seconds; seconds since the game service started
  online                {total, psu, aoti, universes: [{id, name, count}], players: [{name, level, cls, game, uni,
                        where}]}: where is "lobby", "mission" (+ mission = its name) or "room"
  levels                top characters by level, then EXP: {name, level, cls, game, hours}
  types                 per class type with anyone above type level 1: {cls, rows: [{name, level}]}
  missions              per quest with a clear: {quest, name, game, clears, best: [{secs, rank, party, when}]}; best is
                        the fastest clear of each distinct party, fastest first
  recent                the latest clears: {quest, name, game, secs, rank, party, when}
  week                  most clears in the last seven days: {name, clears}
  story                 Network Mode story acts cleared: {name, acts, chapter}
  season                {now: season name or null, next: {name, starts} under PSU_SEASON=auto}
  events                the host's event calendar ($PSU_EVENTS_FILE, psu/events.py), running and upcoming
                        (events.board_rows); an event with featured missions gets results: per mission
                        {quest, name, best: [...]}, the fastest clear of each distinct party made while it ran
  totals                {accounts, characters, clears, clears_week}
Ranks are letters (judge rank index 0..3 = C, B, A, S). cls is the class name (types 0..15).
"""
import datetime
import json
import os
import time

from psu import missions, season as seasons
from psu.area import LOBBY_QUESTS
from psu.charload import UNIVERSES
from psu.observe import LOG_DIR

BOARD_FILE = os.environ.get("PSU_BOARD_FILE", os.path.join(LOG_DIR, "board", "board.json"))
BOARD_SECS = float(os.environ.get("PSU_BOARD_SECS", "60"))
TOP = 25
BEST = 5                            # distinct parties per mission
RECENT = 20
WEEK = 7 * 86400

CLASSES = ("Hunter", "Ranger", "Force", "Fighgunner", "Guntecher", "Wartecher", "Fortefighter", "Fortegunner",
           "Fortetecher", "Protranser", "Acrofighter", "Acrotecher", "Fighmaster", "Gunmaster", "Masterforce",
           "Acromaster")
RANKS = "CBAS"


def cls_name(cls):
    return CLASSES[cls] if isinstance(cls, int) and 0 <= cls < len(CLASSES) else "?"


def char_game(st):
    """The disc a character last played on; an AotI-only class says AotI for characters from before it was kept."""
    return st.get("game") or ("aoti" if (st.get("cls") or 0) >= 10 else "psu")


def mission_name(qid, game):
    m = missions.mission(qid, game) or missions.mission(qid, "aoti" if game == "psu" else "psu") or {}
    return m.get("name") or f"Mission {qid}"


def _where(o):
    q = o.area[0] if o.area else None
    if q is None or q in LOBBY_QUESTS:
        return {"where": "lobby"}
    m = missions.mission(q, o.game)
    if m is not None:
        return {"where": "mission", "mission": m.get("name") or f"Mission {q}"}
    return {"where": "room"}


def _online(world, names):
    per_game, per_uni, players = {"psu": 0, "aoti": 0}, {}, []
    for o in list(world.online.values()):
        per_game[o.game] = per_game.get(o.game, 0) + 1
        per_uni[o.uni] = per_uni.get(o.uni, 0) + 1
        st = o.state or {}
        name = names.get((o.gid, getattr(o, "slot", 0)))
        if name is None:
            continue
        players.append(dict(name=name, level=st.get("level", 1), cls=cls_name(st.get("cls")), game=o.game, uni=o.uni,
                            **_where(o)))
    players.sort(key=lambda p: (-p["level"], p["name"].lower()))
    return {"total": len(world.online), **per_game, "players": players,
            "universes": [{"id": u, "name": n, "count": per_uni.get(u, 0)} for u, n in UNIVERSES]}


def _story(st):
    """(acts, highest chapter) from the per-character story record (missions.record_clear)."""
    acts = set()
    for q in (st.get("story") or {}):
        for game in ("aoti", "psu"):
            s = (missions.mission(q, game) or {}).get("story")
            if s:
                acts.add((s["chapter"], s["act"]))
                break
    return len(acts), max((c for c, _ in acts), default=0)


def _season(today):
    now = seasons.current(today)
    nxt = None
    if (os.environ.get("PSU_SEASON") or "").strip().lower() == "auto":
        for d in range(1, 120):
            day = today + datetime.timedelta(days=d)
            name = seasons.by_date(day)
            if name and name != now:
                nxt = {"name": name, "starts": day.isoformat()}
                break
    return {"now": now, "next": nxt}


def _featured(c, quest, since, until, banned):
    """A featured mission's ranking for one event run: fastest clear of each distinct party."""
    best, seen, name = [], set(), None
    for t, game, secs, rank, party in c.execute(
            "SELECT time, game, secs, rank, party FROM clears WHERE quest = ? AND time >= ? AND time <= ? "
            "ORDER BY secs, time", (quest, since, until)):
        party = json.loads(party)
        name = name or mission_name(quest, game)
        key = frozenset((m[0], m[1]) for m in party)
        who = [m[2] for m in party if m[0] not in banned]
        if who and key not in seen:
            seen.add(key)
            best.append({"secs": secs, "rank": RANKS[rank] if 0 <= rank < 4 else "?", "party": who, "when": int(t)})
            if len(best) >= 10:
                break
    if name is None:
        name = mission_name(quest, "psu")
    return {"quest": quest, "name": name, "best": best}


def snapshot(store, world, started, now=None, events=None):
    now = time.time() if now is None else now
    c = store.c
    banned = {g for g, in c.execute("SELECT gid FROM bans WHERE until >= ?", (now,))}
    chars, names = [], {}
    for gid, slot, name, st in c.execute("SELECT gid, slot, name, state FROM characters"):
        if not name:
            continue
        names[(gid, slot)] = name
        if gid in banned:
            continue
        st = json.loads(st or "{}")
        if "level" in st:
            chars.append((gid, slot, name, st))

    levels = sorted(chars, key=lambda r: (-r[3].get("level", 1), -r[3].get("exp", 0), r[2].lower()))[:TOP]
    levels = [{"name": n, "level": st.get("level", 1), "cls": cls_name(st.get("cls")), "game": char_game(st),
               "hours": int(st.get("playtime", 0)) // 3600} for _, _, n, st in levels]

    types = []
    for t, tname in enumerate(CLASSES):
        rows = [(n, (st.get("type_level") or [])[t]) for _, _, n, st in chars if len(st.get("type_level") or []) > t]
        rows = sorted((r for r in rows if r[1] > 1), key=lambda r: (-r[1], r[0].lower()))[:10]
        if rows:
            types.append({"cls": tname, "rows": [{"name": n, "level": lv} for n, lv in rows]})

    story = []
    for _, _, n, st in chars:
        acts, chapter = _story(st)
        if acts:
            story.append({"name": n, "acts": acts, "chapter": chapter})
    story.sort(key=lambda r: (-r["acts"], -r["chapter"], r["name"].lower()))

    def party_names(party):
        return [m[2] for m in party if m[0] not in banned]

    by_quest, recent, week = {}, [], {}
    for t, game, quest, secs, rank, party in c.execute(
            "SELECT time, game, quest, secs, rank, party FROM clears ORDER BY secs, time"):
        party = json.loads(party)
        who = party_names(party)
        if not who:
            continue
        q = by_quest.setdefault(quest, {"quest": quest, "name": mission_name(quest, game), "game": game,
                                        "clears": 0, "best": [], "_seen": set()})
        q["clears"] += 1
        key = frozenset((m[0], m[1]) for m in party)
        if key not in q["_seen"] and len(q["best"]) < BEST:
            q["_seen"].add(key)
            q["best"].append({"secs": secs, "rank": RANKS[rank] if 0 <= rank < 4 else "?", "party": who, "when": int(t)})
        if now - t <= WEEK:
            for m in party:
                if m[0] not in banned:
                    week[m[2]] = week.get(m[2], 0) + 1
    for t, game, quest, secs, rank, party in c.execute(
            "SELECT time, game, quest, secs, rank, party FROM clears ORDER BY time DESC LIMIT ?", (RECENT * 2,)):
        who = party_names(json.loads(party))
        if who and len(recent) < RECENT:
            recent.append({"quest": quest, "name": mission_name(quest, game), "game": game, "secs": secs,
                           "rank": RANKS[rank] if 0 <= rank < 4 else "?", "party": who, "when": int(t)})
    for q in by_quest.values():
        del q["_seen"]
    events = [dict(e) for e in (events or [])]
    for e in events:
        if e.get("missions") and e["start"] <= now:
            e["results"] = [_featured(c, q, e["start"], min(e["end"], now), banned) for q in e["missions"]]
    mission_rows = sorted(by_quest.values(), key=lambda q: (-q["clears"], q["quest"]))

    total_clears = c.execute("SELECT COUNT(*) FROM clears").fetchone()[0]
    return {
        "v": 1, "updated": int(now), "uptime_s": int(now - started),
        "online": _online(world, names),
        "levels": levels, "types": types, "missions": mission_rows, "recent": recent,
        "week": [{"name": n, "clears": k} for n, k in sorted(week.items(), key=lambda kv: (-kv[1], kv[0].lower()))][:TOP],
        "story": story[:TOP],
        "season": _season(datetime.date.fromtimestamp(now)),
        "events": events,
        "totals": {"accounts": c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], "characters": len(names),
                   "clears": total_clears, "clears_week": sum(1 for _ in c.execute(
                       "SELECT 1 FROM clears WHERE time >= ?", (now - WEEK,)))},
    }


def write(snap, path=BOARD_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(path + ".tmp", path)
