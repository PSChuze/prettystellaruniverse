"""setup.py's .env handling and the files it works with: values land in the template's lines and
comments stay; psu.env.example has every setting compose.yml reads; compose.yml keeps the panel's
contract (labels, network, no published admin port)."""
import os
import re
import shutil
import tempfile
import unittest

import setup

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


class EnvFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.template = os.path.join(self.dir, "example")
        self.env = os.path.join(self.dir, ".env")
        with open(self.template, "w", encoding="utf-8") as f:
            f.write("# the host\nPSU_GAME_ADDR=\n\n#PSU_DNS_BIND=10.0.0.2\n#PSU_STATE=./state\n")

    def write(self, values):
        setup.write_env(values, self.env, self.template)
        with open(self.env, encoding="utf-8") as f:
            return f.read()

    def test_first_write_starts_from_the_template(self):
        text = self.write({"PSU_GAME_ADDR": "203.0.113.9", "PSU_DNS_BIND": "10.0.0.20", "PSU_NEW": 1})
        self.assertEqual(text, "# the host\nPSU_GAME_ADDR=203.0.113.9\n\nPSU_DNS_BIND=10.0.0.20\n"
                               "#PSU_STATE=./state\nPSU_NEW=1\n")
        self.assertEqual(setup.read_env(self.env),
                         {"PSU_GAME_ADDR": "203.0.113.9", "PSU_DNS_BIND": "10.0.0.20", "PSU_NEW": "1"})

    def test_later_writes_keep_the_file(self):
        self.write({"PSU_DNS_BIND": "10.0.0.20"})
        text = self.write({"PSU_GAME_ADDR": "198.51.100.7"})
        self.assertIn("PSU_GAME_ADDR=198.51.100.7\n", text)
        self.assertIn("\nPSU_DNS_BIND=10.0.0.20\n", text)

    def test_none_comments_a_line_out(self):
        self.write({"PSU_DNS_BIND": "10.0.0.20"})
        text = self.write({"PSU_DNS_BIND": None, "PSU_OTHER": None})
        self.assertIn("\n#PSU_DNS_BIND=10.0.0.20\n", text)
        self.assertNotIn("PSU_OTHER", text)


def compose_text():
    with open(os.path.join(REPO, "compose.yml"), encoding="utf-8") as f:
        return f.read()


class Files(unittest.TestCase):
    def test_example_has_every_setting_compose_reads(self):
        example = setup.read_env(os.path.join(REPO, "psu.env.example"))
        used = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", compose_text()))
        # The PSU_PORT_* settings only move the published ports for tests; OPENLOBBY_NETWORK likewise.
        used = {k for k in used if not k.startswith("PSU_PORT_") and k != "OPENLOBBY_NETWORK"}
        self.assertEqual(sorted(used - set(example)), [])
        self.assertIn("COMPOSE_PROFILES", example)

    def test_ports_setup_knows_are_the_ones_published(self):
        text = compose_text()
        for key, port, protocol, _ in setup.PORTS:
            self.assertIn("${%s:-%d}" % (key, port), text)

    def test_panel_contract(self):
        text = compose_text()
        game = text[text.index("\n  game:"):text.index("\n  ssl3-psu:")]
        for line in ("org.openlobby.role: game", "org.openlobby.game.key: psu",
                     "org.openlobby.game.title: Phantasy Star Universe",
                     "org.openlobby.admin.url: http://psu:22191", "aliases: [psu]", 'PSU_PANEL_PORT: "22191"'):
            self.assertIn(line, game)
        self.assertNotIn("22191:", text)            # never published
        self.assertIn("name: ${OPENLOBBY_NETWORK:-openlobby}\n    external: true", text)

    def test_ssl3_terminators(self):
        for name, ports in (("stunnel-psu.conf", ("12110", "12061")), ("stunnel-aoti.conf", ("12140",)),
                            ("stunnel-pc.conf", ("12230",))):
            with open(os.path.join(REPO, "compose", name), encoding="utf-8") as f:
                conf = f.read()
            for port in ports:
                self.assertIn("accept  = 0.0.0.0:%s\n" % port, conf)
            self.assertEqual(conf.count("sslVersionMin = SSLv3"), len(ports))
            self.assertEqual(conf.count("options = -NO_SSLv3"), len(ports))
            self.assertEqual(conf.count("renegotiation = no"), len(ports))
            self.assertTrue(all(c.endswith(":@SECLEVEL=0") for c in re.findall(r"ciphers = (\S+)", conf)))
        self.assertIn("image: ghcr.io/prettyopenlobby/openlobby-ssl3:${OPENLOBBY_TAG:-latest}", compose_text())

    def test_experimental_legs_are_off_by_default(self):
        text = compose_text()
        for service in ("patch-extra", "ssl3-pc"):
            block = text[text.index("\n  %s:" % service):]
            self.assertIn("profiles: [experimental]", block[:300])
        self.assertNotIn("experimental", setup.read_env(os.path.join(REPO, "psu.env.example"))["COMPOSE_PROFILES"])


if __name__ == "__main__":
    unittest.main()
