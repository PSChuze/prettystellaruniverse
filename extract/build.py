"""The build steps behind `python -m extract`: every table and archive the server reads, from the discs given.

Each step runs the tools in this directory as child processes (each stays runnable alone) with
$PSU_EXTRACT_WORK set to the cache directory. A finished step leaves a stamp in <work>/stamps; a re-run skips a
step whose stamp matches (same discs and output directory, outputs still there); --rebuild ignores the stamps.
Once a step runs, every later one runs too, since later steps read what earlier ones wrote.
"""
import glob
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import egs  # noqa: E402

LOBBY_AFS = {"psu": "XF_QE_OFF_EXTRA_LOBBY01.AFS", "aoti": "XF_QE_OFF_LOBBY_ILLUMINUS.AFS"}


class Build:
    def __init__(self, discs, out, work, egs_src=None, rebuild=False):
        self.discs = discs              # {game: (iso path, disc.identify() info)}
        self.out = os.path.abspath(out)
        self.work = os.path.abspath(work)
        self.egs_src = egs_src
        self.dirty = rebuild
        self.env = dict(os.environ, PSU_EXTRACT_WORK=self.work, PYTHONDONTWRITEBYTECODE="1")

    # paths
    def gamedata(self, game):
        return os.path.join(self.out, "gamedata", *(["aoti"] if game == "aoti" else []))

    def nbl(self, game):
        return os.path.join(self.out, "nbl", *(["aoti"] if game == "aoti" else []))

    def iso(self, game):
        return self.discs[game][0]

    # running
    def tool(self, *args):
        r = subprocess.run([sys.executable, os.path.join(HERE, args[0]), *map(str, args[1:])], env=self.env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if r.returncode:
            sys.stdout.write(r.stdout[-4000:])
            raise SystemExit(f"extract: {' '.join(map(str, args))} failed (exit {r.returncode})")
        return r.stdout

    def step(self, name, outputs, fn, *inputs):
        """Run fn() unless the stamp for name matches and every output exists."""
        stamp = os.path.join(self.work, "stamps", name + ".json")
        sig = {"out": self.out, "inputs": [self.disc_key(g) if g in self.discs else g
                                                               for g in inputs]}
        if not self.dirty and os.path.exists(stamp) and all(os.path.exists(p) for p in outputs):
            with open(stamp, encoding="utf-8") as f:
                if json.load(f) == sig:
                    print(f"{name}: up to date")
                    return
        print(f"{name} ...", flush=True)
        self.dirty = True
        fn()
        missing = [p for p in outputs if not os.path.exists(p)]
        if missing:
            raise SystemExit(f"extract: {name} did not write {', '.join(missing)}")
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as f:
            json.dump(sig, f)

    def disc_key(self, game):
        st = os.stat(self.iso(game))
        return [game, st.st_size, int(st.st_mtime), self.discs[game][1]["patched"]]

    # steps
    def run(self):
        games = [g for g in ("psu", "aoti") if g in self.discs]
        for g in games:
            self.step(f"{g}-disc", [os.path.join(self.work, g, "erx", "PSU_US.erx")], lambda g=g: self.disc(g), g)
        for g in games:
            gd = self.gamedata(g)
            names = ("items", "shops", "weapons", "enemylevel")
            self.step(f"{g}-gamedata", [os.path.join(gd, n + ".json") for n in names],
                      lambda g=g, gd=gd: self.tool("gamedata.py", "--game", g, "--iso", self.iso(g), "--out", gd), g)
            self.step(f"{g}-enemies", [os.path.join(gd, "enemies.json")],
                      lambda g=g, gd=gd: self.tool("enemydata.py", "--game", g, "--iso", self.iso(g), "--out", gd), g)
            self.step(f"{g}-missions", [os.path.join(self.nbl(g), "missions.json")], lambda g=g: self.missions(g), g)
            self.step(f"{g}-drops", [os.path.join(gd, "drops.json")],
                      lambda g=g, gd=gd: self.tool("dropdata.py", "--game", g, "--iso", self.iso(g), "--out", gd), g)
            self.step(f"{g}-economy", [os.path.join(gd, n + ".json") for n in ("grind", "probo", "synthesis",
                                                                                  "lumilass")],
                      lambda g=g, gd=gd: self.tool("econdata.py", "--game", g, "--iso", self.iso(g), "--out", gd), g)
            if g == "aoti":
                self.step("aoti-items", [os.path.join(gd, "items.json")], self.aoti_items, g, *self.discs)
            self.step(f"{g}-pa", [os.path.join(gd, "pa.json"), os.path.join(gd, "paparam.json")],
                      lambda g=g, gd=gd: (self.tool("patables.py", "--game", g, os.path.join(gd, "pa.json")),
                                          self.tool("paparams.py", "--game", g, os.path.join(gd, "paparam.json"))), g)
            self.step(f"{g}-parts", [os.path.join(gd, "parts.json"), os.path.join(gd, "ppregen.json")],
                      lambda g=g, gd=gd: (self.tool("partsdata.py", "--game", g, "--out", gd),
                                          self.tool("ppdata.py", "--game", g, "--gamedata", gd)), g)
            self.step(f"{g}-types", [self.retail(g)],
                      lambda g=g: self.tool("gamedata.py", "--game", g, "--iso", self.iso(g), "--plylevel-only",
                                            "--type-overrides", os.path.join(HERE, "overlay", f"{g}_type_overrides.json"),
                                            "--out", os.path.dirname(self.retail(g))), g)
        self.step("types", [os.path.join(self.gamedata(g), "plylevel.json") for g in games], self.types, *games)
        for g in games:
            self.step(f"{g}-lobbies", [os.path.join(self.nbl(g), "zone-1100000-0.nbl")], lambda g=g: self.lobbies(g), g)
        egs_out = os.path.join(self.work, "egs", "out")
        nd = self.nbl("psu")
        self.step("egs", [os.path.join(nd, f"quest-{q}.nbl") for q in egs.QUESTS]
                  + [os.path.join(nd, "zone-1100000-0.egs.nbl")]
                  + [os.path.join(self.out, "captures", c) for c in egs.CAPTURES], lambda: self.egs(egs_out), egs.COMMIT)
        self.step("seasons", [os.path.join(nd, "season-staging")], self.seasons, *games)

    def disc(self, g):
        d = os.path.join(self.work, g, "disc")
        self.tool("iso.py", "x", self.iso(g), d, "--skip", "CDPSU0.AFS,CDPSU1.AFS,PSUROFS.CVM")
        self.tool("erx.py", "unpack", d, os.path.join(self.work, g, "erx"))

    def missions(self, g):
        out = self.nbl(g)
        os.makedirs(out, exist_ok=True)
        print(self.tool("missiondata.py", "--game", g, "--iso", self.iso(g), "--out", out,
                        "--enemies", os.path.join(self.gamedata(g), "enemies.json")).strip().splitlines()[-1])

    def aoti_items(self):
        """AotI's items.json again now that drops.json and XF_BOARDPARAM.NBL are there (ids only its drop tables
        and synthesis boards name), with PSU's ids added as "other_disc" when the PSU disc is built too
        (characters move between the discs). The economy tables come out the same either way."""
        merge = ["--merge", os.path.join(self.gamedata("psu"), "items.json")] if "psu" in self.discs else []
        self.tool("itemdb.py", "--game", "aoti", *merge, "--json", os.path.join(self.gamedata("aoti"), "items.json"))

    def retail(self, g):
        return os.path.join(self.work, g, "gamedata-retail", "plylevel.json")

    def types(self):
        """Retail type tables: with both discs, PSU's types 0..9 take AotI's era (retail_types.py); one disc
        alone keeps its own."""
        if len(self.discs) == 2:
            self.tool("retail_types.py", "build", "--psu", self.retail("psu"), "--aoti", self.retail("aoti"),
                      "--out", self.gamedata("psu"))
            return
        g = next(iter(self.discs))
        shutil.copyfile(self.retail(g), os.path.join(self.gamedata(g), "plylevel.json"))

    def lobbies(self, g):
        """The disc's offline lobby zones under the online quest / zone names the server looks up."""
        cvm = os.path.join(self.work, g, "cvm", "lobbies")
        afs = os.path.join(cvm, LOBBY_AFS[g])
        if not os.path.exists(afs):
            self.tool("cvm.py", "x", self.iso(g), cvm, "--match", LOBBY_AFS[g],
                      "--erx", os.path.join(self.work, g, "erx", "PSU_US.erx"))
        src = afs[:-4]
        if not os.path.exists(os.path.join(src, "quest_ae.nbl")):
            self.tool("nbl.py", "afs", afs, src)
        print("  " + self.tool("lobbies.py", g, src, self.nbl(g)).strip().replace("\n", "\n  "))

    def egs(self, egs_out):
        src = self.egs_src or egs.fetch(self.work)
        shutil.rmtree(egs_out, ignore_errors=True)
        egs.build(src, egs_out)
        nd = self.nbl("psu")
        os.makedirs(nd, exist_ok=True)
        for q in egs.QUESTS:
            shutil.copyfile(os.path.join(egs_out, f"quest-{q}.nbl"), os.path.join(nd, f"quest-{q}.nbl"))
        # the server serves the native Colony zone; EGS's is kept for its season decorations (seasonbuild.py)
        shutil.copyfile(os.path.join(egs_out, "zone-1100000-0.nbl"), os.path.join(nd, "zone-1100000-0.egs.nbl"))
        cap = os.path.join(self.out, "captures")
        os.makedirs(cap, exist_ok=True)
        for c in egs.CAPTURES:
            shutil.copyfile(os.path.join(src, "p", c), os.path.join(cap, c))

    def seasons(self):
        g = next(g for g in ("psu", "aoti") if g in self.discs)
        cvm = os.path.join(self.work, g, "cvm", "season")
        if not glob.glob(os.path.join(cvm, "XF_OB_*.NBL")):
            self.tool("cvm.py", "x", self.iso(g), cvm, "--match", "XF_OB_",
                      "--erx", os.path.join(self.work, g, "erx", "PSU_US.erx"))
        nd = self.nbl("psu")
        out = os.path.join(nd, "season-staging")
        shutil.rmtree(out, ignore_errors=True)
        print("  " + self.tool("seasonbuild.py", "--nbl", nd, "--out", out).strip().replace("\n", "\n  "))
