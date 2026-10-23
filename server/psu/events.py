"""Server events: the host's calendar of limited-time events, the way Sega ran PSU's network service (seasonal
lobbies, EXP / drop campaigns, featured missions), shown in game and on the public board (psu/board.py).

  $PSU_EVENTS_FILE (default $PSU_STATE_DIR/events.json), re-read whenever it changes. A JSON list of events:

    {"key": "halloween-2026", "name": "Halloween Festival",
     "start": "2026-10-28T00:00Z", "end": "2026-11-08T00:00Z",      one window (UTC), or
     "weekly": {"dow": [5, 6], "from": "00:00", "hours": 24},       every week: start days (0 = Monday), UTC
     "game": "psu" | "aoti",                                         optional: one disc only (default both)
     "season": "halloween",                                          optional: the lobby season while it runs
     "exp": 1.5, "meseta": 2, "drops": 2,                            optional: EXP and meseta multipliers, and
                                                                     rolls per kill (2 = a second roll when the
                                                                     first gave nothing)
     "event_drop": 20,                                               optional: the season's event items at 1 in N
     "missions": [110000, 300100],                                   optional: featured missions; the board ranks
                                                                     the fastest clears made while it runs
     "notice": "Pumpkins are dropping all over Gurhal!"}             optional: shown with it

  An entry that does not parse is skipped and reported once on stderr; the rest still run. `tools/announce.py events`
re-reads the file and lists what it will run and what it skipped.

Where it shows: login notice box and news ticker rotation (news.notices adds a line per running event and per
event starting within NOTICE_DAYS), a ticker to everyone online when one starts and ends (watch()), the season
(season.current), kill rewards (game.py: exp_mult, meseta_mult, drop_rolls), and the board (board.py).
"""
import asyncio
import datetime
import json
import os
import sys
import time

from psu.observe import LOG_DIR

NOTICE_DAYS = 7
HORIZON_DAYS = 30                   # how far ahead the board lists events
WATCH_SECS = float(os.environ.get("PSU_EVENTS_WATCH_SECS", "30"))

_cache = {}                         # path -> (mtime, events)
_warned = set()
_problems = []                      # what the last read of the file skipped, for `announce.py events`


def path():
    state = os.environ.get("PSU_STATE_DIR") or os.path.join(LOG_DIR, "..", "state")
    return os.environ.get("PSU_EVENTS_FILE") or os.path.join(state, "events.json")


def _when(s):
    """'2026-10-28T00:00Z' (or with seconds / +00:00) -> epoch seconds."""
    return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def _check(e):
    if not isinstance(e, dict) or not e.get("key") or not e.get("name"):
        raise ValueError("needs key and name")
    if "weekly" in e:
        w = e["weekly"]
        h, m = (int(x) for x in str(w.get("from", "00:00")).split(":"))
        if not (0 <= h < 24 and 0 <= m < 60 and 0 < float(w["hours"]) <= 168 and w.get("dow")):
            raise ValueError("weekly: dow, from HH:MM, hours 1..168")
        if any(not 0 <= int(d) <= 6 for d in w["dow"]):
            raise ValueError("weekly dow is 0 (Monday) .. 6")
    elif _when(e["end"]) <= _when(e["start"]):
        raise ValueError("end is not after start")
    if e.get("game") not in (None, "psu", "aoti"):
        raise ValueError("game is psu or aoti")
    if e.get("season") is not None:
        from psu import season
        if season.name_of(e["season"]) is None:
            raise ValueError(f"unknown season {e['season']!r}")
    for k in ("exp", "meseta"):
        if not 0 < float(e.get(k, 1)) <= 10:
            raise ValueError(f"{k} is a multiplier above 0, at most 10")
    if not 1 <= int(e.get("drops", 1)) <= 5:
        raise ValueError("drops is 1..5")
    [int(q) for q in e.get("missions", [])]
    return e


def load():
    """The events in the file (cached by mtime); [] without one."""
    p = path()
    try:
        mtime = os.stat(p).st_mtime
    except OSError:
        _cache.pop(p, None)
        return []
    hit = _cache.get(p)
    if hit is None or hit[0] != mtime:
        good, _problems[:] = [], []
        try:
            with open(p, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, list):
                raise ValueError("the file is not a JSON list")
        except (OSError, ValueError, UnicodeDecodeError) as e:
            raw = []
            _warn(f"{p}: {e}")
        for e in raw:
            try:
                good.append(_check(e))
            except (KeyError, TypeError, ValueError) as err:
                _warn(f"{p}: event {e.get('key') if isinstance(e, dict) else e!r}: {err}")
        hit = (mtime, good)
        _cache[p] = hit
    return hit[1]


def _warn(msg):
    _problems.append(msg)
    if msg not in _warned:
        _warned.add(msg)
        print(f"events: {msg}", file=sys.stderr, flush=True)


def check():
    """(the events file, its upcoming runs, the entries it skipped and why), re-read now."""
    _cache.clear()
    load()
    return path(), board_rows(), list(_problems)


