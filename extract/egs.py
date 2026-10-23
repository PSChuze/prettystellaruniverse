"""EGS (github.com/essen/egs, AGPL-3.0): fetched at build time at a pinned commit, never shipped here.

EGS is an Erlang server for the PC release by Loic Hoguin. Its quest compiler builds the Colony lobby quests
and the season decorations' set file the PS2 server serves, and its p/ directory has two captured tables
(ppcube.bin, packet170c.bin). egs-build.sh compiles the parts that build the NBLs and runs them.
"""
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request

COMMIT = "ceea04c3b438cee9a8470f2abc6297429bf347c5"
URL = f"https://codeload.github.com/essen/egs/tar.gz/{COMMIT}"
IMAGE = "erlang:26"
HERE = os.path.dirname(os.path.abspath(__file__))
QUESTS = (0, 1100000, 1101000, 1102000, 1103000, 1104000)
CAPTURES = ("ppcube.bin", "packet170c.bin")


def fetch(work):
    """<work>/egs/egs-<commit>: the source tree at COMMIT, downloaded once."""
    dest = os.path.join(work, "egs", f"egs-{COMMIT}")
    if os.path.exists(os.path.join(dest, "apps", "egs", "src", "egs_quests_db.erl")):
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tgz = dest + ".tar.gz"
    print(f"  fetching EGS {COMMIT[:12]} from GitHub")
    with urllib.request.urlopen(URL, timeout=120) as r, open(tgz + ".part", "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(tgz + ".part", tgz)
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    with tarfile.open(tgz) as t:
        if hasattr(tarfile, "data_filter"):
            t.extractall(tmp, filter="data")
        else:
            for m in t.getmembers():
                if m.name.startswith(("/", "..")) or ".." in m.name.split("/") or m.issym() or m.islnk():
                    raise SystemExit(f"{tgz}: unexpected member {m.name}")
            t.extractall(tmp)
    top = os.listdir(tmp)
    if len(top) != 1:
        raise SystemExit(f"{tgz}: expected one top directory, found {top}")
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(os.path.join(tmp, top[0]), dest)
    os.rmdir(tmp)
    os.remove(tgz)
    return dest


def build(src, out):
    """Run egs-build.sh: here when erl, erlc and gcc are installed (the extract image), else in IMAGE."""
    os.makedirs(out, exist_ok=True)
    script = os.path.join(HERE, "egs-build.sh")
    if all(shutil.which(x) for x in ("sh", "erl", "erlc", "gcc")):
        env = dict(os.environ, EGS=src, OUT=out)
        subprocess.run(["sh", script], check=True, env=env)
        return
    if not shutil.which("docker"):
        raise SystemExit("the EGS step needs Erlang (erl, erlc) and gcc, or Docker to run it in the "
                         f"{IMAGE} image; or run python -m extract in the extract image")
    name = f"psupub-extract-egs-{os.getpid()}"
    subprocess.run(["docker", "run", "--rm", "--name", name,
                    "-v", f"{os.path.abspath(src)}:/egs:ro", "-v", f"{os.path.abspath(out)}:/out",
                    "-v", f"{HERE}:/extract:ro", "-e", "EGS=/egs", "-e", "OUT=/out",
                    IMAGE, "sh", "/extract/egs-build.sh"], check=True)


def main():
    a = sys.argv[1:]
    if len(a) != 2:
        raise SystemExit("usage: python egs.py <work dir> <out dir>   fetch EGS and build its lobby NBLs")
    build(fetch(a[0]), a[1])


if __name__ == "__main__":
    main()
