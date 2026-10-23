"""Server storage: one SQLite file shared by the login and game services ($PSU_STATE_DIR/psu.db, WAL).

accounts    username -> GID, PBKDF2-SHA256 password hash (+ salt). Passwords are never stored or logged.
account_resets  accounts whose password is an admin-issued reset code (tools/account.py reset); while the
            row is there, a login with the code followed by a new password sets that password (login())
login_keys  the 4-byte key login.py issues and game.py checks on 0x020D (single use, short lived), with the
            game the client logged in from (psu or aoti, by login port)
characters  per GID and slot: the client's own 0x0D02 body (looks, name, class) + a JSON progression
            state (level, EXP, meseta, inventory, ...)
cards, mail, blacklist   the social data (psu/social.py), one row per entry
clears      one row per mission clear: disc, universe, quest, clear time, rank, score, kills, and the party as
            [[gid, slot, name], ...] (the characters in the mission when it cleared); the boards read it (psu/board.py)

The first open imports the older per-account JSON files under $PSU_STATE_DIR/{chars,cards,mail,blacklist}.
"""
import glob
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time

PBKDF2_ROUNDS = 200_000
RESET_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"   # no 0/O, 1/I: read out to a player
RESET_LEN = 6                         # short so the code + a new password fit the client's password box
MIN_PASSWORD = 4
NAME_MAX = 23                         # 0x0219 username field is 24 bytes
AUDIT_LOG = "account-audit.log"       # JSON lines in the state dir: every account change, never a password
KEY_TTL = 120                         # seconds between the login answer and the game server connect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    username TEXT PRIMARY KEY, gid INTEGER UNIQUE NOT NULL, salt BLOB NOT NULL, pw_hash BLOB NOT NULL,
    created REAL NOT NULL, last_login REAL);
CREATE TABLE IF NOT EXISTS login_keys (gid INTEGER PRIMARY KEY, key BLOB NOT NULL, expires REAL NOT NULL,
    game TEXT NOT NULL DEFAULT 'psu', platform TEXT NOT NULL DEFAULT 'ps2');
CREATE TABLE IF NOT EXISTS characters (
    gid INTEGER NOT NULL, slot INTEGER NOT NULL, name TEXT, raw BLOB NOT NULL, state TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL, PRIMARY KEY (gid, slot));
