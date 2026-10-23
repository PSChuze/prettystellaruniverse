"""Dev test bot: a second player for the server, so one real client can see someone else in the lobby.

Speaks the game protocol straight to the game service's plaintext port (the real PS2 goes through the
stunnel terminator to the same port): hello -> 0x020D key auth -> 0x0D02 create (a copy of an existing
character's 0x0D02 body with a new name) -> 0x020B select -> acks 0x0B03 fragments with 0x0B05 ->
0x021C load complete -> lobby. Then it stands at a spot, walks between two points with 0x0102 updates
and says something now and then (0x0304, PS2 layout: no name field).

  python tools/bot.py --like GID [--host 127.0.0.1] [--port 22061] [--name TestBot]
                      [--at 30,280,-1420] [--say "hello from the bot"]

--replay SID FROM TO: instead of strolling, loop the movement (0x0503 / 0x0514 / 0x0102) a real client
sent in session SID between times FROM and TO (HH:MM:SS, from data/logs/events.jsonl), same timing; the
server re-stamps them with the bot's GID/LID when it relays them.
--like picks whose stored character (state/chars/<gid>.json, slot 0) the bot copies its looks from.
"""
import argparse
import asyncio
import json
import math
import os
import struct
import time

STATE = os.path.join(os.path.dirname(__file__), "..", "data", "state", "chars")
ACK = struct.pack("<I", 8) + b"\x0b\x05\x00\x00"


def pkt(cmd, body, channel=2):
    size = 8 + len(body)
    pad = (-size) % 4
    return struct.pack("<I", size + pad) + struct.pack(">H", cmd) + bytes([channel, 0]) + body + bytes(pad)


def c0102(gid, pos, d, area, anim=1):
    q, z, m, e = area
    x, y, zz = pos
    body = (bytes(0x24) + struct.pack("<I", gid) + bytes(4) + bytes([anim, 0]) + struct.pack("<H", int(d) & 0xFFFF)
            + bytes(4) + struct.pack("<fff", x, y, zz) + struct.pack("<IIII", q, z, m, e) + bytes(4))
    return pkt(0x0102, body, channel=1)


