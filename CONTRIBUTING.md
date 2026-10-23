# Contributing

Thanks for helping bring PSU's online mode back. This page says where things are, how to check a
change and what a pull request needs.

## The one hard rule: no game data

Never commit, attach or paste SEGA data: disc images, NBL / AFS / CVM archives, JSON tables built from
them, captured traffic from the official servers, certificates or keys from the disc, patched images or
xdelta patches. Not in code, tests, issues or pull requests. Checks read the contributor's own local data
(docs/data.md) and skip what is missing. Short facts needed for interoperability (a field offset, a
command number, a client routine address, a formula) are fine and belong in the module docstring.

## Where things are

- `server/psu/`: the services and one module per feature; start with [docs/architecture.md](docs/architecture.md).
- Protocol notes are the module docstrings; [docs/protocol.md](docs/protocol.md) is the index and
  [docs/discs.md](docs/discs.md) lists what differs between PSU and AotI.
- `extract/`: builds the game data from the discs ([docs/data.md](docs/data.md)).
- `tools/`: the scripted test player, the packet checks, admin CLIs and the local DNS.

## Checking a change

```sh
python tools/xgame_check.py
python tools/testplay.py --game both --scenario all
```

Both must pass (see [docs/development.md](docs/development.md)); a change to seasons, events, the panel
or the help desk also runs `tools/season_check.py` or `tools/panel_check.py`. If a change is meant to
change bytes a client receives, update the digests in `xgame_check.py` in the same commit and say why.
A new feature gets a `testplay` scenario or checks in an existing one; a change that differs per disc
gets a check for both discs. CI builds the image and starts every service, but it has no game data, so
paste the summary lines of these checks into the pull request.

## Style

- Python 3.12, standard library only.
- Keep the dispatcher in `game.py` thin; feature logic goes in its module.
- A handler that learns a field documents it in the module docstring: the command, the offset, what it
  means, and the client routine that reads or sends it if known.
- Comments say why, or what the client expects. Keep the history of how something was found in the
  commit message, not in the code.
- Per-disc behaviour keys on `games.current()` (the session's disc) or the recipient's disc.
- Never log a password (login blanks the 0x0219 field in every log).
- Commit messages: one line that says what changed and why.

## Reporting bugs

Say which disc (PSU or AotI, and the serial), PCSX2 or a PS2, the time, what you did and what you saw.
Hosts: the matching lines from `events.jsonl` help most; remove other players' names, chat and account
IDs before posting logs.

Security issues: see [SECURITY.md](SECURITY.md), not the public tracker.

## License of contributions

By contributing you agree that your contribution is licensed under GPL-3.0-or-later, like the rest of
the project.