CREATE TABLE IF NOT EXISTS cards (gid INTEGER NOT NULL, slot INTEGER NOT NULL, rec BLOB NOT NULL, PRIMARY KEY (gid, slot));
CREATE TABLE IF NOT EXISTS mail (id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER NOT NULL, rec BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS blacklist (gid INTEGER NOT NULL, who INTEGER NOT NULL, entry BLOB NOT NULL, PRIMARY KEY (gid, who));
CREATE TABLE IF NOT EXISTS account_resets (gid INTEGER PRIMARY KEY, created REAL NOT NULL, by TEXT);
CREATE TABLE IF NOT EXISTS bans (gid INTEGER PRIMARY KEY, until REAL NOT NULL, reason TEXT, by TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS clears (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL, game TEXT NOT NULL,
    uni INTEGER NOT NULL, quest INTEGER NOT NULL, secs INTEGER NOT NULL, rank INTEGER NOT NULL, score INTEGER NOT NULL,
    kills INTEGER NOT NULL, party TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS clears_quest ON clears (quest, secs);
CREATE TABLE IF NOT EXISTS help_threads (id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER NOT NULL, name TEXT NOT NULL,
    subject TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, closed INTEGER NOT NULL DEFAULT 0,
    waiting INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS help_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, thread INTEGER NOT NULL,
    sender TEXT NOT NULL, text TEXT NOT NULL, at REAL NOT NULL, by TEXT, delivered INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS help_messages_thread ON help_messages (thread, id);
"""


class DB:
    def __init__(self, state_dir):
        os.makedirs(state_dir, exist_ok=True)
        self.state_dir = state_dir
        self.c = sqlite3.connect(os.path.join(state_dir, "psu.db"), isolation_level=None)
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA busy_timeout=5000")
        # login_keys gained `game`, then `platform`; its rows live two minutes, so an old table is just replaced
        cols = [r[1] for r in self.c.execute("PRAGMA table_info(login_keys)")]
        if cols and not {"game", "platform"} <= set(cols):
            self.c.execute("DROP TABLE login_keys")
        self.c.executescript(_SCHEMA)
        self._import_json()

    # --- accounts ----------------------------------------------------------------------------------

    @staticmethod
    def _hash(password, salt):
        return hashlib.pbkdf2_hmac("sha256", password.encode("latin1"), salt, PBKDF2_ROUNDS)

    def login(self, username, password, gid_for, register=True):
        """(gid, None) on success, (None, reason) otherwise. New names register when `register`.

        After an admin reset (reset_password) the password is a RESET_LEN-character code. The code alone
        logs in; the code immediately followed by a new password (at least MIN_PASSWORD characters) logs in
        and makes that the password from then on (the client has no change-password screen)."""
        row = self.c.execute("SELECT gid, salt, pw_hash FROM accounts WHERE username = ?", (username,)).fetchone()
        if row is None:
            if not register:
                return None, "unknown account"
            gid = gid_for(username)
            while self.c.execute("SELECT 1 FROM accounts WHERE gid = ?", (gid,)).fetchone():
                gid = (gid + 1) & 0x7FFFFFFF or 1
            salt = secrets.token_bytes(16)
            self.c.execute("INSERT INTO accounts (username, gid, salt, pw_hash, created, last_login) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           (username, gid, salt, self._hash(password, salt), time.time(), time.time()))
            return gid, None
        gid, salt, pw_hash = row
        if not hmac.compare_digest(self._hash(password, salt), pw_hash):
            if not (len(password) >= RESET_LEN + MIN_PASSWORD and self._reset_pending(gid)
                    and hmac.compare_digest(self._hash(password[:RESET_LEN], salt), pw_hash)):
                return None, "wrong password"
            with self._txn():
                self._set_password(gid, password[RESET_LEN:])
            self.audit("password_set", username, gid, by="player: reset code + new password")
        self.c.execute("UPDATE accounts SET last_login = ? WHERE username = ?", (time.time(), username))
        return gid, None

    def verify(self, username, password):
        """(username, gid) when the password matches the account (exact name, else a unique
        case-insensitive match), (None, reason) otherwise. For the website's account link; no side
        effects (a pending reset code is not consumed here)."""
        row = self.account(username)
        if row is None:
            return None, "no_account"
        salt, pw_hash = self.c.execute("SELECT salt, pw_hash FROM accounts WHERE gid = ?",
                                       (row[1],)).fetchone()
        try:
            ok = hmac.compare_digest(self._hash(password, salt), pw_hash)
        except UnicodeEncodeError:
            ok = False
        return (row, None) if ok else (None, "bad_password")

    def set_password(self, username, password, by=""):
        """The website's reset: a new password for an existing account. Returns (username, gid)."""
        with self._txn():
            row = self.account(username)
            if row is None:
                raise KeyError(username)
            self._set_password(row[1], password)
            self.c.execute("DELETE FROM login_keys WHERE gid = ?", (row[1],))
        self.audit("password_set", row[0], row[1], by=by)
        return row

    def _reset_pending(self, gid):
        return self.c.execute("SELECT 1 FROM account_resets WHERE gid = ?", (gid,)).fetchone() is not None

    def _set_password(self, gid, password):
        """New salt + hash; clears a pending reset."""
        salt = secrets.token_bytes(16)
        self.c.execute("UPDATE accounts SET salt = ?, pw_hash = ? WHERE gid = ?", (salt, self._hash(password, salt), gid))
        self.c.execute("DELETE FROM account_resets WHERE gid = ?", (gid,))

    def audit(self, op, username, gid, **extra):
        """One JSON line in <state>/account-audit.log. Never pass a password or reset code."""
        line = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "op": op, "user": username,
                "gid": f"{gid:#010x}"}
        line.update(extra)
        try:
            with open(os.path.join(self.state_dir, AUDIT_LOG), "a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")
        except OSError:
            pass   # an unwritable audit file must not fail a login; account.py also prints each line

    # Admin functions (tools/account.py). Each runs in one IMMEDIATE transaction, atomic against the login
    # and game services writing the same file (WAL; whoever waits does so for up to busy_timeout).

    def _txn(self):
        c = self.c

        class Txn:
            def __enter__(self):
                c.execute("BEGIN IMMEDIATE")

            def __exit__(self, kind, *_):
                c.execute("COMMIT" if kind is None else "ROLLBACK")
        return Txn()

    def account(self, username):
        """(username, gid) by exact name, else by a unique case-insensitive match, else None."""
        row = self.c.execute("SELECT username, gid FROM accounts WHERE username = ?", (username,)).fetchone()
        if row is None:
            rows = self.c.execute("SELECT username, gid FROM accounts WHERE lower(username) = lower(?)",
                                  (username,)).fetchall()
            row = rows[0] if len(rows) == 1 else None
        return row

    def accounts(self, like=None):
        """For account.py list: (username, gid, created, last_login, character names, ban until, reset pending)."""
        q = ("SELECT a.username, a.gid, a.created, a.last_login, "
             "(SELECT group_concat(coalesce(c.name, '?'), ', ') FROM characters c WHERE c.gid = a.gid), "
             "(SELECT b.until FROM bans b WHERE b.gid = a.gid), "
             "(SELECT 1 FROM account_resets r WHERE r.gid = a.gid) FROM accounts a")
        args = ()
        if like:
            q += (" WHERE lower(a.username) LIKE lower(?)"
                  " OR a.gid IN (SELECT gid FROM characters WHERE lower(name) LIKE lower(?))")
            args = (like, like)
        return self.c.execute(q + " ORDER BY a.created", args).fetchall()

    def reset_password(self, username, by=""):
        """Replace the password with a fresh one-time code. Returns (username, gid, code); the code is
        shown to the admin once and never stored or logged."""
        code = "".join(secrets.choice(RESET_ALPHABET) for _ in range(RESET_LEN))
        with self._txn():
            row = self.account(username)
            if row is None:
                raise KeyError(username)
            self._set_password(row[1], code)
            self.c.execute("INSERT OR REPLACE INTO account_resets VALUES (?, ?, ?)", (row[1], time.time(), by))
            self.c.execute("DELETE FROM login_keys WHERE gid = ?", (row[1],))
        self.audit("password_reset", row[0], row[1], by=by)
        return row[0], row[1], code

    def rename_account(self, username, new, by=""):
        """New login ID for an account; its GID, characters and social data stay. Returns (old, gid)."""
        if not valid_name(new):
            raise ValueError(f"{new!r}: 1..{NAME_MAX} printable Latin-1 characters, no leading or trailing space")
        with self._txn():
            row = self.account(username)
            if row is None:
                raise KeyError(username)
            clash = self.c.execute("SELECT username FROM accounts WHERE lower(username) = lower(?) AND gid != ?",
                                   (new, row[1])).fetchone()
            if clash:
                raise ValueError(f"{new!r} clashes with the account {clash[0]!r}")
            self.c.execute("UPDATE accounts SET username = ? WHERE gid = ?", (new, row[1]))
        self.audit("rename", row[0], row[1], new=new, by=by)
        return row[0], row[1]

    def delete_account(self, username, by=""):
        """Removes the account and every row stored under its GID. Returns (username, gid, {table: rows})."""
        with self._txn():
            row = self.account(username)
            if row is None:
                raise KeyError(username)
            n = {}
            for table in ("characters", "cards", "mail", "blacklist", "bans", "login_keys", "account_resets",
                          "accounts"):
                n[table] = self.c.execute(f"DELETE FROM {table} WHERE gid = ?", (row[1],)).rowcount
        self.audit("delete", row[0], row[1], by=by, removed=n)
        return row[0], row[1], n

    def username(self, gid):
        row = self.c.execute("SELECT username FROM accounts WHERE gid = ?", (gid,)).fetchone()
        return row[0] if row else None

    def find(self, name):
        """gid for an account name or a character name (case-insensitive), or None."""
        row = self.c.execute("SELECT gid FROM accounts WHERE lower(username) = lower(?)", (name,)).fetchone()
        if row is None:
            row = self.c.execute("SELECT gid FROM characters WHERE lower(name) = lower(?)", (name,)).fetchone()
        return row[0] if row else None

    # --- moderation ----------------------------------------------------------------------------------

    def ban(self, gid, until, reason="", by=""):
        self.c.execute("INSERT OR REPLACE INTO bans VALUES (?, ?, ?, ?, ?)", (gid, until, reason, by, time.time()))

    def unban(self, gid):
        return self.c.execute("DELETE FROM bans WHERE gid = ?", (gid,)).rowcount > 0

    def banned(self, gid):
        """(until, reason) while a ban is in force, else None (expired bans are dropped)."""
        row = self.c.execute("SELECT until, reason FROM bans WHERE gid = ?", (gid,)).fetchone()
        if row is None:
            return None
        if row[0] < time.time():
            self.unban(gid)
            return None
        return row

    def issue_key(self, gid, game="psu", platform="ps2"):
        key = secrets.token_bytes(4)
        self.c.execute("INSERT OR REPLACE INTO login_keys VALUES (?, ?, ?, ?, ?)",
                       (gid, key, time.time() + KEY_TTL, game, platform))
        return key

    def take_key(self, gid, key):
        """(game "psu" / "aoti", platform "ps2" / "pc") once for the key login.py issued to gid (then it
        is gone), else None."""
        row = self.c.execute("SELECT key, expires, game, platform FROM login_keys WHERE gid = ?", (gid,)).fetchone()
        self.c.execute("DELETE FROM login_keys WHERE gid = ?", (gid,))
        if row and hmac.compare_digest(row[0], key) and row[1] >= time.time():
            return row[2], row[3]
        return None

    # --- characters --------------------------------------------------------------------------------

    def characters(self, gid):
        """{slot: raw 0x0D02 body}"""
        return {s: bytes(r) for s, r in self.c.execute("SELECT slot, raw FROM characters WHERE gid = ?", (gid,))}

    def save_character(self, gid, slot, name, raw):
        self.c.execute("INSERT INTO characters (gid, slot, name, raw, state, created) VALUES (?, ?, ?, ?, '{}', ?) "
                       "ON CONFLICT (gid, slot) DO UPDATE SET name = excluded.name, raw = excluded.raw, state = '{}'",
                       (gid, slot, name, raw, time.time()))

    def char_state(self, gid, slot):
        row = self.c.execute("SELECT state FROM characters WHERE gid = ? AND slot = ?", (gid, slot)).fetchone()
        return json.loads(row[0]) if row else {}

    def char_name(self, gid, slot):
        row = self.c.execute("SELECT name FROM characters WHERE gid = ? AND slot = ?", (gid, slot)).fetchone()
        return row[0] if row else None

    def save_char_state(self, gid, slot, state):
        self.c.execute("UPDATE characters SET state = ? WHERE gid = ? AND slot = ?", (json.dumps(state), gid, slot))

    def shop_rows(self):
        """[(gid, slot, name raw 0x40, state)] of the characters with a My Shop (psu/playershop.py search)."""
        rows = self.c.execute("SELECT gid, slot, raw, state FROM characters WHERE state LIKE '%\"shop\"%'").fetchall()
        return [(g, s, bytes(r)[0x28:0x68], json.loads(st)) for g, s, r, st in rows]

    # --- mission clears -----------------------------------------------------------------------------

    def record_clear(self, game, uni, quest, secs, rank, score, kills, party):
        """One clear; party = [(gid, slot, name), ...]."""
        self.c.execute("INSERT INTO clears (time, game, uni, quest, secs, rank, score, kills, party) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (time.time(), game, uni, quest, secs, rank, score, kills, json.dumps([list(m) for m in party])))

    # --- social ------------------------------------------------------------------------------------

    def cards(self, gid):
        return {s: bytes(r) for s, r in self.c.execute("SELECT slot, rec FROM cards WHERE gid = ?", (gid,))}

    def store_card(self, gid, slot, rec):
        if rec is None:
            self.c.execute("DELETE FROM cards WHERE gid = ? AND slot = ?", (gid, slot))
        else:
            self.c.execute("INSERT OR REPLACE INTO cards VALUES (?, ?, ?)", (gid, slot, rec))

    def mailbox(self, gid, limit=100):
        rows = self.c.execute("SELECT rec FROM mail WHERE gid = ? ORDER BY id DESC LIMIT ?", (gid, limit)).fetchall()
        return [bytes(r[0]) for r in reversed(rows)]

    def store_mail(self, gid, rec):
        self.c.execute("INSERT INTO mail (gid, rec) VALUES (?, ?)", (gid, rec))

    def update_mail(self, gid, match, update):
        for mid, rec in self.c.execute("SELECT id, rec FROM mail WHERE gid = ?", (gid,)).fetchall():
            rec = bytes(rec)
            if match(rec):
                self.c.execute("UPDATE mail SET rec = ? WHERE id = ?", (update(rec), mid))

    def blacklist(self, gid):
        return [bytes(r[0]) for r in self.c.execute("SELECT entry FROM blacklist WHERE gid = ? ORDER BY rowid", (gid,))]

    def set_blacklist(self, gid, who, entry):
        if entry is None:
            self.c.execute("DELETE FROM blacklist WHERE gid = ? AND who = ?", (gid, who))
        else:
            self.c.execute("INSERT OR REPLACE INTO blacklist VALUES (?, ?, ?)", (gid, who, entry))

    # --- one-time import of the older JSON files -----------------------------------------------------

    def _import_json(self):
        if self.c.execute("SELECT COUNT(*) FROM characters").fetchone()[0]:
            return
        for kind in ("chars", "cards", "mail", "blacklist"):
            for path in glob.glob(os.path.join(self.state_dir, kind, "*.json")):
                gid = int(os.path.basename(path)[:-5], 16)
                data = json.load(open(path, encoding="utf-8"))
                if kind == "chars":
                    for slot, ch in data.items():
                        self.c.execute("INSERT OR IGNORE INTO characters (gid, slot, name, raw, state, created) "
                                       "VALUES (?, ?, ?, ?, '{}', ?)",
                                       (gid, int(slot), ch.get("name"), bytes.fromhex(ch["raw_0d02"]), time.time()))
                elif kind == "cards":
                    for slot, rec in data.items():
                        self.store_card(gid, int(slot), bytes.fromhex(rec))
                elif kind == "mail":
                    for rec in data:
                        self.store_mail(gid, bytes.fromhex(rec))
                else:
                    for entry in data:
                        e = bytes.fromhex(entry)
                        self.set_blacklist(gid, int.from_bytes(e[:4], "little"), e)


def valid_name(name):
    try:
        name.encode("latin1")
    except UnicodeEncodeError:
        return False
    return 0 < len(name) <= NAME_MAX and name == name.strip() and name.isprintable()


_DB = None


def get(state_dir):
    global _DB
    if _DB is None:
        _DB = DB(state_dir)
    return _DB
