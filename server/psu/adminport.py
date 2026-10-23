"""This server in an OpenLobby admin panel (the panel's admin protocol, psu/olport.py).

Off unless PSU_PANEL_PORT is set; the loopback admin channel (psu/admin.py) is not affected.

  PSU_PANEL_PORT      the HTTP port, e.g. 22191
  PSU_PANEL_BIND      default 127.0.0.1; a panel in another container reaches a host-networked server
                      through the Docker gateway, so such a host binds 0.0.0.0 (a token is then required)
  PSU_PANEL_TOKEN     the token the panel sends (POL_ADMIN_GAME_PSU_TOKEN on its side)
  PSU_PANEL_CONTAINERS  the containers its server check covers (comma list), default this compose
                      project's ("psu")

What the panel gets: who is online, where and in which universe (the board's snapshot, psu/board.py),
and the admin commands of psu/admin.py as panel actions: an announcement (the panel's
maintenance warning), kick, mute, unmute, ban and unban (by account name). Each runs admin.handle on
the game's event loop, so it touches the world exactly as tools/announce.py does, and is audited as
"panel:<user>". Its calendar lists the host's events (psu/events.py, every run in the range)
and, with PSU_SEASON=auto, the lobby seasons (psu/season.py CALENDAR). Its tool pages:

  Players    accounts by login or character name, their characters (class, level, meseta, play
             time, disc) and who is online now. Read-only.
  News       the news file (psu/news.py, $PSU_NEWS_FILE) to read and edit. A save replaces the file
             in one rename, is refused when the file changed since the page was opened, then runs
             the "news" command so the server re-reads it, and is audited as gm_action "news_edit"
             by panel:<user>.
  Events     the event calendar (psu/events.py, $PSU_EVENTS_FILE) to read and edit, the same way: a
             save is refused when the file changed since the page was opened or when any entry would
             be skipped (events.validate), then runs the "events" command and is audited as
             gm_action "events_edit". It also shows the lobby season now and what is staged.

Its "Season preview" action shows one character a season on their next lobby load (gm.season_preview),
for checking a season's archives live without dressing everyone's lobbies.

Its help desk (port.help_desk) is psu/helpdesk.py: players' "!gm" requests in the panel's Inbox, the staff's
answers shown in game (at once, or on the player's next lobby load), audited as panel:<user>.

Its player view (@port.player) answers the account window from the PSU logins the member has linked on
the OpenLobby website: each is kept by key, the GID as 8 hex digits (login.link_verify), and the panel
sends them as links["psu"]. Each known GID gives its characters (class, level, disc, online), with
the login, last login and any ban as facts. Handles and PlayOnline IDs are never matched: the game
stores neither.

The game's database connection, its world and the events file cache belong to the event loop:
everything here that reads or writes them runs there (_on_loop), and only the HTML is built in the
port's thread.
"""
import asyncio
import hashlib
import json
import os
import threading
import time
import urllib.parse

from psu import admin, board, events, gm, helpdesk, news, olpage, olport, season
from psu.world import WORLD

PORT = int(os.environ.get("PSU_PANEL_PORT", "0") or 0)
BIND = os.environ.get("PSU_PANEL_BIND", "127.0.0.1")
TOKEN = os.environ.get("PSU_PANEL_TOKEN", "")
CONTAINERS = [c.strip() for c in os.environ.get(
    "PSU_PANEL_CONTAINERS", "psu-game-1,psu-login-1,psu-patch-1,psu-ssl3-1").split(",") if c.strip()]
WAIT = 10.0
SEARCH_MAX = 50                     # accounts on one Players page
NEWS_MAX = 64 * 1024                # bytes a news save may write
_news_lock = threading.Lock()       # one news save at a time (the port serves each request in a thread)
EVENTS_MAX = 256 * 1024             # bytes an events save may write
_events_lock = threading.Lock()


def _on_loop(loop, fn):
    """Run fn() on the game's event loop and wait for it (the world is the loop's alone)."""
    async def run():
        return fn()
    return asyncio.run_coroutine_threadsafe(run(), loop).result(WAIT)


