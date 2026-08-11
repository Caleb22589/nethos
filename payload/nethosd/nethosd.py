#!/usr/bin/env python3
"""
nethosd - the NETHOS shell bridge and app host.

The NETHOS desktop is written in HTML/CSS/JS and runs inside Chromium. This
daemon is the only thing standing between that web shell and the real system:
it serves the shell, the app SDK and every installed NETHOS app, and exposes a
small JSON API over loopback for the things a web page cannot do on its own --
enumerate installed applications, launch them, drive the sway compositor's real
windows, and persist per-app state.

Deliberate constraints:
  * stdlib only. No pip, no venv, nothing to break on a pacman -Syu.
  * binds 127.0.0.1 only.
  * never executes an arbitrary command string from a page. Launch requests
    name a .desktop id or a NETHOS app id that must already exist on disk, or a
    builtin action from a fixed table.

NOT a security boundary: every local app is served from the same origin and
talks to the same API, so app "permissions" below are scoping and hygiene, not
a sandbox. Anything you install can reach anything this daemon exposes.
"""

import json
import os
import queue
import re
import shlex
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

# Fixed table of privileged actions a page may invoke by name.
BUILTINS = {
    "poweroff": ["systemctl", "poweroff"],
    "reboot": ["systemctl", "reboot"],
    "logout": ["swaymsg", "exit"],
    "lock": ["swaylock", "-f", "-c", "0b0e14"],
    "terminal": ["foot"],
    "menu-toggle": None,   # handled internally
}

# Chromium ignores --class in --app mode and builds its own app_id out of the
# URL ("chrome-127.0.0.1__panel.html-Default"), so the shell's own surfaces are
# identified by the page they show rather than by a class we chose.
PANEL_MARK = "panel.html"
MENU_MARK = "menu.html"
MENU_SELECTOR = r'[app_id="^chrome-.*menu\.html.*$"]'

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
]

_app_cache = {"at": 0.0, "apps": []}


def is_shell_surface(app_id):
    return bool(app_id) and (PANEL_MARK in app_id or MENU_MARK in app_id)


# --------------------------------------------------------------------------
# live reload: generation counter + event fan-out
# --------------------------------------------------------------------------

class Events:
    """Broadcast bus for the shell and every open app window.

    This is what makes edits appear without a reboot: bump the generation and
    every subscribed page reloads itself within milliseconds.
    """

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


def watch_files(paths, interval=1.0):
    """Poll for changes under the served directories and trigger a reload.

    Polling rather than inotify keeps this stdlib-only. The tree is small
    (shell + lib + apps), so a 1s stat sweep is cheap even on an emulated CPU.
    """
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
            _app_cache["at"] = 0.0          # manifests may have changed
            EVENTS.bump("files-changed")


# --------------------------------------------------------------------------
# sway plumbing
# --------------------------------------------------------------------------

def sway(*args, capture=True):
    """Run swaymsg. Returns parsed JSON for -t queries, else raw text."""
    cmd = ["swaymsg"] + list(args)
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not capture:
        return None
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return out


def spawn(argv):
    """Start a process detached from nethosd.

    Deliberately not `swaymsg exec` -- routing an argv through sway's IPC means
    flattening it to a string and having sh re-split it, which silently dropped
    every argument after the binary name. nethosd is started inside the sway
    session, so it already carries WAYLAND_DISPLAY/SWAYSOCK.
    """
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
            })
    for kid in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
        walk_tree(kid, out, workspace)


def list_windows():
    tree = sway("-t", "get_tree")
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
    outputs = sway("-t", "get_outputs")
    if isinstance(outputs, list) and outputs:
        rect = outputs[0].get("rect") or {}
        if rect.get("width") and rect.get("height"):
            return rect["width"], rect["height"]
    return default


# --------------------------------------------------------------------------
# NETHOS web apps
# --------------------------------------------------------------------------

SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def app_root(app_id):
    """Resolve an app id to its directory, user apps taking precedence."""
    if not SAFE_ID.match(app_id or ""):
        return None
    for base in APP_DIRS_WEB:
        candidate = os.path.join(base, app_id)
        if os.path.isdir(candidate):
            return candidate
    return None


