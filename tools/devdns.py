"""Local DNS for testing: answer the games' host names with one address, REFUSE everything else.

PCSX2 ignores its [DEV9/Eth/Hosts] table under ModeDNS=Manual, so point DNS1 at the machine running this.
Refusing unknown names is deliberate: the four segaonline.jp names still resolve publicly (Sega of America,
198.176.10.x), and a console that reaches them talks to hosts nobody here controls.

For testing, and for a player who reaches a server over the internet: run it at home with --answer set to the
server's public address. A home host does not need it: PSU Server Setup adds these names to the shared
OpenLobby DNS server (docs/hosting.md).

Usage:
  python devdns.py --answer <this machine's LAN address> [--bind 0.0.0.0] [--port 53] [--name host ...]
"""
import argparse
import socket
import struct

DEFAULT_NAMES = [
    "loginps2.psuus.segaonline.jp", "patchps2.psuus.segaonline.jp",    # PSU (SLUS-21194)
    "loginps2.ipsuus.segaonline.jp", "patchps2.ipsuus.segaonline.jp",  # AotI (SLUS-21631)
    "patch.psueu.segaonline.jp",     # PSU X360 (patch leg only; login/game go via Live XLSP, not DNS)
]


def parse_qname(pkt, off):
    labels = []
    while True:
        n = pkt[off]
        off += 1
        if n == 0:
            return ".".join(labels).lower(), off
        labels.append(pkt[off:off + n].decode("ascii", "replace"))
        off += n


def reply(pkt, names, answer_ip):
    tid, flags, qd = struct.unpack(">HHH", pkt[:6])
    name, off = parse_qname(pkt, 12)
    qtype, qclass = struct.unpack(">HH", pkt[off:off + 4])
    question = pkt[12:off + 4]
    rd = flags & 0x0100
    if name not in names:
        return struct.pack(">HHHHHH", tid, 0x8005 | rd, 1, 0, 0, 0) + question, name, "REFUSED"
    if qtype != 1:  # only A records; empty NOERROR for AAAA etc.
        return struct.pack(">HHHHHH", tid, 0x8400 | rd, 1, 0, 0, 0) + question, name, "NODATA"
    rr = struct.pack(">HHHIH", 0xC00C, 1, 1, 60, 4) + socket.inet_aton(answer_ip)
    return struct.pack(">HHHHHH", tid, 0x8400 | rd, 1, 1, 0, 0) + question + rr, name, answer_ip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--answer", required=True, help="IPv4 to hand out for the game's names")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=53)
    ap.add_argument("--name", action="append", help="extra hostname to redirect")
    a = ap.parse_args()
    names = {n.lower() for n in DEFAULT_NAMES + (a.name or [])}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((a.bind, a.port))
    print(f"devdns: {sorted(names)} -> {a.answer} on {a.bind}:{a.port}", flush=True)
    while True:
        pkt, addr = s.recvfrom(512)
        try:
            out, name, what = reply(pkt, names, a.answer)
        except (IndexError, struct.error):
            continue
        s.sendto(out, addr)
        print(f"{addr[0]} {name} -> {what}", flush=True)


if __name__ == "__main__":
    main()
