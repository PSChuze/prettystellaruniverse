"""Free disk space for the server: what it needs, where, and how much there is.

Two places count: the server folder (the game data, backups, logs) and Docker's own
storage (the images, the extract cache volume). On Docker Desktop that storage is a
disk image on the computer's drive, which grows into the drive's free space up to the
disk image's own limit.
"""
import json
import os
from collections import namedtuple

GB = 1 << 30
MB = 1 << 20

# The server folder.
DATA = 100 * MB             # data/: about 60 MB built from both discs
BACKUPS = 300 * MB          # a few backups of the accounts
FOLDER_MARGIN = 500 * MB    # logs, and room to work
FOLDER = DATA + BACKUPS + FOLDER_MARGIN

# Docker's storage.
CACHE = 1 * GB              # the extract-cache volume: the unpacked discs and EGS, about 750 MB
BUILDER = 2500 * MB         # the extract image (erlang:26 with the tools), 2.4 GB
IMAGES = 700 * MB           # the server program, the SSL terminator and OpenLobby's DNS
DOCKER_MARGIN = 1 * GB      # the build's temporary files, and room for Docker to work
DOCKER = IMAGES + DOCKER_MARGIN

# Where Docker Desktop keeps its disk image unless moved in its settings.
DESKTOP_KEYS = ("customwsldistrodir", "datafolder")

Place = namedtuple("Place", "what path label free need")


def short(place):
    """True when setup knows there is too little space there."""
    return place.free is not None and place.free < place.need


def size(n):
    """"16 GB", "3.4 GB"."""
    n = n / GB
    return "%d GB" % round(n) if n >= 10 else "%.1f GB" % n


def need(n):
    """A need, rounded up: "2 GB", "12 GB"."""
    return "%d GB" % -(-n // GB)


def existing(path):
    """path, or the nearest folder above it that exists."""
    path = os.path.abspath(path)
    while not os.path.exists(path):
        up = os.path.dirname(path)
        if up == path:
            break
        path = up
    return path


def free_at(path, usage=None):
    """Free bytes on the drive holding path, or None."""
    try:
        return (usage or _usage)(existing(path)).free
    except OSError:
        return None


def _usage(path):
    import shutil
    return shutil.disk_usage(path)


def drive_label(path, windows=None):
    """"C:" on Windows, else the path itself."""
    windows = os.name == "nt" if windows is None else windows
    drive = os.path.splitdrive(path)[0] if windows else ""
    return drive.upper() if drive else path


def parse_df(text):
    """Free bytes from `df -k` (or -kP) output, or None."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 2 or not lines[0].lower().startswith("filesystem"):
        return None
    words = " ".join(lines[1:]).split()     # a long device name wraps onto its own line
    try:
        return int(words[3]) * 1024
    except (IndexError, ValueError):
        return None


def desktop_disk_dir(settings_texts, system, environ=None, home=None):
    """The folder holding Docker Desktop's disk image: the one its settings name, else
    the usual one. settings_texts: the contents of its settings files (newest first)."""
    environ = os.environ if environ is None else environ
    home = home or os.path.expanduser("~")
    for text in settings_texts:
        try:
            settings = json.loads(text)
        except ValueError:
            continue
        if not isinstance(settings, dict):
            continue
        lower = {str(k).lower(): v for k, v in settings.items()}
        for key in DESKTOP_KEYS:
            value = lower.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if system == "windows":
        local = environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(local, "Docker", "wsl")
    if system == "mac":
        return os.path.join(home, "Library", "Containers", "com.docker.docker", "Data", "vms", "0", "data")
    return None


def desktop_settings_files(system, environ=None, home=None):
    """Docker Desktop's settings files, newest name first."""
    environ = os.environ if environ is None else environ
    home = home or os.path.expanduser("~")
    if system == "windows":
        roaming = environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        base = os.path.join(roaming, "Docker")
    elif system == "mac":
        base = os.path.join(home, "Library", "Group Containers", "group.com.docker")
    else:
        return []
    return [os.path.join(base, "settings-store.json"), os.path.join(base, "settings.json")]


def read_texts(paths):
    out = []
    for path in paths:
        try:
            with open(path, encoding="utf-8-sig") as f:
                out.append(f.read())
        except OSError:
            pass
    return out


def least(*values):
    known = [v for v in values if v is not None]
    return min(known) if known else None