def validate(text):
    """A whole events file's text -> what is wrong with it ([] = the server would run every entry): not JSON,
    not a list, an entry it would skip (and why), a key used twice. For the panel's editor, which saves
    nothing that has a problem."""
    try:
        raw = json.loads(text) if text.strip() else []
    except ValueError as e:
        return [f"not JSON: {e}"]
    if not isinstance(raw, list):
        return ["the file is a JSON list of events: [ {...}, {...} ]"]
    out, keys = [], set()
    for n, e in enumerate(raw, 1):
        what = f"entry {n}" + (f" ({e.get('key')})" if isinstance(e, dict) and e.get("key") else "")
        try:
            _check(e)
        except (KeyError, TypeError, ValueError) as err:
            out.append(f"{what}: {err}")
            continue
        if e["key"] in keys:
            out.append(f"{what}: the key is used twice")
        keys.add(e["key"])
    return out


def windows(e, since, until):
    """The (start, end) runs of e that overlap [since, until)."""
    if "weekly" not in e:
        s, t = _when(e["start"]), _when(e["end"])
        return [(s, t)] if s < until and t > since else []
    w = e["weekly"]
    h, m = (int(x) for x in str(w.get("from", "00:00")).split(":"))
    length = float(w["hours"]) * 3600
    day = datetime.datetime.fromtimestamp(since - length, datetime.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    out = []
    while day.timestamp() < until:
        if day.weekday() in {int(d) for d in w["dow"]}:
            s = day.timestamp() + h * 3600 + m * 60
            if s < until and s + length > since:
                out.append((s, s + length))
        day += datetime.timedelta(days=1)
    return out


def running(now=None, game=None):
    """[(event, start, end)] running at `now`, for `game` when given (events of the other disc left out)."""
    now = time.time() if now is None else now
    return [(e, s, t) for e in load() if game is None or e.get("game") in (None, game)
            for s, t in windows(e, now, now + 1)]


def upcoming(now=None, days=HORIZON_DAYS):
    """[(event, start, end)] running now or starting within `days`, soonest first."""
    now = time.time() if now is None else now
    out = [(e, s, t) for e in load() for s, t in windows(e, now, now + days * 86400)]
    return sorted(out, key=lambda r: (r[1], r[0]["key"]))


def _best(game, key, default, combine):
    v = default
    for e, _, _ in running(game=game):
        if key in e:
            v = combine(v, e[key])
    return v


def exp_mult(game=None):
    return _best(game, "exp", 1.0, lambda a, b: max(a, float(b)))


def meseta_mult(game=None):
    return _best(game, "meseta", 1.0, lambda a, b: max(a, float(b)))


def drop_rolls(game=None):
    return _best(game, "drops", 1, lambda a, b: max(a, int(b)))


def event_drop_rate(game=None):
    """The 1-in-N rate for season event items from a running event, or None (PSU_SEASON_DROP then)."""
    return _best(game, "event_drop", None, lambda a, b: int(b) if a is None else min(a, int(b)))


def season(now=None):
    """The season a running event puts in the lobbies, or None."""
    for e, _, _ in running(now):
        if e.get("season"):
            return e["season"]
    return None


def stamp(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%b %d %H:%M UTC").replace(" 0", " ")


def notices(now=None):
    """News lines: one per running event, one per event starting within NOTICE_DAYS."""
    now = time.time() if now is None else now
    out = []
    for e, s, t in upcoming(now, NOTICE_DAYS):
        if s <= now:
            line = f"{e['name']} is on until {stamp(t)}."
        else:
            line = f"{e['name']} starts {stamp(s)}."
        if e.get("notice"):
            line += " " + e["notice"]
        out.append(line)
    return out


def board_rows(now=None):
    """What psu/board.py lists: running and upcoming events with their effects, each event's next run once."""
    now = time.time() if now is None else now
    rows, keys = [], set()
    for e, s, t in upcoming(now):
        if e["key"] in keys:
            continue
        keys.add(e["key"])
        rows.append({"key": e["key"], "name": e["name"], "start": int(s), "end": int(t), "running": s <= now,
                     "game": e.get("game"), "season": e.get("season"), "exp": e.get("exp"), "meseta": e.get("meseta"),
                     "drops": e.get("drops"), "missions": [int(q) for q in e.get("missions", [])],
                     "notice": e.get("notice")})
    return rows


async def watch(broadcast, now=time.time):
    """A ticker to everyone online when an event starts or ends (broadcast(text)). Runs from game.py main; an
    event already running when the service starts is not announced again."""
    seen = {(e["key"], s) for e, s, _ in running(now())}
    while True:
        await asyncio.sleep(WATCH_SECS)
        cur = {(e["key"], s): (e, t) for e, s, t in running(now())}
        for k, (e, t) in cur.items():
            if k not in seen:
                broadcast(f"{e['name']} has begun! It runs until {stamp(t)}. " + (e.get("notice") or ""))
        for k in seen - set(cur):
            name = next((e["name"] for e in load() if e["key"] == k[0]), k[0])
            broadcast(f"{name} has ended. Thank you for playing!")
        seen = set(cur)
