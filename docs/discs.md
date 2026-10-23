# The two discs

| | Phantasy Star Universe | Ambition of the Illuminus |
|---|---|---|
| serial (US) | SLUS-21194 | SLUS-21631 |
| boot ELF / CRC | `SLUS_211.94`, 29B44F89 | `SLUS_216.31`, 5EA50CB0 |
| game module | `ERX/PSU_US.GZ`, loaded at 0x248000 | `ERX/PSU_US.GZ`, loaded at 0x23ce00 |
| game id in the server | `psu` | `aoti` |
| patch host / port | `patchps2.psuus.segaonline.jp:11110` | `patchps2.ipsuus.segaonline.jp:11140` |
| login host / port (SSL 3.0) | `loginps2.psuus.segaonline.jp:12110` | `loginps2.ipsuus.segaonline.jp:12140` |
| game server (SSL 3.0) | the address login hands out, port 12061 | same |
| DNAS | 3.00, bypassed on the client ([patches/](../patches/README.md)) | same |

Both discs log in to the same accounts and play in one world. The game is known from the login port and
kept per session (`psu/games.py`). PSU, AotI and the PC version share most of the protocol (EGS's
layouts), so most code is common; the differences below are the ones the server handles.

## The client, briefly

- The game code is not in the boot ELF but in `ERX/PSU_US.GZ`: a plain gzip of a relocatable EE module
  (ERX, image based at 0, REL relocations). It is not encrypted. The DNAS modules are separate ERX files
  (`DNAS_NET`, `DNAS_MC`).
- The host names and ports above are `{host, port}` pairs in the module's data. Game servers past login
  are handed out by the login server. The original names still resolve publicly to Sega of America
  addresses, so a test console must never use public DNS.
- The game's TLS stack is MatrixSSL 1.x. Its certificate check only compares the CN with `PSUServer`
  and never fails the handshake (PSU 0x302620, AotI 0x302290), so any parseable RSA certificate works
  and dates are never checked.
- On the original service a PS2 downloaded a "System File" to the memory card and ran it instead of
  the disc's module. This server always answers "up to date", so no System File is ever written; an old
  one left on a memory card bypasses the DNAS patch ([patches/](../patches/README.md)).
- The official PS2 and PC service shared one world (US and EU together) until it closed on
  2010-03-31.

## Data

AotI is a superset of PSU's data, with its own copies of most archives and tables. The server looks for
an AotI session's file in `aoti/` first and falls back to the shared (PSU-built) one, for NBL archives
(`$PSU_NBL_DIR/aoti/`) and JSON tables (`$PSU_GAMEDATA/aoti/`) alike (`games.data_file`, `json_data`).
See [data.md](data.md).

## Where the discs differ (as the server sees it)

- **Patch hello:** PSU sends command 0x02 (32 bytes), AotI the PC's 0x14 (`patch.py`).
- **Status block and 0x0233:** PSU's per-player status block is 4 bytes longer, so 0x0233 strides 0x2D0 on
  PSU and 0x2CC on AotI (`world.py`, `progression.status_block`).
- **Spaceport:** the shared Spaceport is map 900 on PSU and 901 on AotI; each side sees the other on its own
  copy (`area.map_for`).
- **Types:** PSU has types 0..9, AotI adds Acrofighter, Acrotecher, Fighmaster, Gunmaster and Masterforce
  (10..14). Acromaster (15) was never a retail type and is off (`PSU_AOTI_ACROMASTER=1` offers it). A PSU
  client is sent at most type level 10, because its level table has 10 rows per type (`progression.py`).
- **Photon arts:** PSU levels PAs to 30, AotI to 50; values above PSU's cap are clamped in every record a
  PSU client reads, stored state keeps the real value (`xgame.py`, `growth.py`).
- **Economy:** AotI sessions grind with AotI's refine roll and failure reset, roll drops and synthesis
  AotI's way and use its PA cap; PSU sessions keep PSU's rules (`econ.py`, `drops.py`, `synthesis.py`).
- **Elements:** AotI's weakness / resistance factor differs (1 +- 0.66 (a + d)); `combat.py`.
- **Appearance:** AotI has about twice PSU's part words and extra voices. An AotI character shown to a PSU
  player gets the nearest PSU parts and voice; PSU's client would crash on an unknown part word
  (`xgame.py`, built from `parts.json`).
- **Items:** an item id PSU's tables lack is never given to a PSU player (hidden from ground lists,
  refused in trades); AotI-only grind-failure bits are left out of PSU records.
- **Missions:** PSU serves its free missions; AotI serves its own free missions and the Episode 2 story
  (Guardians HQ counter 115, chapter bases, story order per character, story flags kept per character).
  Story content staged by a host in `PSU_NBL_EXTRA` is AotI only (`missions.py`).
- **Mixed parties:** a mission is played by the members on the leader's disc; the others stay at the
  counter.
- **Seasonal lobbies:** PSU takes the decoration set from the quest archive's zone record, AotI from 0x020F
  +0x2D; both discs play the season's city music from the staged Colony script (`season.py`).

Each of these is covered by `tools/xgame_check.py` (byte digests for PSU -> PSU and AotI -> AotI, checks for
the translations) and by `tools/testplay.py --game both`.

## Experimental: AotI PC and Xbox 360

Not supported, off by default and not offered by the setup program. The code is kept so the work can
continue:

- **AotI PC** (EGS's US PC ports): patch 11230 with the PC's file list dialect, login over SSLv3 on 12230
  (plaintext 22230 behind the terminator), 0x080E Platform = 1; the platform travels with the login key
  like the game does (`games.platform()`). Turn on by adding 11230 to `PSU_PATCH_PORTS` and 22230 to
  `PSU_LOGIN_PORTS`, plus a terminator for 12230. PC-specific packet layouts are not done.
- **Xbox 360**: the disc dials `patch.psueu.segaonline.jp:11200` by plain DNS; `patch.py` can answer it
  (add 11200 to `PSU_PATCH_PORTS`), but its hello has not been observed. Its login and game servers are
  reached through Xbox Live's gateway, not DNS, so nothing past the patch check can be served.
