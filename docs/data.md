# Game data

The server needs tables and archives that only exist on the game discs. Nothing of the discs ships with
this project: `extract/` builds everything on your machine from your own copies, into one data directory
the server reads. The setup program runs it for you; this page is for running it by hand and for knowing
what is in there.

## Build it

You need the US PlayStation 2 discs as ISO images, one or both:

| disc | serial | image size |
|---|---|---|
| Phantasy Star Universe (USA) | SLUS-21194 | 4,251,156,480 bytes |
| Phantasy Star Universe: Ambition of the Illuminus (USA) | SLUS-21631 | 3,703,570,432 bytes |

A DNAS-patched image made by `extract/bypass.py iso` works too (its tables are the retail ones). Other
regions and the PC or Xbox 360 releases are refused with a message saying what the image is.

With Docker (nothing else to install):

```sh
docker build -t psu-extract extract
docker run --rm -v "$PWD/discs:/discs:ro" -v "$PWD/data:/data" -v psu-extract-cache:/work psu-extract \
  --psu "/discs/Phantasy Star Universe (USA).iso" \
  --aoti "/discs/Phantasy Star Universe - Ambition of the Illuminus (USA).iso" --out /data
```

On the host (Python 3.10 or newer, standard library only; the EGS step needs `erl`, `erlc` and `gcc`, or
Docker, in which case it runs in the `erlang:26` image by itself):

```sh
python -m extract --psu PSU.iso --aoti AotI.iso --out data --work extract-cache
```

