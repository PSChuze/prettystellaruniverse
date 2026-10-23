"""GM commands, typed in chat with a "!" prefix by accounts listed in PSU_ADMINS, and the operations
the loopback admin channel (psu/admin.py, tools/announce.py) shares with them.

  PSU_ADMINS=alice,bob          account names (login user IDs), case-insensitive

Everyone: !help, !who (how many are online), !news (the server notices, psu/news.py), !gm <message> (a help
request to the staff, psu/helpdesk.py). Admins also:
  !announce <text>              ticker to everyone online (S>C 0x0228 type 1)
  !uni <text> / !zone <text>    ticker to my universe / my zone
  !notice <text>                timed dialog (0x0228 type 3, 10 s) to everyone
  !online                       every player: character, account, disc, universe, area
  !stats                        uptime, players per disc / universe, zones, parties, trades, mutes
  !spawns                       my zone: players, enemies spawned / alive / in the set data, ground items, boxes
  !kick <name> [reason]         tell the player why, then drop the connection
  !ban <name> [hours] [reason]  suspend an account (default 24 h; 0 = 10 years) and kick it
  !unban <name>
  !mute <name> [minutes] [reason]   chat is not relayed (default 60 min; 0 = until unmuted); kept across restarts
  !unmute <name>
  !summon <name> / !goto <name> bring a player to my lobby spot / go to theirs (lobbies only, same universe,
                                nobody on a mission)
  !give <item id hex> [qty] [name]  add an item to my (or the named player's) inventory (0x0106)
  !meseta <amount> [name]       set meseta (+N / -N adds / takes)
  !level <n>                    set my level (1..cap)
  !warp <quest> <zone> <map> [entry]
  !news reload                  re-read the news file now (it is also re-read when it changes)
  !season [name | none | off]   preview a lobby season on my next lobby load (none: no season; off: back to
                                the server's); no name: what runs now and what is staged
  !clear                        clear the current mission
<name> is a character name of someone online, an account name or a character name. Every command is written
to events.jsonl (ev "gm"), every change an admin makes again with its result (ev "gm_action": by, via, cmd,
target, detail).

Notices: S>C 0x0228, same layout and handler shape on both discs (PSU 0xd9f00, AotI 0xd3390): +0x24 type,
+0x28 duration, +0x2C UTF-16 text. Type 0 centre dialog (PSU 0x47fc80, needs the HUD object [0x62208c]; duration
clamped to 0..2 as a style), 1 top ticker (0x380b60), 2 right-hand ticker (0x1e8bd0), 3 timed dialog (0x48a3c0,
closes after `duration` s). S>C 0x0224 (PSU 0xda0e0) only stores a reason code while the client is in
connect state 0x11, so a kick explains itself with a 0x0228 before the socket closes.
"""
import asyncio
import json
import os
import struct
import time

from psu import db as dbm, games, social
from psu.login import packet
from psu.observe import event
from psu.world import WORLD

ADMINS = {a.strip().lower() for a in os.environ.get("PSU_ADMINS", "").split(",") if a.strip()}
ACC = bytes.fromhex("00011300")
PLAYER = bytes.fromhex("00001200")
SERVER_NAME = "SERVER"
TICKER, RIGHT, DIALOG = 1, 2, 3
STARTED = time.time()


def is_admin(username):
    return bool(username) and username.lower() in ADMINS


def c0228_notice(d, text, kind=1, seconds=0):
    """S>C 0x0228 (handler 0xd9f00, EGS send_0228): +0x24 u32 type (1 = top ticker; 3 = timed dialog),
    +0x28 u32 duration, UTF-16 message + NUL. A 0x0304 cannot carry a custom sender name on PS2."""
    return packet(0x0228, struct.pack("<H", 0xFFFF) + bytes(18) + ACC + struct.pack("<I", d.gid) + bytes(8)
                  + struct.pack("<II", kind, seconds) + text[:511].encode("utf-16-le") + b"\0\0")


def split(text):
    parts = text.strip()[1:].split()
    return (parts[0].lower(), parts[1:]) if parts else ("", [])


