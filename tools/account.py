"""Admin account tool: list, reset a password, rename, delete. Safe while the server runs.

The accounts live in $PSU_STATE_DIR/psu.db (SQLite, WAL), shared by the login and game services. Every change
here is one IMMEDIATE transaction through psu/db.py, so it is atomic against the running services (each side
waits up to 5 s for the other's write lock), and it writes one JSON line to <state>/account-audit.log (never a
password or a reset code). Run it on the server as the owner of the state files, never as root (a root-owned
psu.db-wal or -shm would lock the services out).

  python3 tools/account.py list [pattern]          accounts (SQL LIKE on login ID or character name, % = any)
  python3 tools/account.py reset <login id>        new one-time reset code, shown once
  python3 tools/account.py rename <login id> <new> new login ID; GID, characters, cards and mail stay
  python3 tools/account.py delete <login id> --yes the account and everything under its GID (backup first)
  python3 tools/account.py audit [n]               the last n audit lines (default 20)

Options: --state DIR (default $PSU_STATE_DIR, else $PSU_DATA/state, else data/state in the checkout), --by NAME
(audit; default $SUDO_USER or the login user), --force (delete while players are online).

Reset: the account's password becomes a 6-character code (letters and digits, no 0/O/1/I) and the old one stops
working at once. The player logs in with the code alone, or with the code immediately followed by a new
password (4+ characters), which then becomes the password and ends the reset (psu/db.py login()).
"""
import argparse
import getpass
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
from psu import db as dbm  # noqa: E402


def state_dir(arg):
    if arg:
        return arg
    if os.environ.get("PSU_STATE_DIR"):
        return os.environ["PSU_STATE_DIR"]
    if os.environ.get("PSU_DATA"):
        return os.path.join(os.environ["PSU_DATA"], "state")
    return os.path.join(HERE, "..", "data", "state")


def open_db(path):
    f = os.path.join(path, "psu.db")
    if not os.path.exists(f):
        sys.exit(f"no {f} (pass --state DIR; this tool never creates a database)")
    if hasattr(os, "geteuid"):
        owner = os.stat(f).st_uid
        if os.geteuid() != owner:
            sys.exit(f"run as the owner of {f} (uid {owner}), e.g. sudo -u \"#{owner}\"; "
                     "files SQLite creates as another user lock the services out")
    return dbm.DB(path)


def when(t):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)) if t else "-"


def online(path):
    """Players online per the game's status.json (written every 30 s), or None if it is not there."""
    logs = os.environ.get("PSU_LOG_DIR") or os.path.join(path, "..", "logs")
    try:
        with open(os.path.join(logs, "status.json"), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    return st.get("online", 0)


def backup(db, path, tag):
    out_dir = os.path.join(path, "..", "backups")
    os.makedirs(out_dir, exist_ok=True)
    safe = "".join(ch if ch.isalnum() else "_" for ch in tag)
    out = os.path.join(out_dir, time.strftime("pre-delete-%Y%m%d-%H%M%S-") + safe + ".db")
    dst = sqlite3.connect(out)
    with dst:
        db.c.backup(dst)
    dst.close()
    return os.path.normpath(out)


def last_audit(path):
    try:
        with open(os.path.join(path, dbm.AUDIT_LOG), encoding="utf-8") as f:
            return f.readlines()[-1].rstrip()
    except (OSError, IndexError):
        return "(audit log not written)"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--state")
    ap.add_argument("--by", default=os.environ.get("SUDO_USER") or getpass.getuser())
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("cmd", choices=["list", "reset", "rename", "delete", "audit"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    path = state_dir(a.state)
    need = {"list": (0, 1), "reset": (1, 1), "rename": (2, 2), "delete": (1, 1), "audit": (0, 1)}[a.cmd]
    if not need[0] <= len(a.args) <= need[1]:
        sys.exit(__doc__)

    if a.cmd == "audit":
        n = int(a.args[0]) if a.args else 20
        try:
            with open(os.path.join(path, dbm.AUDIT_LOG), encoding="utf-8") as f:
                sys.stdout.writelines(f.readlines()[-n:])
        except OSError:
            print("no audit log yet")
        return

    db = open_db(path)
    try:
        if a.cmd == "list":
            rows = db.accounts(a.args[0] if a.args else None)
            print(f"{'login id':24} {'gid':10}  {'created':16}  {'last login':16}  flags  characters")
            for user, gid, created, last, chars, ban, reset in rows:
                flags = ("B" if ban and ban > time.time() else "-") + ("R" if reset else "-")
                print(f"{user:24} {gid:#010x}  {when(created):16}  {when(last):16}  {flags:5}  {chars or ''}")
            print(f"{len(rows)} account(s). Flags: B banned, R reset code not yet replaced by the player.")
        elif a.cmd == "reset":
            user, gid, code = db.reset_password(a.args[0], by=a.by)
            print(f"{user} ({gid:#010x}): reset code  {code}")
            print("Give it to the player privately. It is not stored or logged and cannot be shown again.")
            print(f"They log in with {code} as the password, or with {code} followed directly by a new password")
            print(f"(at least {dbm.MIN_PASSWORD} characters, e.g. {code}mypass), which then becomes their password.")
            print("audit:", last_audit(path))
        elif a.cmd == "rename":
            old, gid = db.rename_account(a.args[0], a.args[1], by=a.by)
            print(f"{old} ({gid:#010x}) is now {a.args[1]}; the password is unchanged.")
            print("audit:", last_audit(path))
        elif a.cmd == "delete":
            row = db.account(a.args[0])
            if row is None:
                raise KeyError(a.args[0])
            if not a.yes:
                sys.exit(f"this deletes {row[0]} ({row[1]:#010x}) and its characters, cards, mail and blacklist; "
                         "add --yes")
            n_on = online(path)
            if n_on and not a.force:
                sys.exit(f"{n_on} player(s) online (status.json): kick the player first (!kick), then rerun "
                         "with --force, or the game may write the character back")
            print("backup:", backup(db, path, row[0]))
            user, gid, n = db.delete_account(row[0], by=a.by)
            print(f"deleted {user} ({gid:#010x}):", ", ".join(f"{t} {k}" for t, k in n.items() if k))
            print("audit:", last_audit(path))
    except KeyError as e:
        sys.exit(f"no account {e.args[0]!r} (account.py list shows them)")
    except ValueError as e:
        sys.exit(str(e))
    except sqlite3.OperationalError as e:
        sys.exit(f"database busy or locked ({e}); nothing was changed, try again")


if __name__ == "__main__":
    main()
