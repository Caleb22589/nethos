#!/usr/bin/env python3
"""
Host-side NETHOS dev server.

Runs the real shell, the real SDK and your real apps on your development
machine, with the system-specific parts faked. Iterating here is far faster
than doing it inside an emulated VM, and the code you are editing is exactly
what ships.

    python3 tools/mock_nethosd.py            # serves ./payload
    python3 tools/mock_nethosd.py path/to/payload

Then open:
    http://127.0.0.1:7777/panel.html
    http://127.0.0.1:7777/menu.html
    http://127.0.0.1:7777/apps/system/index.html

Live reload works here exactly as it does on the real system: edit a file and
every open tab reloads itself.
"""

import json
import os
import queue
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAYLOAD = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "payload")
SHELL_DIR = os.path.join(PAYLOAD, "shell")
LIB_DIR = os.path.join(PAYLOAD, "lib")
APPS_DIR = os.path.join(PAYLOAD, "apps")
STATE_DIR = os.path.join(PAYLOAD, ".devstate")

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".json": "application/json",
        ".svg": "image/svg+xml", ".png": "image/png"}

WINDOWS = [
    {"id": 12, "title": "Chromium — Arch Linux", "app_id": "chromium",
     "focused": True, "workspace": "1", "floating": False},
    {"id": 13, "title": "~/projects — foot", "app_id": "foot",
     "focused": False, "workspace": "1", "floating": False},
    {"id": 14, "title": "Downloads — Thunar", "app_id": "thunar",
     "focused": False, "workspace": "2", "floating": True},
]

DESKTOP_APPS = [
    {"id": "chromium.desktop", "name": "Chromium", "comment": "Web browser",
     "icon": "chromium", "categories": ["Network"], "terminal": False, "source": "desktop"},
    {"id": "foot.desktop", "name": "Foot", "comment": "Wayland terminal emulator",
     "icon": "foot", "categories": ["System"], "terminal": False, "source": "desktop"},
    {"id": "thunar.desktop", "name": "Thunar File Manager", "comment": "Browse the filesystem",
     "icon": "thunar", "categories": ["System"], "terminal": False, "source": "desktop"},
    {"id": "mousepad.desktop", "name": "Mousepad", "comment": "Simple text editor",
     "icon": "mousepad", "categories": ["Utility"], "terminal": False, "source": "desktop"},
    {"id": "htop.desktop", "name": "htop", "comment": "Process viewer",
     "icon": "htop", "categories": ["System"], "terminal": True, "source": "desktop"},
]

STATE = {"menu": False, "generation": 0}
SUBS = set()
LOCK = threading.Lock()
START = time.time()


def publish(kind, data=None):
    msg = json.dumps({"type": kind, "data": data or {}, "generation": STATE["generation"]})
    with LOCK:
        for q in list(SUBS):
            try:
                q.put_nowait(msg)
            except queue.Full:
                SUBS.discard(q)


def watcher():
    def snap():
        out = {}
        for root in (SHELL_DIR, LIB_DIR, APPS_DIR):
            for dirpath, _d, files in os.walk(root):
                if ".devstate" in dirpath:
                    continue
                for name in files:
                    full = os.path.join(dirpath, name)
                    try:
                        out[full] = os.stat(full).st_mtime_ns
                    except OSError:
                        pass
        return out

    prev = snap()
    while True:
        time.sleep(1)
        cur = snap()
        if cur != prev:
            prev = cur
            STATE["generation"] += 1
            publish("reload", {"reason": "files-changed"})
            print("[dev] change detected -> reload (gen %d)" % STATE["generation"])


