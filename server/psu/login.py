"""Login service (plaintext; stunnel terminates the client's SSL 3.0 on 12110/12140 in front of it).

Protocol from EGS (github.com/essen/egs: docs/protocol is public domain, the Erlang code is AGPL;
this is a re-implementation from those descriptions). The PS2 client speaks the same login
protocol as the PC one: its first packet is 0x080E with Platform = 0.

Framing, both ways: u32le size (incl. header, padded to 4) | u16 BIG-endian command | u8 channel
(client 2, server 3) | u8 0 | body.

  S>C 0x0202  hello on connect: target id ffff, GID
  C>S 0x080E  client version (language, entrance, platform, version)      -> logged
  C>S 0x0226/0x023F  MOTD page request                                   -> 0x0225 MOTD page
  C>S 0x0219  login: username[24], password[24]                         -> 0x0223 GID + auth key
  C>S 0x0217  game server request                                        -> 0x0216 ip, port; close
  (the client then connects to the game server and sends 0x020D GID + key)

Accounts (psu/db.py): a known name must give its password (PBKDF2 hash check); an unknown name registers
with the password it gave when PSU_AUTOREGISTER=1 (default, for testing), else it is refused. A refusal
is 0x0223 with an error text (EGS system_auth_error). The 4-byte key goes into the shared database;
the game service takes it once on 0x020D, with
the game and platform the login port belongs to (12110 PSU, 12140 AotI on PS2; 12230 AotI PC). Passwords are
never logged or stored in clear. PC logs in over SSLv3 like PS2 (stunnel 12230 -> 22230), 0x080E Platform = 1;
that leg is experimental and off unless 22230 is added to PSU_LOGIN_PORTS.

Env: PSU_LOGIN_PORTS (default 22110,22140; `host:port` binds that item elsewhere),
PSU_GAME_ADDR (default 127.0.0.1), PSU_GAME_PORT (default 12061), PSU_MOTD (used while $PSU_STATE_DIR/news.txt holds no notice, psu/news.py),
PSU_AUTOREGISTER (default 1), PSU_STATE_DIR (default
<log dir>/../state), plus PSU_BIND / PSU_LOG_DIR / PSU_IDLE_SECS.
"""
import asyncio
import hashlib
import os
import secrets
import socket
import struct
import time
import sys

from psu import db as dbm, gamelink, news
from psu.observe import LOG_DIR, IDLE, event, hexdump, listen_spec, now

LISTEN = listen_spec("PSU_LOGIN_PORTS", "22110,22140")
GAME_ADDR = os.environ.get("PSU_GAME_ADDR", "127.0.0.1")
GAME_PORT = int(os.environ.get("PSU_GAME_PORT", "12061"))
MOTD = os.environ.get("PSU_MOTD", "Welcome to Phantasy Star Universe online.")   # about 50 characters fit a line
ROLE = {22110: "psu-login", 22140: "aoti-login", 22230: "aoti-pc-login"}
GAME = {22110: "psu", 22140: "aoti", 22230: "aoti"}   # which disc logs in on which port; travels with the key to game.py
PLATFORM = {22230: "pc"}                              # default ps2; travels with the key too
PLATFORM_BYTE = {0: "ps2", 1: "pc"}                                 # 0x080E +0x28 (EGS); anything else is logged raw
LANG = {0: "ja", 1: "en", 3: "fr", 4: "de"}
TAG = bytes.fromhex("00000f00")   # EGS writes this word big-endian (16#00000f00:32), before a GID
STATE_DIR = os.environ.get("PSU_STATE_DIR", os.path.join(LOG_DIR, "..", "state"))
AUTOREGISTER = os.environ.get("PSU_AUTOREGISTER", "1") == "1"


def packet(cmd, body, channel=3):
    size = 8 + len(body)
    pad = (-size) % 4
    return struct.pack("<I", size + pad) + struct.pack(">H", cmd) + bytes([channel, 0]) + body + bytes(pad)


def redact(data):
    """Blank the password field of any whole 0x0219 packet in data (logs must never hold it).

    A 0x0219 split across reads is not caught here; the per-packet recv event still is.
    """
    out = bytearray(data)
    o = 0
    while o + 8 <= len(out):
        size, = struct.unpack_from("<I", out, o)
        if size < 8:
            break
        if out[o + 4:o + 6] == bytes((0x02, 0x19)) and o + 8 + 0x54 <= len(out):
            out[o + 8 + 0x3C:o + 8 + 0x54] = bytes(24)
        o += size
    return bytes(out)


FAIL_WINDOW, FAIL_LIMIT = 600, int(os.environ.get("PSU_LOGIN_FAIL_LIMIT", "8"))
_fails = {}      # account name -> [failure times] in the last FAIL_WINDOW s (every peer is 127.0.0.1 behind ssl3)


def failed(key):
    _fails.setdefault(key, []).append(time.time())


