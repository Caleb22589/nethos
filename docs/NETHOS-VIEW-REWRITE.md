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

## Phase 1: the actual surface-hosting host process

Built as `payload/nethos-view-native/` (`src/`, `protocols/`, `build.sh`), a full rewrite of every
item this document lists as load-bearing: spec grammar, the apphost socket, shared WebProcess,
layer-shell roles, the `nethosHost` JS bridge, tick/events fan-out, `_settle_wait`. Everything below
was built and run against the real, live Wayfire session on the laptop over SSH -- not merely
compiled -- the same standard the Phase 0 spike used.

**The `related-view` question this document raised is resolved: it exists and works.** Grepping
WPE's installed headers found no `related-view` property, which looked like a real problem --
WebKitGTK's process-sharing mechanism looked absent from the WPE build of the same GLib API layer.
It was a false alarm from grepping headers: GObject construct properties are registered in the
*implementation*, not declared in a public header, so a header grep was never going to find it
either way. A runtime diagnostic (`g_object_class_list_properties` on `WEBKIT_TYPE_WEB_VIEW`) found
`related-view` in the real list of 24 properties, and two views built with it running under one
`WebKitWebContext` produced exactly one `WPEWebProcess`/`WPENetworkProcess` pair for both, confirmed
via `ps`. The ~500MB memory fix carries over unchanged.

**The other open question -- WPE has no windowing system, so input has to be hand-wired -- is real,
and is now done.** The Phase 0 spike had no `wl_seat` at all. This phase binds it, tracks
pointer/keyboard focus per surface (enter/leave), and forwards through
`wpe_view_backend_dispatch_pointer_event`/`dispatch_keyboard_event`/`dispatch_axis_event`. Keyboard
uses `wpe_input_xkb_context_*` (from `wpe/input-xkb.h`, `WPE_ENABLE_XKB=1` in this build) to turn the
compositor's keymap into WPE's key codes -- xkbcommon does the state tracking, WPE's helper does the
translation, no hand-rolled keysym table needed. Verified two ways: a synthetic pointer-button
dispatch through the exact same API produced a real `onclick` handler firing (`BUTTON CLICKED,
count=1` via `console.log`, forwarded to stdout); no tool on the laptop (no `wtype`/`ydotool`) could
synthesize *real* hardware input to prove the `wl_seat` listener wiring itself end-to-end, which
remains to be confirmed by a human at the keyboard.

