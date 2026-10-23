"""Scripted test player: runs the server locally and drives client-side scenarios on both discs.

Starts psu.login + psu.game as subprocesses on 127.0.0.1 with a throwaway $PSU_STATE_DIR / $PSU_LOG_DIR, the
host's NBL archives and a copy of its game tables. Bots log in as PSU (login port 22110) or AotI (22140) and send
what the PS2 clients send (sequences and layouts as documented in the server's module docstrings). Scenarios
(--list): character create / select, lobby, zone and map changes, the Spaceport counter (0x0D04 writes), NPC shop
buy / sell, the client's item pack staying equal to the saved one (invsync), grinding, synthesis, My Room,
missions (counter, accept, zones, hits, drops, clear), parties, techniques, the AotI story counter 115, staged
story acts from $PSU_NBL_EXTRA when a host has built them (ep3), chat, a PSU <-> AotI trade, a mixed party and GM
chat commands (the admin bots are listed in the server's PSU_ADMINS). Every check looks at server replies, and a
scenario fails on handler errors or fragment ack timeouts in events.jsonl.

  python tools/testplay.py [--game psu|aoti|both] [--scenario NAME|all] [--keep] [--verbose] [--list]
                           [--work DIR] [--json FILE] [--ports BASE]

--work: the game data, either the directory extract writes (docs/data.md) or a work/ directory (default:
$PSU_DATA, $PSU_WORK, else work/ next to the repository checkout). --keep leaves the temp dir (logs, state)
for a look afterwards. --ports BASE runs game on BASE and the admin port on BASE + 1 instead of free ports and
leaves the login service out (bots take their keys from the DB), for a machine where a server already runs.
Exit status 0 when every check passed.

Login: the login service only knows the disc by its port (login.GAME: 22110 PSU, 22140 AotI), so it is started
there when those ports are free on 127.0.0.1; otherwise the bots take their key straight from the shared DB
(db.issue_key), as the login service would.
"""
import argparse
import asyncio
import json
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.normpath(os.path.join(HERE, "..", "server"))
ACK = struct.pack("<I", 8) + b"\x0b\x05\x00\x00"
NO_PARTY = 0xFFFFFFFF

# A PS2 0x0D02 body as the PSU client sends it (the AotI client loads the same character): slot 0, name,
# race / gender / class, appearance. The name is replaced per bot.
RAW_0D02 = bytes.fromhex(
    "000000000000000000000000000000000000000000000000000000000000000000000000000000005400650073007400"
    "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000017f0000000006000000060001000600020000000301010004002800050000000000000000"
    "00000000ff7f00003cb60200ff7f0000ff7f000000000000ef8d0000ffff020079d400009a3f0100da1d0100ffff0000"
    "ffff00000f070000136a0000010000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000010000000100000001000000010000000100000001000000010000000100000001000000"
    "01000000010000000100000001000000010000000100000001000000")


def pkt(cmd, body, channel=2):
    size = 8 + len(body)
    pad = (-size) % 4
    return struct.pack("<I", size + pad) + struct.pack(">H", cmd) + bytes([channel, 0]) + body + bytes(pad)


def u32(v):
    return struct.pack("<I", v & 0xFFFFFFFF)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def port_free(p):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def layout(root):
    """(nbl, gamedata, aoti gamedata, captures) under root: the data directory extract writes (nbl/, gamedata/
    with aoti/ inside, captures/), or a work/ directory (lobby-nbl/ps2, psu/gamedata, aoti/gamedata, ref/egs/p)."""
    if os.path.isdir(os.path.join(root, "nbl")):
        return (os.path.join(root, "nbl"), os.path.join(root, "gamedata"), os.path.join(root, "gamedata", "aoti"),
                os.path.join(root, "captures"))
    return (os.path.join(root, "lobby-nbl", "ps2"), os.path.join(root, "psu", "gamedata"),
            os.path.join(root, "aoti", "gamedata"), os.path.join(root, "ref", "egs", "p"))


def default_work():
    for c in (os.environ.get("PSU_DATA"), os.environ.get("PSU_WORK"), os.path.join(HERE, "..", "..", "work")):
        if c and os.path.isdir(layout(c)[0]):
            return os.path.normpath(c)
    return None


class Fail(Exception):
    pass


# --- the local server ------------------------------------------------------------------------------------------

class Server:
    def __init__(self, work, keep=False, ports=None):
        self.work, self.keep = work, keep
        self.tmp = tempfile.mkdtemp(prefix="psu-testplay-")
        self.state = os.path.join(self.tmp, "state")
        self.logs = os.path.join(self.tmp, "logs")
        self.gd = os.path.join(self.tmp, "gamedata")
        os.makedirs(os.path.join(self.logs, "sessions"))
        nbl, gamedata, aoti, captures = layout(work)
        shutil.copytree(gamedata, self.gd)
        if not os.path.isdir(os.path.join(self.gd, "aoti")) and os.path.isdir(aoti):
            shutil.copytree(aoti, os.path.join(self.gd, "aoti"))
        # ports: a base port; game and admin then use base and base + 1, and login is left out (its disc is
        # known only from its fixed ports), so a run stays clear of a server already on the default ports
        self.game_port = ports if ports else free_port()
        self.real_login = not ports and port_free(22110) and port_free(22140)
        self.login_ports = {"psu": 22110, "aoti": 22140}
        # staged story acts for the ep3 scenario, when a host has built them
        staged = os.environ.get("PSU_NBL_EXTRA") or os.path.join(nbl, "ep3-staging")
        self.env = dict(os.environ, PSU_STATE_DIR=self.state, PSU_LOG_DIR=self.logs,
                        PSU_NBL_DIR=nbl, PSU_GAMEDATA=self.gd,
                        PSU_NBL_EXTRA=os.pathsep.join(p for p in staged.split(os.pathsep) if os.path.isdir(p)),
                        PSU_CAPTURE_DIR=captures, PSU_GAME_PORTS=str(self.game_port),
                        PSU_LOGIN_PORTS="22110,22140", PSU_BIND="127.0.0.1", PSU_GAME_ADDR="127.0.0.1",
                        PSU_GAME_PORT=str(self.game_port), PSU_IDLE_SECS="120", PYTHONPATH=SERVER,
                        PSU_BOARD_SECS="1", PSU_EVENTS_WATCH_SECS="1",
                        PYTHONUNBUFFERED="1")
        for k in ("PSU_ADMINS", "PSU_SHOP_TIER", "PSU_ZONE_INVENTORY", "PSU_LOBBY_MAP", "PSU_LOBBY_ENTRY"):
            self.env.pop(k, None)
        self.env["PSU_ADMINS"] = "tpgmapsu,tpgmaaoti,tpinvpsu,tpinvaoti"     # gm and invsync admin bots
        self.env["PSU_ADMIN_PORT"] = str(ports + 1 if ports else free_port())
        self.procs = []
        # this process builds character states with the server's own modules (fixtures)
        for k in ("PSU_STATE_DIR", "PSU_LOG_DIR", "PSU_NBL_DIR", "PSU_NBL_EXTRA", "PSU_GAMEDATA", "PSU_CAPTURE_DIR"):
            os.environ[k] = self.env[k]
        if SERVER not in sys.path:
            sys.path.insert(0, SERVER)

    def start(self):
        out = open(os.path.join(self.tmp, "server.out"), "w")
        mods = ["psu.game"] + (["psu.login"] if self.real_login else [])
        for m in mods:
            self.procs.append(subprocess.Popen([sys.executable, "-m", m], cwd=SERVER, env=self.env,
                                               stdout=out, stderr=subprocess.STDOUT))
        deadline = time.time() + 20
        ports = [self.game_port] + (list(self.login_ports.values()) if self.real_login else [])
        for p in ports:
            while True:
                try:
                    socket.create_connection(("127.0.0.1", p), 0.5).close()
                    break
                except OSError:
                    if time.time() > deadline or any(pr.poll() is not None for pr in self.procs):
                        raise SystemExit("server did not start: see " + os.path.join(self.tmp, "server.out"))
                    time.sleep(0.2)

    def stop(self):
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        if not self.keep:
            shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def events_path(self):
        return os.path.join(self.logs, "events.jsonl")

    def mark(self):
        try:
            return os.path.getsize(self.events_path)
        except OSError:
            return 0

    def events(self, since=0):
        out = []
        try:
            with open(self.events_path, "rb") as f:
                f.seek(since)
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
        return out

    def db(self):
        c = sqlite3.connect(os.path.join(self.state, "psu.db"), timeout=10)
        c.execute("PRAGMA busy_timeout=5000")
        return c

    def char_row(self, gid, slot=0):
        with self.db() as c:
            return c.execute("SELECT raw, state FROM characters WHERE gid = ? AND slot = ?", (gid, slot)).fetchone()

    def char_state(self, gid, slot=0):
        row = self.char_row(gid, slot)
        return json.loads(row[1]) if row else {}

    def save_state(self, gid, st, slot=0):
        with self.db() as c:
            c.execute("UPDATE characters SET state = ? WHERE gid = ? AND slot = ?", (json.dumps(st), gid, slot))

    def issue_key(self, user, password, game):
        from psu import db as dbm, login
        d = dbm.DB(self.state)
        gid, why = d.login(user, password, login.gid_for)
        if gid is None:
            raise Fail(f"login refused: {why}")
        return gid, d.issue_key(gid, game)


# --- one test player -------------------------------------------------------------------------------------------

class Bot:
    def __init__(self, srv, name, game, verbose=False):
        self.srv, self.name, self.game, self.verbose = srv, name, game, verbose
        self.user = name.lower()[:23]
        self.gid, self.key, self.lid = None, None, 0
        self.rx = []                # [(cmd, body)] in arrival order (fragments reassembled)
        self.cond = asyncio.Condition()
        self.w = None
        self.closed = False
        self.nb = 0                 # the client's 0x0806 / 0x0808 counter
        self.area = None
        self.frag = {}

    def log(self, *a):
        if self.verbose:
            print(f"    [{self.name}]", *a, flush=True)

    async def login(self):
        if not self.srv.real_login:
            self.gid, self.key = self.srv.issue_key(self.user, "testplay", self.game)
            return
        r, w = await asyncio.open_connection("127.0.0.1", self.srv.login_ports[self.game])
        w.write(pkt(0x080E, bytes(0x24) + bytes([0, 1, 0, 1 if self.game == "aoti" else 0]) + bytes(16)))
        w.write(pkt(0x0219, bytes(0x24) + self.user.encode("latin1").ljust(24, b"\0") + b"testplay".ljust(24, b"\0")))
        await w.drain()
        buf = b""
        deadline = time.time() + 10
        while self.gid is None:
            if time.time() > deadline:
                raise Fail("no 0x0223 from the login service")
            buf += await asyncio.wait_for(r.read(4096), 10)
            while len(buf) >= 8 and len(buf) >= struct.unpack_from("<I", buf)[0]:
                size, = struct.unpack_from("<I", buf)
                p, buf = buf[:size], buf[size:]
                if struct.unpack_from(">H", p, 4)[0] == 0x0223:
                    if len(p) != 8 + 0x2C:
                        raise Fail("login refused (0x0223 error)")
                    self.gid, self.key = struct.unpack_from("<I", p, 8 + 0x24)[0], p[8 + 0x28:8 + 0x2C]
        w.write(pkt(0x0217, bytes(0x24)))
        await w.drain()
        w.close()

    async def connect(self):
        r, self.w = await asyncio.open_connection("127.0.0.1", self.srv.game_port)
        self.task = asyncio.create_task(self._reader(r))

    async def _reader(self, r):
        buf = b""
        try:
            while True:
                data = await r.read(65536)
                if not data:
                    break
                buf += data
                while len(buf) >= 8:
                    size, = struct.unpack_from("<I", buf)
                    if size < 8 or len(buf) < size:
                        break
                    p, buf = buf[:size], buf[size:]
                    cmd, = struct.unpack_from(">H", p, 4)
                    if cmd == 0x0B03:                       # fragment: ack each one (the server waits)
                        total, off = struct.unpack_from("<II", p, 8)
                        self.frag.setdefault(total, bytearray(total))[off:off + size - 16] = p[16:size]
                        self.w.write(ACK)
                        if off + size - 16 >= total:
                            p = bytes(self.frag.pop(total))
                            cmd, = struct.unpack_from(">H", p, 4)
                        else:
                            continue
                    if size == 8 and cmd == 0:
                        continue                            # keepalive
                    await self._got(cmd, p[8:])
        except (ConnectionError, OSError):
            pass
        finally:
            self.closed = True
            async with self.cond:
                self.cond.notify_all()

    async def _got(self, cmd, body):
        if cmd == 0x0208 and len(body) >= 0x28:         # own zone load done: DestLID = my LID (0x0205 also
            self.lid, = struct.unpack_from("<H", body, 0)  # comes about other players)
        async with self.cond:
            self.rx.append((cmd, body))
            self.cond.notify_all()
        self.log(f"<- {cmd:#06x} ({len(body) + 8} B)")

    def send(self, cmd, payload=b"", channel=2, head=None):
        body = (head if head is not None else bytes(0x24)) + payload
        self.log(f"-> {cmd:#06x} {payload[:24].hex()}")
        self.w.write(pkt(cmd, body, channel))

    def mark(self):
        return len(self.rx)

    def got(self, cmd, since=0, pred=None):
        return [b for c, b in self.rx[since:] if c == cmd and (pred is None or pred(b))]

    async def wait(self, cmd, since=None, pred=None, timeout=6.0):
        """The first packet `cmd` (matching pred) received at or after index `since`; None on timeout."""
        since = self.mark() if since is None else since
        deadline = asyncio.get_running_loop().time() + timeout
        async with self.cond:
            while True:
                for c, b in self.rx[since:]:
                    if c == cmd and (pred is None or pred(b)):
                        return b
                left = deadline - asyncio.get_running_loop().time()
                if left <= 0 or self.closed:
                    return None
                try:
                    await asyncio.wait_for(self.cond.wait(), left)
                except asyncio.TimeoutError:
                    return None

    async def settle(self, t=0.3):
        await self.w.drain()
        await asyncio.sleep(t)

    def close(self):
        if self.w is not None:
            self.w.close()

    # --- client flows (what the PS2 sends, in its order) ---

    def head(self):
        """The 0x24-byte header the client puts on game packets that carry its id (0x0105, 0x010A, ...)."""
        return bytes(0x24)

    def post_load(self, mission=False):
        """After each 0x0236 the client reports in: 0x0806 n, 0x0815 (0x0814 in a mission), 0x010B, 0x0808 n+1."""
        self.nb += 1
        self.send(0x0806, u32(self.nb))
        self.send(0x0814 if mission else 0x0815)
        q, z, m = self.area or (1100000, 0, 4)
        self.send(0x010B, u32(self.gid) + bytes(12) + struct.pack("<fff", 0.0, 0.0, 0.0) + bytes(8)
                  + struct.pack("<III", q & 0xFFFFFFFF, z, m) + bytes(8))
        self.nb += 1
        self.send(0x0808, u32(self.nb))


def chk(results, ok, what, evidence=""):
    results.append((bool(ok), what, "" if ok else evidence))
    return bool(ok)


def hexs(b, n=48):
    return b[:n].hex() if b else "none"


# --- shared steps -------------------------------------------------------------------------------------------

def build_state(srv, bot, raw, seed=None):
    """The state the server makes for a new character (progression.new_state + the starter pack), then `seed`."""
    from psu import games, items, progression
    games.set_current(bot.game)
    st = progression.new_state(raw)
    st["inv"], st["pas"], st["next_uuid"] = items.starter(raw, 1)
    if seed:
        seed(st)
    return st


def add(st, key, n=1, **extra):
    from psu import items
    slot, err = items.add_item(st, key, n)
    if err:
        raise Fail(f"fixture: {key}: {err}")
    slot.update(extra)
    return slot


