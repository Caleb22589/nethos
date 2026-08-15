#!/usr/bin/env python3
"""
nethosd - the NETHOS shell bridge and app host.

The NETHOS desktop is written in HTML/CSS/JS and runs inside Chromium. This
daemon is the only thing standing between that web shell and the real system:
it serves the shell, the app SDK and every installed NETHOS app, and exposes a
small JSON API over loopback for the things a web page cannot do on its own.

Performance notes, because they shaped the design:

  * sway is driven over a persistent IPC socket, not by spawning `swaymsg`.
    A subprocess per taskbar tick is brutal on a slow machine, and the shell
    ticks constantly.
  * window state is push-based. A background thread subscribes to sway's event
    stream and forwards changes to the shell over SSE, so nothing polls a
    multi-megabyte window tree on a timer.
  * the launcher is pre-warmed at session start and toggled with sway's
    scratchpad, so opening it is a compositor operation rather than a cold
    Chromium start.

Deliberate constraints: stdlib only, loopback only, and never execute an
arbitrary command string from a page.

NOT a security boundary: every local app shares one origin and one API, so app
"permissions" are scoping and hygiene, not a sandbox.
"""

import contextlib
import glob
import json
import os
import queue
import re
import shlex
import socket
import struct
import shutil
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 7777

PREFIX = "/usr/share/nethos"
SHELL_DIR = os.path.join(PREFIX, "shell")
LIB_DIR = os.path.join(PREFIX, "lib")
APP_DIRS_WEB = [
    os.path.expanduser("~/.local/share/nethos/apps"),   # user apps win
    os.path.join(PREFIX, "apps"),                       # system apps
]
STATE_DIR = os.path.expanduser("~/.local/state/nethos")

APP_DIRS_XDG = [
    os.path.expanduser("~/.local/share/applications"),
    "/usr/local/share/applications",
    "/usr/share/applications",
]

ICON_DIRS = [
    os.path.expanduser("~/.local/share/icons"),
    "/usr/share/icons",
    "/usr/share/pixmaps",
]

# One Chromium profile for the panel, the launcher and every app. Separate
# profiles mean separate Chromium instances, and a cold start per window is
# exactly what made the launcher feel broken.
CHROME_PROFILE = os.path.expanduser("~/.config/nethos-chromium")

BUILTINS = {
    "poweroff": ["systemctl", "poweroff"],
    "reboot": ["systemctl", "reboot"],
    # logout and lock are per-compositor; see session_command(). These entries
    # are the sway spellings and exist so the key is known to be valid.
    "logout": ["swaymsg", "exit"],
    "lock": ["swaylock", "-f", "-c", "0b0e14"],
    "terminal": ["foot"],
    "menu-toggle": None,
}



# ---------------------------------------------------------------------------
# files -- what the explorer and the extractor are built on
# ---------------------------------------------------------------------------

HOME = os.path.expanduser("~")

# Everything is confined to the user's own tree. nethosd listens on localhost
# and anything that can reach it can already run as this user, so this is not a
# security boundary -- it is a guard against a path bug in the explorer walking
# into /proc or /sys and hanging on a pipe.
FILE_ROOTS = [HOME, "/media", "/mnt", "/run/media"]

# Kind decides the icon and what a double-click does. Extension matching only:
# reading the first bytes of every file in a directory to identify it makes
# opening a folder of a thousand files take a second, and the answer is not
# needed until the user acts on one.
KINDS = {
    "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif"),
    "video": (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"),
    "audio": (".mp3", ".flac", ".ogg", ".wav", ".m4a", ".opus"),
    "text":  (".txt", ".md", ".log", ".conf", ".ini", ".json", ".yaml", ".yml",
              ".py", ".js", ".css", ".html", ".sh", ".c", ".h", ".cpp", ".rs"),
    "pdf":   (".pdf",),
    "archive": (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z",
                ".rar", ".deb", ".pkg.tar.zst"),
}


def file_kind(name, is_dir):
    if is_dir:
        return "folder"
    lower = name.lower()
    for kind, exts in KINDS.items():
        if lower.endswith(exts):
            return kind
    return "file"


def safe_path(raw):
    """Resolve a client path, or None if it escapes the allowed roots."""
    if not raw:
        return HOME
    path = os.path.realpath(os.path.expanduser(str(raw)))
    for root in FILE_ROOTS:
        if path == root or path.startswith(root.rstrip("/") + "/"):
            return path
    return None


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%d B" % n


def list_dir(path):
    """One directory. Folders first, then by name, both case-insensitively.

    os.scandir rather than listdir+stat: it carries the type with the entry, so
    a directory of a few thousand files costs one syscall per entry instead of
    two. Hidden files are included and flagged; the client decides.
    """
    out = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir(follow_symlinks=True)
                    st = entry.stat(follow_symlinks=False)
                    size = 0 if is_dir else st.st_size
                    out.append({
                        "name": entry.name,
                        "path": os.path.join(path, entry.name),
                        "dir": is_dir,
                        "hidden": entry.name.startswith("."),
                        "kind": file_kind(entry.name, is_dir),
                        "size": size,
                        "size_h": "" if is_dir else human_size(size),
                        "mtime": int(st.st_mtime),
                    })
                except OSError:
                    # A broken symlink or a file removed mid-walk is not a
                    # reason to fail the whole listing.
                    continue
    except OSError as exc:
        return None, str(exc)
    out.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return out, None


def places():
    """The left-hand list: the user's own directories, then anything mounted."""
    out = [{"name": "Home", "path": HOME, "kind": "home"}]
    for name in ("Desktop", "Documents", "Downloads", "Pictures", "Music",
                 "Videos"):
        p = os.path.join(HOME, name)
        if os.path.isdir(p):
            out.append({"name": name, "path": p, "kind": name.lower()})
    for base in ("/media", "/run/media", "/mnt"):
        try:
            for entry in sorted(os.listdir(base)):
                p = os.path.join(base, entry)
                if os.path.ismount(p) or (os.path.isdir(p) and base != "/mnt"):
                    out.append({"name": entry, "path": p, "kind": "drive"})
                elif os.path.isdir(p):
                    for sub in sorted(os.listdir(p)):
                        sp = os.path.join(p, sub)
                        if os.path.ismount(sp):
                            out.append({"name": sub, "path": sp, "kind": "drive"})
        except OSError:
            continue
    return out


# Archive handling. bsdtar reads every format worth reading -- tar in all its
# compressions, zip, 7z, iso, and Debian's own .deb -- so one tool covers the
# lot rather than dispatching to five.
ARCHIVE_TOOLS = [
    ("bsdtar", ["bsdtar", "-xf", "{src}", "-C", "{dst}"]),
    ("tar",    ["tar", "-xf", "{src}", "-C", "{dst}"]),
    ("unzip",  ["unzip", "-o", "{src}", "-d", "{dst}"]),
]

EXTRACT_JOB = {"active": "", "log": [], "ok": None}


def extract_archive(src, dst):
    """Unpack src into a new directory under dst. Background, reports as it goes."""
    def worker():
        EXTRACT_JOB["active"] = os.path.basename(src)
        EXTRACT_JOB["log"] = []
        EXTRACT_JOB["ok"] = None
        EVENTS.publish("extract", {"state": "working", "name": os.path.basename(src)})
        # Into a folder named after the archive, never loose into the current
        # directory: a tarball with a hundred files at its root turns the
        # folder you were looking at into a mess you have to clean up by hand.
        stem = os.path.basename(src)
        for suffix in (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst", ".pkg.tar.zst"):
            if stem.lower().endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        else:
            stem = os.path.splitext(stem)[0]
        target = os.path.join(dst, stem)
        n = 2
        while os.path.exists(target):
            target = os.path.join(dst, "%s (%d)" % (stem, n))
            n += 1
        rc = 1
        try:
            os.makedirs(target, exist_ok=True)
            for name, template in ARCHIVE_TOOLS:
                if not shutil.which(name):
                    continue
                cmd = [c.format(src=src, dst=target) for c in template]
                p = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=1800)
                EXTRACT_JOB["log"] = ((p.stdout or "") + (p.stderr or "")).splitlines()[-60:]
                rc = p.returncode
                if rc == 0:
                    break
            else:
                if rc != 0:
                    EXTRACT_JOB["log"].append("no extraction tool available")
        except (OSError, subprocess.SubprocessError) as exc:
            EXTRACT_JOB["log"].append(str(exc))
            rc = 1
        if rc != 0:
            # Do not leave an empty directory behind after a failure.
            try:
                if os.path.isdir(target) and not os.listdir(target):
                    os.rmdir(target)
            except OSError:
                pass
        EXTRACT_JOB["ok"] = (rc == 0)
        EXTRACT_JOB["active"] = ""
        EVENTS.publish("extract", {"state": "done", "ok": rc == 0,
                                   "target": target})
    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------------------
# packages -- what the App Store is built on
# ---------------------------------------------------------------------------

