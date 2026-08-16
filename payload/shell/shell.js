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
/* /api/storage/<key> is written with PUT, not POST -- posting to it returns
   404 and the write is silently lost. */
const put = async (p, b) => {
  const r = await fetch(API + p, {
    method: "PUT",
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

  /* The surface is 360px so menus have somewhere to open; only the bar takes
     input. Without this the transparent area below the bar would swallow
     every click in the top third of the screen. */
  const BAR_H = 54;
  function applyPanelRect() {
    setHostRect({ x: 0, y: 0, w: window.innerWidth, h: BAR_H });
  }
  applyPanelRect();
  window.addEventListener("resize", applyPanelRect);

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
      onContext(b, () => windowMenuItems(w));
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

  // The status cluster is the handle for the control centre, the way the
  // right-hand side of a menu bar is on macOS. It was previously inert, which
  // meant the battery and the clock were the only things on screen that
  // looked like controls and were not.
  const statusEl = document.getElementById("status");
  if (statusEl) {
    statusEl.style.cursor = "default";
    statusEl.addEventListener("click", () => post("/api/control/toggle", {}));
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
    // setHostRect rather than inputRect directly, so an open context menu can
    // widen the region and put it back afterwards.
    if (!autohide) {
      nethosHost.exclusive(82);
      setHostRect({ x: 0, y: h - 82, w: window.innerWidth, h: 82 });
    } else if (hidden) {
      nethosHost.exclusive(0);
      setHostRect({ x: 0, y: h - 4, w: window.innerWidth, h: 4 }); // hover strip
    } else {
      nethosHost.exclusive(0);
      setHostRect({ x: 0, y: h - 92, w: window.innerWidth, h: 92 });
    }
  }

  const hideSoon = debounce(() => {
    if (autohide) { body.classList.add("hidden"); applyHostGeometry(); }
  }, 700);

  /* Hover cannot end on its own here.
     inputRect limits this surface to the dock pill, so the moment the pointer
     leaves it the surface stops receiving events entirely -- mouseleave never
     arrives and whatever was hovered stays hovered, with its label on screen
     for good. So hover is ended by a timer that each pointermove restarts. */
  let hoverTimer = 0;
  const clearHover = () => {
    dock.querySelectorAll(".hover, :hover").forEach((el) =>
      el.classList.remove("hover"));
    body.classList.remove("hovering");
  };
  document.addEventListener("pointermove", () => {
    body.classList.add("hovering");
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(clearHover, 500);
  }, true);

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

  async function savePrefs() {
    try {
      await put("/api/storage/shell.dock", { data: { pinned, autohide } });
    } catch { /* the dock still shows the change; it just will not survive */ }
  }

  function pin(id) {
    if (!id || pinned.includes(id)) return;
    // .desktop entries are stored with their suffix, NETHOS apps without one.
    // A window's app_id carries neither, so try both spellings against what
    // is actually installed rather than guessing.
    const known = apps.some((a) => a.id === id);
    const alt = id.endsWith(".desktop") ? id.slice(0, -8) : id + ".desktop";
    const use = known ? id : (apps.some((a) => a.id === alt) ? alt : id);
    if (pinned.includes(use)) return;
    pinned.push(use);
    savePrefs();
    render();
  }

  function unpin(id) {
    const i = pinned.indexOf(id);
    if (i < 0) return;
    pinned.splice(i, 1);
    savePrefs();
    render();
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
      // Right-click acts on the app's windows if it has any, and offers to
      // unpin whether it does or not. Built at click time so "Quit" only
      // appears when there is actually something to quit.
      onContext(b, () => {
        const wins = running.filter((w) =>
          w.nethos_app === app.id ||
          (w.app_id || "").toLowerCase() ===
            (app.id || "").replace(/\.desktop$/, "").toLowerCase());
        const items = [{ label: "Open", run: () => post("/api/launch", { id: app.id }) }];
        if (wins.length === 1) {
          items.push({ label: "Focus",
                       run: () => post("/api/window", { action: "focus", id: wins[0].id }) });
        }
        items.push("-");
        items.push({ label: "Unpin from dock", run: () => unpin(app.id) });
        if (wins.length) {
          items.push({
            label: wins.length > 1 ? "Quit all (" + wins.length + ")" : "Quit",
            danger: true,
            run: () => wins.forEach((w) =>
              post("/api/window", { action: "close", id: w.id })),
          });
        }
        return items;
      });
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
        // Title first, app_id second. The app_id of a NETHOS window is
        // "nethos-files", which initials to "NF" -- correct and useless. The
        // title is "Files".
        b.append(el("span", "fallback", initials(w.title || w.app_id)));
        b.append(el("span", "dock-tip", w.title || w.app_id));
        b.addEventListener("click", () => post("/api/window", { action: "focus", id: w.id }));
        onContext(b, () => windowMenuItems(w).concat([
          "-",
          { label: "Pin to dock", run: () => pin(w.nethos_app || w.app_id) },
        ]));
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

  /* When the user changes "Hide the dock" in Settings, the data-autohide
     attribute is set globally but the dock's own autohide variable and the
     host input region are not updated — so the dock draws in the right place
     but the thin hover strip is all that accepts clicks. */
  onEvent((msg) => {
    if (msg.type !== "settings" || typeof msg.data.dock_autohide !== "boolean") return;
    autohide = msg.data.dock_autohide;
    body.classList.toggle("hidden", autohide);
    applyHostGeometry();
  });

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

  /* Hosting context menus for the other surfaces.
     The overlay is full-screen and takes clicks anywhere, which the panel and
     dock do not outside their exclusive zones. `ctx` shows the surface without
     showing the launcher. */
  let ctxOpen = false;
  function endCtx(token, index) {
    if (!ctxOpen) return;
    ctxOpen = false;
    closeContextMenu();
    document.body.classList.remove("ctx");
    if (typeof nethosHost !== "undefined" && !open) {
      nethosHost.inputRect(0, 0, 0, 0);
    }
    post("/api/contextmenu/choose", { token, index });
  }

  /* The control centre lives in this surface for the same reason menus do:
     it is full-screen and reliably takes clicks, which the panel is not
     outside its exclusive zone. */
  let ccOpen = false;
  const cc = document.createElement("div");
  cc.id = "cc";
  cc.hidden = true;
  document.body.append(cc);

  function ccClose() {
    if (!ccOpen) return;
    ccOpen = false;
    cc.hidden = true;
    document.body.classList.remove("ctx");
    post("/api/control/toggle", { open: false });
    if (typeof nethosHost !== "undefined" && !open) {
      nethosHost.inputRect(0, 0, 0, 0);
    }
    if (ccDismiss) {
      window.removeEventListener("pointerdown", ccDismiss, true);
      ccDismiss = null;
    }
  }

  function bar(pct) {
    const t = el("div", "cc-bar");
    const f = el("div", "cc-fill");
    f.style.width = Math.max(0, Math.min(100, pct)) + "%";
    t.append(f);
    return t;
  }

  async function ccRender() {
    const d = await get("/api/control").catch(() => null);
    if (!d) return;
    cc.replaceChildren();

    const card = el("div", "cc-card glass");

    // Battery
    if (d.battery && d.battery.percent != null) {
      const b = el("div", "cc-block");
      const head = el("div", "cc-head");
      head.append(el("span", "cc-label", "Battery"));
      head.append(el("span", "cc-value",
        d.battery.percent + "%" + (d.battery.remaining ? " · " + d.battery.remaining : "")));
      b.append(head, bar(d.battery.percent));
      const st = el("div", "cc-sub", d.battery.state);
      b.append(st);
      card.append(b);
    }

    // Brightness
    if (d.brightness != null) {
      const b = el("div", "cc-block");
      const head = el("div", "cc-head");
      head.append(el("span", "cc-label", "Brightness"));
      const val = el("span", "cc-value", d.brightness + "%");
      head.append(val);
      const range = document.createElement("input");
      range.type = "range"; range.min = "5"; range.max = "100"; range.step = "5";
      range.value = d.brightness;
      range.className = "cc-range";
      range.addEventListener("input", () => { val.textContent = range.value + "%"; });
      // change, not input: one request when the drag ends rather than one per
      // pixel, each of which shells out to brightnessctl.
      range.addEventListener("change", () =>
        post("/api/control/brightness", { value: Number(range.value) }));
      b.append(head, range);
      card.append(b);
    }

    // Volume. Absent entirely when there is no audio stack -- a slider that
    // cannot move is worse than no slider, because it looks like a fault.
    if (d.volume) {
      const b = el("div", "cc-block");
      const head = el("div", "cc-head");
      head.append(el("span", "cc-label", "Volume"));
      const val = el("span", "cc-value", d.volume.muted ? "Muted" : d.volume.level + "%");
      head.append(val);
      const mute = el("button", "cc-toggle" + (d.volume.muted ? "" : " on"));
      mute.append(el("i"));
      mute.title = d.volume.muted ? "Unmute" : "Mute";
      mute.addEventListener("click", async () => {
        await post("/api/control/volume", { muted: !d.volume.muted });
        ccRender();
      });
      head.append(mute);
      const range = document.createElement("input");
      range.type = "range"; range.min = "0"; range.max = "100"; range.step = "5";
      range.value = d.volume.level;
      range.className = "cc-range";
      range.disabled = !!d.volume.muted;
      range.addEventListener("input", () => { val.textContent = range.value + "%"; });
      range.addEventListener("change", () =>
        post("/api/control/volume", { value: Number(range.value) }));
      b.append(head, range);
      card.append(b);
    }

    // Wi-Fi. The block is drawn now and its contents arrive after: listing
    // networks takes seconds, and nothing else on this panel should wait for
    // it.
    const net = el("div", "cc-block");
    net.id = "cc-wifi";
    const head = el("div", "cc-head");
    head.append(el("span", "cc-label", "Wi‑Fi"));
    net.append(head);
    if (d.wifi && d.wifi.available) {
      const loading = el("div", "cc-sub", "Looking…");
      net.append(loading);
    } else {
      net.append(el("div", "cc-sub", "NetworkManager is not installed"));
    }
    card.append(net);

    cc.append(card);
    if (d.wifi && d.wifi.available) ccNetworks();
  }

  async function ccNetworks() {
    const r = await get("/api/control/networks").catch(() => null);
    const net = document.getElementById("cc-wifi");
    if (!r || !net) return;
    const w = r.wifi || {};
    net.replaceChildren();
    const head = el("div", "cc-head");
    head.append(el("span", "cc-label", "Wi‑Fi"));
    const toggle = el("button", "cc-toggle" + (w.enabled ? " on" : ""));
    toggle.append(el("i"));
    toggle.addEventListener("click", async () => {
      await post("/api/control/wifi", { action: w.enabled ? "off" : "on" });
      setTimeout(ccNetworks, 900);
    });
    head.append(toggle);
    net.append(head);

    if (!w.enabled) {
      net.append(el("div", "cc-sub", "Off"));
    } else if (!w.networks || !w.networks.length) {
      net.append(el("div", "cc-sub", "Nothing in range yet"));
    } else {
      const list = el("div", "cc-nets");
      for (const n of w.networks) {
        const row = el("button", "cc-net" + (n.active ? " on" : ""));
        row.append(el("span", "cc-ssid", n.ssid));
        row.append(el("span", "cc-sig", (n.secure ? "🔒 " : "") + n.signal + "%"));
        row.addEventListener("click", () => ccJoin(n));
        list.append(row);
      }
      net.append(list);
    }
    const rescan = el("button", "cc-link", "Scan again");
    rescan.addEventListener("click", async () => {
      rescan.textContent = "Scanning…";
      await post("/api/control/wifi", { action: "scan" });
      setTimeout(ccNetworks, 2500);
    });
    net.append(rescan);
  }

  async function ccJoin(n) {
    if (n.active) return;
    let password = "";
    if (n.secure) {
      const pw = await ui.ask([
        { type: "password", label: "Wi-Fi Password", placeholder: "Password for " + n.ssid },
      ]);
      if (!pw) return;
      password = pw;
    }
    const r = await post("/api/control/wifi",
                         { action: "connect", ssid: n.ssid, password });
    post("/api/notify", {
      text: r.ok ? "Connected to " + n.ssid
                 : "Could not join " + n.ssid + (r.message ? ": " + r.message : ""),
      level: r.ok ? "info" : "warn",
    });
    ccNetworks();
  }

  let ccDismiss = null;

  function ccDismissOn(e) {
    if (!ccOpen) {
      // Closed by a path that bypassed ccClose (a toggle message that raced
      // the click); take the listener down rather than leaving it to fire on
      // every pointerdown forever.
      if (ccDismiss) {
        window.removeEventListener("pointerdown", ccDismiss, true);
        ccDismiss = null;
      }
      return;
    }
    if (cc.firstChild && cc.firstChild.contains(e.target)) return;
    ccClose();
  }

  function ccEnsureDismiss() {
    if (ccDismiss) {
      window.removeEventListener("pointerdown", ccDismiss, true);
      ccDismiss = null;
    }
    ccDismiss = ccDismissOn;
    window.addEventListener("pointerdown", ccDismiss, true);
  }

  onEvent((msg) => {
    if (msg.type !== "control-centre") return;
    const want = !!(msg.data && msg.data.open);
    if (want === ccOpen) return;
    ccOpen = want;
    if (want) {
      cc.hidden = false;
      document.body.classList.add("ctx");
      if (typeof nethosHost !== "undefined") {
        nethosHost.inputRect(0, 0, window.innerWidth, window.innerHeight);
        nethosHost.show();
      }
      ccRender();
      ccEnsureDismiss();
    } else {
      ccClose();
    }
  });

  onEvent((msg) => {
    if (msg.type !== "contextmenu") return;
    const { token, x, y, items } = msg.data;
    if (!items || !items.length) return;
    ctxOpen = true;
    document.body.classList.add("ctx");
    if (typeof nethosHost !== "undefined") {
      nethosHost.inputRect(0, 0, window.innerWidth, window.innerHeight);
      nethosHost.show();
    }
    // A frame for the surface to actually map, or getBoundingClientRect
    // measures a menu in a window that has no size yet and clamps it to 8,8.
    requestAnimationFrame(() => {
      renderContextMenu(x, y, items, (i) => endCtx(token, i));
      // Dismissing without choosing still has to answer, or the asking
      // surface keeps its pending token forever and the next menu it opens
      // is ignored.
      const watch = setInterval(() => {
        if (ctxEl) return;
        clearInterval(watch);
        endCtx(token, -1);
      }, 120);
    });
  });

  /* Start unmapped, and say so to the host explicitly.
     The surface is created visible, and setOpen() only acts on a change, so
     nothing ever hid it until the launcher had been opened once. Until then a
     full-screen overlay sat above everything -- invisible, and eating every
     click aimed at the dock or a window. */
  // Mapped, always -- but click-through and invisible when idle.
  //
  // Hiding it unmaps the surface, and WebKit suspends the page of an unmapped
  // surface. A suspended page receives no events, so the overlay never heard
  // the message telling it to show itself: the control centre simply did not
  // open, and neither would a context menu after the first idle period. An
  // empty input region gives the same protection hiding did -- nothing can
  // click it -- without the page going to sleep.
  if (typeof nethosHost !== "undefined") nethosHost.inputRect(0, 0, 0, 0);

  /* The launcher surface exists for the whole session and shows/hides itself,
     which is why opening it is instant: no process start, no compositor call. */
  function setOpen(want) {
    if (want === open) return;
    open = want;
    document.body.classList.toggle("shown", open);
    if (typeof nethosHost !== "undefined") {
      if (open) {
        // Take the whole screen back before mapping; it was released to
        // nothing so the idle surface could not intercept clicks.
        nethosHost.inputRect(0, 0, window.innerWidth, window.innerHeight);
        nethosHost.show();
      } else {
        nethosHost.inputRect(0, 0, 0, 0);
      }
    }
    if (open) {
      search.value = "";
      // Show what is already loaded immediately, and refresh behind it. The
      // launcher used to await a full /api/apps round trip -- with icons --
      // before appearing, which put the whole fetch between the keypress and
      // the window. Opening is now instant and the list updates in place.
      if (apps.length) { filter(); load(); }
      else { load(); }
      // Focus the search box. The surface may have just been shown and the
      // frame clock may not have restarted yet (nethosHost.show() does not
      // guarantee a drawn frame). WebKit drives input dispatch off its
      // rendering pipeline, so a surface with no frames cannot accept focus.
      // Retry after each frame until focus lands.
      const focusSearch = () => {
        search.focus();
        if (document.activeElement !== search) requestAnimationFrame(focusSearch);
      };
      requestAnimationFrame(focusSearch);
    }
  }

  onEvent((msg) => { if (msg.type === "menu") setOpen(!!msg.data.open); });
  // Released, not unmapped: see the note above. The surface stays alive so it
  // can hear the next message; the empty input region is what stops it
  // intercepting clicks meant for the desktop.
  if (typeof nethosHost !== "undefined") nethosHost.inputRect(0, 0, 0, 0);
  load();
}

/* ---------------------------------------------------------------- desktop */

function initDesktop() {
  const host = document.getElementById("widgets");

  /* Desktop icons: whatever is in ~/Desktop.
     Read from the same /api/files the Files app uses, so there is one
     definition of what a folder contains and one place for it to be wrong. */
  const icons = document.createElement("div");
  icons.id = "desk-icons";
  document.body.append(icons);

  const GLYPH = {
    folder: "📁", image: "🖼", video: "🎞", audio: "♪", text: "📄",
    pdf: "📕", archive: "🗜", file: "📄",
  };

  async function loadIcons() {
    let r;
    try { r = await get("/api/files?path=" + encodeURIComponent("~/Desktop")); }
    catch (e) { return; }
    if (!r || r.error) return;
    icons.replaceChildren();
    for (const entry of r.entries.filter((e) => !e.hidden)) {
      const b = el("button", "desk-icon");
      b.append(el("span", "g", GLYPH[entry.kind] || "📄"));
      b.append(el("span", "n", entry.name));
      b.title = entry.name;
      b.addEventListener("dblclick", () => {
        // A folder opens in Files; anything else opens with its own
        // application. Double-click, not single: the desktop is a surface
        // people rest the pointer on.
        if (entry.dir) post("/api/launch", { id: "files" });
        else post("/api/files/open", { path: entry.path });
      });
      onContext(b, () => [
        { label: entry.dir ? "Open in Files" : "Open",
          run: () => (entry.dir ? post("/api/launch", { id: "files" })
                               : post("/api/files/open", { path: entry.path })) },
        "-",
        { label: "Move to Trash", danger: true,
          run: async () => {
            await post("/api/files/trash", { path: entry.path });
            loadIcons();
          } },
      ]);
      icons.append(b);
    }
  }

  loadIcons();
  /* Refreshed on a change signal, not a poll. nethosd's watch_files thread
     publishes a reload event with reason "files-changed" when anything in the
     served tree changes on disk, and the reload handler below repaints the
     icons immediately. The slow timer stays as a safety net for a filesystem
     change that nethosd's watcher never noticed (it polls the tree once a
     second and only tracks mtime, so a rename that preserves mtime slips
     through); a couple of seconds of staleness is fine, losing the icon until
     the next session is not. */
  setInterval(loadIcons, 20000);
  window.addEventListener("nethos-tick", () => {});


  // The desktop is the surface most likely to be right-clicked by reflex, and
  // the one where a browser menu looked most obviously wrong.
  onContext(document.body, () => [
    { label: "Open terminal", run: () => post("/api/launch", { builtin: "terminal" }) },
    { label: "Applications", run: () => post("/api/launch", { builtin: "menu-toggle" }) },
    "-",
    { label: "Settings", run: () => post("/api/launch", { id: "settings" }) },
    { label: "Reload desktop", run: () => post("/api/reload", { reason: "context menu" }) },
  ]);

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

  onEvent((msg) => {
    if (msg.type !== "reload") return;
    if (msg.data && msg.data.reason === "files-changed") loadIcons();
    load();
  });
  load();   // initial widget load
}

/* ------------------------------------------------------------ event stream */

let stream = null;
const handlers = new Set();

// Events arrive from nethos-view, not over a connection of our own.
//
// WebKit runs one network process for every surface, so the ~6 connections per
// host are shared across the whole shell -- and /api/events never returns. Four
// surfaces each holding a stream left almost nothing for actual requests, and
// every fetch after the first handful queued forever: the clock stopped, the
// widgets stopped, and button POSTs never left the browser. nethosd's log shows
// them never arriving, which is what told us the queue was on this side.
//
// nethos-view holds a single stream outside the browser and calls this. One
// connection for the whole desktop instead of one per surface.
window.nethosEvent = function (msg) {
  handlers.forEach((fn) => { try { fn(msg); } catch (e) { console.error(e); } });
};

function onEvent(handler) {
  handlers.add(handler);
  return;                       // no EventSource; see window.nethosEvent above
  /* eslint-disable no-unreachable */
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
  /* eslint-enable no-unreachable */
}

function toast(text, level) {
  let host = document.querySelector(".toasts");
  if (!host) { host = el("div", "toasts"); document.body.appendChild(host); }
  const node = el("div", "toast toast-" + (level || "info"), text);
  host.appendChild(node);
  setTimeout(() => node.remove(), 4000);
}

/* --------------------------------------------------------- context menu --
 * WebKit's own menu is suppressed in nethos-view, so every right-click that
 * should do something has to be answered here. A menu that appears only on
 * some surfaces is worse than none: the user learns the gesture does nothing.
 * So the desktop, the dock and the taskbar all answer it.
 *
 * Items are {label, run, danger, disabled} or the string "-" for a divider.
 */
let ctxEl = null;

/* The surface a menu opens on is usually larger than the part of it that
   accepts clicks -- the panel is a 360px surface showing a 54px bar, so that
   the empty space below does not swallow clicks meant for windows. A menu
   opening into that space needs the input region widened to cover it, or it
   draws correctly and cannot be clicked. Restored when the menu closes. */
let hostBaseRect = null;

function setHostRect(r) {
  hostBaseRect = r;
  if (typeof nethosHost !== "undefined" && r)
    nethosHost.inputRect(r.x, r.y, r.w, r.h);
}

function hostRectFor(menu) {
  if (typeof nethosHost === "undefined" || !hostBaseRect) return;
  const m = menu.getBoundingClientRect();
  const b = hostBaseRect;
  const x1 = Math.min(b.x, m.left), y1 = Math.min(b.y, m.top);
  const x2 = Math.max(b.x + b.w, m.right), y2 = Math.max(b.y + b.h, m.bottom);
  nethosHost.inputRect(Math.floor(x1), Math.floor(y1),
                       Math.ceil(x2 - x1), Math.ceil(y2 - y1));
}

let ctxDismiss = null;

function closeContextMenu() {
  if (ctxDismiss) {
    window.removeEventListener("pointerdown", ctxDismiss, true);
    window.removeEventListener("keydown", ctxDismiss, true);
    ctxDismiss = null;
  }
  if (!ctxEl) return;
  ctxEl.remove();
  ctxEl = null;
  if (hostBaseRect) setHostRect(hostBaseRect);
}

/* Draw a menu into this surface. `choose` is called with the item index.
   Used by the overlay; every other surface goes through contextMenu(). */
function renderContextMenu(x, y, items, choose) {
  closeContextMenu();
  const menu = el("div", "ctxmenu glass");
  items.forEach((item, i) => {
    if (item === "-" || item.sep) { menu.append(el("div", "ctxsep")); return; }
    const b = el("button", "ctxitem" + (item.danger ? " danger" : ""));
    b.textContent = item.label;
    if (item.disabled) b.disabled = true;
    else b.addEventListener("click", () => { closeContextMenu(); choose(i); });
    menu.append(b);
  });
  document.body.append(menu);

  // Placed after insertion so the real size is known: a menu near the right
  // or bottom edge has to open back towards the middle rather than off-screen.
  const r = menu.getBoundingClientRect();
  const px = Math.min(x, window.innerWidth - r.width - 8);
  const py = Math.min(y, window.innerHeight - r.height - 8);
  menu.style.left = Math.max(8, px) + "px";
  menu.style.top = Math.max(8, py) + "px";
  ctxEl = menu;
  hostRectFor(menu);

  // Dismissal must ignore presses that land inside the menu.
  //
  // This was `{ once: true, capture: true }` on pointerdown, unconditionally
  // closing. Pressing an item therefore removed the menu from the document on
  // pointerdown, and `click` -- which needs press and release on the same
  // element -- never fired, because that element was gone by the time the
  // button came up. The menu opened, looked right, highlighted on hover, and
  // did nothing at all when chosen.
  ctxDismiss = (e) => {
    if (e.type === "keydown") {
      if (e.key === "Escape") closeContextMenu();
      return;
    }
    if (ctxEl && ctxEl.contains(e.target)) return;   // let the item run
    closeContextMenu();
  };
  setTimeout(() => {
    if (!ctxDismiss) return;
    window.addEventListener("pointerdown", ctxDismiss, true);
    window.addEventListener("keydown", ctxDismiss, true);
  }, 0);
  return menu;
}

/* Ask for a menu. The overlay draws it; this surface keeps the callbacks and
   runs the one that comes back. Items are sent as plain data, so anything the
   menu needs to do has to live in `run` here, not in the overlay. */
let ctxPending = null;

function contextMenu(x, y, items) {
  if (document.body.dataset.view === "menu") {
    // The overlay itself: no round trip, it is already the right surface.
    renderContextMenu(x, y, items, (i) => items[i] && items[i].run());
    return;
  }
  const token = String(Date.now()) + ":" + Math.random().toString(36).slice(2, 8);
  ctxPending = { token, items };
  post("/api/contextmenu", {
    token, x, y,
    items: items.map((it) => (it === "-" ? { sep: true } : {
      label: it.label, danger: !!it.danger, disabled: !!it.disabled,
    })),
  });
}

/* Every surface listens: only the one holding the matching token acts. */
onEvent((msg) => {
  if (msg.type !== "contextmenu-choice" || !ctxPending) return;
  const { token, items } = ctxPending;
  if (msg.data.token !== token) return;
  ctxPending = null;
  const item = items[msg.data.index];
  if (item && item !== "-" && typeof item.run === "function") item.run();
});

/* Attach a menu builder to an element. The builder returns the item list, so
   it is evaluated at click time and can reflect current state. */
function onContext(node, build) {
  node.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const items = build();
    if (items && items.length) contextMenu(e.clientX, e.clientY, items);
  });
}

/* Menu for one window, used by both the taskbar and the dock. */
function windowMenuItems(w) {
  return [
    { label: "Focus", run: () => post("/api/window", { action: "focus", id: w.id }) },
    { label: "Minimise", run: () => post("/api/window", { action: "minimize", id: w.id }) },
    { label: "Maximise", run: () => post("/api/window", { action: "maximize", id: w.id }) },
    "-",
    { label: "Close", danger: true,
      run: () => post("/api/window", { action: "close", id: w.id }) },
  ];
}

/* ------------------------------------------------------------- settings --
 * Every surface applies the same stored settings, so the theme, accent and
 * text size are one answer rather than four. Applied before the view starts
 * drawing: setting them afterwards means the panel is visibly repainted a
 * moment after it appears, which reads as a glitch rather than as a theme.
 */
function applySettings(s) {
  if (!s) return;
  const root = document.documentElement;
  // "auto" is resolved here rather than left to the stylesheet, because the
  // stylesheet's media query cannot see a stored preference of "dark" on a
  // host that reports light.
  const dark = s.theme === "dark" ||
    (s.theme === "auto" &&
     window.matchMedia("(prefers-color-scheme: dark)").matches);
  root.setAttribute("data-theme", dark ? "dark" : "light");
  if (s.accent) root.style.setProperty("--accent", s.accent);
  if (s.font_scale) root.style.fontSize = s.font_scale + "%";
  root.classList.toggle("no-motion", s.animations === false);
  // The desktop surface paints the wallpaper; the others are transparent and
  // let the compositor blur it. Setting it on body rather than root because
  // the selectors key off body[data-view="desktop"].
  if (s.wallpaper) document.body.dataset.wallpaper = s.wallpaper;
  if (typeof s.dock_size === "number")
    root.style.setProperty("--dock-icon", s.dock_size + "px");
  document.body.dataset.autohide = s.dock_autohide === false ? "0" : "1";
  document.body.dataset.seconds = s.panel_clock_seconds ? "1" : "0";
}

async function loadSettings() {
  try {
    const r = await get("/api/settings");
    applySettings(r.settings);
    return r.settings;
  } catch (e) { return null; }
}

document.addEventListener("DOMContentLoaded", async () => {
  const view = document.body.dataset.view;
  await loadSettings();
  onEvent((msg) => { if (msg.type === "settings") applySettings(msg.data); });
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
