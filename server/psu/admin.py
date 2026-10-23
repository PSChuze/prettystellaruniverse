"""Admin channel into the running game service: a loopback-only TCP port with a token (tools/announce.py).

  PSU_ADMIN_PORT   default 22190; 0 turns the channel off. Always bound to 127.0.0.1 (under host networking
                   only the machine itself reaches it).
  PSU_ADMIN_TOKEN  the shared secret; without it the service writes a random one to $PSU_STATE_DIR/admin.token
                   (mode 0600) at start and tools/announce.py reads it from there.

One JSON object per line each way. Request {"token": ..., "cmd": ..., ...}; reply {"ok": true, ...} or
{"ok": false, "error": ...}. Commands (the same operations as the GM chat commands, psu/gm.py):

  announce   text, scope "all" | "uni:N" | "area:QUEST/ZONE" | "player:NAME", kind "ticker" | "right" | "dialog"
             | "center", seconds (dialog)
  who        players online          stats      server counters        spawns  zone of player NAME
  kick       name, reason            mute       name, minutes, reason  unmute  name
  ban        account (the login name), hours (default 24; 0 = 10 years), reason: the account cannot log in
             until then, and is kicked if it is online         unban   account
  news       the notices now in the news file (re-read)
  events     the event calendar (psu/events.py) re-read: upcoming runs and the entries it skipped
  season     no name: the lobby season now (PSU_SEASON, a running event's), what is staged, the previews;
             name, season: preview that season (or "none", or "off" to end the preview) for one character
             on its next lobby load (gm.season_preview)

Every accepted command is audited to events.jsonl as ev "gm_action" with via "cli"; a bad token is logged as
ev "admin_refused" and the connection closed.
"""
import asyncio
import hmac
import json
import os
import secrets
import sys

from psu import db as dbm, events, gm, news
from psu.observe import event
from psu.world import WORLD

PORT = int(os.environ.get("PSU_ADMIN_PORT", "22190"))
HOST = "127.0.0.1"
KINDS = {"center": 0, "ticker": 1, "right": 2, "dialog": 3}
MAX_LINE = 8192


def token_path(state_dir):
    return os.path.join(state_dir, "admin.token")


