"""Docker's memory for the game data build: how much it has, how to give it more, and
how to tell that Docker itself stopped part way through a build.

The build runs its tools one at a time and peaks at about 300 MB, so it has no worker
pool to size; a Docker that stops under it is usually short of memory for everything
else it runs (other containers, Docker Desktop's VM on a small computer).
"""
import os
import re

GB = 1 << 30
# Below this setup says how to give Docker more before it builds.
LOW = 2 * GB
# What setup offers to give Docker Desktop on Windows (never more than the computer can spare).
WANT = 4 * GB
KEEP_FOR_WINDOWS = 3 * GB

# What the docker command prints when the engine (or Docker Desktop's VM) went away.
ENGINE_GONE = re.compile(r"error waiting for container|unexpected EOF|Cannot connect to the Docker daemon"
                         r"|error during connect|error from daemon in stream|failed to connect to the docker API"
                         r"|The system cannot find the file specified")


def gb(n):
    """"3.8 GB"."""
    return "%.1f GB" % (n / GB)


def parse_bytes(text):
    """docker info's MemTotal (bytes), or None."""
    try:
        n = int((text or "").strip())
    except ValueError:
        return None
    return n if n > 0 else None


def engine_stopped(lines, exit_code=None, oom_killed=False):
    """True when a build ended because Docker stopped or ran out of memory rather than
    because of the build itself. exit_code is the build container's (255 when Docker went
    away under it, 137 when it was killed) or docker's own (125)."""
    if oom_killed or exit_code in (125, 255):
        return True
    return any(ENGINE_GONE.search(line) for line in lines)


def wslconfig_path(home=None):
    return os.path.join(home or os.path.expanduser("~"), ".wslconfig")


def wsl_memory_value(n):
    return "%dGB" % max(1, n // GB)


def set_wsl_memory(text, value):
    """text of a .wslconfig with memory=value in its [wsl2] section; every other line kept."""
    out, section, done = [], None, False
    for line in text.splitlines():
        head = re.match(r"\s*\[([^\]]+)\]", line)
        if head:
            if section == "wsl2" and not done:
                out.append("memory=%s" % value)
                done = True
            section = head.group(1).strip().lower()
        elif section == "wsl2" and re.match(r"\s*memory\s*=", line, re.I):
            if not done:
                out.append("memory=%s" % value)
                done = True
            continue
        out.append(line)
    if not done:
        if section == "wsl2":
            out.append("memory=%s" % value)
        else:
            if out and out[-1].strip():
                out.append("")
            out += ["[wsl2]", "memory=%s" % value]
    return "\n".join(out) + "\n"


def wsl_memory_now(text):
    """The memory= of the [wsl2] section, or ""."""
    section = None
    for line in text.splitlines():
        head = re.match(r"\s*\[([^\]]+)\]", line)
        if head:
            section = head.group(1).strip().lower()
            continue
        m = re.match(r"\s*memory\s*=\s*(\S+)", line, re.I)
        if section == "wsl2" and m:
            return m.group(1)
    return ""


def read_text(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except OSError:
        return ""


def write_wslconfig(value, home=None):
    """Set memory=value in the user's .wslconfig, keeping the rest. Returns the path."""
    path = wslconfig_path(home)
    text = set_wsl_memory(read_text(path), value)
    with open(path, "w", encoding="utf-8", newline="\r\n" if os.name == "nt" else "\n") as f:
        f.write(text)
    return path


def computer_memory():
    """This computer's memory in bytes (Windows only), or None."""
    if os.name != "nt":
        return None
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    s = Status()
    s.dwLength = ctypes.sizeof(s)
    try:
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
            return s.ullTotalPhys
    except (AttributeError, OSError):
        pass
    return None


def memory_to_offer(computer, now):
    """What to set Docker Desktop's memory to (whole GB): WANT, or less on a computer that
    cannot spare it; None when that would not give Docker at least 1 GB more than `now`."""
    n = WANT if not computer else min(WANT, computer - KEEP_FOR_WINDOWS)
    n = n // GB * GB
    return n if n >= (now or 0) + GB else None
