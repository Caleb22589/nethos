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
 *     ask, so this is off everywhere else in the shell),
 *   - the GL renderer not being a software rasteriser. Under QEMU without
 *     virtio-gpu, or on a machine whose driver never loaded, WebKit falls back
 *     to llvmpipe, and a raymarcher on llvmpipe across a 1920px bar is not a
 *     desktop panel, it is a slideshow.
 *
 * When any of those says no the CSS glass is left exactly as it was. Nothing
 * here is load-bearing: the panel works without it.
 *
 * Frames are drawn on demand. A shell surface that renders a shader at 60fps
 * for its own amusement is a battery bug, so the loop runs only while
 * something is still moving and stops as soon as everything has settled.
 */

const SOFTWARE = /llvmpipe|softpipe|swiftshader|software|swrast/i;

function usable() {
  /* Escape hatch. scripts/run.sh on macOS picks the cocoa display backend,
     which cannot hand a GL context to the guest, so the VM renders through
     llvmpipe and the check below correctly refuses. That is the right default
     and the wrong thing when you are trying to look at the bar. Set it from
     the inspector (NETHOS_INSPECTOR=1) and reload:

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

const ease = (a, b, k) => a + (b - a) * k;
const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const px = (name, fallback) =>
  parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || fallback;

/* Returns the height of the bar in CSS pixels so the caller can size the
   input region and the exclusive zone, or 0 when the metal did not start. */
export async function startPanelMetal() {
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

  const lm = new LiquidMetal(canvas, { quality: "medium", background: [0, 0, 0, 0] });

  const fit = () => lm.resize(window.innerWidth, window.innerHeight);
  fit();

  /* Geometry. The metal is derived from the panel's own layout box rather
     than from a second copy of the numbers, so moving the panel in CSS moves
     the metal with it. */
  function housing() {
    const b = panel.getBoundingClientRect();
    const pad = px("--metal-pad", 12);
    const inset = px("--metal-inset", 2);
    return {
      left: inset, right: window.innerWidth - inset,
      top: b.top - pad, bottom: b.bottom + pad,
      height: b.height + pad * 2,
    };
  }

  // --- interaction state ----------------------------------------------------

  const P = { x: -1e4, y: -1e4, inside: false };
  const swell = { x: 0, r: 0 };
  const wake = [{ x: 0, r: 0 }, { x: 0, r: 0 }, { x: 0, r: 0 }];
  let clickAge = 1e3, clickX = 0, flash = 0, markPull = 0;

  addEventListener("pointermove", (e) => {
    P.x = e.clientX; P.y = e.clientY; P.inside = true; wake_up();
  });
  addEventListener("pointerleave", () => { P.inside = false; wake_up(); });
  addEventListener("pointerdown", (e) => {
    const h = housing();
    if (e.clientY > h.bottom + 20 || e.clientY < h.top - 20) return;
    clickAge = 0; clickX = e.clientX; flash = 1; wake_up();
  });
  addEventListener("resize", () => { fit(); wake_up(); });

  // hover state for the task buttons, which the shell rebuilds as windows open
  const hovers = new WeakMap();
  const track = (el) => {
    if (hovers.has(el)) return;
    hovers.set(el, { hover: 0 });
    el.addEventListener("pointerenter", () => { el._hot = true; wake_up(); });
    el.addEventListener("pointerleave", () => { el._hot = false; wake_up(); });
  };
  if (brand) track(brand);

  // --- the scene ------------------------------------------------------------

  function build(dt) {
    const h = housing();
    const rad = h.height / 2;
    const cy = h.top + rad;
    const x0 = h.left + rad, x1 = h.right - rad;
    if (x1 <= x0) return null;

    // one capsule: uniform, pill-ended, and exact -- a lone primitive never
    // enters the smooth-union, so it is not inflated by the blend
    const paths = [[[x0, cy, rad], [x1, cy, rad]]];

    const near = P.inside && P.y > h.top - 80 && P.y < h.bottom + 80;
    const want = near ? Math.max(0, 1 - Math.abs(P.y - cy) / (80 + rad)) : 0;
    if (P.inside) swell.x = ease(swell.x, clamp(P.x, x0, x1), 0.26);
    swell.r = ease(swell.r, want * 5, 0.16);
    if (swell.r > 0.15) {
      const r = rad + swell.r;
      paths.push([[swell.x - 6, cy, r], [swell.x + 6, cy, r]]);
    }

    let lead = swell.x;
    wake.forEach((w, i) => {
      w.x = ease(w.x, lead, 0.14 - i * 0.03);
      w.r = ease(w.r, swell.r * (0.62 - i * 0.18), 0.12);
      lead = w.x;
      if (w.r > 0.15) paths.push([[w.x, cy, rad + w.r], [w.x + 5, cy, rad + w.r]]);
    });

    clickAge += dt;
    if (clickAge < 1.1) {
      const ring = Math.abs(Math.sin(clickAge * 26) * Math.exp(-clickAge * 5.5) * 7);
      const cx = clamp(clickX, x0, x1);
      paths.push([[cx - 4, cy, rad + ring], [cx + 4, cy, rad + ring]]);
    }
    flash = Math.max(0, flash - dt * 2.4);
    lm.env.lights[1].intensity = 6 + flash * 10;

    // a pool under a hovered task. The focused task gets no bulge of its own:
    // a permanent swell reads as a defect in the bar rather than as a state.
    document.querySelectorAll("#panel .task").forEach((el) => {
      track(el);
      const s = hovers.get(el);
      s.hover = ease(s.hover, el._hot ? 1 : 0, 0.18);
      if (s.hover > 0.05) {
        const b = el.getBoundingClientRect();
        const lift = s.hover * 2;
        paths.push([[b.left + 10, cy, rad + lift], [b.right - 10, cy, rad + lift]]);
      }
    });

    if (brand) {
      markPull = ease(markPull, brand._hot ? 1 : 0, 0.16);
      if (markPull > 0.02) {
        const b = brand.getBoundingClientRect();
        const mx = b.left + b.width / 2;
        paths.push([[mx, cy, rad * 0.9],
                    [mx, cy + rad * 0.55 + markPull * 10, rad * 0.4]]);
      }
    }

    return { paths, thickness: 0.78, blend: 0.75 };
  }

  // --- on-demand loop -------------------------------------------------------

  let raf = 0, prev = 0, idle = 0;

  function settled() {
    // still moving? keep drawing. Everything eases towards a target, so "no
    // movement" is the honest end condition rather than a fixed timeout.
    if (clickAge < 1.15 || flash > 0.01) return false;
    if (swell.r > 0.02 || markPull > 0.02) return false;
    if (wake.some((w) => w.r > 0.02)) return false;
    if (P.inside && Math.abs(swell.x - clamp(P.x, 0, window.innerWidth)) > 0.5) return false;
    return true;
  }

  function frame(ms) {
    const t = ms / 1000;
    const dt = Math.min(prev ? t - prev : 0.016, 0.05);
    prev = t;
    const shape = build(dt);
    if (shape) {
      lm.setShapes([shape]);
      lm.render(t);
    }
    if (settled() && ++idle > 4) { raf = 0; return; }
    if (!settled()) idle = 0;
    raf = requestAnimationFrame(frame);
  }

  function wake_up() {
    idle = 0;
    if (!raf) { prev = 0; raf = requestAnimationFrame(frame); }
  }

  wake_up();
  return Math.ceil(housing().bottom) + 4;
}