def load_token(state_dir):
    """PSU_ADMIN_TOKEN, else the token file (created on first use)."""
    tok = os.environ.get("PSU_ADMIN_TOKEN", "").strip()
    if tok:
        return tok
    p = token_path(state_dir)
    try:
        with open(p, encoding="utf-8") as f:
            tok = f.read().strip()
    except OSError:
        tok = ""
    if not tok:
        tok = secrets.token_hex(24)
        os.makedirs(state_dir, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
    return tok


def parse_scope(s):
    s = (s or "all").strip()
    if s == "all":
        return None
    kind, _, val = s.partition(":")
    if kind == "uni":
        return ("uni", int(val))
    if kind == "area":
        q, z = val.split("/")
        return ("area", int(q), int(z))
    if kind == "player":
        gid, p = gm.find_player(val)
        if p is None:
            raise ValueError(f"{val} is not online")
        return ("player", gid)
    raise ValueError(f"bad scope {s!r}")


def handle(req, by):
    """One request (already authenticated) -> reply dict."""
    cmd = req.get("cmd")
    if cmd == "announce":
        kind = KINDS.get(req.get("kind", "ticker"))
        if kind is None:
            raise ValueError("kind: ticker, right, dialog or center")
        n = gm.broadcast(str(req.get("text", "")), parse_scope(req.get("scope")), kind,
                         int(req.get("seconds", 10 if kind == 3 else 0)), by, "cli")
        return {"sent": n}
    if cmd == "who":
        return {"players": gm.who()}
    if cmd == "stats":
        return {"stats": gm.stats(), "line": gm.fmt_stats(gm.stats())}
    if cmd == "events":
        p, rows, problems = events.check()
        return {"events_file": p, "events": rows, "problems": problems}
    if cmd == "news":
        news._cache.clear()
        return {"notices": news.notices(STATE_DIR)}
    if cmd in ("ban", "unban"):
        return _ban(cmd, req, by)
    if cmd == "season":
        if not req.get("name"):
            st = gm.season_status()
            return dict(st, line=gm.fmt_season(st))
        gid, p = gm.find_player(str(req["name"]))
        if gid is None:
            raise ValueError(f"no such player: {req.get('name')}")
        return {"line": gm.season_preview(gid, str(req.get("season", "")), by, "cli")}
    if cmd in ("kick", "mute", "unmute", "spawns"):
        gid, p = gm.find_player(str(req.get("name", "")))
        if gid is None:
            raise ValueError(f"no such player: {req.get('name')}")
        if cmd == "kick":
            return {"online": gm.kick(gid, str(req.get("reason", ""))[:200], by, "cli")}
        if cmd == "mute":
            gm.mute(gid, int(req.get("minutes", 60)), str(req.get("reason", "")), by, "cli")
            return {"muted": True}
        if cmd == "unmute":
            return {"was_muted": gm.unmute(gid, by, "cli")}
        if p is None:
            raise ValueError("not online")
        return {"spawns": gm.spawns(p.zone), "line": gm.fmt_spawns(gm.spawns(p.zone))}
    raise ValueError(f"unknown command {cmd!r}")


BAN_HOURS_MAX = 10 * 365 * 24


def _ban(cmd, req, by):
    """ban / unban by account (login) name, the exact spelling or a unique case-insensitive match."""
    name = str(req.get("account", "")).strip()
    if not dbm.valid_name(name):
        raise ValueError("account: a login name of 1 to %d characters" % dbm.NAME_MAX)
    store = gm._db()
    row = store.account(name)
    if row is None:
        raise ValueError(f"no such account: {name}")
    user, gid = row
    if cmd == "unban":
        was = store.unban(gid)
        gm.audit(by, "cli", "unban", f"{gid:#x}", account=user, was=was)
        return {"account": user, "was_banned": was}
    try:
        hours = float(req.get("hours", 24))
    except (TypeError, ValueError):
        raise ValueError("hours is a number") from None
    if not 0 <= hours <= BAN_HOURS_MAX:      # also refuses nan
        raise ValueError("hours: 0 (10 years) up to %d" % BAN_HOURS_MAX)
    reason = " ".join(str(req.get("reason", "")).split())[:200]
    until = gm.ban_until(hours)
    store.ban(gid, until, reason, by)
    gm.audit(by, "cli", "ban", f"{gid:#x}", account=user, hours=hours, reason=reason)
    online = WORLD.online.get(gid) is not None
    if online:
        gm.kick(gid, reason or "banned", by, "cli")
    return {"account": user, "until": int(until), "online": online}


STATE_DIR = "."
_token = None


async def client(reader, writer):
    peer = writer.get_extra_info("peername") or ("?", 0)

    async def reply(obj):
        writer.write((json.dumps(obj) + "\n").encode())
        await writer.drain()

    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), 30)
            if not line:
                break
            if len(line) > MAX_LINE:
                await reply({"ok": False, "error": "request too long"})
                break
            try:
                req = json.loads(line)
                assert isinstance(req, dict)
            except (ValueError, AssertionError):
                await reply({"ok": False, "error": "bad json"})
                break
            if not hmac.compare_digest(str(req.get("token", "")).encode(), _token.encode()):
                event(ev="admin_refused", peer=peer[0], cmd=str(req.get("cmd"))[:20])
                await reply({"ok": False, "error": "bad token"})
                break
            by = "cli:" + str(req.get("by", "operator"))[:32]
            try:
                out = handle(req, by)
                await reply(dict(ok=True, **out))
            except (ValueError, TypeError, KeyError) as e:
                await reply({"ok": False, "error": str(e)})
    except (asyncio.TimeoutError, ConnectionError, asyncio.LimitOverrunError, ValueError):
        pass
    finally:
        writer.close()


def _ticker_broadcast(text):
    gm.broadcast(text, None, gm.TICKER, 0, "news", "rotation")


async def start(state_dir):
    """Called from game.py main: the admin port (when PSU_ADMIN_PORT != 0) and the news rotation."""
    global STATE_DIR, _token
    STATE_DIR = state_dir
    loop = asyncio.get_running_loop()
    loop.create_task(news.ticker(state_dir, _ticker_broadcast))
    if PORT <= 0:
        return None
    _token = load_token(state_dir)
    server = await asyncio.start_server(client, HOST, PORT, limit=MAX_LINE * 2)
    print(f"game: admin channel on {HOST}:{PORT} (token in {token_path(state_dir)} or PSU_ADMIN_TOKEN)",
          file=sys.stderr, flush=True)
    return server


def welcome(p, delay=None):
    """The current notice as a ticker a few seconds after p's first lobby load."""
    text = news.current(STATE_DIR)
    if not text:
        return

    def fire():
        if WORLD.online.get(p.gid) is p:
            WORLD.relay(p, gm.c0228_notice(p, news.one_line(text), gm.TICKER), "0x0228 news welcome")
    asyncio.get_running_loop().call_later(news.WELCOME_DELAY if delay is None else delay, fire)
