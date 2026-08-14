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


def backend():
    """Which compositor are we driving? Decided per call so a session that
    restarts under the other one keeps working without restarting nethosd."""
    return "hypr" if HyprIPC.available() else "sway"


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
            if backend() == "hypr":
                hypr_event_loop()
            else:
                sway_event_loop()
        except (OSError, struct.error):
            pass
        EVENTS.publish("disconnected", {})
        time.sleep(2)


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
    """Act on a window. Ids are opaque strings: a sway con_id or a Hyprland
    address, so the shell never has to know which compositor is running."""
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

    def log_message(self, fmt, *args):
        # Was `pass`. Silence is why several evenings went into guessing what
        # the shell was doing: nothing anywhere recorded that a request had
        # been made, succeeded, or stopped arriving.
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

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()