DISC = {"psu": "Phantasy Star Universe only", "aoti": "Ambition of the Illuminus only"}


def _effects(e):
    """What an event changes, as the panel's one-line "about"."""
    out = [DISC[e["game"]] + "." if e.get("game") in DISC else ""]
    for k, label in (("exp", "EXP"), ("meseta", "meseta")):
        if e.get(k) not in (None, 1, 1.0):
            out.append("%s x%g." % (label, float(e[k])))
    if int(e.get("drops") or 1) > 1:
        out.append("%d drop rolls." % int(e["drops"]))
    if e.get("season"):
        out.append("Lobbies dressed for %s." % season.name_of(e["season"]))
    if e.get("event_drop"):
        out.append("Event items 1 in %d." % int(e["event_drop"]))
    if e.get("missions"):
        n = len(e["missions"])
        out.append("%d featured mission%s." % (n, "" if n == 1 else "s"))
    if e.get("notice"):
        out.append(str(e["notice"]))
    return " ".join(x for x in out if x)


def update_state():
    """The panel's "update" object: PSU Server Setup's last look at the releases, for the build this runs."""
    running = {"commit": os.environ.get("PSU_BUILD_COMMIT", ""), "built_at": os.environ.get("PSU_BUILT_AT", "")}
    try:
        with open(os.environ.get("PSU_UPDATE_FILE", "/state/update.json"), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict) or (doc.get("running") or {}).get("commit") != running["commit"]:
        doc = {"update": "unknown", "why": "PSU Server Setup has not looked for a release for this build yet."}
    out = {"running": running, "latest": doc.get("latest") or {"commit": "", "date": ""},
           "behind": int(doc.get("behind") or 0), "changes": list(doc.get("changes") or [])[:50],
           "update": doc.get("update", "unknown"), "how": "Run PSU Server Setup and choose Update."}
    if doc.get("why"):
        out["why"] = doc["why"]
    return out


def calendar_entries(lo, hi):
    """The panel's calendar for [lo, hi) (unix seconds): every run of the host's events
    (psu/events.py, a weekly one once per week it runs), and with PSU_SEASON=auto the lobby
    seasons of season.CALENDAR (UTC days), which an event's own season replaces while it runs."""
    out = []
    for e in events.load():
        kind = "weekly event" if "weekly" in e else "event"
        for s, t in events.windows(e, lo, hi):
            out.append({"id": "%s-%d" % (e["key"], int(s)), "title": e["name"], "start": int(s), "end": int(t),
                        "kind": kind, "about": _effects(e)})
    if season.mode() == "auto":
        dressed = [(s, t, season.name_of(e["season"])) for e in events.load() if e.get("season")
                   for s, t in events.windows(e, lo, hi)]
        for name, s, t in season.auto_windows(lo, hi):
            if any(n == name and a <= s and b >= t for a, b, n in dressed):
                continue                               # an event already shows these lobbies, the whole run
            out.append({"id": "season-%s-%d" % (name, s), "title": "Lobby season: " + name, "start": s, "end": t,
                        "kind": "season",
                        "about": "PSU_SEASON=auto: the lobbies' %s decorations and event drops." % name})
    out.sort(key=lambda r: (r["start"], r["end"], r["id"]))
    return out