def ban_until(hours):
    return time.time() + (hours * 3600 if hours > 0 else 10 * 365 * 86400)


def _db():
    return dbm.get(social.STATE_DIR)


def audit(by, via, cmd, target=None, **detail):
    event(ev="gm_action", by=by, via=via, cmd=cmd, target=target, **detail)


def char_name(p):
    return p.raw[0x28:0x68].decode("utf-16-le", "replace").split("\0")[0] if p.raw else "?"


def find_player(name):
    """(gid or None, online Player or None) for a character name online, an account or a character name."""
    low = name.lower()
    for o in list(WORLD.online.values()):
        if char_name(o).lower() == low:
            return o.gid, o
    gid = _db().find(name)
    return gid, (WORLD.online.get(gid) if gid is not None else None)


def label(gid, p=None):
    return char_name(p) if p is not None else (_db().username(gid) or f"{gid:#x}")


def in_session(p, fn, *args):
    """Run fn(*args) as p's disc (games.current() is per session task, psu/games.py): a task of its own copies
    this context, so setting the game there leaves the caller's alone."""
    async def run():
        games.set_current(p.game)
        r = fn(*args)
        return await r if asyncio.iscoroutine(r) else r
    return asyncio.get_running_loop().create_task(run())


# --- broadcasts ------------------------------------------------------------------------------------

def recipients(scope=None):
    """scope: None everyone; ("uni", n); ("area", quest, zone) in any universe; ("zone", world zone key);
    ("player", gid)."""
    out = []
    for o in list(WORLD.online.values()):
        if scope is None:
            out.append(o)
        elif scope[0] == "uni" and o.uni == scope[1]:
            out.append(o)
        elif scope[0] == "area" and o.zone is not None and tuple(o.zone[1:]) == tuple(scope[1:]):
            out.append(o)
        elif scope[0] == "zone" and o.zone == scope[1]:
            out.append(o)
        elif scope[0] == "player" and o.gid == scope[1]:
            out.append(o)
    return out


def broadcast(text, scope=None, kind=TICKER, seconds=0, by="server", via="server"):
    """A notice to everyone in scope; returns how many got it."""
    text = text.strip()[:511]
    if not text:
        return 0
    if kind not in (0, 1, 2, 3):
        kind = TICKER
    if kind == 0:
        seconds = 2
    rs = recipients(scope)
    for o in rs:
        WORLD.relay(o, c0228_notice(o, text, kind, seconds), f"0x0228 notice type {kind}")
    audit(by, via, "announce", scope=list(scope) if scope else "all", kind=kind, seconds=seconds, text=text, to=len(rs))
    return len(rs)


# --- moderation ------------------------------------------------------------------------------------

def kick(gid, reason="", by="server", via="server"):
    """Explain (0x0228 timed dialog), then drop the connection 1.5 s later. True when the player was online."""
    p = WORLD.online.get(gid)
    if p is not None:
        msg = "You have been disconnected by a GM." + (f" Reason: {reason}" if reason else "")
        WORLD.relay(p, c0228_notice(p, msg, DIALOG, 5), "0x0228 kick notice")
        if p.writer is not None:
            asyncio.get_running_loop().call_later(1.5, p.writer.transport.abort)
    audit(by, via, "kick", f"{gid:#x}", reason=reason, online=p is not None)
    return p is not None


_mutes = None                        # gid -> {"until": t or 0, "reason", "by"}


def _mute_path():
    return os.path.join(social.STATE_DIR or ".", "mutes.json")


def mutes():
    global _mutes
    if _mutes is None:
        try:
            with open(_mute_path(), encoding="utf-8") as f:
                _mutes = {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError):
            _mutes = {}
    now_ = time.time()
    for g in [g for g, m in _mutes.items() if m.get("until") and m["until"] <= now_]:
        del _mutes[g]
    return _mutes


def _save_mutes():
    try:
        tmp = _mute_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(g): m for g, m in mutes().items()}, f)
        os.replace(tmp, _mute_path())
    except OSError:
        pass


def muted(gid):
    return mutes().get(gid)


