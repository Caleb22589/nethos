/* NETHOS shell — panel, dock, launcher and desktop.
 *
 * Each view is its own layer-shell surface hosted by nethos-view, and they all
 * share this file. State arrives over the event stream rather than polling:
 * the compositor tells nethosd, nethosd tells us. The timers that remain are
 * slow safety nets. */

const API = "http://127.0.0.1:7777";

const get = async (p) => {
  const r = await fetch(API + p, { cache: "no-store" });
  if (!r.ok) throw new Error(p + " " + r.status);
  return r.json();
};
const post = async (p, b) => {
  const r = await fetch(API + p, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(b || {}),
  });
  return r.json().catch(() => ({}));
};

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

const initials = (name) => {
  const w = String(name || "?").split(/[\s\-_.]+/).filter(Boolean);
  return ((w[0] || "?")[0] + (w.length > 1 ? w[1][0] : "")).toUpperCase();
};

/* An icon tile: real themed icon where nethosd resolved one, initials if not. */
function iconTile(app, cls, fallbackCls) {
  const tile = el("div", cls);
  if (app.icon_url) {
    const img = el("img");
    img.src = app.icon_url;
    img.alt = "";
    img.addEventListener("error", () => {
      tile.replaceChildren(el("span", fallbackCls || "fallback", initials(app.name)));
    });
    tile.append(img);
  } else if (app.icon && app.icon.length <= 2) {
    tile.append(el("span", fallbackCls || "fallback", app.icon));
  } else {
    tile.append(el("span", fallbackCls || "fallback", initials(app.name)));
  }
  return tile;
}

/* ------------------------------------------------------------------ panel */

function initPanel() {
  const tasks = document.getElementById("tasks");
  const brand = document.getElementById("brand");
  const clock = document.getElementById("clock");
  const metrics = document.getElementById("metrics");
  const tray = document.getElementById("tray");

  let busy = false;
  brand.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    try { await post("/api/launch", { builtin: "menu-toggle" }); }
    finally { setTimeout(() => { busy = false; }, 120); }
  });

  async function refreshTasks() {
    let windows = [];
    try { windows = (await get("/api/windows")).windows; } catch { return; }
    tasks.replaceChildren();
    if (!windows.length) return;
    for (const w of windows) {
      const b = el("button", "task" + (w.focused ? " focused" : ""));
      b.append(el("span", "label", w.title || w.app_id || "Window"));
      const x = el("span", "x", "×");
      x.addEventListener("click", (e) => {
        e.stopPropagation();
        post("/api/window", { action: "close", id: w.id });
      });
      b.append(x);
      b.title = w.title || "";
      b.addEventListener("click", () => post("/api/window", { action: "focus", id: w.id }));
      tasks.append(b);
    }
  }

  async function refreshTray() {
    let items = [];
    try { items = (await get("/api/tray")).items; } catch { return; }
    tray.replaceChildren();
    for (const it of items) {
      const b = el("button", "tray-item");
      b.title = it.title || it.id || "";
      if (it.icon_url) {
        const img = el("img");
        img.src = it.icon_url;
        img.addEventListener("error", () =>
          b.replaceChildren(el("span", "fallback", initials(it.title || it.id))));
        b.append(img);
      } else {
        b.append(el("span", "fallback", initials(it.title || it.id)));
      }
      b.addEventListener("click", () => post("/api/tray/activate", { id: it.id }));
      b.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        post("/api/tray/activate", { id: it.id, secondary: true });
      });
      tray.append(b);
    }
  }

  async function refreshStatus() {
    let s;
    try { s = await get("/api/status"); } catch { return; }
    const d = new Date(s.time * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    clock.replaceChildren(
      el("span", "date", d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })),
      el("span", null, pad(d.getHours()) + ":" + pad(d.getMinutes()))
    );
    const parts = [];
    if (s.battery && s.battery.percent != null) parts.push(["", s.battery.percent + "%"]);
    parts.push(["mem", s.mem.used_pct + "%"]);
    metrics.replaceChildren(...parts.map(([k, v]) => {
      const m = el("span", "metric");
      if (k) m.append(el("span", null, k));
      m.append(el("b", null, v));
      return m;
    }));
  }

  const soon = debounce(refreshTasks, 60);
  onEvent((msg) => {
    if (msg.type === "windows") soon();
    else if (msg.type === "menu") brand.classList.toggle("active", !!msg.data.open);
    else if (msg.type === "tray") refreshTray();
    else if (msg.type === "notify") toast(msg.data.text, msg.data.level);
  });

  refreshTasks(); refreshStatus(); refreshTray();
  // Driven by the server tick as well as a timer: the timer is the one that
  // stops when WebKit decides this surface is hidden, and the tick is the one
  // that keeps arriving. Both call the same refreshers, and they are cheap.
  let lastTick = 0;
  window.addEventListener("nethos-tick", () => {
    const now = Date.now();
    refreshStatus();
    if (now - lastTick > 15000) { lastTick = now; refreshTasks(); refreshTray(); }
  });
  onEvent((msg) => {
    if (msg.type !== "tick") return;
    const now = Date.now();
    refreshStatus();
    if (now - lastTick > 15000) { lastTick = now; refreshTasks(); refreshTray(); }
  });
  setInterval(refreshStatus, 15000);
  setInterval(refreshTasks, 20000);
}

