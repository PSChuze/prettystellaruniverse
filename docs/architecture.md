# Architecture

## Services

One Python package, `server/psu/`, and one Docker image (`server/Dockerfile`, `python -m <module>`).
Each service is a module with its own `main()`:

| module | ports | what it does |
|---|---|---|
| `psu.patch` | 11110 PSU, 11140 AotI (public, plaintext) | answers the disc's patch check with "up to date" and logs every byte |
| `psu.login` | 22110 PSU, 22140 AotI (loopback, plaintext) | hello, version, ID / password (accounts in SQLite, created on first login when `PSU_AUTOREGISTER=1`), MOTD, issues a one-time 4-byte key and hands out the game-server address |
| `psu.game` | 22061 (loopback, plaintext) | everything after login, for both discs in one world |
| `psu.observe` | the public ports, off by default | byte recorder for unknown traffic (hexdump per session + `events.jsonl`) |
| `ssl3` (stunnel) | 12110 / 12140 login, 12061 game (public) | terminates the discs' SSL 3.0 (RC4-SHA) and forwards plaintext to login / game |

The discs only speak SSL 3.0 with RC4-SHA, which no current OpenSSL build offers, so the terminator is
a stunnel linked against an OpenSSL 1.1.1 built with SSLv3 re-enabled ([hosting.md](hosting.md)). The
client does not verify the server certificate; a self-made one is enough. This is compatibility, not
security: nothing else may sit behind that terminator.

The disc is told apart by the login port (`login.GAME`): the key login issues carries the game
(`psu` / `aoti`), and the game service sets it for the session on 0x020D (`games.set_current`).

Inside the game service, on loopback unless configured otherwise:

| port | module | what |
|---|---|---|
| 22190 | `admin` | admin channel for `tools/announce.py` (token in `state/admin.token`) |
| `PSU_PANEL_PORT` (off) | `adminport` | the OpenLobby admin panel's protocol (`olport`): status, actions, calendar, tool pages, help desk |

Login can also serve 22192 (`gamelink`), a signed loopback API an OpenLobby website uses to link a
game login to a website account; it stays off without `GAMELINK_SECRET`.

## The game service

`psu.game` runs one asyncio task per connection (`game.session`). The session reads framed packets
(`u32le size | u16 BE command | u8 channel | u8 0 | body`), dispatches on the command and answers. The
dispatcher lives in `game.py`; each feature is its own module:

| area | modules |
|---|---|
| characters | `charload` (records, load sequence), `progression` (levels, types, stats), `growth` (PA growth, disks) |
| world | `world` (zones, local ids, who sees whom), `area` (zone / quest changes, NBL lookup), `presence`, `season` |
| missions | `missions` (counters, story order), `instance` (flags, objects), `judge` (clear / fail / rank / rewards), `combat`, `drops` |
| items | `items` (inventory, equipment, records), `shop`, `dialog`, `econ` (grinding, Lumilass, Probo), `synthesis`, `myroom`, `playershop` |
| social | `social` (parties, cards, mail, blacklist), trade in `game.py` |
| both discs | `games` (which disc, per-disc data lookup), `xgame` (what one disc may show the other) |
| hosting | `gm` (chat commands), `helpdesk` (`!gm` requests), `news`, `events` (calendar), `board` (public snapshot), `admin`, `adminport` |
| storage | `db` (SQLite, WAL, shared with login) |

Ownership: the PS2 client carries its own offline "server" for the offline mode. Online the real server
is expected to own what that code did offline: hit results, enemy HP, EXP, drops, mission judging,
grinding and synthesis rolls. The modules follow the client's offline code, with the client addresses
in comments, so behaviour matches what the disc does offline.

Packets larger than one fragment go out as 0x0B03 fragments; the PS2 acknowledges each one with 0x0B05
before the next may be sent, so sends that need it are paced (`fragments`, `send_paced`).

Handler errors never drop a player: each one becomes an `error` event and a traceback in the session
log; 50 errors in one session drop that session.

## State

- `$PSU_STATE_DIR/psu.db`: accounts (PBKDF2 hashes, never plaintext), login keys, characters (the
  client's own 0x0D02 body plus a JSON progression state), cards, mail, blacklist, bans, resets, help
  desk requests. Login and game share it (WAL).
- `$PSU_STATE_DIR/admin.token`, `news.txt`, `events.json`, `mutes.json`, `account-audit.log`.
- `$PSU_LOG_DIR/events.jsonl` (one JSON line per event), `sessions/` (per-session hexdumps, the 0x0219
  password field blanked), `status.json` (who is online, every 30 s), `board/board.json` (the public
  board's snapshot).

## Game data (read-only)

- `$PSU_NBL_DIR`: the NBL archives the client loads (quests, zones, counter tables) and `missions.json`;
  an AotI session reads `$PSU_NBL_DIR/aoti/` first. Seasonal lobbies come from `season-staging/<name>/`
  inside it. `$PSU_NBL_EXTRA` dirs are laid over it (staged content).
- `$PSU_GAMEDATA`: JSON tables (items, enemies, levels, drops, shops, ...); AotI reads `aoti/` first.
- `$PSU_CAPTURE_DIR`: two optional tables captured from the PC service and taken from EGS
  (`ppcube.bin`, `packet170c.bin`).

Where they come from: [data.md](data.md).
