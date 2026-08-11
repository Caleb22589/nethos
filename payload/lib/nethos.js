/*!
 * nethos.js — the NETHOS app SDK.
 *
 * Every NETHOS app is a web page. This library is the wrapper around nethosd
 * that turns "a web page" into "a program running on the operating system":
 * it can enumerate and launch apps, drive real windows, read system state,
 * persist its own data, and hot-reload itself when you edit its source.
 *
 * Usage inside an app:
 *
 *     <link rel="stylesheet" href="/lib/nethos.css">
 *     <script src="/lib/nethos.js"></script>
 *     <script>
 *       const os = await nethos.ready();
 *       document.body.textContent = (await os.system.status()).host;
 *     </script>
 *
 * Everything returns a promise. Nothing here needs a build step, a bundler or
 * a package manager — it is one plain script served by the OS itself.
 *
 * Note on trust: all apps share one origin and one API. This SDK is a
 * convenience layer, not a sandbox. Do not install an app you would not run
 * as yourself in a terminal.
 */
(function (global) {
  "use strict";

  const API = "http://127.0.0.1:7777";

  /* ---------------------------------------------------------------- http */

  async function request(method, path, body) {
    const opts = { method, cache: "no-store" };
    if (body !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(API + path, opts);
    if (!res.ok) {
      throw new NethosError(method + " " + path + " failed", res.status);
    }
    return res.status === 204 ? null : res.json();
  }

  class NethosError extends Error {
    constructor(message, status) {
      super(message);
      this.name = "NethosError";
      this.status = status;
    }
  }

  const get = (p) => request("GET", p);
  const post = (p, b) => request("POST", p, b || {});
  const put = (p, b) => request("PUT", p, b || {});

  /* ------------------------------------------------------------ identity */

  // An app's id is the directory it is served from: /apps/<id>/index.html.
  // Deriving it from the URL means an app never has to configure itself.
  function detectAppId() {
    const m = global.location.pathname.match(/^\/apps\/([^/]+)\//);
    if (m) return m[1];
    if (global.location.pathname.endsWith("panel.html")) return "shell.panel";
    if (global.location.pathname.endsWith("menu.html")) return "shell.menu";
    return "unknown";
  }

  const APP_ID = detectAppId();

  /* -------------------------------------------------------------- events */

  const listeners = new Map();
  let stream = null;
  let currentGeneration = null;

  function on(type, handler) {
    if (!listeners.has(type)) listeners.set(type, new Set());
    listeners.get(type).add(handler);
    connect();
    return () => listeners.get(type).delete(handler);
  }

  function emit(type, payload) {
    (listeners.get(type) || []).forEach((fn) => {
      try { fn(payload); } catch (err) { console.error("[nethos]", err); }
    });
  }

  function connect() {
    if (stream || typeof EventSource === "undefined") return;
    stream = new EventSource(API + "/api/events");
    stream.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }

      if (msg.type === "reload") {
        // Any change to the shell, the SDK or an app bumps the generation.
        // Apps that opt out of autoReload still get the event and can decide.
        if (currentGeneration !== null && msg.generation !== currentGeneration
            && config.autoReload) {
          global.location.reload();
          return;
        }
        currentGeneration = msg.generation;
      }
      emit(msg.type, msg.data);
      emit("*", msg);
    };
    // EventSource reconnects on its own; a dropped stream means nethosd is
    // restarting, which is exactly when we want to come back and re-sync.
    stream.onerror = () => { emit("disconnected", {}); };
  }

  const config = { autoReload: true };

  /* ------------------------------------------------------------ the API */

  const nethos = {
    version: "2.0.0",
    appId: APP_ID,
    Error: NethosError,

    /** Resolve once the daemon is reachable. Returns the SDK itself. */
    async ready() {
      const v = await get("/api/version");
      currentGeneration = v.generation;
      connect();
      return nethos;
    },

    /** Opt out of automatic reloading (e.g. an app with unsaved state). */
    autoReload(enabled) { config.autoReload = !!enabled; return nethos; },

    /** Subscribe to "reload" | "notify" | "disconnected" | "*". */
    on,

    system: {
      status: () => get("/api/status"),
      version: () => get("/api/version"),
      /** Ask every NETHOS surface to reload itself right now. */
      reload: (reason) => post("/api/reload", { reason: reason || "app" }),
      notify: (text, level) => post("/api/notify", { text, level: level || "info" }),
      poweroff: () => post("/api/launch", { builtin: "poweroff" }),
      reboot: () => post("/api/launch", { builtin: "reboot" }),
      logout: () => post("/api/launch", { builtin: "logout" }),
      lock: () => post("/api/launch", { builtin: "lock" }),
      terminal: () => post("/api/launch", { builtin: "terminal" }),
    },

    apps: {
      /** Every launchable thing: NETHOS apps and .desktop entries. */
      async list(filter) {
        const { apps } = await get("/api/apps");
        if (!filter) return apps;
        if (filter.source) return apps.filter((a) => a.source === filter.source);
        return apps;
      },
      /** Just the NETHOS web apps. */
      async listNethos() { return nethos.apps.list({ source: "nethos" }); },
      launch: (id) => post("/api/launch", { id }),
    },

    windows: {
      list: () => get("/api/windows").then((r) => r.windows),
      focus: (id) => post("/api/window", { action: "focus", id }),
      close: (id) => post("/api/window", { action: "close", id }),
      fullscreen: (id) => post("/api/window", { action: "fullscreen", id }),
      /** Make a window float above the layout. */
      float: (id) => post("/api/window", { action: "float", id }),
      /** Drop a floating window back into the tiling layout. */
      popOut: (id) => post("/api/window", { action: "popout", id }),
    },

    /**
     * This app's own window.
     *
     * nethosd tags each window with the NETHOS app it belongs to, so an app can
     * find itself without guessing from the title.
     */
    window: {
      async self() {
        const all = await nethos.windows.list();
        return all.find((w) => w.nethos_app === APP_ID) || null;
      },
      async close() {
        const me = await nethos.window.self();
        if (me) return nethos.windows.close(me.id);
        global.close();
      },
      async fullscreen() {
        const me = await nethos.window.self();
        if (me) return nethos.windows.fullscreen(me.id);
      },
      /**
       * Turn a widget into an ordinary managed window: it stops floating and
       * being sticky, and joins the tiling layout like any other program.
       * Lets one app be an ambient widget that becomes a real window on demand.
       */
      async popOut() {
        const me = await nethos.window.self();
        if (me) return nethos.windows.popOut(me.id);
      },
      /** The inverse: float this window above the layout. */
      async float() {
        const me = await nethos.window.self();
        if (me) return nethos.windows.float(me.id);
      },
    },

    menu: {
      isOpen: () => get("/api/menu").then((r) => r.open),
      open: () => post("/api/menu", { open: true }),
      close: () => post("/api/menu", { open: false }),
      toggle: () => post("/api/launch", { builtin: "menu-toggle" }),
    },

    /**
     * Per-app persistent storage, kept in ~/.local/state/nethos/apps/<id>.json.
     * Unlike localStorage this survives a profile wipe and is a real file you
     * can read, diff and back up from a terminal.
     */
    storage: {
      async all() { return (await get("/api/storage/" + APP_ID)).data || {}; },
      async get(key, fallback) {
        const data = await nethos.storage.all();
        return key in data ? data[key] : fallback;
      },
      async set(key, value) {
        const data = await nethos.storage.all();
        data[key] = value;
        await put("/api/storage/" + APP_ID, { data });
        return value;
      },
      async remove(key) {
        const data = await nethos.storage.all();
        delete data[key];
        await put("/api/storage/" + APP_ID, { data });
      },
      async clear() { await put("/api/storage/" + APP_ID, { data: {} }); },
    },

    /** Small helpers so apps do not each reinvent them. */
    ui: {
      /** Minimal element factory: el("div", "cls", "text"). */
      el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text != null) node.textContent = text;
        return node;
      },
      /** Transient toast inside this window. */
      toast(text, level) {
        let host = document.querySelector(".neth-toasts");
        if (!host) {
          host = nethos.ui.el("div", "neth-toasts");
          document.body.appendChild(host);
        }
        const node = nethos.ui.el("div", "neth-toast neth-toast-" + (level || "info"), text);
        host.appendChild(node);
        setTimeout(() => node.remove(), 4000);
        return node;
      },
      /** Human-readable byte sizes, used by half of all status widgets. */
      bytes(kb) {
        const units = ["KB", "MB", "GB", "TB"];
        let n = kb, i = 0;
        while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
        return n.toFixed(n < 10 && i > 0 ? 1 : 0) + " " + units[i];
      },
      duration(seconds) {
        const d = Math.floor(seconds / 86400);
        const h = Math.floor((seconds % 86400) / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        if (d) return d + "d " + h + "h";
        if (h) return h + "h " + m + "m";
        return m + "m";
      },
    },
  };

  global.nethos = nethos;
  global.NETHOS = nethos;   // alias, both read naturally at a call site
})(window);