/* ------------------------------------------------------------------- dock */

const DOCK_DEFAULTS = ["chromium.desktop", "foot.desktop", "thunar.desktop", "system"];

function initDock() {
  const dock = document.getElementById("dock");
  const body = document.body;
  let apps = [], pinned = DOCK_DEFAULTS.slice(), running = [];
  let autohide = true;

  /* Auto-hide is done with the host: when hidden the surface reserves no
     space and only a thin strip at the bottom accepts clicks, so the dock
     never eats a click meant for a window underneath. When pinned it reserves
     its height and the compositor shrinks the work area to match. */
  function applyHostGeometry() {
    if (typeof nethosHost === "undefined") return;
    const hidden = body.classList.contains("hidden");
    const h = window.innerHeight;
    if (!autohide) {
      nethosHost.exclusive(82);
      nethosHost.inputRect(0, h - 82, window.innerWidth, 82);
    } else if (hidden) {
      nethosHost.exclusive(0);
      nethosHost.inputRect(0, h - 4, window.innerWidth, 4);   // hover strip
    } else {
      nethosHost.exclusive(0);
      nethosHost.inputRect(0, h - 92, window.innerWidth, 92);
    }
  }

  const hideSoon = debounce(() => {
    if (autohide) { body.classList.add("hidden"); applyHostGeometry(); }
  }, 700);

  function reveal() {
    body.classList.remove("hidden");
    applyHostGeometry();
    hideSoon();
  }

  document.addEventListener("mousemove", (e) => {
    if (!autohide) return;
    if (e.clientY > window.innerHeight - 24) reveal();
    else if (!body.classList.contains("hidden")) hideSoon();
  });
  document.addEventListener("mouseleave", hideSoon);

  async function loadPrefs() {
    try {
      const { data } = await get("/api/storage/shell.dock");
      if (Array.isArray(data.pinned)) pinned = data.pinned;
      if (typeof data.autohide === "boolean") autohide = data.autohide;
    } catch { /* defaults are fine */ }
    body.classList.toggle("hidden", autohide);
    applyHostGeometry();
  }

  function render() {
    const byId = new Map(apps.map((a) => [a.id, a]));
    const runningIds = new Set(running.map((w) => w.nethos_app).filter(Boolean));
    const runningClasses = new Set(running.map((w) => (w.app_id || "").toLowerCase()));

    dock.replaceChildren();
    const shown = pinned.map((id) => byId.get(id)).filter(Boolean);
    for (const app of shown) {
      const b = el("button", "dock-item");
      b.append(iconTile(app, "dock-icon"));
      const live = runningIds.has(app.id) ||
                   runningClasses.has((app.id || "").replace(/\.desktop$/, "").toLowerCase());
      if (live) b.classList.add("running");
      b.append(el("span", "dock-tip", app.name));
      b.addEventListener("click", () => post("/api/launch", { id: app.id }));
      dock.append(b);
    }

    // Anything running that is not pinned still gets a slot, so a window is
    // always one click away even if it was never in the dock.
    const extras = running.filter((w) => {
      const cls = (w.app_id || "").toLowerCase();
      return !shown.some((a) =>
        a.id === w.nethos_app ||
        (a.id || "").replace(/\.desktop$/, "").toLowerCase() === cls);
    });
    if (extras.length) {
      dock.append(el("div", "dock-sep"));
      for (const w of extras) {
        const b = el("button", "dock-item running");
        b.append(el("span", "fallback", initials(w.app_id || w.title)));
        b.append(el("span", "dock-tip", w.title || w.app_id));
        b.addEventListener("click", () => post("/api/window", { action: "focus", id: w.id }));
        dock.append(b);
      }
    }
  }

  async function refresh() {
    try {
      apps = (await get("/api/apps")).apps;
      running = (await get("/api/windows")).windows;
    } catch { return; }
    render();
  }

  const soon = debounce(refresh, 80);
  onEvent((msg) => { if (msg.type === "windows") soon(); });

  loadPrefs().then(refresh);
  window.addEventListener("resize", applyHostGeometry);
  let widgetTick = 0;
  window.addEventListener("nethos-tick", () => {
    const now = Date.now();
    if (now - widgetTick > 30000) { widgetTick = now; refresh(); }
  });
  onEvent((msg) => {
    if (msg.type !== "tick") return;
    const now = Date.now();
    if (now - widgetTick > 30000) { widgetTick = now; refresh(); }
  });
  setInterval(refresh, 30000);
}