async def enter(bot, R, seed=None, create=True):
    """Key auth -> character list -> create (slot 0) -> select -> load -> lobby."""
    await bot.login()
    await bot.connect()
    if not chk(R, await bot.wait(0x0202, since=0), f"{bot.name}: 0x0202 hello on connect"):
        raise Fail("no hello")
    bot.send(0x080E, bytes([0, 1, 1 if bot.game == "aoti" else 0, 1 if bot.game == "aoti" else 0]) + bytes(20))
    m = bot.mark()
    bot.send(0x020D, u32(bot.gid) + bot.key + bytes(8), head=b"\xff\xff" + bytes(0x22))
    if not chk(R, await bot.wait(0x0D05, since=m), f"{bot.name}: 0x020D key auth -> 0x0D05 account flags"):
        raise Fail("auth refused")
    m = bot.mark()
    bot.send(0x0D06)
    lst = await bot.wait(0x0D03, since=m)
    chk(R, lst, f"{bot.name}: 0x0D06 -> 0x0D03 character list")
    if create:
        raw = bytearray(RAW_0D02)
        raw[0x28:0x68] = bot.name.encode("utf-16-le")[:64].ljust(64, b"\0")
        bot.send(0x0D02, bytes(raw[0x24:]))
        await bot.settle(0.2)
        for _ in range(50):
            row = bot.srv.char_row(bot.gid)
            if row is not None:
                break
            await asyncio.sleep(0.1)
        chk(R, row is not None, f"{bot.name}: 0x0D02 create stores the character", "no characters row")
        if row is None:
            raise Fail("character not stored")
        bot.srv.save_state(bot.gid, build_state(bot.srv, bot, bytes(raw), seed))
    m = bot.mark()
    bot.send(0x020B, u32(0) + bytes(8))
    done = await bot.wait(0x021B, since=m, timeout=10)
    chk(R, bot.got(0x0D01, m) and bot.got(0x0A0A, m) and done, f"{bot.name}: 0x020B select -> 0x0D01, 0x0A0A .. 0x021B",
        f"got {[hex(c) for c, _ in bot.rx[m:]]}")
    m = bot.mark()
    bot.send(0x021C, u32(0) + bytes(8))
    z = await bot.wait(0x0236, since=m, timeout=10)
    bot.area = (1100000, 0, 4)
    chk(R, z and bot.got(0x020F, m) and bot.got(0x0201, m), f"{bot.name}: 0x021C -> lobby (0x020F zone, 0x0201, 0x0236)",
        f"got {[hex(c) for c, _ in bot.rx[m:]]}")
    if not z:
        raise Fail("no lobby")
    bot.post_load()
    await bot.settle()
    return lst


async def warp(bot, R, q, z, m, e=0, what=None, expect=True, party=NO_PARTY, mission=False, nb=1):
    """0x0807 area change; returns the packets of the load (or None when nothing came)."""
    k = bot.mark()
    bot.send(0x0807, struct.pack("<IHHHHI", q, z & 0xFFFF, m & 0xFFFF, e & 0xFFFF, nb, party))
    done = await bot.wait(0x0236, since=k, timeout=10)
    if expect:
        chk(R, done, what or f"{bot.name}: 0x0807 {q}/{z}/{m}/{e} -> 0x0236", f"got {[hex(c) for c, _ in bot.rx[k:]]}")
    if done:
        zi = bot.got(0x0205, k)
        if zi:
            zone, mp = struct.unpack_from("<II", zi[-1], 0x28)
            q0 = bot.got(0x0C00, k)
            qq = struct.unpack_from("<I", q0[-1], 0x24)[0] if q0 else (bot.area or (q,))[0]
            bot.area = (qq, zone, mp)
        bot.post_load(mission=mission)
        await bot.settle(0.2)
    return k if done else None


async def errors_since(srv, mark, R):
    await asyncio.sleep(0.3)
    evs = srv.events(mark)
    errs = [e for e in evs if e.get("ev") == "error"]
    for e in errs:
        chk(R, False, f"server handler error on {e.get('cmd')}", f"{e.get('error')} @ {e.get('where')}")
    tos = [e for e in evs if e.get("ev") == "frag_ack_timeout"]
    chk(R, not tos, "no fragment ack timeouts", f"{len(tos)} timeouts")
    if not errs:
        chk(R, True, "no server handler errors")


def item_db(game):
    from psu import games, items
    games.set_current(game)
    return items.db()


def find_item(game, name, cat=None):
    for k, v in item_db(game).items():
        if v["name"] == name and (cat is None or v["cat"] == cat):
            return k
    raise Fail(f"no item named {name} on {game}")


def inv_slot(st, key):
    return next((i for i, x in enumerate(st.get("inv", [])) if x["id"] == key), None)


# --- scenarios ---------------------------------------------------------------------------------------------

async def sc_login(srv, game, R, opt):
    """Create a character, select it, enter the lobby; then reconnect and find it in the list."""
    b = Bot(srv, f"TpLog{game}", game, opt.verbose)
    await enter(b, R)
    chk(R, b.got(0x0C00) and b.got(0x020E), "lobby load has 0x0C00 quest init + 0x020E quest NBL")
    lid = b.lid
    b.close()
    await asyncio.sleep(0.5)
    b2 = Bot(srv, f"TpLog{game}", game, opt.verbose)
    await b2.login()
    await b2.connect()
    await b2.wait(0x0202, since=0)
    b2.send(0x080E, bytes([0, 1, 0, 0]) + bytes(20))
    b2.send(0x020D, u32(b2.gid) + b2.key + bytes(8), head=b"\xff\xff" + bytes(0x22))
    await b2.wait(0x0D05)
    m = b2.mark()
    b2.send(0x0D06)
    lst = await b2.wait(0x0D03, since=m)
    chk(R, lst and b2.name.encode("utf-16-le") in lst, "re-login: 0x0D03 slot 0 holds the created character",
        hexs(lst, 0x80))
    m = b2.mark()
    b2.send(0x020B, u32(0) + bytes(8))
    chk(R, await b2.wait(0x021B, since=m, timeout=10), "re-login: select -> 0x021B")
    m = b2.mark()
    b2.send(0x021C, u32(0) + bytes(8))
    chk(R, await b2.wait(0x0236, since=m, timeout=10), "re-login: lobby again (0x0236)")
    chk(R, lid == b2.lid == 0, f"alone in the lobby: LID 0 (got {lid}, {b2.lid})")
    b2.close()


async def sc_zones(srv, game, R, opt):
    """Map change in the lobby, zone change to Parum, the Spaceport counter flow with its 0x0D04 work writes."""
    b = Bot(srv, f"TpZone{game}", game, opt.verbose)
    await enter(b, R)
    k = await warp(b, R, 1100000, 0, 1, 0x14, what="map change in the Colony lobby (0/1) -> 0x0236")
    if k is not None:
        chk(R, b.got(0x0205, k) and not b.got(0x020F, k), "map change: 0x0205 location only, no zone NBL")
    k = await warp(b, R, 1100000, 3, 1, 1, what="native zone 3 (Parum) -> 0x0236")
    if k is not None:
        q0 = b.got(0x0C00, k)
        chk(R, q0 and struct.unpack_from("<I", q0[0], 0x24)[0] == 1101000, "Parum: quest 1101000 loaded",
            hexs(q0[0] if q0 else None, 0x30))
    await warp(b, R, 1101000, 2, 4, 0, what="back to the Colony (native zone 2 map 4) -> 0x0236")
    # Spaceport counter (as the AotI client does it): object event, SAVE_LC00..05 written, warp to 1104000/0/900
    b.send(0x0F0A, bytes.fromhex("030005000b0004007e0000007b040000090000007b00000000000000") + b"\xff" * 20
           + bytes.fromhex("00000000bc632900000000000900140100000000"))
    m = b.mark()
    for n, v in ((5, 0), (4, 2), (3, 1), (2, 0), (1, 0), (0, 1)):
        b.send(0x0D04, f"SAVE_LC{n:02d}".encode().ljust(12, b"\0") + u32(0x1C) + bytes([2, 2, 0, 0]) + u32(0)
               + struct.pack("<i", v))
    await b.settle(0.5)
    acks = b.got(0x0D04, m)
    chk(R, len(acks) == 6, f"Spaceport: six 0x0D04 work writes acked ({len(acks)})")
    k = await warp(b, R, 1104000, 0, 900, 0, what="Spaceport counter warp 1104000/0/900 -> 0x0236")
    if k is not None:
        chk(R, b.got(0x0D01, k), "Spaceport: 0x0D01 character (SAVE_LC work) before the zone load")
        mp = b.got(0x0205, k)
        want = 901 if game == "aoti" else 900
        chk(R, mp and struct.unpack_from("<I", mp[-1], 0x2C)[0] == want, f"Spaceport: map {want} for {game}",
            hexs(mp[-1] if mp else None, 0x3C))
    m = b.mark()
    for n in (5, 4, 3, 2, 1, 0):
        b.send(0x0D04, f"SAVE_LC{n:02d}".encode().ljust(12, b"\0") + u32(2) + bytes([2, 2, 0, 0]) + u32(0)
               + b"\xff" * 4)
    await b.settle(0.5)
    chk(R, len(b.got(0x0D04, m)) == 6, f"Spaceport: six -1 writes acked ({len(b.got(0x0D04, m))})")
    k = await warp(b, R, 1104000, 2, 4, 3, what="Spaceport -> Colony (1104000/2/4/3) -> 0x0236")
    if k is not None:
        q0 = b.got(0x0C00, k)
        chk(R, q0 and struct.unpack_from("<I", q0[0], 0x24)[0] == 1100000, "Spaceport exit lands in quest 1100000",
            hexs(q0[0] if q0 else None, 0x30))
    b.close()


async def sc_shop(srv, game, R, opt):
    """Colony weapon shop (NPC 30, map 101): enter, buy the first entry, sell it back."""
    b = Bot(srv, f"TpShop{game}", game, opt.verbose)
    await enter(b, R, seed=lambda st: st.update(meseta=50000))
    await warp(b, R, 1100000, 0, 101, 3, what="Colony shop map 101 -> 0x0236")
    m = b.mark()
    b.send(0x1A01, u32(0) + u32(30) + bytes(12))
    chk(R, await b.wait(0x1A02, since=m), "talk to NPC 30 (0x1A01) -> 0x1A02")
    m = b.mark()
    b.send(0x010A, u32(b.gid) + u32(b.lid) + bytes([1, 0, 0, 0]) + u32(30) + bytes(8))
    lst = await b.wait(0x010A, since=m)
    n = lst[0x2E] if lst else 0
    if not chk(R, lst and lst[0x2C] == 1 and not lst[0x2D] & 1 and n > 0, f"shop enter (0x010A 1) -> list ({n} items)",
               hexs(lst, 0x40)):
        return b.close()
    m = b.mark()
    b.send(0x010A, u32(b.gid) + u32(b.lid) + bytes([2, 0, 1, 0]) + u32(0) + bytes(8))
    bought = await b.wait(0x010A, since=m, pred=lambda x: x[0x2C] == 2)
    if not chk(R, bought and not bought[0x2D] & 1, "buy entry 0 (0x010A 2) -> bought record", hexs(bought, 0x40)):
        return b.close()
    chk(R, b.got(0x0115, m), "buy: 0x0115 meseta refresh")
    uuid, = struct.unpack_from("<I", bought, 0x34)
    st = srv.char_state(b.gid)
    chk(R, any(x["uuid"] == uuid for x in st.get("inv", [])) and st.get("meseta", 0) < 50000,
        f"buy: item {uuid:#x} in the saved inventory, meseta {st.get('meseta')} < 50000")
    paid = st.get("meseta", 0)
    m = b.mark()
    b.send(0x010A, u32(b.gid) + u32(b.lid) + bytes([3, 0, 1, 0]) + u32(uuid) + bytes(8))
    sold = await b.wait(0x010A, since=m, pred=lambda x: x[0x2C] == 3)
    chk(R, sold and not sold[0x2D] & 1 and struct.unpack_from("<I", sold, 0x30)[0] == uuid,
        "sell it (0x010A 3) -> sold, same UUID", hexs(sold, 0x40))
    st = srv.char_state(b.gid)
    chk(R, not any(x["uuid"] == uuid for x in st.get("inv", [])) and st.get("meseta", 0) > paid,
        f"sell: item gone, meseta {paid} -> {st.get('meseta')}")
    b.send(0x010A, u32(b.gid) + u32(b.lid) + bytes([5, 0, 0, 0]) + u32(30) + bytes(8))
    b.close()


def a0a_prices(a0a):
    """{uuid: (item id hex, unit price the client shows)} from a 0x0A0A body: +0x24 count, variable records at
    +0x168 (UUID +4, id +8), constants at +0x9D8 (price +0x30, what the online sell menu shows: 0xb80f0)."""
    out = {}
    for i in range(a0a[0x24]):
        v, c = 0x168 + i * 0x24, 0x9D8 + i * 0x48
        out[struct.unpack_from("<I", a0a, v + 4)[0]] = (a0a[v + 8:v + 12].hex(), struct.unpack_from("<I", a0a, c + 0x30)[0])
    return out


async def shop_open(b, R, npc):
    """Talk to shop NPC `npc` and ask for its list (0x010A 1); returns the list body or None."""
    m = b.mark()
    b.send(0x1A01, u32(0) + u32(npc) + bytes(12))
    chk(R, await b.wait(0x1A02, since=m), f"talk to NPC {npc} (0x1A01) -> 0x1A02")
    m = b.mark()
    b.send(0x010A, u32(b.gid) + u32(b.lid) + bytes([1, 0, 0, 0]) + u32(npc) + bytes(8))
    return await b.wait(0x010A, since=m)


async def shop_sell(srv, b, R, uuid, qty, price, what):
    """Sell qty of `uuid` (0x010A 3); checks the reply and that meseta grew by price * qty, nothing else moved."""
    before = srv.char_state(b.gid)
    m = b.mark()
    b.send(0x010A, u32(b.gid) + u32(b.lid) + bytes([3, 0, qty, 0]) + u32(uuid) + bytes(8))
    sold = await b.wait(0x010A, since=m, pred=lambda x: x[0x2C] == 3)
    chk(R, sold and not sold[0x2D] & 1 and sold[0x2E] == qty and struct.unpack_from("<I", sold, 0x30)[0] == uuid,
        f"sell {what} x{qty} ({uuid:#x}) -> 0x010A 3 sold", hexs(sold, 0x40))
    after = srv.char_state(b.gid)
    gain = after.get("meseta", 0) - before.get("meseta", 0)
    chk(R, gain == price * qty, f"sell {what}: meseta +{gain}, the client's price {price} x{qty}")
    left = {x["uuid"]: x["qty"] for x in after.get("inv", [])}
    want = {x["uuid"]: x["qty"] - (qty if x["uuid"] == uuid else 0) for x in before.get("inv", [])}
    want = {k: v for k, v in want.items() if v > 0}
    chk(R, left == want, f"sell {what}: only that item changed in the saved inventory",
        f"{ {hex(k): v for k, v in left.items()} } vs { {hex(k): v for k, v in want.items()} }")