Leave out `--psu` or `--aoti` to build from one disc. The first run takes a while (the archives are
descrambled and decompressed in pure Python; AotI's 600 mission archives are the slow part). The cache
directory (`--work`, under 1 GB for both discs) keeps the unpacked files and a stamp per finished step, so a
second run skips what is done; `--rebuild` runs every step again after you change a tool. `--sha1` also
hashes the whole image and compares it with the known dumps.

Point the server at the result:

```sh
PSU_NBL_DIR=data/nbl PSU_GAMEDATA=data/gamedata PSU_CAPTURE_DIR=data/captures
```

## What is built

```
data/
  nbl/                 PSU_NBL_DIR
    quest-*.nbl, zone-*.nbl, counter-table-*.nbl, missions.json
    aoti/              AotI's own copies (an AotI session looks here first)
    season-staging/<season>/   the seasonal Colony (quest-1100000.nbl, zone-1100000-0.nbl, aoti/)
  gamedata/            PSU_GAMEDATA: PSU's JSON tables
    aoti/              AotI's
  captures/            PSU_CAPTURE_DIR: ppcube.bin, packet170c.bin
```

With both discs, PSU's files sit at the top and AotI's in the `aoti/` subdirectories. With AotI alone, its
tables and archives are only in `aoti/`, and the top level holds what both share (the EGS lobby quests).

| output | tool | from the disc |
|---|---|---|
| `gamedata/items.json` | `gamedata.py`, `itemdb.py` | `GA_ITEMPARAM.NBL`, `GA_TEXT_AE.NBL`, `XF_ITEMSHOPDATA.AFS` (AotI: also every id its drop tables name, and PSU's ids marked `other_disc` when both discs are built) |
| `shops.json`, `weapons.json`, `enemylevel.json` | `gamedata.py` | `itemShopList.snr`, `XF_ITEMSHOPDATA.AFS`, `itemWeaponParam_*.snr`, `SU_ENEMYLEVELBASEPARAM.NBL` |
| `plylevel.json` | `gamedata.py --type-overrides`, `retail_types.py` | `GA_PLYCOMMON.NBL` `PlyLevelParamData.snr`, with the overlay (below) |
| `enemies.json` | `enemydata.py` | `SU_EN_*.NBL`, `SU_ENEMYLVTABLEPS2.NBL`, tables in `PSU_US.erx` |
| `drops.json` | `dropdata.py` | `itemEnemyDrop.snr`, `itemObjectDrop.snr`, `SU_ITEMAREADROP.AFS`, the free missions' zones |
| `grind.json`, `probo.json`, `synthesis.json`, `lumilass.json` | `econdata.py` | `GA_ITEMPARAM.NBL`, `XF_BOARDPARAM.NBL`, `SS_PA_EVOLUTION.NBL`, `GA_PLYMAKECOMMON.NBL`, `PSU_US.erx` |
| `pa.json`, `paparam.json` | `patables.py`, `paparams.py` | `GA_ITEMPARAM.NBL` skill / bullet / tech params |
| `parts.json` | `partsdata.py` | `GA_PLYMAKECOMMON.NBL` `partsinfo.snr`, `selectable.snr` |
| `ppregen.json` | `ppdata.py` | `itemWeaponParam_*.snr` |
| `nbl/counter-table-*.nbl`, mission `quest-*` / `zone-*`, `missions.json` | `missiondata.py`, `exitpatch.py` | `FA_QE_TABLE*.AFS`, `XF_QE_OFF_FM_*.AFS` (AotI: `XF_QE_OFF02_FM_*`, `XF_QE_OFF02_STORY*`); each final goal gate gets a warp out |
| lobby zones `zone-1100000-0`, `zone-110[1-3]000-*`, `zone-1104000-0`, `quest-1120000`, `zone-1120000-1` | `lobbies.py` (`gatepatch.py`, `roompatch.py`, `questpatch.py`, `aotiroompatch.py`) | the offline lobby `XF_QE_OFF_EXTRA_LOBBY01.AFS` (AotI: `XF_QE_OFF_LOBBY_ILLUMINUS.AFS`), with the offline-only checks turned off |
| `nbl/season-staging/` | `seasonbuild.py` | `XF_OB_*.NBL` (the season decorations) and EGS's Colony set file |

Every disc read goes through `cvm.py` (PSUROFS.CVM, descrambled with the password the game module holds),
`nbl.py` (the NBL archives, with the Blowfish tables from the same module) and `erx.py` (the game module
itself, `ERX/PSU_US.GZ`). No key material is written in this repository: it is read from your disc.

## What EGS supplies, and why

[EGS](https://github.com/essen/egs) is Loïc Hoguin's server for the PC release, licensed AGPL-3.0. The
build downloads it from GitHub at a fixed commit (`ceea04c3b438cee9a8470f2abc6297429bf347c5`, see
`extract/egs.py`) into the cache directory and never copies it into this project. From it:

- `nbl/quest-0.nbl` and `quest-1100000.nbl` .. `quest-1104000.nbl`: the online lobby quests (Colony and the
  three planets, the Spaceport, the counter quest). The discs only have the offline lobbies, so EGS's quest
  compiler builds these from its own quest sources. `egs-build.sh` adapts them to the PS2 client (NSR/NSIF
  chunks and `quest.snr` instead of the PC's names, the Job Counter in the Colony, the native zones' maps
  in zone 0). The zone archives the server sends with them are the discs' own (above).
- `nbl/zone-1100000-0.egs.nbl`: EGS's Colony zone. It is not served; `seasonbuild.py` takes the season
  decorations' placements from its set file.
- `captures/ppcube.bin`, `captures/packet170c.bin`: two tables EGS captured from the PC service (the PP
  cube menu and the counter backgrounds). Without them the server leaves those two replies out.

## The overlay

`extract/overlay/psu_type_overrides.json` and `aoti_type_overrides.json` are the published retail type
tables (type stats, race %, unlock requirements and type EXP curves) with their PSUPedia sources. The discs
hold the offline values; the online ones lived on Sega's servers. They are numbers and citations only.
`gamedata.py --type-overrides` lays them over each disc's own table; with both discs `retail_types.py`
gives PSU's types 0..9 AotI's era, as the shared service had. Each file's `_doc` explains the format.

## Running a tool alone

Every tool in `extract/` still runs on its own, for example after changing it. They find the unpacked disc
files through the cache directory, given as `--work DIR` or `$PSU_EXTRACT_WORK` (layout in
`extract/workdir.py`), and read the disc itself only for files the cache lacks (`--iso`):

```sh
export PSU_EXTRACT_WORK=extract-cache
python extract/enemydata.py --game aoti --iso AotI.iso --out data/gamedata/aoti
python extract/missiondata.py --game psu --iso PSU.iso --out data/nbl --enemies data/gamedata/enemies.json
python extract/seasonbuild.py --nbl data/nbl
```

Each tool's docstring describes the file formats and the client addresses it follows.
