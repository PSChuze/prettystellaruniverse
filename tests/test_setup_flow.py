"""setup.py's first run and menu: what the three answers turn into, the Docker commands a first run
gives (against a stand-in docker), the ports players need, the server files of a release, the
progress display and the menu's items."""
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

import setup
from hostsetup import discs
from hostsetup import progress as proglib

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


class Settings(unittest.TestCase):
    def template(self):
        return setup.read_env(os.path.join(REPO, "psu.env.example"))

    def test_home_on_docker_desktop(self):
        c = setup.first_settings("home", "10.0.0.20", "Guardians", self.template(), linux=False)
        self.assertEqual(c["PSU_GAME_ADDR"], "10.0.0.20")
        self.assertEqual(c["PSU_MOTD"], "Welcome to Guardians.")
        self.assertRegex(c["PSU_PANEL_TOKEN"], r"^[A-Za-z0-9_-]{32}$")
        self.assertNotIn("PSU_DNS_BIND", c)
        self.assertNotIn("PSU_UID", c)

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX only")
    def test_linux_binds_dns_to_the_network_address(self):
        c = setup.first_settings("internet", "203.0.113.9", "x", {}, linux=True, lan="10.0.0.20")
        self.assertEqual(c["PSU_DNS_BIND"], "10.0.0.20")

    def test_token_stays(self):
        c = setup.first_settings("home", "10.0.0.20", "x", {"PSU_PANEL_TOKEN": "keep"}, linux=False)
        self.assertNotIn("PSU_PANEL_TOKEN", c)

    def test_containers_follow_the_project(self):
        with mock.patch.object(setup, "PROJECT", "psupub-setup"):
            c = setup.first_settings("home", "10.0.0.20", "x", {}, linux=False)
        self.assertEqual(c["PSU_PANEL_CONTAINERS"].split(",")[0], "psupub-setup-game-1")

    def test_ports(self):
        self.assertEqual(setup.ports_to_open({}), [(11110, "TCP"), (11140, "TCP"), (12110, "TCP"), (12140, "TCP"),
                                                   (12061, "TCP")])        # never the DNS
        with mock.patch.dict(os.environ, {"PSU_PORT_LOGIN_PSU": "30112"}):
            self.assertIn((30112, "TCP"), setup.ports_to_open({}))

    def test_who_plays(self):
        self.assertEqual(setup.who_plays({"PSU_GAME_ADDR": "10.0.0.5"}, {}), "home")
        self.assertEqual(setup.who_plays({"PSU_GAME_ADDR": "203.0.113.9"}, {}), "internet")
        self.assertEqual(setup.who_plays({"PSU_GAME_ADDR": "10.0.0.5"}, {"who": "internet"}), "internet")

    def test_addresses(self):
        self.assertEqual(setup.ipv4_of("203.0.113.9"), "203.0.113.9")
        self.assertEqual(setup.ipv4_of("localhost"), "127.0.0.1")
        with mock.patch("socket.gethostbyname", side_effect=OSError):
            self.assertEqual(setup.ipv4_of("nowhere.invalid"), "")

    def test_motd_fits_the_notice_box(self):
        self.assertLessEqual(len(setup.motd("x" * 80)), 50)


def answers(*lines):
    return mock.patch("builtins.input", side_effect=list(lines))


FOUND = [discs.Disc("/games/Phantasy Star Universe (USA).iso", "psu"),
         discs.Disc("/games/aoti/AotI.iso", "aoti")]


