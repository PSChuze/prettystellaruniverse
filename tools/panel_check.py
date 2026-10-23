"""Local check for psu/adminport.py, the game's side of an OpenLobby admin panel (psu/olport.py).

Runs the game's real admin commands (psu/admin.py handle) on a real event loop in another thread, the
way game.main does, and drives the panel port over HTTP as the panel would: the description, who is
online (from a stand-in board snapshot), and the commands. The world is empty, so an announcement reaches
nobody and a kick finds nobody; what is checked is that every command goes through admin.handle on the
loop and its answer comes back as the panel's {ok, msg}. Ban and unban, the Players page and the News page
run against a real psu.db and news file in a temp state dir (accounts seeded on the loop).

  python tools/panel_check.py
"""
import asyncio
import datetime
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "server"))
TMP = tempfile.mkdtemp(prefix="psu-panel-")
os.environ.setdefault("PSU_STATE_DIR", os.path.join(TMP, "state"))
os.environ.setdefault("PSU_LOG_DIR", os.path.join(TMP, "logs"))
os.environ["PSU_UPDATE_FILE"] = os.path.join(TMP, "update.json")
os.environ.pop("PSU_BUILD_COMMIT", None)
os.makedirs(os.environ["PSU_STATE_DIR"], exist_ok=True)
os.makedirs(os.environ["PSU_LOG_DIR"], exist_ok=True)
os.environ["PSU_PANEL_TOKEN"] = TOKEN = secrets.token_hex(16)

from psu import admin, adminport  # noqa: E402
from psu import gm, news, social  # noqa: E402

social.STATE_DIR = os.environ["PSU_STATE_DIR"]   # what game.main sets before the world runs
admin.STATE_DIR = os.environ["PSU_STATE_DIR"]    # and admin.start
RAW = bytes(0x28) + "Test".encode("utf-16-le").ljust(0x40, b"\0") + bytes(0x100)

FAILS = []


def check(what, cond):
    print(("ok   " if cond else "FAIL ") + what, flush=True)
    if not cond:
        FAILS.append(what)


SNAP = {"online": {"total": 1, "psu": 1, "aoti": 0,
                   "players": [{"name": "Ethan", "level": 12, "cls": "Hunter", "game": "psu", "uni": 1,
                                "where": "mission", "mission": "Hunters' Qualifying Test"}],
                   "universes": [{"id": 1, "name": "Universe 1", "count": 1}]}}
SEEN = []


def utc(iso):
    return int(datetime.datetime.fromisoformat(iso + "+00:00").timestamp())


EVENTS = [{"key": "halloween-2026", "name": "Halloween Festival", "start": "2026-10-28T00:00Z",
           "end": "2026-11-08T00:00Z", "season": "halloween", "exp": 1.5, "drops": 2,
           "notice": "Pumpkins are dropping all over Gurhal!"},
          {"key": "weekend-exp", "name": "Weekend EXP", "game": "aoti",
           "weekly": {"dow": [5, 6], "from": "00:00", "hours": 24}, "exp": 2},
          {"key": "summer-2026", "name": "Summer Splash", "start": "2026-08-01T00:00Z", "end": "2026-08-15T00:00Z"},
          {"key": "broken"}]