# One install at a time. npkg writes /var/lib/npkg and unpacks into /usr; two
# of them at once is how a package database gets corrupted, and the store makes
# it easy to click twice.
PKG_LOCK = threading.Lock()
PKG_JOB = {"active": "", "log": [], "ok": None}


def npkg_run(args, timeout=600):
    """Run npkg and return (rc, output). Never raises."""
    try:
        p = subprocess.run(["npkg"] + args, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def npkg_installed():
    """Names of installed packages, as a set."""
    rc, out = npkg_run(["list"], timeout=60)
    if rc != 0:
        return set()
    names = set()
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # "name  version  ..." -- the first field is the name in every format
        # npkg list has used.
        names.add(line.split()[0].lstrip("*").strip())
    return names


def npkg_search(query):
    """Search the repositories. Returns a list of {id, name, version, summary}."""
    if not query or len(query) < 2:
        return []
    rc, out = npkg_run(["search", query], timeout=120)
    if rc != 0:
        return []
    found, seen = [], set()
    for line in out.splitlines():
        # npkg marks installed packages with a leading "* " as its own token,
        # so strip it before splitting -- otherwise every package you already
        # have is parsed as a package named "*" and dropped, and the store
        # shows you only the things you have not got.
        line = re.sub(r"^\s*\*\s+", "  ", line)
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        name = parts[0].lstrip("*").strip()
        # Skip headers like "Debian trixie/amd64" and "index: N packages",
        # and the usage hint npkg prints after the results -- "install with:
        # npkg fetch <name>" parsed as a package called "install" at version
        # "with:", which then appeared in the store as something installable.
        if not name or name.endswith(":") or name in seen:
            continue
        if not re.match(r"^[a-z0-9][a-z0-9.+-]*$", name):
            continue
        # A version has a digit in it. Nothing else in npkg's output does.
        if not re.search(r"\d", parts[1]) or parts[1].endswith(":"):
            continue
        seen.add(name)
        found.append({
            "id": name,
            "name": name,
            "version": parts[1],
            "summary": (parts[2].strip() if len(parts) > 2 else ""),
        })
        if len(found) >= 60:
            break
    return found


def pkg_job(action, names):
    """Install or remove, in the background, reporting as it goes.

    Held behind PKG_LOCK: npkg is not safe to run twice at once, and a store
    makes double-clicking easy. sudo -n, never a prompt -- there is nowhere
    for a password prompt to appear from here, so a missing sudoers rule has
    to fail loudly rather than hang forever waiting on a tty nobody can see.
    """
    def worker():
        with PKG_LOCK:
            PKG_JOB["active"] = " ".join(names)
            PKG_JOB["log"] = []
            PKG_JOB["ok"] = None
            EVENTS.publish("package", {"state": "working",
                                       "packages": names, "action": action})
            # -y because there is no terminal here to answer "continue?" on.
            # With stdin closed and no --yes, npkg's input() raises EOFError
            # and the install dies with a traceback rather than a refusal.
            cmd = ["sudo", "-n", "npkg",
                   "fetch" if action == "install" else "remove", "-y"] + names
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, text=True)
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        PKG_JOB["log"].append(line)
                        del PKG_JOB["log"][:-200]
                        EVENTS.publish("package", {"state": "log",
                                                   "line": line})
                rc = proc.wait(timeout=900)
            except (OSError, subprocess.SubprocessError) as exc:
                PKG_JOB["log"].append(str(exc))
                rc = 1
            PKG_JOB["ok"] = (rc == 0)
            PKG_JOB["active"] = ""
            _app_cache["at"] = 0.0        # a new .desktop may have appeared
            EVENTS.publish("package", {"state": "done", "ok": rc == 0,
                                       "packages": names, "action": action})
            EVENTS.publish("apps", {})
    threading.Thread(target=worker, daemon=True).start()


SETTINGS_PATH = os.path.expanduser("~/.config/nethos/settings.json")

# Every setting the desktop has, its default, and what it accepts. Kept in one
# table so the Settings app can render itself from the schema rather than
# hardcoding a form that drifts from what the daemon actually stores.
SETTINGS_SCHEMA = [
    {"key": "theme", "label": "Theme", "group": "Appearance",
     "type": "choice", "options": ["auto", "light", "dark"], "default": "auto",
     "help": "Auto follows the time of day."},
    {"key": "accent", "label": "Accent", "group": "Appearance",
     "type": "colour", "default": "#3b6ea5",
     "help": "Used for focus rings and the active item."},
    {"key": "wallpaper", "label": "Wallpaper", "group": "Appearance",
     "type": "choice",
     "options": ["dawn", "slate", "meadow", "dusk"], "default": "dawn"},
    {"key": "font_scale", "label": "Text size", "group": "Appearance",
     "type": "range", "min": 85, "max": 130, "step": 5, "default": 100,
     "unit": "%"},
    {"key": "dock_autohide", "label": "Hide the dock", "group": "Dock",
     "type": "bool", "default": True,
     "help": "Slides out of the way until you reach for it."},
    {"key": "dock_size", "label": "Icon size", "group": "Dock",
     "type": "range", "min": 36, "max": 72, "step": 4, "default": 48,
     "unit": "px"},
    {"key": "panel_clock_seconds", "label": "Show seconds",
     "group": "Panel", "type": "bool", "default": False},
    {"key": "animations", "label": "Animations", "group": "Motion",
     "type": "bool", "default": True,
     "help": "Turn off on a machine without a GPU."},
]
SETTINGS_DEFAULTS = {s["key"]: s["default"] for s in SETTINGS_SCHEMA}


def read_settings():
    """Stored settings over defaults. A corrupt file is not fatal: a desktop
    that will not start because a JSON file lost a brace is a worse failure
    than one that comes up with the defaults and says so."""
    out = dict(SETTINGS_DEFAULTS)
    try:
        with open(SETTINGS_PATH) as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            out.update({k: v for k, v in stored.items() if k in out})
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as exc:
        diag("settings", "unreadable, using defaults: %s" % exc)
    return out


def write_settings(changes):
    """Merge and persist. Returns the full settings after the change.

    Written to a temporary file and renamed, so an interrupted write cannot
    leave a half-written file that the next boot refuses to parse."""
    current = read_settings()
    valid = {s["key"]: s for s in SETTINGS_SCHEMA}
    for key, value in (changes or {}).items():
        spec = valid.get(key)
        if not spec:
            continue
        if spec["type"] == "bool":
            current[key] = bool(value)
        elif spec["type"] == "choice":
            if value in spec["options"]:
                current[key] = value
        elif spec["type"] == "range":
            try:
                n = int(value)
            except (TypeError, ValueError):
                continue
            current[key] = max(spec["min"], min(spec["max"], n))
        else:
            current[key] = str(value)[:64]
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(current, fh, indent=2, sort_keys=True)
    os.replace(tmp, SETTINGS_PATH)
    EVENTS.publish("settings", current)
    return current


def session_command(builtin):
    """Ending or locking a session, in the running compositor's dialect.

    "Log out" ran `swaymsg exit` regardless of what was actually running, so
    under Wayfire it talked to a socket that does not exist and the button did
    nothing at all -- no error, no log line, no session ended.

    Wayfire has no "quit" over its IPC, so the session is ended through logind
    instead, which is both compositor-agnostic and the thing that actually
    tears the session down.
    """
    kind = backend()
    if builtin == "lock":
        return BUILTINS["lock"]
    if kind == "sway":
        return ["swaymsg", "exit"]
    if kind == "hypr":
        return ["hyprctl", "dispatch", "exit"]
    return ["loginctl", "terminate-session",
            os.environ.get("XDG_SESSION_ID", "self")]

PANEL_MARK = "panel.html"
MENU_MARK = "menu.html"
MENU_CRITERIA = r'[app_id="^chrome-.*menu\.html.*$"]'

CHROME_BASE = [
    "chromium",
    "--ozone-platform=wayland",
    "--enable-features=UseOzonePlatform",
    "--disable-gpu",
    "--password-store=basic",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-sync",
    "--user-data-dir=" + CHROME_PROFILE,

    # Memory. Every NETHOS surface -- panel, launcher, each app -- is served
    # from the same origin on loopback, so site isolation buys us nothing and
    # costs a renderer process per window. Collapsing them onto one renderer is
    # the single biggest saving available without changing engines.
    "--process-per-site",
    "--disable-site-isolation-trials",
    "--disable-features=TranslateUI,MediaRouter,SitePerProcess,IsolateOrigins,"
    "OptimizationHints,CalculateNativeWinOcclusion",
    "--renderer-process-limit=2",
    # A desktop shell has no business keeping spare renderers warm.
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-extensions",
    "--disable-component-update",
    "--disable-breakpad",
    "--no-zygote",
    "--disable-dev-shm-usage",
]


def is_shell_surface(app_id):
    return bool(app_id) and (PANEL_MARK in app_id or MENU_MARK in app_id)


# --------------------------------------------------------------------------
# sway IPC
# --------------------------------------------------------------------------

