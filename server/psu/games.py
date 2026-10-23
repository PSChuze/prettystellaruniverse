"""Which disc a session runs: "psu" (SLUS-21194) or "aoti" (Ambition of the Illuminus, SLUS-21631).

Both log in on their own port (login.py GAME) and share one game server and one world. The game
comes with the login key (db.take_key) and is set for the session's task on 0x020D, so code that
runs for that session reads it with current() without passing it around.

The platform ("ps2", "pc") comes with the key the same way (login.py PLATFORM, per login port) and is
set with set_platform(); read it with platform(). Everyone shares the one world; per-platform packet
differences hang off this.

Per-game data: client-side archives (zone/quest NBLs) differ between the discs, so an AotI session
looks in $PSU_NBL_DIR/aoti/ first and falls back to the shared (PSU-built) file; the disc-derived tables
($PSU_GAMEDATA: items, enemies, drops, levels, shops ...) the same way via json_data().
"""
import contextvars
import json
import os

PSU, AOTI = "psu", "aoti"
PS2, PC = "ps2", "pc"
# Default home of the data built from the discs for a local run: $PSU_WORK, else the work/ directory next to the
# repository checkout. Each path below has its own variable that wins over this one (the image sets them all).
WORK = os.environ.get("PSU_WORK") or os.path.join(os.path.dirname(__file__), "..", "..", "..", "work")
GAMEDATA = os.environ.get("PSU_GAMEDATA", os.path.join(WORK, "psu", "gamedata"))
_JSON = {}
_GAME = contextvars.ContextVar("game", default=PSU)
_PLATFORM = contextvars.ContextVar("platform", default=PS2)


def current():
    return _GAME.get()


def set_current(game):
    _GAME.set(game if game in (PSU, AOTI) else PSU)


def platform():
    return _PLATFORM.get()


def set_platform(name):
    _PLATFORM.set(name if name in (PS2, PC) else PS2)


def data_file(root, name, game=None):
    """root/<game>/name when the game has its own copy, else root/name."""
    game = game or current()
    if game != PSU:
        own = os.path.join(root, game, name)
        if os.path.exists(own):
            return own
    return os.path.join(root, name)


def json_data(name, game=None, required=False):
    """$PSU_GAMEDATA/<game>/<name> when that disc has its own table, else the shared one; cached per game.
    Missing: {} (or FileNotFoundError when required)."""
    game = game or current()
    key = (game, name)
    if key not in _JSON:
        path = data_file(GAMEDATA, name, game)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                _JSON[key] = json.load(f)
        elif required:
            raise FileNotFoundError(path)
        else:
            _JSON[key] = {}
    return _JSON[key]
