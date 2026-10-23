"""PSU Server Setup: python setup.py (or the "PSU Server Setup" program)

The first run asks three questions (who plays, where the discs are, the server's name), writes
.env, builds the game data from the discs, starts the server in Docker and checks it. Later runs
show a menu: start and stop, settings, update, backups, pairing with an OpenLobby admin panel,
patched disc images for players. Needs Docker and nothing else (Python 3.8+ when run as a script,
standard library only).
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from hostsetup import discs as disclib
from hostsetup import progress as proglib
from hostsetup import upnp

REPO = os.environ.get("PSU_SETUP_REPO", "PSChuze/prettystellaruniverse")
RELEASES = "https://github.com/%s/releases" % REPO
API = os.environ.get("PSU_SETUP_API", "https://api.github.com")
DOCKER_DOWNLOAD = "https://www.docker.com/products/docker-desktop/"
DOCKER_LINUX = "https://docs.docker.com/engine/install/"
PORT_HELP = "https://portforward.com/router.htm"
DEFAULT_FOLDER = "PSU Server"
DEFAULT_NAME = "PSU Server"
FROZEN = getattr(sys, "frozen", False)
PROJECT = "psu"
HOW_TO_UPDATE = "Run PSU Server Setup and choose Update."

# The names the discs look up. A console finds the server when its DNS answers these with it.
GAME_NAMES = ("loginps2.psuus.segaonline.jp", "patchps2.psuus.segaonline.jp",
              "loginps2.ipsuus.segaonline.jp", "patchps2.ipsuus.segaonline.jp")
# What players reach, as (setting, default port, protocol, what). The discs use the defaults;
# the settings only move them for tests (compose.yml publishes ${setting:-default}).
PORTS = (("PSU_PORT_PATCH_PSU", 11110, "TCP", "PSU patch"),
         ("PSU_PORT_PATCH_AOTI", 11140, "TCP", "AotI patch"),
         ("PSU_PORT_LOGIN_PSU", 12110, "TCP", "PSU login"),
         ("PSU_PORT_LOGIN_AOTI", 12140, "TCP", "AotI login"),
         ("PSU_PORT_GAME", 12061, "TCP", "game"),
         ("PSU_PORT_DNS", 53, "UDP", "DNS"))
SERVICES = ("patch", "login", "game", "ssl3-psu", "ssl3-aoti")
# Files of ours in the server folder that an update must never replace.
KEEP = (".env", "compose.override.yml", "state", "data", "backups", "logs", "discs")
PANEL_ROLE = "org.openlobby.role=panel"
PANEL_URL_LABEL = "org.openlobby.panel.url"
PANEL_ADMIN_URL = "http://psu:22191"
GAME_TITLE = "Phantasy Star Universe"


def use_root(path):
    """Point every path at the server folder `path`."""
    global ROOT, ENV, ENV_EXAMPLE, BACKUPS, CHOICES, LOG, MANIFEST, VERSION_FILE, UPDATE_FILE
    ROOT = os.path.abspath(path)
    ENV = os.path.join(ROOT, ".env")
    ENV_EXAMPLE = os.path.join(ROOT, "psu.env.example")
    BACKUPS = os.path.join(ROOT, "backups")
    CHOICES = os.path.join(ROOT, "state", "setup.json")
    LOG = os.path.join(ROOT, "logs", "setup.log")
    MANIFEST = os.path.join(ROOT, ".setup-files")
    VERSION_FILE = os.path.join(ROOT, ".setup-version")
    UPDATE_FILE = os.path.join(ROOT, "state", "update.json")


use_root(os.path.dirname(os.path.abspath(__file__)))


# --- terminal ----------------------------------------------------------------------------

def say(text=""):
    print(text, flush=True)


def ask(question, default=""):
    shown = " [%s]" % default if default else ""
    try:
        answer = input("%s%s: " % (question, shown)).strip()
    except EOFError:
        # No one to answer (input closed): stop rather than ask forever.
        raise SystemExit("Setup needs its questions answered in this window.")
    return answer or default


def ask_yes(question, default=True):
    answer = ask(question + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not answer else answer.startswith("y")


def ask_choice(question, options, default=1):
    """Number options from 1 and return the 1-based pick."""
    if question:
        say(question)
    for i, text in enumerate(options, 1):
        say("  %d. %s" % (i, text))
    while True:
        answer = ask("Type a number", str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        say("  Type one of the numbers above.")


def heading(text):
    say()
    say(text)
    say("-" * len(text))


def log_file():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    f = open(LOG, "a", encoding="utf-8")
    f.write("\n=== %s ===\n" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return f


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the server's files -------------------------------------------------------------------

def find_root(args_folder=None):
    """The server folder: the one given, else the folder this runs from when it holds the server,
    else "PSU Server" in the home folder (made when missing)."""
    if args_folder:
        return os.path.abspath(args_folder)
    if not FROZEN:
        return os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.dirname(sys.executable), os.getcwd()):
        if os.path.exists(os.path.join(d, "compose.yml")):
            return d
    return os.path.join(os.path.expanduser("~"), DEFAULT_FOLDER)


def bundled(name):
    """A file packed into the setup program, or None."""
    base = getattr(sys, "_MEIPASS", None)
    path = os.path.join(base, name) if base else None
    return path if path and os.path.exists(path) else None


def zip_commit(archive):
    """The commit a `git archive` zip was made from (git writes it as the zip's comment), or ""."""
    try:
        with zipfile.ZipFile(archive) as z:
            comment = z.comment.decode("ascii", "replace").strip()
    except (OSError, zipfile.BadZipFile):
        return ""
    return comment if re.fullmatch(r"[0-9a-f]{40}", comment) else ""


def unpack_server(archive, root, version=None):
    """Unpack a source zip of this repository into root, replacing the files the last one put
    there and leaving the host's own (.env, state, data, ...) alone. version ({"tag", "commit",
    "date"}) goes to .setup-version. Returns the paths it wrote, relative to root."""
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        # GitHub's and git archive's zips put everything in one top folder.
        tops = {n.split("/", 1)[0] for n in z.namelist()}
        strip = len(tops) == 1 and all("/" in n for n in names)
        old = set(read_manifest(root))
        written = []
        for name in names:
            rel = name.split("/", 1)[1] if strip else name
            if not rel or rel.split("/", 1)[0] in KEEP:
                continue
            target = os.path.normpath(os.path.join(root, rel))
            if not target.startswith(os.path.normpath(root) + os.sep):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(name) as src, open(target + ".tmp", "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(target + ".tmp", target)
            written.append(rel)
    for rel in old - set(written):
        try:
            os.remove(os.path.join(root, rel))
        except OSError:
            pass
    with open(os.path.join(root, ".setup-files"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sorted(written)) + "\n")
    if version is not None:
        version = dict(version)
        version.setdefault("commit", zip_commit(archive))
        with open(os.path.join(root, ".setup-version"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(version, f)
            f.write("\n")
    return written


def read_manifest(root):
    try:
        with open(os.path.join(root, ".setup-files"), encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except OSError:
        return []


def installed_version():
    """{"tag", "commit", "date"} of the files in the server folder (what is known of them)."""
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            v = json.load(f)
    except (OSError, ValueError):
        v = {}
    if not v.get("commit") and os.path.isdir(os.path.join(ROOT, ".git")):
        out = run(["git", "log", "-1", "--format=%H %cI"], check=False, capture=True).stdout or ""
        if re.match(r"[0-9a-f]{40} ", out):
            v["commit"], v["date"] = out.split()[:2]
    return v


def source_archive():
    """The server's files to unpack: the zip packed into the setup program, or, run as a script
    from a checkout, its committed files (git archive). Returns (path, version) or (None, None)."""
    archive = bundled("server.zip")
    if archive:
        version = {}
        if bundled("server-version.txt"):
            # packaging/build_setup.py writes the release tag and the commit, one per line.
            with open(bundled("server-version.txt"), encoding="utf-8") as f:
                words = f.read().split() + ["", ""]
            version = {"tag": words[0], "commit": words[1]} if words[1] else {"tag": words[0]}
        return archive, version
    here = os.path.dirname(os.path.abspath(__file__))
    if FROZEN or not os.path.isdir(os.path.join(here, ".git")) and not os.path.isfile(os.path.join(here, ".git")):
        return None, None
    path = os.path.join(tempfile.mkdtemp(prefix="psu-setup-"), "server.zip")
    if subprocess.run(["git", "-C", here, "archive", "--format=zip", "--prefix=psu-server/", "-o", path, "HEAD"]
                      ).returncode != 0:
        return None, None
    return path, {"tag": "dev"}


def ensure_server_files():
    """Put the server's files in ROOT when they are not there yet."""
    if os.path.exists(os.path.join(ROOT, "compose.yml")):
        return True
    archive, version = source_archive()
    if not archive:
        say("The server's files are not in %s, and this setup program does not carry them." % ROOT)
        say("Download PSU Server Setup again from %s" % RELEASES)
        return False
    os.makedirs(ROOT, exist_ok=True)
    unpack_server(archive, ROOT, version)
    say("The server's files are in %s" % ROOT)
    return True


def tree_digest(*folders):
    """One hash over every file under these folders of ROOT, to tell whether an update changed
    the game data builder."""
    h = hashlib.sha256()
    for folder in folders:
        for base, dirs, files in os.walk(os.path.join(ROOT, folder)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                path = os.path.join(base, name)
                h.update(os.path.relpath(path, ROOT).replace("\\", "/").encode())
                with open(path, "rb") as f:
                    h.update(hashlib.sha256(f.read()).digest())
    return h.hexdigest()


# --- docker ------------------------------------------------------------------------------

def run(cmd, check=True, capture=False, **kw):
    """Run a command in the server folder; its output goes to the terminal unless captured."""
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    result = subprocess.run(cmd, cwd=ROOT if os.path.isdir(ROOT) else None, **kw)
    if check and result.returncode != 0:
        raise SystemExit("Failed (exit %d): %s" % (result.returncode, " ".join(cmd)))
    return result


def compose_cmd(*args):
    return ["docker", "compose", "-p", PROJECT] + list(args)


def compose(*args, **kw):
    return run(compose_cmd(*args), **kw)


def add_docker_to_path():
    """Docker installed while this window was open is not on its PATH yet."""
    places = [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Docker", "Docker", "resources",
                           "bin"),
              "/usr/local/bin", "/Applications/Docker.app/Contents/Resources/bin",
              os.path.join(os.path.expanduser("~"), ".docker", "bin")]
    path = os.environ.get("PATH", "").split(os.pathsep)
    for p in places:
        if os.path.isdir(p) and p not in path:
            path.append(p)
    os.environ["PATH"] = os.pathsep.join(path)


def docker_state():
    """"ok", "missing", "stopped", "denied" (Linux user not allowed) or "old"."""
    add_docker_to_path()
    if not shutil.which("docker"):
        return "missing"
    try:
        info = run(["docker", "info"], check=False, capture=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "stopped"
    if info.returncode != 0:
        return "denied" if "permission denied" in (info.stdout or "").lower() else "stopped"
    if run(["docker", "compose", "version"], check=False, capture=True).returncode != 0:
        return "old"
    return "ok"


def start_docker_desktop():
    """Ask Docker Desktop to start; True when there was one to start."""
    if os.name == "nt":
        exe = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Docker", "Docker",
                           "Docker Desktop.exe")
        if os.path.exists(exe):
            subprocess.Popen([exe], close_fds=True)
            return True
    elif sys.platform == "darwin" and os.path.isdir("/Applications/Docker.app"):
        return subprocess.run(["open", "-a", "Docker"]).returncode == 0
    return False


def wait_for_docker(wait=True):
    """Return once Docker works. Explains what is missing and waits for the host to fix it (or
    returns False at once with wait=False)."""
    started = False
    while True:
        state = docker_state()
        if state == "ok":
            return True
        say()
        if state == "missing":
            if sys.platform.startswith("linux"):
                say("Docker is not installed. Install Docker Engine: %s" % DOCKER_LINUX)
            else:
                say("Docker Desktop is not installed. This server runs inside it.")
                say("Download it here, install it and start it: %s" % DOCKER_DOWNLOAD)
                say("If it asks you to sign in, you can skip that.")
        elif state == "stopped" and not started and wait and start_docker_desktop():
            started = True
            say("Starting Docker Desktop. This can take a minute or two.")
            for _ in range(60):
                time.sleep(3)
                if docker_state() == "ok":
                    say("Docker Desktop is running.")
                    return True
            say("Docker Desktop has not started yet. If it shows a message (terms to accept,")
            say("an update to install), answer it.")
        elif state == "stopped":
            if sys.platform.startswith("linux"):
                say("Docker is installed but not running. Start it with: sudo systemctl start docker")
            else:
                say("Docker Desktop is installed but not running. Open it and wait until it says")
                say("it is running.")
        elif state == "denied":
            say("Your user may not use Docker yet. Run this, then log out and back in:")
            say("  sudo usermod -aG docker $USER")
        else:
            say("This Docker is too old (it has no 'docker compose'). Update it: %s" % DOCKER_DOWNLOAD)
        if not wait:
            return False
        ask("Press Enter to check again (or close this window to stop)")


def docker_desktop():
    out = run(["docker", "info", "--format", "{{.OperatingSystem}}"], check=False, capture=True).stdout
    return "docker desktop" in (out or "").lower()


def network_name():
    return os.environ.get("OPENLOBBY_NETWORK") or read_env().get("OPENLOBBY_NETWORK") or "openlobby"


def ensure_network():
    """The openlobby network compose.yml joins the game to. External and shared by every install
    on this machine, so it is made here when missing and never removed."""
    name = network_name()
    if run(["docker", "network", "inspect", name], check=False, capture=True).returncode != 0:
        # Another setup may make it in between: a failed create is fine then.
        run(["docker", "network", "create", name], check=False, capture=True)


def labelled(role):
    """[(container, compose project, panel url)] of the running containers with
    org.openlobby.role=<role>. With PSU_SETUP_ISOLATED=1 (tests) only this project's."""
    cmd = ["docker", "ps", "--filter", "label=org.openlobby.role=" + role]
    if os.environ.get("PSU_SETUP_ISOLATED") == "1":
        cmd += ["--filter", "label=com.docker.compose.project=" + PROJECT]
    cmd += ["--format", '{{.Names}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "%s"}}' % PANEL_URL_LABEL]
    out = run(cmd, check=False, capture=True).stdout or ""
    return [tuple((line.split("\t") + ["", "", ""])[:3]) for line in out.splitlines() if line.strip()]


def find_dns():
    """(container, ours) of the machine's shared DNS server for consoles, or ("", False)."""
    found = labelled("dns")
    for name, project, _ in found:
        if project == PROJECT:
            return name, True
    return (found[0][0], False) if found else ("", False)


def plan_dns():
    """Before a start: run this folder's dns service only when the host wants the DNS and no
    other install runs one (OpenLobby docs/installs.md: never start a second)."""
    env = read_env()
    want = read_choices().get("dns", True)
    name, ours = find_dns()
    profiles = [p for p in (env.get("COMPOSE_PROFILES") or "").split(",") if p and p != "dns"]
    if want and (ours or not name):
        profiles.append("dns")
    if ",".join(profiles) != (env.get("COMPOSE_PROFILES") or ""):
        write_env({"COMPOSE_PROFILES": ",".join(profiles)})


def register_dns(quiet=False):
    """Add the game's names under the key psu to the running shared DNS. True when it took them."""
    if not read_choices().get("dns", True):
        return True
    name, _ = find_dns()
    if not name:
        say("No DNS server for consoles runs here, so consoles will not find the server.")
        return False
    got = run(["docker", "exec", name, "python", "stub.py", "dns-names", "add", "psu"] + list(GAME_NAMES),
              check=False, capture=True)
    if got.returncode != 0:
        say("The DNS server (%s) did not take the game's names: %s" % (name, (got.stdout or "").strip()[-200:]))
        say("Its OpenLobby image may be too old: docker pull ghcr.io/prettyopenlobby/openlobby, then start")
        say("the server again.")
        return False
    if not quiet:
        say("The DNS server answers the game's names with this server.")
    return True


def unregister_dns():
    name, ours = find_dns()
    if name:
        run(["docker", "exec", name, "python", "stub.py", "dns-names", "remove", "psu"], check=False, capture=True)
    return ours


def prepare_start():
    """What every start needs first: the network, the DNS plan and the bind-mounted folders."""
    ensure_network()
    plan_dns()
    make_folders(read_env())


def running_services():
    out = compose("ps", "--status", "running", "--services", check=False, capture=True).stdout or ""
    return set(out.split())


def server_running():
    return "game" in running_services()


def wait_ready(timeout=120, quiet=False, since=None):
    """Wait until every service runs and answers (checks); False when it does not. A traceback
    logged after `since` (UTC, as utc_now writes it) ends the wait at once."""
    if not quiet:
        say("Waiting for the server to start...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(s in running_services() for s in SERVICES) and all(ok for _, ok in checks(read_env())):
            register_dns(quiet=True)
            return True
        recent = compose("logs", "--since", since, "game", "login", "patch", check=False,
                         capture=True).stdout if since else ""
        if "Traceback" in (recent or ""):
            break
        time.sleep(2)
    say("The server did not start. Its last lines:")
    say(compose("logs", "--tail", "40", check=False, capture=True).stdout)
    return False


def read_hello(port, timeout=5.0):
    """The first bytes a service sends on 127.0.0.1:port (patch and login greet first)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(64)
    except OSError:
        return b""


def ssl3_hello(service, port):
    """Bytes the login sends after an SSLv3 handshake on port, tried from inside the terminator."""
    script = ("timeout 5 /opt/ssl3/bin/openssl s_client -quiet -ign_eof -ssl3 -cipher 'RC4-SHA:@SECLEVEL=0' "
              "-connect 127.0.0.1:%d </dev/null 2>/dev/null | head -c 8 | wc -c" % port)
    out = compose("exec", "-T", service, "sh", "-c", script, check=False, capture=True).stdout or ""
    return out.strip().isdigit() and int(out.strip()) >= 8


def game_listening():
    script = "import socket; socket.create_connection(('127.0.0.1', 22061), 3).close()"
    return compose("exec", "-T", "game", "python", "-c", script, check=False, capture=True).returncode == 0


def checks(env):
    """[(what, ok)] for each part a player's console talks to."""
    return [("PSU patch", len(read_hello(port_of(env, "PSU_PORT_PATCH_PSU"))) >= 8),
            ("AotI patch", len(read_hello(port_of(env, "PSU_PORT_PATCH_AOTI"))) >= 8),
            ("PSU login (SSLv3)", ssl3_hello("ssl3-psu", 12110)),
            ("AotI login (SSLv3)", ssl3_hello("ssl3-aoti", 12140)),
            ("game", game_listening())]


def smoke(quiet=False):
    if not quiet:
        say("Checking the server...")
    results = checks(read_env())
    bad = [what for what, ok in results if not ok]
    if not quiet:
        for what, ok in results:
            say("  %-20s %s" % (what, "answers" if ok else "no answer"))
    say("The server answers as it should." if not bad else
        "No answer from: %s. See the server's log (Show what the server is doing)." % ", ".join(bad))
    return not bad


# --- settings ----------------------------------------------------------------------------

def read_env(path=None):
    path = path or ENV
    values = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*([A-Z_][A-Z0-9_]*)=(.*)$", line.rstrip("\n"))
                if m:
                    values[m.group(1)] = m.group(2).strip()
    return values


def write_env(values, path=None, template=None):
    """Write values into the template's lines (a commented-out `#KEY=` line is switched on, a
    value of None comments a line out); keys the template does not have go at the end. Comments
    stay as they are."""
    path, template = path or ENV, template or ENV_EXAMPLE
    source = path if os.path.exists(path) else template
    with open(source, encoding="utf-8") as f:
        lines = f.read().splitlines()
    left = dict(values)
    out = []
    for line in lines:
        m = re.match(r"(#?)([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(2) in left:
            value = left.pop(m.group(2))
            out.append(("#" + line.lstrip("#")) if value is None else "%s=%s" % (m.group(2), value))
            continue
        out.append(line)
    for key, value in left.items():
        if value is not None:
            out.append("%s=%s" % (key, value))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)


def read_choices():
    try:
        with open(CHOICES, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_choices(choices):
    os.makedirs(os.path.dirname(CHOICES), exist_ok=True)
    with open(CHOICES, "w", encoding="utf-8", newline="\n") as f:
        json.dump(choices, f, indent=1)
        f.write("\n")


def port_of(env, key):
    """A published port: the environment's value (tests move them), .env's, or the default."""
    default = next(p for k, p, *_ in PORTS if k == key)
    value = os.environ.get(key) or env.get(key) or ""
    return int(value) if value.isdigit() else default


def lan_address():
    """This machine's address on the local network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return ""


def public_address():
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=4) as r:
            ip = r.read().decode().strip()
        socket.inet_aton(ip)
        return ip
    except (OSError, ValueError):
        return ""


def ipv4_of(text):
    """The IPv4 address for an address or a name, or "" (the login service needs an address)."""
    try:
        socket.inet_aton(text)
        if text.count(".") == 3:
            return text
    except OSError:
        pass
    try:
        return socket.gethostbyname(text)
    except (OSError, UnicodeError):
        return ""


def who_plays(env, choices):
    """"home" or "internet": as chosen, or guessed from the address."""
    if choices.get("who") in ("home", "internet"):
        return choices["who"]
    return "home" if upnp.is_private(env.get("PSU_GAME_ADDR", "")) else "internet"


def project_containers():
    return ",".join("%s-%s-1" % (PROJECT, s) for s in ("game", "login", "patch", "ssl3-psu", "ssl3-aoti"))


def first_settings(who, address, name, env, linux=None, lan=""):
    """The .env values for the three answers. Everything not asked keeps its value in env (the
    template on a first run) or gets the usual default."""
    linux = sys.platform.startswith("linux") if linux is None else linux
    changes = {"PSU_GAME_ADDR": address, "PSU_MOTD": motd(name),
               "PSU_PANEL_CONTAINERS": project_containers()}
    if not env.get("PSU_PANEL_TOKEN"):
        changes["PSU_PANEL_TOKEN"] = secrets.token_urlsafe(24)
    if linux and hasattr(os, "getuid") and os.getuid() != 0:
        changes.update(PSU_UID=os.getuid(), PSU_GID=os.getgid())
    if linux and (lan or who == "home"):
        # systemd-resolved holds 127.0.0.53:53, so the DNS port goes on the network address only.
        changes["PSU_DNS_BIND"] = lan or address
    return changes


def motd(name):
    return ("Welcome to %s." % name)[:50]


def ports_to_open(env):
    """[(port, protocol)] players over the internet must reach, in order."""
    # Never the DNS port: the shared resolver forwards other names, so it must not face the internet.
    return [(port_of(env, key), protocol) for key, _, protocol, _ in PORTS if key != "PSU_PORT_DNS"]


def write_news(env, server_name, text):
    path = os.path.join(state_dir(env), "news.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# The login notices (server/psu/news.py): one per block, blocks separated by ---\n")
        f.write("Welcome to %s!\n%s\n" % (server_name, text))


def folder_of(env, key, default):
    return os.path.join(ROOT, env.get(key) or default)


def state_dir(env):
    return folder_of(env, "PSU_STATE", "state")


def data_dir(env):
    return folder_of(env, "PSU_DATA", "data")


def has_data(env):
    """True when the game data is built: lobby archives and the game's tables."""
    d = data_dir(env)
    try:
        nbl = any(n.lower().endswith(".nbl") for n in os.listdir(os.path.join(d, "nbl")))
        tables = any(n.endswith(".json") for n in os.listdir(os.path.join(d, "gamedata")))
    except OSError:
        return False
    return nbl and tables


def make_folders(env):
    """Make the bind-mounted folders here first, so Docker does not make them as root."""
    for key, default in (("PSU_DATA", "data"), ("PSU_STATE", "state"), ("PSU_LOGS", "logs")):
        os.makedirs(folder_of(env, key, default), exist_ok=True)
    for sub in ("nbl", "gamedata", "captures"):
        os.makedirs(os.path.join(data_dir(env), sub), exist_ok=True)


# --- the three questions -----------------------------------------------------------------

def ask_who(current="home"):
    heading("Question 1 of 3: Who will play on this server?")
    pick = ask_choice("", ["Just people on my home network", "Friends over the internet"],
                      2 if current == "internet" else 1)
    return "internet" if pick == 2 else "home"


def ask_address(who, current=""):
    """The IPv4 address the server gives players: this machine's for home, the internet one
    otherwise. Returns (address, what the host typed)."""
    if who == "home":
        lan = lan_address()
        if lan:
            say("Players at home reach this computer at %s." % lan)
            return lan, lan
        say("Setup could not find this computer's address on your network.")
        default = current
    else:
        public = public_address()
        default = current if current and not upnp.is_private(current) else public
        if public:
            say("Your internet address is %s." % public)
        if default:
            say("Players reach the server at this address. If you have a name for it (like")
            say("myserver.example.net), you can type the name. Otherwise press Enter.")
        else:
            say("Setup could not find your internet address. Type it, or a name that leads to it.")
    while True:
        typed = ask("Address", default)
        if not typed:
            continue
        address = ipv4_of(typed)
        if address:
            if address != typed:
                say("  %s is %s now. If that address changes, run setup again and choose" % (typed, address))
                say("  Change settings, Who will play.")
            return address, typed
        say("  That is not an address, and setup could not look the name up.")


def find_discs(extra=()):
    """Look in the usual places and offer what is there; ask until at least one disc is known.
    Returns {"psu": Disc, "aoti": Disc} (one of them may be missing)."""
    heading("Question 2 of 3: Where are your Phantasy Star Universe discs?")
    say("Setup needs a disc image (.iso) of Phantasy Star Universe (SLUS-21194), of Ambition of the")
    say("Illuminus (SLUS-21631), or of both. Setup only reads them.")
    places = [ROOT] + ([os.path.dirname(sys.executable)] if FROZEN else []) + list(extra)
    say("Looking in Downloads, Documents, Desktop, Games and PCSX2's game folders...")
    found = disclib.search(disclib.likely_folders(places))
    chosen = disclib.pick(found)
    if chosen:
        show_discs(chosen)
        if ask_yes("Use %s?" % ("these" if len(chosen) == 2 else "this one")):
            if len(chosen) == 2 or not ask_yes("Add the other game's disc too?", False):
                return chosen
        else:
            found, chosen = [], {}
    while True:
        if chosen:
            missing = "aoti" if "psu" in chosen else "psu"
            say("Type the folder (or file) where the %s disc is, or press Enter to go on" % disclib.NAMES[missing])
            say("without it.")
        else:
            say("Type the folder (or file) where the disc images are. You can also drag a disc file")
            say("into this window and press Enter.")
        place = ask("Folder or file").strip().strip('"').strip("'")
        if not place:
            if chosen:
                return chosen
            continue
        place = os.path.abspath(os.path.expanduser(place))
        if os.path.isfile(place):
            new = disclib.identify(place)
        elif os.path.isdir(place):
            new = disclib.search([place], depth=4)
        else:
            say("  There is no folder or file at %s" % place)
            continue
        if not new:
            say("  No Phantasy Star Universe disc image there (a .iso of SLUS-21194 or SLUS-21631).")
            continue
        found = new + found
        chosen = disclib.pick(found)
        show_discs(chosen)
        if len(chosen) == 2:
            return chosen


def show_discs(chosen):
    say("Found:")
    for game in ("psu", "aoti"):
        if game in chosen:
            d = chosen[game]
            say("  %s%s" % (d.shown(), "" if d.retail else " (not the retail image's size)"))


def ask_name(current=DEFAULT_NAME):
    heading("Question 3 of 3: Give your server a name")
    say("Players see it in the notice at login.")
    return ask("Server name", current) or DEFAULT_NAME


def disc_records(chosen):
    """The chosen discs as setup.json keeps them."""
    return [{"path": d.path, "game": d.game, "retail": d.retail} for g, d in sorted(chosen.items())]


# --- discs to game data ------------------------------------------------------------------

def disc_mounts(records):
    """(docker -v options, extract's arguments for the discs): each disc's folder is shared
    read-only."""
    folders, volumes, args = [], [], []
    for item in records:
        folder = os.path.dirname(os.path.abspath(item["path"]))
        if folder not in folders:
            folders.append(folder)
            volumes += ["-v", "%s:/discs/%d:ro" % (folder.replace("\\", "/"), len(folders))]
        args += ["--" + item["game"], "/discs/%d/%s" % (folders.index(folder) + 1, os.path.basename(item["path"]))]
    return volumes, args


def ensure_discs(choices, ask_again=False):
    if not ask_again and choices.get("discs"):
        return
    choices["discs"] = disc_records(find_discs())
    write_choices(choices)


def openlobby_tag():
    return os.environ.get("OPENLOBBY_TAG") or read_env().get("OPENLOBBY_TAG") or "latest"


def have_image(name):
    return run(["docker", "image", "inspect", name], check=False, capture=True).returncode == 0


def build_image(progress, log):
    progress.begin("Getting the server program ready", "a few minutes the first time")
    code, tail = proglib.run_logged(compose_cmd("build", "game"), log, cwd=ROOT, progress=progress)
    if code == 0:
        progress.set("the SSL terminator")
        code, tail = proglib.run_logged(compose_cmd("pull", "--ignore-buildable", "ssl3-psu"), log, cwd=ROOT,
                                        progress=progress)
        if code != 0 and have_image("ghcr.io/prettyopenlobby/openlobby-ssl3:" + openlobby_tag()):
            code = 0                    # offline, or a local tag: the image here will do
    progress.end(code == 0)
    if code != 0:
        failed("Getting the server program ready failed.", tail)
        return False
    version = installed_version()
    write_env({"PSU_BUILD_COMMIT": version.get("commit", ""), "PSU_BUILT_AT": utc_now()})
    return True


def build_game_data(env, choices, progress, log):
    """Run the game data build with a plain progress display. True when it worked."""
    records = [r for r in choices.get("discs", []) if os.path.exists(r["path"])]
    if not records:
        say("The disc images setup used before are gone. Choose Rebuild the game data and pick them again.")
        return False
    make_folders(env)
    progress.begin("Getting the game data builder ready", "a few minutes the first time")
    code, tail = proglib.run_logged(compose_cmd("--profile", "extract", "build", "extract"), log, cwd=ROOT,
                                    progress=progress)
    progress.end(code == 0)
    if code != 0:
        failed("Getting the game data builder ready failed.", tail)
        return False
    volumes, args = disc_mounts(records)
    cmd = compose_cmd("--profile", "extract", "run", "--rm") + volumes + ["extract"] + args + ["--out", "/data"]
    say("Building the game data takes 30 to 40 minutes the first time (it downloads EGS once).")
    progress.begin("Reading your discs", "a few minutes")
    code, tail = proglib.run_logged(cmd, log, on_line=proglib.ExtractWatcher(progress), cwd=ROOT,
                                    progress=progress)
    progress.end(code == 0)
    if code != 0:
        failed("Building the game data failed.", tail)
    elif not has_data(env):
        failed("The game data builder finished but left no game data in %s." % data_dir(env), tail)
        return False
    return code == 0


def failed(what, tail):
    say()
    say(what)
    reason = proglib.explain(tail)
    if reason:
        say(reason)
    say("The last lines it wrote:")
    for line in tail[-12:]:
        say("  " + line)
    say("Everything it wrote is in %s" % LOG)


def start_server(progress, log):
    prepare_start()
    progress.begin("Starting the server", "about a minute")
    since = utc_now()
    code, tail = proglib.run_logged(compose_cmd("up", "-d", "--remove-orphans"), log, cwd=ROOT, progress=progress)
    ok = code == 0 and wait_ready(quiet=True, since=since)
    progress.end(ok)
    if code != 0:
        failed("Starting the server failed.", tail)
    return ok


# --- router ------------------------------------------------------------------------------

def open_router_ports(env, quiet=False):
    """Ask the router to forward the ports. Returns (all open?, the router's internet address or "")."""
    lan = lan_address()
    ports = ports_to_open(env)
    if not quiet:
        say("Asking your router to open the ports...")
    gateway = upnp.find_gateway(lan) if lan else None
    if not gateway:
        if not quiet:
            say("Your router did not answer (its UPnP setting may be off). The ports need opening by hand.")
        return False, ""
    results = upnp.open_ports(gateway, ports, lan)
    try:
        external = gateway.external_ip()
    except upnp.UpnpError:
        external = ""
    good = [p for p, r in results.items() if r in ("opened", "already")]
    bad = {p: r for p, r in results.items() if p not in good}
    if not quiet or bad:
        if not bad:
            say("Your router opened the ports for this computer (%s)." % lan)
        else:
            say("Your router opened %d of %d ports. Not opened:" % (len(good), len(ports)))
            for (port, protocol), r in bad.items():
                say("  %d %s: %s" % (port, protocol, "another device uses it" if r == "taken" else r))
    if external and upnp.is_private(external):
        say()
        say("Note: your router has no internet address of its own (%s). Your internet provider" % external)
        say("probably shares one address between customers, or there is a second router in front")
        say("of this one. Friends outside your home will likely not reach the server. Ask your")
        say("provider for a public IP address.")
    return not bad, external


def close_router_ports(env):
    lan = lan_address()
    gateway = upnp.find_gateway(lan) if lan else None
    if not gateway:
        return
    for port, protocol in ports_to_open(env):
        have = gateway.mapping(port, protocol)
        if have and have[0] == lan:
            try:
                gateway.remove(port, protocol)
            except upnp.UpnpError:
                pass


def show_port_help(env, router=""):
    lan = lan_address() or "this computer"
    heading("Open these ports on your router")
    say("Your router needs to send these ports to this computer (%s)." % lan)
    say("Open the router's settings page (usually http://%s, or the address printed on" % (router or "10.0.0.1"))
    say('the router) and look for "Port forwarding" (or "Virtual server", or "NAT").')
    say()
    say("  Port    Type   To")
    for port, protocol in ports_to_open(env):
        say("  %-7d %-6s %s" % (port, protocol, lan))
    say()
    say("Steps for many router models: %s" % PORT_HELP)


# --- OpenLobby admin panel ----------------------------------------------------------------

def find_panel():
    """The URL of an OpenLobby admin panel running on this machine (its container's
    org.openlobby.panel.url label), or ""."""
    for _, _, url in labelled("panel"):
        if url.strip().startswith("http"):
            return url.strip().rstrip("/")
    return ""


def post_json(url, body, timeout=20):
    """(HTTP status, JSON answer or {}) for a POST; status 0 when the panel cannot be reached."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, data = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, data = e.code, e.read()
    except (OSError, ValueError):
        return 0, {}
    try:
        answer = json.loads(data.decode("utf-8") or "{}")
    except ValueError:
        answer = {}
    return status, answer if isinstance(answer, dict) else {}


def pair(panel_url, code, token):
    """Pair this server with the panel by the code it shows (docs/installs.md in OpenLobby,
    "Pairing"). Returns (result, message, pairing): result is "paired", "retry" (the same code
    can be typed again once the cause is fixed), "new code" (make another in the panel) or
    "stop" (pairing cannot work here)."""
    status, answer = post_json(panel_url + "/api/pair", {"code": code, "key": "psu", "title": GAME_TITLE,
                                                         "url": PANEL_ADMIN_URL, "token": token})
    error = answer.get("error") or ""
    if status == 200 and answer.get("ok") and answer.get("pairing"):
        title = answer.get("title") or GAME_TITLE
        return "paired", "Paired. %s shows in the panel now." % title, answer["pairing"]
    if status == 0:
        return "stop", "Setup could not reach the panel at %s. Is it running?" % panel_url, ""
    if status == 400 and "POL_ADMIN_GAME_" in error:
        return "stop", ("This panel already manages %s through its own settings (POL_ADMIN_GAME_PSU_URL). "
                        "Nothing to pair." % GAME_TITLE), ""
    if status == 400:
        # A failed check does not use the code up.
        return "retry", ("The panel could not check this server: %s Make sure the server is running, then "
                         "type the same code again." % (error or "it gave no reason.")), ""
    if status == 403:
        return "new code", error or "That code is wrong, used or expired. Make a new one in the panel.", ""
    if status == 429:
        return "retry", "Too many tries. Wait a minute, then type the code again.", ""
    return "stop", "The panel answered %s (%s)." % (status, error or "no reason"), ""


def pair_update(panel_url, pairing, token):
    """Tell the panel about a new token. Returns (ok, message)."""
    status, answer = post_json(panel_url + "/api/pair/update", {"key": "psu", "pairing": pairing, "token": token,
                                                                "url": PANEL_ADMIN_URL})
    if status == 200:
        return True, "The panel has the new token."
    if status in (403, 404):
        return False, ("The panel no longer knows this server (%s) Pair again: Pair with an OpenLobby admin panel."
                       % (answer.get("error") or "unknown pairing."))
    if status == 0:
        return False, "Setup could not reach the panel at %s. Pair again once it runs." % panel_url
    return False, "The panel answered %s (%s)." % (status, answer.get("error") or "no reason")


def ask_code():
    say("In the panel, open Settings > Modules > Pair with a setup program. It shows an")
    say("8-character code.")
    while True:
        code = ask("The code (Enter to stop)").replace(" ", "").replace("-", "")
        if not code:
            return ""
        if re.fullmatch(r"[A-Za-z0-9]{8}", code):
            return code.upper()
        say("  The code has 8 letters and digits.")


def pair_flow(panel_url, ask_first=True):
    """Ask, then pair with the panel at panel_url; saves the pairing. True when paired."""
    if ask_first and not ask_yes("An OpenLobby admin panel is running here. Manage %s from it?" % GAME_TITLE):
        say("You can pair later: run setup again and choose Pair with an OpenLobby admin panel.")
        return False
    if not server_running():
        say("The server has to run for the panel to check it. Starting it...")
        prepare_start()
        compose("up", "-d")
        if not wait_ready():
            return False
    while True:
        code = ask_code()
        if not code:
            return False
        result, message, pairing = pair(panel_url, code, read_env().get("PSU_PANEL_TOKEN", ""))
        say(message)
        if result == "paired":
            write_env({"OPENLOBBY_PAIRING": pairing})
            return True
        if result == "stop":
            return False


def pair_menu():
    panel = find_panel()
    if not panel:
        say("No OpenLobby admin panel runs on this computer. Start the panel, then try again.")
        return
    if read_env().get("OPENLOBBY_PAIRING"):
        say("This server is paired with a panel already.")
        if not ask_yes("Pair again?", False):
            return
    pair_flow(panel, ask_first=False)


def new_panel_token():
    say("The admin port's token is what the panel uses to manage this server. Make a new one if")
    say("the old one may have leaked.")
    if not ask_yes("Make a new token?", False):
        return False
    token = secrets.token_urlsafe(24)
    write_env({"PSU_PANEL_TOKEN": token})
    if server_running():
        compose("up", "-d", "game")
    env = read_env()
    panel = find_panel()
    if env.get("OPENLOBBY_PAIRING") and panel:
        ok, message = pair_update(panel, env["OPENLOBBY_PAIRING"], token)
        say(message)
    elif env.get("OPENLOBBY_PAIRING"):
        say("No panel runs here now: pair again once it runs, so it learns the new token.")
    else:
        say("Saved.")
    return False


# --- update state for the panel -----------------------------------------------------------

def github(path, timeout=10):
    req = urllib.request.Request(API + path, headers={"Accept": "application/vnd.github+json",
                                                      "User-Agent": "psu-server-setup"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


NO_RELEASE = []         # why the last look found no release, for the panel's "why"


def latest_release():
    """{"tag", "zip", "commit", "date"} of the newest release, or None."""
    NO_RELEASE[:] = ["Setup could not reach GitHub to look for a newer release."]
    try:
        info = github("/repos/%s/releases/latest" % REPO)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            NO_RELEASE[:] = ["%s has no release on GitHub yet." % REPO]
        return None
    except (OSError, ValueError):
        return None
    try:
        tag = info.get("tag_name")
        if not tag:
            return None
        commit = github("/repos/%s/commits/%s" % (REPO, urllib.parse.quote(tag, safe="")))
    except (OSError, ValueError, AttributeError):
        return None
    commit = commit if isinstance(commit, dict) else {}
    sha = commit.get("sha") or ""
    # The zip of the tag's commit, so what is installed is exactly the commit compared.
    ref = sha if re.fullmatch(r"[0-9a-f]{40}", sha) else "refs/tags/" + tag
    return {"tag": tag, "zip": "https://github.com/%s/archive/%s.zip" % (REPO, ref), "commit": sha,
            "date": ((commit.get("commit") or {}).get("committer") or {}).get("date", "")}


def compare(base, head):
    """(commits head is ahead of base, [{"commit", "date", "subject"}] newest first, merges left
    out, at most 50) or (None, []) when GitHub cannot tell (base is not on GitHub, or offline)."""
    try:
        info = github("/repos/%s/compare/%s...%s" % (REPO, base, head))
    except (OSError, ValueError):
        return None, []
    if not isinstance(info, dict):
        return None, []
    changes = [{"commit": c.get("sha", ""),
                "date": ((c.get("commit") or {}).get("committer") or {}).get("date", ""),
                "subject": (((c.get("commit") or {}).get("message") or "").splitlines() or [""])[0][:200]}
               for c in info.get("commits") or [] if len(c.get("parents") or []) < 2]
    return int(info.get("ahead_by") or 0), list(reversed(changes))[:50]


def update_state(running, latest, behind, changes):
    """The "update" object of the panel's status (OpenLobby docs/installs.md, "Updates"): what
    runs, the newest release, what is new."""
    why = ""
    if not running.get("commit"):
        why = "This server was not built by PSU Server Setup, so its version is not known."
    elif not latest:
        why = (NO_RELEASE or ["Setup could not reach GitHub to look for a newer release."])[0]
    elif behind is None:
        why = "The running build is not a commit GitHub knows (local changes?)."
    state = "unknown" if why else "available" if behind > 0 and running["commit"] != latest["commit"] else "current"
    doc = {"running": {"commit": running.get("commit", ""), "built_at": running.get("built_at", "")},
           "latest": {"commit": (latest or {}).get("commit", ""), "date": (latest or {}).get("date", "")},
           "behind": behind or 0, "changes": changes if state == "available" else [],
           "update": state, "how": HOW_TO_UPDATE}
    if why:
        doc["why"] = why
    return doc


def refresh_update_state(latest=None):
    """Look at the releases and write state/update.json for the game's status (the panel)."""
    env = read_env()
    running = {"commit": env.get("PSU_BUILD_COMMIT", ""), "built_at": env.get("PSU_BUILT_AT", "")}
    latest = latest or latest_release()
    behind, changes = (None, [])
    if latest and running["commit"]:
        behind, changes = (0, []) if running["commit"] == latest["commit"] else compare(running["commit"],
                                                                                         latest["commit"])
    doc = update_state(running, latest, behind, changes)
    os.makedirs(os.path.dirname(UPDATE_FILE), exist_ok=True)
    with open(UPDATE_FILE + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    os.replace(UPDATE_FILE + ".tmp", UPDATE_FILE)
    return doc


# --- finishing ---------------------------------------------------------------------------

def finish(env, choices):
    address = env["PSU_GAME_ADDR"]
    home = who_plays(env, choices) == "home"
    dns = choices.get("dns", True)
    dns_at = lan_address() or address
    say()
    say("=" * 72)
    say("Your server is running.")
    say()
    if home and dns:
        say("Tell your players: primary DNS %s, a DNAS-bypassed disc (PLAYING.md), any new user ID." % dns_at)
    else:
        say("Tell your players: the game's names lead to %s, a DNAS-bypassed disc (PLAYING.md), any new "
            "user ID." % address)
    say()
    say("The names the discs look up, which have to lead to %s:" % address)
    for name in GAME_NAMES:
        say("    %s" % name)
    if not home:
        say("The DNS server on this computer only answers your home network. Players elsewhere run")
        say("tools/devdns.py at their home (python devdns.py --answer %s) and set their" % address)
        say("console's DNS to that computer, or add the names to a DNS server of their own.")
    say("In PCSX2 the DNS goes in Settings > Network & HDD (DNS mode Manual). For the bypass,")
    say("PCSX2 players can use the pnach files in %s;" % os.path.join(ROOT, "patches"))
    say("others need a patched disc image (Make a patched disc image for a player, in the menu).")
    if who_plays(env, choices) == "internet":
        say()
        if choices.get("router") == "open":
            say("Your router has the ports open.")
        else:
            say("Friends can join once the ports are open on your router (Open the router ports again).")
        say("Setup cannot test the internet side from inside your home. Ask a friend to try.")
    say()
    say("The server keeps running when you close this window, as long as Docker runs.")
    say("Run PSU Server Setup again to stop it, update it, make backups or pair it with a panel.")
    say("=" * 72)


def first_run(dry_run=False):
    say("PSU Server Setup")
    say("================")
    say("This sets up a Phantasy Star Universe server on this computer. Answer three questions,")
    say("then setup builds everything. The first time takes 30 to 40 minutes and needs the internet;")
    say("you can leave it running.")
    env = read_env(ENV_EXAMPLE)
    choices = read_choices()
    who = ask_who()
    address, typed = ask_address(who)
    chosen = find_discs()
    name = ask_name()
    changes = first_settings(who, address, name, env, lan=lan_address())
    choices.update(who=who, address=typed, name=name, discs=disc_records(chosen))

    heading("Ready")
    say("  Players:  %s, at %s" % ("people on your home network" if who == "home" else "friends over the internet",
                                   typed))
    say("  Discs:    %s" % ", ".join(os.path.basename(d.path) for _, d in sorted(chosen.items())))
    say("  Name:     %s" % name)
    say("Everything else uses the usual settings. You can change them later: run PSU Server Setup")
    say("again and choose Change settings.")
    if dry_run:
        say()
        say("Dry run: nothing was written. Setup would write these settings to %s:" % ENV)
        for key, value in changes.items():
            if key != "PSU_PANEL_TOKEN":
                say("  %s=%s" % (key, "(default)" if value is None else value))
        say("  PSU_PANEL_TOKEN: made at random")
        return
    if not ask_yes("Start?"):
        return
    write_env(changes)
    env = read_env()
    write_choices(choices)
    make_folders(env)
    write_news(env, name, "Have fun, and tell the people who run this server about any bugs.")
    say()
    say("Details of every step go to %s" % LOG)
    if who == "internet":
        heading("Router")
        ok, router = open_router_ports(env)
        choices["router"] = "open" if ok else "closed"
        write_choices(choices)
        if not ok:
            show_port_help(env, router)
            say("You can do this while setup builds the game data.")
    if complete_setup(env, choices):
        offer_panel()


def offer_panel():
    """After a first start: pair with a panel that runs here, if the host wants."""
    panel = find_panel()
    if panel and not read_env().get("OPENLOBBY_PAIRING"):
        say()
        pair_flow(panel)


def complete_setup(env, choices):
    """Build what is missing and start the server."""
    heading("Building")
    say("This takes a while. Each step says roughly how long.")
    progress = proglib.Progress()
    with log_file() as log:
        if not build_image(progress, log):
            return False
        if not has_data(env):
            ensure_discs(choices)
            if not build_game_data(env, choices, progress, log):
                say("Fix the cause, then run PSU Server Setup again and choose Rebuild the game data.")
                return False
        env = read_env()
        refresh_update_state()
        if not start_server(progress, log):
            return False
    finish(env, read_choices())
    return True


# --- menu --------------------------------------------------------------------------------

def restart_if_running(was_running):
    if was_running:
        say("Restarting the server with the new settings...")
        prepare_start()
        compose("up", "-d", "--remove-orphans")
        if wait_ready():
            smoke()


def change_settings():
    """The settings menu. Returns once the host goes back."""
    while True:
        env, choices = read_env(), read_choices()
        who = who_plays(env, choices)
        items = [
            ("Who will play (now: %s, at %s)" % ("home network" if who == "home" else "friends over the internet",
                                                 choices.get("address") or env.get("PSU_GAME_ADDR")), change_who),
            ("Server name and login notice (now: %s)" % (choices.get("name") or DEFAULT_NAME), change_name),
            ("Who can make an account (now: %s)" % ("anyone, by logging in" if env.get("PSU_AUTOREGISTER", "1") != "0"
                                                    else "only accounts that exist"), change_signup),
            ("Game masters (now: %s)" % (env.get("PSU_ADMINS") or "none"), change_gm),
            ("The DNS server for players' consoles (now: %s)" % ("on" if choices.get("dns", True) else "off"),
             change_dns),
            ("Make a new admin panel token", lambda env, choices: new_panel_token()),
        ]
        heading("Change settings")
        for i, (title, _) in enumerate(items, 1):
            say("  %d. %s" % (i, title))
        say("  0. Back")
        pick = ask("Type a number", "0")
        if pick == "0":
            return
        if pick.isdigit() and 1 <= int(pick) <= len(items):
            running = server_running()
            if items[int(pick) - 1][1](env, read_choices()):
                restart_if_running(running)


def change_who(env, choices):
    who = ask_who(who_plays(env, choices))
    address, typed = ask_address(who, choices.get("address") or env.get("PSU_GAME_ADDR", ""))
    choices.update(who=who, address=typed)
    changes = {"PSU_GAME_ADDR": address}
    if sys.platform.startswith("linux"):
        changes["PSU_DNS_BIND"] = lan_address() or address
    write_env(changes)
    if who == "internet":
        ok, router = open_router_ports(read_env())
        choices["router"] = "open" if ok else "closed"
        if not ok:
            show_port_help(read_env(), router)
    write_choices(choices)
    say("Players' consoles now find the server at %s (their primary DNS)." % address)
    return True


def change_name(env, choices):
    name = ask("Server name", choices.get("name") or DEFAULT_NAME)
    text = ask("A line under the welcome", "Have fun, and tell the people who run this server about any bugs.")
    write_news(env, name, text)
    write_env({"PSU_MOTD": motd(name)})
    choices["name"] = name
    write_choices(choices)
    say("Saved. Players see it at their next login.")
    return False


def change_signup(env, choices):
    pick = ask_choice("Who can make an account?", [
        "Anyone: logging in with a new user ID and password makes the account",
        "Only accounts that exist already"])
    write_env({"PSU_AUTOREGISTER": 1 if pick == 1 else 0})
    return True


def change_gm(env, choices):
    say("Game masters can use the ! commands in chat (!help lists them).")
    gm = ask("Their login user IDs, with commas between them (empty for none)", env.get("PSU_ADMINS", ""))
    write_env({"PSU_ADMINS": gm.replace(" ", "")})
    return True


def change_dns(env, choices):
    say("Consoles find the server through a DNS server that answers the game's four names with it.")
    say("One runs for every OpenLobby install on this computer; players at home set their console's")
    say("primary DNS to %s. Turn it off when your players get the names elsewhere." % (lan_address() or "it"))
    on = ask_yes("Keep it on?", choices.get("dns", True))
    choices["dns"] = on
    write_choices(choices)
    if not on and unregister_dns():
        others = "Other installs here may use it: start them again with their own setup."
        say("This server's DNS server stops. " + others)
        compose("rm", "-sf", "dns", check=False, capture=True)
    return True


def start_server_menu():
    if server_running():
        say("The server is already running.")
        return
    env, choices = read_env(), read_choices()
    if not has_data(env):
        say("The game data is not built yet.")
        if ask_yes("Build it now and start?"):
            complete_setup(env, choices)
        return
    if who_plays(env, choices) == "internet":
        open_router_ports(env, quiet=True)
    prepare_start()
    compose("up", "-d")
    if wait_ready():
        smoke()


def stop_server():
    if not server_running():
        say("The server is not running.")
        return
    if ask_yes("Stop the server? Players in the game are disconnected.", False):
        compose("stop")
        say("Stopped. It starts again when you choose Start the server.")


def update():
    """Fetch the new version, rebuild the image, and rebuild the game data when its builder changed."""
    before = tree_digest("extract")
    latest = None
    if os.path.exists(os.path.join(ROOT, ".git")):
        if run(["git", "pull", "--ff-only"], check=False).returncode != 0:
            say("git pull failed (local changes?). Nothing was changed.")
            return
    else:
        latest = latest_release()
        if not latest:
            say("Could not reach %s. Check the internet connection." % RELEASES)
            return
        if latest["tag"] == installed_version().get("tag"):
            say("This is the newest version (%s)." % latest["tag"])
            if not ask_yes("Rebuild the server program anyway?", False):
                refresh_update_state(latest)
                return
        else:
            say("Downloading %s..." % latest["tag"])
            archive = os.path.join(ROOT, "logs", "update.zip")
            os.makedirs(os.path.dirname(archive), exist_ok=True)
            try:
                urllib.request.urlretrieve(latest["zip"], archive)
                unpack_server(archive, ROOT, {"tag": latest["tag"], "commit": latest["commit"],
                                              "date": latest["date"]})
            except (OSError, zipfile.BadZipFile) as e:
                say("The download failed (%s). Nothing was changed." % e)
                return
            finally:
                if os.path.exists(archive):
                    os.remove(archive)
            say("Updated the server's files to %s." % latest["tag"])
            if FROZEN:
                say("A new PSU Server Setup is also on %s" % RELEASES)
    progress = proglib.Progress()
    with log_file() as log:
        if not build_image(progress, log):
            return
    refresh_update_state(latest)
    if tree_digest("extract") != before:
        say("This update changes the game data builder: building the game data again.")
        rebuild_data(ask_first=False)
        return
    prepare_start()
    compose("up", "-d", "--remove-orphans")
    if wait_ready():
        smoke()


def rebuild_data(ask_first=True):
    env, choices = read_env(), read_choices()
    if ask_first:
        say("This builds the game data again from your discs. The server is stopped meanwhile.")
        if not ask_yes("Go on?", False):
            return
        if choices.get("discs") and not ask_yes("Use the same discs as last time?"):
            ensure_discs(choices, ask_again=True)
    ensure_discs(choices)
    compose("stop", "game", check=False)
    progress = proglib.Progress()
    with log_file() as log:
        if not build_game_data(env, choices, progress, log):
            say("The game stays stopped until the game data is built.")
            return
        start_server(progress, log)


def patch_disc():
    """Write a DNAS-bypassed copy of a retail disc image next to it (extract/bypass.py)."""
    say("A player whose console or emulator boots disc images needs a copy of the game with the")
    say("DNAS check skipped. This makes one from a disc image they own, next to it. Never share")
    say("the patched image: it is the whole game. PCSX2 players can use the pnach files instead.")
    found = [d for d in disclib.search(disclib.likely_folders([ROOT])) if d.retail]
    for item in read_choices().get("discs", []):
        if item.get("retail") and os.path.exists(item["path"]) and item["path"] not in [d.path for d in found]:
            found.insert(0, disclib.Disc(item["path"], item["game"]))
    options = [d.shown() for d in found] + ["Another file"]
    pick = ask_choice("Which disc image?", options)
    if pick == len(options):
        path = ask("The disc image (.iso)").strip().strip('"').strip("'")
        new = disclib.identify(os.path.abspath(os.path.expanduser(path))) if path else []
        if not new:
            say("That is not a Phantasy Star Universe disc image.")
            return
        disc = new[0]
    else:
        disc = found[pick - 1]
    if not disc.retail:
        say("This image does not have the retail disc's size. The patch only works on a plain copy of")
        say("the disc (the Redump image).")
        return
    folder, name = os.path.split(os.path.abspath(disc.path))
    out = os.path.splitext(name)[0] + " [ONLINE].iso"
    if os.path.exists(os.path.join(folder, out)) and not ask_yes("%s exists already. Make it again?" % out, False):
        return
    if shutil.disk_usage(folder).free < os.path.getsize(disc.path) + (256 << 20):
        say("Not enough free disk space next to the image (it needs %d GB)." % (os.path.getsize(disc.path) >> 30))
        return
    env = read_env()
    short = "PSU" if disc.game == "psu" else "AotI"
    cmd = ["docker", "run", "--rm", "--user", "%s:%s" % (env.get("PSU_UID", "10110"), env.get("PSU_GID", "10110")),
           "-v", "%s:/app/extract:ro" % os.path.join(ROOT, "extract").replace("\\", "/"),
           "-v", "%s:/app/patches:ro" % os.path.join(ROOT, "patches").replace("\\", "/"),
           "-v", "%s:/discs" % folder.replace("\\", "/"),
           "--entrypoint", "python", "psu-server:local", "/app/extract/bypass.py", "iso", short,
           "/discs/" + name, "/discs/" + out]
    progress = proglib.Progress()
    with log_file() as log:
        if not have_image("psu-server:local"):
            if not build_image(progress, log):
                return
        progress.begin("Writing %s" % out, "a few minutes")
        code, tail = proglib.run_logged(cmd, log, cwd=ROOT, progress=progress)
        progress.end(code == 0)
    if code != 0:
        failed("Making the patched image failed.", tail)
        return
    say("Written: %s" % os.path.join(folder, out))
    say("It boots in PCSX2, or on a console that loads disc images (OPL, a burned DVD).")


def backup(quiet=False):
    """Copy the accounts database (SQLite's online backup, inside the game container) and the
    small state files into backups/psu-<time>.zip. Returns the zip's path or None."""
    if not server_running():
        if not quiet:
            say("Start the server first: the backup is made by the running game.")
        return None
    env = read_env()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    script = ("import sqlite3; s = sqlite3.connect('file:/state/psu.db?mode=ro', uri=True); "
              "d = sqlite3.connect('/state/backup.tmp'); s.backup(d); d.close(); s.close()")
    if compose("exec", "-T", "game", "python", "-c", script, check=False, capture=True).returncode != 0:
        say("The backup failed: the database could not be read. See the server's log.")
        return None
    os.makedirs(BACKUPS, exist_ok=True)
    path = os.path.join(BACKUPS, "psu-%s.zip" % stamp)
    state = state_dir(env)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.join(state, "backup.tmp"), "psu.db")
        for name in ("news.txt", "events.json", "setup.json"):
            if os.path.exists(os.path.join(state, name)):
                z.write(os.path.join(state, name), name)
    os.remove(os.path.join(state, "backup.tmp"))
    say("Saved %s" % path)
    if not quiet:
        say("Also keep a copy of %s somewhere safe." % ENV)
    return path


def restore():
    zips = sorted(f for f in os.listdir(BACKUPS) if f.endswith(".zip")) if os.path.isdir(BACKUPS) else []
    if not zips:
        say("No backups in %s." % BACKUPS)
        return
    for i, name in enumerate(zips, 1):
        say("  %d. %s" % (i, name))
    pick = ask("Restore which one (number, Enter to cancel)")
    if not pick.isdigit() or not 1 <= int(pick) <= len(zips):
        return
    say("This replaces every account and character with the ones in %s." % zips[int(pick) - 1])
    say("Play since then is lost.")
    if not ask_yes("Go on?", False):
        return
    state = state_dir(read_env())
    compose("stop", "game", "login", check=False)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(os.path.join(state, "psu.db" + suffix)):
            os.remove(os.path.join(state, "psu.db" + suffix))
    with zipfile.ZipFile(os.path.join(BACKUPS, zips[int(pick) - 1])) as z:
        for name in z.namelist():
            if name in ("psu.db", "news.txt", "events.json"):
                z.extract(name, state)
    compose("up", "-d")
    if wait_ready():
        smoke()


def account_tool(*args):
    """Run tools/account.py on the server's database, in the server image (no Python needed here)."""
    tools = os.path.join(ROOT, "tools").replace("\\", "/")
    return compose("run", "--rm", "--no-deps", "-e", "PYTHONPATH=/app", "-e", "USER=setup", "-v",
                   tools + ":/app/tools:ro", "--entrypoint", "python", "game", "/app/tools/account.py",
                   "--state", "/state", *args, check=False)


def accounts():
    pick = ask_choice("Player accounts", ["List the accounts", "Reset a password (gives a one-time code)",
                                          "Back"], 3)
    if pick == 1:
        account_tool("list", ask("Login ID or character name to look for (Enter for all)", "%"))
    elif pick == 2:
        login = ask("The player's login user ID")
        if login:
            say("The player logs in with the code as the password, or the code followed directly by a new")
            say("password (4 or more characters), which then becomes their password.")
            account_tool("reset", login)


def logs():
    say("Showing what the server is doing. Press Ctrl+C to go back to the menu.")
    try:
        compose("logs", "-f", "--tail", "100", check=False)
    except KeyboardInterrupt:
        pass


def router_again():
    env, choices = read_env(), read_choices()
    if who_plays(env, choices) != "internet":
        say("Your server is set up for your home network only, so the router needs no open ports.")
        say("Choose Change settings, then Who will play, to change that.")
        return
    ok, router = open_router_ports(env)
    choices["router"] = "open" if ok else "closed"
    write_choices(choices)
    if not ok:
        show_port_help(env, router)


def tell_players():
    finish(read_env(), read_choices())


def uninstall():
    say("This stops the server and removes its containers and images from Docker.")
    if not ask_yes("Remove the server?", False):
        return
    env, choices = read_env(), read_choices()
    if server_running() and ask_yes("Make a backup of the accounts first?"):
        backup(quiet=True)
    if unregister_dns():
        say("The DNS server for consoles was this server's. Other installs here that used it need")
        say("their own setup run again to start one.")
    compose("--profile", "extract", "--profile", "dns", "--profile", "observe", "--profile", "experimental",
            "down", "--volumes", "--rmi", "local", "--remove-orphans", check=False)
    if who_plays(env, choices) == "internet":
        close_router_ports(env)
    say("Removed from Docker.")
    if read_manifest(ROOT) and ask_yes("Also delete the folder %s, with every account, character and backup?"
                                       % ROOT, False):
        shutil.rmtree(ROOT, ignore_errors=True)
        say("Deleted %s." % ROOT)
        raise SystemExit(0)
    say("The folder %s stays; run setup again to set the server up anew." % ROOT)
    raise SystemExit(0)


MENU = [("Start the server", start_server_menu),
        ("Stop the server", stop_server),
        ("Show what to tell players", tell_players),
        ("Change settings", change_settings),
        ("Update", update),
        ("Rebuild the game data", rebuild_data),
        ("Pair with an OpenLobby admin panel", pair_menu),
        ("Make a patched disc image for a player", patch_disc),
        ("Make a backup", backup),
        ("Restore a backup", restore),
        ("Player accounts (list, reset a password)", accounts),
        ("Show what the server is doing (logs)", logs),
        ("Check that the server works", smoke),
        ("Open the router ports again", router_again),
        ("Remove the server from this computer", uninstall)]


def menu_status(env):
    if not has_data(env):
        return "game data not built yet"
    return "running" if server_running() else "stopped"


def menu():
    while True:
        env = read_env()
        heading("PSU server: %s (address %s)" % (menu_status(env), env.get("PSU_GAME_ADDR", "?")))
        for i, (title, _) in enumerate(MENU, 1):
            say("  %2d. %s" % (i, title))
        say("   0. Quit")
        pick = ask("Type a number", "0")
        if pick == "0":
            return
        if pick.isdigit() and 1 <= int(pick) <= len(MENU):
            try:
                MENU[int(pick) - 1][1]()
            except KeyboardInterrupt:
                say()
            except SystemExit as e:
                if e.code == 0:
                    raise
                say(str(e))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="PSU Server Setup", description=(
        "Sets up and runs a Phantasy Star Universe (PS2) server in Docker. The first run asks three "
        "questions; later runs show a menu."))
    ap.add_argument("--folder", help="the server folder (default: the one this runs from, or "
                                     "'PSU Server' in your home folder for the setup program)")
    ap.add_argument("--dry-run", action="store_true",
                    help="ask the questions and look for the discs, then show what would be done; "
                         "writes nothing and needs no Docker")
    ap.add_argument("--backup", action="store_true",
                    help="make a backup of the accounts and quit (for a scheduled task or cron)")
    ap.add_argument("--project", default=os.environ.get("PSU_SETUP_PROJECT", "psu"),
                    help="the Docker Compose project name (default psu)")
    return ap.parse_args(argv)


def main(argv=None):
    global PROJECT
    args = parse_args(argv)
    PROJECT = args.project
    use_root(find_root(args.folder))
    try:
        if args.dry_run:
            say("Server folder: %s%s" % (ROOT, "" if os.path.exists(os.path.join(ROOT, "compose.yml")) else
                                         " (the server's files would be unpacked here)"))
            say("Docker: %s" % docker_state())
            first_run(dry_run=True)
            return
        if not ensure_server_files():
            return
        os.chdir(ROOT)
        if args.backup:
            if not wait_for_docker(wait=False) or not backup(quiet=True):
                sys.exit(1)
            return
        wait_for_docker()
        if not os.path.exists(ENV):
            first_run()
            return
        env = read_env()
        if not has_data(env) and read_choices().get("discs"):
            say("Setup did not finish last time.")
            if ask_yes("Carry on where it stopped?"):
                if complete_setup(env, read_choices()):
                    offer_panel()
                return
        refresh_update_state()
        menu()
    except KeyboardInterrupt:
        say()
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    finally:
        if FROZEN:
            # A double-clicked program's window closes when it ends: keep it open to be read.
            try:
                input("Press Enter to close this window.")
            except (EOFError, KeyboardInterrupt):
                pass