class Questions(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        patches = [mock.patch.object(setup, "lan_address", return_value="10.0.0.20"),
                   mock.patch.object(setup, "public_address", return_value="203.0.113.9"),
                   mock.patch.object(discs, "likely_folders", return_value=["/games"]),
                   mock.patch.object(discs, "search", return_value=FOUND),
                   mock.patch("sys.stdout", self.out)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        old = setup.ROOT
        setup.use_root(REPO)
        self.addCleanup(setup.use_root, old)

    def test_three_questions(self):
        with answers("2", "", "", "Guardians"):
            setup.first_run(dry_run=True)
        out = self.out.getvalue()
        self.assertIn("Question 1 of 3: Who will play on this server?", out)
        self.assertIn("Your internet address is 203.0.113.9.", out)
        self.assertIn("Phantasy Star Universe: /games/Phantasy Star Universe (USA).iso", out)
        self.assertIn("Ambition of the Illuminus: /games/aoti/AotI.iso", out)
        self.assertIn("  PSU_GAME_ADDR=203.0.113.9\n", out)
        self.assertIn("  PSU_MOTD=Welcome to Guardians.\n", out)
        self.assertNotIn("\u2014", out)

    def test_home_and_one_disc(self):
        with mock.patch.object(discs, "search", return_value=FOUND[:1]), answers("", "", "", ""):
            setup.first_run(dry_run=True)
        out = self.out.getvalue()
        self.assertIn("Players at home reach this computer at 10.0.0.20.", out)
        self.assertIn("  Discs:    Phantasy Star Universe (USA).iso\n", out)
        self.assertIn("  PSU_MOTD=Welcome to PSU Server.\n", out)

    def test_typed_folder_for_the_other_disc(self):
        other = [discs.Disc("/elsewhere/AotI.iso", "aoti")]
        with mock.patch.object(discs, "search", side_effect=[FOUND[:1], other]), \
                mock.patch("os.path.isdir", return_value=True), mock.patch("os.path.isfile", return_value=False):
            with answers("", "y", '"/elsewhere"'):
                chosen = setup.find_discs()
        self.assertEqual(chosen, {"psu": FOUND[0], "aoti": other[0]})

    def test_no_disc_found_asks_until_one_is_given(self):
        with mock.patch.object(discs, "search", side_effect=[[], [], FOUND[1:]]), \
                mock.patch("os.path.isdir", return_value=True), mock.patch("os.path.isfile", return_value=False):
            with answers("", "/empty", "/aoti", ""):
                chosen = setup.find_discs()
        self.assertEqual(chosen, {"aoti": FOUND[1]})
        self.assertIn("No Phantasy Star Universe disc image there", self.out.getvalue())


class FakeDocker:
    """Stands in for every docker command setup runs: records them and answers like a working
    Docker Desktop on which nothing runs yet. `extract` writes a small DATA."""

    def __init__(self, root):
        self.root, self.calls, self.networks = root, [], set()

    def answer(self, cmd):
        self.calls.append(cmd)
        out, code = "", 0
        if cmd[:3] == ["docker", "network", "inspect"]:
            code = 0 if cmd[3] in self.networks else 1
        elif cmd[:3] == ["docker", "network", "create"]:
            self.networks.add(cmd[3])
        elif cmd[:2] == ["docker", "ps"]:
            out = ""
        elif "run" in cmd and "extract" in cmd:
            data = os.path.join(self.root, "data")
            for rel in ("nbl/lobby.nbl", "gamedata/items.json", "captures/ppcube.bin"):
                os.makedirs(os.path.dirname(os.path.join(data, rel)), exist_ok=True)
                open(os.path.join(data, rel), "w").close()
            out = "psu: Phantasy Star Universe (USA) (SLUS-21194)\npsu-disc ...\npsu-gamedata ...\n"
        return code, out

    def run(self, cmd, check=True, capture=False, **kw):
        code, out = self.answer(cmd)
        return mock.Mock(returncode=code, stdout=out)

    def run_logged(self, cmd, log, on_line=None, progress=None, cwd=None, env=None):
        code, out = self.answer(cmd)
        for line in out.splitlines():
            log.write(line + "\n")
            if on_line:
                on_line(line)
        return code, out.splitlines()


class FirstRun(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name in ("psu.env.example", "compose.yml"):
            shutil.copy(os.path.join(REPO, name), self.dir)
        old = setup.ROOT
        setup.use_root(self.dir)
        self.addCleanup(setup.use_root, old)
        self.docker = FakeDocker(self.dir)
        self.out = io.StringIO()
        disc = os.path.join(self.dir, "discs in here", "Phantasy Star Universe (USA).iso")
        os.makedirs(os.path.dirname(disc))
        open(disc, "w").close()
        self.disc = disc
        patches = [mock.patch.object(setup, "run", self.docker.run),
                   mock.patch.object(proglib, "run_logged", self.docker.run_logged),
                   mock.patch.object(setup, "lan_address", return_value="10.0.0.20"),
                   mock.patch.object(setup, "wait_ready", return_value=True),
                   mock.patch.object(setup, "github", side_effect=OSError("offline")),
                   mock.patch.object(setup, "installed_version", return_value={"commit": "c" * 40}),
                   mock.patch.object(discs, "likely_folders", return_value=[]),
                   mock.patch.object(discs, "search", return_value=[discs.Disc(disc, "psu")]),
                   mock.patch.object(setup, "PROJECT", "psupub-test"),
                   mock.patch("sys.stdout", self.out)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_first_run_builds_and_starts(self):
        with answers("1", "y", "", "Guardians", "y"):
            setup.first_run()
        out = self.out.getvalue()
        env = setup.read_env()
        self.assertEqual((env["PSU_GAME_ADDR"], env["PSU_BUILD_COMMIT"]), ("10.0.0.20", "c" * 40))
        self.assertEqual(env["PSU_PANEL_CONTAINERS"].split(",")[0], "psupub-test-game-1")
        self.assertTrue(env["PSU_PANEL_TOKEN"])
        compose = [c[4:] for c in self.docker.calls if c[:4] == ["docker", "compose", "-p", "psupub-test"]]
        self.assertIn(["build", "game"], compose)
        self.assertIn(["--profile", "extract", "build", "extract"], compose)
        extract = next(c for c in compose if "run" in c)
        folder = os.path.dirname(self.disc).replace("\\", "/")
        self.assertEqual(extract, ["--profile", "extract", "run", "--rm", "-v", folder + ":/discs/1:ro", "extract",
                                   "--psu", "/discs/1/Phantasy Star Universe (USA).iso", "--out", "/data"])
        self.assertIn(["up", "-d", "--remove-orphans"], compose)
        self.assertIn(["docker", "network", "create", "openlobby"], self.docker.calls)
        self.assertIn("Tell your players: primary DNS 10.0.0.20, a DNAS-bypassed disc (PLAYING.md), any new "
                      "user ID.", out)
        for name in setup.GAME_NAMES:
            self.assertIn("    %s\n" % name, out)
        with open(os.path.join(self.dir, "state", "news.txt")) as f:
            self.assertIn("Welcome to Guardians!", f.read())
        with open(setup.UPDATE_FILE) as f:
            self.assertIn('"update": "unknown"', f.read())
        self.assertIn("Done: Building the game data", out)

    def test_a_failed_extract_says_what_to_do(self):
        def fail(cmd, log, **kw):
            self.docker.calls.append(cmd)
            if "run" in cmd:
                return 1, ["error: sha1 mismatch for /discs/1/PSU.iso"]
            return 0, []
        with mock.patch.object(proglib, "run_logged", fail), answers("1", "y", "", "", "y"):
            setup.first_run()
        out = self.out.getvalue()
        self.assertIn("Building the game data failed.", out)
        self.assertIn("not a plain copy of the retail disc", out)
        self.assertIn("choose Rebuild the game data", out)
        self.assertFalse(any("up" in c for c in self.docker.calls))


class SharedDns(unittest.TestCase):
    """One DNS server for every install on the machine (OpenLobby docs/installs.md)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        shutil.copy(os.path.join(REPO, "psu.env.example"), self.dir)
        old = setup.ROOT
        setup.use_root(self.dir)
        self.addCleanup(setup.use_root, old)
        self.calls = []
        p = mock.patch.object(setup, "PROJECT", "psu")
        p.start()
        self.addCleanup(p.stop)

    def docker(self, ps):
        def run(cmd, check=True, capture=False, **kw):
            self.calls.append(cmd)
            return mock.Mock(returncode=0, stdout=ps if cmd[:2] == ["docker", "ps"] else "")
        return mock.patch.object(setup, "run", run)

    def profiles(self, ps):
        with self.docker(ps):
            setup.plan_dns()
        return setup.read_env().get("COMPOSE_PROFILES")

    def test_start_ours_only_when_none_runs(self):
        self.assertEqual(self.profiles(""), "dns")
        self.assertEqual(self.profiles("openlobby-dns-1\topenlobby\t\n"), "")
        self.assertEqual(self.profiles("psu-dns-1\tpsu\t\nother-dns-1\tother\t\n"), "dns")
        setup.write_choices({"dns": False})
        self.assertEqual(self.profiles(""), "")

    def test_names_go_to_whichever_runs(self):
        with self.docker("openlobby-dns-1\topenlobby\t\n"), mock.patch("sys.stdout", io.StringIO()):
            self.assertTrue(setup.register_dns())
            self.assertFalse(setup.unregister_dns())
        self.assertIn(["docker", "ps", "--filter", "label=org.openlobby.role=dns", "--format",
                       '{{.Names}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "org.openlobby.panel.url"}}'],
                      self.calls)
        self.assertIn(["docker", "exec", "openlobby-dns-1", "python", "stub.py", "dns-names", "add", "psu",
                       "loginps2.psuus.segaonline.jp", "patchps2.psuus.segaonline.jp",
                       "loginps2.ipsuus.segaonline.jp", "patchps2.ipsuus.segaonline.jp"], self.calls)
        self.assertIn(["docker", "exec", "openlobby-dns-1", "python", "stub.py", "dns-names", "remove", "psu"],
                      self.calls)

    def test_isolated_tests_see_only_their_project(self):
        with self.docker(""), mock.patch.dict(os.environ, {"PSU_SETUP_ISOLATED": "1"}):
            setup.find_dns()
        self.assertIn("label=com.docker.compose.project=psu", self.calls[-1])


class Menu(unittest.TestCase):
    def test_items(self):
        titles = [t for t, _ in setup.MENU]
        for wanted in ("Start the server", "Stop the server", "Update", "Rebuild the game data",
                       "Pair with an OpenLobby admin panel", "Show what to tell players",
                       "Make a patched disc image for a player", "Remove the server from this computer",
                       "Make a backup", "Restore a backup"):
            self.assertIn(wanted, titles)
        self.assertTrue(all(callable(fn) for _, fn in setup.MENU))

    def test_a_number_runs_its_item(self):
        called = []
        menu = [(t, (lambda t=t: called.append(t))) for t, _ in setup.MENU]
        with mock.patch.object(setup, "MENU", menu), mock.patch.object(setup, "read_env", return_value={}), \
                mock.patch.object(setup, "menu_status", return_value="stopped"), \
                mock.patch("sys.stdout", io.StringIO()), answers("2", "x", "99", "7", "0"):
            setup.menu()
        self.assertEqual(called, ["Stop the server", "Pair with an OpenLobby admin panel"])

    def test_settings_items_restart_a_running_server(self):
        with mock.patch.object(setup, "read_env", return_value={"PSU_GAME_ADDR": "10.0.0.5"}), \
                mock.patch.object(setup, "read_choices", return_value={}), \
                mock.patch.object(setup, "server_running", return_value=True), \
                mock.patch.object(setup, "change_gm", return_value=True) as gm, \
                mock.patch.object(setup, "restart_if_running") as restart, \
                mock.patch("sys.stdout", io.StringIO()), answers("4", "0"):
            setup.change_settings()
        gm.assert_called_once()
        restart.assert_called_once_with(True)


class ServerFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def release(self, name, files, comment=b""):
        path = os.path.join(self.dir, name)
        with zipfile.ZipFile(path, "w") as z:
            for rel, text in files.items():
                z.writestr("psu-server-1.0/" + rel, text)
            z.comment = comment
        return path

    def test_unpack_and_update(self):
        root = os.path.join(self.dir, "server")
        os.makedirs(os.path.join(root, "state"))
        with open(os.path.join(root, ".env"), "w") as f:
            f.write("mine")
        first = self.release("a.zip", {"compose.yml": "1", "old.py": "x", ".env": "theirs", "state/x": "no",
                                       "data/nbl/x.nbl": "no", "../escape": "no"}, b"a" * 40)
        setup.unpack_server(first, root, {"tag": "v1.0.0"})
        self.assertEqual(sorted(setup.read_manifest(root)), ["compose.yml", "old.py"])
        with open(os.path.join(root, ".env")) as f:
            self.assertEqual(f.read(), "mine")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "escape")))
        old = setup.ROOT
        setup.use_root(root)
        self.addCleanup(setup.use_root, old)
        self.assertEqual(setup.installed_version(), {"tag": "v1.0.0", "commit": "a" * 40})
        second = self.release("b.zip", {"compose.yml": "2", "new.py": "y"})
        setup.unpack_server(second, root, {"tag": "v1.1.0", "commit": "b" * 40, "date": "2025-05-03T09:00:00Z"})
        self.assertFalse(os.path.exists(os.path.join(root, "old.py")))
        with open(os.path.join(root, "compose.yml")) as f:
            self.assertEqual(f.read(), "2")
        self.assertEqual(setup.installed_version()["commit"], "b" * 40)

    def test_root(self):
        self.assertEqual(setup.find_root(self.dir), os.path.abspath(self.dir))
        self.assertEqual(setup.find_root(), os.path.dirname(os.path.abspath(setup.__file__)))

    def test_disc_mounts(self):
        records = [{"path": os.path.join(self.dir, "a", "PSU.iso"), "game": "psu"},
                   {"path": os.path.join(self.dir, "b c", "AotI.iso"), "game": "aoti"}]
        volumes, args = setup.disc_mounts(records)
        self.assertEqual(volumes, ["-v", "%s:/discs/1:ro" % os.path.join(self.dir, "a").replace("\\", "/"),
                                   "-v", "%s:/discs/2:ro" % os.path.join(self.dir, "b c").replace("\\", "/")])
        self.assertEqual(args, ["--psu", "/discs/1/PSU.iso", "--aoti", "/discs/2/AotI.iso"])
        same, args = setup.disc_mounts([dict(records[0]), dict(records[1], path=os.path.join(self.dir, "a", "x.iso"))])
        self.assertEqual(len(same), 2)
        self.assertEqual(args[-1], "/discs/1/x.iso")


class Progress(unittest.TestCase):
    def test_extract_output_becomes_steps(self):
        out = io.StringIO()
        now = [0.0]
        p = proglib.Progress(out, clock=lambda: now[0])
        watch = proglib.ExtractWatcher(p)
        for line in ("psu: Phantasy Star Universe (USA) (SLUS-21194)", "psu-disc ...", "aoti-disc: up to date",
                     "  fetching EGS ceea04c3b438 from GitHub", "lobbies ...", "  12 zones", "psu-drops ..."):
            watch(line)
            now[0] += 120
        self.assertEqual(p.detail, "step 4, psu-drops")
        p.end()
        text = out.getvalue()
        self.assertIn("  Done: Reading your discs (4 minutes)\n", text)
        self.assertIn("  Done: Downloading the lobby builder (EGS) (2 minutes)\n", text)
        self.assertIn("  Done: Building the game data (6 minutes)\n", text)

    def test_run_logged(self):
        log = io.StringIO()
        seen = []
        code, tail = proglib.run_logged([sys.executable, "-c", "print('a'); print('b'); raise SystemExit(3)"],
                                        log, on_line=seen.append)
        self.assertEqual((code, tail, seen), (3, ["a", "b"], ["a", "b"]))

    def test_explain(self):
        self.assertIn("disk is full", proglib.explain(["OSError: [Errno 28] No space left on device"]))
        self.assertIn("ports", proglib.explain(["Bind for 0.0.0.0:53 failed: port is already allocated"]))
        self.assertEqual(proglib.explain(["something else"]), "")


class Docker(unittest.TestCase):
    def test_missing_docker_explains_and_returns(self):
        out = io.StringIO()
        with mock.patch.object(setup, "docker_state", return_value="missing"), \
                mock.patch("sys.platform", "win32"), mock.patch("sys.stdout", out):
            self.assertFalse(setup.wait_for_docker(wait=False))
        self.assertIn(setup.DOCKER_DOWNLOAD, out.getvalue())

    def test_waits_until_docker_runs(self):
        out = io.StringIO()
        with mock.patch.object(setup, "docker_state", side_effect=["stopped", "ok"]), \
                mock.patch.object(setup, "start_docker_desktop", return_value=False), \
                mock.patch("sys.platform", "darwin"), mock.patch("sys.stdout", out), answers(""):
            self.assertTrue(setup.wait_for_docker())
        self.assertIn("Docker Desktop is installed but not running", out.getvalue())


if __name__ == "__main__":
    unittest.main()
