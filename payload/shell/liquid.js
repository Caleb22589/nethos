/* NETHOS — the liquid metal panel surface.
 *
 * The panel's glass is a CSS surface; this replaces it with a raymarched
 * chrome bar drawn by lib/liquid-metal.js, with the panel's own contents
 * running through the middle of it.
 *
 * Three things decide whether it runs at all, and the answer is allowed to be
 * no on every one of them:
 *
 *   - the `panel_liquid` setting,
 *   - WebGL2 being available (nethos-view only enables it for surfaces that
 *     ask, and gives them their own web process when they do),
 *   - the GL renderer not being a software rasteriser. Under QEMU without
 *     virtio-gpu, or on a machine whose driver never loaded, WebKit falls back
 *     to llvmpipe, and a raymarcher on llvmpipe across a 1920px bar is not a
 *     desktop panel, it is a slideshow.
 *
 * When any of those says no the CSS glass is left exactly as it was. Nothing
 * here is load-bearing: the panel works without it.
 */

import { PRESETS, resolve } from "/lib/liquid-presets.js";

const SOFTWARE = /llvmpipe|softpipe|swiftshader|software|swrast/i;

/* panel_quality -> the renderer's own quality tier + supersample. "low" is
   the bar's long-standing hardcoded default (one bounce, no supersampling
   beyond the shader's own edge smoothing, tuned for the oldest machine this
   runs on); this only widens what a newer GPU can opt into -- baked into the
   WebGL context at construction, so changing it needs the shell rebuilt, not
   just re-applied, which is why this is read once here rather than through
   applyLook() with everything else. */
const QUALITY_TIERS = {
  low:    { quality: "low",    supersample: 1 },
  medium: { quality: "medium", supersample: 1.5 },
  high:   { quality: "high",   supersample: 2 },
};
function qualityFor(settings) {
  return QUALITY_TIERS[(settings || {}).panel_quality] || QUALITY_TIERS.low;
}

/* The look, shared by both surfaces and rebuilt whenever settings change.
   `swell` and `pad` live here rather than as constants because they are the
   two things a user is most likely to want turned down. */
const look = {
  preset: null, thickness: 0.78, swell: 2.5, pad: 16,
  dock: true, paneAlpha: 0.34,
};

/* Settings -> one coherent look. The preset supplies the environment, the
   conductor and the ink together; the sliders scale what it chose rather than
   replacing it, so no combination of them can produce type that cannot be
   read on its own bar. */
function applyLook(lm, s) {
  s = s || {};
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  const name = resolve(s.liquid_preset, dark);
  const p = PRESETS[name];
  look.preset = name;
  look.thickness = p.thickness;
  look.swell = typeof s.liquid_swell === "number" ? s.liquid_swell : 2.5;
  look.dock = s.liquid_dock !== false;
  look.paneAlpha = (typeof s.liquid_pane === "number" ? s.liquid_pane : 34) / 100;

  const height = typeof s.liquid_height === "number" ? s.liquid_height : 62;
  look.pad = Math.max(2, (height - 30) / 2);      // the panel box is 30px tall

  const root = document.documentElement.style;
  root.setProperty("--metal-pad", look.pad + "px");
  const [r, g, b] = p.pane;
  root.setProperty("--metal-pane", `rgba(${r},${g},${b},${look.paneAlpha.toFixed(3)})`);
  /* The dock's pane needs a heavier alpha to land on the same colour: it sits
     over the bright band of the reflection where the panel's box sits over the
     dark core. 2.15 is the ratio measured off the screen at the default. */
  root.setProperty("--metal-pane-dock",
    `rgba(${r},${g},${b},${Math.min(0.95, look.paneAlpha * 2.15).toFixed(3)})`);
  root.setProperty("--metal-type", p.type);
  root.setProperty("--metal-ink", p.ink);

  if (lm) {
    lm.setEnvironment(p.env);
    const m = p.material;
    lm.opts.tint = m.tint;
    lm.opts.roughness = m.roughness;
    lm.opts.exposure = m.exposure * ((s.liquid_exposure || 100) / 100);
    lm.opts.contrast = m.contrast * ((s.liquid_contrast || 100) / 100);
  }
}

/* How long a pointer may be silent before the bar treats it as gone.
 *
 * A layer-shell surface stops receiving pointer events the moment the cursor
 * leaves its input region, and no leave event is delivered -- the same thing
 * that makes :hover stick on the dock (see shell.js's clearHover). Waiting for
 * pointerleave meant the swell never retracted, which also meant the settle
 * check below never came true and the render loop ran at 60fps for the rest of
 * the session. That is the whole cost of this constant. */