/* --------------------------------------------------------------- launcher */

function initMenu() {
  const grid = document.getElementById("grid");
  const search = document.getElementById("search");
  const count = document.getElementById("count");
  let apps = [], view = [], sel = 0, open = false;

  function render() {
    grid.replaceChildren();
    view.forEach((app, i) => {
      const b = el("button", "app" + (i === sel ? " sel" : ""));
      b.append(iconTile(app, "icon"));
      const meta = el("div", "meta");
      const row = el("div", "name-row");
      row.append(el("span", "name", app.name));
      if (app.source === "nethos") {
        row.append(el("span", "tag", app.mode === "widget" ? "Widget" : "App"));
      }
      meta.append(row);
      meta.append(el("div", "desc", app.comment || (app.categories || []).join(" · ")));
      b.append(meta);
      b.addEventListener("click", () => launch(app));
      b.addEventListener("mousemove", () => { if (sel !== i) { sel = i; paint(); } });
      grid.append(b);
    });
    count.textContent = view.length ? view.length + " results" : "Nothing matches";
  }

  function paint() {
    [...grid.children].forEach((c, i) => c.classList.toggle("sel", i === sel));
    if (grid.children[sel]) grid.children[sel].scrollIntoView({ block: "nearest" });
  }

  function filter() {
    const q = search.value.trim().toLowerCase();
    view = !q ? apps.slice() : apps.filter((a) =>
      (a.name + " " + a.comment + " " + (a.categories || []).join(" ") + " " + a.id)
        .toLowerCase().includes(q));
    sel = 0;
    render();
  }

  const launch = (app) => post("/api/launch", { id: app.id });
  const close = () => post("/api/menu", { open: false });

  search.addEventListener("input", filter);
  document.addEventListener("keydown", (e) => {
    const cols = Math.max(1, Math.round(grid.clientWidth / 234));
    if (e.key === "Escape") close();
    else if (e.key === "Enter" && view[sel]) launch(view[sel]);
    else if (e.key === "ArrowRight") { sel = Math.min(view.length - 1, sel + 1); paint(); }
    else if (e.key === "ArrowLeft") { sel = Math.max(0, sel - 1); paint(); }
    else if (e.key === "ArrowDown") { sel = Math.min(view.length - 1, sel + cols); paint(); }
    else if (e.key === "ArrowUp") { sel = Math.max(0, sel - cols); paint(); }
    else return;
    e.preventDefault();
  });
  document.querySelectorAll("[data-builtin]").forEach((b) =>
    b.addEventListener("click", () => post("/api/launch", { builtin: b.dataset.builtin })));

  async function load() {
    try { apps = (await get("/api/apps")).apps; filter(); }
    catch { count.textContent = "nethosd unreachable"; }
  }

  /* The launcher surface exists for the whole session and shows/hides itself,
     which is why opening it is instant: no process start, no compositor call. */
  function setOpen(want) {
    if (want === open) return;
    open = want;
    document.body.classList.toggle("shown", open);
    if (typeof nethosHost !== "undefined") {
      if (open) nethosHost.show(); else setTimeout(() => nethosHost.hide(), 200);
    }
    if (open) { search.value = ""; load().then(() => search.focus()); }
  }

  onEvent((msg) => { if (msg.type === "menu") setOpen(!!msg.data.open); });
  if (typeof nethosHost !== "undefined") nethosHost.hide();
  load();
}

/* ---------------------------------------------------------------- desktop */