def build(loop, snapshot):
    """The AdminPort; `snapshot()` is the board document (board.snapshot with the game's store)."""
    port = olport.AdminPort(
        "psu", "Phantasy Star Universe", short="PSU", platform="PS2 disc",
        about="Phantasy Star Universe and Ambition of the Illuminus.",
        token=TOKEN, bind=BIND, port=PORT,
        services=[{"name": "Phantasy Star Universe", "containers": CONTAINERS,
                   "warning": "Everyone in Phantasy Star Universe is disconnected."}])

    def run(req, actor):
        try:
            out = _on_loop(loop, lambda: admin.handle(req, "panel:" + (actor or "?")[:32]))
        except (ValueError, TypeError, KeyError) as e:
            return False, str(e)
        return True, out

    @port.status
    def status():
        doc = _on_loop(loop, snapshot)
        on = doc.get("online") or {}
        players = [{"name": p["name"], "where": p.get("mission") or p.get("where") or "",
                    "level": p.get("level"), "cls": p.get("cls"), "game": p.get("game")}
                   for p in on.get("players") or []]
        return {"up": True, "online": on.get("total", 0), "players": players,
                "rooms": [{"name": u["name"], "players": u.get("count", 0)} for u in on.get("universes") or []],
                "rooms_label": "universes",
                "facts": [{"label": "in PSU", "value": on.get("psu", 0)},
                          {"label": "in Ambition of the Illuminus", "value": on.get("aoti", 0)}],
                "update": update_state()}

    @port.calendar
    def calendar(lo, hi):
        return _on_loop(loop, lambda: calendar_entries(lo, hi))

    @port.action("announce", "Announce", kind="announce",
                 fields=[{"id": "text", "label": "Message", "type": "text"}],
                 about="A ticker line for everyone online, in every universe.")
    def announce(params, actor):
        text = " ".join(str(params.get("text") or "").split())
        if not text:
            return False, "Nothing to announce."
        ok, out = run({"cmd": "announce", "text": text, "scope": "all", "kind": "ticker"}, actor)
        return (ok, "Sent to %d player%s." % (out["sent"], "" if out["sent"] == 1 else "s")) if ok else (ok, out)

    @port.action("kick", "Kick", kind="kick", danger=True,
                 fields=[{"id": "name", "label": "Character", "type": "player"},
                         {"id": "reason", "label": "Reason", "type": "text"}],
                 about="Disconnects the character now.")
    def kick(params, actor):
        # from the panel's form a character name; from an account's Kick its handles, tried as names
        names = [n.strip() for n in str(params.get("name") or params.get("handles") or "").split(",") if n.strip()]
        if not names:
            return False, "No character named."
        kicked, last = 0, ""
        for n in names:
            ok, out = run({"cmd": "kick", "name": n, "reason": str(params.get("reason") or "")}, actor)
            if ok and out.get("online"):
                kicked += 1
            elif not ok:
                last = out
        if kicked:
            return True, "Kicked %d character%s." % (kicked, "" if kicked == 1 else "s")
        return False, last or "Not online."

    @port.action("mute", "Mute", fields=[{"id": "name", "label": "Character", "type": "player"},
                                         {"id": "minutes", "label": "Minutes", "type": "text"},
                                         {"id": "reason", "label": "Reason", "type": "text"}],
                 about="Stops the character's chat for a while (60 minutes unless given).")
    def mute(params, actor):
        try:
            minutes = int(str(params.get("minutes") or "60").strip())
        except ValueError:
            return False, "Minutes is a number."
        ok, out = run({"cmd": "mute", "name": str(params.get("name") or ""), "minutes": minutes,
                       "reason": str(params.get("reason") or "")}, actor)
        return (True, "Muted for %d minutes." % minutes) if ok else (False, out)

    @port.action("unmute", "Unmute", fields=[{"id": "name", "label": "Character", "type": "player"}])
    def unmute(params, actor):
        ok, out = run({"cmd": "unmute", "name": str(params.get("name") or "")}, actor)
        if not ok:
            return False, out
        return True, "Unmuted." if out.get("was_muted") else "That character was not muted."

    @port.action("ban", "Ban", danger=True,
                 fields=[{"id": "account", "label": "Account (login name)", "type": "text"},
                         {"id": "hours", "label": "Hours (0 = permanent)", "type": "text"},
                         {"id": "reason", "label": "Reason", "type": "text"}],
                 about="The account cannot log in for that long (24 hours unless given); kicked if online.")
    def ban(params, actor):
        try:
            hours = float(str(params.get("hours") or "24").strip())
        except ValueError:
            return False, "Hours is a number."
        ok, out = run({"cmd": "ban", "account": str(params.get("account") or "").strip(), "hours": hours,
                       "reason": str(params.get("reason") or "")}, actor)
        if not ok:
            return False, out
        how = "permanently" if hours <= 0 else "until " + events.stamp(out["until"])
        return True, "%s is banned %s%s." % (out["account"], how, " and was kicked" if out["online"] else "")

    @port.action("unban", "Unban", fields=[{"id": "account", "label": "Account (login name)", "type": "text"}])
    def unban(params, actor):
        ok, out = run({"cmd": "unban", "account": str(params.get("account") or "").strip()}, actor)
        if not ok:
            return False, out
        return True, ("%s is unbanned." if out["was_banned"] else "%s was not banned.") % out["account"]

    port.help_desk(
        lambda open_only: _on_loop(loop, lambda: helpdesk.threads(open_only)),
        lambda tid: _on_loop(loop, lambda: helpdesk.thread(tid)),
        lambda tid, text, close, actor: _on_loop(
            loop, lambda: helpdesk.reply(tid, text, close, "panel:" + (actor or "?")[:32])),
        lambda tid, actor: _on_loop(loop, lambda: helpdesk.close(tid, "panel:" + (actor or "?")[:32])))

    @port.action("season", "Season preview",
                 fields=[{"id": "name", "label": "Character", "type": "player"},
                         {"id": "season", "label": "Season (or none, off)", "type": "text"}],
                 about="Shows one character a lobby season on their next lobby load; off ends it.")
    def season_preview(params, actor):
        ok, out = run({"cmd": "season", "name": str(params.get("name") or "").strip(),
                       "season": str(params.get("season") or "").strip()}, actor)
        return (True, out["line"]) if ok else (False, out)

    @port.players
    def pick_characters(q, limit):
        return _on_loop(loop, lambda: character_picks(q, limit))

    @port.player
    def player(who):
        gids = link_gids(((who or {}).get("links") or {}).get("psu") or [])
        return _on_loop(loop, lambda: player_view(gids)) if gids else None

    @port.tool("players", "Players", "players", about="Accounts and their characters. Read-only.")
    def players(method, sub, query, body, actor):
        q = (olpage.posted(body).get("q", "") if method == "POST"
             else dict(urllib.parse.parse_qsl(query or "")).get("q", "")).strip()[:40]
        found = _on_loop(loop, lambda: find_accounts(q))
        return 200, olpage.CTYPE, players_page(q, found).encode()

    @port.tool("news", "News", "news", about="The server news: the login notices and the ticker.")
    def news_tool(method, sub, query, body, actor):
        note = None
        if method == "POST":
            f = olpage.posted(body)
            note = save_news(f.get("text", ""), f.get("was", ""), actor,
                             lambda *a: _on_loop(loop, lambda: after_news_save(*a)))
        return 200, olpage.CTYPE, news_page(note).encode()

    @port.tool("events", "Events", "events",
               about="The event calendar: lobby seasons, EXP and drop boosts, featured missions.")
    def events_tool(method, sub, query, body, actor):
        note = None
        if method == "POST":
            f = olpage.posted(body)
            note = save_events(f.get("text", ""), f.get("was", ""), actor,
                               lambda *a: _on_loop(loop, lambda: after_events_save(*a)))
        rows, sea = _on_loop(loop, lambda: (events.board_rows(), gm.season_status()))
        return 200, olpage.CTYPE, events_page(rows, sea, note).encode()

    return port


