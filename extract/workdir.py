"""Where the tools keep what they unpack from the discs: --work DIR, else $PSU_EXTRACT_WORK, else ./work.

`python -m extract` lays the directory out per disc, and the tools find their inputs there:
  <work>/<game>/disc   iso.py x: the files outside the big archives (ERX/, SYSTEM.CNF, the boot ELF)
  <work>/<game>/erx    erx.py unpack: PSU_US.erx (CVM password, Blowfish tables, the client's tables)
  <work>/<game>/cvm    cvm.py x: the PSUROFS.CVM files the tools read, unpacked next to them
<game> is psu (SLUS-21194) or aoti (SLUS-21631). The tools that need the disc itself take --iso.
"""
import os
import sys

GAMES = ("psu", "aoti")


def work():
    """The work directory. A --work DIR on the command line is taken out of sys.argv; the result goes to
    $PSU_EXTRACT_WORK as an absolute path, so child tools (run from their own directory) find the same one."""
    a = sys.argv
    if "--work" in a[1:]:
        i = a.index("--work", 1)
        if i + 1 >= len(a):
            raise SystemExit("--work needs a directory")
        os.environ["PSU_EXTRACT_WORK"] = a[i + 1]
        del a[i:i + 2]
    os.environ["PSU_EXTRACT_WORK"] = os.path.abspath(os.environ.get("PSU_EXTRACT_WORK") or "work")
    return os.environ["PSU_EXTRACT_WORK"]


def path(game, *parts):
    """<work>/<game>/<parts>."""
    return os.path.join(work(), game, *parts)


def erx(game):
    return path(game, "erx", "PSU_US.erx")


def any_erx():
    """A game module for the tables both discs share (the NBL Blowfish tables): PSU's, else AotI's."""
    for game in GAMES:
        if os.path.exists(erx(game)):
            return erx(game)
    return erx("psu")


def iso_arg(argv):
    """The value of --iso, or None: the tools only open the disc for files the work directory lacks."""
    return argv[argv.index("--iso") + 1] if "--iso" in argv else None


def need_iso(iso):
    """iso, or exit with a hint when a tool has to read the disc and was not given one."""
    if not iso:
        raise SystemExit("--iso <disc image> is needed: the work directory does not have these files yet")
    return iso