function initDesktop() {
  const host = document.getElementById("widgets");

  /* Widgets are iframes in this one surface rather than one window each, so
     five widgets cost one web process instead of five. */
  async function load() {
    let apps = [];
    try { apps = (await get("/api/apps")).apps; } catch { return; }
    const widgets = apps.filter((a) => a.source === "nethos" && a.mode === "widget");

    host.replaceChildren();
    for (const w of widgets) {
      const frame = document.createElement("iframe");
      frame.src = "/apps/" + w.id + "/" + (w.entry || "index.html");
      frame.style.cssText =
        "width:" + (w.width || 300) + "px;height:" + (w.height || 190) +
        "px;border:0;border-radius:var(--r-lg);overflow:hidden;" +
        "box-shadow:inset 0 1px 0 0 var(--rim),inset 0 0 0 1px var(--rim-soft)," +
        "var(--shadow);background:var(--glass);";
      frame.loading = "lazy";
      host.append(frame);
    }
  }

  onEvent((msg) => { if (msg.type === "reload") load(); });
  load();
}

/* ------------------------------------------------------------ event stream */

let stream = null;
const handlers = new Set();

function onEvent(handler) {
  handlers.add(handler);
  if (stream || typeof EventSource === "undefined") return;
  // Not from inside a widget iframe. /api/events never returns, so every
  // document that opens one holds a connection for the life of the session,
  // and a browser allows only about six per host. The desktop carries three
  // widget iframes: with one stream each plus the page's own, the pool is
  // nearly full before any data is fetched, and every later request queues
  // forever -- a clock that stops, buttons that do nothing, and in the
  // inspector a column of requests that never complete.
  //
  // Widgets get the host tick, which costs no connection at all.
  if (window.top !== window.self) return;
  let generation = null;
  stream = new EventSource(API + "/api/events");
  stream.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "reload") {
      if (generation !== null && msg.generation !== generation) { location.reload(); return; }
      generation = msg.generation;
    }
    handlers.forEach((fn) => { try { fn(msg); } catch (e) { console.error(e); } });
  };
}

function toast(text, level) {
  let host = document.querySelector(".toasts");
  if (!host) { host = el("div", "toasts"); document.body.appendChild(host); }
  const node = el("div", "toast toast-" + (level || "info"), text);
  host.appendChild(node);
  setTimeout(() => node.remove(), 4000);
}

document.addEventListener("DOMContentLoaded", () => {
  const view = document.body.dataset.view;
  if (view === "panel") initPanel();
  else if (view === "dock") initDock();
  else if (view === "menu") initMenu();
  else if (view === "desktop") initDesktop();
});

/* ---------------------------------------------------------- diagnostics --
 * The shell surfaces have no visible output. Their console goes to the
 * compositor's stdout, which nothing collects on a running machine, so a page
 * whose timers have stopped looks exactly like a page with nothing to do. This
 * reports errors to nethosd and beats every ten seconds; `nethos-doctor` reads
 * both back. If a beat stops arriving, that page's JavaScript has stopped, and
 * the timestamp says when.
 */
(function diagnostics() {
  const surface = (location.pathname.replace(/^\/|\.html$/g, "") || "shell");
  const send = (kind, message) => {
    try {
      fetch("/api/log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, surface, message }),
        keepalive: true,
      }).catch(() => {});
    } catch (e) { /* never let logging break the shell */ }
  };
  window.addEventListener("error", (e) =>
    send("error", e.message + " @ " + (e.filename || "?") + ":" + (e.lineno || 0)));
  window.addEventListener("unhandledrejection", (e) =>
    send("reject", String(e.reason)));
  send("start", "loaded " + new Date().toISOString());
  send("beat", "");
  // Called from nethos-view, which is not throttled. This is the only thing
  // that reliably runs once WebKit has suspended the page, so everything
  // periodic hangs off it.
  window.nethosTick = function () {
    send("beat", "host");
    try { window.dispatchEvent(new Event("nethos-tick")); } catch (e) {}
  };
  // One beat source, and no new connection for it.
  //
  // This used to open its own EventSource, which was actively harmful: a
  // browser allows about six connections per host, /api/events never returns,
  // and the desktop already holds one plus one per widget iframe. A second
  // permanent stream per surface pushed the pool over the edge, and every
  // fetch after that queued forever -- visible in the inspector as requests
  // that never complete, and on screen as a clock that stops and buttons that
  // do nothing. The host tick needs no connection at all.
  setInterval(() => send("beat", "timer"), 10000);
})();
