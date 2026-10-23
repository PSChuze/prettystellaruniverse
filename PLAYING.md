# Playing Phantasy Star Universe (PS2) online again

This server brings back the online mode of the US PlayStation 2 releases of **Phantasy Star Universe**
(SLUS-21194) and its standalone expansion **Ambition of the Illuminus** (AotI, SLUS-21631). Both games
share one world: PSU and AotI players log in with the same accounts and meet in the same universes and
lobbies.

## What you need

1. **Your own copy of the game** (the US disc, or a disc image you made from it). Nobody here
   distributes the game, patched images or patch files made from it.
2. **The DNAS bypass for that disc.** Sony's DNAS servers are gone, so the unmodified game stops at the
   network check. The bypass skips only that check (four instructions). It is in this repository's
   [patches/](patches/README.md) folder.
3. **The server's DNS address.** Your server's host gives you one. Set it as the **primary DNS** in the
   game's network settings (the PS2 "Your Network Configuration" file, or PCSX2's DEV9 DNS setting).
   Leave the secondary DNS empty or set it to the same address. With any other DNS the game tries to
   reach Sega's old addresses, not the server.

### Which bypass for which disc

Use one form, never two at once.

| | Phantasy Star Universe | Ambition of the Illuminus |
|---|---|---|
| disc serial | SLUS-21194 | SLUS-21631 |
| PCSX2, original disc or image | `patches/SLUS-21194_29B44F89.pnach` | `patches/SLUS-21631_5EA50CB0.pnach` |
| patched image (PCSX2, or a console loading images: OPL, burned DVD) | `python extract/bypass.py iso PSU <your.iso> <out.iso>` | `python extract/bypass.py iso AotI <your.iso> <out.iso>` |
| real PS2 with FreeMcBoot + Cheat Device, original disc | `patches/cheats/PSU-Online.txt`, PSU section | `patches/cheats/PSU-Online.txt`, AotI section |
| Cheat Device Enable Code | `901B149C 00A0902D` | `901A68A4 00A0902D` |

- **pnach:** copy the file into PCSX2's `cheats/` folder with exactly that name and enable cheats.
- **Patched image:** `bypass.py` only accepts the Redump dump of the disc (it checks size and sha1) and
  writes a new image; your original is only read. `bypass.py xdelta ...` can make an `.xdelta` from
  your own patched image for your own use. An xdelta carries most of the game code, so do not share it.
- **Cheat Device:** turn on **Enable Code (must be on)** and **DNAS bypass (required)** in the section
  for the disc you boot. Nothing is written until the game's own code is loaded.
- A memory card save named "System File" (`BASLUS-21194system`, a download from the original service)
  makes the game run that instead of the disc's code, and the bypass does not reach it. Delete that
  save if the bypass seems to do nothing.

More detail: [patches/README.md](patches/README.md).

### PCSX2 setup

- Network adapter (DEV9): Ethernet enabled, a working host adapter, **DNS mode manual** with the
  server's DNS address. Leave the internal DHCP on.
- Load the pnach for the disc's CRC (or boot the patched image), and enable cheats.
- If the game ever reports a network error, **restart PCSX2 completely** before trying again: a
  reconnect without a restart can leave the character floating in the Spaceport.

## Your account

- At the login screen, type **any new user ID and password**. The first login with a new ID creates the
  account with that password (unless the host turned that off); after that, the password must match.
  The same account works on both discs.
- The game's 2006 encryption is weak by today's standards: use a password you use nowhere else.
- Several wrong passwords in a row lock that ID for 10 minutes.
- **Forgot your password?** Ask your server's host. They give you a 6-character **reset code** (letters
  and digits) and your old password stops working. Log in with the reset code as the password; or, to
  pick a new password at the same time, type the code **followed directly by your new password** (at
  least 4 characters), for example `K7Q2MXsecret` for code `K7Q2MX` and new password `secret`. From then
  on log in with just the new password. Your characters are kept.
- Hosts can also change your login ID or delete your account on request.
- Create a character as usual. It keeps its level, types, items, meseta, photon arts, palette, room and
  cards. A character made on AotI can be loaded on PSU (AotI-only looks and items show as the nearest PSU
  ones there).
- Need help in game? Type `!gm` followed by your message in chat. The staff answer in game, at once if
  you are online or the next time you enter a lobby. `!help` lists the other chat commands.

## What works

Both discs unless marked.

- **Lobbies:** the Colony (shops, Job Counter, elevators), the Spaceport gates and planet travel to
  Parum, Neudaiz and Moatoob; on AotI its own city and lobbies, with each gate leading to its planet's
  shuttle as in the offline game.
- **Other players:** see and walk with each other, chat (also party chat), partner cards, mail,
  blacklist, parties with HP bars, trade, My Shop.
- **Universes:** the Uni-cube (Server Select) lists several universes with player counts.
- **Missions:**
  - PSU: the free missions at the Linear Line and Space Docks counters and the planet counters.
  - AotI: its own free missions at the six mission counters (one map layout per mission, difficulty
    C to S), and the **Episode 2 story**: all ten chapters at the Guardians HQ counter, each first act
    leading to its chapter's base and the second act there.
  - Enemies, damage, EXP and level-ups, item and meseta drops, breakable boxes, keys and gates, healing
    pads, Scape Dolls and revive items, mission ranks with clear rewards and type EXP. Parties play
    missions together.
- **Your character:** levels and stats, inventory and equipment, the weapon/item palette, photon arts
  and their growth (to Lv30 on PSU, Lv50 on AotI).
- **Types:** change type at the Job Counter. PSU: its ten types (Hunter to Protranser). AotI adds
  Acrofighter, Acrotecher, Fighmaster, Gunmaster and Masterforce, with their own levels and
  requirements.
- **Techniques:** attack techniques (damage from TP) and support techniques on other players: Resta and
  Giresta heal, Giresta revives, Reverser cures, Shifta, Deband, Zodial, Retier and Megistar buff for a
  while. Technique levels grow with use.
- **Shops and items:** NPC shops (buy and sell), grinding weapons with grinders, Lumilass (free), synthesis
  (boards and materials in My Room).
- **My Room:** via the Colony elevators: furniture (room goods from the goods shop), the item storage,
  synthesis, the partner machine (give items; Probo growth), visiting other players' rooms.
- **Events:** when the host runs them, seasonal lobby decorations and music, event item drops and EXP,
  meseta or drop campaigns, announced in the login notices and the news ticker.

## Known gaps

- PSU's own story (Episode 1) was offline only and is not served. AotI's Episode 3 story missions were
  online-only downloads that are not on the disc, so they are not available.
- Mixed PSU and AotI parties: a mission is played only by members on the leader's disc; the others
  stay behind at the counter.
- AotI's Acromaster type is not offered (it was never a retail type).
- Some lobby exit labels can show the wrong area name right after logging in.
- The PC-only areas (Falz Memoria, Rykros) do not exist on the PS2 discs.
- Real PS2s are less tested than PCSX2; mission loads on real hardware may still stall.
- AotI is the less tested disc: PSU and AotI players together, AotI-only items and late story chapters
  most of all. The Cheat Device codes have not yet been run on a real console; reports are very welcome.
- Only the PS2 discs are supported. The PC and Xbox 360 versions cannot connect.

## Rules

Be decent to other players. Hosts can kick and suspend accounts. Please report bugs to your server's host
with the time it happened, which disc you were on, what you did and what you saw.