async def sc_shop_sell(srv, game, R, opt):
    """Sell several different items (weapon, shield, a Monomate stack, PA disc, picked-up materials with ground
    UUIDs, a bought weapon) and check each one pays its own price; the buy takes a character UUID."""
    from psu import items
    b = Bot(srv, f"TpSell{game}", game, opt.verbose)

    def seed(st):
        st["meseta"] = 50000
        for uuid, key in ((0x40000002, "07100000"), (0x40000004, "07040000"), (0x40000005, "07040000")):
            items.add_item(st, key, 1, uuid=uuid)               # picked up: keep the ground UUID (game.py 0x0106)
        add(st, "04070400")
    await enter(b, R, seed=seed)
    a0a = b.got(0x0A0A)
    shown = a0a_prices(a0a[-1]) if a0a else {}
    db = item_db(game)
    chk(R, shown and all(p == (db[k].get("price") or 0) for k, p in shown.values()),
        f"0x0A0A: every constants price is items.json price ({len(shown)} items)")
    await warp(b, R, 1100000, 0, 101, 3, what="Colony shop map 101 -> 0x0236")
    lst = await shop_open(b, R, 30)
    if not chk(R, lst and lst[0x2E] > 0, f"weapon shop list ({lst[0x2E] if lst else 0} items)", hexs(lst, 0x40)):
        return b.close()
    held = {x["uuid"] for x in srv.char_state(b.gid).get("inv", [])}
    m = b.mark()
    b.send(0x010A, u32(b.gid) + u32(b.lid) + bytes([2, 0, 1, 0]) + u32(0) + bytes(8))
    bought = await b.wait(0x010A, since=m, pred=lambda x: x[0x2C] == 2)
    if not chk(R, bought and not bought[0x2D] & 1, "buy entry 0 -> bought record", hexs(bought, 0x40)):
        return b.close()
    buuid, = struct.unpack_from("<I", bought, 0x34)
    chk(R, buuid < 0x40000000 and buuid not in held,
        f"buy: UUID {buuid:#x} is a character UUID (below the ground range 0x40000000) and new")
    shown[buuid] = (bought[0x38:0x3C].hex(), struct.unpack_from("<I", bought, 0x54 + 0x30)[0])
    plan = []
    for x in srv.char_state(b.gid).get("inv", []):
        it = db[x["id"]]
        if it["cat"] in (1, 2, 3, 4, 7) and (it.get("price") or 0) > 0 and not x.get("active"):
            plan.append((x["uuid"], 2 if it["cat"] == 3 and x["qty"] >= 2 else 1, it["name"]))
    names = {n for _, _, n in plan}
    chk(R, len(plan) >= 6 and {"Par Wood", "Harnium", "Monomate"} <= names, f"sell plan: {sorted(names)}")
    prices = set()
    for uuid, qty, name in plan:
        price = shown.get(uuid, (None, None))[1]
        if not chk(R, price is not None, f"{name} ({uuid:#x}) has a price on the client"):
            continue
        prices.add(price)
        await shop_sell(srv, b, R, uuid, qty, price, name)
    chk(R, len(prices) >= 4, f"different items sold for different prices: {sorted(prices)}")
    b.send(0x010A, u32(b.gid) + u32(b.lid) + bytes([5, 0, 0, 0]) + u32(30) + bytes(8))
    b.close()


async def sc_disc_shop(srv, game, R, opt):
    """The Guardians Colony disc shop (NPC 32: list 3, category 3 Disk) for a level 1 character: the list is not
    empty (the client closes the window on 0 entries), buy a disk, sell it back for its price."""
    from psu import games, shop as npcshop
    games.set_current(game)
    b = Bot(srv, f"TpDisc{game}", game, opt.verbose)
    await enter(b, R, seed=lambda st: st.update(meseta=50000))
    chk(R, list(npcshop.data()["npcs"][32]) == [3, 3], f"shops.json: NPC 32 is list 3 category 3 (Disk)")
    await warp(b, R, 1100000, 0, 101, 3, what="Colony shop map 101 -> 0x0236")
    lst = await shop_open(b, R, 32)
    n = lst[0x2E] if lst else 0
    if not chk(R, lst and lst[0x2C] == 1 and not lst[0x2D] & 1 and n > 0 and lst[0x2F] == 3,
               f"disc shop list: {n} entries, category {lst[0x2F] if lst else None}", hexs(lst, 0x40)):
        return b.close()
    db = item_db(game)
    ids = [lst[0x30 + 4 + i * 0x4C + 0x30:0x30 + 4 + i * 0x4C + 0x34].hex() for i in range(n)]
    chk(R, all(db.get(i, {}).get("cat") in (4, 5, 6) for i in ids), f"disc shop sells disks only ({ids[:4]} ...)")
    m = b.mark()
    b.send(0x010A, u32(b.gid) + u32(b.lid) + bytes([2, 0, 1, 0]) + u32(0) + bytes(8))
    bought = await b.wait(0x010A, since=m, pred=lambda x: x[0x2C] == 2)
    if not chk(R, bought and not bought[0x2D] & 1 and bought[0x38:0x3C].hex() == ids[0],
               f"buy entry 0 ({db[ids[0]]['name']}) -> bought record", hexs(bought, 0x40)):
        return b.close()
    buuid, = struct.unpack_from("<I", bought, 0x34)
    st = srv.char_state(b.gid)
    chk(R, st.get("meseta") == 50000 - db[ids[0]]["value_base"], f"buy: paid {db[ids[0]]['value_base']} (meseta {st.get('meseta')})")
    price, = struct.unpack_from("<I", bought, 0x54 + 0x30)
    chk(R, price == db[ids[0]].get("price"), f"bought disk's constants price {price} = items.json price")
    await shop_sell(srv, b, R, buuid, 1, price, db[ids[0]]["name"])
    b.send(0x010A, u32(b.gid) + u32(b.lid) + bytes([5, 0, 0, 0]) + u32(32) + bytes(8))
    b.close()


class ClientPack:
    """The item pack as the PS2 client keeps it (PSU addresses; AotI has the same rules): records in order
    {uuid, id, qty, max}. Loaded from 0x0A0A as sent; an add (0x0106 6 pickup / 0x0B add, 0x010A bought: 0x145c50)
    merges a stackable (category 3, 7, 12, 15 unless id byte 3 & 0x40: 0xafb70) into the first live record with
    the same id (0xf5f70), its quantity clamped to that record's max (+0x10 of the variable record: 0xb0870 ->
    0xb0780 / 0xb0630), else appends with the sent UUID; a zone load's 0x0A06 hands the UUIDs out in order to the
    live records (0x97fb0, an empty stack takes none) and the rebuild drops the empty stacks (0x144fb0)."""

    def __init__(self, a0a):
        self.recs = []
        for i in range(a0a[0x24]):
            self.recs.append(self.rec(a0a, 0x168 + i * 0x24 + 4))

    @staticmethod
    def rec(b, o):
        uuid, iid, qty = struct.unpack_from("<I4sI", b, o)
        return {"uuid": uuid, "id": iid.hex(), "qty": qty, "max": struct.unpack_from("<h", b, o + 0xC)[0]}

    @staticmethod
    def stackable(iid):
        i = bytes.fromhex(iid)
        return i[0] in (3, 7, 12, 15) and not i[3] & 0x40

    def live(self, r):
        return not self.stackable(r["id"]) or r["qty"] & 0x7FFFFFFF > 0

    def add(self, b, o):
        r = self.rec(b, o)
        if r["id"].startswith("12"):                    # meseta (0xafb60)
            return
        if self.stackable(r["id"]):
            held = next((x for x in self.recs if x["id"] == r["id"] and self.live(x)), None)
            if held is not None:
                if 0 <= r["qty"] < 100:
                    held["qty"] = max(0, min(held["qty"] + r["qty"], held["max"]))
                return
        self.recs.append(r)

    def zone_load(self, a06):
        ids = [u for u in struct.unpack_from("<60I", a06, 0x24) if u != 0xFFFFFFFF]
        for r in self.recs:
            if self.live(r) and ids:
                r["uuid"] = ids.pop(0)
        self.recs = [r for r in self.recs if self.live(r)]

    def rows(self):
        return [(r["uuid"], r["id"], r["qty"] if self.stackable(r["id"]) else 1) for r in self.recs]   # +0xC is
                                                                                    # no count for the rest


def saved_rows(st):
    return [(x["uuid"], x["id"], x["qty"]) for x in st.get("inv", [])]


async def sc_invsync(srv, game, R, opt):
    """Client and server item packs stay identical: a Par Wood and two Harnium added one by one (the same client
    add 0x145c50 as the mission pickups), a zone load (0x0A06 hands out UUIDs by position), a weapon bought, another
    zone load; the client's pack (modelled from what the server sent) must equal the saved one, and selling the
    weapon by the client's UUID must sell the weapon. A material record with max 0 makes the client's second merge
    empty the stack, and the UUIDs of every later item shift by one."""
    b = Bot(srv, f"TpInv{game}", game, opt.verbose)          # PSU_ADMINS lists tpinvpsu, tpinvaoti (!give)
    await enter(b, R, seed=lambda st: st.update(meseta=50000))
    a0a = b.got(0x0A0A)
    if not chk(R, a0a, "0x0A0A at load"):
        return b.close()
    pack = ClientPack(a0a[-1])
    chk(R, pack.rows() == saved_rows(srv.char_state(b.gid)), "client pack from 0x0A0A = saved inventory")

    def compare(what):
        mine, saved = pack.rows(), saved_rows(srv.char_state(b.gid))
        return chk(R, mine == saved, f"{what}: client pack = saved inventory (order, UUIDs, ids, quantities)",
                   f"client {[(hex(u), i, q) for u, i, q in mine]} vs saved {[(hex(u), i, q) for u, i, q in saved]}")

    for name in ("Par Wood", "Harnium", "Harnium"):
        key = find_item(game, name, 7)
        k = b.mark()
        say(b, f"!give {key}")
        add = await b.wait(0x0106, since=k, pred=lambda x: x[0x2D] == 0x0B)
        if not chk(R, add, f"!give {name} -> 0x0106 add"):
            return b.close()
        pack.add(add, 0x34)
    hn = [r for r in pack.recs if r["id"] == find_item(game, "Harnium", 7)]
    chk(R, hn and hn[0]["qty"] == 2, f"client: the two Harnium are one stack of 2 (max {hn[0]['max'] if hn else None})",
        f"{hn}")

    async def zone(q, z, m, e, what):
        k = await warp(b, R, q, z, m, e, what=what)
        a06 = b.got(0x0A06, k or 0)
        if chk(R, k is not None and a06, f"{what}: 0x0A06 item UUIDs"):
            pack.zone_load(a06[-1])
        return compare(what)

    await zone(1100000, 3, 1, 1, "zone load to Parum")
    await zone(1100000, 0, 101, 3, "zone load to the Colony shop map")
    lst = await shop_open(b, R, 30)
    if not chk(R, lst and lst[0x2E] > 0, "weapon shop list", hexs(lst, 0x40)):
        return b.close()
    m = b.mark()
    b.send(0x010A, u32(b.gid) + u32(b.lid) + bytes([2, 0, 1, 0]) + u32(0) + bytes(8))
    bought = await b.wait(0x010A, since=m, pred=lambda x: x[0x2C] == 2)
    if not chk(R, bought and not bought[0x2D] & 1, "buy a weapon -> bought record", hexs(bought, 0x40)):
        return b.close()
    pack.add(bought, 0x34)
    wid = bought[0x38:0x3C].hex()
    compare("after the buy")
    b.send(0x010A, u32(b.gid) + u32(b.lid) + bytes([5, 0, 0, 0]) + u32(30) + bytes(8))
    await zone(1100000, 3, 1, 1, "zone load to Parum")
    await zone(1100000, 0, 101, 3, "zone load back to the shop map")
    wuuid = next((r["uuid"] for r in pack.recs if r["id"] == wid), None)
    lst = await shop_open(b, R, 30)
    before = {x["uuid"]: x["id"] for x in srv.char_state(b.gid).get("inv", [])}
    m = b.mark()
    b.send(0x010A, u32(b.gid) + u32(b.lid) + bytes([3, 0, 1, 0]) + u32(wuuid or 0) + bytes(8))
    await b.wait(0x010A, since=m, pred=lambda x: x[0x2C] == 3)
    after = {x["uuid"]: x["id"] for x in srv.char_state(b.gid).get("inv", [])}
    gone = [before[u] for u in before if u not in after]
    chk(R, gone == [wid], f"sell the client's weapon UUID {wuuid:#x} -> the server sells the weapon {wid}" if wuuid
        else "the client holds the bought weapon", f"server removed {gone}")
    b.send(0x010A, u32(b.gid) + u32(b.lid) + bytes([5, 0, 0, 0]) + u32(30) + bytes(8))
    b.close()


async def sc_grind(srv, game, R, opt):
    """Grind the starter weapon with Grinder C+1: rate quote (0x0A09) then commit (0x0105 event 8)."""
    b = Bot(srv, f"TpGrind{game}", game, opt.verbose)
    from psu import econ, games
    games.set_current(game)
    gd = econ.data("grind")
    holder = {}

    def seed(st):
        w = next(x for x in st["inv"] if x["id"] in gd.get("weapons", {}))
        holder["weapon"] = w["id"]
        for _ in range(5):
            add(st, "0f010000")
    await enter(b, R, seed=seed)
    st = srv.char_state(b.gid)
    wslot = inv_slot(st, holder["weapon"])
    for attempt in range(5):
        st = srv.char_state(b.gid)
        gslot = inv_slot(st, "0f010000")
        if gslot is None:
            break
        g = st["inv"][gslot]
        m = b.mark()
        b.send(0x0A09, bytes([1, gslot]) + struct.pack("<H", wslot))
        q = await b.wait(0x0A09, since=m)
        chance = struct.unpack_from("<H", q, 0x26)[0] if q else None
        if attempt == 0:
            chk(R, q and chance not in (None, 0xFFFF), f"grind quote (0x0A09) -> chance {chance}", hexs(q, 0x30))
        m = b.mark()
        b.send(0x0105, u32(b.gid) + u32(b.lid) + bytes([gslot, 8, 0x0F, wslot]) + u32(g["uuid"]) + bytes(4))
        r = await b.wait(0x0106, since=m, pred=lambda x: x[0x2D] == 0x0D)
        rm = await b.wait(0x0106, since=m, pred=lambda x: x[0x2D] == 0x0C)
        if attempt == 0:
            chk(R, r is not None, "grind commit (0x0105 8) -> 0x0106 event 0x0D", hexs(r, 0x40))
            chk(R, rm is not None, "grind commit -> 0x0106 0x0C grinder removed", hexs(rm, 0x40))
        if r is None:
            break
        if r[0x2E]:
            st = srv.char_state(b.gid)
            w = next(x for x in st["inv"] if x["id"] == holder["weapon"])
            chk(R, w.get("grind", 0) >= 1, f"grind success saved: +{w.get('grind')}")
            rec_grind = r[0x34 + 0x12] >> 4
            chk(R, rec_grind == w.get("grind"), f"grind reply record shows +{rec_grind} (saved +{w.get('grind')})",
                hexs(r[0x34:0x54], 0x20))
            break
    else:
        chk(R, True, "grind: no success in 5 tries (random), commits answered")
    b.close()