def calendar(call, desc):
    """The host's event file (psu/events.py) and the auto lobby seasons, as the panel's calendar."""
    with open(os.path.join(os.environ["PSU_STATE_DIR"], "events.json"), "w", encoding="utf-8") as f:
        json.dump(EVENTS, f)
    check("it says it has a calendar", desc.get("calendar") is True)
    os.environ["PSU_SEASON"] = "none"
    lo, hi = utc("2026-11-01T00:00:00"), utc("2026-11-15T00:00:00")   # a Sunday .. a Sunday
    st, doc = call(f"/openlobby/calendar.json?from={lo}&to={hi}")
    got = [(e["id"], e["start"], e["end"]) for e in doc.get("events", [])]
    check("the range's runs, in UTC seconds, a weekly event once per weekend day, nothing outside",
          st == 200 and got == [
              ("halloween-2026-%d" % utc("2026-10-28T00:00:00"), utc("2026-10-28T00:00:00"),
               utc("2026-11-08T00:00:00")),
              ("weekend-exp-%d" % utc("2026-11-01T00:00:00"), utc("2026-11-01T00:00:00"),
               utc("2026-11-02T00:00:00")),
              ("weekend-exp-%d" % utc("2026-11-07T00:00:00"), utc("2026-11-07T00:00:00"),
               utc("2026-11-08T00:00:00")),
              ("weekend-exp-%d" % utc("2026-11-08T00:00:00"), utc("2026-11-08T00:00:00"),
               utc("2026-11-09T00:00:00")),
              ("weekend-exp-%d" % utc("2026-11-14T00:00:00"), utc("2026-11-14T00:00:00"),
               utc("2026-11-15T00:00:00"))])
    ev = {e["id"].rsplit("-", 1)[0]: e for e in doc.get("events", [])}
    check("an event's title, kind and what it changes",
          (ev["halloween-2026"]["title"], ev["halloween-2026"]["kind"], ev["halloween-2026"]["about"])
          == ("Halloween Festival", "event", "EXP x1.5. 2 drop rolls. Lobbies dressed for halloween. "
              "Pumpkins are dropping all over Gurhal!"))
    check("a weekly one for one disc says so",
          (ev["weekend-exp"]["kind"], ev["weekend-exp"]["about"])
          == ("weekly event", "Ambition of the Illuminus only. EXP x2."))
    def summer(lo, hi):
        return [e["id"] for e in call(f"/openlobby/calendar.json?from={lo}&to={hi}")[1]["events"]
                if e["id"].startswith("summer-")]
    check("a run that ends at the range's start is left out, one that starts at its end too",
          summer(utc("2026-08-15T00:00:00"), utc("2026-08-16T00:00:00")) == []
          and summer(utc("2026-07-31T00:00:00"), utc("2026-08-01T00:00:00")) == []
          and summer(utc("2026-08-14T23:59:00"), utc("2026-08-15T00:00:00")) != [])
    os.environ["PSU_SEASON"] = "auto"
    st, doc = call(f"/openlobby/calendar.json?from={utc('2026-12-01T00:00:00')}&to={utc('2027-01-01T00:00:00')}")
    seasons = [(e["title"], e["start"], e["end"]) for e in doc["events"] if e["kind"] == "season"]
    check("PSU_SEASON=auto adds the lobby seasons, each run whole (UTC days)",
          seasons == [("Lobby season: christmas", utc("2026-12-12T00:00:00"), utc("2026-12-27T00:00:00")),
                      ("Lobby season: snow", utc("2026-12-27T00:00:00"), utc("2026-12-31T00:00:00")),
                      ("Lobby season: newyear", utc("2026-12-31T00:00:00"), utc("2027-01-15T00:00:00"))])
    st, doc = call(f"/openlobby/calendar.json?from={utc('2027-03-20T00:00:00')}&to={utc('2027-04-01T00:00:00')}")
    check("...Easter computed (March 28 in 2027, a week either side)",
          [(e["title"], e["start"]) for e in doc["events"] if e["kind"] == "season"]
          == [("Lobby season: whiteday", utc("2027-03-07T00:00:00")),
              ("Lobby season: easter", utc("2027-03-21T00:00:00"))])
    os.environ["PSU_SEASON"] = "none"
    check("from and to must be numbers", call("/openlobby/calendar.json?from=x&to=1")[0] == 400)


