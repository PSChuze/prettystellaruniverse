"""Pairing with an OpenLobby admin panel: setup.py against a stand-in panel and the game's real
admin port (server/psu/olport.py), and the update state the panel shows.

The stand-in also runs alone, for an end-to-end test with the server in Docker:
  python tests/test_setup_panel.py --serve [--port 8090] [--code ABCD2345] [--rewrite psu:22191=host:port]

It speaks the panel's side of the pairing contract and nothing else:

  POST /api/pair         {"code", "key", "title", "url", "token", "updater" (optional)}
      -> 200 {"ok": true, "title", "pairing"}   the code was right; the panel probed
                                               <url>/openlobby/module.json with X-Devtool-Token
                                               and the module answered with this key
      -> 400 {"error"}                         bad shape, the key is wired in the panel's own
                                               settings, or the probe failed (the code stays good)
      -> 403 {"error"}                         wrong, used or expired code
      -> 429 {"error"}                         more than 5 tries a minute from one address
  POST /api/pair/update  {"key", "pairing", "token", "url" (optional), "updater" (optional)}
      -> 200 {"ok": true}; 403 wrong pairing; 404 unknown key
  "updater" is {"folder", "command" (argv starting with python or python3, at most 16 items
  under 200 characters), "timeout_minutes" (5..240)}: a bad one is a 400; a new one waits as
  "requested" until the owner allows it.
  GET  /paired           what it holds (for tests)

--rewrite maps the module's host:port to one this process can reach (a test outside Docker).
"""
import argparse
import io
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "server")]

TRIES, WINDOW = 5, 60.0             # every try counts, per client address (the panel's Limiter)


def updater_ok(block):
    """The panel's rules for an updater block (absent is fine)."""
    if block is None:
        return True
    command = block.get("command") if isinstance(block, dict) else None
    return (isinstance(block, dict) and isinstance(block.get("folder"), str) and bool(block["folder"])
            and isinstance(command, list) and 1 <= len(command) <= 16 and command[0] in ("python", "python3")
            and all(isinstance(a, str) and len(a) < 200 for a in command)
            and isinstance(block.get("timeout_minutes"), int) and 5 <= block["timeout_minutes"] <= 240)