async def sc_myroom(srv, game, R, opt):
    """Enter My Room from the Colony elevator, place a goods item, take it back, leave."""
    b = Bot(srv, f"TpRoom{game}", game, opt.verbose)
    goods = sorted(k for k in item_db(game) if k.startswith("0b01"))[0]
    await enter(b, R, seed=lambda st: add(st, goods))
    k = await warp(b, R, 1120000, 0, 100, 0, what="Colony elevator to My Room (1120000/0/100) -> 0x0236")
    if k is None:
        return b.close()
    room = await b.wait(0x1327, since=k, timeout=3)
    chk(R, room and struct.unpack_from("<I", room, 0x24)[0] == b.gid, "0x1327 room data, owner = me", hexs(room, 0x30))
    b.send(0x1328, u32(b.gid))
    st = srv.char_state(b.gid)
    g = next(x for x in st["inv"] if x["id"] == goods)
    m = b.mark()
    b.send(0x130B, u32(b.gid) + struct.pack("<hh", 1, 0) + u32(g["uuid"]) + bytes(8))
    put = await b.wait(0x130C, since=m)
    chk(R, put and struct.unpack_from("<hh", put, 0x28) == (1, int(goods[4:6], 16)),
        f"place goods {goods} in spot 1 (0x130B) -> 0x130C", hexs(put, 0x30))
    chk(R, b.got(0x0106, m, lambda x: x[0x2D] == 0x0C), "place: 0x0106 removes it from the inventory")
    m = b.mark()
    b.send(0x130B, u32(b.gid) + struct.pack("<hh", 1, 0) + u32(0xFFFFFFFF) + bytes(8))
    back = await b.wait(0x130C, since=m)
    chk(R, back and struct.unpack_from("<hh", back, 0x28)[1] == 0xFF, "take it back (0x130B -1) -> 0x130C empty",
        hexs(back, 0x30))
    chk(R, b.got(0x0106, m, lambda x: x[0x2D] == 0x0B), "take back: 0x0106 adds it to the inventory")
    k = await warp(b, R, 1120000, 2, 1, 0, what="leave My Room (1120000/2/1) -> 0x0236")
    if k is not None:
        q0 = b.got(0x0C00, k)
        chk(R, q0 and struct.unpack_from("<I", q0[0], 0x24)[0] == 1100000, "My Room exit lands in the Colony",
            hexs(q0[0] if q0 else None, 0x30))
    b.close()


async def sc_synthesis(srv, game, R, opt):
    """My Room synthesis: register a board, pick its materials, start, take the product."""
    b = Bot(srv, f"TpSynth{game}", game, opt.verbose)
    from psu import games, synthesis
    games.set_current(game)
    bid = "0d010000"
    bd = synthesis.lookup(bid)
    mats = [bd["materials"][k][0] for k in range(4)]

    def seed(st):
        add(st, bid)
        stock = st.setdefault("room_stock", [])
        for i, mt in enumerate(mats):
            if mt["id"] not in ("ffffffff", "00000000"):
                stock.append({"uuid": 0x31000000 + i, "id": mt["id"], "qty": mt["qty"] * 2, "active": 0, "slots": 0,
                              "grind": 0, "pa": [0, 0]})
    await enter(b, R, seed=seed)
    if await warp(b, R, 1120000, 0, 100, 0, what="to My Room for synthesis -> 0x0236") is None:
        return b.close()
    b.send(0x1328, u32(b.gid))
    st = srv.char_state(b.gid)
    board = next(x for x in st["inv"] if x["id"] == bid)

    def c130f(sub, P=0, Q=0, uuid=0, rid=None):
        rec = u32(uuid) + bytes.fromhex(rid) if rid else bytes(8)
        return u32(b.gid) + struct.pack("<hh", sub, P) + bytes(4) + u32(Q) + bytes(8) + u32(uuid) + rec + bytes(0x18)

    def c130d(sub):
        return u32(b.gid) + u32(sub) + bytes(0x10)
    m = b.mark()
    b.send(0x130F, c130f(2, -1, 0, board["uuid"], bid))
    rm = await b.wait(0x0106, since=m, pred=lambda x: x[0x2D] == 0x0C)
    chk(R, rm, f"register board {bid} (0x130F 2) -> 0x0106 board removed", hexs(rm, 0x40))
    m = b.mark()
    b.send(0x130D, c130d(7))
    await b.settle(0.5)
    chk(R, b.got(0x130E, m) or b.got(0x131B, m) or b.got(0x1310, m), "production list (0x130D 7) answered",
        f"got {[hex(c) for c, _ in b.rx[m:]]}")
    m = b.mark()
    b.send(0x130F, c130f(6, 0, 0))
    chk(R, await b.wait(0x130E, since=m), "select entry 0 (0x130F 6) -> 0x130E")
    m = b.mark()
    b.send(0x130D, c130d(8))
    chk(R, await b.wait(0x131B, since=m), "open the board (0x130D 8) -> 0x131B output/materials")
    m = b.mark()
    b.send(0x130F, c130f(3, 0, 0, 0, mats[0]["id"]))
    chk(R, await b.wait(0x131B, since=m), "pick slot 0 material (0x130F 3) -> 0x131B")
    m = b.mark()
    b.send(0x130F, c130f(0xB, 0, 0))
    chk(R, await b.wait(0x130E, since=m, pred=lambda x: struct.unpack_from("<i", x, 0x28)[0] == 0xA),
        "start (0x130F 0xB) -> 0x130E 0xA started")
    m = b.mark()
    b.send(0x130D, c130d(6))
    res = await b.wait(0x131C, since=m)
    chk(R, res, "result (0x130D 6) -> 0x1310 / 0x130E / 0x131B / 0x131C", f"got {[hex(c) for c, _ in b.rx[m:]]}")
    r1310 = b.got(0x1310, m)
    out_id = r1310[0][0x30:0x34].hex() if r1310 else None   # +0x2C record: UUID, id
    m = b.mark()
    b.send(0x130F, c130f(0xD, 0, 1))
    add_ = await b.wait(0x0106, since=m, pred=lambda x: x[0x2D] == 0x0B)
    chk(R, add_, f"take the product to the inventory (0x130F 0xD) -> 0x0106 add ({out_id})", "none")
    await b.settle(0.5)                  # the server saves after sending the 0x0106s
    st = srv.char_state(b.gid)
    chk(R, out_id and any(x["id"] == out_id for x in st["inv"]), f"product {out_id} in the saved inventory",
        f"saved inventory ids {[x['id'] for x in st['inv']]}")
    left = sum(x["qty"] for x in st.get("room_stock", []) if x["id"] == mats[0]["id"])
    same = [mt["qty"] for mt in mats if mt["id"] == mats[0]["id"]]       # materials stack: each slot takes its need
    chk(R, left == sum(same) * 2 - sum(same), f"materials consumed from the room stock ({left} of {sum(same) * 2} left,"
        f" the recipe needs {sum(same)})")
    b.close()


def pick_mission(game, table="0"):
    """First offered mission of the counter table, with its enter zone's enemies and clear flags."""
    from psu import games, missions
    games.set_current(game)
    d = missions.data(game)
    for q in d["counters"][table]["quests"]:
        mm = missions.mission(q, game)
        if mm and missions.shown(q) and mm.get("gateways", {}).get(str(mm["enter"][0])):
            return q, mm
    raise Fail(f"no mission at counter {table} on {game}")


def hit(pos, src, dst, w, D=None):
    """One 0x50-byte hit record (B/C blocks as the PSU client sends them)."""
    B = bytes.fromhex("0101bda5956ab5c38c9b804109941044")
    C = bytes.fromhex("010189249235c2c30c00604150370b44")
    D = D or bytes.fromhex("0000000000000000000000000000640000000000")
    return struct.pack("<fff", *pos) + struct.pack("<IIII", src, dst, w, 0) + B + C + D


# an enemy's striking block as its client sends it (0x1204: damaging 0x200 + 0x1000, no sure-hit 0x4000, power 0 so
# the server's enemy stats stand in); it rolls accuracy, so callers repeat it until it lands
ENEMY_D = bytes.fromhex("04120000") + bytes(16)


class Hits:
    def __init__(self):
        self.seq = 0

    def body(self, bot, hits):
        self.seq += 1
        return u32(len(hits)) + u32(bot.lid) + u32(self.seq) + b"".join(hits)


def e07_entries(body):
    n, = struct.unpack_from("<I", body, 0x24)
    out = []
    for i in range(n):
        e = body[0x28 + 0x68 * i:0x28 + 0x68 * (i + 1)]
        if len(e) < 0x68:
            break
        dmg, = struct.unpack_from("<I", e, 4)
        src, dst = struct.unpack_from("<II", e, 0x14)
        a_se, a_se2, a_hp = struct.unpack_from("<III", e, 0x24)
        t_se, t_se2, t_hp = struct.unpack_from("<III", e, 0x34)
        out.append(dict(dmg=dmg, src=src, dst=dst, a_hp=a_hp, t_se=t_se, t_se2=t_se2, t_hp=t_hp, a_se=a_se))
    return out


async def to_counter(b, R, counter=0, table=None):
    k = b.mark()
    b.send(0x0811, struct.pack("<BBHHHI", 1, 0, 0, 4, 6, counter) + bytes(12))
    ok = await b.wait(0x0236, since=k, timeout=10)
    chk(R, ok, f"{b.name}: enter counter {counter} (0x0811) -> counter load 0x0236", f"got {[hex(c) for c, _ in b.rx[k:]]}")
    if not ok:
        return False
    b.post_load()
    m = b.mark()
    b.send(0x1710, u32(counter))
    chk(R, await b.wait(0x1711, since=m), f"{b.name}: counter options (0x1710) -> 0x1711")
    m = b.mark()
    b.send(0x0C05, u32(counter))
    t = await b.wait(0x0C06, since=m, timeout=8)
    chk(R, t, f"{b.name}: counter files (0x0C05) -> 0x0C06 table", f"got {[hex(c) for c, _ in b.rx[m:]]}")
    b.send(0x170B)
    m = b.mark()
    b.send(0x0C0F, u32(counter))
    o = await b.wait(0x0C10, since=m)
    chk(R, o and o[0x24] == 1, f"{b.name}: mission options (0x0C0F) -> 0x0C10", hexs(o, 0x30))
    return bool(t)


async def accept(b, R, qid, expect_ok=True):
    m = b.mark()
    b.send(0x1705)
    b.send(0x1709)
    b.send(0x1707, u32(qid))
    b.send(0x0C01, u32(qid))
    r = await b.wait(0x0C02, since=m, timeout=8)
    if expect_ok:
        chk(R, r and r[0x24] == 0 and b.got(0x1015, m) and b.got(0x1020, m),
            f"{b.name}: accept {qid} (0x0C01) -> 0x1020, 0x1015, 0x0C02 0", f"0x0C02 {hexs(r[0x24:0x28] if r else None)}; "
            f"got {[hex(c) for c, _ in b.rx[m:]]}")
    return r


async def depart(b, R, qid, what=None):
    k = b.mark()
    b.send(0x0807, struct.pack("<IHHHHI", qid, 0xFFFF, 0xFFFF, 0xFFFF, 7, NO_PARTY))
    done = await b.wait(0x0236, since=k, timeout=12)
    chk(R, done and b.got(0x0215, k), what or f"{b.name}: depart into {qid} (0x0807 ffff) -> mission zone (0x0215 host, 0x0236)",
        f"got {[hex(c) for c, _ in b.rx[k:]]}")
    if not done:
        return None
    zi = b.got(0x0205, k)
    if zi:
        zone, mp = struct.unpack_from("<II", zi[-1], 0x28)
        b.area = (qid, zone, mp)
    m = b.mark()
    b.post_load(mission=True)
    await b.settle(0.3)
    return m


async def kill_all(b, R, mm, H, w, label, max_rounds=150, pos=(0.0, 0.0, 0.0)):
    """Report every enemy of the enter zone's gateways in (0x0402), then hit each until dead."""
    zone = str(mm["enter"][0])
    enemies = [e for g in mm["gateways"][zone] for e in g["enemies"]]
    m = b.mark()
    for e in enemies:
        b.send(0x0402, u32(e["id"] - 150) + struct.pack("<hhhh", -428, 0, 481, 0x2000)
               + bytes.fromhex("02002000ffffffffffffffff"))
    await b.settle(0.5)
    st08 = {struct.unpack_from("<I", x, 0x24)[0] for x in b.got(0x0E08, m)}
    chk(R, all(e["id"] in st08 for e in enemies), f"{label}: 0x0402 per enemy -> 0x0E08 status for all {len(enemies)}",
        f"0x0E08 for {sorted(st08)}; roster {[e['id'] for e in enemies]}")
    m = b.mark()
    alive = {e["id"] for e in enemies}
    rounds = 0
    landed = 0
    while alive and rounds < max_rounds:
        rounds += 1
        k = b.mark()
        b.send(0x0E00, H.body(b, [hit(pos, b.lid, eid, w) for eid in sorted(alive)[:20]]))
        r = await b.wait(0x0E07, since=k, timeout=4)
        if r is None:
            break
        for e in e07_entries(r):
            if e["dmg"] > 0:
                landed += 1
            if e["t_se"] & 0x20000 or (e["dst"] in alive and e["t_hp"] == 0):
                alive.discard(e["dst"])
    chk(R, landed > 0, f"{label}: hits (0x0E00) -> 0x0E07 with damage ({landed} landed)")
    chk(R, not alive, f"{label}: all {len(enemies)} enemies killed in {rounds} rounds", f"{len(alive)} still alive")
    await b.settle(0.5)
    return m, enemies


async def mission_run(b, R, qid, mm, H, w, label):
    m0 = await depart(b, R, qid)
    if m0 is None:
        return None
    start = await b.wait(0x0C09, since=m0, pred=lambda x: x[0x24] == 0, timeout=4)
    st_rule = (mm.get("quest") or {}).get("start")
    if st_rule == [100, 1]:
        chk(R, start, f"{label}: 0x0808 -> 0x1202 snapshot + 0x0C09 start", f"got {[hex(c) for c, _ in b.rx[m0:]]}")
    chk(R, b.got(0x1202, m0), f"{label}: join snapshot 0x1202 (conditions ready)")
    return await kill_all(b, R, mm, H, w, label)


async def finish_mission(b, R, qid, mm, m, label, drop_mark=None):
    """Kill EXP, drops + pickup, wave clears, then the clear rule's flags (0x0404) -> 0x0C09 results. drop_mark: pick
    up only drops since then (the drops of the zone the player is in; default m)."""
    exp = b.got(0x0115, m, lambda x: struct.unpack_from("<I", x, 0x2C)[0] not in (0xFFFFFFFF,))
    chk(R, exp, f"{label}: kills -> 0x0115 EXP", "no 0x0115 with an enemy id")
    drops = b.got(0x0A02, m if drop_mark is None else drop_mark)
    chk(R, b.got(0x1205, m), f"{label}: gateway cleared -> 0x1205 wave clear flag")
    if drops:
        d = drops[-1]                                   # the zone keeps the newest 50
        # 0x0A02 item: +0x24 GID? the 0x20-byte item from +0x34 (UUID first)
        uuid = struct.unpack_from("<I", d, 0x6C)[0]       # record: map, name, price, consts, item (UUID first)
        uuid = uuid if uuid & 0xF0000000 == 0x40000000 else None
        if uuid is None:
            chk(R, False, f"{label}: drop 0x0A02 carries a ground UUID", hexs(d, 0x60))
        else:
            k = b.mark()
            b.send(0x0106, u32(b.gid) + u32(b.lid) + bytes([0, 6, 0, 0]) + b"\xff" * 4 + u32(uuid) + bytes(0x1C))
            pk = await b.wait(0x0106, since=k, timeout=4)
            chk(R, pk and pk[0x2D] == 6, f"{label}: pick up drop {uuid:#x} (0x0106 6) -> 0x0106 pickup", hexs(pk, 0x40))
    else:
        chk(R, True, f"{label}: no drop rolled ({len(b.got(0x0115, m))} kills)")
    clear = (mm.get("quest") or {}).get("clear") or []
    flags = []
    zones = mm["zones"]
    i = 0
    while i < len(clear):
        if clear[i] == 120 and i + 3 < len(clear):
            z, mp, ev = clear[i + 1:i + 4]
            maps = zones.get(str(z), [])
            if mp in maps:
                flags.append(maps.index(mp) * 256 + ev)
            i += 4
        elif clear[i] in (100,):
            i += 2
        else:
            i += 1
    res = await b.wait(0x0C09, since=m, pred=lambda x: x[0x24] == 1, timeout=0.5)
    k = m
    if res is not None:          # the last gateway's wave-clear flag was the clear rule's flag
        chk(R, True, f"{label}: cleared by the last wave (0x0C09 clear with results)")
    else:
        k = b.mark()
        for f in flags:
            b.send(0x0404, u32(f) + u32(0))
        res = await b.wait(0x0C09, since=k, pred=lambda x: x[0x24] == 1, timeout=6)
        chk(R, res, f"{label}: clear flags {flags} (0x0404) -> 0x0C09 clear with results",
            f"got {[hex(c) for c, _ in b.rx[k:]]}")
    if res:
        chk(R, await b.wait(0x0115, since=k, timeout=4), f"{label}: clear -> 0x0115 rewards")
    return bool(res)


