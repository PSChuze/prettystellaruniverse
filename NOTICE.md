# Notice and attribution

## Trademarks

Phantasy Star, Phantasy Star Universe and Phantasy Star Universe: Ambition of the Illuminus are
trademarks or registered trademarks of SEGA Corporation. PlayStation and DNAS are trademarks of Sony
Interactive Entertainment. This project is an independent, non-commercial re-implementation of the
games' online service. It is not affiliated with, endorsed or sponsored by SEGA or Sony. The names are
used only to say which games this server works with.

## No game data

This repository contains no SEGA code, art, text, game data, certificates or keys, and none is
distributed with it or with its container image. Running a server needs data built from discs you own
(docs/data.md). Do not open issues or pull requests containing game files, disc images, patched images,
xdelta patches, captured traffic from the official servers, or key material.

## EGS

The protocol work started from EGS, the open-source server for the PC version of Phantasy Star Universe
by Loic Hoguin and contributors (https://github.com/essen/egs). EGS's protocol documentation is in the
public domain; its code is licensed under the GNU Affero General Public License v3. This project
re-implements the protocol in Python from those descriptions and from observing the PS2 clients; it
contains no EGS code. Modules that took a packet layout or a sequence from EGS name the EGS source in
their docstring.

EGS also supplies material the data build uses. It is fetched from github.com/essen/egs at a pinned
commit when a host builds the game data, and never vendored into this repository or the image:

- the lobby and counter quest archives: quest-0 and quests 1100000 to 1104000 (the Colony, the
  Spaceport and the planet lobbies), built with EGS's own tools;
- the lobby zone source the seasonal Colony archives are built from (zone-1100000-0);
- two tables captured from the official PC service, `ppcube.bin` (the PP cube dialog) and
  `packet170c.bin` (counter background locations).

They keep EGS's license and are treated like game data: never committed here.

## Retail numbers

Some tables (for example the retail type level curves) use numbers published by the PSU community,
mainly PSUPedia (https://psupedia.info). Only ids and numbers are used, with their sources cited where
they are built; no game text.

## Reverse engineering

Packet layouts and game rules specific to the PS2 discs were worked out by observing the clients and by
static analysis of the game executables, for interoperability with the discs' own online mode. Client
routine addresses in the docstrings refer to the retail US executables, which this repository does not
contain.

## License

This project is licensed under the GNU General Public License, version 3 or (at your option) any later
version (GPL-3.0-or-later). See LICENSE.
