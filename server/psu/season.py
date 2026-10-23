"""Seasonal lobbies and event drops, per disc. Off unless PSU_SEASON says otherwise (default "none").

What the discs ship (identical on PSU and AotI): 16 decoration archives XF_OB_<NAME>.NBL, each the models of one
season's "extra" objects (nbl_obj_extra_filelist.rel + obj_param.snr, object ids 3500..3575), and 7 seasonal
city BGMs (XA_CITY_MARRIAGE/CARNIVAL/NEWYEAR/VALENTINE/BLOSSOM/HALLOWEEN/CHRISTMAS: sound.play_bgm ids 44..52 in
the client's {id, name} table, PSU 0x53b7b0, AotI 0x55bae0; the list at PSU 0x595860 is My Room's). The season
id is the index into the client's name table (PSU 0x57a070, AotI 0x5d5510): SEASONS below.
The archives hold no placements: the lobby's set file places the objects (static models, set object type 4,
model = object id - 3000, the numbering the disc stages use: 51/52 are ids 3051/3052).

How the client picks the season (static reading):
  PSU   zone entry 0x1c3930 reads the quest NBL's zone record (quest.snr main block +0xC, 0x40-byte records keyed by
        zone id, 0x1e0c50) and loads xf_ob_<n>.nbl when record +0xB = n is 0..15 (0xde980; 0xFF = none). Its
        0x020F handler (0x1e55f0) reads only the set nb at +0x2C.
  AotI  0x020F (0x1e60f0) keeps +0x2D (s8) as the season (0x1e62b0, cached by the online session, vtable 0x656c00
        +0x19c), zone entry 0x1c3030 loads xf_ob_<n>.nbl from it (0xd7e90). The zone record byte is not read.
  both  0x0205 +0x43 = 1 sets the "seasonal" flag (PSU 0x8c810, 0x619884 bit 0); the script call
        obj.get_eventflag returns the season id while it is set, else -1 (PSU 0x2466d0, AotI 0x24acf0). The
        disc's (offline) lobby scripts never call it and always play 40 (GUARDIANS_CITY), so extract/seasonbuild.py
        sets that play_bgm operand to the season's track in each staged Colony script (SEASON_BGM there).
So a season needs, per lobby zone: the season id where that disc reads it, and a set file with the season's
objects. This module sends the id (0x020F +0x2D, 0x0205 flag; area.py) and serves the season's staged archives
(the quest NBL with the zone byte for PSU, the zone NBL with the placements for both) from
$PSU_SEASON_DIR/<name>/ (default $PSU_NBL_DIR/season-staging/<name>/, built by extract/seasonbuild.py with the
other NBLs). A lobby zone with no staged zone NBL for the session's disc gets nothing:
the bytes stay as without a season.

  PSU_SEASON=none (default) | auto (CALENDAR, server local date) | <name> (one of SEASONS or ALIASES)
  PSU_SEASON_DROP=40   with a season on: 1 in N enemy kills that rolled nothing drop one of the season's items
                       (EVENT_ITEMS, only ids the session's disc has); 0 = no event drops.
A running event in the host's calendar (psu/events.py) with a "season" wins over PSU_SEASON, and its
"event_drop" over PSU_SEASON_DROP. A preview (PREVIEW, gm.season_preview: GM !season, the admin
"season" command, the panel) wins over both for one character's lobbies, until it is turned off or the
service restarts; it changes nothing for anyone else and no drops.
"""
import datetime
import os
import random

from psu import games

SEASONS = ("party", "newyear", "valentine", "whiteday", "cherryblossoms", "easter", "parumfestival", "sonic",
           "neudaizfestival", "fireworks", "redleaves", "halloween", "moatoobfestival", "christmas", "snow",
           "wedding")
ALIASES = {"spring": "cherryblossoms", "autumn": "redleaves", "winter": "snow", "anniversary": "party",
           "summer": "fireworks"}

# Retail-like windows (month, day) inclusive, first match wins. Easter is computed (Easter Sunday -7 .. +7) and
# checked first. The planet festivals (parum/neudaiz/moatoob) and fireworks have no placement data for the
# lobbies served here, so auto never picks them; PSU_SEASON=<name> still can.
CALENDAR = (
    ((1, 1), (1, 14), "newyear"),
    ((1, 15), (2, 6), "snow"),
    ((2, 7), (2, 20), "valentine"),
    ((3, 7), (3, 20), "whiteday"),
    ((3, 25), (4, 20), "cherryblossoms"),
    ((6, 1), (6, 14), "wedding"),
    ((6, 16), (6, 30), "sonic"),
    ((9, 22), (10, 14), "redleaves"),
    ((10, 15), (10, 27), "party"),         # PSU's US launch, 2006-10-24
    ((10, 28), (11, 7), "halloween"),
    ((12, 12), (12, 26), "christmas"),
    ((12, 27), (12, 30), "snow"),
    ((12, 31), (12, 31), "newyear"),
)

