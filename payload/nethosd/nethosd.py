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

import glob
import json
import os
import queue
import re
import shlex
import socket
import struct
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
    "logout": ["swaymsg", "exit"],
    "lock": ["swaylock", "-f", "-c", "0b0e14"],
    "terminal": ["foot"],
    "menu-toggle": None,
}

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
    "--disable-features=TranslateUI,MediaRouter",
    "--disable-background-networking",
    "--disable-sync",
    "--user-data-dir=" + CHROME_PROFILE,
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


SWAY = SwayIPC()


def spawn(argv):
    """Start a detached process. nethosd runs inside the session, so the child
    inherits WAYLAND_DISPLAY and friends."""
    try:
        subprocess.Popen(
            argv, start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


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


def sway_event_loop():
    """Subscribe to sway and push window changes to the shell.

    This is what removes the taskbar's polling: the panel no longer asks "what
    windows exist" every second and a half, it is told when that changes.
    """
    while True:
        try:
            sock = SwayIPC.connect()
            sock.settimeout(None)
            SwayIPC.send(sock, SwayIPC.SUBSCRIBE, b'["window","workspace"]')
            SwayIPC.recv(sock)                      # subscription ack
            while True:
                mtype, data = SwayIPC.recv(sock)
                if not (mtype & 0x80000000):
                    continue
                if isinstance(data, dict) and data.get("change") == "new":
                    apply_window_rules(data.get("container") or {})
                EVENTS.publish("windows", {})
        except (OSError, struct.error):
            EVENTS.publish("disconnected", {})
            time.sleep(2)


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
    tree = SWAY.get_tree()
    if not isinstance(tree, dict):
        return []
    out = []
    walk_tree(tree, out)
    return out


def walk_all_app_ids(node, out):
    if not isinstance(node, dict):
        return
    if node.get("app_id"):
        out.append(node["app_id"])
    for kid in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
        walk_all_app_ids(kid, out)


def output_size(default=(1440, 900)):
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


def apply_window_rules(container):
    """Place a newly mapped NETHOS app window according to its manifest.

    sway's own for_window rules cannot read our manifests, so window vs widget
    placement is decided here, on the sway event stream.
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
        ow, oh = output_size()
        w, h = app["width"], app["height"]
        margin = 12
        panel_h = 44
        positions = {
            "top-right":     (ow - w - margin, panel_h + margin),
            "top-left":      (margin, panel_h + margin),
            "bottom-right":  (ow - w - margin, oh - h - margin),
            "bottom-left":   (margin, oh - h - margin),
            "center":        ((ow - w) // 2, (oh - h) // 2),
        }
        x, y = positions.get(app.get("position", "top-right"), positions["top-right"])
        SWAY.command(
            "%s floating enable, border none, sticky enable, "
            "resize set width %d px height %d px, move absolute position %d %d"
            % (sel, w, h, x, y)
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
    url = "http://%s:%d/apps/%s/%s" % (HOST, PORT, app["id"], app["entry"])
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

MENU_STATE = {"open": False}


def menu_window_exists():
    tree = SWAY.get_tree()
    if not isinstance(tree, dict):
        return False
    found = []
    walk_all_app_ids(tree, found)
    return any(MENU_MARK in a for a in found)


def menu_prewarm():
    """Open the launcher once and park it in the scratchpad.

    Opening it later is then a compositor operation instead of a Chromium
    start, which is the difference between instant and several seconds.
    """
    if menu_window_exists():
        return
    ow, oh = output_size()
    spawn(CHROME_BASE + [
        "--app=http://%s:%d/menu.html" % (HOST, PORT),
        "--window-size=%d,%d" % (int(ow * 0.78), int(oh * 0.74)),
    ])


def menu_toggle(force=None):
    want = (not MENU_STATE["open"]) if force is None else bool(force)

    if not menu_window_exists():
        # Lost it (crash, or the session restarted): bring one back.
        menu_prewarm()
        MENU_STATE["open"] = True
        EVENTS.publish("menu", {"open": True})
        return True

    if want:
        SWAY.command("%s scratchpad show, move position center" % MENU_CRITERIA)
    else:
        SWAY.command("%s move scratchpad" % MENU_CRITERIA)

    MENU_STATE["open"] = want
    EVENTS.publish("menu", {"open": want})
    return want


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


class Handler(BaseHTTPRequestHandler):
    server_version = "nethosd/3.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

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
        if route == "/api/apps":
            return self.send_json({"apps": [
                {k: v for k, v in a.items() if not k.startswith("_")}
                for a in load_apps()
            ]})
        if route == "/api/windows":
            return self.send_json({"windows": list_windows()})
        if route == "/api/status":
            return self.send_json(status())
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

        if route == "/api/launch":
            builtin = data.get("builtin", "")
            if builtin:
                if builtin not in BUILTINS:
                    return self.send_json({"error": "unknown builtin"}, 400)
                if builtin == "menu-toggle":
                    return self.send_json({"ok": True, "open": menu_toggle()})
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
            action, wid = data.get("action"), data.get("id")
            if not isinstance(wid, int):
                return self.send_json({"error": "bad id"}, 400)
            sel = "[con_id=%d]" % wid
            if action == "focus":
                SWAY.command("%s focus" % sel)
            elif action == "close":
                SWAY.command("%s kill" % sel)
            elif action == "fullscreen":
                SWAY.command("%s fullscreen toggle" % sel)
            elif action == "popout":
                # Turn a widget into an ordinary managed window.
                SWAY.command("%s floating disable, sticky disable, border pixel 2, focus" % sel)
            elif action == "float":
                SWAY.command("%s floating enable, border pixel 2" % sel)
            else:
                return self.send_json({"error": "bad action"}, 400)
            return self.send_json({"ok": True})

        if route == "/api/menu":
            return self.send_json({"ok": True, "open": menu_toggle(data.get("open"))})

        if route == "/api/reload":
            return self.send_json({"ok": True,
                                   "generation": EVENTS.bump(data.get("reason", "manual"))})

        if route == "/api/notify":
            EVENTS.publish("notify", {"text": str(data.get("text", ""))[:300],
                                      "level": data.get("level", "info")})
            return self.send_json({"ok": True})

        return self.send_error(404)


def prewarm_when_ready(timeout=90):
    """Park a launcher window in the scratchpad once the panel is up.

    Waiting for the panel matters: Chromium's first window owns the shared
    profile, and we want that to be the panel rather than a hidden launcher.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        tree = SWAY.get_tree()
        if isinstance(tree, dict):
            found = []
            walk_all_app_ids(tree, found)
            if any(PANEL_MARK in a for a in found):
                time.sleep(2)
                menu_prewarm()
                return
        time.sleep(1)


def main():
    os.chdir("/")
    os.makedirs(STATE_DIR, exist_ok=True)

    threading.Thread(target=build_icon_index, daemon=True).start()
    threading.Thread(target=sway_event_loop, daemon=True).start()
    threading.Thread(target=prewarm_when_ready, daemon=True).start()

    watched = [d for d in [SHELL_DIR, LIB_DIR] + APP_DIRS_WEB if os.path.isdir(d)]
    if watched:
        threading.Thread(target=watch_files, args=(watched,), daemon=True).start()

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()
