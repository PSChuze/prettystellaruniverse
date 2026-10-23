"""gamelink.py: the loopback account API the OpenLobby website (its regapi service) calls to link a
game login to a PlayOnline account and to reset its password from the website.

Same two locks as regapi: the peer must be loopback (host networking; regapi runs on this host), and
X-Signature = HMAC-SHA256(GAMELINK_SECRET, X-Timestamp + "\\n" + body), with the timestamp inside
GAMELINK_SKEW seconds. No secret = the API does not start. Requests and replies are JSON; passwords
are never logged.

  POST /v1/verify        {"account", "password"} -> {"ok": true, "account", "key"}
                                                   | {"ok": false, "error": "no_account"|"bad_password"}
  POST /v1/set-password  {"account", "password"} -> {"ok": true} | {"ok": false, "error": ...}

`account` is the name the player types in the game; the reply's `account` is the stored spelling and
`key` the game's own stable id for it (PSU: the GID, as 8 hex digits). regapi keeps the link by key.

pol_auth() is the other direction: the game's own login asks regapi whether what the player typed is
an OpenLobby website login (regapi /v1/game-auth, signed with the same secret).
"""
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ.get("GAMELINK_SECRET", "").encode()
SKEW = int(os.environ.get("GAMELINK_SKEW", "60"))
ALLOW = {"127.0.0.1", "::1"}
MAX_BODY = 4096


def signature(secret, ts, body):
    return hmac.new(secret, ts.encode() + b"\n" + body, hashlib.sha256).hexdigest()


def serve(routes, port, log=print):
    """Start the API on 127.0.0.1:port in a daemon thread. routes = {"/v1/x": fn(req) -> dict}.
    Returns the server, or None when GAMELINK_SECRET is unset."""
    if len(SECRET) < 32:
        log("gamelink: GAMELINK_SECRET unset or shorter than 32 characters, account API off")
        return None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, obj=None):
            out = json.dumps(obj or {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_POST(self):
            if self.client_address[0] not in ALLOW:
                return self._send(403)
            fn = routes.get(self.path)
            if fn is None:
                return self._send(404)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 < n <= MAX_BODY:
                return self._send(400)
            body = self.rfile.read(n)
            ts = self.headers.get("X-Timestamp") or ""
            try:
                fresh = abs(time.time() - int(ts)) <= SKEW
            except ValueError:
                fresh = False
            if not fresh or not hmac.compare_digest(signature(SECRET, ts, body),
                                                    self.headers.get("X-Signature") or ""):
                log("gamelink: refused %s %s: bad signature or stale timestamp"
                    % (self.client_address[0], self.path))
                return self._send(401)
            try:
                req = json.loads(body)
            except ValueError:
                return self._send(400)
            if not isinstance(req, dict):
                return self._send(400)
            res = fn(req)
            log("gamelink: %s %s -> %s" % (self.path, req.get("account", ""),
                                          "ok" if res.get("ok") else res.get("error")))
            return self._send(200, res)

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("gamelink: account API on 127.0.0.1:%d" % port)
    return srv


# ---- the other direction: a game login that is an OpenLobby login ---------------------------------
REGAPI_URL = os.environ.get("GAMELINK_REGAPI", "http://127.0.0.1:8650")
#: The website's username rule (regapi USERNAME_RE): anything else is never an OpenLobby login,
#: so it is not worth a request.
_WEB_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,19}$")


def pol_auth(game, username, password, account=None, key=None, timeout=8):
    """Ask regapi (/v1/game-auth) whether `username` / `password`, as typed into the game, are an
    OpenLobby website login. A dict {"ok": True, "linked": [{"key", "account"}, ...], ...} when they
    are; {"ok": False, ...} when they are not; None when it could not be asked (no secret, regapi
    down). With `account` + `key`, regapi first links that login (just made for the member)."""
    if len(SECRET) < 32 or not password or not _WEB_USERNAME.match(username or ""):
        return None
    fields = {"game": game, "username": username, "password": password}
    if account and key:
        fields.update(account=account, key=key)
    body = json.dumps(fields).encode()
    ts = str(int(time.time()))
    req = urllib.request.Request(REGAPI_URL + "/v1/game-auth", data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": signature(SECRET, ts, body)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            reply = json.load(r)
    except (OSError, ValueError):
        return None
    return reply if isinstance(reply, dict) else None
