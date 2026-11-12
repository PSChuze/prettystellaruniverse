"""Observation stub: accept the PS2 client on the login and patch ports and log every byte.

The US PS2 clients dial (read from ERX/PSU_US.GZ, see docs/discs.md):

    PSU   loginps2.psuus.segaonline.jp:12110    patchps2.psuus.segaonline.jp:11110
    AotI  loginps2.ipsuus.segaonline.jp:12140   patchps2.ipsuus.segaonline.jp:11140

This listener only records: one hex-dump log per
session under $PSU_LOG_DIR/sessions plus a line per event in events.jsonl. The
client links RSA SSL-C 2.1.0 (SSLv2/v3), so a session that opens with an SSL
ClientHello gets it decoded into the event line (record version, offered cipher
suites). It never answers unless PSU_OBSERVE_GREETING (hex) is set, which is a
probe knob for trying a first server message by hand.

Env:
  PSU_OBSERVE_PORTS    comma list of TCP ports (default 11110,11140,12110,12140)
  PSU_BIND             bind address (default 0.0.0.0)
  PSU_LOG_DIR          log root (default ./logs)
  PSU_OBSERVE_GREETING hex bytes sent on accept (default: send nothing)
  PSU_IDLE_SECS        close a silent session after this long (default 600)
"""
import asyncio
import datetime as dt
import json
import os
import struct
import sys

BIND = os.environ.get("PSU_BIND", "0.0.0.0")
PORTS = [int(p) for p in os.environ.get("PSU_OBSERVE_PORTS", "11110,11140,12110,12140").split(",") if p.strip()]
LOG_DIR = os.environ.get("PSU_LOG_DIR", os.path.join(os.getcwd(), "logs"))
GREETING = bytes.fromhex(os.environ.get("PSU_OBSERVE_GREETING", ""))
IDLE = float(os.environ.get("PSU_IDLE_SECS", "600"))
# server-sent packets dump only their first bytes to the session logs (0 = all): a zone download is disc data
# and hexdumping it whole was most of a login's CPU time with a full lobby
HEX_SENT = int(os.environ.get("PSU_LOG_HEX_SENT", "256"))
# walking (0x0503 / 0x0514 / 0x0102 on channel 1, a few a second per player) stays out of the session logs and
# events.jsonl unless set: with a full lobby it was most of what was logged (tools/bot.py --replay needs it)
LOG_MOVES = os.environ.get("PSU_LOG_MOVES", "") not in ("", "0")
MOVES = (0x0503, 0x0514, 0x0102)

ROLE = {11110: "psu-patch", 12110: "psu-login", 11140: "aoti-patch", 12140: "aoti-login",
        11200: "x360-patch"}


def listen_spec(env, default):
    """Ports from a comma list where an item is `port` (on PSU_BIND) or `host:port` (its own bind address,
    e.g. a public plaintext port next to loopback ones). [(host, port)]"""
    out = []
    for item in os.environ.get(env, default).split(","):
        item = item.strip()
        if item:
            host, _, port = item.rpartition(":")
            out.append((host or BIND, int(port)))
    return out


def now():
    return dt.datetime.now(dt.timezone.utc)


def hexdump(data, width=16, limit=0):
    """limit > 0: only the first `limit` bytes, then a line with how many were left out."""
    out = []
    end = min(len(data), limit) if limit > 0 else len(data)
    for i in range(0, end, width):
        chunk = data[i:min(i + width, end)]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"  {i:06x}  {hx:<{width * 3}} {asc}")
    if end < len(data):
        out.append(f"  ... {len(data) - end} more bytes")
    return "\n".join(out)


def client_hello(data):
    """Describe an SSLv2 or SSLv3/TLS ClientHello at the start of a stream, else None."""
    try:
        if len(data) >= 5 and data[0] == 0x16 and data[1] == 3:
            ver = f"3.{data[2]}"
            body = data[5:]
            if body[0] != 1:
                return None
            hv = f"3.{body[5]}"
            p = 4 + 2 + 32
            p += 1 + body[p]
            n, = struct.unpack_from(">H", body, p)
            suites = [f"{x:04x}" for x in struct.unpack_from(f">{n // 2}H", body, p + 2)]
            return {"hello": "ssl3", "record": ver, "client_version": hv, "suites": suites}
        if len(data) >= 11 and data[0] & 0x80 and data[2] == 1:
            hv = f"{data[3]}.{data[4]}"
            n, = struct.unpack_from(">H", data, 5)
            specs = data[11:11 + n]
            suites = [specs[i:i + 3].hex() for i in range(0, len(specs), 3)]
            return {"hello": "ssl2", "client_version": hv, "suites": suites}
    except (IndexError, struct.error):
        return {"hello": "truncated"}
    return None


_events = None             # events.jsonl, kept open (line buffered): an open per line was a big share of the CPU


def event(**kw):
    global _events
    kw["t"] = now().isoformat()
    line = json.dumps(kw)
    try:
        if _events is None:
            _events = open(os.path.join(LOG_DIR, "events.jsonl"), "a", encoding="utf-8", buffering=1)
        _events.write(line + "\n")
    except (OSError, ValueError):
        _events = None
    print(line, flush=True)


async def session(reader, writer, port):
    peer = writer.get_extra_info("peername") or ("?", 0)
    sid = f"{now():%Y%m%dT%H%M%S%f}_{peer[0]}_{peer[1]}_p{port}"
    path = os.path.join(LOG_DIR, "sessions", sid + ".log")
    event(ev="open", sid=sid, peer=peer[0], port=port, role=ROLE.get(port, "?"))
    total = 0
    with open(path, "a", encoding="utf-8") as log:
        def record(direction, data):
            log.write(f"{now().isoformat()} {direction} {len(data)} bytes\n{hexdump(data)}\n")
            log.flush()

        if GREETING:
            writer.write(GREETING)
            await writer.drain()
            record("S>C", GREETING)
        try:
            while True:
                data = await asyncio.wait_for(reader.read(65536), IDLE)
                if not data:
                    break
                if total == 0:
                    hello = client_hello(data)
                    if hello:
                        event(ev="hello", sid=sid, **hello)
                total += len(data)
                record("C>S", data)
        except asyncio.TimeoutError:
            log.write(f"{now().isoformat()} idle timeout\n")
        except ConnectionError as e:
            log.write(f"{now().isoformat()} {e!r}\n")
    writer.close()
    event(ev="close", sid=sid, bytes_in=total)


async def main():
    os.makedirs(os.path.join(LOG_DIR, "sessions"), exist_ok=True)
    servers = []
    for port in PORTS:
        servers.append(await asyncio.start_server(
            lambda r, w, p=port: session(r, w, p), BIND, port))
        print(f"observe: listening on {BIND}:{port} ({ROLE.get(port, '?')}), logs in {LOG_DIR}",
              file=sys.stderr, flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