def main():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    loop_thread = []

    def snapshot():
        loop_thread.append(threading.current_thread() is not threading.main_thread())
        return SNAP

    real = admin.handle

    def spy(req, by):
        SEEN.append((req.get("cmd"), by, threading.current_thread().name))
        return real(req, by)
    admin.handle = spy
    port = adminport.build(loop, snapshot)
    host, n = port.start()
    url = f"http://127.0.0.1:{n}"

    def call(path, body=None, token=TOKEN):
        req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Devtool-Token": token, "X-Devtool-Actor": "admin"},
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {}

    check("nothing answers without the token", call("/openlobby/module.json", token="x")[0] == 403)
    st, desc = call("/openlobby/module.json")
    check("it describes itself as psu with its commands",
          desc.get("key") == "psu"
          and [a["id"] for a in desc["actions"]] == ["announce", "kick", "mute", "unmute", "ban", "unban", "season"])
    check("announce and kick are the panel's", [a["kind"] for a in desc["actions"]][:2] == ["announce", "kick"])
    st, live = call("/openlobby/status.json")
    check("who is online, from the board snapshot taken on the loop",
          live.get("online") == 1 and live["players"][0]["name"] == "Ethan"
          and live["players"][0]["where"] == "Hunters' Qualifying Test" and loop_thread == [True])
    check("universes are the rooms", live["rooms"] == [{"name": "Universe 1", "players": 1}])
    check("the update state is unknown until setup has looked for a release for this build",
          live["update"]["update"] == "unknown" and live["update"]["how"].startswith("Run PSU Server Setup"))
    os.environ["PSU_BUILD_COMMIT"] = "abc123"
    with open(os.environ["PSU_UPDATE_FILE"], "w", encoding="utf-8") as f:
        json.dump({"running": {"commit": "abc123"}, "latest": {"commit": "def456", "date": "2001-01-02"},
                   "behind": 2, "changes": [{"commit": "def456", "date": "2001-01-02", "subject": "x"}] * 60,
                   "update": "available"}, f)
    up = call("/openlobby/status.json")[1]["update"]
    check("setup's look at the releases is reported for the build it was made for, at most 50 changes",
          up["update"] == "available" and up["behind"] == 2 and up["latest"]["commit"] == "def456"
          and len(up["changes"]) == 50 and "why" not in up)
    os.environ["PSU_BUILD_COMMIT"] = "other"
    check("a look made for another build is unknown",
          call("/openlobby/status.json")[1]["update"]["update"] == "unknown")
    st, out = call("/openlobby/actions/announce", {"text": "Maintenance at 10:00 UTC"})
    check("an announcement goes through admin.handle on the loop, audited as the panel user",
          out == {"ok": True, "msg": "Sent to 0 players."}
          and SEEN[-1][:2] == ("announce", "panel:admin") and SEEN[-1][2] != "MainThread")
    st, out = call("/openlobby/actions/kick", {"name": "Nobody"})
    check("a kick of someone unknown says so", out == {"ok": False, "msg": "no such player: Nobody"})
    st, out = call("/openlobby/actions/kick", {"polid": "ABCD1234", "handles": "Karen,Leo"})
    check("an account's Kick tries each handle as a character",
          out.get("ok") is False and [c for c, _b, _t in SEEN[-2:]] == ["kick", "kick"])
    check("mute needs a number of minutes",
          call("/openlobby/actions/mute", {"name": "Ethan", "minutes": "soon"})[1]
          == {"ok": False, "msg": "Minutes is a number."})
    load_threads = []
    real_load = adminport.events.load

    def load_spy():
        load_threads.append(threading.current_thread().name)
        return real_load()
    adminport.events.load = load_spy
    calendar(call, desc)
    adminport.events.load = real_load
    check("the calendar reads the events file on the loop (its cache is the loop's)",
          len(set(load_threads)) == 1 and load_threads[0] == SEEN[0][2] != "MainThread")
    check("it says it has a player view", desc.get("player") is True)

    def page(path, form=None):
        req = urllib.request.Request(url + path, data=None if form is None else form.encode(),
                                     headers={"X-Devtool-Token": TOKEN, "X-Devtool-Actor": "admin",
                                              "Content-Type": "application/x-www-form-urlencoded"},
                                     method="GET" if form is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, ""

    # --- ban / unban, by account name ---------------------------------------------------------------
    def on_loop(fn):
        async def run():
            return fn()
        return asyncio.run_coroutine_threadsafe(run(), loop).result(10)

    store_thread = []

    def seed():
        store = gm._db()          # the connection is the loop's, as in game.main
        store_thread.append(threading.current_thread().name)
        t = time.time()
        for user, gid, created in (("Alice", 0x1001, t - 86400), ("bob", 0x1002, t - 3600), ("a_b", 0x1003, t)):
            store.c.execute("INSERT INTO accounts (username, gid, salt, pw_hash, created, last_login) "
                            "VALUES (?, ?, x'00', x'00', ?, ?)", (user, gid, created, created))
        store.save_character(0x1001, 0, "Ethan", RAW)
        store.save_char_state(0x1001, 0, {"cls": 1, "level": 23, "meseta": 123456, "playtime": 3 * 3600 + 120,
                                          "game": "aoti"})
        store.save_character(0x1002, 0, "<b>Hyuga", RAW)
        store.save_char_state(0x1002, 0, {"cls": 0, "level": 5, "meseta": 900, "playtime": 59})
    on_loop(seed)

    def gm_actions(cmd):
        try:
            with open(os.path.join(os.environ["PSU_LOG_DIR"], "events.jsonl"), encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
        except OSError:
            return []
        return [r for r in rows if r.get("ev") == "gm_action" and r.get("cmd") == cmd]

    check("ban and unban are offered, ban as a danger action",
          [a["id"] for a in desc["actions"]][4:6] == ["ban", "unban"]
          and [a["danger"] for a in desc["actions"] if a["id"] == "ban"] == [True])
    check("a ban of an unknown account says so",
          call("/openlobby/actions/ban", {"account": "Nobody"})[1] == {"ok": False, "msg": "no such account: Nobody"})
    check("a ban needs an account name",
          call("/openlobby/actions/ban", {"account": " "})[1]["ok"] is False)
    check("a ban's hours are a number",
          call("/openlobby/actions/ban", {"account": "Alice", "hours": "a week"})[1]
          == {"ok": False, "msg": "Hours is a number."})
    check("negative or endless hours are refused",
          call("/openlobby/actions/ban", {"account": "Alice", "hours": "-5"})[1]["ok"] is False
          and call("/openlobby/actions/ban", {"account": "Alice", "hours": "1e9"})[1]["ok"] is False
          and call("/openlobby/actions/ban", {"account": "Alice", "hours": "nan"})[1]["ok"] is False
          and on_loop(lambda: gm._db().banned(0x1001)) is None)
    st, out = call("/openlobby/actions/ban", {"account": "alice", "hours": "2", "reason": "spam  bots"})
    ban = on_loop(lambda: gm._db().banned(0x1001))
    check("a ban by a case-insensitive account name bans that account for the hours given",
          out["ok"] and out["msg"].startswith("Alice is banned until ")
          and ban is not None and abs(ban[0] - (time.time() + 7200)) < 30 and ban[1] == "spam bots")
    check("the ban ran through admin.handle on the loop and is audited as the panel user",
          SEEN[-1][:2] == ("ban", "panel:admin") and SEEN[-1][2] != "MainThread"
          and gm_actions("ban")[-1]["by"] == "panel:admin" and gm_actions("ban")[-1]["account"] == "Alice")
    check("0 hours is permanent (10 years)",
          call("/openlobby/actions/ban", {"account": "bob", "hours": "0"})[1]
          == {"ok": True, "msg": "bob is banned permanently."}
          and on_loop(lambda: gm._db().banned(0x1002))[0] > time.time() + 9 * 365 * 86400)
    check("unban lifts it",
          call("/openlobby/actions/unban", {"account": "Alice"})[1] == {"ok": True, "msg": "Alice is unbanned."}
          and on_loop(lambda: gm._db().banned(0x1001)) is None and gm_actions("unban")[-1]["was"] is True)
    check("unban of an account that is not banned says so",
          call("/openlobby/actions/unban", {"account": "Alice"})[1] == {"ok": True, "msg": "Alice was not banned."})

    # --- the account window's player view, by linked PSU login (GID as 8 hex digits) ------------------
    def player(links, polid="ABCD1234", handles="Ethan,Alice"):
        q = urllib.parse.urlencode({"polid": polid, "handles": handles, "members": "7",
                                    "links": json.dumps(links)})
        return call("/openlobby/player.json?" + q)
    on_loop(lambda: gm._db().c.execute("INSERT INTO accounts (username, gid, salt, pw_hash, created, last_login) "
                                       "VALUES ('empty', 4100, x'00', x'00', 0, 0)"))
    st, got = player({"psu": [{"key": "00001001", "name": "Alice"}]})
    chars = got.get("characters") or []
    check("a linked login's characters: name, class, level, disc, offline, its login and play time",
          st == 200 and [c["name"] for c in chars] == ["Ethan"] and chars[0]["detail"] == "Ranger, level 23, AotI"
          and chars[0]["online"] is False
          and {"label": "PSU login", "value": "Alice"} in chars[0]["facts"]
          and {"label": "Play time", "value": "3 h 02 min"} in chars[0]["facts"])
    blob = json.dumps(got).lower()
    check("nothing sensitive in it: no GID, hash, salt, password, token or address",
          all(w not in blob for w in ("1001", "4097", "gid", "hash", "salt", "password", "token", "127.0.0.1",
                                      "session")))
    st, got = player({"psu": [{"key": "00001002", "name": "bob"}, {"key": "00001002", "name": "bob"},
                              {"key": "00001001", "name": "Alice"}]})
    check("several links, a repeat once, in the order given",
          [c["name"] for c in got.get("characters") or []] == ["<b>Hyuga", "Ethan"])
    st, got = player({"psu": [{"key": "00001004", "name": "empty"}]})
    check("a linked login with no characters says so",
          got == {"characters": [], "note": "The linked PSU login has no characters yet."})
    st, got = player({"psu": [{"key": "0badf00d", "name": "Ghost"}, {"key": "zz; DROP", "name": "x"}]})
    check("a key the game does not know (or that is not hex) is nobody", st == 200 and got == {})
    st, got = player({})
    check("no links is nobody, whatever the handles say (Ethan, Alice)", st == 200 and got == {})
    st, got = player({"feega": [{"key": "00001001", "name": "Alice"}]})
    check("another game's links are not PSU logins", st == 200 and got == {})
    check("a links parameter that is not JSON is no links",
          call("/openlobby/player.json?polid=X&handles=Ethan&links=%7Bnot")[1] == {})

    # --- Players tool ---------------------------------------------------------------------------------
    tools = {t["id"]: t["path"] for t in desc.get("tools") or []}
    check("the Players, News and Events tools are listed",
          tools == {"players": "players", "news": "news", "events": "events"})
    st, html = page("/players")
    check("the Players page with nobody online says so", st == 200 and "Nobody is online" in html
          and "<script" not in html.lower())
    before = on_loop(lambda: gm._db().c.execute("SELECT state FROM characters ORDER BY gid").fetchall())
    st, html = page("/players", "q=ali")
    check("a search by login lists the account with its characters: class, level, meseta, play time, disc",
          st == 200 and ">Alice<" in html and ">Ethan<" in html and ">Ranger<" in html and ">23<" in html
          and "123,456" in html and "3 h 02 min" in html and ">AotI<" in html and "Hyuga" not in html)
    st, html = page("/players", "q=hyuga")
    check("a search by character name finds its account, and names are escaped",
          ">bob<" in html and "&lt;b&gt;Hyuga" in html and "<b>Hyuga" not in html)
    st, html = page("/players?q=ETHAN")
    check("the search also comes as ?q= and ignores case", ">Alice<" in html)
    check("% and _ in a search are not wildcards, and _ is found as itself",
          ">Alice<" not in page("/players", "q=%25")[1] and ">Alice<" not in page("/players", "q=_")[1]
          and ">a_b<" in page("/players", "q=a_")[1] and ">bob<" not in page("/players", "q=a_")[1])
    # --- the panel's player picker ----------------------------------------------------------------------
    check("it says it has a player picker, and the character fields use it",
          desc.get("players") is True
          and all(f["type"] == "player" for a in desc["actions"] for f in a.get("fields") or []
                  if f["id"] == "name"))
    st, got = call("/openlobby/players.json?q=eth&limit=5")
    check("a pick by character name gives the name as the id, with its login, level and class",
          st == 200 and got == {"players": [{"id": "Ethan", "label": "Ethan", "detail": "Alice, level 23 Ranger",
                                             "online": False}]})
    st, got = call("/openlobby/players.json?q=bob")
    check("a pick by login gives that login's characters", [p["id"] for p in got["players"]] == ["<b>Hyuga"])
    check("the picker's limit is 1 to 50", call("/openlobby/players.json?limit=0")[0] == 400)
    check("the Players page writes nothing and reads the store on the loop",
          on_loop(lambda: gm._db().c.execute("SELECT state FROM characters ORDER BY gid").fetchall()) == before
          and set(store_thread) == {store_thread[0]} and store_thread[0] != "MainThread")

    # --- News tool -----------------------------------------------------------------------------------
    path = news.path(os.environ["PSU_STATE_DIR"])
    with open(path, "w", encoding="utf-8") as f:
        f.write("Old notice.\n")
    st, html = page("/news")
    was = re.search(r'name=was value="([0-9a-f]+)"', html)
    check("the News page shows the file in an editor", st == 200 and was is not None
          and "Old notice." in html and "<textarea" in html and "<script" not in html.lower())
    n_news = len([s for s in SEEN if s[0] == "news"])
    st, html = page("/news", urllib.parse.urlencode({"was": "0" * 16, "text": "Lost edit"}))
    check("a save over a newer file is refused and the file kept",
          "changed since this page was opened" in html and open(path, encoding="utf-8").read() == "Old notice.\n")
    text = "Maintenance Sunday 18:00 UTC.\r\n---\r\n# staff only\r\nAotI players are welcome <3\r\n"
    st, html = page("/news", urllib.parse.urlencode({"was": was.group(1), "text": text}))
    saved = open(path, "rb").read()
    check("a save writes the file with LF line ends", st == 200 and "Saved." in html
          and saved == b"Maintenance Sunday 18:00 UTC.\n---\n# staff only\nAotI players are welcome <3\n")
    check("the save runs the server's news reload on the loop as the panel user",
          len([s for s in SEEN if s[0] == "news"]) == n_news + 1 and SEEN[-1][:2] == ("news", "panel:admin")
          and SEEN[-1][2] != "MainThread"
          and news.file_notices(os.environ["PSU_STATE_DIR"]) == ["Maintenance Sunday 18:00 UTC.",
                                                                  "AotI players are welcome <3"])
    edit = gm_actions("news_edit")
    check("the edit is audited with the panel user, the file and both digests",
          edit and edit[-1]["by"] == "panel:admin" and edit[-1]["via"] == "panel" and edit[-1]["target"] == path
          and edit[-1]["was"] == was.group(1) and edit[-1]["now"] != edit[-1]["was"])
    check("no temporary file is left beside it",
          sorted(os.listdir(os.path.dirname(path))) == sorted(x for x in os.listdir(os.path.dirname(path))
                                                                if not x.endswith(".tmp")))
    st, html = page("/news")
    check("the page shows the new notices, escaped", "AotI players are welcome &lt;3" in html)
    st, html = page("/news", urllib.parse.urlencode({"was": re.search(r'name=was value="([0-9a-f]+)"', html).group(1),
                                                     "text": "A </textarea><b>bold</b> notice"}))
    check("the editor holds the file escaped", html.count("</textarea>") == 1 and "<b>bold" not in html)
    was2 = re.search(r'name=was value="([0-9a-f]+)"', html).group(1)
    saved = open(path, "rb").read()
    st, html = page("/news", urllib.parse.urlencode({"was": was2, "text": "x" * (70 * 1024)}))
    check("a news file over 64 KB is refused", "limited to 64 KB" in html and open(path, "rb").read() == saved)

    # --- Events tool and season preview --------------------------------------------------------------
    from psu import events, season
    epath = events.path()
    if os.path.exists(epath):
        os.unlink(epath)
    events._cache.clear()
    st, html = page("/events")
    ewas = re.search(r'name=was value="([0-9a-f]+)"', html)
    check("the Events page with no file shows an empty editor and the season",
          st == 200 and ewas is not None and "Nothing running" in html and "Lobbies now" in html
          and "<script" not in html.lower())
    good = json.dumps([{"key": "xmas", "name": "Christmas <Fair>", "start": "2099-12-12T00:00Z",
                        "end": "2099-12-27T00:00Z", "season": "christmas", "exp": 1.5}], indent=1)
    bad = json.dumps([{"key": "a", "name": "A", "start": "2099-01-02T00:00Z", "end": "2099-01-01T00:00Z"},
                      {"key": "b", "name": "B", "weekly": {"dow": [9], "hours": 5}}])
    st, html = page("/events", urllib.parse.urlencode({"was": ewas.group(1), "text": bad}))
    check("a file with a bad entry is refused, naming each problem",
          "Nothing was saved" in html and "entry 1 (a): end is not after start" in html
          and "entry 2 (b)" in html and not os.path.exists(epath))
    st, html = page("/events", urllib.parse.urlencode({"was": ewas.group(1), "text": "{not json"}))
    check("text that is not JSON is refused", "not JSON" in html and not os.path.exists(epath))
    dup = json.dumps([json.loads(good)[0], json.loads(good)[0]])
    check("a key used twice is refused",
          "used twice" in page("/events", urllib.parse.urlencode({"was": ewas.group(1), "text": dup}))[1])
    n_ev = len([x for x in SEEN if x[0] == "events"])
    st, html = page("/events", urllib.parse.urlencode({"was": ewas.group(1), "text": good}))
    check("a good file is saved and the server re-reads it on the loop as the panel user",
          "Saved." in html and open(epath, encoding="utf-8").read() == good + "\n"
          and len([x for x in SEEN if x[0] == "events"]) == n_ev + 1 and SEEN[-1][:2] == ("events", "panel:admin")
          and SEEN[-1][2] != "MainThread")
    edit = gm_actions("events_edit")
    check("the events edit is audited", edit and edit[-1]["by"] == "panel:admin" and edit[-1]["target"] == epath)
    st, html = page("/events", urllib.parse.urlencode({"was": ewas.group(1), "text": "[]"}))
    check("a save over a newer events file is refused",
          "changed since this page was opened" in html and open(epath, encoding="utf-8").read() == good + "\n")
    st, cal = call("/openlobby/calendar.json?from=%d&to=%d" % (utc("2099-12-01T00:00"), utc("2100-01-01T00:00")))
    check("the saved event is on the panel's calendar",
          [e["title"] for e in cal.get("events") or []] == ["Christmas <Fair>"])
    os.unlink(epath)
    events._cache.clear()

    st, out = call("/openlobby/actions/season", {"name": "Nobody", "season": "christmas"})
    check("a season preview for someone unknown says so", out == {"ok": False, "msg": "no such player: Nobody"})
    gid = on_loop(lambda: gm.find_player("Alice")[0])
    st, out = call("/openlobby/actions/season", {"name": "Alice", "season": "xmas"})
    check("an unknown season is refused with the list", out["ok"] is False and "christmas" in out["msg"])
    st, out = call("/openlobby/actions/season", {"name": "Alice", "season": "winter"})
    check("a preview (alias) is set for that character only, nothing else changes",
          out["ok"] and "snow" in out["msg"] and season.PREVIEW == {gid: "snow"}
          and season.current(gid=gid) == "snow" and season.current() is None and season.current(gid=gid + 1) is None)
    check("the preview is audited", gm_actions("season_preview")[-1]["season"] == "snow")
    st, out = call("/openlobby/actions/season", {"name": "Alice", "season": "none"})
    check("none previews no season even while the server runs one",
          out["ok"] and season.PREVIEW == {gid: None} and season.current(gid=gid) is None)
    st, html = page("/events")
    check("the Events page lists the preview", "Alice: none" in html)
    st, out = call("/openlobby/actions/season", {"name": "Alice", "season": "off"})
    check("off ends the preview", out["ok"] and season.PREVIEW == {})
    st, html = page("/events")
    check("the page no longer lists it", "Alice: none" not in html)

    # --- help desk -----------------------------------------------------------------------------------
    from psu import helpdesk
    check("the description offers a help desk", desc.get("help") is True)
    check("no requests yet", call("/openlobby/help.json")[1] == {"threads": []})
    ok1, line = on_loop(lambda: helpdesk.ask(gid, "Ethan", "  My   Photon Arts are gone after a crash  "))
    ok2, line2 = on_loop(lambda: helpdesk.ask(gid, "Ethan", "Still missing <b>today</b>"))
    check("!gm opens a request and a second message joins it", ok1 and ok2 and "#1" in line and "#1" in line2)
    st, lst = call("/openlobby/help.json?open=1")
    t = (lst.get("threads") or [{}])[0]
    check("the Inbox lists it, waiting, with both messages",
          len(lst["threads"]) == 1 and t["id"] == "1" and t["who"] == "Ethan" and t["waiting"] and not t["closed"]
          and t["count"] == 2 and t["subject"] == "My Photon Arts are gone after a crash")
    check("each request names its player as the picker does, and its newest message's time",
          t.get("player") == "Ethan" and isinstance(t.get("last"), int) and t["last"] >= t["at"] - 1)
    st, th = call("/openlobby/help/thread.json?id=1")
    check("the thread holds the player's messages as sent, offline",
          [m["from"] for m in th["messages"]] == ["player", "player"]
          and th["messages"][1]["text"] == "Still missing <b>today</b>" and th["who"] == {"name": "Ethan", "online": False})
    check("an unknown thread is a 404", call("/openlobby/help/thread.json?id=99")[0] == 404)
    st, out = call("/openlobby/help/reply", {"id": "1", "text": "Restored them, sorry!", "close": False})
    rows = on_loop(lambda: gm._db().c.execute("SELECT sender, delivered, by FROM help_messages ORDER BY id").fetchall())
    check("a staff answer is stored for the next lobby load, by the panel user",
          out["ok"] and "next enter a lobby" in out["msg"] and rows[-1] == ("staff", 0, "panel:admin"))
    check("the request is no longer waiting", not call("/openlobby/help.json")[1]["threads"][0]["waiting"])
    check("!gm alone shows the latest answer",
          "Restored them, sorry!" in on_loop(lambda: helpdesk.ask(gid, "Ethan", "")[1]))
    check("an empty answer is refused", call("/openlobby/help/reply", {"id": "1", "text": " "})[1]["ok"] is False)
    st, out = call("/openlobby/help/close", {"id": "1"})
    check("close closes it, and it leaves the open list",
          out == {"ok": True, "msg": "Closed."} and call("/openlobby/help.json?open=1")[1] == {"threads": []}
          and call("/openlobby/help.json")[1]["threads"][0]["closed"] is True)
    _ok, line = on_loop(lambda: helpdesk.ask(gid, "Ethan", "One more thing"))
    check("a later !gm opens a new request", "#2" in line)
    spam = [on_loop(lambda: helpdesk.ask(gid, "Ethan", "again"))[0] for _ in range(5)]
    check("a player is held to %d messages per %d minutes" % (helpdesk.MAX_PER_WINDOW, helpdesk.WINDOW // 60),
          spam == [True, True, False, False, False])   # 3 already sent in the window

    port.stop()
    loop.call_soon_threadsafe(loop.stop)
    print("panel_check: " + ("PASS" if not FAILS else f"{len(FAILS)} FAILED"), flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
