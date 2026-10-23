# Hosting a server

This page is for you if you want to run a Phantasy Star Universe server for yourself, your family or
your friends. You do not need to know anything about networks or programming: a setup program asks
three questions and does the rest. The second half of the page says what it does, for doing it by
hand.

## What you need

- A computer that stays on while people play: Windows 10 or 11, a Mac with Apple silicon, or Linux.
  About 10 GB of free disk space.
- Your own disc image of **Phantasy Star Universe** (SLUS-21194), of **Ambition of the Illuminus**
  (SLUS-21631), or of both, as `.iso` files ([discs.md](discs.md) says how to make one). The server's
  game data is built from them on your computer; none of it comes with the server.
- Docker Desktop (free). The server runs inside it. On Linux, Docker Engine.

## Steps

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it. If it asks
   you to sign in, you can skip that. On Linux, install
   [Docker Engine](https://docs.docker.com/engine/install/) instead.
2. Download PSU Server Setup from this project's Releases page:
   - Windows: `PSU-Server-Setup.exe`
   - Mac: `PSU-Server-Setup-mac.zip` (open it, then right-click the program and choose Open the first
     time, because it is not signed by Apple)
   - Linux: `PSU-Server-Setup-linux-x86_64` (make it runnable: `chmod +x`)

   From a clone of this repository, `python setup.py` does the same (Python 3.8 or newer, nothing to
   install).
3. Double-click it. A window with text opens. If Docker Desktop is not running yet, setup starts it
   and waits.
4. Answer the three questions (below).
5. Wait. Setup shows one line per step and roughly how long it takes; the first time takes 30 to
   40 minutes, mostly building the game data from your discs, and needs the internet once (the
   lobby builder, EGS, is downloaded from GitHub).
6. At the end setup shows one line to send your players, like this:

   ```
   Tell your players: primary DNS 10.0.0.20, a DNAS-bypassed disc (PLAYING.md), any new user ID.
   ```

Setup puts the server's files in a folder called **PSU Server** in your home folder and says where it
is. Everything it does is written to `logs/setup.log` in that folder.

## The three questions

**1. Who will play on this server?**

- *Just people on my home network*: everyone plays from the same home (the same router) as this
  computer. Nothing on your router needs changing.
- *Friends over the internet*: people play from their own homes. Setup finds your internet address and
  asks your router to let the game through (see "Router"). If you have a name for your address (a
  "dynamic DNS" name), you can type it; setup looks it up, because the game needs the address itself.

**2. Where are your Phantasy Star Universe discs?**

Setup looks in Downloads, Documents, Desktop, Games and PCSX2's game folders and shows what it finds.
If your images are somewhere else, type the folder, or drag an `.iso` into the window. It tells the two
games apart by the disc itself, so file names do not matter. One game is enough; with both, PSU and
AotI players share one world. Setup only reads the images.

**3. Give your server a name.**

Players see it in the notice at login. You can change it later.

## What to tell players

Players need three things ([PLAYING.md](../PLAYING.md) is their guide):

1. Their own copy of the game with the **DNAS bypass**: PCSX2 players copy the pnach file for their
   disc from [patches/](../patches/README.md); everyone else needs a patched disc image. Setup's menu
   has **Make a patched disc image for a player**: pick an image (a plain copy of the retail disc) and
   setup writes `<name> [ONLINE].iso` next to it. Do not share patched images: they are the whole game.
2. **DNS**: the disc looks up these names, and they have to lead to your server:

   ```
   loginps2.psuus.segaonline.jp
   patchps2.psuus.segaonline.jp
   loginps2.ipsuus.segaonline.jp
   patchps2.ipsuus.segaonline.jp
   ```

   At home, players set their console's (or PCSX2's) **primary DNS** to this computer's address, the
   one setup shows. The DNS server setup starts answers those names with your server and passes
   everything else on. It only answers your home network. Friends over the internet run
   `python tools/devdns.py --answer <your internet address>` on a computer at their home (it answers
   the four names and refuses everything else) and point their console's DNS at that computer, or add
   the four names to a DNS server of their own.
3. **An account**: any new user ID and password at the login screen makes the account.

## Router

When friends play over the internet, your router must pass their connections on to this computer
(opening, or forwarding, ports). Setup asks the router to do it (UPnP), which works on most home
routers, and tells you whether it worked. If not, open these on the router's settings page (often
http://10.0.0.1 or the address printed on the router, under "Port forwarding" or "Virtual server"),
pointing at this computer:

| Port | Type | What |
|---|---|---|
| 11110 | TCP | PSU patch |
| 11140 | TCP | AotI patch |
| 12110 | TCP | PSU login |
| 12140 | TCP | AotI login |
| 12061 | TCP | game |

Never forward port 53: the DNS server is for your home network only.

Some internet providers share one internet address between many customers. Then friends cannot reach
a server at your home whatever you do on the router; setup warns you when it sees this.

Players inside your home on an "internet" server reach it at your internet address too, which some
routers do not allow (no "NAT loopback"). If that happens, they use this computer's home address.

## Later: the menu

Run PSU Server Setup again for a menu:

- **Start the server**, **Stop the server**. The server also starts by itself whenever Docker starts,
  so set Docker Desktop to start when you log in.
- **Show what to tell players**.
- **Change settings**: who plays and the address, the server's name and login notice, who can make an
  account, game masters, the DNS server, a new admin panel token.
- **Update**: gets the newest release from GitHub, rebuilds the server and, when the game data builder
  changed, the game data. Your accounts and settings stay.
- **Rebuild the game data**: from the same or other disc images.
- **Pair with an OpenLobby admin panel** (below).
- **Make a patched disc image for a player** (above).
- **Make a backup**, **Restore a backup**, **Player accounts** (list, reset a password),
  **Show what the server is doing**, **Check that the server works**, **Open the router ports
  again**, **Remove the server from this computer**.