class Panel:
    """The answers the panel gives (OpenLobby services/adminpair.py), with the same error texts."""

    def __init__(self, codes, rewrite=None, env_keys=()):
        self.codes = set(codes)             # waiting codes; a used one moves to self.used
        self.used = set()
        self.rewrite = dict(rewrite or {})
        self.env_keys = set(env_keys)       # keys the panel's own settings wire (POL_ADMIN_GAME_<KEY>_URL)
        self.paired = {}                    # key -> {"title", "url", "token", "pairing", "updater"}
        self.hits = {}
        self.lock = threading.Lock()

    def allow(self, addr):
        now = time.monotonic()
        with self.lock:
            q = [t for t in self.hits.get(addr, []) if t > now - WINDOW]
            if len(q) >= TRIES:
                self.hits[addr] = q
                return False, int(q[0] + WINDOW - now) + 1
            self.hits[addr] = q + [now]
            return True, 0

    def probe(self, url, token, key):
        """(title, None) when <url>/openlobby/module.json answers this key with the token, else (None, why)."""
        parts = urllib.parse.urlsplit(url)
        netloc = self.rewrite.get(parts.netloc, parts.netloc)
        target = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip("/") + "/openlobby/module.json",
                                          "", ""))
        try:
            req = urllib.request.Request(target, headers={"X-Devtool-Token": token})
            with urllib.request.urlopen(req, timeout=5) as r:
                doc = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return None, "The game at %s refused that token." % url
            return None, "%s/openlobby/module.json answered HTTP %d." % (url, e.code)
        except (OSError, ValueError):
            return None, "Nothing answered at %s." % url
        if not isinstance(doc, dict) or doc.get("key") != key:
            return None, "The game at %s answered, but its key is not %s." % (url, key)
        return doc.get("title") or key, None

    def pair(self, body):
        if not isinstance(body, dict) or not body:
            return 400, {"error": "Send JSON: code, key, title, url and token."}
        code = str(body.get("code") or "").replace(" ", "").replace("-", "").upper()
        if not code:
            return 400, {"error": "code is missing: the 8 characters the panel shows."}
        key, url, token = body.get("key"), body.get("url"), body.get("token")
        if not all(isinstance(v, str) and v for v in (key, url, token, body.get("title"))):
            return 400, {"error": "key, title, url and token are required."}
        with self.lock:
            if code in self.used:
                return 403, {"error": "That code was already used. Make a new one in the panel."}
            if code not in self.codes:
                return 403, {"error": "That code is wrong. Check it against the one the panel shows."}
        if key in self.env_keys:
            return 400, {"error": "This panel's settings already point %s at a game (POL_ADMIN_GAME_%s_URL). "
                                  "Take that out first." % (key, key.upper())}
        if not updater_ok(body.get("updater")):
            return 400, {"error": "updater: a folder, a python command and a timeout of 5 to 240 minutes."}
        title, why = self.probe(url, token, key)
        if why:
            return 400, {"error": why}          # the code stays good
        with self.lock:
            if code not in self.codes:
                return 403, {"error": "That code was already used. Make a new one in the panel."}
            self.codes.discard(code)
            self.used.add(code)
            pairing = secrets.token_urlsafe(32)
            self.paired[key] = {"title": title, "url": url, "token": token, "pairing": pairing,
                                "updater": dict(body["updater"], state="requested") if body.get("updater") else None}
        return 200, {"ok": True, "title": title, "pairing": pairing,
                     "updater": {"state": "requested"} if body.get("updater") else None}

    def update(self, body):
        if not isinstance(body, dict) or not body:
            return 400, {"error": "Send JSON: key, pairing and token (and url, when it changed)."}
        key = body.get("key")
        with self.lock:
            known = self.paired.get(key)
            if not known:
                return 404, {"error": "This panel has no game with the key %s." % key}
            if body.get("pairing") != known["pairing"]:
                return 403, {"error": "That pairing secret is not this panel's for %s. Pair again." % key}
            if not updater_ok(body.get("updater")):
                return 400, {"error": "updater: a folder, a python command and a timeout of 5 to 240 minutes."}
            known["token"] = body.get("token") or known["token"]
            known["url"] = body.get("url") or known["url"]
            if body.get("updater"):
                old = {k: v for k, v in (known.get("updater") or {}).items() if k != "state"}
                if old != body["updater"]:
                    known["updater"] = dict(body["updater"], state="requested")
        return 200, {"ok": True, "updater": {"state": (known.get("updater") or {}).get("state")}}

    def serve(self, port, bind="127.0.0.1"):
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, status, doc):
                data = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/paired":
                    return self.send(200, panel.paired)
                self.send(404, {"error": "not found"})

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except ValueError:
                    return self.send(400, {"error": "not JSON"})
                if not isinstance(body, dict):
                    return self.send(400, {"error": "not an object"})
                ok, wait = panel.allow(self.client_address[0])
                if not ok:
                    return self.send(429, {"error": "Too many tries from this address. Try again in %d seconds."
                                                    % wait})
                if self.path == "/api/pair":
                    return self.send(*panel.pair(body))
                if self.path == "/api/pair/update":
                    return self.send(*panel.update(body))
                self.send(404, {"error": "not found"})

        server = ThreadingHTTPServer((bind, port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


def serve_main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--code", action="append", default=[])
    ap.add_argument("--rewrite", action="append", default=[], help="host:port=host:port")
    args = ap.parse_args(argv)
    panel = Panel(args.code or ["ABCD2345"], dict(r.split("=", 1) for r in args.rewrite))
    server = panel.serve(args.port, args.bind)
    print("fake panel on %s:%d, codes %s" % (args.bind, args.port, ", ".join(sorted(panel.codes))), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


TOKEN = "t" * 32


def game_port():
    """The game's admin port as the server runs it (no world behind it: module.json is enough)."""
    from psu import olport
    port = olport.AdminPort("psu", "Phantasy Star Universe", token=TOKEN, bind="127.0.0.1", port=0)
    host, n = port.start()
    return port, "%s:%d" % (host, n)


class PanelCase(unittest.TestCase):
    def setUp(self):
        import setup
        self.setup = setup
        self.game, where = game_port()
        self.addCleanup(self.game.stop)
        self.panel = Panel(["ABCD2345"], {"psu:22191": where})
        server = self.panel.serve(0)
        self.addCleanup(server.shutdown)
        self.url = "http://127.0.0.1:%d" % server.server_address[1]


class Pairing(PanelCase):
    def test_pair_then_new_token(self):
        result, message, pairing = self.setup.pair(self.url, "ABCD2345", TOKEN)
        self.assertEqual(result, "paired", message)
        self.assertEqual(message, "Paired. Phantasy Star Universe shows in the panel now. " + self.setup.UPDATER_APPROVAL)
        held = self.panel.paired["psu"]
        self.assertEqual((held["url"], held["token"], held["pairing"]), ("http://psu:22191", TOKEN, pairing))
        self.assertEqual(held["updater"], {"folder": self.setup.ROOT, "command": ["python3", "setup.py", "--update",
                                                                                 "--yes"],
                                           "timeout_minutes": 180, "state": "requested"})
        self.assertEqual(self.setup.pair_update(self.url, pairing, "n" * 32), (True, "The panel has the new token."))
        self.assertEqual(self.panel.paired["psu"]["token"], "n" * 32)
        ok, message = self.setup.pair_update(self.url, "wrong", "x" * 32)
        self.assertFalse(ok)
        self.assertIn("That pairing secret is not this panel's for psu. Pair again.", message)

    def test_used_and_wrong_codes(self):
        self.assertEqual(self.setup.pair(self.url, "ABCD2345", TOKEN)[0], "paired")
        self.assertEqual(self.setup.pair(self.url, "ABCD2345", TOKEN)[:2],
                         ("new code", "That code was already used. Make a new one in the panel."))
        self.assertEqual(self.setup.pair(self.url, "ZZZZ9999", TOKEN)[:2],
                         ("new code", "That code is wrong. Check it against the one the panel shows."))
        for _ in range(2):
            self.setup.pair(self.url, "ZZZZ9999", TOKEN)
        self.assertEqual(self.setup.pair(self.url, "ZZZZ9999", TOKEN)[:2],
                         ("retry", "Too many tries. Wait a minute, then type the code again."))

    def test_a_failed_probe_keeps_the_code(self):
        result, message, _ = self.setup.pair(self.url, "ABCD2345", "not the token")
        self.assertEqual(result, "retry")
        self.assertIn("The game at http://psu:22191 refused that token.", message)
        self.assertIn("type the same code again", message)
        self.assertEqual(self.setup.pair(self.url, "ABCD2345", TOKEN)[0], "paired")

    def test_panel_settings_already_wire_the_game(self):
        self.panel.env_keys.add("psu")
        self.assertEqual(self.setup.pair(self.url, "ABCD2345", TOKEN)[:2], ("stop", (
            "This panel already manages Phantasy Star Universe through its own settings "
            "(POL_ADMIN_GAME_PSU_URL). Nothing to pair.")))

    def test_unknown_key_and_no_panel(self):
        self.assertIn("This panel has no game with the key psu.", self.setup.pair_update(self.url, "p", TOKEN)[1])
        result, message, _ = self.setup.pair("http://127.0.0.1:9", "ABCD2345", TOKEN)
        self.assertEqual(result, "stop")
        self.assertIn("could not reach the panel", message)


class PairFlow(PanelCase):
    """The question setup asks when it sees a panel, through to the saved pairing."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        old = self.setup.ROOT
        self.setup.use_root(self.dir)
        self.addCleanup(self.setup.use_root, old)
        shutil.copy(os.path.join(HERE, "..", "psu.env.example"), self.setup.ENV_EXAMPLE)
        self.setup.write_env({"PSU_PANEL_TOKEN": TOKEN})

    def flow(self, *answers):
        out, left = io.StringIO(), list(answers)

        def typed(prompt):
            out.write(prompt + "\n")
            return left.pop(0)

        with mock.patch.object(self.setup, "server_running", return_value=True), \
                mock.patch("builtins.input", typed), mock.patch("sys.stdout", out):
            result = self.setup.pair_flow(self.url)
        return result, out.getvalue()

    def test_yes_and_a_code(self):
        result, out = self.flow("", "zzzz", "abcd-2345")
        self.assertTrue(result)
        self.assertIn("An OpenLobby admin panel is running here. Manage Phantasy Star Universe from it?", out)
        self.assertIn("Settings > Modules > Pair with a setup program", out)
        self.assertIn("The code has 8 letters and digits.", out)
        self.assertEqual(self.setup.read_env()["OPENLOBBY_PAIRING"], self.panel.paired["psu"]["pairing"])

    def test_no(self):
        result, out = self.flow("n")
        self.assertFalse(result)
        self.assertIn("pair later", out)
        self.assertEqual(self.setup.read_env().get("OPENLOBBY_PAIRING"), "")

    def test_new_token_reaches_the_panel(self):
        self.assertTrue(self.flow("y", "ABCD2345")[0])
        with mock.patch.object(self.setup, "server_running", return_value=False), \
                mock.patch.object(self.setup, "find_panel", return_value=self.url), \
                mock.patch("builtins.input", side_effect=["y"]), mock.patch("sys.stdout", io.StringIO()):
            self.setup.new_panel_token()
        token = self.setup.read_env()["PSU_PANEL_TOKEN"]
        self.assertNotEqual(token, TOKEN)
        self.assertEqual(self.panel.paired["psu"]["token"], token)

    def test_retry_after_a_failed_check(self):
        out = io.StringIO()
        tokens = iter(["wrong", TOKEN])         # the host fixes the token between the two tries
        with mock.patch.object(self.setup, "server_running", return_value=True), \
                mock.patch("builtins.input", side_effect=["ABCD2345", "ABCD2345"]), mock.patch("sys.stdout", out), \
                mock.patch.object(self.setup, "read_env", side_effect=lambda: {"PSU_PANEL_TOKEN": next(tokens)}), \
                mock.patch.object(self.setup, "write_env") as saved:
            self.assertTrue(self.setup.pair_flow(self.url, ask_first=False))
        self.assertIn("refused that token", out.getvalue())
        saved.assert_called_once_with({"OPENLOBBY_PAIRING": self.panel.paired["psu"]["pairing"],
                                       "OPENLOBBY_UPDATER": self.setup.updater_key(self.setup.updater_block())})
        self.assertIn("allows its one-click Update once, under Settings > Modules", out.getvalue())
        self.assertEqual(self.setup.updater_note({"ok": True, "pairing": "p"}), self.setup.NO_UPDATER)

    def test_a_panel_without_the_updater_gets_it_once_it_learns_it(self):
        real = {"pair": self.panel.pair, "update": self.panel.update}

        def older(name):                            # a panel from before the one-click Update
            def answer(body):
                status, doc = real[name](dict(body, updater=None))
                return status, {k: v for k, v in doc.items() if k != "updater"}
            return answer
        self.panel.pair, self.panel.update = older("pair"), older("update")
        out = io.StringIO()
        self.assertTrue(self.flow("y", "ABCD2345")[0])
        self.assertEqual(self.setup.read_env().get("OPENLOBBY_UPDATER"), "")
        self.panel.pair, self.panel.update = real["pair"], real["update"]       # the panel is updated
        with mock.patch.object(self.setup, "find_panel", return_value=self.url), mock.patch("sys.stdout", out):
            self.setup.check_updater()
        self.assertEqual(self.panel.paired["psu"]["updater"]["state"], "requested")
        self.assertEqual(self.setup.read_env().get("OPENLOBBY_UPDATER"),
                         self.setup.updater_key(self.setup.updater_block()))
        self.assertIn("allows its one-click Update once", out.getvalue())

    def test_a_moved_folder_is_sent_again(self):
        self.assertTrue(self.flow("y", "ABCD2345")[0])
        self.panel.paired["psu"]["updater"]["state"] = "approved"
        out = io.StringIO()
        with mock.patch.object(self.setup, "find_panel", return_value=self.url), mock.patch("sys.stdout", out):
            self.setup.check_updater()                      # nothing changed: nothing sent
            self.assertEqual(self.panel.paired["psu"]["updater"]["state"], "approved")
            self.setup.use_root(os.path.join(self.dir, "moved"))
            os.makedirs(self.setup.ROOT)
            shutil.copy(os.path.join(self.dir, ".env"), self.setup.ENV)
            self.setup.check_updater()
        held = self.panel.paired["psu"]["updater"]
        self.assertEqual((held["folder"], held["state"]), (self.setup.ROOT, "requested"))
        self.assertIn("told the OpenLobby panel where this server is now. The panel's owner allows", out.getvalue())


    def test_found_by_label(self):
        ps = mock.Mock(stdout="olpanel-admin-1\tolpanel\thttp://127.0.0.1:8090/\n", returncode=0)
        with mock.patch.object(self.setup, "run", return_value=ps) as run:
            self.assertEqual(self.setup.find_panel(), "http://127.0.0.1:8090")
        self.assertEqual(run.call_args[0][0][:4], ["docker", "ps", "--filter", "label=org.openlobby.role=panel"])
        with mock.patch.object(self.setup, "run", return_value=mock.Mock(stdout="", returncode=0)):
            self.assertEqual(self.setup.find_panel(), "")


class UpdaterBlock(unittest.TestCase):
    def test_shape_keeps_the_panels_rules(self):
        import setup
        block = setup.updater_block("/srv/PSU Server")
        self.assertEqual(sorted(block), ["command", "folder", "timeout_minutes"])
        self.assertTrue(setup.updater_valid(block) and updater_ok(block))
        self.assertEqual(block["command"][0], "python3")
        self.assertLessEqual(len(block["command"]), 16)
        self.assertTrue(5 <= block["timeout_minutes"] <= 240)
        for bad in (dict(block, command=["sh", "-c", "x"]), dict(block, timeout_minutes=300),
                    dict(block, command=["python3"] + ["x"] * 16), dict(block, folder="")):
            self.assertFalse(setup.updater_valid(bad))
            self.assertFalse(updater_ok(bad))


class UpdateState(unittest.TestCase):
    RUNNING = {"commit": "a" * 40, "built_at": "2025-05-01T10:00:00Z"}
    LATEST = {"tag": "v1.1.0", "zip": "", "commit": "b" * 40, "date": "2025-05-03T09:00:00Z"}

    def setUp(self):
        import setup
        self.setup = setup

    def test_available(self):
        changes = [{"commit": "b" * 40, "date": "2025-05-03T09:00:00Z", "subject": "game: fix a shop"}]
        doc = self.setup.update_state(self.RUNNING, self.LATEST, 1, changes)
        self.assertEqual(doc, {"running": self.RUNNING,
                               "latest": {"commit": "b" * 40, "date": "2025-05-03T09:00:00Z"},
                               "behind": 1, "changes": changes, "update": "available",
                               "how": "Run PSU Server Setup and choose Update."})

    def test_current_and_unknown(self):
        same = dict(self.LATEST, commit="a" * 40)
        self.assertEqual(self.setup.update_state(self.RUNNING, same, 0, [])["update"], "current")
        self.assertEqual(self.setup.update_state(self.RUNNING, None, None, [])["update"], "unknown")
        self.assertEqual(self.setup.update_state({"commit": ""}, self.LATEST, None, [])["update"], "unknown")

    def test_from_github(self):
        repo = self.setup.REPO
        answers = {
            "/repos/%s/releases/latest" % repo: {"tag_name": "v1.1.0"},
            "/repos/%s/commits/v1.1.0" % repo: {"sha": "b" * 40,
                                                "commit": {"committer": {"date": "2025-05-03T09:00:00Z"}}},
            "/repos/%s/compare/%s...%s" % (repo, "a" * 40, "b" * 40): {"ahead_by": 2, "commits": [
                {"sha": "c" * 40, "commit": {"message": "extract: one\n\nbody", "committer": {"date": "d1"}}},
                {"sha": "e" * 40, "parents": [{}, {}], "commit": {"message": "Merge setup"}},
                {"sha": "b" * 40, "commit": {"message": "game: two", "committer": {"date": "d2"}}}]},
        }
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        old = self.setup.ROOT
        self.setup.use_root(d)
        self.addCleanup(self.setup.use_root, old)
        with open(self.setup.ENV, "w") as f:
            f.write("PSU_BUILD_COMMIT=%s\nPSU_BUILT_AT=2025-05-01T10:00:00Z\n" % ("a" * 40))
        with mock.patch.object(self.setup, "github", side_effect=lambda path: answers[path]):
            self.assertEqual(self.setup.latest_release()["zip"],
                             "https://github.com/%s/archive/%s.zip" % (repo, "b" * 40))
            doc = self.setup.refresh_update_state()
        self.assertEqual((doc["update"], doc["behind"]), ("available", 2))
        self.assertEqual([c["subject"] for c in doc["changes"]], ["game: two", "extract: one"])
        with open(self.setup.UPDATE_FILE) as f:
            self.assertEqual(json.load(f), doc)

    def test_offline(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        old = self.setup.ROOT
        self.setup.use_root(d)
        self.addCleanup(self.setup.use_root, old)
        with mock.patch.object(self.setup, "github", side_effect=OSError("offline")):
            doc = self.setup.refresh_update_state()
        self.assertEqual(doc["update"], "unknown")
        self.assertTrue(doc["why"])


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve_main(sys.argv[1:])
    else:
        unittest.main()
