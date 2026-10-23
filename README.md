# prettystellaruniverse

A self-hostable server that brings back the online mode of the US PlayStation 2 releases of
**Phantasy Star Universe** and its standalone expansion **Phantasy Star Universe: Ambition of the
Illuminus**. Sega's servers for both discs closed in 2010.

| disc | serial | status |
|---|---|---|
| Phantasy Star Universe (USA) | SLUS-21194 | supported |
| Phantasy Star Universe: Ambition of the Illuminus (USA) | SLUS-21631 | supported, less tested |

Both discs share one world: the same accounts, universes and lobbies, with PSU and AotI players
seeing, chatting, trading and partying with each other.

**Only the PS2 versions are supported right now.** The repository also has code for the AotI PC
client and the Xbox 360 disc's patch check. It is experimental, off by default, not offered by the
setup program, and documented only in [docs/discs.md](docs/discs.md).

The server is plain Python 3 (asyncio, standard library only, SQLite) in one Docker image. It contains
**no game data**: everything it needs from the games is built on your machine from your own discs.

Phantasy Star Universe is a trademark of SEGA. This project is not affiliated with or endorsed by
SEGA. See [NOTICE.md](NOTICE.md).

## Quick start (hosting)

You need Docker, your own copy of one or both discs as ISO images, and a few GB of free disk space.

1. Run the setup program: `python setup.py` from a clone of this repository (Python 3.8 or newer,
   nothing else to install), or the download for your system from the Releases page.
2. Answer its questions: where your disc images are, and whether players join from your home network
   or over the internet.

Setup builds the game data from your discs, starts the server and the DNS resolver players need, and
tells you the DNS address to give your players. Run it again for a menu (start, stop, settings,
update, backups, accounts). What it does, and how to do the same by hand, is in
[docs/hosting.md](docs/hosting.md). Day-to-day administration (accounts, GM commands, news, events,
seasons) is in [docs/operations.md](docs/operations.md).

## Playing

Players need their own disc, the DNAS bypass for it (in [patches/](patches/)) and the DNS address
of a server. [PLAYING.md](PLAYING.md) is the player guide: PCSX2 and real PS2 setup, accounts and
what works.

## What works

- accounts (created on first login, shared by both discs), character creation and select, universes
- the Colony, the Spaceport and the planet cities (Parum, Neudaiz, Moatoob); AotI's own city and lobbies
- other players: movement, chat, partner cards, mail, blacklist, parties, trade, My Room visits, My Shop
- missions: PSU's free missions, AotI's free missions and its Episode 2 story, with enemies, damage,
  status effects, EXP, drops, boxes, keys, gates, ranks and rewards
- character progression: levels, types (PSU's ten, AotI's extra five), photon arts and technique
  levels, support techniques, PA disks
- economy: NPC shops, grinding, synthesis, the Partner Machine, Lumilass
- for hosts: GM chat commands, a player help desk (`!gm`), news ticker and login notices, an event
  calendar (EXP, meseta and drop campaigns, featured missions), seasonal lobby decorations, account
  resets, bans and mutes, an admin CLI, and an optional OpenLobby admin panel connection

Known gaps are listed in [PLAYING.md](PLAYING.md#known-gaps).

## Building the game data

The server reads tables and archives that come from the discs (item, enemy, level and drop tables,
mission and lobby archives). They are SEGA's data, so they are never committed here, baked into the
image or published. The setup program builds them for you; [docs/data.md](docs/data.md) describes
what is built, where it goes and how to run the build by hand (`python -m extract`).

## How it fits together

```
 PS2 / PCSX2
   | DNS: the four *.segaonline.jp game names -> your server
   |
   +--> patch   11110 (PSU) / 11140 (AotI)      plaintext: "you are up to date"
   +--> ssl3    12110 / 12140 login, 12061 game  SSL 3.0 RC4-SHA terminator (stunnel)
           +--> login 22110 / 22140   accounts, auth key, game-server address
           +--> game  22061           everything after login: characters, world, missions ...
                    |
                    +-- state/psu.db (SQLite, shared with login)
                    +-- game data: NBL archives + JSON tables built from the discs (read-only)
```

More: [docs/architecture.md](docs/architecture.md). The protocol is documented in the module
docstrings, with [docs/protocol.md](docs/protocol.md) as the index. Disc differences:
[docs/discs.md](docs/discs.md). Development and the scripted test player:
[docs/development.md](docs/development.md).

## Repository layout

| path | what |
|---|---|
| `server/psu/` | the services (`patch`, `login`, `game`, `observe`) and one module per game feature |
| `server/Dockerfile` | the one image every service runs (code only) |
| `extract/` | builds the game data from your discs (and EGS's lobby sources) |
| `tools/` | test player, packet checks, admin CLIs, local DNS |
| `patches/` | the DNAS bypass for both discs (pnach files, Cheat Device codes, the patched-image recipe) |
| `compose/`, `setup.py`, `hostsetup/` | the compose stack and the setup program |
| `docs/` | architecture, protocol index, disc differences, data, hosting, operations, development |

## Credits

- [EGS](https://github.com/essen/egs), the open-source server for the PC version of Phantasy Star
  Universe by Loic Hoguin and contributors. Its protocol documentation is where this work started,
  and the data build fetches its lobby and counter quest sources and two captured tables from it
  (see [NOTICE.md](NOTICE.md)).
- The PSU community, whose records of the retail game keep this server accurate, in particular
  [PSUPedia](https://psupedia.info) for the retail type stats.
- Everyone who played on the test servers and reported what broke.

## License

GPL-3.0-or-later: see [LICENSE](LICENSE). Source files carry no per-file headers; the repository as
a whole is `SPDX-License-Identifier: GPL-3.0-or-later`. Third-party material fetched at build time
keeps its own license ([NOTICE.md](NOTICE.md)).

Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: [SECURITY.md](SECURITY.md).
