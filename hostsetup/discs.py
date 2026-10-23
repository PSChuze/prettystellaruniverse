"""Find the host's Phantasy Star Universe disc images: in the usual folders, in PCSX2's game folders,
or where they point us.

A disc is told apart by the boot file its SYSTEM.CNF names (BOOT2 = cdrom0:\\SLUS_211.94;1), read
from the image's ISO9660 directory, so file names do not matter. Only 2048-byte-sector images
(.iso) are read; a .bin/.cue or a compressed image (.chd, .cso) has to be turned into an .iso first.
"""
import os
import re
import struct
import sys

SECTOR = 2048
# The two US PS2 discs, by boot file. The Redump images' sizes (patches/bypass.json "source") tell
# a retail image apart from a rebuilt one; tools/bypass.py only patches the retail ones.
GAMES = {
    "SLUS_211.94": ("psu", "Phantasy Star Universe", 4251156480),
    "SLUS_216.31": ("aoti", "Ambition of the Illuminus", 3703570432),
}
NAMES = {key: title for key, title, _ in GAMES.values()}
IMAGE_EXT = (".iso",)
MIN_IMAGE = 1 << 30         # both discs are DVDs of 3.7 GB and more
SKIP_DIRS = {"node_modules", "appdata", "library", "$recycle.bin", "windows", "program files",
             "program files (x86)", "system volume information", ".git", ".cache", "__pycache__"}
_BOOT = re.compile(rb"BOOT2\s*=\s*cdrom0:\\?\\?([A-Z]{4}_\d{3}\.\d{2})", re.I)


class Disc:
    """One disc image found: path, game ("psu" or "aoti") and whether it has the retail size."""

    def __init__(self, path, game, retail=True):
        self.path, self.game, self.retail = path, game, retail

    def __repr__(self):
        return "Disc(%r, %r%s)" % (self.path, self.game, "" if self.retail else ", retail=False")

    def __eq__(self, other):
        return (self.path, self.game, self.retail) == (other.path, other.game, other.retail)

    def __hash__(self):
        return hash((self.path, self.game))

    @property
    def title(self):
        return NAMES[self.game]

    def shown(self):
        return "%s: %s" % (self.title, self.path)


def _dir_records(data):
    """(name, extent, size) of each record in an ISO9660 directory's bytes."""
    off = 0
    while off < len(data):
        n = data[off]
        if n == 0:                          # records never span a sector: skip to the next one
            off = (off // SECTOR + 1) * SECTOR
            continue
        extent, size = struct.unpack_from("<I", data, off + 2)[0], struct.unpack_from("<I", data, off + 10)[0]
        name_len = data[off + 32]
        name = data[off + 33:off + 33 + name_len].decode("ascii", "replace")
        yield name, extent, size
        off += n


def boot_file(f):
    """The boot file SYSTEM.CNF names in the open image f, e.g. "SLUS_211.94", or ""."""
    f.seek(16 * SECTOR)
    pvd = f.read(SECTOR)
    if len(pvd) < 190 or pvd[1:6] != b"CD001" or pvd[0] != 1:
        return ""
    root_extent, root_size = struct.unpack_from("<I", pvd, 156 + 2)[0], struct.unpack_from("<I", pvd, 156 + 10)[0]
    f.seek(root_extent * SECTOR)
    for name, extent, size in _dir_records(f.read(min(root_size, 64 * SECTOR))):
        if name.upper().split(";")[0] == "SYSTEM.CNF":
            f.seek(extent * SECTOR)
            m = _BOOT.search(f.read(min(size, SECTOR)))
            return m.group(1).decode("ascii").upper() if m else ""
    return ""


def identify(path):
    """[Disc] for a PSU or AotI image at path, else []."""
    if not path.lower().endswith(IMAGE_EXT):
        return []
    try:
        with open(path, "rb") as f:
            boot = boot_file(f)
        size = os.path.getsize(path)
    except (OSError, struct.error, IndexError):
        return []
    if boot not in GAMES:
        return []
    game, _, retail_size = GAMES[boot]
    return [Disc(path, game, size == retail_size)]


def scan(folder, depth=3, limit=20000):
    """Every PSU disc under folder, looking `depth` folders down and at most `limit` entries."""
    found, seen = [], 0
    stack = [(folder, 0)]
    while stack and seen < limit:
        d, level = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in sorted(entries, key=lambda e: e.name.lower()):
            seen += 1
            try:
                if e.is_dir(follow_symlinks=False):
                    if level < depth and not e.name.startswith(".") and e.name.lower() not in SKIP_DIRS:
                        stack.append((e.path, level + 1))
                elif e.is_file() and e.name.lower().endswith(IMAGE_EXT) and e.stat().st_size >= MIN_IMAGE:
                    found += identify(e.path)
            except OSError:
                continue
    return found


def pcsx2_configs(home=None, platform=None, env=None):
    """The PCSX2.ini files this user may have."""
    home = home or os.path.expanduser("~")
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if platform.startswith("win"):
        bases = [os.path.join(home, "Documents", "PCSX2")]
    elif platform == "darwin":
        bases = [os.path.join(home, "Library", "Application Support", "PCSX2")]
    else:
        config = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        bases = [os.path.join(config, "PCSX2"),
                 os.path.join(home, ".var", "app", "net.pcsx2.PCSX2", "config", "PCSX2")]
    return [os.path.join(b, "inis", "PCSX2.ini") for b in bases]


def pcsx2_game_folders(ini_text):
    """The game list folders of a PCSX2.ini ([GameList] Paths and RecursivePaths)."""
    return [m.group(1).strip() for m in re.finditer(r"(?m)^\s*(?:Recursive)?Paths\s*=\s*(.+?)\s*$", ini_text)
            if m.group(1).strip()]


def likely_folders(extra=(), home=None, platform=None, env=None):
    """Where discs usually are: the given folders first, then Downloads, Documents, Desktop, Games
    and PCSX2's game folders. Only folders that exist."""
    home = home or os.path.expanduser("~")
    folders = list(extra) + [os.path.join(home, n) for n in ("Downloads", "Documents", "Desktop", "Games")]
    for ini in pcsx2_configs(home, platform, env):
        try:
            with open(ini, encoding="utf-8", errors="replace") as f:
                folders += pcsx2_game_folders(f.read())
        except OSError:
            pass
    out, seen = [], set()
    for d in folders:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen and os.path.isdir(d):
            seen.add(key)
            out.append(d)
    return out


def search(folders, depth=3):
    """Scan the folders in order; each disc file is listed once."""
    found, seen = [], set()
    for d in folders:
        for disc in scan(d, depth):
            key = os.path.normcase(os.path.abspath(disc.path))
            if key not in seen:
                seen.add(key)
                found.append(disc)
    return found


def _rank(disc):
    # A retail-sized image first, and of those one that is not named as patched.
    name = os.path.basename(disc.path).lower()
    return (not disc.retail, "online" in name or "patched" in name)


def pick(found):
    """{"psu": Disc, "aoti": Disc} from what was found, the best image per game (either may be
    missing)."""
    chosen = {}
    for game in ("psu", "aoti"):
        discs = sorted((d for d in found if d.game == game), key=_rank)
        if discs:
            chosen[game] = discs[0]
    return chosen
