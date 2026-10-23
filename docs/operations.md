# Operations

Running a server day to day: settings, data, accounts, GM commands, news, events, seasons, the help
desk and the admin panel. Installing and exposing a server (Docker, compose, DNS, ports, the SSLv3
terminator, backups) is in [hosting.md](hosting.md). Values in `<angle brackets>` are yours to choose.

Commands below run on the server machine. With the compose stack, run the tools from the repository
checkout with `PSU_DATA` set to the data directory (the setup program's menu has the common ones).

## Settings

All settings are environment variables (`.env` in the server folder, passed through the compose file; every one is described in
[psu.env.example](../psu.env.example)). After a change,
recreate the containers (`docker compose ... up -d`); `docker restart` keeps the old environment.

| variable | default | what |
|---|---|---|
| `PSU_GAME_ADDR` | 127.0.0.1 | IPv4 address login hands out for the game server: the address consoles reach the server on |
| `PSU_GAME_PORT` | 12061 | game port login hands out (the ssl3 side) |
| `PSU_AUTOREGISTER` | 1 | first login with an unknown ID creates the account |
| `PSU_LOGIN_FAIL_LIMIT` | 8 | failed logins per account name in 10 minutes before it is refused |
| `PSU_UNIVERSES` | `1:Universe 1,2:Universe 2,3:Universe 3` | the Uni-cube list |
| `PSU_ADMINS` | empty | login IDs that get GM chat commands |
| `PSU_ADMIN_PORT` / `PSU_ADMIN_TOKEN` | 22190 / generated | loopback admin port (0 = off) and its token |
| `PSU_MOTD` | a welcome line | login MOTD while `news.txt` has no notice |
| `PSU_NEWS_TICKER_SECS` | 1800 | news ticker interval (0 = off) |
| `PSU_EVENTS_FILE` | `state/events.json` | the event calendar |
| `PSU_SEASON` / `PSU_SEASON_DROP` | none / 40 | seasonal lobbies (`none`, `auto`, a name) and event drop rate 1 in N |
| `PSU_NBL_EXTRA` | empty | staged archive dirs laid over the NBL dir |
| `PSU_IDLE_SECS` | 600 | drop a silent session after this long |
| `PSU_AOTI_ACROMASTER` | off | 1 offers AotI's never-released Acromaster type |
| `PSU_BOARD_FILE` / `PSU_BOARD_SECS` | `logs/board/board.json` / 60 | the public board snapshot and how often it is written |
| `PSU_PANEL_PORT`, `PSU_PANEL_BIND`, `PSU_PANEL_TOKEN` | off, 127.0.0.1, none | the OpenLobby admin panel connection (below) |
| `PSU_PATCH_PORTS`, `PSU_LOGIN_PORTS`, `PSU_GAME_PORTS` | the PS2 ports | listening ports; the PC and Xbox 360 legs are opt-in ([discs.md](discs.md)) |
| `PSU_LOG_DIR`, `PSU_STATE_DIR`, `PSU_NBL_DIR`, `PSU_GAMEDATA`, `PSU_CAPTURE_DIR` | set by the image | where things live |

## Game data

The image is code only. The data directory's `nbl/`, `gamedata/` and `captures/` are mounted read-only
into the game service (`PSU_NBL_DIR`, `PSU_GAMEDATA`, `PSU_CAPTURE_DIR`); each has an `aoti/` level that
AotI sessions read first. They are built by `extract/` from your discs ([data.md](data.md)).

- New or changed NBLs are live on a player's next zone or quest load, no restart.
- Changed JSON (any gamedata table, either `missions.json`) is cached: restart the game service, which
  drops every live session.

**Retail type tables.** `plylevel.json` with a `type_caps` key on both discs (the retail type build in
`extract/`, [data.md](data.md)) switches type levels to the retail curves. On the first start with those
tables the game converts every character's stored type EXP once, keeping each type level, after backing
up `psu.db` (`state/backups/psu-pre-type-curve-*.db`, one `type_curve` line per character in
`state/account-audit.log`). Do not go back to older tables afterwards.

**Staged content.** Archives in a `PSU_NBL_EXTRA` directory win over `$PSU_NBL_DIR`, and a
`missions.json` there extends the disc's. Set `PSU_NBL_EXTRA=/nbl/<dir>` and restart the game service;
empty turns it off.

## Accounts

`tools/account.py` works on the live `psu.db` while the stack runs. Run it as the owner of the state
directory (it refuses any other user, since a root-owned WAL file would lock the services out):

```sh
python3 tools/account.py list [pattern]       # login IDs, GID, created, last login, banned, reset, characters
python3 tools/account.py reset <login id>     # prints a 6-character one-time code, once
python3 tools/account.py rename <login id> <new id>
python3 tools/account.py delete <login id> --yes   # backs the DB up first; refuses while players are online
python3 tools/account.py audit [n]            # the last n lines of state/account-audit.log
```

The state dir is `--state`, else `$PSU_STATE_DIR`, else `$PSU_DATA/state`. Each change is one IMMEDIATE
transaction and one JSON line in `state/account-audit.log`; passwords and codes are never written
anywhere. After a reset the player logs in with the code, or the code directly followed by a new password
(4+ characters), which becomes the password (the client has no change-password screen).

## GM commands, news and the admin CLI

- **GM chat commands** for accounts in `PSU_ADMINS` (`!help` lists them, `psu/gm.py`): `!announce`, `!uni`,
  `!zone`, `!notice`, `!online`, `!stats`, `!spawns`, `!kick`, `!ban` / `!unban`, `!mute` / `!unmute`,
  `!summon` / `!goto`, `!give`, `!meseta`, `!level`, `!warp`, `!news reload`, `!season`, `!clear`.
  Everyone has `!who`, `!news` and `!gm`. Every command is a `gm` event and every change a `gm_action`
  event in `events.jsonl`.
- **News:** `state/news.txt`, notices separated by a `---` line (`#` lines are comments), re-read when it
  changes. All notices form the login MOTD; the current one runs as a ticker after a player's lobby loads.
- **Admin CLI:** the game service listens on `127.0.0.1:$PSU_ADMIN_PORT` for `tools/announce.py` (token in
  `state/admin.token`, mode 0600, or `PSU_ADMIN_TOKEN`). Example:
  `PSU_DATA=<data dir> python3 tools/announce.py "Restart in 10 minutes"`; also `--uni`, `--area`,
  `--player`, `--dialog`, and `who`, `stats`, `news`, `events`, `season`, `spawns`, `kick`, `mute`,
  `unmute`. The docstring at the top of `tools/announce.py` lists them all.
- **Bans** are in the `bans` table; login refuses a banned account with its end date.

## Help desk

Any player can type `!gm <message>` in chat to ask the staff for help; `!gm` alone shows the request's
state and the latest answer. Requests are kept in `psu.db` (one open request per account, 5 messages per
10 minutes, 400 characters each). Staff read and answer them in the OpenLobby panel's Inbox; an answer
shows in game as a timed dialog at once, or on the player's next lobby load. Without a panel, the
requests are still in the database and in `events.jsonl` (`ev "help"`).

## Events

`state/events.json` (or `$PSU_EVENTS_FILE`) is a calendar of limited-time events, the way Sega ran the
original service: a seasonal lobby, EXP / meseta / drop multipliers, the season's event items and
featured missions, for one window or every week, on one disc or both. The format is in the docstring of
`server/psu/events.py`. Example:

```json
[{"key": "halloween", "name": "Halloween Festival",
  "start": "2027-10-28T00:00Z", "end": "2027-11-08T00:00Z",
  "season": "halloween", "exp": 1.5, "event_drop": 20,
  "notice": "Pumpkins are dropping all over Gurhal!"}]
```

The file is re-read when it changes. An entry that does not parse is skipped and reported;
`tools/announce.py events` lists what will run and what was skipped. Running and upcoming events show in
the login notices and the news ticker, a ticker announces each start and end, and the board lists them.

## Seasons

Both discs ship 16 lobby decoration sets. The data build stages a season's Colony archives (objects and
city music) under `nbl/season-staging/<name>/` ([data.md](data.md)); a season with nothing staged for a
disc leaves that disc's lobbies as they are.

