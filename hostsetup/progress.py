"""Long commands shown as a few plain steps, with their full output in a log file."""
import queue
import re
import subprocess
import sys
import threading
import time


def minutes(seconds):
    m = int(seconds // 60)
    return "under a minute" if m < 1 else "1 minute" if m == 1 else "%d minutes" % m


class Progress:
    """One status line per step: "Building the game data (about 30 minutes): step 3,
    psu-drops, 7 minutes so far", redrawn in place, and a closing line when the step
    ends. Without a terminal (output to a file) only the closing lines are written."""

    def __init__(self, out=None, clock=time.monotonic):
        self.out = out or sys.stdout
        self.clock = clock
        self.live = hasattr(self.out, "isatty") and self.out.isatty()
        self.title = None
        self.detail = ""
        self.started = 0.0
        self.width = 0

    def begin(self, title, estimate=""):
        if self.title == title:
            return
        self.end()
        self.title, self.estimate, self.detail, self.started = title, estimate, "", self.clock()
        if not self.live:
            self._write("%s%s...\n" % (title, " (%s)" % estimate if estimate else ""))
        self.tick()

    def set(self, detail):
        self.detail = detail
        self.tick()

    def line(self):
        text = "  " + self.title
        if self.estimate:
            text += " (%s)" % self.estimate
        parts = [p for p in (self.detail, "%s so far" % minutes(self.clock() - self.started)) if p]
        return text + ": " + ", ".join(parts)

    def tick(self):
        if self.live and self.title:
            text = self.line()
            self._write("\r" + text + " " * max(0, self.width - len(text)))
            self.width = len(text)

    def end(self, ok=True):
        if not self.title:
            return
        took = minutes(self.clock() - self.started)
        text = "  %s %s (%s)" % ("Done:" if ok else "Stopped:", self.title, took)
        if self.live:
            self._write("\r" + text + " " * max(0, self.width - len(text)) + "\n")
        else:
            self._write(text + "\n")
        self.title, self.width = None, 0

    def _write(self, text):
        self.out.write(text)
        self.out.flush()


def run_logged(cmd, log, on_line=None, progress=None, cwd=None, env=None):
    """Run cmd with its output going to the open file `log` (and to on_line,
    one line at a time) while the progress line keeps ticking. Returns
    (exit code, the last 30 lines)."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    lines = queue.Queue()

    def read():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    tail = []
    while True:
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            if progress:
                progress.tick()
            continue
        if line is None:
            break
        log.write(line)
        log.flush()
        line = line.rstrip("\r\n")
        tail = (tail + [line])[-30:]
        if on_line:
            on_line(line)
    proc.stdout.close()
    return proc.wait(), tail


class ExtractWatcher:
    """Turns the game data builder's output (python -m extract, extract/build.py) into steps for a
    Progress: it prints "<step> ..." as each step starts and "<step>: up to date" for one a
    previous run did; "<game>-disc" steps unpack the discs, and EGS is fetched once."""

    STEP = re.compile(r"^([a-z][a-z0-9_-]*)(?: \.\.\.|: up to date)$")

    def __init__(self, progress):
        self.progress, self.steps = progress, 0

    def __call__(self, line):
        if line.strip().startswith("fetching EGS"):
            self.progress.begin("Downloading the lobby builder (EGS)", "about a minute")
            return
        m = self.STEP.match(line)
        if not m:
            return
        self.steps += 1
        if m.group(1).endswith("-disc"):
            self.progress.begin("Reading your discs", "a few minutes")
        else:
            self.progress.begin("Building the game data", "about 30 minutes")
        self.progress.set("step %d, %s" % (self.steps, m.group(1)))


# What a failed build's last lines usually mean, in words a host can act on.
KNOWN_ERRORS = [
    (r"(?i)not a (PSU|Phantasy Star Universe)|no SYSTEM\.CNF|unknown disc|pass it as --",
     "One of the files is not the Phantasy Star Universe disc setup took it for."),
    (r"(?i)sha1 mismatch|not the retail|not a Redump", "The disc image is not a plain copy of the retail disc. "
                                                        "Make a new image of the disc and try again."),
    (r"No space left on device|no space left", "The disk is full. Free some space (the build needs about "
                                                "10 GB) and try again."),
    (r"(?i)urlopen error|name resolution|timed out|connection (reset|refused)|Could not resolve host",
     "A download failed. Check the internet connection and try again."),
    (r"(?i)port is already allocated|address already in use",
     "Another program on this computer uses one of the server's ports. Close it (or stop the other "
     "server) and try again."),
]


def explain(tail):
    """A plain sentence for a known failure in these output lines, or ""."""
    text = "\n".join(tail)
    for pattern, message in KNOWN_ERRORS:
        if re.search(pattern, text):
            return message
    return ""
