/* NETHOS shell logic. One file, two views: the panel and the launcher.
   Everything privileged goes through nethosd on loopback.

   The shell is event-driven. It used to poll /api/windows every 1.5s, which
   meant nethosd spawned a swaymsg subprocess and parsed a whole window tree on
   a timer -- painful on a slow machine. Now sway's own event stream is
   forwarded over SSE and the taskbar redraws only when something changed. The
   timers that remain are slow safety nets, not the mechanism. */

const API = "http://127.0.0.1:7777";

async function get(path) {
  const r = await fetch(API + path, { cache: "no-store" });
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}

async function post(path, body) {
  const r = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json().catch(() => ({}));
}

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

/* Collapse bursts of events into one redraw: sway emits several in a row for
   a single user action (new, focus, title), and redrawing per event is waste. */
function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

/* Shared icon tile: a real application icon when nethosd resolved one from the
   icon themes, otherwise initials. */
function iconTile(app, cls) {
  const initials = (name) => {
    const w = String(name).split(/[\s\-_]+/).filter(Boolean);
    return ((w[0] || "?")[0] + (w.length > 1 ? w[1][0] : "")).toUpperCase();
  };

  const tile = el("div", cls);
  if (app.icon_url) {
    const img = el("img");
    img.src = app.icon_url;
    img.alt = "";
    img.loading = "lazy";
    // Themes lie: an index entry can point at a file that will not decode.
    img.addEventListener("error", () => {
      tile.replaceChildren();
      tile.textContent = initials(app.name);
    });
    tile.append(img);
  } else if (app.icon && app.icon.length <= 2) {
    tile.textContent = app.icon;          // manifest text tile, e.g. "SY"
  } else {
    tile.textContent = initials(app.name);
  }
  return tile;
}

/* ------------------------------------------------------------------ panel */