def read_manifest(directory):
    path = os.path.join(directory, "app.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or not manifest.get("id"):
        return None
    if not SAFE_ID.match(manifest["id"]):
        return None

    window = manifest.get("window") or {}
    return {
        "id": manifest["id"],
        "name": manifest.get("name") or manifest["id"],
        "comment": manifest.get("description", ""),
        "icon": manifest.get("icon", ""),
        "version": manifest.get("version", "0.0.0"),
        "categories": manifest.get("categories") or ["NETHOS"],
        "entry": manifest.get("entry", "index.html"),
        "permissions": manifest.get("permissions") or [],
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
    width, height = app["width"], app["height"]
    url = "http://%s:%d/apps/%s/%s" % (HOST, PORT, app["id"], app["entry"])
    return spawn(CHROME_BASE + [
        "--app=" + url,
        "--window-size=%d,%d" % (width, height),
        # One shared profile for every app window: separate profiles would mean
        # a separate Chromium instance per app, which this VM cannot afford.
        "--user-data-dir=" + os.path.expanduser("~/.config/nethos-chromium-apps"),
    ])


# --------------------------------------------------------------------------
# .desktop parsing
# --------------------------------------------------------------------------

EXEC_FIELD_CODES = re.compile(r"%[fFuUdDnNickvm]")


def parse_desktop(path):
    """Minimal .desktop reader: only the [Desktop Entry] group, no locale merging."""
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

    return {
        "id": os.path.basename(path),
        "name": entry["Name"],
        "comment": entry.get("Comment", ""),
        "icon": entry.get("Icon", ""),
        "categories": [c for c in entry.get("Categories", "").split(";") if c],
        "terminal": entry.get("Terminal", "").lower() == "true",
        "source": "desktop",
        "_exec": entry["Exec"],
    }


def load_apps(force=False):
    """Every launchable thing: NETHOS web apps first, then .desktop entries."""
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
# menu window
# --------------------------------------------------------------------------

def menu_is_open():
    tree = sway("-t", "get_tree")
    if not isinstance(tree, dict):
        return False
    found = []
    walk_all_app_ids(tree, found)
    return any(MENU_MARK in a for a in found)


def menu_toggle(force=None):
    open_now = menu_is_open()
    want = (not open_now) if force is None else force
    if want and not open_now:
        ow, oh = output_size()
        spawn(CHROME_BASE + [
            "--app=http://%s:%d/menu.html" % (HOST, PORT),
            "--window-size=%d,%d" % (int(ow * 0.78), int(oh * 0.74)),
            "--user-data-dir=" + os.path.expanduser("~/.config/nethos-chromium-menu"),
        ])
    elif not want and open_now:
        sway(MENU_SELECTOR, "kill")
    return want


# --------------------------------------------------------------------------
# per-app storage
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
        os.replace(tmp, path)     # atomic: never leave a half-written file
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
    bat_dir = "/sys/class/power_supply/BAT0"
    if os.path.isdir(bat_dir):
        cap = read_first(os.path.join(bat_dir, "capacity"))
        battery = {
            "percent": int(cap) if cap and cap.isdigit() else None,
            "state": read_first(os.path.join(bat_dir, "status")) or "Unknown",
        }

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
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "nethosd/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- helpers ---------------------------------------------------------
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

    def send_file(self, base, rel, fallback=None):
        rel = urllib.parse.unquote(rel).lstrip("/") or (fallback or "")
        full = os.path.normpath(os.path.join(base, rel))
        if not full.startswith(os.path.realpath(base)) and not full.startswith(base):
            return self.send_error(403)
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            return self.send_error(404)
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type",
                         MIME.get(os.path.splitext(full)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # Never cache: hot reload is worthless if the browser serves stale files.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    # -- server-sent events ----------------------------------------------
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
                    self.wfile.write(b": keepalive\n\n")   # keep proxies/clients alive
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            EVENTS.unsubscribe(q)

    # -- routes ----------------------------------------------------------
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
            return self.send_json({"open": menu_is_open()})
        if route == "/api/version":
            return self.send_json({"generation": EVENTS.generation,
                                   "version": read_first("/etc/nethos-release") or "unknown"})
        if route.startswith("/api/storage/"):
            return self.send_json({"data": storage_read(route[len("/api/storage/"):])})

        if route.startswith("/lib/"):
            return self.send_file(LIB_DIR, route[len("/lib/"):])

        if route.startswith("/apps/"):
            rest = route[len("/apps/"):]
            app_id, _, sub = rest.partition("/")
            root = app_root(app_id)
            if not root:
                return self.send_error(404)
            return self.send_file(root, sub, fallback="index.html")

        return self.send_file(SHELL_DIR, route, fallback="panel.html")

    def do_PUT(self):
        route = urllib.parse.urlparse(self.path).path
        if route.startswith("/api/storage/"):
            app_id = route[len("/api/storage/"):]
            data = self.read_json()
            ok = storage_write(app_id, data.get("data", {}))
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
                sway(sel, "focus")
            elif action == "close":
                sway(sel, "kill")
            elif action == "fullscreen":
                sway(sel, "fullscreen", "toggle")
            else:
                return self.send_json({"error": "bad action"}, 400)
            return self.send_json({"ok": True})

        if route == "/api/menu":
            return self.send_json({"ok": True, "open": menu_toggle(data.get("open"))})

        if route == "/api/reload":
            return self.send_json({"ok": True,
                                   "generation": EVENTS.bump(data.get("reason", "manual"))})

        if route == "/api/notify":
            EVENTS.publish("notify", {
                "text": str(data.get("text", ""))[:300],
                "level": data.get("level", "info"),
            })
            return self.send_json({"ok": True})

        return self.send_error(404)


def main():
    os.chdir("/")
    os.makedirs(STATE_DIR, exist_ok=True)

    watched = [d for d in [SHELL_DIR, LIB_DIR] + APP_DIRS_WEB if os.path.isdir(d)]
    if watched:
        threading.Thread(target=watch_files, args=(watched,), daemon=True).start()

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()
