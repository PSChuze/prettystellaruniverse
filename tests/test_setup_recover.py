"""setup.py when things go wrong or change under it: a build that outlives the window, one that
stopped part way, Docker stopping under a build, a newer setup program over older files, Start
over, the status above the menu, free space, and `--update --yes` / `--check-update` as an
OpenLobby panel's updater runs them (stdin closed, one line a minute, the reason last)."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "..")
sys.path[:0] = [HERE, REPO, os.path.join(REPO, "extract")]
import build as extract_build  # noqa: E402
import setup  # noqa: E402
from hostsetup import diskspace  # noqa: E402
from hostsetup import dockermem  # noqa: E402
from hostsetup import progress as proglib  # noqa: E402
from test_setup_flow import FakeDocker, answers  # noqa: E402

REAL_RUN_LOGGED = proglib.run_logged
LATEST = {"tag": "v0.2.0", "zip": "https://example.invalid/v0.2.0.zip", "commit": "b" * 40,
          "date": "2026-10-20T08:00:00Z"}


class Docker(FakeDocker):
    """FakeDocker with a server that can run, builds that can still be running or end with an
    exit code (255: Docker went away), and a build that writes the real fingerprint."""

    def __init__(self, root):
        super().__init__(root)
        self.running = set()
        self.exits = []             # exit codes of the next builds, in order (0 when empty)
        self.volumes = {"psupub-test_extract-cache"}
        self.log_cmd = None         # a command whose output stands in for `docker logs -f`

    def answer(self, cmd):
        if "ps" in cmd and "--status" in cmd:
            self.calls.append(cmd)
            return 0, "\n".join(sorted(self.running))
        if cmd[:3] == ["docker", "volume", "ls"]:
            self.calls.append(cmd)
            return 0, "\n".join(sorted(self.volumes))
        if cmd[:3] == ["docker", "volume", "rm"]:
            self.calls.append(cmd)
            self.volumes.discard(cmd[3])
            return 0, ""
        if cmd[:4] == ["docker", "compose", "-p", "psupub-test"] and "up" in cmd:
            self.calls.append(cmd)
            self.running |= set(setup.SERVICES)
            return 0, ""
        if cmd[:4] == ["docker", "compose", "-p", "psupub-test"] and "stop" in cmd:
            self.calls.append(cmd)
            self.running.discard("game")
            return 0, ""
        if "run" in cmd and "extract" in cmd and cmd[:2] == ["docker", "compose"]:
            self.exit = self.exits.pop(0) if self.exits else 0
            code, out = super().answer(cmd)
            if self.exit == 0:
                with open(os.path.join(self.root, "data", ".built"), "w") as f:
                    json.dump({"fingerprint": setup.extract_version()}, f)
            return code, out
        return super().answer(cmd)

    def built_by_hand(self):
        os.makedirs(os.path.join(self.root, "data"), exist_ok=True)
        with open(os.path.join(self.root, "data", ".built"), "w") as f:
            json.dump({"fingerprint": setup.extract_version()}, f)

    def run_logged(self, cmd, log, on_line=None, progress=None, cwd=None, env=None):
        if cmd[:3] == ["docker", "logs", "-f"] and self.log_cmd:
            self.calls.append(cmd)
            code, tail = REAL_RUN_LOGGED(self.log_cmd, log, on_line=on_line, progress=progress)
            if self.build and self.build[1] == "running":
                self.build = ("c0ffee", "exited", 0)
                self.built_by_hand()
            return code, tail
        return super().run_logged(cmd, log, on_line=on_line, progress=progress)


def release_zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for rel, text in files.items():
            z.writestr("prettystellaruniverse-0.2.0/" + rel, text)


class Case(unittest.TestCase):
    """A server folder in a temp dir, set up already (.env, discs chosen), against Docker."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name in ("psu.env.example", "compose.yml"):
            shutil.copy(os.path.join(REPO, name), self.dir)
        os.makedirs(os.path.join(self.dir, "extract"))
        with open(os.path.join(self.dir, "extract", "tool.py"), "w") as f:
            f.write("print('v1')\n")
        old = setup.ROOT
        setup.use_root(self.dir)
        self.addCleanup(setup.use_root, old)
        self.disc = os.path.join(self.dir, "discs in here", "PSU.iso")
        os.makedirs(os.path.dirname(self.disc))
        open(self.disc, "w").close()
        setup.write_env({"PSU_GAME_ADDR": "10.0.0.20", "PSU_PANEL_TOKEN": "t" * 32,
                         "COMPOSE_PROJECT_NAME": "psupub-test"})
        setup.write_choices({"who": "home", "name": "Guardians", "discs": [{"path": self.disc, "game": "psu"}]})
        with open(setup.VERSION_FILE, "w") as f:
            json.dump({"tag": "v0.1.0", "commit": "a" * 40}, f)
        self.docker = Docker(self.dir)
        self.out = io.StringIO()
        patches = [mock.patch.object(setup, "run", self.docker.run),
                   mock.patch.object(proglib, "run_logged", self.docker.run_logged),
                   mock.patch.object(setup, "lan_address", return_value="10.0.0.20"),
                   mock.patch.object(setup, "wait_ready", return_value=True),
                   mock.patch.object(setup, "docker_state", return_value="ok"),
                   mock.patch.object(setup, "docker_resources", return_value=(8 << 30, True)),
                   mock.patch.object(setup, "space_places", return_value=[]),
                   mock.patch.object(setup, "github", side_effect=OSError("offline")),
                   mock.patch.object(setup, "PROJECT", "psupub-test"),
                   mock.patch.object(setup, "UNATTENDED", False),
                   mock.patch("time.sleep"),
                   mock.patch("sys.stdout", self.out)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def built(self, fingerprint=None):
        os.makedirs(os.path.join(self.dir, "data", "nbl"), exist_ok=True)
        with open(os.path.join(self.dir, "data", "nbl", "lobby.nbl"), "w"):
            pass
        with open(os.path.join(self.dir, "data", ".built"), "w") as f:
            json.dump({"fingerprint": fingerprint or setup.extract_version()}, f)


class DataState(Case):
    def test_marked_partial_none_and_older_setups(self):
        env = setup.read_env()
        self.assertEqual(setup.game_data(env, {}), "none")
        os.makedirs(os.path.join(self.dir, "data", "gamedata"))
        with open(os.path.join(self.dir, "data", "gamedata", "items.json"), "w"):
            pass
        # What used to count as built (a .json and an .nbl) is not, without the mark.
        os.makedirs(os.path.join(self.dir, "data", "nbl"))
        with open(os.path.join(self.dir, "data", "nbl", "quest-0.nbl"), "w"):
            pass
        self.assertEqual(setup.game_data(env, {"build": "started"}), "partial")
        self.assertEqual(setup.game_data(env, {}), "partial")
        self.assertEqual(setup.game_data(env, {}, ("c", "running", None)), "building")
        os.makedirs(os.path.join(self.dir, "data", "nbl", "season-staging"))
        self.assertEqual(setup.game_data(env, {}), "built")        # built before the mark, by v0.1.0
        self.assertEqual(setup.game_data(env, {"build": "failed"}), "partial")
        self.built()
        self.assertEqual(setup.game_data(env, {"build": "failed"}), "built")
        self.assertTrue(setup.data_current(env))
        with open(os.path.join(self.dir, "extract", "tool.py"), "w") as f:
            f.write("print('v2')\n")
        self.assertFalse(setup.data_current(env))

    def test_setup_and_extract_agree_on_the_fingerprint(self):
        self.assertEqual(setup.extract_version(os.path.join(REPO, "extract")), extract_build.fingerprint())


class ExtractStamps(unittest.TestCase):
    """extract/build.py's per-step stamps carry the builder's fingerprint; .built comes last."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_a_rerun_skips_finished_steps_and_redoes_changed_ones(self):
        b = extract_build.Build({}, os.path.join(self.dir, "out"), os.path.join(self.dir, "work"))
        target = os.path.join(self.dir, "out", "x.json")
        ran = []

        def step():
            os.makedirs(os.path.dirname(target), exist_ok=True)
            ran.append(1)
            open(target, "w").close()
        with mock.patch("sys.stdout", io.StringIO()) as out:
            b.step("x", [target], step, "input")
            again = extract_build.Build({}, os.path.join(self.dir, "out"), os.path.join(self.dir, "work"))
            again.step("x", [target], step, "input")
            self.assertIn("x: up to date", out.getvalue())
            again.code = "another builder"
            again.step("x", [target], step, "input")
        self.assertEqual(len(ran), 2)

    def test_built_mark_is_written_last(self):
        out = os.path.join(self.dir, "out")
        os.makedirs(out)
        open(os.path.join(out, ".built"), "w").close()
        b = extract_build.Build({"psu": ("/x.iso", {"serial": "SLUS-21194"})}, out, os.path.join(self.dir, "w"))
        seen = []
        with mock.patch.object(b, "steps", side_effect=lambda: seen.append(os.path.exists(os.path.join(out, ".built")))):
            b.run()
        self.assertEqual(seen, [False])
        with open(os.path.join(out, ".built")) as f:
            info = json.load(f)
        self.assertEqual((info["fingerprint"], info["discs"]), (b.code, {"psu": "SLUS-21194"}))

        def stops():
            raise SystemExit("extract: psu-drops failed (exit 1)")
        with mock.patch.object(b, "steps", side_effect=stops), self.assertRaises(SystemExit):
            b.run()
        self.assertFalse(os.path.exists(os.path.join(out, ".built")))


class Recover(Case):
    def test_a_build_that_outlived_the_window_is_followed(self):
        self.docker.build = ("c0ffee", "running", None)
        os.makedirs(os.path.join(self.dir, "data"))

        self.docker.log_cmd = [sys.executable, "-c", "print('2026-01-01T00:01:00Z seasons ...')"]
        with answers():
            setup.resume()
        out = self.out.getvalue()
        self.assertIn("The game data is still being built: step 2 of 13, PSU items, shops and weapons.", out)
        self.assertIn("close this window: the build keeps going", out)
        self.assertIn(["docker", "rm", "c0ffee"], self.docker.calls)
        self.assertIn("game", self.docker.running)          # the first run went on to start it

    def test_docker_stopping_carries_on_where_it_stopped(self):
        self.docker.exits = [255, 0]
        with answers():
            with setup.log_file() as log:
                self.assertTrue(setup.build_game_data(setup.read_env(), setup.read_choices(), proglib.Progress(
                    out=io.StringIO()), log))
        out = self.out.getvalue()
        self.assertIn(setup.DOCKER_STOPPED, out)
        self.assertIn("Carrying on where it stopped.", out)
        runs = [c for c in self.docker.calls if "run" in c and "extract" in c and c[:2] == ["docker", "compose"]]
        self.assertEqual(len(runs), 2)
        self.assertNotIn("--rebuild", runs[1])                  # the finished steps are kept
        self.assertNotIn(["docker", "volume", "rm", "psupub-test_extract-cache"], self.docker.calls)
        self.assertEqual(setup.read_choices()["build"], "done")

    def test_a_stopped_build_offers_to_carry_on(self):
        self.docker.build = ("dead", "exited", 1)
        os.makedirs(os.path.join(self.dir, "data", "nbl"))
        with open(os.path.join(self.dir, "data", "nbl", "quest-0.nbl"), "w"):
            pass
        setup.write_choices(dict(setup.read_choices(), build="started"))
        with mock.patch.object(setup, "complete_setup", return_value=True) as again, \
                mock.patch.object(setup, "offer_panel"), answers("y"):
            setup.resume()
        self.assertIn("the game data is only partly built", self.out.getvalue())
        again.assert_called_once()


class NewerSetup(Case):
    def carry(self, tag, commit):
        archive = os.path.join(self.dir, "carried.zip")
        release_zip(archive, {"compose.yml": "new", "extract/tool.py": "print('v2')\n", ".env": "theirs"})
        return setup.apply_carried(archive, {"tag": tag, "commit": commit})

    def test_newer_files_replace_older_ones_and_keep_the_hosts(self):
        self.assertTrue(self.carry("v0.2.0", "b" * 40))
        with open(os.path.join(self.dir, "compose.yml")) as f:
            self.assertEqual(f.read(), "new")
        self.assertEqual(setup.read_env()["PSU_GAME_ADDR"], "10.0.0.20")
        self.assertEqual(setup.installed_version()["tag"], "v0.2.0")
        self.assertTrue(setup.read_choices()["files_updated"])
        self.assertIn("It updated your server's files from 0.1.0", self.out.getvalue())

    def test_never_older_and_not_the_same(self):
        self.assertFalse(self.carry("v0.0.9", "c" * 40))
        self.assertIn("older than your server (0.0.9 < 0.1.0)", self.out.getvalue())
        self.assertFalse(self.carry("v0.1.0", "a" * 40))
        with open(os.path.join(self.dir, "compose.yml")) as f:
            self.assertNotEqual(f.read(), "new")

    def test_versions(self):
        self.assertEqual(setup.compare_versions({"tag": "v0.10.0"}, {"tag": "v0.9.1"}), "newer")
        self.assertEqual(setup.compare_versions({"tag": "dev", "commit": "a"}, {"tag": "v1.0.0", "commit": "a"}),
                         "same")
        self.assertEqual(setup.compare_versions({}, {"tag": "v1.0.0"}), "same")


class StartOver(Case):
    def test_keeps_the_accounts_unless_asked(self):
        self.built()
        with open(os.path.join(self.dir, "state", "psu.db"), "w"):
            pass
        with mock.patch.object(setup, "first_run") as again, answers("", "start over"):
            self.assertTrue(setup.start_over())
        again.assert_called_once()
        self.assertFalse(os.path.exists(os.path.join(self.dir, "data")))
        self.assertFalse(os.path.exists(setup.ENV))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "state", "psu.db")))
        self.assertIn(["docker", "volume", "rm", "psupub-test_extract-cache"], self.docker.calls)

    def test_anything_but_the_words_cancels(self):
        self.built()
        with answers("y", "yes"):
            self.assertFalse(setup.start_over())
        self.assertTrue(os.path.exists(os.path.join(self.dir, "data", ".built")))


class Status(Case):
    def test_the_address_on_every_start_and_in_a_file(self):
        self.built()
        with open(setup.UPDATE_FILE, "w") as f:
            json.dump({"update": "available"}, f)
        setup.show_status(setup.read_env(), setup.read_choices())
        out = self.out.getvalue()
        self.assertIn("Your server is stopped. Choose 1 to start it.", out)
        self.assertIn("Tell your players: primary DNS 10.0.0.20", out)
        self.assertIn("A newer version is out: choose Update.", out)
        with open(setup.NOTE, encoding="utf-8") as f:
            note = f.read()
        self.assertIn("Guardians", note)
        self.assertIn("    loginps2.psuus.segaonline.jp", note)
        self.assertIn("What to tell players.txt", setup.KEEP)


class Space(Case):
    def test_short_stops_unattended_and_asks_otherwise(self):
        short = [diskspace.Place("docker", "", "C:", 1 << 30, 4 << 30)]
        with mock.patch.object(setup, "space_places", return_value=short):
            with mock.patch.object(setup, "UNATTENDED", True):
                self.assertFalse(setup.check_space())
            with answers("2"):
                self.assertTrue(setup.check_space())
        self.assertIn("It needs about 4 GB there, and has 1.0 GB free.", self.out.getvalue())


class Memory(unittest.TestCase):
    def test_wslconfig_keeps_the_rest(self):
        text = "# mine\n[wsl2]\nmemory=2GB\nswap=0\n\n[experimental]\nautoMemoryReclaim=gradual\n"
        got = dockermem.set_wsl_memory(text, "4GB")
        self.assertIn("[wsl2]\nmemory=4GB\nswap=0\n", got)
        self.assertIn("autoMemoryReclaim=gradual", got)
        self.assertEqual(dockermem.set_wsl_memory("", "4GB"), "[wsl2]\nmemory=4GB\n")
        self.assertEqual(dockermem.wsl_memory_now(text), "2GB")

    def test_engine_crashes(self):
        self.assertTrue(dockermem.engine_stopped(["error waiting for container: unexpected EOF"]))
        self.assertTrue(dockermem.engine_stopped([], 255))
        self.assertFalse(dockermem.engine_stopped(["extract: psu-drops failed (exit 1)"], 1))
        self.assertEqual(dockermem.memory_to_offer(16 << 30, 2 << 30), 4 << 30)
        self.assertIsNone(dockermem.memory_to_offer(6 << 30, 3 << 30))


class Unattended(Case):
    """`setup.py --update --yes` as the panel's updater runs it."""

    def setUp(self):
        super().setUp()
        self.built()
        self.docker.running |= set(setup.SERVICES)
        archive = os.path.join(self.dir, "release.zip")
        release_zip(archive, {"compose.yml": "v0.2.0", "psu.env.example": "", "extract/tool.py": "print('v2')\n"})
        self.download = mock.patch("urllib.request.urlretrieve",
                                   side_effect=lambda url, path: shutil.copy(archive, path))
        for p in (self.download, mock.patch.object(setup, "latest_release", return_value=dict(LATEST)),
                  mock.patch.object(setup, "compare", return_value=(1, [])),
                  mock.patch("sys.stdin", io.StringIO("")),
                  mock.patch("builtins.input", side_effect=EOFError)):
            p.start()
            self.addCleanup(p.stop)

    def main(self, *args):
        with self.assertRaises(SystemExit) as stop:
            setup.main(["--folder", self.dir, "--yes"] + list(args))
        return stop.exception.code, self.out.getvalue().splitlines()

    def test_update_applies_builds_restarts_and_reports_current(self):
        self.docker.log_cmd = [sys.executable, "-c", "import time\nfor s in ('psu-disc ...', 'psu-gamedata ...'):\n"
                                                     "    print('2026-01-01T00:00:09Z ' + s, flush=True)\n"
                                                     "    time.sleep(1.5)\n"]
        with mock.patch.object(proglib.Progress, "every", 0.25):
            code, lines = self.main("--update")
        self.assertEqual(code, 0, "\n".join(lines))
        self.assertEqual(lines[-1], "The update is done.")
        for n in range(1, 6):
            self.assertTrue(any(line.startswith("Step %d of 5: " % n) for line in lines), n)
        self.assertEqual(setup.installed_version()["tag"], "v0.2.0")
        self.assertEqual(setup.read_env()["PSU_GAME_ADDR"], "10.0.0.20")          # .env kept
        self.assertIn("Update state: current", lines)
        with open(setup.UPDATE_FILE) as f:
            self.assertEqual(json.load(f)["update"], "current")
        # The builder changed, so the game data was built again; a long step keeps talking.
        heartbeats = [line for line in lines if line.endswith(" so far") and ": step " in line]
        self.assertGreaterEqual(len(heartbeats), 2, "\n".join(lines))
        self.assertIn("up", [c[4] for c in self.docker.calls if c[:4] == ["docker", "compose", "-p", "psupub-test"]])
        self.assertFalse(os.path.exists(setup.LOCK))
        # A second run finds nothing to do.
        self.out.truncate(0)
        self.out.seek(0)
        code, lines = self.main("--update")
        self.assertEqual(code, 0)
        self.assertIn("The server's files are the newest release (0.2.0).", lines)
        self.assertIn("The game data is up to date.", lines)

    def test_never_downgrades(self):
        with open(setup.VERSION_FILE, "w") as f:
            json.dump({"tag": "v0.3.0", "commit": "c" * 40}, f)
        code, lines = self.main("--update")
        self.assertIn("The server's files (0.3.0) are newer than the newest release (0.2.0): they stay.", lines)
        with open(os.path.join(self.dir, "compose.yml")) as f:
            self.assertNotEqual(f.read(), "v0.2.0")

    def test_a_failure_ends_with_the_reason(self):
        self.docker.exits = [1]
        self.docker.LOG = ["psu-drops ...", "extract: dropdata.py failed (exit 1)"]
        code, lines = self.main("--update")
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], "Building the game data failed; see %s." % setup.LOG)
        # A rerun carries on: the files are there already, the data is built again.
        self.out.truncate(0)
        self.out.seek(0)
        code, lines = self.main("--update")
        self.assertEqual((code, lines[-1]), (0, "The update is done."))

    def test_never_asks(self):
        setup.write_choices({"who": "home"})            # no discs known
        with open(os.path.join(self.dir, "data", ".built"), "w") as f:
            json.dump({"fingerprint": "older builder"}, f)
        code, lines = self.main("--update")
        self.assertEqual(code, 1)
        self.assertIn("setup does not know where the discs are", lines[-1])

    def test_offline_and_check_update(self):
        with mock.patch.object(setup, "latest_release", return_value=None):
            code, lines = self.main("--update")
        self.assertEqual((code, lines[-1]), (1, "Setup could not reach GitHub to look for a newer release."))
        self.out.truncate(0)
        self.out.seek(0)
        code, lines = self.main("--check-update")
        self.assertEqual(code, 0)
        self.assertTrue(lines[-1].startswith("Update state: "))
        self.assertTrue(os.path.exists(setup.UPDATE_FILE))

    def test_the_project_comes_from_env(self):
        with mock.patch.object(setup, "run_update") as update:
            self.main("--update")
        update.assert_called_once()
        self.assertEqual(setup.PROJECT, "psupub-test")


if __name__ == "__main__":
    unittest.main()