const POINTER_TTL = 0.2;

/* An old iGPU is the target, so frames are capped rather than free-running.
 * At 30 the swell still reads as liquid and costs half the GPU of 60. */
const MIN_FRAME = 1 / 30;

/* How far the bar swells under the pointer, and how fast it lets go.
 *
 * This is a panel, not a demo. The swell should be noticeable when looked for
 * and invisible when working, so the amplitude stays small. Release used to
 * be faster than the grab (0.42 against 0.22) on the theory that a bulge
 * lingering after the cursor moved on would read as the interface lagging
 * behind the mouse -- in practice that made the release read as the metal
 * simply vanishing rather than a liquid settling back to rest, which is its
 * own tell. Slower than the grab now, closer to how the material is
 * supposed to behave: forming a bulge is effortful (the grab), the metal
 * relaxing back to flat afterward should look effortless rather than cut. */
const SWELL = 2.5;
const GRAB = 0.22, LET_GO = 0.10;

function usable() {
  /* Escape hatch. scripts/run.sh on macOS picks the cocoa display backend,
     which cannot hand a GL context to the guest, so the VM renders through
     llvmpipe and the check below correctly refuses. Set it from the inspector
     (NETHOS_INSPECTOR=1) and reload:

         localStorage.nethosLiquidForce = "1"

     Expect single-digit frame rates; this is for seeing it, not using it. */
  let forced = false;
  try { forced = localStorage.getItem("nethosLiquidForce") === "1"; } catch (e) {}

  const c = document.createElement("canvas");
  const gl = c.getContext("webgl2",
    { alpha: true, failIfMajorPerformanceCaveat: !forced });
  if (!gl) return false;
  if (forced) { console.log("liquid: forced on"); return true; }
  const dbg = gl.getExtension("WEBGL_debug_renderer_info");
  const name = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)
                   : gl.getParameter(gl.RENDERER);
  const ok = !SOFTWARE.test(String(name || ""));
  const lose = gl.getExtension("WEBGL_lose_context");
  if (lose) lose.loseContext();
  if (!ok) console.log("liquid: software renderer (" + name + "), keeping glass");
  return ok;
}

/* The renderer string cannot be trusted on its own. WebKit masks it: on this
   hardware -- Intel HD 4400, Mesa i915 -- it reports "Apple GPU", so the check
   above can never match llvmpipe however software the rasteriser really is.
   Timing actual frames is the honest question anyway: what matters is whether
   this machine can draw the bar, not what it calls its GPU. */
const TOO_SLOW = 26;   // ms/frame; beyond this a swell would visibly stutter

function benchmark(lm, shape, t) {
  const N = 8;
  lm.setShapes([shape]); lm.render(t);          // warm: compile, upload
  lm.gl.finish();
  const t0 = performance.now();
  for (let i = 0; i < N; i++) { lm.setShapes([shape]); lm.render(t); }
  lm.gl.finish();
  return (performance.now() - t0) / N;
}

const ease = (a, b, k) => a + (b - a) * k;
const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const px = (name, fallback) =>
  parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || fallback;

/* The dock's surround. Same renderer, same gates; the shape is derived from
   the dock pill rather than the panel box, and it wraps the pill instead of
   running through it.

   A pill only contains a rectangle along its straight section, which starts
   one radius in from each end -- so the horizontal padding is the radius
   itself. Padding all four sides equally would leave the dock's corners
   hanging out of the rounded ends. */