# Holiday items both item tables carry and no drop table or shop gives (they were event handouts). Not a retail
# drop list: one per season where the disc has a fitting item.
EVENT_ITEMS = {
    "valentine": ("03030400", "03030500"),          # Chocolat, Gourmet Chocolat
    "whiteday": ("03030900", "03030a00"),           # Milkina Candy, Specina Candy
    "sonic": ("03031d00", "0b013f00"),              # Sonic Gum, Sonic (room goods)
    "easter": ("0b010400",),                        # Easter Egg
    "wedding": ("0b010c00",),                       # Wedding Cake
    "fireworks": ("0b010600",),                     # Fireworks (room goods)
    "halloween": ("0b014400",),                     # Cursed Pumpkin
    "christmas": ("0b010b00",),                     # Christmas Tree
    "snow": ("0b010100",),                          # Snowman
}


def easter(year):
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    n = h + el - 7 * m + 114
    return datetime.date(year, n // 31, n % 31 + 1)


def by_date(day):
    """The CALENDAR season of a date, or None."""
    e = easter(day.year)
    if abs((day - e).days) <= 7:
        return "easter"
    md = (day.month, day.day)
    return next((name for lo, hi, name in CALENDAR if lo <= md <= hi), None)


def name_of(value):
    v = (value or "").strip().lower()
    v = ALIASES.get(v, v)
    return v if v in SEASONS else None


PREVIEW = {}                    # gid -> season name, or None for "no season" (gm.season_preview)


def current(today=None, gid=None):
    """The active season's name, or None (PSU_SEASON); for `gid`, its preview first."""
    from psu import events
    if gid is not None and gid in PREVIEW:
        return PREVIEW[gid]
    ev = events.season()
    if ev is not None:
        return name_of(ev)
    mode = (os.environ.get("PSU_SEASON") or "none").strip().lower()
    if mode in ("", "none", "off", "0"):
        return None
    if mode == "auto":
        return by_date(today or datetime.date.today())
    return name_of(mode)


def mode():
    """PSU_SEASON as current() reads it: "none", "auto" or a season name (None: a name it does not know)."""
    m = (os.environ.get("PSU_SEASON") or "none").strip().lower()
    if m in ("", "none", "off", "0"):
        return "none"
    return "auto" if m == "auto" else name_of(m)


def auto_windows(since, until):
    """The CALENDAR seasons (PSU_SEASON=auto) whose days touch [since, until), unix seconds:
    [(name, start, end)], each run whole (from the midnight it starts to the one after its last day).
    Days are UTC, the date the server's container keeps."""
    utc = datetime.timezone.utc
    day = datetime.datetime.fromtimestamp(since, utc).date() - datetime.timedelta(days=40)
    last = datetime.datetime.fromtimestamp(until, utc).date() + datetime.timedelta(days=40)
    out, run = [], None
    while day <= last:
        name = by_date(day)
        if run and run[0] != name:
            out.append(run)
            run = None
        if name and run is None:
            run = [name, day, day]
        elif name:
            run[2] = day
        day += datetime.timedelta(days=1)
    if run:
        out.append(run)

    def midnight(d):
        return int(datetime.datetime(d.year, d.month, d.day, tzinfo=utc).timestamp())
    # the scan starts and ends 40 days out, past the longest run, so every run kept is whole
    rows = [(n, midnight(a), midnight(b + datetime.timedelta(days=1))) for n, a, b in out]
    return [r for r in rows if r[2] > since and r[1] < until]


def season_id(name):
    return SEASONS.index(name)


def season_dir(nbl_dir):
    return os.environ.get("PSU_SEASON_DIR") or os.path.join(nbl_dir, "season-staging")


def staged(nbl_dir, name, fname, game=None):
    """The season's copy of an archive, at the same relative place as the one the disc would get
    (games.data_file: <dir>/aoti/ for an AotI-only file, else <dir>/), or None."""
    normal = games.data_file(nbl_dir, fname, game)
    rel = os.path.relpath(normal, nbl_dir)
    p = os.path.join(season_dir(nbl_dir), name, rel)
    return p if os.path.exists(p) else None


def staged_names(nbl_dir):
    """The seasons with anything staged, in SEASONS order."""
    d = season_dir(nbl_dir)
    return [n for n in SEASONS if os.path.isdir(os.path.join(d, n))]


def for_zone(nbl_dir, q, z, game=None, today=None, gid=None):
    """(season id, quest NBL path or None, zone NBL path) when the active season (for `gid`: its preview first)
    has a staged zone NBL for this lobby zone and disc, else None."""
    name = current(today, gid)
    if name is None:
        return None
    zp = staged(nbl_dir, name, f"zone-{q}-{z}.nbl", game)
    if zp is None:
        return None
    return season_id(name), staged(nbl_dir, name, f"quest-{q}.nbl", game), zp


def event_drop(rnd=random.randrange, today=None, game=None):
    """("item", id) for a kill that rolled nothing, at 1 in PSU_SEASON_DROP (or a running event's event_drop,
    psu/events.py) while a season is on; else None."""
    from psu import events
    name = current(today)
    ids = EVENT_ITEMS.get(name or "")
    try:
        rate = events.event_drop_rate(game) or int(os.environ.get("PSU_SEASON_DROP", "40"))
    except ValueError:
        rate = 0
    if not ids or rate <= 0 or rnd(rate) != 0:
        return None
    have = games.json_data("items.json")
    ids = [i for i in ids if i in have] if have else list(ids)
    return ("item", ids[rnd(len(ids))]) if ids else None