# --- players --------------------------------------------------------------------------------------

def find_accounts(q):
    """Accounts whose login or a character name contains q (with no q, the accounts online), each with
    its characters. On the loop: the store and the world are read there."""
    store = gm._db()
    if q:
        like = "%" + q.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"   # q is literal
        rows = store.c.execute(
            "SELECT username, gid, created, last_login, (SELECT until FROM bans b WHERE b.gid = a.gid) "
            "FROM accounts a WHERE lower(username) LIKE lower(?) ESCAPE '!' OR gid IN "
            "(SELECT gid FROM characters WHERE lower(name) LIKE lower(?) ESCAPE '!')", (like, like)).fetchall()
    else:
        rows = [r[:4] + (r[5],) for r in store.accounts() if r[1] in WORLD.online]
    rows = sorted(rows, key=lambda r: -(r[3] or r[2] or 0))
    out = []
    for user, gid, created, last, ban_until in rows[:SEARCH_MAX]:
        p = WORLD.online.get(gid)
        chars = []
        for slot, name, st in store.c.execute("SELECT slot, name, state FROM characters WHERE gid = ? "
                                              "ORDER BY slot", (gid,)).fetchall():
            live = p is not None and getattr(p, "slot", None) == slot and p.state is not None
            st = dict(p.state) if live else _state(st)
            chars.append({"slot": slot, "name": name or "?", "level": st.get("level", 1),
                          "cls": board.cls_name(st.get("cls")), "meseta": int(st.get("meseta", 0)),
                          "playtime": int(st.get("playtime", 0)), "game": board.char_game(st),
                          "online": live, "uni": p.uni if live else None})
        out.append({"account": user, "gid": gid, "created": created, "last_login": last,
                    "banned": ban_until if ban_until and ban_until >= time.time() else None,
                    "online": p is not None, "characters": chars})
    return {"accounts": out, "more": len(rows) > SEARCH_MAX, "online": len(WORLD.online)}