**One real bug, found and fixed the same way the frame-clock bug in HANDOFF.md was: a crash, not a
guess.** The first multi-surface build segfaulted, deterministically, every time a page finished its
first load -- inside `libWPEWebKit` itself, reached through `wl_event_loop_dispatch` and `libffi`
(WPE's own internal UI-process/WebProcess IPC, unrelated to this program's own Wayland connection to
Wayfire). Three plausible-looking causes were tried and ruled out in order (calling
`wpe_view_backend_add_activity_state` too early; two rapid `wpe_view_backend_dispatch_set_size` calls
from an initial 0x0 layer-shell configure) before a `SIGSEGV` handler
(`backtrace_symbols_fd`, `-rdynamic`) gave an actual stack trace, and re-running the *unmodified*
Phase 0 spike against the same live session and the same page at the same moment, with no crash,
proved the environment itself was fine. The real cause: `struct
wpe_view_backend_exportable_fdo_egl_client` -- the table of export callback function pointers -- was
a local (stack) variable passed by address to `wpe_view_backend_exportable_fdo_egl_create()`. WPE
keeps that pointer for the exportable's whole lifetime, not just for the call; once the creating
function returned, the pointer was dangling, and the eventual callback dispatch jumped through
whatever garbage now occupied that stack slot. The spike never hit this because it declares the
identical struct `static const` at file scope. Fixed the same way here -- `static const`, function
pointers only, the per-surface pointer passed as the separate `data` argument the API already
provides for exactly this. Confirmed fixed: real pages (including production `dock.html` and
`menu.html`, not just a synthetic test page) now load, render, and round-trip real `nethosHost.*`
calls (`shell.js`'s actual `exclusive`/`input_rect` calls, unmodified) with no crash, across dozens
of runs.

**JS bridge**: same six methods (`exclusive`/`inputRect`/`hide`/`show`/`repaint`/`keyboard`), same
message shapes as far as `shell.js` can tell -- the wire format underneath differs on purpose (a
plain JS object via WPE's `JSCValue`-based `script-message-received` signal, not
JSON.stringify/json.loads; WPE's WebKit version here, 2.48, only ever had the `JSCValue` signal
signature, `WebKitJavascriptResult` doesn't exist in these headers at all). Verified against real
`dock.html`: `nethosHost.exclusive()` and `nethosHost.inputRect()` calls it makes on load both
arrived and were handled correctly. `hide()`/`show()` verified with a driver page that calls both on
a timer, including the `wpe_view_backend_add_activity_state` call that turned out to be safe *there*
(unlike calling it during surface construction, see above) -- confirmed by console output reaching
stdout both before and after the hide/show cycle, meaning the page kept running rather than actually
dying. `repaint()`'s exact pixel-level effect (a bare `wl_surface_commit`, no `wpe_view_backend`
call, since WPE's own export/release cycle already re-arms the next frame) has not been checked
against the specific ghost-frame scenario `shell.js`'s own comments describe -- log-level
verification only, see below.

**Apphost socket**: same framing (`$XDG_RUNTIME_DIR/nethos-apphost.sock`, one spec per connection,
half-close, empty = liveness probe). Verified end-to-end against a real spec (opening a `role=window`
app) and a liveness probe, both handled without touching the live socket the real Python shell was
already serving -- tested against a temporarily-renamed socket path, reverted immediately after.

**`_settle_wait`**: direct port of the `/proc` parsing (one off-by-two bug caught before it shipped:
the field-22-via-rsplit arithmetic is easy to get wrong by exactly the amount that only shows up as
a slightly-wrong wait, not a crash -- worth double-checking against the Python original's own comment
rather than re-deriving it).

**Update, same day: the full shell was run as the actual live session, with the user at the physical
machine.** With explicit sign-off (this is someone's real desktop), the live Python `nethos-view` was
killed and `nethos-view-native` launched in its place with the identical five-surface spec, then
restored afterward -- three times, chasing three findings below. Confirmed visually by the user,
something no earlier testing in this document could do:

- **`related-view` sharing holds at full scale, not just two views.** All five shell surfaces plus a
  `settings` window opened through nethosd's real `/api/launch` -> `ensure_apphost()` ->
  `apphost_send()` path -- the actual production flow, not a hand-built test client -- shared exactly
  one `WPEWebProcess`/`WPENetworkProcess` pair throughout. Total RSS for the whole shell plus one app
  window: ~394MB, against the Python build's own ~613MB baseline for the shell alone (measured earlier
  the same session) -- consistent with the doc's original ~500MB-saved framing, on the real number
  this time rather than an estimate.

- **A real, confirmed, now-fixed bug: `nethosHost.inputRect(0,0,0,0)` was making things click-through
  proof instead of click-through.** `shell.js` calls that specifically to make an idle full-screen
  overlay (`splash.html`, `menu.html`) stop capturing clicks -- see its own `overlayMapped()`
  comments. `bridge.c`'s `set_input_region()` treated `w<=0 || h<=0` as "pass `NULL` to
  `wl_surface_set_input_region`", which the protocol defines as *resetting to the whole surface*, the
  opposite of the intent. Because `splash` and `menu` are both `ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY`
  (always on top, full-screen), this meant they silently captured every click across the entire
  screen for the surface's whole lifetime -- nothing under them, including a freshly opened app
  window, was ever clickable. Found by tracing real `wl_pointer` events live while the user clicked:
  `ptr_enter` kept matching `splash`/`menu` no matter where the cursor was. Fixed: always allocate a
  real `wl_region` and only add a rectangle to it when `w>0 && h>0`; an empty region (zero rectangles)
  is what the protocol actually means by click-through. Confirmed fixed the same way it was found --
  live tracing showed the pointer correctly reaching a `role=window` surface afterward, and real
  hardware clicks (not a synthetic dispatch) produced correct `ptr_button` events with the right
  button code and coordinates.

- **A second, still-open finding: at least one `role=window` surface rendered with only firedecor's
  title bar and no visible content.** Reported directly by the user looking at the screen ("your click
  me window has no window just top bar"). The one clean counterexample in the same session --
  `settings`, opened via the real apphost path -- rendered real content, confirmed by the user
  ("actual content!"). The window that failed was opened via a throwaway local `http.server` whose
  first connection attempt failed (`Connection refused`, the server hadn't finished starting) before a
  second attempt against the same URL succeeded and produced exactly one `render_surface` call in the
  log. Two live surfaces is not enough to separate "something about a failed-then-retried initial
  load leaves the EGL/xdg_toplevel state wrong" from "something else entirely" -- that is the leading
  hypothesis, not a confirmed cause. Reproduce without guessing at the mechanism: open a `role=window`
  surface whose first `webkit_web_view_load_uri` genuinely fails to connect and watch whether the
  *retry's* successful load still fails to present, against a control window that loads cleanly on the
  first attempt.

Boot-time itself -- the entire reason for this rewrite -- has still not been re-measured; that needs
a full image rebuild and reboot cycle, not a live swap on an already-running session.

## What still needs doing before this is real

- **`NETHOS_VIEW_IMPL=native` is wired into `nethos-session`, but python stays the default.** Opt-in
  only, and only reachable at all when `WAYFIRE_SOCKET` is set (sway always gets the Python build,
  matching the Wayfire-only scope decision above) and `nethos-view-native` is actually on `PATH`.
- **`pkg/npkg_bootstrap.py`'s `SETS["desktop"]` now has the three runtime WPE packages**
  (`libwpe-1.0-1`, `libwpebackend-fdo-1.0-1`, `libwpewebkit-2.0-1`) confirmed present under those
  exact names in this repo's own vendored `Packages-main-amd64.gz`. `-dev` packages are deliberately
  not added -- those are a build-host concern (`payload/nethos-view-native/build.sh`), not something
  a real install needs.
- **`payload/nethos-view-native/build.sh` exists** and produces `payload/bin/nethos-view-native` via
  `wayland-scanner` + one `gcc` line against packages already confirmed installed; it is not yet
  wired into `scripts/build-x86.sh`'s own image build, so a fresh image still needs this run by hand
  before `NETHOS_VIEW_IMPL=native` has anything to select.
- **`docs/SYSTEM.md`'s "no compiled code" policy note** still needs updating once (if) this actually
  ships as more than an opt-in experiment -- deliberately not touched yet, per this document's own
  original wording.
- **Open bug: some `role=window` surfaces render decoration-only, no content.** See the finding
  above -- reproduce deliberately (a window whose first load genuinely fails and retries) rather than
  by chance, ideally on a disposable test session rather than someone's live one now that the full
  shell has proven itself safe to swap in and back out of once a fix is ready to verify.
- `repaint()`'s exact ghost-frame behaviour (the specific scenario `shell.js`'s own comments describe)
  has only log-level verification, not a visual one.