export async function startDockMetal(settings) {
  if (!usable()) return false;
  if (settings && settings.liquid_dock === false) return false;
  let LiquidMetal;
  try { ({ LiquidMetal } = await import("/lib/liquid-metal.js")); }
  catch (e) { console.log("liquid: " + e.message); return false; }

  const dock = document.getElementById("dock");
  if (!dock) return false;
  document.body.classList.add("metal");

  const canvas = document.createElement("canvas");
  canvas.id = "metal";
  document.body.insertBefore(canvas, document.body.firstChild);

  const lm = new LiquidMetal(canvas, {
    ...qualityFor(settings), fov: 6, background: [0, 0, 0, 0],
  });
  applyLook(lm, settings);
  document.addEventListener("nethos:settings", (e) => {
    applyLook(lm, e.detail);
    if (!look.dock) { canvas.style.display = "none"; return; }
    canvas.style.display = "";
    measure(); wake();
  });

  let L = null;
  function measure() {
    const b = dock.getBoundingClientRect();
    const vpad = px("--metal-dock-pad", 10);
    const rad = b.height / 2 + vpad;
    L = {
      rad, cy: b.top + b.height / 2,
      x0: b.left - rad + rad, x1: b.right + rad - rad,   // straight section
      top: b.top - vpad, bottom: b.bottom + vpad,
      items: [...dock.querySelectorAll(".dock-item")].map((el) => {
        const r = el.getBoundingClientRect();
        return { el, cx: r.left + r.width / 2 };
      }),
    };
    return L;
  }
  const fit = () => { measure(); lm.resize(window.innerWidth, window.innerHeight); };
  fit();
  addEventListener("resize", () => { fit(); wake(); });
  // the dock slides, hides and regrows its icons; all of that moves the pill
  new MutationObserver(() => { measure(); wake(); })
    .observe(document.body, { attributes: true, attributeFilter: ["class"] });
  new MutationObserver(() => { measure(); wake(); })
    .observe(dock, { childList: true, subtree: true, attributes: true });

  const P = { x: -1e4, y: -1e4, seen: -1e9 };
  const hover = new WeakMap();
  const swell = { x: 0, r: 0 };
  let now = 0;
  addEventListener("pointermove", (e) => {
    P.x = e.clientX; P.y = e.clientY; P.seen = now; wake();
  }, true);

  function build() {
    const g = L;
    if (!g || g.x1 <= g.x0) return null;
    const paths = [[[g.x0, g.cy, g.rad], [g.x1, g.cy, g.rad]]];
    const live = now - P.seen < POINTER_TTL;
    const want = live && P.y > g.top - 40 && P.y < g.bottom + 40
      ? Math.max(0, 1 - Math.abs(P.y - g.cy) / (60 + g.rad)) : 0;
    if (live) swell.x = ease(swell.x, clamp(P.x, g.x0, g.x1), 0.26);
    const tgt = want * look.swell;
    swell.r = ease(swell.r, tgt, tgt > swell.r ? GRAB : LET_GO);
    // Matches still()'s own 0.02 threshold below, not a separate, larger
    // one: the shape used to stop being drawn at all once swell.r fell
    // under 0.15, which on a 0-2.5-ish range is not "gone", it is a visible
    // remaining bulge that then vanished in one frame instead of finishing
    // its fade -- the "smoothly retracts, then suddenly snaps" the easing
    // fix alone did not explain, because the easing itself was never the
    // problem past this point.
    if (swell.r > 0.02) {
      const r = g.rad + swell.r;
      paths.push([[swell.x - 6, g.cy, r], [swell.x + 6, g.cy, r]]);
    }
    for (const it of g.items) {
      if (!hover.has(it.el)) hover.set(it.el, { v: 0 });
      const s = hover.get(it.el);
      const on = live && Math.abs(P.x - it.cx) < 26 && P.y > g.top && P.y < g.bottom;
      s.v = ease(s.v, on ? 1 : 0, on ? GRAB : LET_GO);
      if (s.v > 0.02) {
        const lift = s.v * 3;
        paths.push([[it.cx - 6, g.cy, g.rad + lift], [it.cx + 6, g.cy, g.rad + lift]]);
      }
    }
    return { paths, thickness: look.thickness, blend: 0.75 };
  }

  let raf = 0, lastDraw = -1e9, idle = 0;
  const still = () => {
    if (swell.r > 0.02) return false;
    for (const it of (L ? L.items : [])) {
      const s = hover.get(it.el);
      if (s && s.v > 0.02) return false;
    }
    return !(now - P.seen < POINTER_TTL &&
             Math.abs(swell.x - clamp(P.x, 0, window.innerWidth)) > 0.5);
  };
  function frame(ms) {
    now = ms / 1000;
    if (now - lastDraw >= MIN_FRAME) {
      const sh = build();
      if (sh) { lm.setShapes([sh]); lm.render(now); }
      lastDraw = now;
    }
    if (still()) { if (++idle > 3) { raf = 0; return; } } else idle = 0;
    raf = requestAnimationFrame(frame);
  }
  function wake() { idle = 0; if (!raf) raf = requestAnimationFrame(frame); }

  const ms = benchmark(lm, build(), 0);
  if (ms > TOO_SLOW) {
    canvas.remove();
    document.body.classList.remove("metal");
    return false;
  }
  fetch("/api/log", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ surface: "dock", kind: "liquid",
      message: `dock metal up: ${ms.toFixed(2)}ms/frame` }) }).catch(() => {});
  wake();
  return true;
}

/* Returns the height of the bar in CSS pixels so the caller can size the
   input region and the exclusive zone, or 0 when the metal did not start. */