def muted_text(gid):
    m = muted(gid) or {}
    if m.get("until"):
        left = max(1, int((m["until"] - time.time()) // 60) + 1)
        return f"You are muted for {left} more minute(s)."
    return "You are muted."


def mute(gid, minutes=60, reason="", by="server", via="server"):
    until = time.time() + minutes * 60 if minutes > 0 else 0
    mutes()[gid] = {"until": until, "reason": reason[:200], "by": by}
    _save_mutes()
    p = WORLD.online.get(gid)
    if p is not None:
        WORLD.relay(p, c0228_notice(p, muted_text(gid) + (f" Reason: {reason}" if reason else ""), DIALOG, 5),
                    "0x0228 muted")
    audit(by, via, "mute", f"{gid:#x}", minutes=minutes, reason=reason)


def unmute(gid, by="server", via="server"):
    had = mutes().pop(gid, None) is not None
    if had:
        _save_mutes()
    audit(by, via, "unmute", f"{gid:#x}", was=had)
    return had


def season_preview(gid, value, by="server", via="server"):
    """Preview a lobby season for one character (season.PREVIEW): a season name or alias, "none" for no season,
    "off" to follow the server again. -> what it is now, as a line. Seen on that character's next lobby load."""
    from psu import season
    v = (value or "").strip().lower()
    if v in ("off", "clear", "server"):
        was = season.PREVIEW.pop(gid, "off")
        now_ = "off"
    elif v in ("none", "no"):
        was, now_ = season.PREVIEW.get(gid, "off"), None
        season.PREVIEW[gid] = None
    else:
        now_ = season.name_of(v)
        if now_ is None:
            raise ValueError("season: one of %s, none or off" % ", ".join(season.SEASONS))
        was = season.PREVIEW.get(gid, "off")
        season.PREVIEW[gid] = now_
    audit(by, via, "season_preview", f"{gid:#x}", season=now_ or "none", was=was or "none")
    if now_ == "off":
        return "Season preview off: the server's lobbies again on the next lobby load."
    if now_ is None:
        return "Previewing no season on the next lobby load."
    staged = now_ in season.staged_names(_nbl_dir())
    return "Previewing %s on the next lobby load%s." % (now_, "" if staged else " (nothing staged for it: no change)")


def _nbl_dir():
    from psu.area import NBL_DIR
    return NBL_DIR


def season_status():
    """What the lobbies are dressed for: PSU_SEASON, the running event's season, the result, the staged
    seasons and the previews."""
    from psu import events, season
    return {"mode": season.mode(), "event": events.season(), "current": season.current(),
            "staged": season.staged_names(_nbl_dir()),
            "previews": {label(g, WORLD.online.get(g)): n or "none" for g, n in season.PREVIEW.items()}}


def fmt_season(st):
    return "Season %s (PSU_SEASON=%s%s). Staged: %s.%s" % (
        st["current"] or "none", st["mode"], ", event: %s" % st["event"] if st["event"] else "",
        ", ".join(st["staged"]) or "nothing",
        " Previews: " + ", ".join("%s %s" % kv for kv in st["previews"].items()) if st["previews"] else "")


# --- reports ---------------------------------------------------------------------------------------

def who():
    out = []
    for o in list(WORLD.online.values()):
        out.append({"name": char_name(o), "account": _db().username(o.gid) or "", "gid": f"{o.gid:#x}",
                    "game": o.game, "uni": o.uni, "area": list(o.area) if o.area else None,
                    "party": o.party is not None, "muted": muted(o.gid) is not None})
    return out


def stats():
    per_game, per_uni = {}, {}
    parties = set()
    for o in list(WORLD.online.values()):
        per_game[o.game] = per_game.get(o.game, 0) + 1
        per_uni[str(o.uni)] = per_uni.get(str(o.uni), 0) + 1
        if o.party is not None:
            parties.add(id(o.party))
    return {"uptime_s": int(time.time() - STARTED), "online": len(WORLD.online), "games": per_game,
            "universes": per_uni, "zones": len(WORLD.zones), "parties": len(parties),
            "trades": len({id(t) for t in social.TRADES.values()}), "mutes": len(mutes())}


def spawns(zone):
    """What the server holds for a world zone: players, enemies, ground items, breakable boxes."""
    from psu import combat, drops, instance
    if zone is None:
        return {"zone": None}
    fights = combat.fights.get(zone, {})
    alive = sum(1 for e in fights.values() if not e.dead)
    q, z = combat.zone_ids(zone)
    inst = instance.instances.get(zone)
    try:
        listed = len(combat.roster(zone))
    except Exception:                # noqa: BLE001  no mission data for a lobby
        listed = 0
    return {"zone": f"U{zone[0]} {q}/{z}" + (f" owner {zone[2][1]:#x}" if isinstance(zone[2], tuple) else ""),
            "players": len(WORLD.zones.get(zone, {})), "enemies": len(fights), "alive": alive, "in_set": listed,
            "ground": len(drops.ground.get(zone, {})), "boxes": len(inst.boxes) if inst is not None else 0}


def fmt_stats(s):
    up = s["uptime_s"]
    return (f"Up {up // 3600}h{up % 3600 // 60:02d}m, {s['online']} online "
            + " ".join(f"{g}:{n}" for g, n in sorted(s["games"].items()))
            + " | unis " + " ".join(f"U{u}:{n}" for u, n in sorted(s["universes"].items()))
            + f" | zones {s['zones']} parties {s['parties']} trades {s['trades']} mutes {s['mutes']}")


def fmt_spawns(s):
    if s.get("zone") is None:
        return "Not in a zone."
    return (f"{s['zone']}: {s['players']} player(s), enemies {s['enemies']} spawned / {s['alive']} alive / "
            f"{s['in_set']} in the set data, {s['ground']} ground item(s), {s['boxes']} box(es)")


# --- giving ----------------------------------------------------------------------------------------

def give_item(p, key, qty):
    """In p's session context (in_session): add, save, 0x0106 to p. Returns a reply line."""
    from psu import items
    if p.state is None:
        return "No character state."
    if key not in items.db():
        return f"No item {key} on {p.game}"
    added, err = items.add_item(p.state, key, max(1, min(qty, 99)))
    if err:
        return err
    _db().save_char_state(p.gid, p.slot, p.state)
    WORLD.relay(p, items.c0106_add(p.gid, p.lid, added, game=p.game), f"0x0106 add {key} (gm)")
    return f"Gave {items.db()[key]['name']} x{added['qty']} to {char_name(p)}"


def set_meseta(p, arg):
    from psu import combat
    if p.state is None:
        return "No character state."
    cur = p.state.get("meseta", 0)
    n = cur + int(arg) if arg[0] in "+-" else int(arg)
    p.state["meseta"] = max(0, min(n, 999_999_999))
    _db().save_char_state(p.gid, p.slot, p.state)
    WORLD.relay(p, combat.c0115(p, 0xFFFFFFFF), "0x0115 meseta (gm)")
    return f"{char_name(p)}: meseta {p.state['meseta']}"


# --- moving players --------------------------------------------------------------------------------

def lobby_spot(p):
    """p's (quest, zone, map), entry when it is a lobby other players can be brought to, else None."""
    from psu.area import LOBBY_QUESTS
    if p.area is None or p.zone is None or isinstance(p.zone[2], tuple) or p.area[0] not in LOBBY_QUESTS:
        return None
    return p.area, p.entry


async def bring(p, to, gm_player=None):
    """Send p to `to`'s lobby spot. The zone change runs in p's own session loop (Player.run_job / Player.warp,
    set by game.py: it reads p's fragment acks, which only p's session may do), after this returns; a refusal
    there (p on a mission) is told to gm_player. Returns an error found up front, else None."""
    from psu.area import available, map_for, resolve
    spot = lobby_spot(to)
    if spot is None:
        return f"{char_name(to)} is not in a lobby."
    if p.uni != to.uni:
        return f"{char_name(p)} is in universe {p.uni}, {char_name(to)} in {to.uni}."
    warp, run_job = getattr(p, "warp", None), getattr(p, "run_job", None)
    if warp is None or run_job is None:
        return f"{char_name(p)} cannot be moved now."
    (q, z, m), entry = spot

    def dest():
        qq, zz, mm = resolve(*map_for((q, z, m), p.game, to.game))
        return ((qq, zz, mm), entry) if available(qq, zz) else None
    d = await in_session(p, dest)
    if d is None:
        return f"No archives for {q}/{z} on {p.game}."

    async def job():
        try:
            err = await warp(*d)
        except Exception as e:       # noqa: BLE001  the job runs in p's session: never let it end that session
            err = f"move failed: {e!r}"[:200]
        event(ev="gm_move", gid=f"{p.gid:#x}", to=f"{d[0][0]}/{d[0][1]}/{d[0][2]}", error=err)
        if err and gm_player is not None and gm_player.gid in WORLD.online:
            WORLD.relay(gm_player, c0228_notice(gm_player, err, DIALOG, 5), "0x0228 server")
    run_job(job)
    return None


# --- chat commands ---------------------------------------------------------------------------------

class Ctx:
    """What a chat command needs from the sender's session (game.py)."""
    def __init__(self, sid, gid, me, state, send, save_state, go_zone):
        self.sid, self.gid, self.me, self.state = sid, gid, me, state
        self.send, self.save_state, self.go_zone = send, save_state, go_zone


HELP_ALL = "!who !news !gm <message to the staff>"
HELP_ADMIN = ("!announce !uni !zone !notice !online !stats !spawns !kick !ban !unban !mute !unmute !summon !goto "
              "!give !meseta !level !warp !news !season !gm !clear")


def _int(s, default):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


async def run(ctx, text):
    """A "!" chat command from ctx.me. Replies go to the sender as 0x0228 timed dialogs."""
    from psu import news
    me, gid = ctx.me, ctx.gid
    cmd, args = split(text)
    user = _db().username(gid)
    admin = is_admin(user)
    by = user or f"{gid:#x}"
    event(ev="gm", sid=ctx.sid, user=user, admin=admin, cmd=cmd, args=args[:6])

    async def say(line, seconds=5):
        await ctx.send(c0228_notice(me, line, DIALOG, seconds), "0x0228 server")

    if cmd == "help":
        return await say(HELP_ADMIN if admin else HELP_ALL)
    if cmd == "news" and not (admin and args[:1] == ["reload"]):
        n = news.notices(social.STATE_DIR)
        return await say("\n\n".join(n)[:511] if n else "No news.", 15)
    if cmd == "gm":
        from psu import helpdesk
        _ok, line = helpdesk.ask(gid, char_name(me), " ".join(args))
        return await say(line, 8)
    if cmd == "who" and not admin:
        return await say(f"{len(WORLD.online)} player(s) online.")
    if not admin:
        return await say("Unknown command.")

    if cmd in ("who", "online"):
        rows = who()
        line = ", ".join(f"{r['name']} ({r['account']} {r['game']} U{r['uni']} {r['area'][0] if r['area'] else '-'})"
                         for r in rows)
        return await say(f"{len(rows)} online: {line}"[:511], 10)
    if cmd == "news":                                   # reload
        news._cache.clear()
        n = news.notices(social.STATE_DIR)
        audit(by, "chat", "news_reload", notices=len(n))
        return await say(f"News: {len(n)} notice(s).")
    if cmd == "stats":
        return await say(fmt_stats(stats()), 8)
    if cmd == "spawns":
        return await say(fmt_spawns(spawns(me.zone)), 8)
    if cmd in ("announce", "uni", "zone", "notice") and args:
        line = " ".join(args)
        scope = {"uni": ("uni", me.uni), "zone": ("zone", me.zone)}.get(cmd)
        n = broadcast(line, scope, DIALOG if cmd == "notice" else TICKER, 10 if cmd == "notice" else 0, by, "chat")
        return await say(f"Sent to {n}.", 3) if cmd != "announce" or n == 0 else None

    if cmd in ("kick", "ban", "unban", "mute", "unmute", "summon", "goto") and args:
        target, p = find_player(args[0])
        if target is None:
            return await say(f"No such player: {args[0]}")
        name = label(target, p)
        if cmd == "unban":
            ok = _db().unban(target)
            audit(by, "chat", "unban", f"{target:#x}", was=ok)
            return await say("Unbanned." if ok else "Not banned.")
        if cmd == "ban":
            hours = float(args[1]) if len(args) > 1 and args[1].replace(".", "", 1).isdigit() else 24
            reason = " ".join(args[2:])[:200]
            _db().ban(target, ban_until(hours), reason, user or "")
            audit(by, "chat", "ban", f"{target:#x}", hours=hours, reason=reason)
            if p is not None and p is not me:
                kick(target, reason or "banned", by, "chat")
            return await say(f"ban {name}: done" + ("" if p else " (offline)"))
        if cmd == "kick":
            if p is None or p is me:
                return await say(f"{name} is not online." if p is None else "Not yourself.")
            kick(target, " ".join(args[1:])[:200], by, "chat")
            return await say(f"kick {name}: done")
        if cmd == "mute":
            minutes = _int(args[1], 60) if len(args) > 1 else 60
            mute(target, minutes, " ".join(args[2:]), by, "chat")
            return await say(f"Muted {name}" + (f" for {minutes} min." if minutes > 0 else "."))
        if cmd == "unmute":
            return await say(f"Unmuted {name}." if unmute(target, by, "chat") else f"{name} was not muted.")
        if p is None:
            return await say(f"{name} is not online.")
        if p is me:
            return await say("Not yourself.")
        mover, to = (p, me) if cmd == "summon" else (me, p)
        err = await bring(mover, to, me)
        audit(by, "chat", cmd, f"{target:#x}", error=err)
        return await say(err) if err else None

    if cmd == "season":
        if not args:
            return await say(fmt_season(season_status())[:511], 10)
        try:
            return await say(season_preview(gid, args[0], by, "chat"))
        except ValueError as e:
            return await say(str(e)[:511], 10)

    if cmd == "give" and args and ctx.state is not None:
        key = args[0].lower().rjust(8, "0")
        qty = _int(args[1], 1) if len(args) > 1 else 1
        p = me
        if len(args) > 2:
            _, p = find_player(" ".join(args[2:]))
            if p is None:
                return await say(f"{args[2]} is not online.")
        line = await in_session(p, give_item, p, key, qty)
        audit(by, "chat", "give", f"{p.gid:#x}", item=key, qty=qty, result=line)
        return await say(line)
    if cmd == "meseta" and args and args[0].lstrip("+-").isdigit():
        p = me
        if len(args) > 1:
            _, p = find_player(" ".join(args[1:]))
            if p is None:
                return await say(f"{args[1]} is not online.")
        line = await in_session(p, set_meseta, p, args[0])
        audit(by, "chat", "meseta", f"{p.gid:#x}", amount=args[0], result=line)
        return await say(line)
    if cmd == "level" and args and args[0].isdigit() and ctx.state is not None:
        from psu import progression
        from psu.area import NO_ENEMY, c010d_char
        st = ctx.state
        st["level"] = max(1, min(int(args[0]), progression.level_cap()))
        st["exp"] = progression.exp_for_level(me.raw, st["level"])
        st["hp"] = progression.derived(me.raw, st)["max_hp"]
        ctx.save_state()
        await ctx.send(c010d_char(gid, me.raw, me.area, me.entry, lid=me.lid, st=st, source=NO_ENEMY),  # level-up shown
                       "0x010d level (gm)")
        audit(by, "chat", "level", f"{gid:#x}", level=st["level"])
        return await say(f"Level {st['level']}")
    if cmd == "warp" and len(args) >= 3 and all(a.isdigit() for a in args[:4]):
        from psu.area import available, resolve
        q, z, m = (int(a) for a in args[:3])
        e = int(args[3]) if len(args) > 3 else 0
        q, z, m = resolve(q, z, m)
        if not available(q, z):
            return await say(f"No archives for {q}/{z}")
        audit(by, "chat", "warp", f"{gid:#x}", to=f"{q}/{z}/{m}", entry=e)
        return await ctx.go_zone((q, z, m), e)
    return await say("Unknown command. !help")
