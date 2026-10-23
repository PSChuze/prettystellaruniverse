"""setup.py's disc search: telling the PSU discs apart by the boot file in SYSTEM.CNF, looking in the
usual folders and PCSX2's, and choosing one image per game."""
import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock

from hostsetup import discs


def record(name, lba, size, folder=False):
    raw = name.encode("ascii")
    n = 33 + len(raw) + (len(raw) + 1) % 2
    return (bytes([n, 0]) + struct.pack("<I", lba) + struct.pack(">I", lba) + struct.pack("<I", size)
            + struct.pack(">I", size) + bytes(7) + bytes([2 if folder else 0, 0, 0]) + b"\x01\x00\x00\x01"
            + bytes([len(raw)]) + raw + bytes(n - 33 - len(raw)))


def image(boot="SLUS_211.94", pad_to=0, cnf=None):
    """A small ISO9660 image with a SYSTEM.CNF naming `boot`."""
    s = discs.SECTOR
    cnf = cnf if cnf is not None else ("BOOT2 = cdrom0:\\%s;1\r\nVER = 1.00\r\nVMODE = NTSC\r\n" % boot).encode()
    root = record("\x00", 20, s, True) + record("\x01", 20, s, True) + record("SLUS_211.94;1", 22, 4) \
        + record("SYSTEM.CNF;1", 21, len(cnf))
    pvd = bytearray(s)
    pvd[0:6] = b"\x01CD001"
    pvd[156:156 + 34] = record("\x00", 20, s, True)
    out = bytearray(23 * s)
    out[16 * s:17 * s] = pvd
    out[20 * s:20 * s + len(root)] = root
    out[21 * s:21 * s + len(cnf)] = cnf
    return bytes(out) + bytes(max(0, pad_to - len(out)))


class Files(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        # Real discs are gigabytes: let small files through, and call their sizes retail.
        for target, value in (("MIN_IMAGE", 0), ("GAMES", {
                "SLUS_211.94": ("psu", "Phantasy Star Universe", len(image()) + 2048),
                "SLUS_216.31": ("aoti", "Ambition of the Illuminus", len(image()))})):
            p = mock.patch.object(discs, target, value)
            p.start()
            self.addCleanup(p.stop)

    def put(self, rel, data):
        path = os.path.join(self.dir, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_identify(self):
        psu = self.put("Phantasy Star Universe (USA).iso", image(pad_to=len(image()) + 2048))
        self.assertEqual(discs.identify(psu), [discs.Disc(psu, "psu", True)])
        aoti = self.put("x/whatever.ISO", image("SLUS_216.31", pad_to=len(image()) + 4096))
        self.assertEqual(discs.identify(aoti), [discs.Disc(aoti, "aoti", False)])
        self.assertEqual(discs.identify(self.put("other.iso", image("SLUS_123.45"))), [])
        self.assertEqual(discs.identify(self.put("blank.iso", bytes(40000))), [])
        self.assertEqual(discs.identify(self.put("short.iso", b"CD001")), [])
        self.assertEqual(discs.identify(self.put("psu.bin", image())), [])

    def test_boot_line_variants(self):
        path = self.put("a.iso", image(cnf=b"BOOT2=cdrom0:\\SLUS_216.31;1\n"))
        self.assertEqual(discs.identify(path)[0].game, "aoti")

    def test_search_and_pick(self):
        retail = self.put("games/Phantasy Star Universe (USA).iso", image(pad_to=len(image()) + 2048))
        self.put("games/Phantasy Star Universe (USA) [ONLINE].iso", image(pad_to=len(image()) + 2048))
        self.put("games/deep/a/b/c/d/PSU.iso", image())                      # too deep
        aoti = self.put("games/aoti/AotI.iso", image("SLUS_216.31"))
        found = discs.search([os.path.join(self.dir, "games")])
        self.assertEqual(len(found), 3)
        chosen = discs.pick(found)
        self.assertEqual(chosen["psu"].path, retail)
        self.assertEqual(chosen["aoti"].path, aoti)
        self.assertEqual(discs.pick([d for d in found if d.game == "aoti"]), {"aoti": chosen["aoti"]})
        self.assertEqual(discs.pick([]), {})

    def test_pcsx2_folders(self):
        ini = "[GameList]\nRecursivePaths = D:\\PS2 Games\nPaths = /mnt/games\n\n[EmuCore]\nPaths=\n"
        self.assertEqual(discs.pcsx2_game_folders(ini), ["D:\\PS2 Games", "/mnt/games"])
        home = os.path.join(self.dir, "home")
        games = os.path.join(self.dir, "ps2")
        os.makedirs(games)
        os.makedirs(os.path.join(home, "Downloads"))
        cfg = os.path.join(home, ".config", "PCSX2", "inis")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "PCSX2.ini"), "w") as f:
            f.write("[GameList]\nPaths = %s\n" % games)
        folders = discs.likely_folders([home], home=home, platform="linux", env={})
        self.assertEqual(folders, [home, os.path.join(home, "Downloads"), games])
        self.assertEqual(discs.pcsx2_configs("C:\\Users\\x", "win32", {})[0],
                         os.path.join("C:\\Users\\x", "Documents", "PCSX2", "inis", "PCSX2.ini"))


class RetailSizes(unittest.TestCase):
    def test_match_the_bypass_manifest(self):
        import json
        with open(os.path.join(os.path.dirname(__file__), "..", "patches", "bypass.json"), encoding="utf-8") as f:
            games = json.load(f)["games"]
        for serial, g in games.items():
            self.assertEqual(discs.GAMES[g["boot_elf"]][2], g["source"]["size"], serial)


if __name__ == "__main__":
    unittest.main()