def c0304(gid, text):
    msg = (text + "\0").encode("utf-16-le")
    mods = bytes([0, 0, 0, len(text) + 1, 0, 0, 0, 0])
    return pkt(0x0304, bytes(0x24) + bytes.fromhex("00001200") + struct.pack("<I", gid) + bytes(8) + mods + msg)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=22061)
    ap.add_argument("--login-port", type=int, default=22110)
    ap.add_argument("--user", help="account name (default: the bot name, lowercased)")
    ap.add_argument("--password", default="botpass")
    ap.add_argument("--name", default="TestBot")
    ap.add_argument("--like", required=True, help="GID (hex) of the stored character to copy the looks from")
    ap.add_argument("--at", default="30,280,-1420")
    ap.add_argument("--say", default="hello from the bot")
    ap.add_argument("--replay", nargs=3, metavar=("SID", "FROM", "TO"))
    ap.add_argument("--invite", type=lambda v: int(v, 0), help="after entering the lobby, invite this GID to a party")
    ap.add_argument("--card", type=lambda v: int(v, 0), help="after entering the lobby, send this GID our partner card")
    a = ap.parse_args()

    chars = json.load(open(os.path.join(STATE, f"{a.like}.json"), encoding="utf-8"))
    raw = bytearray(bytes.fromhex(chars["0"]["raw_0d02"]))
    struct.pack_into("<I", raw, 0x24, 0)                                   # slot 0
    raw[0x28:0x68] = a.name.encode("utf-16-le")[:64].ljust(64, b"\0")      # name

    # real login (plaintext login port behind the SSL terminator): 0x0219 -> 0x0223 GID + key
    user = (a.user or a.name.lower())[:23]
    lr, lw = await asyncio.open_connection(a.host, a.login_port)
    lw.write(pkt(0x0219, bytes(0x24) + user.encode("latin1").ljust(24, b"\0") + a.password.encode("latin1").ljust(24, b"\0")))
    await lw.drain()
    lbuf, gid, key = b"", None, None
    while gid is None:
        lbuf += await lr.read(4096)
        while len(lbuf) >= 8 and len(lbuf) >= struct.unpack_from("<I", lbuf)[0]:
            size, = struct.unpack_from("<I", lbuf)
            p, lbuf = lbuf[:size], lbuf[size:]
            if struct.unpack_from(">H", p, 4)[0] == 0x0223:
                if len(p) != 8 + 0x2C:
                    raise SystemExit("login refused")
                gid, key = struct.unpack_from("<I", p, 8 + 0x24)[0], p[8 + 0x28:8 + 0x2C]
    lw.close()
    a.gid = gid
    print(f"logged in as {user}: GID {gid:#010x}", flush=True)
    r, w = await asyncio.open_connection(a.host, a.port)
    buf = b""
    state = {"lobby": False}

    async def reader():
        nonlocal buf
        while True:
            data = await r.read(65536)
            if not data:
                print("server closed", flush=True)
                return
            buf += data
            while len(buf) >= 8:
                size, = struct.unpack_from("<I", buf)
                if len(buf) < size:
                    break
                p, buf = buf[:size], buf[size:]
                cmd, = struct.unpack_from(">H", p, 4)
                if cmd == 0x0B03:
                    w.write(ACK)
                elif cmd == 0x021B:                 # end of character loading
                    w.write(pkt(0x021C, bytes(0x24) + struct.pack("<I", 0) + bytes(8)))
                elif cmd == 0x0236:
                    state["lobby"] = True
                    print(time.strftime("%H:%M:%S"), "in the lobby", flush=True)
                    if a.invite:
                        w.write(pkt(0x100A, bytes(0x24) + struct.pack("<I", a.invite) + bytes(0x40) + bytes(8)))
                    if a.card:
                        w.write(pkt(0x1514, bytes(0x24) + struct.pack("<I", a.card) + bytes(4)))
                elif cmd == 0x100A:                 # party invite: accept (echo key, result 1, inviter name)
                    print(time.strftime("%H:%M:%S"), "invited to a party: accepting", flush=True)
                    w.write(pkt(0x100C, bytes(0x24) + p[0x2C:0x34] + p[0x34:0x3C] + bytes([1, 0, 0, 0]) + p[0x3C:0x7C]))
                elif cmd == 0x1901:                 # trade request: accept
                    who, = struct.unpack_from("<I", p, 0x2C)
                    print(time.strftime("%H:%M:%S"), f"trade request from {who:#x}: accepting", flush=True)
                    w.write(pkt(0x1901, bytes(0x24) + struct.pack("<I", who) + bytes([2, 0]) + bytes(14)))
                elif cmd == 0x1902:                 # follow the partner: confirm, then final OK
                    who, act, prm = struct.unpack_from("<I", p, 0x30)[0], p[0x34], p[0x35]
                    print(time.strftime("%H:%M:%S"), f"trade state ({act}, {prm}) from {who:#x}", flush=True)
                    reply = {(4, 5): 4, (4, 1): 6, (5, 5): 6}.get((act, prm))
                    if reply:
                        w.write(pkt(0x1901, bytes(0x24) + struct.pack("<I", who) + bytes([reply, 0]) + bytes(14)))
                elif cmd == 0x1503:                 # partner card offered: accept into the first free slot
                    print(time.strftime("%H:%M:%S"), "offered a partner card: accepting", flush=True)
                    w.write(pkt(0x1504, bytes(0x24) + struct.pack("<hBB", -1, 1, 0)))
                elif cmd in (0x0233, 0x0201, 0x0204, 0x0304, 0x1004, 0x1005, 0x1006, 0x100C, 0x1509, 0x150F, 0x0A07):
                    print(time.strftime("%H:%M:%S"), f"got {cmd:#06x} ({size} B)", flush=True)
            await w.drain()

    task = asyncio.create_task(reader())
    auth = b"\xff\xff" + bytes(0x22) + struct.pack("<I", a.gid) + key + bytes(8)
    w.write(pkt(0x020D, auth))
    await asyncio.sleep(0.5)
    w.write(pkt(0x0D02, bytes(raw)))                                       # (re)store the bot's character
    await asyncio.sleep(0.5)
    w.write(pkt(0x020B, bytes(0x24) + struct.pack("<I", 0) + bytes(8)))
    await w.drain()

    track = []
    if a.replay:
        sid, t_from, t_to = a.replay
        log = os.path.join(os.path.dirname(__file__), "..", "data", "logs", "events.jsonl")
        for line in open(log, encoding="utf-8"):
            if sid not in line or '"recv"' not in line:
                continue
            e = json.loads(line)
            if e.get("cmd") in ("0x0503", "0x0514", "0x0102") and t_from <= e["t"][11:19] <= t_to:
                ts = time.mktime(time.strptime(e["t"][:19], "%Y-%m-%dT%H:%M:%S")) + float("0." + e["t"][20:26])
                track.append((ts, int(e["cmd"], 16), bytes.fromhex(e["body"])))
        print(f"replaying {len(track)} packets", flush=True)

    x0, y0, z0 = (float(v) for v in a.at.split(","))
    t0 = time.time()
    last_say = 0
    while track and not task.done():
        if not state["lobby"]:
            await asyncio.sleep(0.5)
            continue
        start, t_first = time.time(), track[0][0]
        for ts, cmd, body in track:
            await asyncio.sleep(max(0.0, (ts - t_first) - (time.time() - start)))
            w.write(pkt(cmd, body, channel=1))
            await w.drain()
            if time.time() - last_say > 30:
                w.write(c0304(a.gid, a.say))
                last_say = time.time()
    while not task.done():
        await asyncio.sleep(1.0)
        if not state["lobby"]:
            continue
        t = time.time() - t0
        x = x0 + 20 * math.sin(t / 4)                                      # stroll back and forth
        w.write(c0102(a.gid, (x, y0, z0), 16384 if math.cos(t / 4) > 0 else 49152, (1100000, 0, 4, 0), anim=1))
        if time.time() - last_say > 30:
            w.write(c0304(a.gid, a.say))
            last_say = time.time()
        await w.drain()


if __name__ == "__main__":
    asyncio.run(main())