- `PSU_SEASON=auto` follows the calendar in `psu/season.py`; a name runs that season; `none` (the default)
  turns seasons off. A running event with a `season` wins over this setting.
- `PSU_SEASON_DROP=N`: while a season runs, 1 in N kills that dropped nothing drop one of its items.
- Preview a season for one character before turning it on: GM `!season <name>` (on your next lobby load),
  `tools/announce.py season <character> <name>`, or the panel's Season preview action. Nobody else sees it.

## The OpenLobby admin panel

Set `PSU_PANEL_PORT` (for example 22191) and `PSU_PANEL_TOKEN`, and register the server in your OpenLobby
panel with that port and token. Keep `PSU_PANEL_BIND=127.0.0.1` unless the panel runs in another
container, which reaches a host-networked server through the Docker gateway (then bind `0.0.0.0`; the
token is required). The panel gets who is online, the admin commands as actions (announce, kick, mute,
ban, season preview), a calendar of events and seasons, and pages for players, news, events and the help
desk. Every panel action is audited as `panel:<user>`.

## Status and logs

- `logs/status.json`: online count, per universe and area, uptime; every 30 s.
- `logs/board/board.json`: the public board snapshot (levels, clears, events; character names only).
  Mount only `logs/board/` into a board service.
- `logs/events.jsonl` and `logs/sessions/`: events and per-session hexdumps (no passwords). They do
  hold login IDs, character names and chat text: treat them as personal data, keep them only as long as
  you need them, and tell your players what you keep.
- Backups and restores: [hosting.md](hosting.md).
