# Rewriting `nethos-view` in native C against WPE WebKit

Written while the rewrite is still in its spike phase, so the reasoning behind it survives even if
the attempt stalls. Read this before touching `payload/bin/nethos-view` in the direction of a
native rewrite -- it records what was already tried, measured, and ruled out.

## Why

`nethos-view` (Python 3, GTK4, WebKitGTK, `payload/bin/nethos-view`) hosts every WebKit surface on
NETHOS -- the shell (panel/dock/desktop/menu/splash, one process, five layer-shell surfaces sharing
a WebProcess) and every app window (opened on demand as a related WebView of that same process via
the `--apphost` Unix-socket protocol). It sits on the boot-time critical path: the shell is the
first thing a user waits for.

Measured on the real laptop (i7-4600U Haswell, 2 cores/4 threads), boot to a usable desktop is
~40 seconds not counting BIOS/POST. Two costs inside that number are structurally tied to
CPython + GTK4 + WebKitGTK and cannot be trimmed further without leaving that stack:

- **Python/GTK4/WebKit import cost**: ~2.8s idle, measured up to ~8s under real boot-time CPU
  contention (`python3 -X importtime` plus `/proc/PID/stat`-based process-start timing -- SSH
  polling is too flaky under boot load to trust for this).
- **Engine cold start**: a minimal WebKitGTK embedder reaches `WEBKIT_LOAD_FINISHED` in 5.471s;
  a minimal WPE WebKit embedder loading the identical page reaches it in 3.193s -- both timed with
  `CLOCK_MONOTONIC` from `main()`, both against the real EGL/DRM path, no software rendering. ~42%
  faster.

WPE has no GObject-Introspection bindings, so using it forces dropping Python entirely -- which
also removes the ~8s import tax. Combined estimate: **~8-11 seconds off the current ~40s boot**, a
20-27% reduction. This is the best remaining lever after firmware timing, `nethos-growroot`'s
6.267s/boot bug, and `nethos-memory`'s subprocess overhead were already fixed this session -- see
the commit history around `9774937`/`ca230eb`/`d3b9d6d`.

## Scope: Wayfire only

`nethos-view` currently hand-draws its own window chrome (`_own_chrome` in `Surface.__init__`,
rounded corners, draggable titlebar, three traffic-light buttons, all GTK widgets plus a CSS
provider) but *only* when the compositor doesn't decorate windows itself -- i.e. only under sway.
Under Wayfire, `reform-firedecor` already decorates every window, including foreign ones, with
matching rounded corners and buttons, so `_own_chrome` is false and none of that code runs.

Reimplementing that hand-drawn frame in raw Wayland/EGL, with no GTK widget toolkit underneath, is
real extra work for a compositor (sway) that is already the documented fallback, not the primary
target. **Decision: the native rewrite targets Wayfire only.** sway keeps running the existing
Python `nethos-view` unmodified, selected by a new `NETHOS_VIEW_IMPL` environment variable
alongside the existing `NETHOS_COMPOSITOR` fallback switch already in
`payload/install-nethos.sh`'s `.bash_profile` heredoc. A broken native build must never leave a
machine with no shell at all.

## What the rewrite must reproduce exactly

Established by reading `payload/bin/nethos-view`, `payload/bin/nethos-session`, and
`payload/nethosd/nethosd.py` in full:

1. **Spec grammar** -- bare positional `key=value,key=value` strings (or `--surface SPEC`),
   `--apphost` flag, mandatory `url=`, per-role defaults (`panel`/`dock`/`overlay`/`widget`/
   `window` -- layer/anchor/exclusive table, `nethos-view` lines 124-130).
2. **The `--apphost` Unix socket** -- `$XDG_RUNTIME_DIR/nethos-apphost.sock`, one spec string per
   connection, client writes then half-closes, empty string is a liveness probe only. `nethosd.py`
   hardcodes the same path independently; this framing is not changing on the daemon side.
3. **One shared WebProcess** -- shell and every app window must share a single WPE web process,
   the way WebKitGTK's `related_view` construct property does today. This is the ~500MB memory
   fix already shipped this session; regressing it would undo that. WPE's GLib API is expected to
   expose the same `webkit_web_view_new_with_related_view()` entry point WebKitGTK does (same
   "WebKit2 GLib API" layer, different backend) -- needs confirming once the surface-hosting code
   is written, not yet verified.
4. **Layer-shell roles** via `wlr-layer-shell-unstable-v1` -- currently `gtk4-layer-shell`,
   LD_PRELOAD'd into the Python process. No GTK under WPE, so this becomes a hand-rolled Wayland
   client (see the spike, below). `window`-role surfaces are plain `xdg_toplevel`s, decorated
   server-side by Wayfire/firedecor -- exactly the path the Wayfire-only scope cut keeps.
5. **The `nethosHost` JS bridge** -- the only `WebKitUserContentManager` message handler anywhere
   in the codebase. Methods: `exclusive(n)`, `inputRect(x,y,w,h)`, `hide()`, `show()`, `repaint()`,
   `keyboard(on)`, plus a static `surface` name. `payload/shell/shell.js` (1670 lines, unmodified
   by this task) calls every one of these from many places -- get the message shape wrong and the
   shell silently misbehaves (ghost frames, a search box that never receives focus, etc.).
