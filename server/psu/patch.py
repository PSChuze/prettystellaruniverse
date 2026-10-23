"""Patch server (11110 PSU / 11140 AotI on PS2): tell the client it is up to date, log every byte.

Protocol from EGS's PC patch server (github.com/essen/egs, apps/egs_patch; protocol docs public
domain, code AGPL; this is a re-implementation from the description, not a copy). Every
command is `u32le size (incl. header) | u16le cmd | u16 pad | body`:

  S>C 0x01  hello, 0x28 bytes, encryption off
  C>S 0x14  hello, 52 bytes: magic e44c0915, four unknown u32, game version, unknown, 16 zero
            (PSU's PS2 disc sends 0x02 instead, 32 bytes: e44c0915 0 0 1 1 000100be; AotI's sends 0x14)
  S>C 0x06  file list begin   (then 0x07 per file, 0x09/0x0a folder in/out)
  S>C 0x08  file list end
  C>S 0x0c  file info begin, 0x0d per file {number, crc, size}, 0x0e end
  S>C 0x13  update complete; server closes

That is the PC flow. Neither PS2 disc knows the file list commands: both are answered 0x13 right
after their hello (up to date). The dialect is per port (DIALECT): "ps2" answers 0x13 at once, "pc"
sends an empty file list (0x06, 0x08), takes the client's file info (0x0c, 0x0d..., 0x0e) and answers
0x13. 11230 is EGS's US PC patch port (AotI PC, experimental and off by default). Every byte both ways goes to $PSU_LOG_DIR/sessions
and one line per command to events.jsonl.

Experimental: the Xbox 360 disc dials `patch.psueu.segaonline.jp` by plain DNS and opens a plain
TCP socket to 11200 (from static analysis of its executable). Its TPatchClient is "as on other
platforms", so the same framing is assumed; its hello has not been observed. 11200 defaults to the
"ps2" dialect (answer 0x13 complete at once, the least likely to wedge the client) and whatever it
sends is logged; PSU_PATCH_DIALECTS=11200:pc switches it once a real hello is known. The 360
login/game servers are reached through Live's XLSP gateway, not DNS, so only this patch leg is
server-reachable.

Env: PSU_PATCH_PORTS (default 11110,11140; add 11200 and 11230 for the experimental legs), PSU_PATCH_DIALECTS ("port:ps2|pc,..." over
DIALECT), PSU_BIND, PSU_LOG_DIR, PSU_IDLE_SECS.
"""
import asyncio
import os
import struct
import sys

from psu.observe import BIND, LOG_DIR, IDLE, event, hexdump, now

PORTS = [int(p) for p in os.environ.get("PSU_PATCH_PORTS", "11110,11140").split(",") if p.strip()]
ROLE = {11110: "psu-patch", 11140: "aoti-patch", 11200: "x360-patch", 11230: "aoti-pc-patch"}
DIALECT = {11110: "ps2", 11140: "ps2", 11200: "ps2", 11230: "pc"}
for _item in os.environ.get("PSU_PATCH_DIALECTS", "").split(","):
    if ":" in _item:
        _port, _dialect = _item.split(":", 1)
        DIALECT[int(_port)] = _dialect.strip()


def cmd(n, body=b""):
    return struct.pack("<IHH", 8 + len(body), n, 0) + body


HELLO = cmd(0x01, bytes.fromhex("8b9f2dfa") + bytes(12) + struct.pack("<I", 1) + bytes(12))
COMPLETE = cmd(0x13)
FILE_LIST_EMPTY = cmd(0x06) + cmd(0x08)     # PC: list begin, no folders or files, list end


async def session(reader, writer, port):
    peer = writer.get_extra_info("peername") or ("?", 0)
    sid = f"{now():%Y%m%dT%H%M%S%f}_{peer[0]}_{peer[1]}_p{port}"
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
        dialect = DIALECT.get(port, "ps2")
        try:
            await send(HELLO, "0x01 hello")
            while True:
                while len(buf) >= 8:
                    size, c = struct.unpack_from("<IH", buf)
                    if size < 8 or len(buf) < size:
                        break
                    body, buf = buf[8:size], buf[size:]
                    event(ev="recv", sid=sid, cmd=f"{c:#04x}", size=size, body=body[:64].hex())
                    if c in (0x02, 0x14):
                        # Both PS2 discs: PSU sends 0x02 (32 B, e44c0915 0 0 1 1 000100be). AotI's hello
                        # handler (0x390b40) sends the PC-sized 0x14 (52 B, version 20bcc000) when our
                        # 0x01 has a non-zero word at +0x18, which it does.
                        words = struct.unpack_from(f"<{len(body) // 4}I", body)
                        event(ev="hello", sid=sid, dialect=dialect, hello=f"{c:#04x}",
                              words=[f"{w:08x}" for w in words])
                        if dialect == "pc":
                            # EGS: the file list, then the client reports what it has (0x0c / 0x0d / 0x0e)
                            await send(FILE_LIST_EMPTY, "0x06+0x08 empty file list")
                            continue
                        # PS2 has no file list. PSU's 8 handlers (table 0x59cedc): hello, no-op, redirect,
                        # update info, file begin, data, file end, complete; the last five are PC
                        # 0x0f..0x13. AotI's command table (0x56a280, {cmd, max size, handler}): 01 hello,
                        # 03 (u16 list), 04 redirect, 0f-12 update/file, 13 complete; 0x06/0x08 are not
                        # in it, so AotI drops the connection on the PC empty list. On PSU, 0x06+0x08 ->
                        # "Failed to download system file"; bare 0x08 -> state 7 err 7. Both games:
                        # 0x13 complete (state 1 -> 2 = up to date).
                        await send(COMPLETE, "0x13 complete")
                    elif c == 0x0D and len(body) >= 12:
                        number, crc, fsize = struct.unpack_from("<3I", body)
                        event(ev="fileinfo", sid=sid, number=number, crc=f"{crc:08x}", size=fsize)
                    elif c == 0x0E:
                        await send(COMPLETE, "0x13 complete")
                        return
                data = await asyncio.wait_for(reader.read(65536), IDLE)
                if not data:
                    break
                record("C>S", data)
                buf += data
        except asyncio.TimeoutError:
            log.write(f"{now().isoformat()} idle timeout\n")
        except ConnectionError as e:
            log.write(f"{now().isoformat()} {e!r}\n")
        finally:
            writer.close()
            event(ev="close", sid=sid)


async def main():
    os.makedirs(os.path.join(LOG_DIR, "sessions"), exist_ok=True)
    servers = []
    for port in PORTS:
        servers.append(await asyncio.start_server(lambda r, w, p=port: session(r, w, p), BIND, port))
        print(f"patch: listening on {BIND}:{port} ({ROLE.get(port, '?')}, {DIALECT.get(port, 'ps2')})",
              file=sys.stderr, flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