def web_apps():
    apps = []
    if not os.path.isdir(APPS_DIR):
        return apps
    for name in sorted(os.listdir(APPS_DIR)):
        manifest_path = os.path.join(APPS_DIR, name, "app.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path) as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            continue
        window = m.get("window") or {}
        icon = m.get("icon", "")
        icon_url = ""
        if icon and os.path.isfile(os.path.join(APPS_DIR, name, icon)):
            icon_url = "/apps/%s/%s" % (m["id"], icon)
        apps.append({
            "id": m["id"], "name": m.get("name", m["id"]),
            "comment": m.get("description", ""), "icon": icon,
            "icon_url": icon_url,
            "version": m.get("version", "0.0.0"),
            "categories": m.get("categories") or ["NETHOS"],
            "entry": m.get("entry", "index.html"),
            "permissions": m.get("permissions") or [],
            "mode": m.get("mode", "window"),
            "position": m.get("position", "top-right"),
            "floating": bool(window.get("floating", False)),
            "width": int(window.get("width", 960)),
            "height": int(window.get("height", 640)),
            "source": "nethos", "terminal": False,
        })
    return apps


def storage_file(app_id):
    os.makedirs(STATE_DIR, exist_ok=True)
    safe = "".join(c for c in app_id if c.isalnum() or c in "._-")
    return os.path.join(STATE_DIR, safe + ".json")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def j(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def serve(self, base, rel, fallback=""):
        rel = urllib.parse.unquote(rel).lstrip("/") or fallback
        full = os.path.normpath(os.path.join(base, rel))
        if not full.startswith(base) or not os.path.isfile(full):
            return self.send_error(404)
        data = open(full, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type",
                         MIME.get(os.path.splitext(full)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def events(self):
        q = queue.Queue(maxsize=64)
        with LOCK:
            SUBS.add(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b": dev stream\n\n")
            self.wfile.flush()
            while True:
                try:
                    self.wfile.write(("data: %s\n\n" % q.get(timeout=20)).encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with LOCK:
                SUBS.discard(q)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/api/events":
            return self.events()
        if p == "/api/apps":
            return self.j({"apps": web_apps() + DESKTOP_APPS})
        if p == "/api/windows":
            return self.j({"windows": WINDOWS})
        if p == "/api/menu":
            return self.j({"open": STATE["menu"]})
        if p == "/api/version":
            return self.j({"generation": STATE["generation"], "version": "dev"})
        if p == "/api/status":
            return self.j({
                "time": time.time(), "host": "nethos-dev", "user": "dev",
                "kernel": "dev", "nethos": "dev-server",
                "uptime": time.time() - START, "load": 0.42,
                "mem": {"total_kb": 6291456, "avail_kb": 4194304, "used_pct": 33},
                "battery": None, "generation": STATE["generation"],
            })
        if p.startswith("/api/storage/"):
            path = storage_file(p[len("/api/storage/"):])
            if os.path.isfile(path):
                return self.j({"data": json.load(open(path))})
            return self.j({"data": {}})
        if p.startswith("/lib/"):
            return self.serve(LIB_DIR, p[len("/lib/"):])
        if p.startswith("/apps/"):
            app_id, _, sub = p[len("/apps/"):].partition("/")
            return self.serve(os.path.join(APPS_DIR, app_id), sub, "index.html")
        return self.serve(SHELL_DIR, p, "panel.html")

    def do_PUT(self):
        p = urllib.parse.urlparse(self.path).path
        if p.startswith("/api/storage/"):
            with open(storage_file(p[len("/api/storage/"):]), "w") as fh:
                json.dump(self.body().get("data", {}), fh, indent=2)
            return self.j({"ok": True})
        return self.send_error(404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        data = self.body()
        if p == "/api/reload":
            STATE["generation"] += 1
            publish("reload", {"reason": "manual"})
            return self.j({"ok": True, "generation": STATE["generation"]})
        if p == "/api/notify":
            publish("notify", {"text": data.get("text", ""),
                               "level": data.get("level", "info")})
            return self.j({"ok": True})
        if p == "/api/launch" and data.get("builtin") == "menu-toggle":
            STATE["menu"] = not STATE["menu"]
            return self.j({"ok": True, "open": STATE["menu"]})
        if p == "/api/menu":
            STATE["menu"] = data.get("open", not STATE["menu"])
            return self.j({"ok": True, "open": STATE["menu"]})
        if p == "/api/launch":
            print("[dev] launch requested:", data.get("id"))
            return self.j({"ok": True})
        return self.j({"ok": True})


if __name__ == "__main__":
    if not os.path.isdir(SHELL_DIR):
        sys.exit("no shell/ under %s — pass the payload directory" % PAYLOAD)
    threading.Thread(target=watcher, daemon=True).start()
    ThreadingHTTPServer.allow_reuse_address = True
    print("NETHOS dev server on http://127.0.0.1:7777  (payload: %s)" % PAYLOAD)
    print("  /panel.html   /menu.html   /apps/<id>/index.html")
    ThreadingHTTPServer(("127.0.0.1", 7777), H).serve_forever()
