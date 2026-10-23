# Development

## Game data for a local run

The checks and a hand-started server read the game data built from your discs ([data.md](data.md)).
Point them at it in one of these ways:

- `PSU_DATA=<dir>`: the directory `python -m extract` wrote (`nbl/`, `gamedata/`, `captures/`). The
  test player reads this layout; for the services and the other checks set `PSU_NBL_DIR=$PSU_DATA/nbl`,
  `PSU_GAMEDATA=$PSU_DATA/gamedata` and `PSU_CAPTURE_DIR=$PSU_DATA/captures`.
- `PSU_WORK=<dir>`: a work directory in the older layout (`lobby-nbl/ps2/`, `psu/gamedata/`,
  `aoti/gamedata/`, `ref/egs/p/`); default `work/` next to the repository checkout. Every path has its
  own variable that wins over it.

## Checks without a console

```sh
python tools/xgame_check.py                         # packet digests and checks, both discs (seconds)
python tools/testplay.py --game both --scenario all # scripted players against a local server (minutes)
python tools/testplay.py --scenario mission --game aoti --keep --verbose   # one scenario, keep the logs
python tools/season_check.py                        # seasonal lobbies leave everything else byte-identical
python tools/panel_check.py                         # the OpenLobby panel port, events page, help desk
python tools/judge_scan.py                          # every quest rule in missions.json against psu/judge.py
```

- **`tools/xgame_check.py`** builds packets from fixed test players with the server's own modules and
  compares them with fixed digests (`--print` shows the current ones). PSU -> PSU and AotI -> AotI bytes
  must not change unless a change is meant to change them; the AotI -> PSU translations are checked
  structurally. `PSU_GAMEDATA` picks the tables.
- **`tools/testplay.py`** starts `psu.login` and `psu.game` as subprocesses on 127.0.0.1 with a throwaway
  state and log directory, then drives bots through the client's real sequences: login, lobbies, shops,
  grinding, synthesis, My Room, missions, parties, techniques, the AotI story, chat, a PSU <-> AotI trade,
  a mixed party, GM commands and the board. Each scenario checks the server's replies and fails on
  handler errors or fragment-ack timeouts in `events.jsonl`. `--list` lists the scenarios, `--json FILE`
  writes the report. A scenario that does not apply to a disc (or whose staged data is absent) reports
  SKIP. On a machine where a server already runs, `--ports 30200` puts game and admin on 30200 / 30201
  and leaves login out (the bots take their keys from the database).

Run both before sending a change; a pull request says what they printed.

## Running against PCSX2 or a PS2

The setup program does all of this for you ([hosting.md](hosting.md)). By hand:

1. Build the data and start patch, login and game (below), with `PSU_GAME_ADDR` set to the address the
   console reaches this machine on.
2. Put an SSLv3 terminator in front of login and game: `compose/stunnel-psu.conf` (PSU) and
   `compose/stunnel-aoti.conf` (AotI) in an SSLv3-capable stunnel ([hosting.md](hosting.md)). Their
   `connect` lines name the compose services; outside compose, point them at your login (22110, 22140)
   and game (22061) ports.
3. DNS: `python tools/devdns.py --answer <your LAN address>` (port 53, so as administrator / root) and
   set the console's or PCSX2's primary DNS to this machine. It answers the four game names and refuses
   everything else, so the client never reaches Sega's old addresses.
4. Boot the disc with the DNAS bypass ([patches/](../patches/README.md)).

```sh
cd server
export PSU_NBL_DIR=<data>/nbl PSU_GAMEDATA=<data>/gamedata PSU_CAPTURE_DIR=<data>/captures
export PSU_LOG_DIR=$PWD/../data/logs                # state goes next to it, in data/state
export PSU_GAME_ADDR=<your LAN address>             # the address the console reaches this machine on
export PSU_ADMINS=<your login ID>                   # GM commands for you
python -m psu.patch &
python -m psu.login &
python -m psu.game &
```

PCSX2 notes: under DEV9 `ModeDNS=Manual` the hosts table is ignored, so DNS is the only redirect. The
Sockets backend loses data from large bursts, which is one reason the server paces large sends. After a
network error, restart PCSX2 completely before reconnecting.

Real PS2s are stricter than PCSX2: a malformed archive or packet that the emulator tolerates can freeze
the console. Check a new archive or layout on hardware before relying on it.

## Seeing what the client sends

- `PSU_LOG_DIR/sessions/` holds a hexdump per session (login's password field is blanked) and
  `events.jsonl` one line per event, including every handler error with its traceback location.
- `python -m psu.observe` listens on the public ports and only records, for traffic nothing handles yet.
- `tools/bot.py` is a second player for a single real client (it walks, talks, or replays a recorded
  movement sequence from `events.jsonl`).

## Code layout and style

- Standard library only (the image has no pip step). Python 3.12.
- One module per feature under `server/psu/`; the dispatcher in `game.py` stays thin.
- Module docstrings are the protocol documentation: when a handler learns a new field, the docstring
  says what it is and which client routine reads it ([protocol.md](protocol.md)).
- Anything that differs per disc keys on the session's game (`games.current()`) or the recipient's game,
  never on a guess; add an `xgame_check` or `testplay` check for it ([discs.md](discs.md)).
- `server/psu/olport.py` is OpenLobby's published panel helper, kept identical to upstream: change it
  there, not here.
- No game data in tests: checks read the host's local data or skip.