class SwayIPC:
    """Minimal i3/sway IPC client over the unix socket.

    Replaces shelling out to swaymsg. The protocol is small: a fixed header of
    magic + length + type, then a JSON payload.
    """

    MAGIC = b"i3-ipc"
    RUN_COMMAND = 0
    GET_WORKSPACES = 1
    SUBSCRIBE = 2
    GET_OUTPUTS = 3
    GET_TREE = 4

    def __init__(self):
        self._sock = None
        self._lock = threading.Lock()

    @staticmethod
    def socket_path():
        path = os.environ.get("SWAYSOCK")
        if path and os.path.exists(path):
            return path
        # sway was started before nethosd inherited an environment, or the
        # session was restarted; fall back to the newest socket for this user.
        candidates = glob.glob("/run/user/%d/sway-ipc.*.sock" % os.getuid())
        candidates.sort(key=lambda p: os.stat(p).st_mtime, reverse=True)
        return candidates[0] if candidates else None

    @classmethod
    def connect(cls):
        path = cls.socket_path()
        if not path:
            raise OSError("no sway socket")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(path)
        return sock

    @classmethod
    def send(cls, sock, mtype, payload=b""):
        sock.sendall(cls.MAGIC + struct.pack("=II", len(payload), mtype) + payload)

    @staticmethod
    def recv_exactly(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("sway closed the connection")
            buf += chunk
        return buf

    @classmethod
    def recv(cls, sock):
        header = cls.recv_exactly(sock, 14)
        length, mtype = struct.unpack("=II", header[6:14])
        body = cls.recv_exactly(sock, length) if length else b"{}"
        try:
            return mtype, json.loads(body)
        except ValueError:
            return mtype, None

    def request(self, mtype, payload=b""):
        """Send a request on the shared connection, reconnecting once."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._sock = self.connect()
                    self.send(self._sock, mtype, payload)
                    _, data = self.recv(self._sock)
                    return data
                except (OSError, struct.error):
                    try:
                        if self._sock:
                            self._sock.close()
                    except OSError:
                        pass
                    self._sock = None
                    if attempt == 2:
                        return None
        return None

    def command(self, cmd):
        return self.request(self.RUN_COMMAND, cmd.encode())

    def get_tree(self):
        return self.request(self.GET_TREE)

    def get_outputs(self):
        return self.request(self.GET_OUTPUTS)


class HyprIPC:
    """Hyprland IPC.

    Simpler than sway's: two unix sockets, line oriented. `.socket.sock` takes
    a request and returns a reply, `.socket2.sock` streams events as text.
    Hyprland is what gives NETHOS rounded corners and real blur -- sway can do
    neither -- so it is the default when present.
    """

    def __init__(self):
        self._lock = threading.Lock()

    @staticmethod
    def base():
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        runtime = os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
        if sig:
            return os.path.join(runtime, "hypr", sig)
        candidates = sorted(glob.glob(os.path.join(runtime, "hypr", "*")),
                            key=lambda p: os.stat(p).st_mtime, reverse=True)
        return candidates[0] if candidates else None

    @classmethod
    def available(cls):
        base = cls.base()
        return bool(base) and os.path.exists(os.path.join(base, ".socket.sock"))

    def _request(self, text):
        base = self.base()
        if not base:
            return None
        path = os.path.join(base, ".socket.sock")
        # closing() rather than a bare close() at the end: the close used to sit
        # on the success path only, so every failed connect, timeout or short
        # read leaked a file descriptor. A daemon that leaks descriptors keeps
        # working until it hits its limit and then cannot accept() any more --
        # at which point already-open connections carry on and new ones are
        # refused. The clock stops, buttons do nothing, and the event stream
        # still works, which reads as "the API died" when it is still running.
        try:
            with self._lock:
                with contextlib.closing(
                        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as sock:
                    sock.settimeout(5)
                    sock.connect(path)
                    sock.sendall(text.encode())
                    chunks = []
                    while True:
                        chunk = sock.recv(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", "replace")
        except OSError:
            return None

    def json(self, what):
        raw = self._request("j/" + what)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def dispatch(self, *args):
        return self._request("dispatch " + " ".join(str(a) for a in args))

    def keyword(self, *args):
        return self._request("keyword " + " ".join(str(a) for a in args))


HYPR = HyprIPC()
SWAY = SwayIPC()


def _focused_floating(node):
    """The focused window, if it is floating. Depth-first, focus follows."""
    if node.get("focused") and node.get("app_id") is not None:
        if node.get("type") == "floating_con" or node.get("floating") in (
                "user_on", "auto_on"):
            return {"id": node.get("id"), "rect": node.get("rect") or {}}
        return None
    for key in ("nodes", "floating_nodes"):
        for child in node.get(key) or []:
            found = _focused_floating(child)
            if found:
                return found
    return None


class WayfireIPC:
    """Wayfire's IPC, which is JSON behind a 4-byte little-endian length.

    Enabled by the ipc and ipc-rules plugins; the socket path arrives in
    WAYFIRE_SOCKET. Without this the panel has no window list under Wayfire,
    because sway's IPC is a different protocol on a different socket.
    """

    _lock = threading.Lock()

    @staticmethod
    def socket_path():
        path = os.environ.get("WAYFIRE_SOCKET", "")
        if path and os.path.exists(path):
            return path
        for candidate in glob.glob("/tmp/wayfire-wayland-*.socket"):
            return candidate
        return None

    @classmethod
    def available(cls):
        return bool(cls.socket_path())

    @classmethod
    def call(cls, method, **data):
        path = cls.socket_path()
        if not path:
            return None
        payload = json.dumps({"method": method, "data": data}).encode()
        try:
            with cls._lock:
                with contextlib.closing(
                        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as sock:
                    # 1s, not 3: this sits between a click and the
                    # screen, and three seconds of it is a hang.
                    sock.settimeout(1.0)
                    sock.connect(path)
                    sock.sendall(struct.pack("=I", len(payload)) + payload)
                    head = sock.recv(4)
                    if len(head) < 4:
                        return None
                    size = struct.unpack("=I", head)[0]
                    buf = b""
                    while len(buf) < size:
                        chunk = sock.recv(size - len(buf))
                        if not chunk:
                            break
                        buf += chunk
            return json.loads(buf.decode("utf-8", "replace"))
        except (OSError, ValueError, struct.error):
            return None

    _cache = {"at": 0.0, "views": []}

    @classmethod
    def views(cls):
        # The panel asks on every tick and the answer barely changes. Without
        # this each tick is a fresh connect, and several of those queued behind
        # each other is what a slow desktop is made of.
        now = time.time()
        if now - cls._cache["at"] < 0.8:
            return cls._cache["views"]
        reply = cls.call("window-rules/list-views") or []
        out = []
        for v in reply if isinstance(reply, list) else []:
            # Layer surfaces and Wayfire's own bits are not windows.
            if v.get("role") != "toplevel" or not v.get("mapped", True):
                continue
            out.append({
                "id": str(v.get("id")),
                "title": v.get("title") or "",
                "app_id": v.get("app-id") or v.get("app_id") or "",
                "focused": bool(v.get("activated")),
                "workspace": "1",
                "floating": True,
                "nethos_app": "",
            })
        cls._cache = {"at": now, "views": out}
        return out


def backend():
    """Which compositor are we driving? Decided per call so a session that
    restarts under another one keeps working without restarting nethosd.

    Wayfire is recognised but not yet driven: it does its own snapping,
    decorations and blur, so the things nethosd adds to sway are already there
    -- but its window list needs a Wayfire IPC backend that does not exist
    here. Returning "wayfire" makes that explicit, so the sway paths decline
    instead of firing IPC at a socket that will never answer.
    """
    if HyprIPC.available():
        return "hypr"
    if WayfireIPC.available():
        return "wayfire"
    return "sway"


def spawn(argv):
    """Start a detached process. nethosd runs inside the session, so the child
    inherits WAYLAND_DISPLAY and friends.

    Popen succeeding only means the binary existed. A program that starts and
    dies a tenth of a second later looked exactly like one that launched, which
    is how "apps do not open" stayed a mystery: the launcher reported success
    every time. Output goes to the log now, and a child that exits straight
    away is reported as the failure it is.
    """
    log = os.path.expanduser("~/.cache/nethos/launch.log")
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
        out = open(log, "a")
        out.write("\n--- %s  %s\n" % (time.strftime("%H:%M:%S"), " ".join(argv)))
        out.flush()
    except OSError:
        out = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            argv, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=out, stderr=out,
        )
    except OSError as exc:
        diag("launch", "%s: %s" % (argv[0], exc))
        return False

    def watch():
        time.sleep(1.5)
        code = proc.poll()
        if code is not None and code != 0:
            diag("launch", "%s exited %s straight away -- see %s"
                 % (argv[0], code, log))

    threading.Thread(target=watch, daemon=True).start()
    diag("launch", " ".join(argv))
    return True


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

class Events:
    def __init__(self):
        self.lock = threading.Lock()
        self.subscribers = set()
        self.generation = 0

    def subscribe(self):
        q = queue.Queue(maxsize=64)
        with self.lock:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subscribers.discard(q)

    def publish(self, kind, payload=None):
        msg = json.dumps({"type": kind, "data": payload or {},
                          "generation": self.generation})
        with self.lock:
            dead = []
            for q in self.subscribers:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self.subscribers.discard(q)

    def bump(self, reason="manual"):
        with self.lock:
            self.generation += 1
        self.publish("reload", {"reason": reason})
        return self.generation


EVENTS = Events()
_app_cache = {"at": 0.0, "apps": []}


def watch_files(paths, interval=1.0):
    """Poll the served tree for edits and trigger a live reload."""
    def snapshot():
        stamps = {}
        for root in paths:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    try:
                        stamps[full] = os.stat(full).st_mtime_ns
                    except OSError:
                        pass
        return stamps

    previous = snapshot()
    while True:
        time.sleep(interval)
        try:
            current = snapshot()
        except OSError:
            continue
        if current != previous:
            previous = current
            _app_cache["at"] = 0.0
            EVENTS.bump("files-changed")


def compositor_event_loop():
    """Push window changes to the shell instead of letting it poll.

    The panel no longer asks "what windows exist" on a timer; it is told.
    """
    while True:
        try:
            kind = backend()
            if kind == "hypr":
                hypr_event_loop()
            elif kind == "wayfire":
                wayfire_event_loop()
            else:
                sway_event_loop()
        except (OSError, struct.error):
            pass
        EVENTS.publish("disconnected", {})
        time.sleep(2)


# The events worth a taskbar redraw. Deliberately not view-geometry-changed:
# that fires for every pixel of a drag and would redraw the panel continuously
# while a window is being moved.
WAYFIRE_INTERESTING = (
    "view-mapped", "view-unmapped", "view-focused",
    "view-title-changed", "view-app-id-changed",
)


def wayfire_event_loop():
    """Subscribe to Wayfire's event stream.

    Without this the taskbar under Wayfire was never told anything. The
    dispatch above sent every non-Hyprland session to sway_event_loop(), which
    opens SWAYSOCK -- absent under Wayfire -- so it raised OSError, the caller
    swallowed it, slept two seconds and tried again, forever. Nothing logged a
    failure and the panel still worked, because a 20s poll in the shell was
    quietly carrying it. Closing a window from the top bar therefore took up
    to twenty seconds to leave the taskbar, which reads as slow IPC even
    though /api/windows answers in about two milliseconds.
    """
    path = WayfireIPC.socket_path()
    if not path:
        raise OSError("no wayfire socket")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(None)
    sock.connect(path)
    with contextlib.closing(sock):
        payload = json.dumps({
            "method": "window-rules/events/watch",
            "data": {"events": list(WAYFIRE_INTERESTING)},
        }).encode()
        sock.sendall(struct.pack("=I", len(payload)) + payload)
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("wayfire closed the event socket")
            buf += chunk
            # One recv can carry several frames, or half of one.
            while len(buf) >= 4:
                size = struct.unpack("=I", buf[:4])[0]
                if len(buf) < 4 + size:
                    break
                frame, buf = buf[4:4 + size], buf[4 + size:]
                try:
                    msg = json.loads(frame.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("event") not in WAYFIRE_INTERESTING:
                    continue
                # views() holds its answer for 0.8s. Publishing without
                # clearing that means the panel asks the instant it is told
                # and gets the list from before the window closed.
                WayfireIPC._cache = {"at": 0.0, "views": []}
                EVENTS.publish("windows", {})


def sway_event_loop():
    sock = SwayIPC.connect()
    sock.settimeout(None)
    SwayIPC.send(sock, SwayIPC.SUBSCRIBE, b'["window","workspace"]')
    SwayIPC.recv(sock)                      # subscription ack
    while True:
        mtype, data = SwayIPC.recv(sock)
        if not (mtype & 0x80000000):
            continue
        if isinstance(data, dict) and data.get("change") == "new":
            apply_window_rules_sway(data.get("container") or {})
        EVENTS.publish("windows", {})


HYPR_INTERESTING = (
    "openwindow", "closewindow", "movewindow", "activewindow",
    "windowtitle", "workspace", "fullscreen", "changefloatingmode",
)


def hypr_event_loop():
    base = HyprIPC.base()
    if not base:
        raise OSError("no hyprland socket")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(os.path.join(base, ".socket2.sock"))
    buf = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("hyprland closed the event socket")
        buf += chunk
        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            text = line.decode("utf-8", "replace")
            name, _, payload = text.partition(">>")
            if name == "openwindow":
                # ADDRESS,WORKSPACE,CLASS,TITLE
                parts = payload.split(",", 3)
                if len(parts) >= 3:
                    apply_window_rules_hypr("0x" + parts[0], parts[2])
            if name in HYPR_INTERESTING:
                EVENTS.publish("windows", {})


# --------------------------------------------------------------------------
# window model
# --------------------------------------------------------------------------

def walk_tree(node, out, workspace=None):
    if node is None:
        return
    if node.get("type") == "workspace":
        workspace = node.get("name")
    if node.get("type") in ("con", "floating_con") and node.get("app_id") is not None \
            or node.get("window_properties"):
        app_id = node.get("app_id") or (node.get("window_properties") or {}).get("class")
        if not is_shell_surface(app_id):
            out.append({
                "id": node.get("id"),
                "title": node.get("name") or "",
                "app_id": app_id or "",
                "focused": bool(node.get("focused")),
                "workspace": workspace,
                "floating": node.get("type") == "floating_con",
                "nethos_app": nethos_app_for(app_id),
            })
    for kid in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
        walk_tree(kid, out, workspace)


def list_windows():
    if backend() == "wayfire":
        return WayfireIPC.views()
    if backend() == "hypr":
        return hypr_windows()
    tree = SWAY.get_tree()
    if not isinstance(tree, dict):
        return []
    out = []
    walk_tree(tree, out)
    # sway ids are ints; the API uses strings so both backends look the same
    # to the shell.
    for w in out:
        w["id"] = str(w["id"])
    return out


def hypr_windows():
    clients = HYPR.json("clients")
    if not isinstance(clients, list):
        return []
    active = HYPR.json("activewindow") or {}
    active_addr = active.get("address") if isinstance(active, dict) else None

    out = []
    for c in clients:
        app_id = c.get("class") or c.get("initialClass") or ""
        if is_shell_surface(app_id):
            continue
        out.append({
            "id": c.get("address", ""),
            "title": c.get("title") or "",
            "app_id": app_id,
            "focused": c.get("address") == active_addr,
            "workspace": (c.get("workspace") or {}).get("name"),
            "floating": bool(c.get("floating")),
            "nethos_app": nethos_app_for(app_id),
        })
    return out


def walk_all_app_ids(node, out):
    if not isinstance(node, dict):
        return
    if node.get("app_id"):
        out.append(node["app_id"])
    for kid in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
        walk_all_app_ids(kid, out)


def output_size(default=(1440, 900)):
    if backend() == "hypr":
        monitors = HYPR.json("monitors")
        if isinstance(monitors, list) and monitors:
            m = monitors[0]
            if m.get("width") and m.get("height"):
                scale = m.get("scale") or 1
                return int(m["width"] / scale), int(m["height"] / scale)
        return default
    outputs = SWAY.get_outputs()
    if isinstance(outputs, list) and outputs:
        rect = outputs[0].get("rect") or {}
        if rect.get("width") and rect.get("height"):
            return rect["width"], rect["height"]
    return default


def nethos_app_for(app_id):
    """Which NETHOS app, if any, a Chromium app_id belongs to.

    Chromium builds app_id from the URL, so /apps/system/index.html becomes
    chrome-127.0.0.1__apps_system_index.html-Default. Rather than parse that
    fragile string, check it against the app ids we already know about.
    """
    if not app_id or "__apps_" not in app_id:
        return ""
    for app in load_apps():
        if app.get("source") == "nethos" and ("_apps_%s_" % app["id"]) in app_id:
            return app["id"]
    return ""


def widget_geometry(app):
    """Where a widget sits, from its manifest."""
    ow, oh = output_size()
    w, h = app["width"], app["height"]
    margin, top = 16, 56
    return {
        "top-right":    (ow - w - margin, top),
        "top-left":     (margin, top),
        "bottom-right": (ow - w - margin, oh - h - margin - 90),
        "bottom-left":  (margin, oh - h - margin - 90),
        "center":       ((ow - w) // 2, (oh - h) // 2),
    }.get(app.get("position", "top-right"), (ow - w - margin, top))


def apply_window_rules_hypr(address, app_class):
    which = nethos_app_for(app_class)
    if not which:
        return
    app = find_app(which)
    if not app:
        return
    target = "address:%s" % address

    if app.get("mode") == "widget":
        x, y = widget_geometry(app)
        HYPR.dispatch("setfloating", target)
        HYPR.dispatch("resizewindowpixel", "exact %d %d,%s" % (app["width"], app["height"], target))
        HYPR.dispatch("movewindowpixel", "exact %d %d,%s" % (x, y, target))
        HYPR.dispatch("pin", target)          # follow across workspaces
    elif app.get("floating"):
        HYPR.dispatch("setfloating", target)
        HYPR.dispatch("resizewindowpixel",
                      "exact %d %d,%s" % (app["width"], app["height"], target))
        HYPR.dispatch("centerwindow")


def apply_window_rules_sway(container):
    """Place a newly mapped NETHOS app window according to its manifest.

    A compositor's own rules cannot read our manifests, so window vs widget
    placement is decided here, on the compositor's event stream.
    """
    app_id = container.get("app_id") or ""
    which = nethos_app_for(app_id)
    if not which:
        return
    app = find_app(which)
    if not app:
        return

    con_id = container.get("id")
    if not isinstance(con_id, int):
        return
    sel = "[con_id=%d]" % con_id

    if app.get("mode") == "widget":
        # A widget is furniture: it floats above the desktop, follows you
        # between workspaces, has no border, and never takes focus.
        x, y = widget_geometry(app)
        SWAY.command(
            "%s floating enable, border none, sticky enable, "
            "resize set width %d px height %d px, move absolute position %d %d"
            % (sel, app["width"], app["height"], x, y)
        )
    elif app.get("floating"):
        SWAY.command(
            "%s floating enable, border pixel 2, "
            "resize set width %d px height %d px, move position center"
            % (sel, app["width"], app["height"])
        )
    else:
        # A real window: leave it to the tiling layout like any other program.
        SWAY.command("%s border pixel 2" % sel)


# --------------------------------------------------------------------------
# icons
# --------------------------------------------------------------------------

_icon_index = {"map": {}, "lock": threading.Lock(), "ready": threading.Event()}
ICON_EXT_RANK = {".svg": 3, ".png": 2, ".xpm": 1}


def build_icon_index():
    """Index every icon file once, best format and largest size winning.

    Walking the icon themes on demand for each app would be slow; doing it once
    in the background costs a second at startup and makes lookups a dict hit.
    """
    index = {}
    for base in ICON_DIRS:
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            # crude size hint from paths like .../128x128/apps/foo.png
            size = 0
            m = re.search(r"/(\d+)x\1/", dirpath)
            if m:
                size = int(m.group(1))
            if "scalable" in dirpath:
                size = 1024
            for name in files:
                stem, ext = os.path.splitext(name)
                rank = ICON_EXT_RANK.get(ext.lower())
                if not rank:
                    continue
                score = (rank, size)
                current = index.get(stem)
                if current is None or score > current[0]:
                    index[stem] = (score, os.path.join(dirpath, name))
    with _icon_index["lock"]:
        _icon_index["map"] = {k: v[1] for k, v in index.items()}
    _icon_index["ready"].set()


def resolve_icon(name, wait=15):
    """Path for an icon name, waiting for the index if it is still building.

    Called from a request thread, so blocking briefly is fine and is much
    better than the alternative: returning "no icon" during startup and having
    that answer cached, which is why icons silently never appeared.
    """
    if not name:
        return None
    if os.path.isabs(name) and os.path.isfile(name):
        return name
    _icon_index["ready"].wait(timeout=wait)
    with _icon_index["lock"]:
        return _icon_index["map"].get(name)


# --------------------------------------------------------------------------
# NETHOS web apps
# --------------------------------------------------------------------------

SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def app_root(app_id):
    if not SAFE_ID.match(app_id or ""):
        return None
    for base in APP_DIRS_WEB:
        candidate = os.path.join(base, app_id)
        if os.path.isdir(candidate):
            return candidate
    return None


def read_manifest(directory):
    try:
        with open(os.path.join(directory, "app.json"), "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or not manifest.get("id"):
        return None
    if not SAFE_ID.match(manifest["id"]):
        return None

    window = manifest.get("window") or {}
    mode = manifest.get("mode", "window")
    if mode not in ("window", "widget"):
        mode = "window"

    icon = manifest.get("icon", "")
    icon_url = ""
    # A manifest icon can be a file shipped with the app, or one or two
    # characters used as a text tile.
    if icon and os.path.isfile(os.path.join(directory, icon)):
        icon_url = "/apps/%s/%s" % (manifest["id"], icon)

    return {
        "id": manifest["id"],
        "name": manifest.get("name") or manifest["id"],
        "comment": manifest.get("description", ""),
        "icon": icon,
        "icon_url": icon_url,
        "version": manifest.get("version", "0.0.0"),
        "categories": manifest.get("categories") or ["NETHOS"],
        "entry": manifest.get("entry", "index.html"),
        "permissions": manifest.get("permissions") or [],
        "mode": mode,
        "position": manifest.get("position", "top-right"),
        "floating": bool(window.get("floating", False)),
        "width": int(window.get("width", 960)),
        "height": int(window.get("height", 640)),
        "source": "nethos",
        "terminal": False,
    }


def load_web_apps():
    found, seen = [], set()
    for base in APP_DIRS_WEB:
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            directory = os.path.join(base, name)
            if not os.path.isdir(directory) or name in seen:
                continue
            manifest = read_manifest(directory)
            if manifest:
                seen.add(name)
                found.append(manifest)
    return found


def launch_web_app(app):
    """Run a NETHOS app in nethos-view, the same host the shell uses.

    These used to start a second Chromium in --app mode: a whole browser, its
    own profile directory and several hundred megabytes, to show a page the
    shell's own engine could already draw. It also meant NETHOS apps failed in
    ways real applications did not, because they depended on Chromium starting
    correctly -- and on hardware where its GPU path is broken it draws nothing
    at all.

    nethos-view is already installed, already hosts WebKit, and gives the app a
    normal window that the compositor and the taskbar treat like any other.
    """
    url = "http://%s:%d/apps/%s/%s" % (HOST, PORT, app["id"], app["entry"])
    spec = "url=%s,role=window,name=%s,title=%s,width=%d,height=%d,transparent=0" % (
        url, app["id"], app.get("name", app["id"]),
        app.get("width", 900), app.get("height", 650))
    if spawn(["nethos-view", spec]):
        return True
    # Chromium remains the fallback: an app that opens in the wrong engine
    # beats an app that does not open.
    diag("launch", "nethos-view failed for %s; falling back to chromium" % app["id"])
    return spawn(CHROME_BASE + [
        "--app=" + url,
        "--window-size=%d,%d" % (app["width"], app["height"]),
    ])


# --------------------------------------------------------------------------
# .desktop apps
# --------------------------------------------------------------------------

EXEC_FIELD_CODES = re.compile(r"%[fFuUdDnNickvm]")


def parse_desktop(path):
    entry, in_group = {}, False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    in_group = line == "[Desktop Entry]"
                    continue
                if not in_group or "=" not in line or line.startswith("#"):
                    continue
                key, _, val = line.partition("=")
                entry.setdefault(key.strip(), val.strip())
    except OSError:
        return None

    if entry.get("Type", "Application") != "Application":
        return None
    if entry.get("NoDisplay", "").lower() == "true":
        return None
    if entry.get("Hidden", "").lower() == "true":
        return None
    if not entry.get("Exec") or not entry.get("Name"):
        return None

    icon = entry.get("Icon", "")
    # Advertise the icon URL without resolving it here. Resolution needs the
    # icon index, this runs on the cached app-list path, and a miss during
    # startup would be cached as "no icon". The endpoint 404s if it cannot find
    # the file and the shell falls back to initials.
    return {
        "id": os.path.basename(path),
        "name": entry["Name"],
        "comment": entry.get("Comment", ""),
        "icon": icon,
        "icon_url": "/api/icon/" + urllib.parse.quote(icon) if icon else "",
        "categories": [c for c in entry.get("Categories", "").split(";") if c],
        "terminal": entry.get("Terminal", "").lower() == "true",
        "mode": "window",
        "source": "desktop",
        "_exec": entry["Exec"],
    }


def load_apps(force=False):
    now = time.time()
    if not force and now - _app_cache["at"] < 30 and _app_cache["apps"]:
        return _app_cache["apps"]

    apps = load_web_apps()
    seen = set()
    for directory in APP_DIRS_XDG:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".desktop") or name in seen:
                continue
            app = parse_desktop(os.path.join(directory, name))
            if app:
                seen.add(name)
                apps.append(app)

    apps.sort(key=lambda a: (a["source"] != "nethos", a["name"].lower()))
    _app_cache.update(at=now, apps=apps)
    return apps


def find_app(app_id):
    for app in load_apps():
        if app["id"] == app_id:
            return app
    return None


def launch_desktop_app(app):
    line = EXEC_FIELD_CODES.sub("", app["_exec"]).strip()
    try:
        argv = shlex.split(line)
    except ValueError:
        return False
    if not argv:
        return False
    if app["terminal"]:
        argv = ["foot", "-e"] + argv
    return spawn(argv)


# --------------------------------------------------------------------------
# launcher
# --------------------------------------------------------------------------

def window_action(action, wid):
    """Act on a window. Ids are opaque strings: a sway con_id, a Hyprland
    address or a Wayfire view id, so the shell never has to know which
    compositor is running."""
    if backend() == "wayfire":
        try:
            view = int(wid)
        except (TypeError, ValueError):
            return False
        if action == "focus":
            return WayfireIPC.call("window-rules/focus-view", id=view) is not None
        if action == "close":
            return WayfireIPC.call("window-rules/close-view", id=view) is not None
        if action in ("fullscreen", "maximize"):
            return WayfireIPC.call("window-rules/configure-view", id=view,
                                   maximized=True) is not None
        if action == "minimize":
            return WayfireIPC.call("window-rules/minimize-view", id=view,
                                   state=True) is not None
        return False
    if backend() == "hypr":
        target = "address:%s" % wid
        if action == "focus":
            HYPR.dispatch("focuswindow", target)
        elif action == "close":
            HYPR.dispatch("closewindow", target)
        elif action == "fullscreen":
            HYPR.dispatch("focuswindow", target)
            HYPR.dispatch("fullscreen", "1")
        elif action == "popout":
            HYPR.dispatch("unpin", target)
            HYPR.dispatch("settiled", target)
            HYPR.dispatch("focuswindow", target)
        elif action == "float":
            HYPR.dispatch("setfloating", target)
        else:
            return False
        return True

    try:
        sel = "[con_id=%d]" % int(wid)
    except (TypeError, ValueError):
        return False
    commands = {
        "focus": "%s focus",
        "close": "%s kill",
        "fullscreen": "%s fullscreen toggle",
        "popout": "%s floating disable, sticky disable, border pixel 2, focus",
        "float": "%s floating enable, border pixel 2",
    }
    if action not in commands:
        return False
    SWAY.command(commands[action] % sel)
    return True


# --------------------------------------------------------------------------
# launcher
# --------------------------------------------------------------------------

MENU_STATE = {"open": False}


def menu_toggle(force=None):
    """Show or hide the launcher.

    The launcher is a layer-shell surface that exists for the whole session and
    hides itself; toggling is a broadcast on the event bus, not a compositor
    operation and certainly not a browser start. That makes it instant and
    identical on sway and Hyprland.
    """
    want = (not MENU_STATE["open"]) if force is None else bool(force)
    MENU_STATE["open"] = want
    EVENTS.publish("menu", {"open": want})
    return want


# --------------------------------------------------------------------------
# system tray (StatusNotifierItem)
# --------------------------------------------------------------------------

TRAY = {"items": {}, "lock": threading.Lock()}


def tray_items():
    with TRAY["lock"]:
        return [dict(v) for v in TRAY["items"].values()]


def tray_run():
    """Be a StatusNotifierWatcher and host, so tray apps have somewhere to go.

    This is how Steam, Discord, Slack, nm-applet and friends put an icon in a
    panel: they register a StatusNotifierItem on the session bus and expect
    something to be watching. Without a watcher they either hide the icon or
    fall back to nothing at all. Runs in its own thread with a GLib main loop,
    which is what dbus-python wants.
    """
    try:
        import dbus
        import dbus.service
        import dbus.mainloop.glib
        from gi.repository import GLib
    except ImportError:
        return                      # tray simply unavailable; panel shows none

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()

    WATCHER_IFACE = "org.kde.StatusNotifierWatcher"
    ITEM_IFACE = "org.kde.StatusNotifierItem"

    def read_item(service, path):
        try:
            obj = bus.get_object(service, path)
            props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
            get = lambda k: props.Get(ITEM_IFACE, k)  # noqa: E731
            entry = {
                "id": "%s%s" % (service, path),
                "service": str(service),
                "path": str(path),
                "title": str(get("Title") or get("Id") or ""),
                "icon_name": str(get("IconName") or ""),
                "status": str(get("Status") or "Active"),
            }
            entry["icon_url"] = ("/api/icon/" + urllib.parse.quote(entry["icon_name"])
                                if entry["icon_name"] else "")
            return entry
        except Exception:
            return None

    def add(service, path="/StatusNotifierItem"):
        entry = read_item(service, path)
        if not entry:
            return
        with TRAY["lock"]:
            TRAY["items"][entry["id"]] = entry
        EVENTS.publish("tray", {})

    class Watcher(dbus.service.Object):
        def __init__(self):
            name = dbus.service.BusName(WATCHER_IFACE, bus,
                                        do_not_queue=True, replace_existing=False)
            super().__init__(bus, "/StatusNotifierWatcher", name)

        @dbus.service.method(WATCHER_IFACE, in_signature="s", sender_keyword="sender")
        def RegisterStatusNotifierItem(self, service, sender=None):
            # Callers pass either a bus name or an object path; the spec allows
            # both and real applications use both.
            if service.startswith("/"):
                add(sender, service)
            else:
                add(service)

        @dbus.service.method(WATCHER_IFACE, in_signature="s")
        def RegisterStatusNotifierHost(self, service):
            pass

        @dbus.service.method("org.freedesktop.DBus.Properties",
                             in_signature="ss", out_signature="v")
        def Get(self, iface, prop):
            if prop == "IsStatusNotifierHostRegistered":
                return dbus.Boolean(True)
            if prop == "RegisteredStatusNotifierItems":
                with TRAY["lock"]:
                    return dbus.Array([i["service"] for i in TRAY["items"].values()],
                                      signature="s")
            if prop == "ProtocolVersion":
                return dbus.Int32(0)
            return dbus.String("")

        @dbus.service.method("org.freedesktop.DBus.Properties",
                             in_signature="s", out_signature="a{sv}")
        def GetAll(self, iface):
            return dbus.Dictionary(
                {"IsStatusNotifierHostRegistered": dbus.Boolean(True),
                 "ProtocolVersion": dbus.Int32(0)}, signature="sv")

        @dbus.service.signal(WATCHER_IFACE, signature="s")
        def StatusNotifierItemRegistered(self, service):
            pass

    def on_name_owner_changed(name, old, new):
        # An application quitting should take its icon with it.
        if not new:
            with TRAY["lock"]:
                gone = [k for k, v in TRAY["items"].items() if v["service"] == str(name)]
                for k in gone:
                    del TRAY["items"][k]
            if gone:
                EVENTS.publish("tray", {})

    try:
        Watcher()
    except Exception:
        # Another tray host owns the name; leave it alone rather than fight.
        return

    bus.add_signal_receiver(on_name_owner_changed,
                            signal_name="NameOwnerChanged",
                            dbus_interface="org.freedesktop.DBus")
    GLib.MainLoop().run()


def tray_activate(item_id, secondary=False):
    try:
        import dbus
    except ImportError:
        return False
    with TRAY["lock"]:
        entry = TRAY["items"].get(item_id)
    if not entry:
        return False
    try:
        obj = dbus.SessionBus().get_object(entry["service"], entry["path"])
        iface = dbus.Interface(obj, "org.kde.StatusNotifierItem")
        if secondary:
            iface.SecondaryActivate(0, 0)
        else:
            iface.Activate(0, 0)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def storage_path(app_id):
    if not SAFE_ID.match(app_id or ""):
        return None
    directory = os.path.join(STATE_DIR, "apps")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, app_id + ".json")


def storage_read(app_id):
    path = storage_path(app_id)
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def storage_write(app_id, data):
    path = storage_path(app_id)
    if not path:
        return False
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
        return True
    except (OSError, TypeError):
        return False


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def read_first(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def status():
    mem_total = mem_avail = 0
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1])
    except OSError:
        pass

    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
    raw = read_first("/proc/uptime")
    uptime = float(raw.split()[0]) if raw else 0.0

    battery = None
    for bat in sorted(glob.glob("/sys/class/power_supply/BAT*")):
        cap = read_first(os.path.join(bat, "capacity"))
        battery = {"percent": int(cap) if cap and cap.isdigit() else None,
                   "state": read_first(os.path.join(bat, "status")) or "Unknown"}
        break

    return {
        "time": time.time(),
        "host": os.uname().nodename,
        "user": os.environ.get("USER", "nethos"),
        "kernel": os.uname().release,
        "nethos": read_first("/etc/nethos-release") or "unknown",
        "uptime": uptime,
        "load": round(load, 2),
        "mem": {"total_kb": mem_total, "avail_kb": mem_avail,
                "used_pct": round(100 * (1 - mem_avail / mem_total)) if mem_total else 0},
        "battery": battery,
        "generation": EVENTS.generation,
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".xpm": "image/x-xpixmap",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
}


STARTED = time.time()
DIAG_PATH = os.path.expanduser("~/.cache/nethos/nethosd.log")
DIAG_LOCK = threading.Lock()
# Last time each surface said it was alive, and what it last complained about.
HEARTBEAT = {}
CLIENT_ERRORS = []


def diag(kind, message):
    """Append one line to the daemon log, capped so it cannot fill the disk."""
    line = "%s %-6s %s" % (time.strftime("%H:%M:%S"), kind, str(message)[:400])
    try:
        with DIAG_LOCK:
            os.makedirs(os.path.dirname(DIAG_PATH), exist_ok=True)
            if os.path.exists(DIAG_PATH) and os.path.getsize(DIAG_PATH) > 2_000_000:
                with open(DIAG_PATH) as fh:
                    tail = fh.readlines()[-2000:]
                with open(DIAG_PATH, "w") as fh:
                    fh.writelines(tail)
            with open(DIAG_PATH, "a") as fh:
                fh.write(line + "\n")
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "nethosd/3.0"
    protocol_version = "HTTP/1.1"

    # Timing starts once the request has been read, not when we begin waiting
    # for one. handle_one_request() opens by blocking on readline() for the
    # request line, and on a keep-alive connection that blocks until the
    # client's next request -- so timing the whole call measured how long the
    # shell stayed idle between heartbeats. With a 1s heartbeat every entry
    # came out at "1.00s", which reads exactly like a daemon that takes a
    # second to answer. It answers in about two milliseconds.
    _started = None

    def parse_request(self):
        ok = BaseHTTPRequestHandler.parse_request(self)
        self._started = time.time()
        return ok

    def handle_one_request(self):
        self._started = None
        BaseHTTPRequestHandler.handle_one_request(self)
        if self._started is None:
            return
        took = time.time() - self._started
        # 250ms is the threshold at which a person notices. Anything over it
        # between a click and the screen is worth a line in the log.
        if took > 0.25:
            diag("slow", "%.2fs  %s" % (took, getattr(self, "path", "?")))

    # Endpoints the shell hits on a timer. Logging them buries every
    # interesting line under heartbeats -- this log reached 1.8MB in minutes,
    # which is its own kind of silence.
    QUIET = ("/api/log", "/api/status", "/api/events")

    def log_message(self, fmt, *args):
        # Was `pass`. Silence is why several evenings went into guessing what
        # the shell was doing: nothing anywhere recorded that a request had
        # been made, succeeded, or stopped arriving.
        if any(q in (getattr(self, "path", "") or "") for q in self.QUIET):
            return
        diag("http", fmt % args)

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            return {}

    def send_path(self, full, cache=False):
        if not os.path.isfile(full):
            return self.send_error(404)
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type",
                         MIME.get(os.path.splitext(full)[1].lower(),
                                  "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # Icons never change under us; everything else must not go stale or
        # hot reload silently stops working.
        self.send_header("Cache-Control",
                         "public, max-age=86400" if cache else "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, base, rel, fallback=""):
        rel = urllib.parse.unquote(rel).lstrip("/") or fallback
        full = os.path.normpath(os.path.join(base, rel))
        if not full.startswith(base):
            return self.send_error(403)
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        self.send_path(full)

    def serve_events(self):
        q = EVENTS.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b": nethos event stream\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=20)
                    self.wfile.write(("data: %s\n\n" % msg).encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            EVENTS.unsubscribe(q)

    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path

        if route == "/api/events":
            return self.serve_events()
        if route == "/api/health":
            now = time.time()
            return self.send_json({
                "uptime": round(now - STARTED, 1),
                "surfaces": {k: round(now - v, 1) for k, v in HEARTBEAT.items()},
                "client_errors": CLIENT_ERRORS[-20:],
                "log": DIAG_PATH,
            })
        if route == "/api/apps":
            return self.send_json({"apps": [
                {k: v for k, v in a.items() if not k.startswith("_")}
                for a in load_apps()
            ]})
        if route == "/api/windows":
            return self.send_json({"windows": list_windows()})
        if route == "/api/status":
            return self.send_json(status())
        if route == "/api/settings":
            # Schema travels with the values so the Settings app renders from
            # what the daemon actually accepts, and cannot drift from it.
            return self.send_json({"settings": read_settings(),
                                   "schema": SETTINGS_SCHEMA})

        if route == "/api/files":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            path = safe_path(qs.get("path", [""])[0])
            if path is None:
                return self.send_json({"error": "outside the allowed roots"}, 403)
            entries, err = list_dir(path)
            if err:
                return self.send_json({"error": err, "path": path}, 404)
            parent = os.path.dirname(path.rstrip("/")) or "/"
            return self.send_json({
                "path": path,
                "parent": parent if safe_path(parent) else "",
                "home": HOME,
                "entries": entries,
                "places": places(),
                "job": dict(EXTRACT_JOB),
            })

        if route == "/api/packages":
            q = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("q", [""])[0]
            return self.send_json({
                "results": npkg_search(q),
                "installed": sorted(npkg_installed()),
                "job": {"active": PKG_JOB["active"],
                        "ok": PKG_JOB["ok"],
                        "log": PKG_JOB["log"][-40:]},
            })

        if route == "/api/diagnostics":
            # What a person would otherwise have to open a terminal and read
            # four files to learn.
            try:
                with open(DIAG_PATH) as fh:
                    tail = fh.readlines()[-40:]
            except OSError:
                tail = []
            surfaces = {}
            now = time.time()
            for name, seen in list(HEARTBEAT.items()):
                surfaces[name] = round(now - seen, 1)
            return self.send_json({
                "backend": backend(),
                "surfaces": surfaces,
                "windows": len(list_windows()),
                "settings_path": SETTINGS_PATH,
                "log": [ln.rstrip() for ln in tail],
            })
        if route == "/api/menu":
            return self.send_json({"open": MENU_STATE["open"]})
        if route == "/api/version":
            return self.send_json({"generation": EVENTS.generation,
                                   "version": read_first("/etc/nethos-release") or "unknown"})
        if route.startswith("/api/icon/"):
            path = resolve_icon(urllib.parse.unquote(route[len("/api/icon/"):]))
            if not path:
                return self.send_error(404)
            return self.send_path(path, cache=True)
        if route == "/api/tray":
            return self.send_json({"items": tray_items()})
        if route.startswith("/api/storage/"):
            return self.send_json({"data": storage_read(route[len("/api/storage/"):])})

        if route.startswith("/lib/"):
            return self.send_file(LIB_DIR, route[len("/lib/"):])

        if route.startswith("/apps/"):
            app_id, _, sub = route[len("/apps/"):].partition("/")
            root = app_root(app_id)
            if not root:
                return self.send_error(404)
            return self.send_file(root, sub, fallback="index.html")

        return self.send_file(SHELL_DIR, route, fallback="panel.html")

    def do_PUT(self):
        route = urllib.parse.urlparse(self.path).path
        if route.startswith("/api/storage/"):
            data = self.read_json()
            ok = storage_write(route[len("/api/storage/"):], data.get("data", {}))
            return self.send_json({"ok": ok}, 200 if ok else 400)
        return self.send_error(404)

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        data = self.read_json()

        if route == "/api/log":
            # The pages have no other way to be heard: their console output goes
            # to the compositor's stdout, which nothing collects on a running
            # system. A heartbeat here is also the only way to tell "the page is
            # idle" from "the page stopped running", which look identical on
            # screen.
            kind = str(data.get("kind", "log"))[:16]
            surface = str(data.get("surface", "?"))[:24]
            if kind == "beat":
                HEARTBEAT[surface] = time.time()
            else:
                entry = "%s [%s] %s" % (time.strftime("%H:%M:%S"), surface,
                                        str(data.get("message", ""))[:300])
                CLIENT_ERRORS.append(entry)
                del CLIENT_ERRORS[:-100]
                diag(kind, "%s: %s" % (surface, data.get("message", "")))
            return self.send_json({"ok": True})

        # Context menus are drawn by the overlay surface on behalf of whichever
        # surface was right-clicked.
        #
        # The panel and the dock cannot draw their own: left-clicks only reach
        # a layer surface inside a reserved exclusive zone, so a menu opening
        # past the end of the panel's 46px zone highlights under the pointer
        # and cannot be chosen. Widening the input region does not help; the
        # surface never sees the button. The overlay is full-screen and
        # already takes clicks -- the launcher works -- so it draws the menu
        # and reports back which item was picked. The callbacks stay in the
        # surface that opened it, keyed by token.
        if route == "/api/contextmenu":
            EVENTS.publish("contextmenu", {
                "token": str(data.get("token", "")),
                "x": int(data.get("x", 0)),
                "y": int(data.get("y", 0)),
                "items": data.get("items") or [],
            })
            return self.send_json({"ok": True})

        if route == "/api/contextmenu/choose":
            EVENTS.publish("contextmenu-choice", {
                "token": str(data.get("token", "")),
                "index": int(data.get("index", -1)),
            })
            return self.send_json({"ok": True})

        # Repairing the interface from inside the interface.
        #
        # Every fault in this desktop so far -- a stopped clock, a dock that
        # ignored clicks, an overlay swallowing the screen -- has been
        # invisible from the desktop and diagnosable only over SSH. These are
        # the three things that actually fixed them, in the order of how much
        # they disturb.
        if route.startswith("/api/files/"):
            what = route[len("/api/files/"):]
            path = safe_path(data.get("path", ""))
            if path is None:
                return self.send_json({"error": "outside the allowed roots"}, 403)

            if what == "open":
                # xdg-open, so the user's own default applies rather than a
                # table of ours that would immediately be wrong.
                spawn(["xdg-open", path])
                return self.send_json({"ok": True})

            if what == "mkdir":
                name = str(data.get("name", "")).strip().strip("/")
                if not name or name in (".", "..") or "/" in name:
                    return self.send_json({"error": "bad name"}, 400)
                try:
                    os.makedirs(os.path.join(path, name))
                except OSError as exc:
                    return self.send_json({"error": str(exc)}, 400)
                return self.send_json({"ok": True})

            if what == "rename":
                name = str(data.get("name", "")).strip().strip("/")
                if not name or name in (".", "..") or "/" in name:
                    return self.send_json({"error": "bad name"}, 400)
                target = os.path.join(os.path.dirname(path), name)
                if os.path.exists(target):
                    return self.send_json({"error": "already exists"}, 409)
                try:
                    os.rename(path, target)
                except OSError as exc:
                    return self.send_json({"error": str(exc)}, 400)
                return self.send_json({"ok": True, "path": target})

            if what == "trash":
                # Moved, not deleted. A file manager that destroys on a
                # mis-click is one people stop trusting, and the recovery for
                # "I meant the other file" should not be a backup.
                trash = os.path.join(HOME, ".local/share/Trash/files")
                try:
                    os.makedirs(trash, exist_ok=True)
                    base = os.path.basename(path)
                    dest = os.path.join(trash, base)
                    n = 2
                    while os.path.exists(dest):
                        dest = os.path.join(trash, "%s.%d" % (base, n))
                        n += 1
                    shutil.move(path, dest)
                except OSError as exc:
                    return self.send_json({"error": str(exc)}, 400)
                return self.send_json({"ok": True})

            if what == "extract":
                if EXTRACT_JOB["active"]:
                    return self.send_json({"error": "busy"}, 409)
                extract_archive(path, os.path.dirname(path))
                return self.send_json({"ok": True})

            return self.send_json({"error": "unknown action"}, 400)

        if route == "/api/packages/install" or route == "/api/packages/remove":
            names = data.get("packages") or ([data["id"]] if data.get("id") else [])
            names = [n for n in names
                     if re.match(r"^[a-z0-9][a-z0-9.+-]*$", str(n))]
            if not names:
                return self.send_json({"error": "no valid package names"}, 400)
            if PKG_JOB["active"]:
                return self.send_json(
                    {"error": "busy", "active": PKG_JOB["active"]}, 409)
            pkg_job("install" if route.endswith("install") else "remove", names)
            return self.send_json({"ok": True, "packages": names})

        if route == "/api/troubleshoot":
            action = data.get("action", "")
            if action == "reload":
                EVENTS.publish("reload", {"reason": "troubleshooter"})
                return self.send_json({"ok": True, "did": "reloaded surfaces"})
            if action == "restart-shell":
                # The surfaces, not the compositor: losing the compositor
                # takes every open application with it.
                spawn(["sh", "-c",
                       "pkill -f 'nethos-view url=' ; sleep 2 ; "
                       "setsid nethos-session >/dev/null 2>&1 &"])
                return self.send_json({"ok": True, "did": "restarting shell"})
            if action == "restart-daemon":
                spawn(["sh", "-c",
                       "sleep 1 ; systemctl --user restart nethosd"])
                return self.send_json({"ok": True, "did": "restarting nethosd"})
            return self.send_json({"error": "unknown action"}, 400)

        if route == "/api/settings":
            if data.get("reset"):
                changed = write_settings(dict(SETTINGS_DEFAULTS))
            else:
                changed = write_settings(data.get("settings") or data)
            return self.send_json({"ok": True, "settings": changed})

        if route == "/api/launch":
            builtin = data.get("builtin", "")
            if builtin:
                if builtin not in BUILTINS:
                    return self.send_json({"error": "unknown builtin"}, 400)
                if builtin == "menu-toggle":
                    return self.send_json({"ok": True, "open": menu_toggle()})
                if builtin in ("logout", "lock"):
                    spawn(session_command(builtin))
                    return self.send_json({"ok": True})
                spawn(BUILTINS[builtin])
                return self.send_json({"ok": True})

            app = find_app(data.get("id", ""))
            if not app:
                return self.send_json({"error": "no such app"}, 404)
            ok = launch_web_app(app) if app["source"] == "nethos" \
                else launch_desktop_app(app)
            menu_toggle(force=False)
            return self.send_json({"ok": ok})

        if route == "/api/window":
            action, wid = data.get("action"), str(data.get("id", ""))
            if not wid:
                return self.send_json({"error": "bad id"}, 400)
            if not window_action(action, wid):
                return self.send_json({"error": "bad action"}, 400)
            return self.send_json({"ok": True})

        if route == "/api/menu":
            return self.send_json({"ok": True, "open": menu_toggle(data.get("open"))})

        if route == "/api/tray/activate":
            ok = tray_activate(str(data.get("id", "")), bool(data.get("secondary")))
            return self.send_json({"ok": ok})

        if route == "/api/reload":
            return self.send_json({"ok": True,
                                   "generation": EVENTS.bump(data.get("reason", "manual"))})

        if route == "/api/notify":
            EVENTS.publish("notify", {"text": str(data.get("text", ""))[:300],
                                      "level": data.get("level", "info")})
            return self.send_json({"ok": True})

        return self.send_error(404)


def main():
    os.chdir("/")
    os.makedirs(STATE_DIR, exist_ok=True)

    threading.Thread(target=build_icon_index, daemon=True).start()
    threading.Thread(target=compositor_event_loop, daemon=True).start()
    threading.Thread(target=tray_run, daemon=True).start()

    watched = [d for d in [SHELL_DIR, LIB_DIR] + APP_DIRS_WEB if os.path.isdir(d)]
    if watched:
        threading.Thread(target=watch_files, args=(watched,), daemon=True).start()

    # A clock the surfaces can trust.
    #
    # WebKit throttles timers in pages it considers hidden, and layer-shell
    # surfaces never take focus, so setInterval stops dead a moment after load:
    # measured on real hardware as four surfaces that report in once and then
    # never again, at 0.2% CPU with no errors. Event-driven code keeps running
    # -- Super+D still opened the launcher -- so the periodic work moves onto
    # the event stream, which is pushed from here and cannot be throttled away.
    def ticker():
        while True:
            time.sleep(5)
            EVENTS.publish("tick", {"t": time.time()})

    threading.Thread(target=ticker, daemon=True).start()

    # Snap-on-drag.
    #
    # sway has no such feature -- mod+arrow snapping is a keybinding, and
    # dragging a window into a corner does nothing. There is also no event for
    # "the user finished dragging", so this watches the focused floating
    # window's geometry and acts when it stops changing near an edge. Polling
    # is inelegant; it is also the only thing the compositor makes possible.
    #
    # Deliberately conservative: it only ever touches a *floating* window that
    # the user has just moved to an edge themselves. A snap that fires when you
    # did not ask for it is far more annoying than one that occasionally does
    # not fire.
    def snapper():
        EDGE = 24            # how close to an edge counts as intent
        last = {}
        while True:
            time.sleep(0.2)
            try:
                # Only sway needs this. Wayfire snaps on drag itself, and doing
                # it twice would fight the compositor.
                if backend() != "sway":
                    time.sleep(2)
                    continue
                tree = SWAY.request(SWAY.GET_TREE) or {}
                out = (SWAY.request(SWAY.GET_OUTPUTS) or [{}])[0]
                ow = out.get("rect", {}).get("width", 0)
                oh = out.get("rect", {}).get("height", 0)
                if not ow or not oh:
                    continue
                win = _focused_floating(tree)
                if not win:
                    last.clear()
                    continue
                wid, r = win["id"], win["rect"]
                key = (r["x"], r["y"], r["width"], r["height"])
                # Still moving: remember and wait.
                if last.get(wid) != key:
                    last[wid] = key
                    last["settled"] = 0
                    continue
                # Unchanged for two ticks -- the drag has ended.
                last["settled"] = last.get("settled", 0) + 1
                if last["settled"] != 2:
                    continue

                left, top = r["x"] <= EDGE, r["y"] <= EDGE
                right = r["x"] + r["width"] >= ow - EDGE
                bottom = r["y"] + r["height"] >= oh - EDGE
                cmd = None
                if top and not (left or right):
                    cmd = "resize set 100ppt 100ppt, move position 0 0"
                elif left and top:
                    cmd = "resize set 50ppt 50ppt, move position 0 0"
                elif right and top:
                    cmd = "resize set 50ppt 50ppt, move position 50ppt 0"
                elif left and bottom:
                    cmd = "resize set 50ppt 50ppt, move position 0 50ppt"
                elif right and bottom:
                    cmd = "resize set 50ppt 50ppt, move position 50ppt 50ppt"
                elif left:
                    cmd = "resize set 50ppt 100ppt, move position 0 0"
                elif right:
                    cmd = "resize set 50ppt 100ppt, move position 50ppt 0"
                if cmd:
                    SWAY.command("[con_id=%s] %s" % (wid, cmd))
                    diag("snap", "con_id=%s %s" % (wid, cmd))
                    last[wid] = None
            except Exception as exc:             # noqa: BLE001
                diag("snap", "error: %s" % exc)
                time.sleep(2)

    threading.Thread(target=snapper, daemon=True).start()

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()
