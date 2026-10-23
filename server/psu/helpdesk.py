"""Players' help requests: "!gm <message>" in chat reaches the staff, staff answer from the OpenLobby panel's
Inbox (psu/adminport.py, olport help_desk), and the answer shows in game.

  !gm <message>     opens a request, or adds to the one still open (one open request per account)
  !gm               the request's state and the latest staff answer

A staff answer is a timed dialog (S>C 0x0228 type 3) "Staff: <text>" at once when the player is online, else on
their next lobby load (deliver(), from game.py next to admin.welcome); it is marked delivered once sent. Closing a
request tells an online player it was closed; a later !gm opens a new one. Requests live in psu.db (help_threads,
help_messages), so they survive restarts. A player may send MAX_PER_WINDOW messages per WINDOW seconds, each at
most TEXT_MAX characters. Every request, answer and close is an event (ev "help").

Everything here touches the store and the world, so it runs on the game's event loop (adminport calls it there).
"""
import asyncio
import time

from psu import gm
from psu.observe import event
from psu.world import WORLD

TEXT_MAX = 400
SUBJECT_MAX = 60
WINDOW, MAX_PER_WINDOW = 600, 5
DELIVER_DELAY = 8.0                 # seconds after the lobby load, after the news welcome
DIALOG_SECS = 15
LIST_MAX = 200


def _c():
    return gm._db().c


def _clean(text, limit):
    return " ".join(str(text or "").split())[:limit]


def open_thread(gid):
    row = _c().execute("SELECT id FROM help_threads WHERE gid = ? AND closed = 0 ORDER BY id DESC LIMIT 1",
                       (gid,)).fetchone()
    return row[0] if row else None


def ask(gid, name, text, now=None):
    """A player's message -> (ok, the line to show them)."""
    now = time.time() if now is None else now
    text = _clean(text, TEXT_MAX)
    if not text:
        return False, status(gid)
    sent = _c().execute("SELECT count(*) FROM help_messages m JOIN help_threads t ON t.id = m.thread "
                        "WHERE t.gid = ? AND m.sender = 'player' AND m.at > ?", (gid, now - WINDOW)).fetchone()[0]
    if sent >= MAX_PER_WINDOW:
        return False, "You have sent the staff several messages already. Please wait a few minutes."
    tid = open_thread(gid)
    if tid is None:
        tid = _c().execute("INSERT INTO help_threads (gid, name, subject, created, updated) VALUES (?, ?, ?, ?, ?)",
                           (gid, name, _clean(text, SUBJECT_MAX), now, now)).lastrowid
    else:
        _c().execute("UPDATE help_threads SET updated = ?, waiting = 1, name = ? WHERE id = ?", (now, name, tid))
    _c().execute("INSERT INTO help_messages (thread, sender, text, at, delivered) VALUES (?, 'player', ?, ?, 1)",
                 (tid, text, now))
    event(ev="help", act="ask", thread=tid, gid=f"{gid:#x}", name=name, chars=len(text))
    return True, "Sent to the staff (request #%d). Their answer will show up here." % tid


def status(gid):
    tid = open_thread(gid)
    if tid is None:
        return "To reach the staff, type !gm and your message."
    row = _c().execute("SELECT text FROM help_messages WHERE thread = ? AND sender = 'staff' ORDER BY id DESC LIMIT 1",
                       (tid,)).fetchone()
    if row is None:
        return "Request #%d is open; the staff have not answered yet." % tid
    return ("Request #%d, latest answer: %s" % (tid, row[0]))[:511]


def _show(p, text):
    WORLD.relay(p, gm.c0228_notice(p, text[:511], gm.DIALOG, DIALOG_SECS), "0x0228 help answer")


