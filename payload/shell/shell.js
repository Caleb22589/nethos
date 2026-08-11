/* NETHOS shell logic. One file, two views: the panel and the launcher menu.
   Everything privileged goes through nethosd on loopback. */

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

/* ------------------------------------------------------------------ panel */

function initPanel() {
  const tasks = document.getElementById("tasks");
  const brand = document.getElementById("brand");
  const clock = document.getElementById("clock");
  const metrics = document.getElementById("metrics");

  brand.addEventListener("click", async () => {
    const r = await post("/api/launch", { builtin: "menu-toggle" });
    brand.classList.toggle("active", !!r.open);
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
        post("/api/window", { action: "close", id: w.id }).then(refreshTasks);
      });
      b.append(x);
      b.title = (w.app_id || "") + "  ·  ws " + (w.workspace || "?");
      b.addEventListener("click", () =>
        post("/api/window", { action: "focus", id: w.id }).then(refreshTasks));
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

  async function syncBrand() {
    try {
      brand.classList.toggle("active", (await get("/api/menu")).open);
    } catch { /* daemon restarting */ }
  }

  refreshTasks(); refreshStatus(); syncBrand();
  setInterval(refreshTasks, 1500);
  setInterval(refreshStatus, 5000);
  setInterval(syncBrand, 1500);
}

/* ------------------------------------------------------------------- menu */

function initMenu() {
  const grid = document.getElementById("grid");
  const search = document.getElementById("search");
  const count = document.getElementById("count");
  let apps = [], view = [], sel = 0;

  const initials = (name) => {
    const w = name.split(/[\s\-_]+/).filter(Boolean);
    return ((w[0] || "?")[0] + (w.length > 1 ? w[1][0] : "")).toUpperCase();
  };

  function render() {
    grid.replaceChildren();
    view.forEach((app, i) => {
      const b = el("button", "app" + (i === sel ? " sel" : "")
                            + (app.source === "nethos" ? " app-nethos" : ""));
      b.append(el("div", "icon", app.icon && app.icon.length <= 2
                                 ? app.icon : initials(app.name)));
      const meta = el("div", "meta");
      const nameRow = el("div", "name-row");
      nameRow.append(el("span", "name", app.name));
      // NETHOS apps are the ones you can edit and hot-reload; mark them.
      if (app.source === "nethos") nameRow.append(el("span", "tag", "APP"));
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
    if (grid.children[sel]) {
      grid.children[sel].scrollIntoView({ block: "nearest" });
    }
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

  get("/api/apps").then((r) => { apps = r.apps; filter(); search.focus(); })
    .catch(() => { count.textContent = "nethosd unreachable"; });
}

/* --------------------------------------------------------------- reload */

/* The shell eats its own dog food: it subscribes to the same event stream the
   app SDK exposes, so editing panel.html or style.css reloads the panel within
   a second — no reboot, no logout, no relaunching Chromium. */
function initLiveReload() {
  if (typeof EventSource === "undefined") return;
  let generation = null;

  const stream = new EventSource(API + "/api/events");
  stream.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }

    if (msg.type === "reload") {
      if (generation !== null && msg.generation !== generation) {
        location.reload();
        return;
      }
      generation = msg.generation;
    } else if (msg.type === "notify" && document.body.dataset.view === "panel") {
      toast(msg.data.text, msg.data.level);
    }
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
  initLiveReload();
});