async def leave_mission(b, R, mm, label):
    ex = mm.get("exit") or [2, 4, 6]
    k = b.mark()
    b.send(0x0807, struct.pack("<IHHHHI", 1100000, ex[0] & 0xFFFF, ex[1] & 0xFFFF, ex[2] & 0xFFFF, 9, NO_PARTY))
    done = await b.wait(0x0236, since=k, timeout=12)
    chk(R, done and b.got(0x1006, k, lambda x: x[0x24] == 10), f"{label}: leave via the exit warp -> 0x1006 10 + lobby",
        f"got {[hex(c) for c, _ in b.rx[k:]]}")
    if done:
        b.post_load()
        await b.settle(0.2)


async def sc_mission(srv, game, R, opt):
    """Counter -> accept -> mission zone -> kill everything -> drops/pickup -> clear -> back."""
    b = Bot(srv, f"TpMis{game}", game, opt.verbose)
    qid, mm = pick_mission(game)
    await enter(b, R)
    st = srv.char_state(b.gid)
    wpn = next(x for x in st["inv"] if item_db(game)[x["id"]]["cat"] == 1)
    b.send(0x0105, u32(b.gid) + u32(b.lid) + bytes([inv_slot(st, wpn["id"]), 1, 0, 0]) + u32(wpn["uuid"]) + bytes(4))
    if not await to_counter(b, R, 0):
        return b.close()
    await accept(b, R, qid)
    H = Hits()
    r = await mission_run(b, R, qid, mm, H, wpn["uuid"], f"mission {qid}")
    if r is None:
        return b.close()
    m, _ = r
    await finish_mission(b, R, qid, mm, m, f"mission {qid}")
    await leave_mission(b, R, mm, f"mission {qid}")
    b.close()


async def sc_board(srv, game, R, opt):
    """Board and events (psu/board.py, psu/events.py): with a running event for this disc (EXP x3, a featured
    mission) the bot gets the "has begun" ticker, every kill's EXP is tripled, the clear is stored with the party and
    the board file lists the bot online, in the level ranking, under the mission and in the event's results."""
    b = Bot(srv, f"TpBrd{game}", game, opt.verbose)
    qid, mm = pick_mission(game)
    await enter(b, R)
    events_file = os.path.join(srv.state, "events.json")
    now = time.time()
    with open(events_file, "w", encoding="utf-8") as f:
        json.dump([{"key": "tp", "name": "TP Festival", "game": game, "exp": 3, "missions": [qid],
                    "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                    "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 3600))}], f)
    try:
        t = await b.wait(0x0228, since=0, pred=lambda x: "TP Festival has begun".encode("utf-16-le") in x, timeout=8)
        chk(R, t is not None, "the event's start reaches the player as a 0x0228 ticker")
        st = srv.char_state(b.gid)
        wpn = next(x for x in st["inv"] if item_db(game)[x["id"]]["cat"] == 1)
        b.send(0x0105, u32(b.gid) + u32(b.lid) + bytes([inv_slot(st, wpn["id"]), 1, 0, 0]) + u32(wpn["uuid"]) + bytes(4))
        if not await to_counter(b, R, 0):
            return b.close()
        await accept(b, R, qid)
        mark = srv.mark()
        r = await mission_run(b, R, qid, mm, Hits(), wpn["uuid"], f"board {qid}")
        if r is None:
            return b.close()
        m, _ = r
        await finish_mission(b, R, qid, mm, m, f"board {qid}")
        gains = [e["exp"] for e in srv.events(mark) if e.get("ev") == "exp"]
        chk(R, gains and all(x > 0 and x % 3 == 0 for x in gains), "kill EXP is tripled by the event", f"{gains}")
        board_file = os.path.join(srv.logs, "board", "board.json")
        snap, deadline = None, time.time() + 6
        while time.time() < deadline:
            try:
                with open(board_file, encoding="utf-8") as f:
                    snap = json.load(f)
            except (OSError, ValueError):
                snap = None
            if snap and any(b.name in x["party"] for x in snap["recent"]):   # a file written after this clear
                break
            await asyncio.sleep(0.5)
        chk(R, snap is not None, "the board file is written", board_file)
        if snap is None:
            return b.close()
        chk(R, any(p["name"] == b.name and p["where"] == "mission" for p in snap["online"]["players"]),
            "the board lists the bot online, in a mission", f"{snap['online']}")
        chk(R, any(x["name"] == b.name for x in snap["levels"]), "the board ranks the bot by level")
        q = next((x for x in snap["missions"] if x["quest"] == qid), None)
        chk(R, q and any(x["party"] == [b.name] for x in q["best"]), "the mission record names the party", f"{q}")
        ev = next((e for e in snap["events"] if e["key"] == "tp"), None)
        chk(R, ev and ev["running"] and ev["results"][0]["best"] and ev["results"][0]["best"][0]["party"] == [b.name],
            "the event's featured mission ranks only clears made while it runs", f"{ev}")
        await leave_mission(b, R, mm, f"board {qid}")
    finally:
        os.remove(events_file)
    b.close()


# Where the level block (char +0xA4: u16 level, u32 EXP +0xC, u32 play time +0x18) sits in the bot's bodies (packet
# offset - 8): 0x0D01 char at +0x24, 0x0D03 slot 0 char at +0x30, 0x010D record char at +0x60 (record +0x34 = the
# enemy the EXP came from, body +0x5C), 0x0115 level block at +0x30 (enemy at +0x2C).
LV_0D01, LV_0D03, LV_010D, LV_0115 = 0x24 + 0xA4, 0x30 + 0xA4, 0x60 + 0xA4, 0x30


def level_at(body, off):
    """(level, EXP, play time) of a level block at body + off."""
    return struct.unpack_from("<H", body, off)[0], *struct.unpack_from("<I", body, off + 0xC), \
        *struct.unpack_from("<I", body, off + 0x18)


async def sc_levelup(srv, game, R, opt):
    """Level-up and play time: a character 1 EXP short of Lv2 kills an enemy; the client (0x010D apply PSU 0x144ad0,
    AotI 0x1416c0) plays ef_pl_levelup + the jingle and opens the stat-gain window only when that 0x010D raises its EXP
    and level over what it holds and carries an enemy id (not -1), so no 0x0115 with the new EXP may come first.
    Play time (u32 seconds at level block +0x18) runs while the character is selected, reaches the 0x010D, is saved on
    logout and shows in the 0x0D03 slot and the 0x0D01 on the next login."""
    from psu import games, progression
    games.set_current(game)
    played = 5 * 3600 + 7
    need = progression.exp_for_level(RAW_0D02, 2)

    def seed(st):
        st["exp"], st["playtime"] = need - 1, played

    b = Bot(srv, f"TpLvl{game}", game, opt.verbose)
    qid, mm = pick_mission(game)
    await enter(b, R, seed)
    d01 = b.got(0x0D01)
    chk(R, d01 and level_at(d01[0], LV_0D01)[2] >= played, f"0x0D01 carries the stored play time {played}",
        f"{level_at(d01[0], LV_0D01) if d01 else None}")
    st = srv.char_state(b.gid)
    wpn = next(x for x in st["inv"] if item_db(game)[x["id"]]["cat"] == 1)
    b.send(0x0105, u32(b.gid) + u32(b.lid) + bytes([inv_slot(st, wpn["id"]), 1, 0, 0]) + u32(wpn["uuid"]) + bytes(4))
    await asyncio.sleep(2.2)                            # some play time to count
    if not await to_counter(b, R, 0):
        return b.close()
    await accept(b, R, qid)
    r = await mission_run(b, R, qid, mm, Hits(), wpn["uuid"], f"levelup {qid}")
    if r is None:
        return b.close()
    _, enemies = r
    # replay what the client holds: 0x0D01, then each 0x0115 / own 0x010D overwrites level, EXP and play time
    have, up, before = None, None, None
    for cmd, body in b.rx:
        if cmd == 0x0D01:
            have = level_at(body, LV_0D01)
        elif cmd == 0x0115 and struct.unpack_from("<I", body, 0x18)[0] == b.gid:
            have = level_at(body, LV_0115)
        elif cmd == 0x010D and struct.unpack_from("<I", body, 0x54)[0] == b.gid:
            now_ = level_at(body, LV_010D)
            if up is None and have is not None and now_[0] >= 2:
                up, before = body, have
            have = now_
    chk(R, up is not None, f"{game}: the kill that reaches Lv2 sends a 0x010D at Lv2", f"client ended at {have}")
    if up is None:
        return b.close()
    lv = level_at(up, LV_010D)
    src, = struct.unpack_from("<I", up, 0x5C)
    chk(R, src != 0xFFFFFFFF and src in {e["id"] for e in enemies},
        f"{game}: level-up 0x010D names the enemy the EXP came from (+0x34 {src:#x}, -1 = no effect)")
    chk(R, before[0] < lv[0] and before[1] < lv[1],
        f"{game}: level-up 0x010D raises level and EXP over what the client held (Lv{before[0]} {before[1]} EXP -> "
        f"Lv{lv[0]} {lv[1]} EXP; a 0x0115 with the new EXP first leaves nothing to show)")
    chk(R, lv[2] >= played + 2, f"{game}: play time advances while playing ({played} -> {lv[2]} s in the 0x010D)")
    b.close()
    for _ in range(50):
        await asyncio.sleep(0.1)
        saved = srv.char_state(b.gid).get("playtime", 0)
        if saved >= lv[2]:
            break
    chk(R, saved >= lv[2], f"{game}: play time saved on logout ({saved} s)")
    b2 = Bot(srv, f"TpLvl{game}", game, opt.verbose)
    await b2.login()
    await b2.connect()
    await b2.wait(0x0202, since=0)
    b2.send(0x080E, bytes([0, 1, 1 if game == "aoti" else 0, 1 if game == "aoti" else 0]) + bytes(20))
    b2.send(0x020D, u32(b2.gid) + b2.key + bytes(8), head=b"\xff\xff" + bytes(0x22))
    await b2.wait(0x0D05)
    m = b2.mark()
    b2.send(0x0D06)
    lst = await b2.wait(0x0D03, since=m)
    slot = level_at(lst, LV_0D03) if lst else None
    chk(R, slot and slot[0] == have[0] and slot[2] >= lv[2], f"{game}: 0x0D03 slot shows Lv{have[0]} and the play time",
        f"{slot}")
    m = b2.mark()
    b2.send(0x020B, u32(0) + bytes(8))
    d01 = await b2.wait(0x0D01, since=m, timeout=10)
    chk(R, d01 and level_at(d01, LV_0D01)[2] >= lv[2], f"{game}: 0x0D01 on the next select carries it",
        f"{level_at(d01, LV_0D01) if d01 else None}")
    b2.close()


async def sc_party(srv, game, R, opt):
    """Invite + accept, then a party mission: the member gets the accept at the counter and follows the leader."""
    a = Bot(srv, f"TpPtyA{game}", game, opt.verbose)
    c = Bot(srv, f"TpPtyB{game}", game, opt.verbose)
    await enter(a, R)
    await enter(c, R)
    chk(R, a.got(0x0201) and (a.got(0x0233) or c.got(0x0233)), "both in the lobby see each other (0x0233 / spawn)",
        "no 0x0233")
    m = a.mark()
    mc = c.mark()
    a.send(0x100A, u32(c.gid) + a.name.encode("utf-16-le").ljust(0x40, b"\0") + bytes(8))
    inv = await c.wait(0x100A, since=mc)
    if not chk(R, inv, "invite (0x100A) reaches the invitee"):
        a.close(), c.close()
        return
    c.send(0x100C, inv[0x24:0x2C] + inv[0x2C:0x34] + bytes([1, 0, 0, 0]) + inv[0x34:0x74])
    chk(R, await a.wait(0x100C, since=m), "accept (0x100C 1) -> inviter gets 0x100C")
    chk(R, await a.wait(0x1004, since=m), "inviter gets 0x1004 new member")
    p5 = await c.wait(0x1005, since=mc)
    chk(R, p5, "invitee gets 0x1005 party")
    qid, mm = pick_mission(game)
    for bot in (a, c):
        st = srv.char_state(bot.gid)
        wpn = next(x for x in st["inv"] if item_db(game)[x["id"]]["cat"] == 1)
        bot.wpn = wpn["uuid"]
    await to_counter(a, R, 0)
    await to_counter(c, R, 0)
    mc = c.mark()
    await accept(a, R, qid)
    r = await c.wait(0x0C02, since=mc, timeout=4)
    chk(R, r and r[0x24] == 0 and c.got(0x1015, mc), "member at the counter gets the party accept (0x1015, 0x0C02 0)",
        hexs(r, 0x28))
    H = Hits()
    m0 = await depart(a, R, qid, what="leader departs -> mission zone")
    mc = c.mark()
    m1 = await depart(c, R, qid, what="member follows (0x0807 ffff) -> same mission zone")
    if m0 is None or m1 is None:
        a.close(), c.close()
        return
    await asyncio.sleep(0.5)
    chk(R, c.got(0x0233, mc) or c.got(0x0201, mc, lambda x: struct.unpack_from("<I", x, 0x08)[0] == a.gid),
        "member sees the leader in the mission zone (0x0233)", "no 0x0233")
    chk(R, a.lid != c.lid, f"different LIDs in the instance ({a.lid}, {c.lid})")
    zone = str(mm["enter"][0])
    eid = mm["gateways"][zone][0]["enemies"][0]["id"]
    mc = c.mark()
    k = a.mark()
    a.send(0x0E00, H.body(a, [hit((0.0, 0.0, 0.0), a.lid, eid, a.wpn)]))
    chk(R, await a.wait(0x0E07, since=k), "leader's hit -> 0x0E07")
    chk(R, await c.wait(0x0E07, since=mc), "member sees the leader's hit result (0x0E07 relayed)")
    mc = c.mark()
    a.send(0x0304, bytes.fromhex("00001200") + u32(a.gid) + bytes(8) + bytes([0, 0, 0, 0, 0, 1, 0, 0])
           + "party hi\0".encode("utf-16-le"))
    chk(R, await c.wait(0x0304, since=mc), "party chat reaches the member")
    a.close(), c.close()