## OpenLobby admin panel

An [OpenLobby](https://github.com/PrettyOpenLobby/OpenLobby) admin panel on the same computer can show
who is online, send announcements, kick, ban, edit the news and events, and answer players' help
requests. When setup sees a panel running, it asks "An OpenLobby admin panel is running here. Manage
Phantasy Star Universe from it?" Say yes, then in the panel open Settings > Modules > Pair with a
setup program and type the 8-character code it shows. That is all: the panel and the server find each
other on the `openlobby` Docker network, and no token is copied by hand. To pair later, choose **Pair
with an OpenLobby admin panel** in the menu.

The panel also shows when a newer release is out and what changed; updating stays in setup (choose
Update).

## Backups

**Make a backup** writes `backups/psu-<date>-<time>.zip` in the server folder: every account,
character, card and mail (`psu.db`, copied safely while the server runs), the news and the events.
**Restore a backup** puts one back (play since then is lost). Keep a copy of the `backups` folder and
of `.env` somewhere else too.

To back up every night without opening the menu, schedule `PSU-Server-Setup --backup` (or
`python setup.py --backup`): Task Scheduler on Windows, or cron on Linux and macOS:

```
30 4 * * *  cd "$HOME/PSU Server" && python3 setup.py --backup
```

## Doing it by hand

The server folder is this repository. Setup does exactly this:

```sh
cp psu.env.example .env        # then set PSU_GAME_ADDR and PSU_PANEL_TOKEN in it
docker network create openlobby
docker compose run --rm -v "/path/to/discs:/discs:ro" extract \
    --psu "/discs/Phantasy Star Universe (USA).iso" \
    --aoti "/discs/Phantasy Star Universe - Ambition of the Illuminus (USA).iso" \
    --out /data
docker compose up -d
```

What runs ([compose.yml](../compose.yml)):

| service | ports | what |
|---|---|---|
| `patch` | 11110, 11140 | the patch servers (plain TCP) |
| `ssl3-psu` | 12110, 12061 | SSLv3 terminator for PSU's login and the game server |
| `ssl3-aoti` | 12140 | SSLv3 terminator for AotI's login |
| `login` | (22110, 22140 inside) | accounts; hands every client `PSU_GAME_ADDR`:12061 |
| `game` | (22061, admin 22191 inside) | the game world; the admin port is on the `openlobby` network only |
| `dns` | 53/udp | profile `dns`: the DNS server for consoles, when no other install runs one |
| `extract` | | profile `extract`: builds the game data from your discs ([data.md](data.md)) |

Folders: `data/` (game data from your discs), `state/` (`psu.db`, `news.txt`, `events.json`), `logs/`.
Every setting is in [psu.env.example](../psu.env.example), with what it does.

**The SSLv3 terminator.** The PS2 discs log in over SSL 3.0 with one cipher, RC4-SHA, which current
TLS libraries refuse. `ssl3-psu` and `ssl3-aoti` run the `ghcr.io/prettyopenlobby/openlobby-ssl3`
image (OpenSSL 1.1.1 built with SSLv3, and stunnel) with [compose/stunnel-psu.conf](../compose/stunnel-psu.conf)
and [compose/stunnel-aoti.conf](../compose/stunnel-aoti.conf) mounted as `/etc/stunnel/stunnel.conf`.
On its first start the image makes a CA and one RSA-1024 certificate per login name (`SSL3_CN`) in the
shared `/certs` volume, at `SSL3_CERT`; the discs accept any RSA certificate, so nothing is installed
on the consoles. Each `[section]` accepts on a public port and connects to the plain backend by its
compose name (`login:22110`, `game:22061`). Under host networking, where compose names do not resolve,
the image's `SSL3_CONNECT_HOST` rewrites every `connect` host (for example to `127.0.0.1`).

**Networking.** Docker Desktop has no host networking, so the stack uses Docker's own network with
published ports. Behind the terminators every login and game connection comes from the terminator's
address; the server does not need the players' own.

**The DNS server** is shared by every OpenLobby install on the machine, because a console takes one
DNS address and there is one port 53. Setup looks for a running one (`docker ps --filter
label=org.openlobby.role=dns`), starts this folder's `dns` service only when there is none, and adds
the four names under the key `psu`:

```sh
docker exec <the dns container> python stub.py dns-names add psu loginps2.psuus.segaonline.jp \
    patchps2.psuus.segaonline.jp loginps2.ipsuus.segaonline.jp patchps2.ipsuus.segaonline.jp
```

On Linux with systemd-resolved (which holds 127.0.0.53:53), set `PSU_DNS_BIND` to this machine's
network address.

**Experimental legs.** The Xbox 360 disc's patch leg (11200) and the Ambition of the Illuminus PC client
(patch 11230, login 12230) are in compose under the `experimental` profile, off by default and not set
up by setup. Neither has been run end to end. To try them, add `experimental` to `COMPOSE_PROFILES`
and set `PSU_LOGIN_PORTS=22110,22140,22230` in `.env` (`patch-extra` serves 11200 and 11230).

## Words you may see

- **Address**: how consoles find your server, like `10.0.0.20` at home or `203.0.113.5` on the
  internet.
- **DNS**: the service that turns a name (`loginps2.psuus.segaonline.jp`) into an address. The discs
  ask it where Sega's servers were; yours answers with this computer.
- **DNAS**: Sony's online check, shut down long ago. The bypass skips it on the console.
- **Port**: a numbered door on a computer. The game uses five.
- **Docker** (Docker Desktop): a free program that runs the server in its own sealed-off space.
- **`.env`**: the file in the server folder that holds the settings. Setup writes it.