function initPanel() {
  const tasks = document.getElementById("tasks");
  const brand = document.getElementById("brand");
  const clock = document.getElementById("clock");
  const metrics = document.getElementById("metrics");

  let busy = false;
  brand.addEventListener("click", async () => {
    // Toggling is fast now, but a double click should still not queue two
    // opposite toggles and land back where it started.
    if (busy) return;
    busy = true;
    try {
      const r = await post("/api/launch", { builtin: "menu-toggle" });
      brand.classList.toggle("active", !!r.open);
    } finally {
      setTimeout(() => { busy = false; }, 150);
    }
  });

  async function refreshTasks() {
    let windows = [];
    try { windows = (await get("/api/windows")).windows; } catch { return; }

    tasks.replaceChildren();
    if (!windows.length) {
      tasks.append(el("div", "empty", "no windows — press the NETHOS button"));
      return;
    }
    for (const w of windows) {
      const b = el("button", "task" + (w.focused ? " focused" : ""));
      b.append(el("span", "dot"));
      b.append(el("span", "label", w.title || w.app_id || "window"));
      const x = el("span", "x", "×");
      x.title = "close";
      x.addEventListener("click", (e) => {
        e.stopPropagation();
        post("/api/window", { action: "close", id: w.id });
      });
      b.append(x);
      b.title = (w.app_id || "") + "  ·  ws " + (w.workspace || "?");
      b.addEventListener("click", () => post("/api/window", { action: "focus", id: w.id }));
      tasks.append(b);
    }
  }

  async function refreshStatus() {
    let s;
    try { s = await get("/api/status"); } catch { return; }

    const d = new Date(s.time * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    clock.replaceChildren(
      el("span", "date", d.toLocaleDateString(undefined,
        { weekday: "short", day: "numeric", month: "short" })),
      el("span", null, pad(d.getHours()) + ":" + pad(d.getMinutes()))
    );

    const parts = [["mem", s.mem.used_pct + "%"], ["load", s.load.toFixed(2)]];
    if (s.battery && s.battery.percent != null) {
      parts.unshift(["bat", s.battery.percent + "%"]);
    }
    metrics.replaceChildren(...parts.map(([k, v]) => {
      const m = el("span", "metric");
      m.append(el("span", null, k), el("b", null, v));
      return m;
    }));
  }

  const tasksSoon = debounce(refreshTasks, 60);

  onShellEvent((msg) => {
    if (msg.type === "windows") tasksSoon();
    else if (msg.type === "menu") brand.classList.toggle("active", !!msg.data.open);
    else if (msg.type === "notify") toast(msg.data.text, msg.data.level);
  });

  refreshTasks();
  refreshStatus();
  setInterval(refreshStatus, 10000);
  setInterval(refreshTasks, 15000);   // safety net if the event stream drops
}

/* ------------------------------------------------------------------- menu */

function initMenu() {
  const grid = document.getElementById("grid");
  const search = document.getElementById("search");
  const count = document.getElementById("count");
  let apps = [], view = [], sel = 0;

  function render() {
    grid.replaceChildren();
    view.forEach((app, i) => {
      const b = el("button", "app" + (i === sel ? " sel" : "")
                            + (app.source === "nethos" ? " app-nethos" : ""));
      b.append(iconTile(app, "icon"));
      const meta = el("div", "meta");
      const nameRow = el("div", "name-row");
      nameRow.append(el("span", "name", app.name));
      if (app.source === "nethos") {
        nameRow.append(el("span", "tag", app.mode === "widget" ? "WIDGET" : "APP"));
      }
      meta.append(nameRow);
      meta.append(el("div", "desc", app.comment || app.categories.join(" · ") || app.id));
      b.append(meta);
      b.addEventListener("click", () => launch(app));
      b.addEventListener("mousemove", () => {
        if (sel !== i) { sel = i; paintSel(); }
      });
      grid.append(b);
    });
    count.textContent = view.length + " of " + apps.length + " applications";
  }

  function paintSel() {
    [...grid.children].forEach((c, i) => c.classList.toggle("sel", i === sel));
    if (grid.children[sel]) grid.children[sel].scrollIntoView({ block: "nearest" });
  }

  function filter() {
    const q = search.value.trim().toLowerCase();
    view = !q ? apps.slice() : apps.filter((a) =>
      (a.name + " " + a.comment + " " + a.categories.join(" ") + " " + a.id)
        .toLowerCase().includes(q));
    sel = 0;
    render();
  }

  function launch(app) { post("/api/launch", { id: app.id }); }
  function close() { post("/api/menu", { open: false }); }

  search.addEventListener("input", filter);

  document.addEventListener("keydown", (e) => {
    const cols = Math.max(1, Math.round(grid.clientWidth / 198));
    if (e.key === "Escape") { close(); }
    else if (e.key === "Enter" && view[sel]) { launch(view[sel]); }
    else if (e.key === "ArrowRight") { sel = Math.min(view.length - 1, sel + 1); paintSel(); }
    else if (e.key === "ArrowLeft") { sel = Math.max(0, sel - 1); paintSel(); }
    else if (e.key === "ArrowDown") { sel = Math.min(view.length - 1, sel + cols); paintSel(); }
    else if (e.key === "ArrowUp") { sel = Math.max(0, sel - cols); paintSel(); }
    else { return; }
    e.preventDefault();
  });

  document.querySelectorAll("[data-builtin]").forEach((b) => {
    b.addEventListener("click", () => post("/api/launch", { builtin: b.dataset.builtin }));
  });

  async function load() {
    try {
      apps = (await get("/api/apps")).apps;
      filter();
    } catch {
      count.textContent = "nethosd unreachable";
    }
  }

  // The launcher window is never destroyed, only hidden in the scratchpad, so
  // "opening" it is a focus event. Reset it here rather than on load.
  window.addEventListener("focus", () => {
    search.value = "";
    load().then(() => search.focus());
  });

  load().then(() => search.focus());
  onShellEvent((msg) => { if (msg.type === "windows") load(); });
}

/* ------------------------------------------------------- events + toasts */

let stream = null;
const shellHandlers = new Set();

function onShellEvent(handler) {
  shellHandlers.add(handler);
  if (stream || typeof EventSource === "undefined") return;

  let generation = null;
  stream = new EventSource(API + "/api/events");
  stream.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }

    if (msg.type === "reload") {
      // Editing panel.html or style.css reloads the shell in place.
      if (generation !== null && msg.generation !== generation) {
        location.reload();
        return;
      }
      generation = msg.generation;
    }
    shellHandlers.forEach((fn) => {
      try { fn(msg); } catch (err) { console.error("[nethos]", err); }
    });
  };
}

function toast(text, level) {
  let host = document.querySelector(".toasts");
  if (!host) {
    host = el("div", "toasts");
    document.body.appendChild(host);
  }
  const node = el("div", "toast toast-" + (level || "info"), text);
  host.appendChild(node);
  setTimeout(() => node.remove(), 4000);
}

document.addEventListener("DOMContentLoaded", () => {
  if (document.body.dataset.view === "panel") initPanel();
  else initMenu();
});