6. **Tick/events fan-out** -- `window.nethosTick()` every 1000ms and `window.nethosEvent(payload)`
   per SSE `data:` line from `nethosd`'s `GET /api/events`, both invoked into every surface's JS
   context from the host process. This exists because WebKit suspends background/never-focused
   layer-shell surfaces -- confirmed real and documented at length in the existing code. One shared
   SSE connection in the host, fanned out to every surface, not one `EventSource` per page (that
   was tried once and exhausts WebKit's shared per-origin connection pool).
7. **`_settle_wait()`** -- find Wayfire's PID via `/proc/[0-9]*/comm`, read its start ticks from
   `/proc/PID/stat` field 22, wait via `/proc/uptime` until `NETHOS_SETTLE` (default 4s) of
   compositor uptime has passed. Plain `/proc` + `sysconf(_SC_CLK_TCK)`, no GLib needed for this
   specific piece.
8. Minimize/maximize button wiring (`_on_light`/`_window_action`, an HTTP POST to nethosd's
   `/api/window`) is **dropped from scope** along with `_own_chrome` -- under Wayfire, firedecor's
   own buttons drive this already, compositor-side, with no involvement from `nethos-view`.

Everything else nethos-view does is unaffected: `nethosd`'s HTTP API, `shell.js`, every app's
`index.html`, and `nethos-session`'s five-surface launch line all stay as they are.

## The one real unknown: presenting WPE's frames

`wpe_view_backend_exportable_fdo_egl` hands back exported EGL images via callbacks -- it does not
put pixels on screen by itself. WPE's own reference compositor, **cog**, solves this in its Wayland
platform plugin via `linux-dmabuf-v1` + `wl_surface.attach/commit`, but cog is not packaged for
Debian trixie and doesn't cover layer-shell surfaces regardless, so it isn't a drop-in answer here.

This was the first thing tested, before committing to anything larger. A standalone C program
(`~/nethview-spike/spike.c` on the laptop, not yet in this repo) does the whole pipeline by hand:
binds `wl_compositor` + `zwlr_layer_shell_v1` from the registry, creates a `zwlr_layer_surface_v1`
anchored to the top edge with an exclusive zone, creates its own `wl_egl_window` + EGL window
surface for presentation on the *same* `EGLDisplay` WPE uses, and on every `export_fdo_egl_image`
callback binds the exported `EGLImageKHR` as a GL texture (`GL_OES_EGL_image` /
`glEGLImageTargetTexture2DOES`) and blits it into the layer-shell surface with a trivial
textured-quad shader before releasing the image back to WPE.

It links and builds cleanly against packages already present on the laptop (`wayland-client`,
`wayland-egl`, `egl`, `glesv2`, `wpe-webkit-2.0`, `wpe-1.0`, `wpebackend-fdo-1.0`, `glib-2.0`), with
`wlr-layer-shell-unstable-v1.xml` fetched from `wlr-protocols` upstream (not packaged on this
system, so it will need vendoring into the repo -- see below) and `xdg-shell.xml` from Debian's
`wayland-protocols` package. One build snag worth recording: `wlr-layer-shell-unstable-v1.xml`'s
`get_popup` request references `xdg_popup`, so linking the generated layer-shell protocol code
requires also generating and linking `xdg-shell`'s protocol code, even though this spike never
creates an `xdg_popup` itself -- the symbol reference is unconditional in the generated bindings.

**Confirmed against the real Wayfire session on the laptop.** Run over SSH with `WAYLAND_DISPLAY`
pointed at the live session: the layer surface configured to the real output width (1366x40, not
the 1200 fallback), WPE loaded the App Store page (`WEBKIT_LOAD_FINISHED` at t=2.9s) and exported
exactly one real `EGLImageKHR`, which was bound as a texture and blitted into the layer-shell
surface via `eglSwapBuffers`. Only one export arrived over a 15s run -- consistent with WPE only
repainting on invalidation and the App Store page having no continuous animation, not a bug; this
is the same "WebKit suspends a surface with nothing driving repaints" behaviour the existing
tick/events fan-out (see above) already exists to work around, and the rewrite will need that same
mechanism. Not yet confirmed by a human looking at the physical screen -- the evidence so far is
program-level (a real exported image was captured and swapped), not a visual check. That's the
next thing to do, then Phase 1 (the actual surface-hosting host process) can start.

## What still needs doing before this is real

- **wlroots-protocols is not packaged.** `wlr-layer-shell-unstable-v1.xml` had to be fetched from
  `gitlab.freedesktop.org/wlroots/wlr-protocols` directly; it needs to live in this repo (e.g.
  `payload/nethos-view-native/protocols/`) rather than being fetched at build time.
- **No compiled-from-source component has ever existed in this repo.** `docs/SYSTEM.md` currently
  states source compilation is avoided as a matter of policy. This rewrite is the deliberate
  exception; that doc should be updated once (if) this ships.
- **WPE's Debian packages (`libwpe-1.0-1`, `libwpebackend-fdo-1.0-1`, `libwpewebkit-2.0-1`, `-dev`
  variants) are not yet in `pkg/npkg_bootstrap.py`'s `SETS["desktop"]`.** They exist on the laptop
  only because they were installed by hand for this session's benchmarking.
- **No build step exists yet** to compile anything and drop the resulting binary into
  `payload/bin/` before `install_desktop()`/`install-nethos.sh` copy it onto a target system --
  both of those copy loops are already binary-agnostic (`install -m 0755` over whatever is in
  `payload/bin/nethos-*`), so nothing there needs to change, but nothing currently produces the
  binary in the first place.
- Full implementation phases, the `NETHOS_VIEW_IMPL` fallback design, and the verification plan are
  recorded in the session's working plan; this document exists to survive independently of that.
