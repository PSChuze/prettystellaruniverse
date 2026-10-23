# DNAS bypass: getting a US PSU disc online

Both US discs, Phantasy Star Universe (SLUS-21194) and Ambition of the Illuminus (SLUS-21631),
run Sony's DNAS check before they go online. Sony shut DNAS down, so the check can never pass,
and a server cannot answer for it (the replies are signed by Sony). The bypass skips that
check on the console. It changes nothing else: four instructions per game, in the DNAS object's
calls into the DNAS library.

Pick one of three forms. They make the same four edits and are all built from `bypass.json` by
`../extract/bypass.py`.

| you are playing on | use |
|---|---|
| PCSX2, or a console loading an image (OPL from USB/HDD/network, burned DVD-R) | a patched image |
| PCSX2 with the original, unpatched image | the pnach |
| a console with FreeMcBoot, booting the original disc | the Cheat Device codes |

After the bypass, set the console's primary DNS to the DNS address your server's host gives you
(secondary empty or the same). That resolver answers the four game names with the server.

## Patched image

From your own disc image (a 2048-byte-sector `.iso`, not a `.bin`). The tool checks the image's
size and sha1 first and refuses anything else:

```sh
python extract/bypass.py iso PSU  "Phantasy Star Universe (USA).iso"  "Phantasy Star Universe (USA) [ONLINE].iso"
python extract/bypass.py iso AotI "Phantasy Star Universe - Ambition of the Illuminus (USA).iso"  "... [ONLINE].iso"
python extract/bypass.py check PSU "Phantasy Star Universe (USA).iso"      # verify only, write nothing
```

Standard-library Python 3, nothing to install. The source image is only read.

| game | source sha1 | source size | patched sha1 |
|---|---|---|---|
| PSU | `92e54e8f0e6ab0f00aa245bab3e67e19a935473e` | 4,251,156,480 | `176f6f0c542307d410d2a62decf4e8536756b2b2` |
| AotI | `b3dd9016bb03f7c4ca6f245d364369142783a04b` | 3,703,570,432 | `275cc78d54117d59d28488e2511f77674b7aaa26` |

What it does: the game code is `ERX/PSU_US.GZ`, a gzip of a relocatable module. The tool unpacks
it, patches the four words, removes the loader relocation on each (otherwise the loader would
rewrite the new instruction), and compresses it back to exactly the original length. Nothing else
on the disc moves.

**Distribution.** A patched image is the whole game, and so is an `.xdelta` of it in practice
(re-compressing the module means the delta carries all of it, about 2.6 MB). Neither can be
published. Share the tool and `bypass.json`: they contain no game data beyond the four original
and four new instruction words per game.

## PCSX2 pnach

Copy `SLUS-21194_29B44F89.pnach` (PSU) or `SLUS-21631_5EA50CB0.pnach` (AotI) into PCSX2's
`cheats/` folder and enable cheats. The file must keep that exact name and must not have a
`[Section]` header (a sectioned patch is a toggle that defaults to off, and PCSX2 still logs it as
found). Do not use a pnach with a patched image; the guards make it harmless, but it is pointless.

## Cheat Device PS2

`cheats/PSU-Online.txt`, for [Cheat Device](https://github.com/root670/CheatDevicePS2) on a
FreeMcBoot console with the original disc. It has a section for each game; turn on **Enable Code**
and **DNAS bypass (required)** in the one for the disc you boot.

| game | Enable Code | module base the guards assume |
|---|---|---|
| PSU (SLUS-21194) | `901B149C 00A0902D` | 0x248000 |
| AotI (SLUS-21631) | `901A68A4 00A0902D` | 0x23ce00 |

The Enable Code is a hook in libpad2's `scePad2Read` in the boot ELF (the same library code on
both discs, entry 12 of its export table; AotI's copy is linked lower). Cheat Device checks the
opcode there before installing, so the codes do nothing on another disc. Each write is guarded by
an E-type compare of the site's relocated retail halfword, so nothing is written until the game
module is in memory at that base. Both discs' boot ELFs end below their module base (AotI's at
0x232a80), so no guard can match boot ELF code.

To check the codes and the pnach against your own disc without building an image, copy the boot
ELF and `ERX/PSU_US.GZ` off it and run:

```sh
python extract/bypass.py files AotI SLUS_216.31 PSU_US.GZ    # or: files PSU SLUS_211.94 PSU_US.GZ
```

It verifies the ELF's CRC and hook word, each site's retail word and relocation in the module,
and prints the runtime address, guard and replacement per site.

## What has been tested

- PSU pnach: in PCSX2, the game gets past DNAS and dials the patch server.
- PSU patched image: boots in PCSX2; the module loads at 0x248000 with the four sites patched and
  its other relocations intact.
- AotI pnach: all four sites patched in memory on the retail disc (PCSX2).
- AotI patched image: built, and `bypass.py` rebuilds the same module byte for byte.
- Cheat Device codes, both games: checked statically against the retail boot ELFs and modules (hook
  words, ELF CRCs, site words, relocations, guards).
- Not yet tested: the Cheat Device codes on a real console.

A memory card holding a downloaded "System File" (`BASLUS-21194system`) makes the loader run that
instead of the disc's module. The disc patch does not reach it; the pnach and codes leave it
alone (their guards fail). Delete that save if the bypass seems to do nothing.