async def sc_technique(srv, game, R, opt):
    """Learn Foie / Resta / Shifta from disks, link them to a Staff, then: Foie on an enemy, Resta and Shifta on self."""
    b = Bot(srv, f"TpTech{game}", game, opt.verbose)
    staff = "01190100"
    disks = [find_item(game, n, 6) for n in ("Foie", "Resta", "Shifta")]
    sword, dagger, renkai = "01010000", "010a0000", "04070400"
    await enter(b, R, seed=lambda st: [add(st, k) for k in (sword, dagger, renkai, staff)]
                + [add(st, d_) for d_ in disks + disks[:1]])
    st = srv.char_state(b.gid)
    npas = len(st.get("pas", []))
    # A client whose pack has drifted from the saved one: its Renkai Buyou-zan disk at index 7 with UUID
    # 0x40000006, which is the Sword in the saved pack. The 0x3C request as the PSU client sends it (its +0x20
    # words, no padding after the UUID), naming the saved pack's Sword
    si = inv_slot(st, sword)
    m = b.mark()
    b.send(0x0105, u32(b.gid) + u32(b.lid) + bytes([si, 7, 0x24, 0]) + u32(st["inv"][si]["uuid"]),
           head=bytes(0x18) + bytes.fromhex("20ec3300" "00000000" "25000000"))
    r = await b.wait(0x0105, since=m)
    chk(R, r and r[0x2D] == 7 and r[0x2C] == si and r[0x2E] == npas and r[0x2F] == 4
        and r[0x30:0x34] == u32(st["inv"][si]["uuid"]) and r[0x68:0x6C].hex() == renkai,
        f"real client's learn (index {si} / UUID of the saved Sword) -> the held disk Renkai Buyou-zan learned at "
        f"{npas}, the client's index and UUID echoed", hexs(r, 0x6C))
    await b.settle(0.2)
    st = srv.char_state(b.gid)
    chk(R, [p[1][:6] for p in st.get("pas", [])][npas:] == [renkai[:6]] and inv_slot(st, renkai) is None
        and inv_slot(st, sword) is not None, "saved: Renkai Buyou-zan learned and gone, the Sword kept",
        f"pas {st.get('pas')} inv {[x['id'] for x in st['inv']]}")
    npas += 1
    for t, d_ in enumerate(disks + disks[:1]):
        st = srv.char_state(b.gid)
        i = inv_slot(st, d_)
        m = b.mark()
        # the item menu's "learn" (0x15b780): event 7, +0x2E 0x24 = append, +0x2F 0
        b.send(0x0105, u32(b.gid) + u32(b.lid) + bytes([i, 7, 0x24, 0]) + u32(st["inv"][i]["uuid"]) + bytes(4))
        r = await b.wait(0x0105, since=m)
        if t < len(disks):                 # the client drops it unless +0x2E < 36 and +0x2F is the disk category
            chk(R, r and r[0x2D] == 7 and r[0x2E] == npas + t and r[0x2F] == 6 and len(r) >= 0x6C
                and r[0x68:0x6C].hex() == d_[:6] + "00",
                f"learn disk {d_} (0x0105 7) -> 0x0105 7 at list position {npas + t}, PA id at +0x68", hexs(r, 0x6C))
        else:
            chk(R, r and r[0x2E] == 0x24, f"learn {d_} again -> refused (+0x2E stays 0x24)", hexs(r, 0x34))
    await b.settle(0.3)
    st = srv.char_state(b.gid)
    learned = [p[1][:6] for p in st.get("pas", [])]
    chk(R, learned[npas:] == [d_[:6] for d_ in disks], f"disks learned: saved PA list has Foie/Resta/Shifta once",
        f"saved PA list {learned}")
    chk(R, sum(x["id"] in disks for x in st["inv"]) == 1, "learned disks left the inventory (the refused one stays)",
        str([x["id"] for x in st["inv"]]))
    w = next(x for x in st["inv"] if x["id"] == staff)
    wi = inv_slot(st, staff)
    for t, d_ in enumerate(disks):
        m = b.mark()
        b.send(0x0105, u32(b.gid) + u32(b.lid) + bytes([wi, 3, npas + t, t]) + u32(w["uuid"]) + bytes(4))
        await b.wait(0x0105, since=m)
    b.send(0x0105, u32(b.gid) + u32(b.lid) + bytes([wi, 1, 0, 0]) + u32(w["uuid"]) + bytes(4))
    await b.settle(0.3)
    st = srv.char_state(b.gid)
    w = next(x for x in st["inv"] if x["id"] == staff)
    techs = w.get("techs") or []
    linked = chk(R, len(techs) >= 3 and all(c_ for c_, _ in techs[:3]),
                 "link the three techniques to the Staff (0x0105 3) -> saved tech slots", f"techs {techs}")
    if not linked:                       # fixture so the hit checks still run
        from psu import growth, games
        games.set_current(game)
        st.setdefault("pas", [])
        for d_ in disks:
            if d_[:6] not in [p[1][:6] for p in st["pas"]]:
                st["pas"].append([0, d_[:6] + "00"])
        w["techs"] = [[growth.tech_code(d_[:6] + "00"), 0] for d_ in disks] + [[0, 0]]
        b.close()
        await asyncio.sleep(0.5)
        srv.save_state(b.gid, st)
        b = Bot(srv, f"TpTech{game}", game, opt.verbose)
        await enter(b, R, create=False)
        b.send(0x0105, u32(b.gid) + u32(b.lid) + bytes([wi, 1, 0, 0]) + u32(w["uuid"]) + bytes(4))
        chk(R, True, "(fixture: PAs + tech slots written to the saved state, reloaded)")
    qid, mm = pick_mission(game)
    if not await to_counter(b, R, 0):
        return b.close()
    await accept(b, R, qid)
    m0 = await depart(b, R, qid)
    if m0 is None:
        return b.close()
    zone = str(mm["enter"][0])
    eid = mm["gateways"][zone][0]["enemies"][0]["id"]
    H = Hits()
    b.send(0x0402, u32(eid - 150) + struct.pack("<hhhh", -428, 0, 481, 0x2000) + bytes.fromhex("02002000ffffffffffffffff"))
    await b.settle(0.3)
    k = b.mark()
    misses = 0
    for _ in range(6):                   # the server rolls accuracy for technique hits too
        k = b.mark()
        b.send(0x0E00, H.body(b, [hit((0.0, 0.0, 0.0), b.lid, eid, w["uuid"] | (0 << 29))]))
        r = await b.wait(0x0E07, since=k)
        e = e07_entries(r)[0] if r else None
        if not e or e["dmg"] > 0 or not e["t_se"] & 0x10:
            break
        misses += 1
    chk(R, e and e["dmg"] > 0, f"Foie (tech slot 0) on an enemy -> 0x0E07 damage ({misses} misses first)", str(e))
    chk(R, await b.wait(0x0108, since=k, timeout=2), "Foie hit grows its PA EXP (0x0108)")
    st = srv.char_state(b.gid)
    # take damage from the enemy, then Resta / Shifta on self
    hp0 = st.get("hp")
    k = b.mark()
    for _ in range(30):                  # enemy blocks (no sure-hit 0x4000) roll accuracy: repeat until 3 land
        k1 = b.mark()
        b.send(0x0E00, H.body(b, [hit((0.0, 0.0, 0.0), eid, b.lid, 0, D=ENEMY_D)]))
        await b.wait(0x0E07, since=k1, pred=lambda x: any(y["dst"] == b.lid for y in e07_entries(x)), timeout=2)
        if sum(x["dmg"] > 0 for r_ in b.got(0x0E07, k) for x in e07_entries(r_) if x["dst"] == b.lid) >= 3:
            break
    await b.settle(0.3)
    hurt = [x for r_ in b.got(0x0E07, k) for x in e07_entries(r_) if x["dst"] == b.lid]
    hp1 = hurt[-1]["t_hp"] if hurt else None
    chk(R, hurt and hp1 < (hp0 or 1 << 30), f"enemy hits on the player -> 0x0E07 player HP {hp0} -> {hp1}", str(hurt[-1:]))
    k = b.mark()
    b.send(0x0E00, H.body(b, [hit((0.0, 0.0, 0.0), b.lid, b.lid, w["uuid"] | (1 << 29))]))
    r = await b.wait(0x0E07, since=k)
    e = e07_entries(r)[0] if r else None
    chk(R, e and e["t_se"] & 0x80000 and e["t_hp"] > (hp1 or 0), f"Resta (slot 1) on self -> 0x0E07 heal (HP {hp1} -> "
        f"{e and e['t_hp']})", str(e))
    k = b.mark()
    b.send(0x0E00, H.body(b, [hit((0.0, 0.0, 0.0), b.lid, b.lid, w["uuid"] | (2 << 29))]))
    r = await b.wait(0x0E07, since=k)
    e = e07_entries(r)[0] if r else None
    chk(R, e and e["t_se"] & 0x1000000 and e["t_se2"] & 0x80000, "Shifta (slot 2) on self -> 0x0E07 buff (SE1 0x1000000, "
        "SE2 0x80000)", str(e))
    b.close()


def pp_events(bot, since, uuid=None):
    """(uuid, flags, amount) of each S>C 0x0105 event 0x0A (PP) since `since`."""
    out = []
    for x in bot.got(0x0105, since, lambda x: len(x) >= 0x38 and x[0x2D] == 0x0A):
        u, a = struct.unpack_from("<Ii", x, 0x30)
        if uuid is None or u == uuid:
            out.append((u, x[0x2F], a))
    return out


def cast(bot, cmd, action, word=0, extra=b""):
    """A channel-1 cast as the client sends it: +0x34 action (body +0x2C), +0x38 u32 word, then the rest."""
    bot.send(cmd, bytes(8) + bytes([action, 0, 0, 0]) + u32(word) + extra, channel=1)


async def sc_pp(srv, game, R, opt):
    """Weapon PP: a striking PA (0x0102 3), a left-hand shot (0x0104 mode + 0x010F 1) and their PP spent (S>C 0x0105
    event 0x0A, flag 8 = set), a plain swing spends none, regeneration in a mission, Photon Charge, the PP saved
    and sent back in 0x0A0A after a relog."""
    from psu import games, items
    b = Bot(srv, f"TpPP{game}", game, opt.verbose)
    charge = find_item(game, "Photon Charge", 3)
    await enter(b, R, seed=lambda st: [add(st, charge, 2)])
    st = srv.char_state(b.gid)
    db = item_db(game)
    saber = next(x for x in st["inv"] if x["id"].startswith("0109"))
    gun = next(x for x in st["inv"] if x["id"].startswith("0114"))
    ri = next(i for i, p_ in enumerate(st.get("pas", [])) if p_[1].startswith("040906"))
    for x, ev, a in ((saber, 1, 0), (gun, 1, 0), (saber, 3, ri)):
        i = inv_slot(st, x["id"])
        m = b.mark()
        b.send(0x0105, u32(b.gid) + u32(b.lid) + bytes([i, ev, a, 0]) + u32(x["uuid"]) + bytes(4))
        await b.wait(0x0105, since=m)
    await b.settle(0.3)
    games.set_current(game)
    pa = items.paparam()["pa"]["04090600"]
    rs = bytes.fromhex(pa["base"][0])
    mx_s = struct.unpack_from("<H", bytes.fromhex(db[saber["id"]]["consts"]), 0)[0]
    mx_g = struct.unpack_from("<H", bytes.fromhex(db[gun["id"]]["consts"]), 0)[0]
    cost_rs = mx_s * rs[4] // 100 if rs[3] & 1 else rs[4]
    bd = bytes.fromhex(items.paparam()["bullet_default"][0x14 - 0x0E])[4:]
    cost_shot = mx_g * bd[4] // 100 if bd[3] & 1 else bd[4]
    k = b.mark()                                    # in the lobby: no regeneration ticks in between
    cast(b, 0x0102, 2, 0x11)                        # a plain swing (kind 2 on a striking weapon): no PP
    cast(b, 0x0102, 3, 0x23)                        # Rising Strike, first step ((0x23 >> 4) - 2 = 0)
    await b.settle(0.5)
    ev = pp_events(b, k, saber["uuid"])
    chk(R, len(ev) == 1 and ev[0][1] == 8 and ev[0][2] == mx_s - cost_rs,
        f"striking PA (0x0102 3 0x23) -> 0x0105 0x0A set saber PP {mx_s} - {cost_rs}, plain swing (0x0102 2) none",
        str(ev))
    k = b.mark()
    cast(b, 0x0104, 2)                              # left-hand mode, then a shot (0x010F 1)
    cast(b, 0x010F, 1, 0, bytes(4))
    cast(b, 0x010F, 1, 0, bytes(4))
    cast(b, 0x0104, 0)                              # mode off: the next PA is the saber's again
    await b.settle(0.5)
    ev = pp_events(b, k, gun["uuid"])
    chk(R, [a for _, _, a in ev] == [mx_g - cost_shot, mx_g - 2 * cost_shot],
        f"two left-hand shots (0x0104 2, 0x010F 1) -> handgun PP {mx_g} - {cost_shot} each", str(ev))
    st = srv.char_state(b.gid)
    sv = {x["uuid"]: x.get("pp") for x in st["inv"]}
    chk(R, sv.get(saber["uuid"]) == mx_s - cost_rs and sv.get(gun["uuid"]) == mx_g - 2 * cost_shot,
        "spent PP saved in the character state", str(sv))
    k = b.mark()
    await b.settle(combat_tick() + 0.5)
    chk(R, not pp_events(b, k), "no regeneration outside a mission (the room skips room types 0 / 2)",
        str(pp_events(b, k)))
    qid, mm = pick_mission(game)
    if not await to_counter(b, R, 0):
        return b.close()
    await accept(b, R, qid)
    if await depart(b, R, qid) is None:
        return b.close()
    cast(b, 0x0102, 8, 0)                           # moving in the mission zone: the client is in
    k = b.mark()
    r = await b.wait(0x0105, since=k, pred=lambda x: x[0x2D] == 0x0A, timeout=combat_tick() + 2)
    await b.settle(0.3)
    ev = pp_events(b, k)
    up = {u: a for u, _, a in ev}
    chk(R, r and up.get(saber["uuid"], 0) > mx_s - cost_rs and up.get(gun["uuid"], 0) > mx_g - 2 * cost_shot,
        "regeneration tick in the mission -> 0x0105 0x0A PP up on both equipped weapons", str(ev))
    st = srv.char_state(b.gid)
    ci = inv_slot(st, charge)
    si = inv_slot(st, saber["id"])
    cu = st["inv"][ci]["uuid"]
    k = b.mark()
    b.send(0x0105, u32(b.gid) + u32(b.lid) + bytes([ci, 8, 0, si]) + u32(cu) + bytes(4))   # +0x37 the weapon's index
    echo = await b.wait(0x0105, since=k, pred=lambda x: x[0x2D] == 8)
    await b.settle(0.3)
    seq = [(x[0x2D], struct.unpack_from("<Ii", x, 0x30) if len(x) >= 0x38 else None) for x in b.got(0x0105, k)]
    full = [i for i, (e_, ua) in enumerate(seq) if e_ == 0x0A and ua == (saber["uuid"], mx_s)]
    used = [i for i, (e_, _) in enumerate(seq) if e_ == 8]
    chk(R, echo and full and used and full[0] < used[0],
        f"Photon Charge on the saber (0x0105 8, A = {si}) -> 0x0105 0x0A full PP {mx_s}, then the use echo", str(seq))
    cast(b, 0x0102, 3, 0x23)
    await b.settle(0.5)
    b.close()
    await asyncio.sleep(0.5)
    st = srv.char_state(b.gid)
    want = {x["uuid"]: items.cur_pp(x) for x in st["inv"] if db[x["id"]]["cat"] == 1}
    chk(R, want.get(saber["uuid"], mx_s) < mx_s, "the PA after the charge spent PP again (saved)", str(want))
    b = Bot(srv, f"TpPP{game}", game, opt.verbose)
    await enter(b, R, create=False)
    a0a = b.got(0x0A0A)
    got = {}
    if a0a:
        body = a0a[-1]
        n = body[0x24]
        for i in range(n):
            rec = body[0x170 - 8 + 0x24 * i:0x170 - 8 + 0x24 * (i + 1)]
            u = struct.unpack_from("<I", rec, 4)[0]
            if u in want:
                got[u] = struct.unpack_from("<HH", rec, 0x10)
    chk(R, got and all(got[u][0] == want[u] for u in want) and got[saber["uuid"]][1] == mx_s,
        "relog: 0x0A0A weapon records carry the saved PP (+0x10) and max PP (+0x12)", f"want {want} got {got}")
    b.close()


def combat_tick():
    from psu import combat
    return combat.PP_TICK


