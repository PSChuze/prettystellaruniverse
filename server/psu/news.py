"""Server news: notices from a text file, shown at login (0x0225 MOTD), on a rotating ticker and on demand.

  $PSU_NEWS_FILE (default $PSU_STATE_DIR/news.txt), re-read whenever it changes:

      # comment lines start with '#'
      Maintenance Sunday 18:00 UTC, about one hour.
      ---
      Ambition of the Illuminus players share the world with PSU players.

  Notices are separated by a line holding only '---'. Without the file (or with no notice in it) the
  login MOTD is PSU_MOTD as before and nothing rotates.

Where the client shows them (both discs, statically read):
  login   0x0225 pages (handler PSU 0xd9fd0 / AotI 0xd3460) fill the title flow's notice box (the 0x226/0x23F
          page requests come from its state machine 0x38b110, part of the login screens only): every notice,
          wrapped to the box's ~50 characters a line.
  in game there is no notice window outside the login flow; notices go out as 0x0228 (PSU 0xd9f00 /
          AotI 0xd3390, same layout): type 1 top ticker for the welcome line a few seconds after the lobby
          loads and for the rotation (one notice every PSU_NEWS_TICKER_SECS, default 1800, 0 = off), type 3
          timed dialog for "!news" (psu/gm.py).
"""
import asyncio
import os
import textwrap
import time

WIDTH = 50                           # the login notice box fits about 50 characters a line
TICKER_SECS = float(os.environ.get("PSU_NEWS_TICKER_SECS", "1800"))
WELCOME_DELAY = 6.0                  # seconds after the lobby sequence: the HUD must be up for a ticker
_cache = {}                          # path -> (mtime, notices)


def path(state_dir):
    return os.environ.get("PSU_NEWS_FILE") or os.path.join(state_dir or ".", "news.txt")


def parse(text):
    notices, cur = [], []
    for line in text.splitlines():
        if line.strip() == "---":
            notices.append(cur)
            cur = []
        elif not line.lstrip().startswith("#"):
            cur.append(line.rstrip())
    notices.append(cur)
    return ["\n".join(n).strip("\n") for n in notices if "".join(n).strip()]


def notices(state_dir):
    """The notices in the news file, then a line per running or soon starting event (psu/events.py)."""
    from psu import events
    return file_notices(state_dir) + events.notices()


def file_notices(state_dir):
    """The notices in the news file, newest edit picked up (cached by mtime). [] without a file."""
    p = path(state_dir)
    try:
        mtime = os.stat(p).st_mtime
    except OSError:
        _cache.pop(p, None)
        return []
    hit = _cache.get(p)
    if hit is None or hit[0] != mtime:
        try:
            with open(p, encoding="utf-8") as f:
                hit = (mtime, parse(f.read()))
        except (OSError, UnicodeDecodeError):
            hit = (mtime, [])
        _cache[p] = hit
    return hit[1]


def wrap(text, width=WIDTH):
    out = []
    for line in text.split("\n"):
        out += textwrap.wrap(line, width) or [""]
    return out


def motd(state_dir, default):
    """Login MOTD text: every notice, wrapped for the box, a blank line between; else `default`."""
    n = notices(state_dir)
    if not n:
        return default
    lines = []
    for i, text in enumerate(n):
        if i:
            lines.append("")
        lines += wrap(text)
    return "\n".join(lines)


def current(state_dir, period=None, now=None):
    """The notice the rotation is on (one per `period` seconds), or None."""
    n = notices(state_dir)
    if not n:
        return None
    period = period or TICKER_SECS or 1800
    return n[int((now if now is not None else time.time()) // period) % len(n)]


def one_line(text, limit=500):
    """0x0228 carries up to 511 UTF-16 units; the ticker is one line."""
    return " ".join(text.split())[:limit]


async def ticker(state_dir, broadcast):
    """Every TICKER_SECS: the current notice to everyone online as a ticker (broadcast(text))."""
    if TICKER_SECS <= 0:
        return
    while True:
        await asyncio.sleep(TICKER_SECS)
        text = current(state_dir)
        if text:
            broadcast(one_line(text))
