#!/usr/bin/env python3
"""
Minimal PINE client for PCSX2: read and write the running game's EE memory.

PINE is PCSX2's IPC socket (TCP 127.0.0.1:<slot> on Windows; the slot is
`PINESlot` in PCSX2.ini). It shows what is really in memory, which settles
questions static disassembly leaves open.

    python pine.py status              # is anything listening / what game
    python pine.py id                  # game serial, e.g. SLUS-21194
    python pine.py title
    python pine.py read32 0x001d5280
    python pine.py dump   0x001d5280 32
    python pine.py write32 0x001d5280 0x03e00008
    python pine.py savestate 1         # whole EE RAM lands in sstates/*.p2s

If you run more than one PCSX2 instance, give each its own PINE slot in its
PCSX2.ini (EnablePINE, PINESlot); two instances sharing one slot means the
loser gets no PINE and no emulog. Pass --slot to pick which instance, and confirm with `id` before
trusting a read. On Windows, which process owns a slot:
    Get-NetTCPConnection -State Listen -LocalPort <slot> | select OwningProcess

Wire format: every request is [u32 total_len][u8 opcode][args...], where
total_len counts its own 4 bytes. Every reply is [u32 total_len][u8 result]
[data...], result 0 = OK.
"""
import argparse
import socket
import struct
import sys

DEFAULT_SLOT = 28014   # E:mulatorsPCSX2-PSU; override with --slot

MSG_READ8, MSG_READ16, MSG_READ32, MSG_READ64 = 0x00, 0x01, 0x02, 0x03
MSG_WRITE8, MSG_WRITE16, MSG_WRITE32, MSG_WRITE64 = 0x04, 0x05, 0x06, 0x07
MSG_VERSION, MSG_TITLE, MSG_ID, MSG_UUID = 0x08, 0x0B, 0x0C, 0x0D
MSG_SAVESTATE, MSG_LOADSTATE = 0x09, 0x0A
MSG_GAMEVERSION, MSG_STATUS = 0x0E, 0x0F

STATUS = {0: "RUNNING", 1: "PAUSED", 2: "SHUTDOWN"}


class PineError(RuntimeError):
    pass


class Pine:
    def __init__(self, slot=DEFAULT_SLOT, host="127.0.0.1", timeout=5.0):
        self.addr = (host, slot)
        self.timeout = timeout
        self.sock = None

    def _connect(self):
        try:
            self.sock = socket.create_connection(self.addr, timeout=self.timeout)
        except OSError as e:
            self.sock = None
            raise PineError("cannot reach PINE at %s:%d (%s). Is PCSX2 running "
                            "with EnablePINE=true, and does THIS instance own "
                            "the slot?" % (self.addr[0], self.addr[1], e))

    def _txn(self, payload):
        """One request/response on a kept-open connection (PCSX2 serves many per connection).
        A fresh connection per request exhausts Windows' ephemeral ports at 100 Hz polling
        (WinError 10048), so reconnect only after an error."""
        pkt = struct.pack("<I", 4 + len(payload)) + payload
        for attempt in (1, 2):
            if self.sock is None:
                self._connect()
            try:
                self.sock.sendall(pkt)
                head = self._recvn(self.sock, 4)
                total = struct.unpack("<I", head)[0]
                if total < 5:
                    raise PineError("short reply header (%d)" % total)
                body = self._recvn(self.sock, total - 4)
                break
            except (OSError, PineError):
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
                if attempt == 2:
                    raise
        if body[0] != 0:
            raise PineError("PINE returned failure code 0x%02x" % body[0])
        return body[1:]

    @staticmethod
    def _recvn(s, n):
        buf = b""
        while len(buf) < n:
            c = s.recv(n - len(buf))
            if not c:
                raise PineError("connection closed mid-reply")
            buf += c
        return buf

    def read(self, addr, size):
        op = {1: MSG_READ8, 2: MSG_READ16, 4: MSG_READ32, 8: MSG_READ64}[size]
        d = self._txn(struct.pack("<BI", op, addr))
        return int.from_bytes(d[:size], "little")

    def write(self, addr, size, value):
        op = {1: MSG_WRITE8, 2: MSG_WRITE16, 4: MSG_WRITE32, 8: MSG_WRITE64}[size]
        val = int(value).to_bytes(size, "little")
        self._txn(struct.pack("<BI", op, addr) + val)

    def _string(self, op):
        d = self._txn(struct.pack("<B", op))
        n = struct.unpack("<I", d[:4])[0]
        return d[4:4 + n].rstrip(b"\x00").decode("utf-8", "replace")

    def title(self):
        return self._string(MSG_TITLE)

    def game_id(self):
        return self._string(MSG_ID)

    def game_version(self):
        return self._string(MSG_GAMEVERSION)

    def uuid(self):
        return self._string(MSG_UUID)

    def status(self):
        d = self._txn(struct.pack("<B", MSG_STATUS))
        v = struct.unpack("<I", d[:4])[0]
        return STATUS.get(v, "UNKNOWN(%d)" % v)

    def savestate(self, slot):
        """Ask PCSX2 to save state slot N (asynchronous; the .p2s appears in sstates/)."""
        self._txn(struct.pack("<BB", MSG_SAVESTATE, slot))

    def dump(self, addr, nbytes):
        out = b""
        for off in range(0, nbytes, 4):
            out += self.read(addr + off, 4).to_bytes(4, "little")
        return out[:nbytes]


def main():
    ap = argparse.ArgumentParser(description="PCSX2 PINE memory client")
    ap.add_argument("--slot", type=int, default=DEFAULT_SLOT)
    ap.add_argument("cmd", choices=["status", "id", "title", "version", "uuid",
                                    "read8", "read16", "read32", "read64",
                                    "write8", "write16", "write32", "dump", "savestate"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    p = Pine(slot=a.slot)
    try:
        if a.cmd == "status":
            print("status : %s" % p.status())
            print("id     : %s" % p.game_id())
            print("title  : %s" % p.title())
        elif a.cmd == "id":
            print(p.game_id())
        elif a.cmd == "title":
            print(p.title())
        elif a.cmd == "version":
            print(p.game_version())
        elif a.cmd == "uuid":
            print(p.uuid())
        elif a.cmd.startswith("read"):
            size = int(a.cmd[4:]) // 8
            addr = int(a.args[0], 0)
            print("0x%08x = 0x%0*x" % (addr, size * 2, p.read(addr, size)))
        elif a.cmd.startswith("write"):
            size = int(a.cmd[5:]) // 8
            addr, val = int(a.args[0], 0), int(a.args[1], 0)
            before = p.read(addr, size)
            p.write(addr, size, val)
            after = p.read(addr, size)
            print("0x%08x : 0x%0*x -> 0x%0*x%s"
                  % (addr, size * 2, before, size * 2, after,
                     "" if after == val else "   !! WRITE DID NOT STICK"))
        elif a.cmd == "savestate":
            n = int(a.args[0]) if a.args else 1
            p.savestate(n)
            print("savestate slot %d requested" % n)
        elif a.cmd == "dump":
            addr = int(a.args[0], 0)
            n = int(a.args[1], 0) if len(a.args) > 1 else 32
            data = p.dump(addr, n)
            for off in range(0, len(data), 16):
                ch = data[off:off + 16]
                hx = " ".join("%02x" % c for c in ch)
                asc = "".join(chr(c) if 0x20 <= c < 0x7f else "." for c in ch)
                print("%08x  %-47s  %s" % (addr + off, hx, asc))
    except PineError as e:
        sys.exit("PINE error: %s" % e)


if __name__ == "__main__":
    main()