async def sc_story(srv, game, R, opt):
    """AotI: Clyez HQ counter 115 (lobby map 5): table 0115, accept the first act, depart."""
    if game != "aoti":
        chk(R, True, "story counter 115 is AotI's (Episode 2): not on the PSU disc")
        return "skip"
    b = Bot(srv, f"TpStory{game}", game, opt.verbose)
    await enter(b, R)
    await warp(b, R, 1100000, 0, 5, 2, what="Clyez HQ map (1100000/0/5) -> 0x0236")
    if not await to_counter(b, R, 115):
        return b.close()
    from psu import games, missions
    games.set_current("aoti")
    qs = [q for q in missions.data("aoti")["counters"]["115"]["quests"] if missions.mission(q, "aoti")]
    qid = qs[0]
    await accept(b, R, qid)
    m0 = await depart(b, R, qid)
    if m0 is not None:
        chk(R, b.got(0x1202, m0) or await b.wait(0x1202, since=m0, timeout=3), f"story {qid}: join snapshot 0x1202")
    b.close()


EP3_FIRST = 23                      # staged story chapters 23..30 = Ep3 Ch1..8, 41 / 42 side stories
EP3_HQ = (1100000, 0, 5)            # the Clyez lobby's GUARDIANS HQ map (counter 115)


def flag_req(name, value):
    """C>S 0x0D04 for work.on_chrflag_set (value) / on_chrflag_get (value 0): 16-byte name, type bytes, value."""
    return name.encode().ljust(12, b"\0") + u32(0) + bytes([1, 2, 0, 0]) + u32(0) + struct.pack("<i", value)


async def chrflag(b, name, value):
    k = b.mark()
    b.send(0x0D04, flag_req(name, value))
    r = await b.wait(0x0D04, since=k)
    return struct.unpack_from("<i", r, 0x3C)[0] if r else None


def ep3_acts():
    """The staged story acts: {(chapter, act): Difficulty C quest id} for chapters 23.. and the side stories."""
    from psu import missions
    acts = {}
    for q, m in missions.data("aoti")["missions"].items():
        st = m.get("story")
        if st and st["chapter"] >= EP3_FIRST:
            acts.setdefault((st["chapter"], st["act"]), []).append(int(q))
    return {k: min(v) for k, v in sorted(acts.items())}


def rule_flags(tokens):
    """[(zone, map, flag)] of the 120 tokens of a quest rule."""
    out, i = [], 0
    while tokens and i < len(tokens):
        if tokens[i] == 120 and i + 3 < len(tokens):
            out.append(tuple(tokens[i + 1:i + 4]))
            i += 4
        elif tokens[i] == 100:
            i += 2
        else:
            i += 1
    return out


async def counter_options(b, R, counter, came_from=(0, 4, 6)):
    """Enter counter `counter` (0x0811 from came_from) and fetch its table: -> {quest id: option byte} or None."""
    from psu import missions
    k = b.mark()
    b.send(0x0811, struct.pack("<BBHHHI", 1, 0, *came_from, counter) + bytes(12))
    ok = await b.wait(0x0236, since=k, timeout=10)
    chk(R, ok, f"enter counter {counter} (0x0811) -> counter load 0x0236", f"got {[hex(c) for c, _ in b.rx[k:]]}")
    if not ok:
        return None
    b.post_load()
    m = b.mark()
    b.send(0x0C05, u32(counter))
    t = await b.wait(0x0C06, since=m, timeout=15)
    m = b.mark()
    b.send(0x0C0F, u32(counter))
    o = await b.wait(0x0C10, since=m)
    table = missions.data("aoti")["counters"].get(str(counter))
    chk(R, t and o and table, f"counter {counter}: 0x0C06 table + 0x0C10 options", hexs(o, 0x30))
    if not (t and o and table):
        return None
    n = struct.unpack_from("<h", o, 0x26)[0]
    opts = o[0x28:0x28 + n]
    chk(R, n == table["groups"] + len(table["quests"]), f"counter {counter}: {table['groups']} groups + "
        f"{len(table['quests'])} quests in 0x0C10 ({n} options)")
    return dict(zip(table["quests"], opts[table["groups"]:]))


async def ep3_walk(b, R, srv, qid, mm, w, label):
    """Depart into a story act and walk it as its scripts would: every zone and map in order (0x0807 change_unit),
    the trial start flag where the start rule names it, every enemy of each zone from the trial start on, the clear
    flags (or the last wave) -> 0x0C09 results, then the rest of the act's zones. True when it cleared."""
    m0 = await depart(b, R, qid, what=f"{label}: depart -> zone {mm['enter'][0]} map {mm['enter'][1]}")
    if m0 is None:
        return False
    q = mm["quest"]
    starts, clears = rule_flags(q.get("start")), rule_flags(q.get("clear"))
    started = not starts
    cleared, kmark, H = False, None, Hits()
    for z in sorted(int(x) for x in mm["zones"]):
        maps = mm["zones"][str(z)]
        for mp in maps:
            if b.area != (qid, z, mp):
                k = await warp(b, R, qid, z, mp, 0, what=f"{label}: change_unit {z}/{mp} -> 0x0236", mission=True)
                if k is None:
                    return False
            for sz, smp, f in starts:
                if (sz, smp) == (z, mp) and not started:
                    k = b.mark()
                    b.send(0x0404, u32(maps.index(mp) * 256 + f) + u32(0))
                    started = await b.wait(0x0C09, since=k, pred=lambda x: x[0x24] == 0, timeout=4) is not None
                    chk(R, started, f"{label}: trial start flag {z}/{mp}/{f} (0x0404) -> 0x0C09 start")
        if cleared:
            continue
        zmark = b.mark()                                # drops lie in this zone's instance
        if started and mm["gateways"].get(str(z)):
            m, _ = await kill_all(b, R, dict(mm, enter=[z, maps[0], 0]), H, w, f"{label} zone {z}", max_rounds=900)
            kmark = m if kmark is None else kmark
        if started and any(cz == z for cz, _, _ in clears):
            if b.area[2] != next(cm for cz, cm, _ in clears if cz == z):
                cm = next(cm for cz, cm, _ in clears if cz == z)
                await warp(b, R, qid, z, cm, 0, what=f"{label}: to the clear map {z}/{cm}", mission=True)
            cleared = await finish_mission(b, R, qid, dict(mm, zones={str(z): maps}),
                                           kmark if kmark is not None else m0, f"{label} trial", drop_mark=zmark)
    chk(R, cleared, f"{label}: trial cleared")
    st = srv.char_state(b.gid)
    chk(R, str(qid) in st.get("story", {}), f"{label}: clear recorded in the character's story progress",
        str(st.get("story")))
    return cleared


async def ep3_leave(b, R, to, label, mission_end=True):
    """0x0807 out of the act (the scripts' syscall 270): -> 0x1006 mission end, lands at `to` (quest, zone, map)."""
    k = b.mark()
    b.send(0x0807, struct.pack("<IHHHHI", 200000, 2, 5, 2, 11, NO_PARTY))
    done = await b.wait(0x0236, since=k, timeout=12)
    chk(R, done and (not mission_end or b.got(0x1006, k, lambda x: x[0x24] == 10)),
        f"{label}: syscall 270 -> 0x1006 mission end + load", f"got {[hex(c) for c, _ in b.rx[k:]]}")
    if not done:
        return False
    q0, zi = b.got(0x0C00, k), b.got(0x0205, k)
    at = ((struct.unpack_from("<I", q0[-1], 0x24)[0] if q0 else None),) + \
        (struct.unpack_from("<II", zi[-1], 0x28) if zi else (None, None))
    chk(R, at == tuple(to), f"{label}: landed at {to[0]}/{to[1]}/{to[2]} ({at})")
    b.area = at
    b.post_load()
    await b.settle(0.3)
    return at == tuple(to)