def character_picks(q, limit):
    """The panel's player picker: characters by name or login, those online first. The id is the
    character name, which is what kick, mute and the season preview take."""
    out = []
    for a in find_accounts(q.strip()[:40])["accounts"]:
        for c in a["characters"]:
            if q and q.lower() not in c["name"].lower() and q.lower() not in a["account"].lower():
                continue
            out.append({"id": c["name"], "label": c["name"],
                        "detail": "%s, level %d %s" % (a["account"], c["level"], c["cls"]),
                        "online": c["online"]})
    out.sort(key=lambda r: not r["online"])
    return out[:limit]


def link_gids(links):
    """The GIDs of the panel's links["psu"] entries, in order, once each. A key is the GID as hex
    (login.link_verify: 8 digits); anything else is skipped."""
    out = []
    for x in links:
        k = str((x or {}).get("key", "")).strip().lower()
        if 1 <= len(k) <= 8 and all(c in "0123456789abcdef" for c in k):
            gid = int(k, 16)
            if gid not in out:
                out.append(gid)
    return out


def player_view(gids):
    """On the loop: the account window's characters for these GIDs, or None when none is an account."""
    store = gm._db()
    chars, logins = [], 0
    for gid in gids:
        row = store.c.execute("SELECT username, last_login, (SELECT until FROM bans b WHERE b.gid = a.gid) "
                              "FROM accounts a WHERE gid = ?", (gid,)).fetchone()
        if row is None:
            continue
        logins += 1
        user, last, ban_until = row
        p = WORLD.online.get(gid)
        facts = [{"label": "PSU login", "value": user}, {"label": "Last login", "value": _when(last)}]
        if ban_until and ban_until >= time.time():
            facts.append({"label": "Banned until", "value": _when(ban_until)})
        for slot, name, st in store.c.execute("SELECT slot, name, state FROM characters WHERE gid = ? "
                                              "ORDER BY slot", (gid,)).fetchall():
            live = p is not None and getattr(p, "slot", None) == slot and p.state is not None
            st = dict(p.state) if live else _state(st)
            disc = {"psu": "PSU", "aoti": "AotI"}.get(board.char_game(st), board.char_game(st))
            chars.append({"name": name or "?", "online": live,
                          "detail": "%s, level %s, %s" % (board.cls_name(st.get("cls")), st.get("level", 1), disc),
                          "facts": facts + [{"label": "Play time", "value": _hours(int(st.get("playtime", 0)))}]})
    if not logins:
        return None
    out = {"characters": chars}
    if not chars:
        out["note"] = "The linked PSU login%s %s no characters yet." % (("", "has") if logins == 1 else ("s", "have"))
    return out


def _state(text):
    try:
        v = json.loads(text or "{}")
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _when(t):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t)) if t else "-"


