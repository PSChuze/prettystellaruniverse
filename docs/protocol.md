# Protocol notes: where they are

The protocol is documented where it is implemented: every module under `server/psu/` starts with a
docstring that lists the commands it handles, their body layouts and, where it matters, the PS2 client
routine that reads or sends them. This page is the index.

Conventions used throughout:

- `C>S` client to server, `S>C` server to client. Offsets are body offsets unless a docstring says
  otherwise.
- Framing (login and game): `u32le size (incl. the 8-byte header, padded to 4) | u16 BIG-endian command |
  u8 channel (client 2, server 3) | u8 0 | body`. The patch service uses its own framing (`patch.py`).
- Hex addresses such as `0x1bfdd8` are addresses in the PSU client's main module `PSU_US.erx` (image
  address; RAM = image + 0x248000 on PSU). "AotI 0x..." is the same routine on the AotI disc (its module
  loads at 0x23ce00). They let anyone with a disc check a claim in a disassembler; they are not needed to
  run the server.
- Field names and the first layouts come from EGS, the open PSU PC server (see [NOTICE.md](../NOTICE.md));
  where the PS2 discs differ from the PC client the docstrings say so.

| topic | module | commands (main ones) |
|---|---|---|
| patch check | `patch.py` | 0x01 / 0x02 / 0x14 hello, file info, up to date |
| login | `login.py` | 0x0202, 0x080E, 0x0219 (login), 0x0223, 0x0226 / 0x0225 (MOTD), 0x0217 / 0x0216 (game server) |
| session, character select, dispatch | `game.py` | 0x020D (key auth), 0x0D02 / 0x0D03 / 0x0D06 (characters), 0x0807 (area change), 0x021D / 0x021F (universes), 0x0304 (chat), 0x0B03 / 0x0B05 (fragments) |
| character records and load | `charload.py` | 0x0D01, 0x0D03 slots, the load sequence |
| zone changes, lobbies | `area.py` | 0x0205, 0x020E / 0x020F (quest / zone archives), 0x0215, 0x1015 |
| seeing other players | `world.py` | 0x0111, 0x010D, 0x0201, 0x0203, 0x0204, 0x0233, channel-1 casts |
| counters and missions | `missions.py` | 0x0811 / 0x0812, 0x0C01, 0x0C05 / 0x0C06, 0x0C0E, 0x0C0F / 0x0C10, 0x0C11..0x0C14 |
| mission state | `instance.py` | 0x0402 / 0x0404, 0x0F0A, 0x1202..0x1213 |
| mission judge | `judge.py` | 0x0C09, rank, rewards; quest rule tokens |
| combat | `combat.py` | 0x0E00 hits, 0x0E07 / 0x0E08 results, 0x0115, 0x1022, 0x1201 / 0x1203 |
| drops and pickup | `drops.py` | 0x0A02 / 0x0A03, 0x0106 |
| inventory and equipment | `items.py` | 0x0105 events, 0x0106, 0x0109, 0x0A0A |
| PA growth, disks | `growth.py` | 0x0105 events 3 / 4 / 7, 0x0108 |
| NPC shops and dialogs | `shop.py`, `dialog.py` | 0x1A01 / 0x1A02, 0x1A04 (PP cube), 0x010A |
| grinding, Lumilass, Probo | `econ.py` | 0x0105, 0x0A09, 0x1A03, 0x1A05 |
| synthesis | `synthesis.py` | 0x130E / 0x130F |
| My Room | `myroom.py` | 0x130B..0x1328 |
| My Shop | `playershop.py` | 0x130D, 0x130F, 0x1313, 0x1319, 0x131A |
| parties, cards, mail, blacklist | `social.py` | 0x0303, 0x100E, 0x1022, 0x15xx |
| partner card presence | `presence.py` | 0x1506 / 0x1507, 0x1510, 0x1515 / 0x1516 |
| levels, types, stats | `progression.py` | 0x0113, type tables, status block |
| GM commands, notices, help desk | `gm.py`, `news.py`, `helpdesk.py` | 0x0224, 0x0228, 0x0225 |
| both discs | `games.py`, `xgame.py` | what differs per disc, what one disc may show the other |
| seasonal lobbies | `season.py` | 0x020F +0x2D, 0x0205 +0x43, the quest archive's zone record |

Not client protocol, but documented the same way: `admin.py` (the admin channel's JSON lines),
`adminport.py` and `olport.py` (the OpenLobby panel's HTTP protocol), `gamelink.py` (the account link
API), `board.py` (the board snapshot's fields) and `events.py` (the event file's format).

For the per-disc differences in one place, see [discs.md](discs.md).

Unknown traffic: run `psu.observe` on the public ports ([development.md](development.md)); it records
every byte per session.
