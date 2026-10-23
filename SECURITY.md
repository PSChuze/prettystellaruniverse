# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use the repository's private reporting
instead: **Security > Report a vulnerability** on the repository page (GitHub private vulnerability
reporting). Include what is affected (service, command, file), how to reproduce it, and what an
attacker gains. You should get an answer within a week.

In scope: anything in this repository that lets someone take over or read another player's account or
character, crash or stall a service for everyone, read or write files or the database outside what the
protocol allows, reach the admin port, the panel port or GM commands without being an admin, or makes a
server leak its host's private address.

## What is weak on purpose

The discs are from 2006 and 2007 and their online mode cannot be changed, so some things are weak by
design and are not vulnerabilities in this project:

- **SSL 3.0 with RC4-SHA** between console and server, with a certificate the client does not verify.
  Anyone on the path can read and alter that traffic, including the login ID and password. Players
  should use a password they use nowhere else (PLAYING.md says so).
- **The DNAS check** is bypassed on the client; there is no console authentication.
- **Accounts are created on first login** when `PSU_AUTOREGISTER=1` (the default).
- **The client is trusted** for much of what it reports (positions, many item actions), as the original
  service did. Server-side checks are added where a client could hurt other players; reports of a way to
  hurt other players are welcome.

## Hosts

- Keep login (22110 / 22140), game (22061), the admin port (22190) and the account link API (22192) on
  loopback; only the patch ports and the SSLv3 terminator face players (docs/hosting.md). The OpenLobby
  panel port (`PSU_PANEL_PORT`, off by default) needs `PSU_PANEL_TOKEN` when it is bound to anything but
  loopback.
- `state/admin.token` and `psu.db` are secrets; `logs/` holds login IDs, character names and chat.
- Passwords are stored as salted PBKDF2-SHA256 hashes and never logged.
