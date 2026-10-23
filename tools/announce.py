#!/usr/bin/env python3
"""Admin CLI for the running game service: announcements, who / stats, kick / mute (psu/admin.py).

Talks to the loopback admin channel (127.0.0.1:$PSU_ADMIN_PORT, default 22190) with the token from
$PSU_ADMIN_TOKEN or <state dir>/admin.token (state dir: --state, $PSU_STATE_DIR, else $PSU_DATA/state).
Stdlib only, so it runs on the host next to the container (host networking).

  announce.py "Server restart in 10 minutes"                 ticker to everyone
  announce.py --uni 2 "Universe 2 event at 20:00"            one universe
  announce.py --area 1100000/0 "Colony lobby notice"         one lobby zone (any universe)
  announce.py --player Alice --dialog 8 "Please read mail"   timed dialog to one player
  announce.py --kind center "..."                            centre dialog (ticker | right | dialog | center)
  announce.py who | stats | news
  announce.py events                                         the event calendar: what runs when, what is skipped
  announce.py spawns <name>
  announce.py kick <name> [reason...]
  announce.py mute <name> [minutes] [reason...]   (0 minutes = until unmuted)
  announce.py unmute <name>
  announce.py season                                         the lobby season: what runs, what is staged
  announce.py season <name> <season | none | off>           preview a season for one character

Exit status 0 when the server answered ok, 1 otherwise. Every command is audited server-side (events.jsonl,
ev "gm_action", via "cli", by "cli:<--by or $USER>").
"""
import argparse
import getpass
import json
import os
import socket
import sys
import time

COMMANDS = ("who", "stats", "news", "events", "season", "spawns", "kick", "mute", "unmute")


def state_dir(arg):
    if arg:
        return arg
    if os.environ.get("PSU_STATE_DIR"):
        return os.environ["PSU_STATE_DIR"]
    if os.environ.get("PSU_DATA"):
        return os.path.join(os.environ["PSU_DATA"], "state")
    return os.path.join(os.getcwd(), "state")


def token(args):
    tok = os.environ.get("PSU_ADMIN_TOKEN", "").strip()
    if tok:
        return tok
    path = args.token_file or os.path.join(state_dir(args.state), "admin.token")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        sys.exit(f"no admin token: {e} (set PSU_ADMIN_TOKEN, --token-file or --state)")


def call(args, req):
    req = dict(req, token=token(args), by=args.by)
    with socket.create_connection(("127.0.0.1", args.port), timeout=10) as s:
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf or b'{"ok": false, "error": "no reply"}')


def build(args):
    words = args.words
    if words and words[0] in COMMANDS:
        cmd, rest = words[0], words[1:]
        if cmd in ("who", "stats", "news", "events") or (cmd == "season" and not rest):
            return {"cmd": cmd}
        if cmd == "season":
            if len(rest) != 2:
                sys.exit("season <name> <season | none | off>")
            return {"cmd": cmd, "name": rest[0], "season": rest[1]}
        if not rest:
            sys.exit(f"{cmd} needs a player name")
        req = {"cmd": cmd, "name": rest[0]}
        if cmd == "kick":
            req["reason"] = " ".join(rest[1:])
        elif cmd == "mute":
            req["minutes"] = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 60
            req["reason"] = " ".join(rest[2 if len(rest) > 1 and rest[1].isdigit() else 1:])
        return req
    text = " ".join(words).strip()
    if not text:
        sys.exit("nothing to announce")
    scope = "all"
    if args.uni is not None:
        scope = f"uni:{args.uni}"
    elif args.area:
        scope = f"area:{args.area}"
    elif args.player:
        scope = f"player:{args.player}"
    kind = "dialog" if args.dialog is not None else args.kind
    return {"cmd": "announce", "text": text, "scope": scope, "kind": kind,
            "seconds": args.dialog if args.dialog is not None else 0}


def show(reply):
    if not reply.get("ok"):
        print("error:", reply.get("error"), file=sys.stderr)
        return 1
    if "players" in reply:
        for p in reply["players"]:
            area = "/".join(str(x) for x in p["area"]) if p["area"] else "-"
            print(f"{p['name']:<16} {p['account']:<16} {p['game']:<4} U{p['uni']} {area}"
                  + (" party" if p["party"] else "") + (" muted" if p["muted"] else ""))
        print(f"{len(reply['players'])} online")
    elif "line" in reply:
        print(reply["line"])
    elif "events_file" in reply:
        print(reply["events_file"])
        for e in reply["events"]:
            print(("RUNNING " if e["running"] else "        ") + time.strftime("%Y-%m-%d %H:%M", time.gmtime(e["start"]))
                  + " .. " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(e["end"])) + f"  {e['key']}: {e['name']}")
        if not reply["events"]:
            print("nothing running or in the next 30 days")
        for p in reply["problems"]:
            print("SKIPPED", p)
        return 1 if reply["problems"] else 0
    elif "notices" in reply:
        for i, n in enumerate(reply["notices"]):
            print(f"[{i}] {n}")
        if not reply["notices"]:
            print("no notices (news file missing or empty)")
    else:
        print(json.dumps({k: v for k, v in reply.items() if k != "ok"}))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("words", nargs="*", help="announcement text, or a command: " + " ".join(COMMANDS))
    ap.add_argument("--uni", type=int)
    ap.add_argument("--area", help="QUEST/ZONE, e.g. 1100000/0")
    ap.add_argument("--player")
    ap.add_argument("--kind", default="ticker", choices=("ticker", "right", "dialog", "center"))
    ap.add_argument("--dialog", type=int, metavar="SECS", help="timed dialog shown SECS seconds")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PSU_ADMIN_PORT", "22190")))
    ap.add_argument("--state", help="state dir holding admin.token")
    ap.add_argument("--token-file")
    ap.add_argument("--by", default=os.environ.get("USER") or getpass.getuser())
    args = ap.parse_args()
    try:
        return show(call(args, build(args)))
    except OSError as e:
        print(f"cannot reach the game service on 127.0.0.1:{args.port}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
