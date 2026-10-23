"""The game data build's steps (extract/build.py) in run order, with plain names for
the progress line. A step this list does not know is still counted, by its own name."""

GAMES = {"psu": "PSU", "aoti": "AotI"}
# The per-disc steps, in the order Build.steps runs them for each disc.
PER_GAME = ("gamedata", "enemies", "missions", "drops", "economy", "items", "pa", "parts", "types")
PLAIN = {"disc": "unpacking the disc",
         "gamedata": "items, shops and weapons",
         "enemies": "enemies",
         "missions": "missions and counters",
         "drops": "drop tables",
         "economy": "grinding, synthesis and Lumilass",
         "items": "the item list",
         "pa": "photon arts",
         "parts": "character parts",
         "types": "type levels",
         "lobbies": "lobbies",
         "egs": "the Colony lobby quests (EGS)",
         "seasons": "the seasonal decorations"}
# The steps that take more than a few seconds.
ROUGH = {"aoti-missions": "about 2 minutes"}


def step_order(games):
    """The step names for these discs ("psu", "aoti"), in the order the build runs them."""
    games = [g for g in ("psu", "aoti") if g in games]
    names = ["%s-disc" % g for g in games]
    for g in games:
        names += ["%s-%s" % (g, s) for s in PER_GAME if s != "items" or g == "aoti"]
    names.append("types")
    names += ["%s-lobbies" % g for g in games]
    return names + ["egs", "seasons"]


def plain_name(name):
    """"AotI missions and counters" for aoti-missions."""
    game, _, step = name.partition("-")
    if game in GAMES and step:
        return "%s %s" % (GAMES[game], PLAIN.get(step, step))
    return PLAIN.get(name, name)


def rough_time(name):
    return ROUGH.get(name, "")