async def sc_ep3(srv, game, R, opt):
    """AotI Episode 3 acts staged in $PSU_NBL_EXTRA: every chapter in order on one character. Counter 115
    offers Ch1 Act 1 and Innocent Girl only; each act is walked (zones, trial start, every wave, boss / clear flags,
    results), Act 1 lands in its chapter base, the base's act 2 counter opens table 01xx with Act 2, Act 2 ends back
    in the base, its way back leads to the HQ, where the next chapter is now open; The Magashi Plan after Ch5 Act 2.
    Story flags: kept and read back (0x0D04), the subplot gates answered by the server, per-run choices forgotten on
    accept."""
    if game != "aoti":
        chk(R, True, "Episode 3 is AotI's (staged story acts): not on the PSU disc")
        return "skip"
    from psu import games, missions
    games.set_current("aoti")
    acts = ep3_acts()
    if (EP3_FIRST, "A") not in acts:
        chk(R, True, "no Episode 3 acts staged in $PSU_NBL_EXTRA (online-only content, not on the disc)")
        return "skip"
    chapters = sorted({c for c, _ in acts if c < 40})
    sides = sorted({c for c, _ in acts if c > 40})
    chk(R, True, f"staged: chapters {chapters}, side stories {sides}, {len(acts)} acts")
    b = Bot(srv, f"TpEp3{game}", game, opt.verbose)
    await enter(b, R)
    st = srv.char_state(b.gid)
    wpn = next(x for x in st["inv"] if item_db(game)[x["id"]]["cat"] == 1)
    b.send(0x0105, u32(b.gid) + u32(b.lid) + bytes([inv_slot(st, wpn["id"]), 1, 0, 0]) + u32(wpn["uuid"]) + bytes(4))
    await warp(b, R, *EP3_HQ, 2, what="Clyez HQ map (1100000/0/5) -> 0x0236")

    async def hq_counter():
        return await counter_options(b, R, 115, came_from=(0, 5, 2))

    opts = await hq_counter()
    if opts is None:
        return b.close()
    first = acts[(EP3_FIRST, "A")]
    chk(R, opts.get(first) == 3, f"fresh character: Ch1 Act 1 ({first}) selectable", str(opts.get(first)))
    if (EP3_FIRST + 1, "A") in acts:
        nxt = acts[(EP3_FIRST + 1, "A")]
        chk(R, opts.get(nxt) == 2, f"fresh character: Ch2 Act 1 ({nxt}) greyed", str(opts.get(nxt)))
        r = await accept(b, R, nxt, expect_ok=False)
        chk(R, r and r[0x24] == 4, f"accept {nxt} before Ch1 is cleared -> 0x0C02 4 (prerequisites)",
            hexs(r[0x24:0x28] if r else None))
    for c in sides:
        open_ = c not in missions.SIDE_AFTER
        chk(R, opts.get(acts[(c, "A")]) == (3 if open_ else 2),
            f"fresh character: side story {acts[(c, 'A')]} {'open' if open_ else 'greyed'}", str(opts.get(acts[(c, 'A')])))
    chk(R, await chrflag(b, "S13_FLAG_20", 0) == 0, "Ch1 skip prompt: S13_FLAG_20 not set yet (0x0D04 get -> 0)")

    async def play(qid, label, back_to):
        mm = missions.mission(qid, "aoti")
        await accept(b, R, qid)
        ok = await ep3_walk(b, R, srv, qid, mm, wpn["uuid"], label)
        if ok:
            ok = await ep3_leave(b, R, back_to, label)
        return ok

    async def side(c):
        qid = acts[(c, "A")]
        name = missions.mission(qid, "aoti")["name"]
        if not await play(qid, f"side {c} {name} ({qid})", EP3_HQ):
            return False
        return await hq_counter() is not None

    for c in sides:                                     # the ones open from the start (Innocent Girl)
        if c not in missions.SIDE_AFTER and not await side(c):
            return b.close()
    for ch in chapters:
        a, bq = acts.get((ch, "A")), acts.get((ch, "B"))
        mm = missions.mission(a, "aoti")
        base = mm["story"]["base"]
        bb = missions.base(base)
        label = f"Ch{ch - 22} Act 1 {mm['name']} ({a})"
        if ch == EP3_FIRST:
            chk(R, await chrflag(b, "S13_FLAG_20", 1) == 1, "Ch1: intro seen S13_FLAG_20 = 1 (0x0D04 set acked)")
        if ch == 26 and bq:
            chk(R, await chrflag(b, "S16_FLAG_24", 1) == 1, "Ch4: a previous run's Lou choice S16_FLAG_24 = 1 kept")
        if ch == 28 and bq:
            chk(R, await chrflag(b, "S18_FLAG_27", 1) == 1, "Ch6: a previous run's party choice S18_FLAG_27 = 1 kept")
        if not await play(a, label, (base, *bb["enter"][:2])):
            return b.close()
        if ch == EP3_FIRST:
            chk(R, await chrflag(b, "S13_FLAG_23", 0) == 0, "Ch1: Lou subplot gate S13_FLAG_23 stays off (Ep2 / Ch4 "
                "not cleared)")
            chk(R, await chrflag(b, "S13_FLAG_22", 1) == 1, "Ch1: Lou event seen S13_FLAG_22 = 1 (for Ch4's gate)")
        if ch == 25:
            chk(R, await chrflag(b, "S15_FLAG_24", 1) == 1, "Ch3: subplot talk seen S15_FLAG_24 = 1 (for Ch6's gate)")
        if ch == 26:
            chk(R, await chrflag(b, "S16_FLAG_25", 0) == 1, "Ch4 Act 2 Lou choice gate S16_FLAG_25 open (S13_FLAG_22 + "
                "Ch4 Act 1 cleared at S)")
        if ch == 28:
            chk(R, await chrflag(b, "S18_FLAG_25", 0) == 1, "Ch6 subplot 1 gate S18_FLAG_25 open (S15_FLAG_24 + Ch6 "
                "Act 1 cleared at S)")
            chk(R, await chrflag(b, "S18_FLAG_26", 0) == 0, "Ch6 subplot 2 gate S18_FLAG_26 closed (Ch7 not cleared)")
        if not bq:
            continue
        act2 = next((cn for cn in bb["counters"] if cn[3] is not None), None)
        chk(R, act2 is not None, f"base {base}: an act 2 counter", str(bb["counters"]))
        if act2 is None:
            return b.close()
        opts2 = await counter_options(b, R, act2[2], came_from=tuple(bb["enter"]))
        if opts2 is None:
            return b.close()
        chk(R, opts2.get(bq) == 3, f"base {base} counter {act2[2]}: Act 2 ({bq}) open after Act 1", str(opts2))
        if ch == 26:
            await accept(b, R, bq)
            chk(R, await chrflag(b, "S16_FLAG_24", 0) == 0, "Ch4 Act 2 accepted: the Lou choice S16_FLAG_24 forgotten")
        if ch == 28:
            await accept(b, R, bq)
            chk(R, await chrflag(b, "S18_FLAG_27", 0) == 0, "Ch6 Act 2 accepted: the party choice S18_FLAG_27 "
                "forgotten")
        label = f"Ch{ch - 22} Act 2 {missions.mission(bq, 'aoti')['name']} ({bq})"
        if not await play(bq, label, (base, *bb["enter"][:2])):
            return b.close()
        way = next(cn for cn in bb["counters"] if cn[3] is None)
        k = b.mark()
        b.send(0x0811, struct.pack("<BBHHHI", 1, 0, *bb["enter"], way[2]) + bytes(12))
        done = await b.wait(0x0236, since=k, timeout=10)
        q0 = b.got(0x0C00, k)
        chk(R, done and q0 and struct.unpack_from("<I", q0[-1], 0x24)[0] == EP3_HQ[0],
            f"base {base}: way back (counter {way[2]}) -> the Clyez HQ", f"got {[hex(c) for c, _ in b.rx[k:]]}")
        if not done:
            return b.close()
        b.area = EP3_HQ
        b.post_load()
        await b.settle(0.3)
        opts = await hq_counter()
        if opts is None:
            return b.close()
        if (ch + 1, "A") in acts:
            chk(R, opts.get(acts[(ch + 1, "A")]) == 3, f"after Ch{ch - 22}: Ch{ch - 21} Act 1 open",
                str(opts.get(acts[(ch + 1, "A")])))
        for c, need in missions.SIDE_AFTER.items():
            if (c, "A") in acts:
                want = 3 if missions.story_done(srv.char_state(b.gid), *need) else 2
                chk(R, opts.get(acts[(c, "A")]) == want, f"after Ch{ch - 22}: side story {acts[(c, 'A')]} "
                    f"{'open' if want == 3 else 'greyed'}", str(opts.get(acts[(c, 'A')])))
    for c in sides:                                     # the ones opened by the story (The Magashi Plan)
        if c in missions.SIDE_AFTER and not await side(c):
            return b.close()
    st = srv.char_state(b.gid)
    done = sorted(int(q) for q in st.get("story", {}) if int(q) // 100000 == 2 and int(q) % 1000 >= 100)
    chk(R, len(done) == len(acts), f"every staged act cleared once ({len(done)} / {len(acts)})", str(done))
    chk(R, st.get("chrflags", {}).get("S13_FLAG_20") == 1, "story flags kept in the character state",
        str(st.get("chrflags")))
    b.close()


async def sc_chat(srv, game, R, opt):
    """Lobby chat: the sender gets its echo, a second player in the zone gets the line."""
    a = Bot(srv, f"TpChatA{game}", game, opt.verbose)
    c = Bot(srv, f"TpChatB{game}", game, opt.verbose)
    await enter(a, R)
    await enter(c, R)
    text = "hello from testplay"
    m, mc = a.mark(), c.mark()
    a.send(0x0304, bytes.fromhex("00001200") + u32(a.gid) + bytes(8) + bytes([0, 0, 0, len(text) + 1, 0, 0, 0, 0])
           + (text + "\0").encode("utf-16-le"))
    e = await a.wait(0x0304, since=m)
    o = await c.wait(0x0304, since=mc)
    chk(R, e and text.encode("utf-16-le") in e, "chat (0x0304) -> echo to the sender", hexs(e, 0x60))
    chk(R, o and text.encode("utf-16-le") in o, "chat -> relayed to the other player", hexs(o, 0x60))
    a.close(), c.close()


async def sc_trade(srv, game, R, opt):
    """A PSU player trades a Monomate and meseta to an AotI player (request, accept, offer, confirm, final)."""
    a = Bot(srv, "TpTradePsu", "psu", opt.verbose)
    c = Bot(srv, "TpTradeAoti", "aoti", opt.verbose)
    await enter(a, R, seed=lambda st: st.update(meseta=1000))
    await enter(c, R, seed=lambda st: st.update(meseta=10))
    m, mc = a.mark(), c.mark()
    a.send(0x0304, bytes.fromhex("00001200") + u32(a.gid) + bytes(8) + bytes([0, 0, 0, 6, 0, 0, 0, 0])
           + "trade\0".encode("utf-16-le"))
    chk(R, await c.wait(0x0304, since=mc), "PSU chat reaches the AotI player")

    def t1901(bot, other, action, param=0):
        bot.send(0x1901, u32(other.gid) + bytes([action, param]) + bytes(14))
    mc = c.mark()
    t1901(a, c, 1)
    chk(R, await c.wait(0x1901, since=mc), "trade request (0x1901 1) reaches the AotI player")
    m = a.mark()
    t1901(c, a, 2)
    s = await a.wait(0x1902, since=m)
    chk(R, s and (s[0x2C], s[0x2D]) == (1, 1), "AotI accepts (0x1901 2) -> PSU gets 0x1902 (1, 1)", hexs(s[0x24:0x30] if s else None))
    m = a.mark()
    t1901(a, c, 2)
    s = await a.wait(0x1902, since=m)
    chk(R, s and (s[0x2C], s[0x2D]) == (2, 1), "PSU confirms -> 0x1902 (2, 1) trade window", hexs(s[0x24:0x30] if s else None))
    st = srv.char_state(a.gid)
    mono = next(x for x in st["inv"] if x["id"] == "03010000")
    m, mc = a.mark(), c.mark()
    a.send(0x1903, u32(a.gid) + u32(c.gid) + u32(mono["uuid"]) + u32(1) + bytes([1, 0, 0, 0]) + bytes(8))
    chk(R, await a.wait(0x1904, since=m), "offer a Monomate (0x1903 1) -> echo 0x1904")
    chk(R, await c.wait(0x1904, since=mc), "the AotI partner sees the offer (0x1904)")
    a.send(0x1903, u32(a.gid) + u32(c.gid) + u32(0) + u32(100) + bytes([3, 0, 0, 0]) + bytes(8))
    await a.settle(0.3)
    mc = c.mark()
    t1901(a, c, 4)
    s = await c.wait(0x1902, since=mc)
    chk(R, s and (s[0x2C], s[0x2D]) == (4, 5), "PSU confirms items (0x1901 4) -> partner 0x1902 (4, 5)", hexs(s[0x24:0x30] if s else None))
    m = a.mark()
    t1901(c, a, 4)
    s = await a.wait(0x1902, since=m)
    chk(R, s and (s[0x2C], s[0x2D]) == (4, 1), "both confirmed -> 0x1902 (4, 1)", hexs(s[0x24:0x30] if s else None))
    mc = c.mark()
    t1901(a, c, 6)
    s = await c.wait(0x1902, since=mc)
    chk(R, s and (s[0x2C], s[0x2D]) == (5, 5), "PSU final OK (0x1901 6) -> partner 0x1902 (5, 5)", hexs(s[0x24:0x30] if s else None))
    m, mc = a.mark(), c.mark()
    t1901(c, a, 6)
    s = await a.wait(0x1902, since=m, pred=lambda x: x[0x2C] == 6)
    chk(R, s and s[0x2D] == 1, "both final -> 0x1902 (6, 1) done", hexs(s[0x24:0x30] if s else None))
    chk(R, await c.wait(0x0106, since=mc, pred=lambda x: x[0x2D] == 0x0B), "AotI player gets 0x0106 add (Monomate)")
    chk(R, await a.wait(0x0106, since=m, pred=lambda x: x[0x2D] == 0x0C), "PSU player gets 0x0106 remove")
    await a.settle(0.3)
    sa, sc = srv.char_state(a.gid), srv.char_state(c.gid)
    ma = next((x["qty"] for x in sa["inv"] if x["id"] == "03010000"), 0)
    mcq = next((x["qty"] for x in sc["inv"] if x["id"] == "03010000"), 0)
    chk(R, ma == mono["qty"] - 1 and mcq >= 6, f"saved: PSU Monomate {mono['qty']} -> {ma}, AotI {mcq}")
    chk(R, sa.get("meseta") == 900 and sc.get("meseta") == 110, f"saved meseta PSU {sa.get('meseta')} (900), "
        f"AotI {sc.get('meseta')} (110)")
    a.close(), c.close()


async def sc_xparty(srv, game, R, opt):
    """Mixed party: a PSU leader and an AotI member; the leader's PSU mission is refused to the member (0x0C02 4)."""
    a = Bot(srv, "TpXPtyPsu", "psu", opt.verbose)
    c = Bot(srv, "TpXPtyAoti", "aoti", opt.verbose)
    await enter(a, R)
    await enter(c, R)
    mc = c.mark()
    a.send(0x100A, u32(c.gid) + a.name.encode("utf-16-le").ljust(0x40, b"\0") + bytes(8))
    inv = await c.wait(0x100A, since=mc)
    if not chk(R, inv, "PSU invite reaches the AotI player"):
        a.close(), c.close()
        return
    m = a.mark()
    c.send(0x100C, inv[0x24:0x2C] + inv[0x2C:0x34] + bytes([1, 0, 0, 0]) + inv[0x34:0x74])
    chk(R, await a.wait(0x1004, since=m), "AotI accepts -> PSU leader gets 0x1004")
    await to_counter(a, R, 0)
    await to_counter(c, R, 0)
    qid, mm = pick_mission("psu")
    mc = c.mark()
    await accept(a, R, qid)
    r = await c.wait(0x0C02, since=mc, timeout=4)
    chk(R, r and r[0x24] == 4 and not c.got(0x1015, mc), f"AotI member gets 0x0C02 4 for PSU mission {qid}, no 0x1015",
        hexs(r, 0x28))
    await depart(a, R, qid, what="PSU leader departs -> mission zone")
    k = c.mark()
    c.send(0x0807, struct.pack("<IHHHHI", qid, 0xFFFF, 0xFFFF, 0xFFFF, 7, NO_PARTY))
    await c.settle(1.5)
    chk(R, not c.got(0x0215, k), "AotI member's follow is not a departure (no mission zone, no 0x0215)",
        f"got {[hex(x) for x, _ in c.rx[k:]]}")
    a.close(), c.close()


def say(bot, text):
    bot.send(0x0304, bytes.fromhex("00001200") + u32(bot.gid) + bytes(8) + bytes([0, 0, 0, len(text) + 1, 0, 0, 0, 0])
             + (text + "\0").encode("utf-16-le"))


async def sc_gm(srv, game, R, opt):
    """GM chat commands (admin account): !summon a player from Parum (a zone change run in that player's session,
    fragments acked), !give to them, !mute (their chat is no longer relayed)."""
    from psu import gm
    if "summon" not in (gm.__doc__ or ""):
        chk(R, True, "this server has no !summon (older gm.py)")
        return "skip"
    a = Bot(srv, f"TpGmA{game}", game, opt.verbose)          # PSU_ADMINS lists tpgmapsu, tpgmaaoti
    c = Bot(srv, f"TpGmB{game}", game, opt.verbose)
    await enter(a, R)
    await enter(c, R)
    await warp(c, R, 1100000, 3, 1, 1, what="player goes to Parum -> 0x0236")
    k = c.mark()
    m = a.mark()
    say(a, f"!summon {c.name}")
    done = await c.wait(0x0236, since=k, timeout=10)
    chk(R, done and c.got(0x020F, k), "!summon -> the player gets a zone load (0x020F .. 0x0236)",
        f"got {[hex(x) for x, _ in c.rx[k:]]}")
    q0 = c.got(0x0C00, k)
    chk(R, q0 and struct.unpack_from("<I", q0[0], 0x24)[0] == 1100000, "!summon lands in the admin's lobby (1100000)",
        hexs(q0[0] if q0 else None, 0x30))
    if done:
        c.post_load()
        await c.settle(0.3)
        chk(R, a.got(0x0201, m) or a.got(0x0111, m), "the admin sees the summoned player spawn", "no 0x0201 / 0x0111")
    k = c.mark()
    say(a, f"!give 03010000 2 {c.name}")
    chk(R, await c.wait(0x0106, since=k, pred=lambda x: x[0x2D] == 0x0B), "!give <id> 2 <name> -> 0x0106 add to them")
    say(a, f"!mute {c.name} 5")
    await a.settle(0.5)
    m = a.mark()
    say(c, "muted line")
    await a.settle(0.8)
    chk(R, not [x for x in a.got(0x0304, m) if "muted line".encode("utf-16-le") in x], "!mute: their chat is not relayed")
    say(a, f"!unmute {c.name}")
    if "!gm <message>" in (gm.__doc__ or ""):                # psu/helpdesk.py
        k = c.mark()
        say(c, "!gm my items vanished")
        t = await c.wait(0x0228, since=k, pred=lambda x: "Sent to the staff".encode("utf-16-le") in x, timeout=5)
        chk(R, t is not None, "!gm from any player -> a 0x0228 saying the staff got it")
    a.close(), c.close()


SCENARIOS = {
    "login": sc_login, "zones": sc_zones, "shop": sc_shop, "shop_sell": sc_shop_sell, "disc_shop": sc_disc_shop,
    "invsync": sc_invsync, "grind": sc_grind, "synthesis": sc_synthesis,
    "myroom": sc_myroom, "mission": sc_mission, "levelup": sc_levelup, "party": sc_party, "technique": sc_technique, "story": sc_story,
    "ep3": sc_ep3, "chat": sc_chat, "trade": sc_trade, "xparty": sc_xparty, "gm": sc_gm, "pp": sc_pp,
    "board": sc_board,
}
CROSS = {"trade", "xparty"}                     # one run with a PSU and an AotI player
TIMEOUT = {"ep3": 3600}                         # scenarios that need more than --timeout (every Ep3 act in turn)


async def run(srv, plan, opt):
    report = []
    for name, game in plan:
        R = []
        mark = srv.mark()
        t0 = time.time()
        status = None
        try:
            status = await asyncio.wait_for(SCENARIOS[name](srv, game, R, opt), max(opt.timeout, TIMEOUT.get(name, 0)))
        except asyncio.TimeoutError:
            chk(R, False, "scenario finished in time", f"timed out after {opt.timeout} s")
        except Fail as e:
            chk(R, False, "scenario ran to the end", str(e))
        except Exception as e:
            chk(R, False, "scenario ran without a client-side exception", f"{e!r}: {traceback.format_exc().splitlines()[-3:]}")
        await errors_since(srv, mark, R)
        ok = all(c[0] for c in R)
        report.append({"scenario": name, "game": game, "ok": ok, "skip": status == "skip", "secs": round(time.time() - t0, 1),
                       "checks": [{"ok": c[0], "what": c[1], "evidence": c[2]} for c in R]})
        tag = "SKIP" if status == "skip" else ("PASS" if ok else "FAIL")
        print(f"[{tag}] {name:<10} {game:<5} {sum(c[0] for c in R)}/{len(R)} checks  ({report[-1]['secs']} s)", flush=True)
        for c in R:
            if not c[0] or opt.verbose:
                print(f"       {'ok ' if c[0] else 'BAD'} {c[1]}" + (f"\n           {c[2]}" if c[2] else ""), flush=True)
        await asyncio.sleep(0.3)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--game", choices=("psu", "aoti", "both"), default="both")
    ap.add_argument("--scenario", default="all", help="one of " + ", ".join(SCENARIOS) + " or all (comma list ok)")
    ap.add_argument("--work", default=default_work())
    ap.add_argument("--keep", action="store_true", help="keep the temp dir (state, logs)")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--json", help="write the report here")
    ap.add_argument("--ports", type=int, help="game on this port, admin on the next; login keys come from the DB")
    ap.add_argument("--list", action="store_true")
    opt = ap.parse_args()
    if opt.list:
        for k, f in SCENARIOS.items():
            print(f"{k:<10} {f.__doc__.splitlines()[0]}")
        return 0
    if not opt.work:
        raise SystemExit("no game data: pass --work or set PSU_DATA / PSU_WORK")
    opt.work = os.path.abspath(opt.work)        # the services run in another directory
    names = list(SCENARIOS) if opt.scenario == "all" else opt.scenario.split(",")
    for n in names:
        if n not in SCENARIOS:
            raise SystemExit(f"unknown scenario {n}")
    games = ["psu", "aoti"] if opt.game == "both" else [opt.game]
    plan = []
    for n in names:
        if n in CROSS:
            plan.append((n, "both" if opt.game == "both" else opt.game))
        else:
            plan += [(n, g) for g in games]
    srv = Server(opt.work, opt.keep, opt.ports)
    print(f"server: {srv.tmp} (game port {srv.game_port}, login {'22110/22140' if srv.real_login else 'keys from the DB'})",
          flush=True)
    srv.start()
    try:
        report = asyncio.run(run(srv, plan, opt))
    finally:
        srv.stop()
    bad = [r for r in report if not r["ok"]]
    print(f"\n{len(report) - len(bad)}/{len(report)} scenarios passed" + (f"; failed: " + ", ".join(
        f"{r['scenario']}/{r['game']}" for r in bad) if bad else ""))
    if opt.json:
        with open(opt.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