def deliver(gid):
    """Show `gid` its undelivered staff answers if it is online; -> how many were shown."""
    p = WORLD.online.get(gid)
    if p is None:
        return 0
    rows = _c().execute("SELECT m.id, m.text FROM help_messages m JOIN help_threads t ON t.id = m.thread "
                        "WHERE t.gid = ? AND m.sender = 'staff' AND m.delivered = 0 ORDER BY m.id", (gid,)).fetchall()
    for mid, text in rows:
        _show(p, "Staff: " + text)
        _c().execute("UPDATE help_messages SET delivered = 1 WHERE id = ?", (mid,))
    if rows:
        event(ev="help", act="delivered", gid=f"{gid:#x}", count=len(rows))
    return len(rows)


def deliver_later(p, delay=None):
    """Undelivered answers a few seconds after p's lobby load (game.py)."""
    def fire():
        if WORLD.online.get(p.gid) is p:
            deliver(p.gid)
    asyncio.get_running_loop().call_later(DELIVER_DELAY if delay is None else delay, fire)


# --- the panel's Inbox (olport help_desk) ------------------------------------------------------------

def threads(open_only=False):
    q = ("SELECT t.id, t.subject, t.name, t.updated, t.closed, t.waiting, count(m.id), max(m.at) FROM help_threads t "
         "LEFT JOIN help_messages m ON m.thread = t.id %s GROUP BY t.id ORDER BY t.updated DESC LIMIT ?"
         % ("WHERE t.closed = 0" if open_only else ""))
    # "player" is the character name, the id the panel's player picker uses (adminport.character_picks)
    return [{"id": str(i), "subject": s, "who": n, "player": n, "at": int(u), "last": int(la or u),
             "closed": bool(c), "waiting": bool(w) and not c, "count": k}
            for i, s, n, u, c, w, k, la in _c().execute(q, (LIST_MAX,)).fetchall()]


def _tid(tid):
    try:
        return int(str(tid))
    except ValueError:
        return None


def thread(tid):
    t = _tid(tid)
    row = t is not None and _c().execute("SELECT id, gid, name, subject, closed FROM help_threads WHERE id = ?",
                                         (t,)).fetchone()
    if not row:
        return None
    i, gid, name, subject, closed = row
    msgs = [{"from": s, "subject": subject if n == 0 else "", "text": x, "at": int(a)}
            for n, (s, x, a) in enumerate(_c().execute(
                "SELECT sender, text, at FROM help_messages WHERE thread = ? ORDER BY id", (i,)).fetchall())]
    return {"id": str(i), "subject": subject, "closed": bool(closed),
            "who": {"name": name, "online": gid in WORLD.online}, "messages": msgs}


def reply(tid, text, close, by, now=None):
    """A staff answer -> (ok, message for the panel)."""
    now = time.time() if now is None else now
    t = _tid(tid)
    row = t is not None and _c().execute("SELECT gid, closed FROM help_threads WHERE id = ?", (t,)).fetchone()
    if not row:
        return False, "That request is gone."
    gid, closed = row
    text = _clean(text, TEXT_MAX)
    if not text:
        return False, "The answer is empty."
    _c().execute("INSERT INTO help_messages (thread, sender, text, at, by) VALUES (?, 'staff', ?, ?, ?)",
                 (t, text, now, by))
    _c().execute("UPDATE help_threads SET updated = ?, waiting = 0, closed = ? WHERE id = ?",
                 (now, 1 if close or closed else 0, t))
    shown = deliver(gid)
    event(ev="help", act="reply", thread=t, by=by, closed=bool(close), shown=bool(shown))
    where = "shown in game now" if shown else "shown when they next enter a lobby"
    return True, "Answered (%s)%s." % (where, " and closed" if close else "")


def close(tid, by):
    t = _tid(tid)
    row = t is not None and _c().execute("SELECT gid, closed FROM help_threads WHERE id = ?", (t,)).fetchone()
    if not row:
        return False, "That request is gone."
    if row[1]:
        return True, "It was already closed."
    _c().execute("UPDATE help_threads SET closed = 1, waiting = 0, updated = ? WHERE id = ?", (time.time(), t))
    p = WORLD.online.get(row[0])
    if p is not None:
        _show(p, "The staff closed your request #%d. Type !gm to send a new one." % t)
    event(ev="help", act="close", thread=t, by=by)
    return True, "Closed."