def throttled(key):
    """True once an account name has FAIL_LIMIT failed logins within FAIL_WINDOW seconds."""
    now_ = time.time()
    recent = [t for t in _fails.get(key, []) if now_ - t < FAIL_WINDOW]
    if recent:
        _fails[key] = recent
    else:
        _fails.pop(key, None)
    return len(recent) >= FAIL_LIMIT


def gid_for(username):
    """Stable 32-bit account id from the name. Never 0."""
    return (int.from_bytes(hashlib.sha1(username.encode("latin1")).digest()[:4], "little") & 0x7FFFFFFF) or 1


def hello(gid, target=0xFFFF):
    return packet(0x0202, struct.pack("<H", target) + bytes(34) + struct.pack("<I", gid) + bytes(128))


def auth_info(gid, key):
    return packet(0x0223, bytes(20) + TAG + struct.pack("<I", gid) + bytes(8) + struct.pack("<I", gid) + key)


def auth_error(gid, text):
    """EGS system_auth_error: 0x0223 with code 3 and a UTF-16 message the client shows."""
    msg = text.encode("utf-16-le")
    return packet(0x0223, bytes(20) + TAG + struct.pack("<I", gid) + bytes(16) + struct.pack("<I", 3) + bytes(6)
                  + struct.pack("<H", len(msg) // 2 + 2) + msg + bytes(2))


def game_server(gid, addr, port, target=0xFFFF):
    return packet(0x0216, struct.pack("<H", target) + bytes(2) + bytes(16)
                  + TAG + struct.pack("<I", gid) + bytes(8)
                  + socket.inet_aton(addr) + struct.pack("<H", port) + bytes(2))


def motd_page(text, page, target=0xFFFF):
    lines = text.split("\n")
    pages = max(1, (len(lines) + 14) // 15)
    page = min(page, pages - 1)
    body = "".join(line + "\n" for line in lines[page * 15:page * 15 + 15]).encode("utf-16-le")
    length = len(body) // 2 + 2
    return packet(0x0225, struct.pack("<H", target) + bytes(34) + bytes([pages, page])
                  + struct.pack("<H", length) + body + bytes(2))


async def session(reader, writer, port):
    peer = writer.get_extra_info("peername") or ("?", 0)
    sid = f"{now():%Y%m%dT%H%M%S%f}_{peer[0]}_{peer[1]}_p{port}"
    gid = 0x10000000 | secrets.randbits(24)   # temporary until the client logs in
    event(ev="open", sid=sid, peer=peer[0], port=port, role=ROLE.get(port, "?"))
    with open(os.path.join(LOG_DIR, "sessions", sid + ".log"), "a", encoding="utf-8") as log:
        def record(direction, data):
            log.write(f"{now().isoformat()} {direction} {len(data)} bytes\n{hexdump(data)}\n")
            log.flush()

        async def send(data, what):
            writer.write(data)
            await writer.drain()
            record("S>C", data)
            event(ev="send", sid=sid, what=what, n=len(data))

        buf = b""
        try:
            await send(hello(gid), "0x0202 hello")
            while True:
                while len(buf) >= 8:
                    size, = struct.unpack_from("<I", buf)
                    if size < 8 or len(buf) < size:
                        break
                    pkt, buf = buf[:size], buf[size:]
                    cmd, = struct.unpack_from(">H", pkt, 4)
                    body = pkt[8:]
                    # 0x0219 carries the password at body 0x3C..0x54: never log it (see redact())
                    shown = redact(pkt)[8:]
                    event(ev="recv", sid=sid, cmd=f"{cmd:#06x}", channel=pkt[6], size=size, body=shown[:96].hex())
                    if cmd == 0x080E and len(body) >= 0x30:
                        lang, entrance, platform = body[0x25], body[0x27], body[0x28]
                        ver, = struct.unpack_from("<I", body, 0x2C)
                        event(ev="version", sid=sid, language=LANG.get(lang, lang), entrance=entrance,
                              platform=PLATFORM_BYTE.get(platform, platform), port_platform=PLATFORM.get(port, "ps2"),
                              version=f"{ver:#010x}")
                    elif cmd in (0x0226, 0x023F) and len(body) >= 0x28:
                        await send(motd_page(news.motd(STATE_DIR, MOTD), body[0x24]), f"0x0225 motd page {body[0x24]}")
                    elif cmd == 0x0219 and len(body) >= 0x54:
                        user = body[0x24:0x3C].split(b"\0")[0].decode("latin1")
                        password = body[0x3C:0x54].split(b"\0")[0].decode("latin1")
                        if throttled(user.lower()):
                            del password
                            event(ev="login_refused", sid=sid, user=user, reason="rate limited")
                            await send(auth_error(gid, "Too many login attempts. Please wait a few minutes."),
                                       "0x0223 auth error (rate limited)")
                            continue
                        acct, why = await sign_in(user, password)
                        del password
                        if acct is None:
                            failed(user.lower())
                            event(ev="login_refused", sid=sid, user=user, reason=why)   # password never logged
                            await send(auth_error(gid, "Login failed: wrong user ID or password."), "0x0223 auth error")
                            continue
                        ban = dbm.get(STATE_DIR).banned(acct)
                        if ban is not None:
                            event(ev="login_refused", sid=sid, user=user, reason="banned")
                            until = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ban[0]))
                            await send(auth_error(gid, f"This account is suspended until {until}."), "0x0223 banned")
                            continue
                        gid = acct
                        key = dbm.get(STATE_DIR).issue_key(gid, GAME.get(port, "psu"), PLATFORM.get(port, "ps2"))
                        event(ev="login", sid=sid, user=user, gid=f"{gid:#010x}", game=GAME.get(port, "psu"),
                              platform=PLATFORM.get(port, "ps2"))   # password never logged
                        await send(auth_info(gid, key), "0x0223 auth ok")
                    elif cmd == 0x0217:
                        await send(game_server(gid, GAME_ADDR, GAME_PORT), f"0x0216 game {GAME_ADDR}:{GAME_PORT}")
                        return
                data = await asyncio.wait_for(reader.read(65536), IDLE)
                if not data:
                    break
                buf += data
                record("C>S", redact(data))
        except asyncio.TimeoutError:
            log.write(f"{now().isoformat()} idle timeout\n")
        except ConnectionError as e:
            log.write(f"{now().isoformat()} {e!r}\n")
        finally:
            writer.close()
            event(ev="close", sid=sid)


async def sign_in(user, password):
    """(gid, None) or (None, reason) for a 0x0219 login.

    1. The PSU account with its own password (or reset code) is itself.
    2. Otherwise, when what was typed is an OpenLobby website login (regapi /v1/game-auth), the
       member's linked PSU login is used; a member with none gets this name registered and linked,
       if no one has it.
    3. Otherwise an unknown name registers when PSU_AUTOREGISTER=1, except the name of an
       OpenLobby login typed with the wrong password ("reserved").
    The database stays on the event loop (its connection is not shared between threads); only the
    regapi request runs in a worker thread."""
    db = dbm.get(STATE_DIR)
    acct, why = db.login(user, password, gid_for, register=False)
    if acct is not None:
        return acct, None
    loop = asyncio.get_running_loop()
    pol = await loop.run_in_executor(None, gamelink.pol_auth, "psu", user, password)
    if pol and pol.get("ok"):
        if pol.get("linked"):
            return int(pol["linked"][0]["key"], 16), None
        if why != "unknown account" or not dbm.valid_name(user):
            return None, why
        acct, why = db.login(user, password, gid_for, register=True)
        if acct is not None:
            db.audit("register", user, acct, by="OpenLobby login")
            await loop.run_in_executor(None, lambda: gamelink.pol_auth(
                "psu", user, password, account=user, key=f"{acct:08x}"))
        return acct, why
    if pol and pol.get("reserved") and why == "unknown account":
        return None, "OpenLobby login name"
    if why == "unknown account" and AUTOREGISTER:
        return db.login(user, password, gid_for, register=True)
    return None, why


def link_verify(req):
    """The website's account link (psu/gamelink.py). Its own connection: the API runs in a thread."""
    row, why = dbm.DB(STATE_DIR).verify(str(req.get("account", "")), str(req.get("password", "")))
    if row is None:
        return {"ok": False, "error": why}
    return {"ok": True, "account": row[0], "key": f"{row[1]:08x}"}


def link_set_password(req):
    pw = str(req.get("password", ""))
    if not (dbm.MIN_PASSWORD <= len(pw) <= dbm.NAME_MAX and pw.isascii() and pw.isprintable()
            and pw == pw.strip()):
        return {"ok": False, "error": "bad_new_password"}
    try:
        row = dbm.DB(STATE_DIR).set_password(str(req.get("account", "")), pw, by="website")
    except KeyError:
        return {"ok": False, "error": "no_account"}
    return {"ok": True, "account": row[0], "key": f"{row[1]:08x}"}


async def main():
    os.makedirs(os.path.join(LOG_DIR, "sessions"), exist_ok=True)
    gamelink.serve({"/v1/verify": link_verify, "/v1/set-password": link_set_password},
                   int(os.environ.get("PSU_LINK_PORT", "22192")),
                   log=lambda m: print(m, file=sys.stderr, flush=True))
    servers = []
    for host, port in LISTEN:
        servers.append(await asyncio.start_server(lambda r, w, p=port: session(r, w, p), host, port))
        print(f"login: listening on {host}:{port} ({ROLE.get(port, '?')}, {PLATFORM.get(port, 'ps2')}), "
              f"game server {GAME_ADDR}:{GAME_PORT}", file=sys.stderr, flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