export async function startPanelMetal(settings) {
  if (!usable()) return 0;

  let LiquidMetal;
  try {
    ({ LiquidMetal } = await import("/lib/liquid-metal.js"));
  } catch (e) {
    console.log("liquid: " + e.message);
    return 0;
  }

  const panel = document.getElementById("panel");
  const brand = document.getElementById("brand");
  if (!panel) return 0;

  document.body.classList.add("metal");

  const canvas = document.createElement("canvas");
  canvas.id = "metal";
  document.body.insertBefore(canvas, document.body.firstChild);

  /* Defaults to quality "low": one bounce, no supersampling beyond a touch
     of edge smoothing. The bar is a plain capsule with nothing concave in
     it, so the second bounce buys almost no detail and costs a whole extra
     march -- a reasonable default for the oldest machine this is expected
     to run on, not the newest, which is exactly why panel_quality exists as
     a setting rather than this staying a fixed choice: a stair-stepped edge
     that reads as "low quality" on real hardware to someone who is not the
     oldest machine it was tuned for should be a choice, not a ceiling.

     A long lens rather than a wide one: the canvas is a short strip, so its
     aspect ratio is extreme, and an 18-degree vertical field becomes a
     70-degree horizontal one that visibly bends the reflection at both ends. */
  const lm = new LiquidMetal(canvas, {
    ...qualityFor(settings),
    fov: 4.5,
    background: [0, 0, 0, 0],
  });
  applyLook(lm, settings);
  // Live: the panel repaints as a slider moves rather than on next login.
  document.addEventListener("nethos:settings", (e) => {
    applyLook(lm, e.detail);
    fit(); wake();
  });

  // --- layout, measured rarely ------------------------------------------------

  /* Geometry comes from the panel's own layout box rather than a second copy
     of the numbers. It is cached: getBoundingClientRect forces layout, and
     calling it for the panel and every task on every frame was enough to make
     the pointer itself feel late. Refreshed when something can actually have
     moved. */
  let L = null;

  function measure() {
    const b = panel.getBoundingClientRect();
    const pad = look.pad;
    const inset = px("--metal-inset", 2);
    const top = b.top - pad, bottom = b.bottom + pad;
    const rad = (bottom - top) / 2;
    L = {
      top, bottom, rad,
      cy: top + rad,
      x0: inset + rad,
      x1: window.innerWidth - inset - rad,
      band: Math.ceil(bottom) + 20,
      tasks: [...document.querySelectorAll("#panel .task")].map((el) => {
        const r = el.getBoundingClientRect();
        return { el, left: r.left, right: r.right };
      }),
      mark: brand ? (() => {
        const r = brand.getBoundingClientRect();
        return { x: r.left + r.width / 2 };
      })() : null,
    };
    return L;
  }

  /* The canvas covers only the bar's band, not the whole 360px surface. The
     surface is tall so menus have somewhere to open; compositing and clearing
     a 1366x360 GL buffer every frame to draw a 60px bar was most of what the
     GPU was being asked to do. */
  function fit() {
    measure();
    lm.resize(window.innerWidth, L.band);
    canvas.style.height = L.band + "px";
  }
  fit();

  addEventListener("resize", () => { fit(); wake(); });
  // tasks come and go as windows open; their rects are cached, so re-measure
  const tasksEl = document.getElementById("tasks");
  if (tasksEl) {
    new MutationObserver(() => { measure(); wake(); })
      .observe(tasksEl, { childList: true, subtree: true });
  }

  // --- interaction state ------------------------------------------------------

  const P = { x: -1e4, y: -1e4, seen: -1e9 };
  const swell = { x: 0, r: 0 };
  const hover = new WeakMap();
  let clickAge = 1e3, clickX = 0, flash = 0, markPull = 0;
  let now = 0;

  addEventListener("pointermove", (e) => {
    P.x = e.clientX; P.y = e.clientY; P.seen = now;
    wake();
  }, true);
  addEventListener("pointerdown", (e) => {
    if (!L) return;
    if (e.clientY > L.bottom + 20 || e.clientY < L.top - 20) return;
    clickAge = 0; clickX = e.clientX; flash = 1; wake();
  }, true);

  const hot = (el) => {
    if (!hover.has(el)) hover.set(el, { v: 0 });
    return hover.get(el);
  };
  const over = (el, r) =>
    now - P.seen < POINTER_TTL &&
    P.x >= r.left && P.x <= r.right && P.y >= L.top && P.y <= L.bottom;

  // --- the scene --------------------------------------------------------------

  function build(dt) {
    const g = L;
    if (!g || g.x1 <= g.x0) return null;

    // one capsule: uniform, pill-ended, and exact -- a lone primitive never
    // enters the smooth-union, so it is not inflated by the blend
    const paths = [[[g.x0, g.cy, g.rad], [g.x1, g.cy, g.rad]]];

    // A pointer that has gone quiet is a pointer that has left. See POINTER_TTL.
    const live = now - P.seen < POINTER_TTL;
    const want = live
      ? Math.max(0, 1 - Math.abs(P.y - g.cy) / (80 + g.rad))
      : 0;
    const target = want * look.swell;
    if (live) swell.x = ease(swell.x, clamp(P.x, g.x0, g.x1), 0.3);
    swell.r = ease(swell.r, target, target > swell.r ? GRAB : LET_GO);
    // See the dock's own version of this same fix, just below in this file:
    // 0.02, matching still()'s own cutoff, not a larger threshold that cut
    // the shape from the render while it was still visibly mid-fade.
    if (swell.r > 0.02) {
      const r = g.rad + swell.r;
      paths.push([[swell.x - 6, g.cy, r], [swell.x + 6, g.cy, r]]);
    }

    clickAge += dt;
    if (clickAge < 0.6) {
      const ring = Math.abs(Math.sin(clickAge * 30) * Math.exp(-clickAge * 8) * 4);
      const cx = clamp(clickX, g.x0, g.x1);
      paths.push([[cx - 4, g.cy, g.rad + ring], [cx + 4, g.cy, g.rad + ring]]);
    }
    flash = Math.max(0, flash - dt * 2.4);
    lm.env.lights[1].intensity = 6 + flash * 10;

    // a pool under a hovered task. The focused task gets no bulge of its own:
    // a permanent swell reads as a defect in the bar rather than as a state.
    for (const t of g.tasks) {
      const s = hot(t.el);
      const on = over(t.el, t);
      s.v = ease(s.v, on ? 1 : 0, on ? GRAB : LET_GO);
      if (s.v > 0.02) {
        const lift = s.v * 1.5;
        paths.push([[t.left + 10, g.cy, g.rad + lift], [t.right - 10, g.cy, g.rad + lift]]);
      }
    }

    if (g.mark) {
      const on = live && Math.abs(P.x - g.mark.x) < 16 && P.y < g.bottom;
      markPull = ease(markPull, on ? 1 : 0, on ? GRAB : LET_GO);
      if (markPull > 0.02) {
        paths.push([[g.mark.x, g.cy, g.rad * 0.92],
                    [g.mark.x, g.cy + g.rad * 0.6 + markPull * 5, g.rad * 0.34]]);
      }
    }

    return { paths, thickness: look.thickness, blend: 0.75 };
  }

  // --- on-demand loop ---------------------------------------------------------

  let raf = 0, prev = 0, lastDraw = -1e9, idle = 0;

  function settled() {
    if (clickAge < 0.65 || flash > 0.01) return false;
    if (swell.r > 0.02 || markPull > 0.02) return false;
    for (const t of (L ? L.tasks : [])) {
      const s = hover.get(t.el);
      if (s && s.v > 0.02) return false;
    }
    // a live pointer that has not reached its target yet is still animating
    if (now - P.seen < POINTER_TTL &&
        Math.abs(swell.x - clamp(P.x, 0, window.innerWidth)) > 0.5) return false;
    return true;
  }

  function frame(ms) {
    now = ms / 1000;
    const dt = Math.min(prev ? now - prev : 0.016, 0.05);
    prev = now;

    if (now - lastDraw >= MIN_FRAME) {
      const shape = build(dt);
      if (shape) { lm.setShapes([shape]); lm.render(now); }
      lastDraw = now;
    }

    // Park once nothing is moving. Without this the loop is a 60fps shader
    // running for the life of the session.
    if (settled()) {
      if (++idle > 3) { raf = 0; return; }
    } else idle = 0;
    raf = requestAnimationFrame(frame);
  }

  function wake() {
    idle = 0;
    if (!raf) { prev = 0; raf = requestAnimationFrame(frame); }
  }

  /* Measure before committing. If this machine cannot draw the bar fast
     enough, take the canvas back out and let the glass panel stand -- a
     stuttering shell surface is worse than a plain one. */
  const ms = benchmark(lm, build(0.016), now);
  const report = (msg) => fetch("/api/log", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ surface: "panel", kind: "liquid", message: msg }),
  }).catch(() => {});

  if (ms > TOO_SLOW) {
    report(`too slow: ${ms.toFixed(1)}ms/frame, keeping glass`);
    canvas.remove();
    document.body.classList.remove("metal");
    return 0;
  }
  report(`metal up: ${ms.toFixed(2)}ms/frame ` +
         `buffer=${canvas.width}x${canvas.height} band=${L.band}px`);

  wake();
  return Math.ceil(L.bottom) + 4;
}