def _hours(secs):
    return "%d h %02d min" % (secs // 3600, secs % 3600 // 60)


def players_page(q, found):
    parts = [olpage.panel("", olpage.form([("q", "Login or character name", q)], "Search"))]
    if not found["accounts"]:
        parts.append(olpage.panel("", olpage.Raw('<p class="muted">%s</p>' % olpage.esc(
            "No account matches." if q else "Nobody is online. Search for a login or character name."))))
    for a in found["accounts"]:
        state = (["online"] if a["online"] else []) + (["banned until " + _when(a["banned"])] if a["banned"] else [])
        facts = olpage.kv([("GID", olpage.Raw('<span class="mono">%s</span>' % olpage.esc("%#010x" % a["gid"]))),
                           ("Created", _when(a["created"])), ("Last login", _when(a["last_login"])),
                           ("State", ", ".join(state) or "offline")])
        rows = [[c["name"], c["cls"], c["level"], "{:,}".format(c["meseta"]), _hours(c["playtime"]),
                 {"psu": "PSU", "aoti": "AotI"}.get(c["game"], c["game"]),
                 "universe %d" % c["uni"] if c["online"] else ""]
                for c in a["characters"]]
        parts.append(olpage.panel(a["account"], facts, olpage.Raw("<p></p>"), olpage.table(
            ["Character", "Class", "Level", "Meseta", "Play time", "Disc", "Online"], rows,
            empty="No characters.")))
    sub = "Accounts matching “%s”." % q if q else "The accounts online now (%d)." % found["online"]
    if found["more"]:
        sub += " The first %d are shown." % SEARCH_MAX
    return olpage.page("Players", *parts, sub=sub)


# --- news -----------------------------------------------------------------------------------------

def _news_path():
    return news.path(admin.STATE_DIR)


def read_news():
    """(the news file's text, a digest of it); "" without a file."""
    try:
        with open(_news_path(), encoding="utf-8", newline="") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    return text, hashlib.sha256(text.encode()).hexdigest()[:16]


def write_atomic(path, text):
    """Replace path with text in one rename: a reader sees the old file or the new one, never half."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.%d.tmp" % (os.path.basename(path), os.getpid()))
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def after_news_save(by, path, size, was, now):
    """On the loop: the server re-reads the news (admin "news") and the save is audited."""
    notices = admin.handle({"cmd": "news"}, by)["notices"]
    gm.audit(by, "panel", "news_edit", path, bytes=size, notices=len(notices), was=was, now=now)
    return notices


def save_news(text, was, actor, reload):
    """A posted news file -> (ok, note) for the page. `reload(by, path, bytes, was, now)` runs the
    server's re-read and the audit on the loop."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    text = text + "\n" if text else ""
    if len(text.encode()) > NEWS_MAX:
        return False, "The news file is limited to %d KB. Nothing was saved." % (NEWS_MAX // 1024)
    by = "panel:" + (actor or "?")[:32]
    with _news_lock:
        old, digest = read_news()
        if was != digest:
            return False, "The news changed since this page was opened; this is it now. Nothing was saved."
        if text == old:
            return True, "No change."
        write_atomic(_news_path(), text)
        notices = reload(by, _news_path(), len(text.encode()), digest, read_news()[1])
    return True, "Saved. The server now shows %d notice%s, events included." % (
        len(notices), "" if len(notices) == 1 else "s")


def news_page(note=None):
    text, digest = read_news()
    editor = olpage.Raw(
        '<form method=post><input type=hidden name=was value="%s">'
        '<label>%s<textarea name=text rows=16 spellcheck=false style="font:13px/1.45 ui-monospace,Consolas,'
        'monospace;color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:8px;'
        'padding:8px 10px;width:100%%;resize:vertical">%s</textarea></label>'
        '<p class=muted>One notice per block with a line holding only --- between them. Lines starting'
        ' with # are comments. The server re-reads the file as soon as it is saved.</p>'
        '<button type=submit class=act>Save</button></form>'
        % (olpage.esc(digest), olpage.esc(_news_path()), olpage.esc(text)))
    shown = olpage.table(["#", "Notice"], [[i + 1, n] for i, n in enumerate(news.parse(text))],
                         empty="No notices in the file: the login shows PSU_MOTD and nothing rotates.")
    return olpage.page("News", olpage.panel("The news file", editor), olpage.panel("Its notices", shown),
                       sub="The login notice box and the in-game news ticker.", note=note)


# --- events ---------------------------------------------------------------------------------------

def read_events():
    """(the events file's text, a digest of it); "" without a file."""
    try:
        with open(events.path(), encoding="utf-8", newline="") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    return text, hashlib.sha256(text.encode()).hexdigest()[:16]


def after_events_save(by, path, size, was, now):
    """On the loop: the server re-reads the calendar (admin "events") and the save is audited."""
    out = admin.handle({"cmd": "events"}, by)
    gm.audit(by, "panel", "events_edit", path, bytes=size, upcoming=len(out["events"]), was=was, now=now)
    return out["events"]


def save_events(text, was, actor, reload):
    """A posted events file -> (ok, note) for the page; nothing is saved while any entry has a problem."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = text + "\n" if text else ""
    if len(text.encode()) > EVENTS_MAX:
        return False, "The events file is limited to %d KB. Nothing was saved." % (EVENTS_MAX // 1024)
    problems = events.validate(text)
    if problems:
        return False, "Nothing was saved: " + "; ".join(problems[:5]) + ("; ..." if len(problems) > 5 else "")
    by = "panel:" + (actor or "?")[:32]
    with _events_lock:
        old, digest = read_events()
        if was != digest:
            return False, "The events changed since this page was opened; this is the file now. Nothing was saved."
        if text == old:
            return True, "No change."
        write_atomic(events.path(), text)
        rows = reload(by, events.path(), len(text.encode()), digest, read_events()[1])
    return True, "Saved. %d event%s running or in the next %d days." % (
        len(rows), "" if len(rows) == 1 else "s", events.HORIZON_DAYS)


EVENTS_HELP = (
    'A JSON list. Each event: "key", "name", and a UTC window "start" / "end" ("2026-10-28T00:00Z") or'
    ' "weekly": {"dow": [5, 6], "from": "18:00", "hours": 24} (0 = Monday). Optional: "game" ("psu" or'
    ' "aoti"), "season" (dresses the lobbies while it runs), "exp" / "meseta" (multipliers, up to 10),'
    ' "drops" (rolls per kill, 1..5), "event_drop" (season items 1 in N), "missions" (featured, ranked on'
    ' the board), "notice" (shown with it). The server re-reads the file as soon as it is saved.')


def events_page(rows, sea, note=None):
    text, digest = read_events()
    editor = olpage.Raw(
        '<form method=post><input type=hidden name=was value="%s">'
        '<label>%s<textarea name=text rows=22 spellcheck=false style="font:13px/1.45 ui-monospace,Consolas,'
        'monospace;color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:8px;'
        'padding:8px 10px;width:100%%;resize:vertical">%s</textarea></label>'
        '<p class=muted>%s</p><button type=submit class=act>Save</button></form>'
        % (olpage.esc(digest), olpage.esc(events.path()), olpage.esc(text), olpage.esc(EVENTS_HELP)))
    shown = olpage.table(
        ["", "Event", "From", "To", "What it does"],
        [["running" if r["running"] else "", r["name"], events.stamp(r["start"]), events.stamp(r["end"]),
          _effects(r)] for r in rows],
        empty="Nothing running or in the next %d days." % events.HORIZON_DAYS)
    season_kv = olpage.kv([
        ("Lobbies now", sea["current"] or "no season"),
        ("PSU_SEASON", sea["mode"] or "(a name it does not know)"),
        ("Event season", sea["event"] or "none running"),
        ("Staged", ", ".join(sea["staged"]) or "nothing (built by extract, docs/data.md)"),
        ("Previews", ", ".join("%s: %s" % kv for kv in sea["previews"].items()) or "none")])
    return olpage.page("Events", olpage.grid(olpage.panel("Running and next", shown),
                                             olpage.panel("Lobby season", season_kv)),
                       olpage.panel("The events file", editor),
                       sub="Limited-time events: the login notices, tickers, kill rewards and the board.", note=note)


def start(snapshot):
    """Called from game.main inside the running loop. Never raises."""
    if not PORT:
        return None
    try:
        port = build(asyncio.get_running_loop(), snapshot)
        host, n = port.start()
    except (OSError, ValueError) as e:
        print(f"game: panel port not started ({e})", flush=True)
        return None
    print(f"game: OpenLobby admin protocol on {host}:{n}", flush=True)
    return port
