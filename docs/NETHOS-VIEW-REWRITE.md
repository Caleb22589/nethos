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
- `repaint()`'s exact ghost-frame behaviour (the specific scenario `shell.js`'s own comments describe)
  has only log-level verification, not a visual one.

## The white-window bug: root cause found (the WebProcess sandbox) -- see below, fixed

A second overnight session chased the "some windows render decoration-only, no content" finding
above all the way to ground, using `grim` (installed via `npkg fetch grim -y` -- not previously
in this repo's package set, worth adding if this becomes a standing dev tool) to screenshot the
real screen directly rather than relying on someone physically looking at it each time. That
turned out to be essential: it let dozens of hypotheses get tested and discarded in the time a
single round of "does this look right?" would have taken.

**What got ruled out, in order, each with a real test against the live laptop:**

1. **Not a code regression.** The exact last-committed binary (`1e8177c`), rebuilt fresh from a
   clean `git clone`, reproduces the same blank rendering. So does the untouched Phase 0 spike
   (`~/nethview-spike/spike.c` on the laptop, never modified since Phase 0) -- and its own log shows
   the identical "real image exported, WEBKIT_LOAD_FINISHED" sequence the doc originally recorded as
   a success, just with nothing appearing on screen this time.
2. **Not GPU/driver resource exhaustion from repeated process restarts.** A full reboot (twice) did
   not fix it; the very first `nethos-view-native` launch after a clean boot, before any other
   process in this session had touched the GPU at all, still rendered nothing.
3. **Not a screenshot-tool artifact.** Confirmed by a human looking at the physical screen at the
   time -- genuinely blank, not a `grim` bug.
4. **Not atomic-vs-legacy KMS.** `nethos-session`'s existing `WLR_DRM_NO_ATOMIC` mechanism turned out
   to have never worked in the first place -- it sets the variable in *its own* environment, which is
   a child of Wayfire's autostart and therefore runs strictly after Wayfire has already initialised
   its DRM backend with whatever it inherited from `.bash_profile`'s `exec wayfire`. Set correctly
   (in `.bash_profile`, before `exec wayfire`, confirmed present in Wayfire's own `/proc/PID/environ`
   afterward) and tested with a real reboot: no change. `nethos-session`'s own copy of this mechanism
   is a latent, separate, real bug worth fixing on its own merits, independent of this one.
5. **Not "Wayfire stops compositing new surfaces".** A brand-new `foot` terminal window, launched in
   the same session moments after the native shell failed, rendered perfectly -- real content, real
   firedecor frame. New surfaces reaching the screen at all is not the problem.
6. **Not EGL/GL presentation itself.** A from-scratch, self-contained test client
   (`pure_egl_test.c`, written and run live on the laptop, no WPE and no cross-process buffer of any
   kind involved) presented a plain glClear'd red rectangle via the exact same
   `wl_egl_window`/`eglSwapBuffers` mechanism `nethos-view-native` uses for its own output --
   and it worked, immediately, screenshot-confirmed.
7. **Not dma-buf/EGLImage import specifically.** Since (6) narrowed it to "importing WPE's exported
   buffer" rather than "presenting at all", the exportable_fdo backend was switched from the
   dma-buf/EGLImage path (`wpe/fdo-egl.h`) to WPE's plain SHM export path (`wpe/unstable/fdo-shm.h`,
   `wpe_fdo_initialize_shm()`) -- CPU-side pixel upload via `glTexImage2D`, sharing nothing but a
   memory-mapped file across the process boundary. Still blank. Sampling the actual SHM buffer bytes
   (`wl_shm_buffer_get_data()`, real pixel values, not just "did the API call report success") showed
   why: the buffer WPE handed over was genuinely, entirely zero -- every byte, every surface, every
   run. Not a bug in this process's rendering at all; WPE's own WebProcess was not painting anything.
8. **Not the shader pipeline.** Checked directly -- link status, `glUseProgram`, `glDrawArrays` all
   report success with zero GL errors. (No verification code existed for this before tonight; adding
   it is a real, permanent improvement, kept regardless of the outcome here.)
9. **Not WPE's activity-state suspension.** `wpe_view_backend_add_activity_state(visible|focused|
   in_window)`, called right after backend creation, was removed early in this rewrite's history
   after appearing to cause a crash -- but the real cause of that crash, found later the same night,
   was the unrelated dangling-pointer bug in the callback struct (see the `s_client`/`s_egl_client`
   history above), now long since fixed. Added back on that basis; made no difference to this bug
   either way, but is being kept -- there is no reason not to tell WPE a surface is actually visible.

**What it actually is:** `payload/bin/nethos-view` (Python/WebKitGTK) and every one of the
elimination tests above that worked share one thing every failing `nethos-view-native` run does not:
none of them run WebKit's own `WPEWebProcess`/`WPENetworkProcess` inside its `bwrap` sandbox and then
try to read real output from it. Confirmed directly: launching `nethos-view-native` with
`WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1` set -- the real WPE env var for exactly this kind of
diagnosis, not something this project invented -- produced real, correct, screenshot-confirmed
content (App Store's title bar, search field, everything) on the very first try, with no other
change. `ps` confirmed no `bwrap` process existed for that run at all, where every other run had one
wrapping `WPEWebProcess`. The `bwrap` sandbox around the WebProcess is silently preventing it from
painting anything at all on this machine -- not crashing, not erroring, just producing a WebProcess
that runs, loads pages, responds to JS, and paints nothing, ever, into any buffer it hands back,
regardless of whether that buffer is dma-buf or plain SHM. This is a sandbox-vs-this-system
compatibility problem (most likely a missing bind-mount, device node, or syscall the sandbox denies
that this particular npkg-converted install lacks something Debian's own postinst scripts would
normally have set up -- see `docs/HANDOFF.md`'s whole table of exactly this failure class for
unrelated things), not a bug in `nethos-view-native`'s own code.

**Why this is not fixed yet, deliberately:** running the WebProcess unsandboxed is a real security
regression -- the sandbox exists specifically to contain a compromised web renderer, and WPE's own
naming (`_THIS_IS_DANGEROUS`) is not decoration. Disabling it was the right diagnostic, not a
shippable fix. What's actually needed is finding *what* the sandbox is denying that breaks painting
specifically (fonts? a `/dev/dri` render node bind-mount? a seccomp-denied syscall the software
rasterizer needs?) and either fixing that one thing or, if this really is an environment-specific gap
`npkg`'s conversion should be closing, treating it the same way every other entry in `docs/HANDOFF.md`
was: a missing step, not a reason to weaken the sandbox everywhere. Concretely, next session:
compare this system's bwrap invocation/profile against a stock Debian WebKitGTK install's (the
Python build's WebKitGTK path uses the *same* `bwrap`, and *it* renders fine -- meaning whatever is
missing is either specific to the WPE package's own sandbox profile, or specific to something
`nethos-view-native` does differently in how it launches the WebProcess that the Python/WebKitGTK
path does not).

The SHM rendering path from step 7 is being kept regardless of this outcome -- it is a real
simplification (no dma-buf/modifier negotiation, no cross-process GPU buffer sharing question to ever
re-litigate) independent of the sandbox question, and the elimination above only worked *because* it
existed to test against.

## The white-window bug: fixed -- four bugs, not one

The sandbox theory above was half right and half a dead end. `neth` genuinely was missing from the
`render` group (`sudo usermod -aG render neth`) -- a real, separate gap, and exactly the class of bug
`docs/HANDOFF.md` predicts (a Debian postinst a real install would have run and npkg's conversion
does not) -- but fixing it alone was not enough: swapping to the full shell afterward still showed a
blank screen. `strace -f -e trace=openat,access,stat,statx` on a sandboxed `WPEWebProcess` after the
group fix showed `/dev/dri/renderD128` opening fine (`O_RDWR|O_CLOEXEC`, no `EACCES`/`ENOENT`
anywhere near it) -- so the *sandbox* half of the original diagnosis was already resolved by the
group fix, and the remaining blank screen was a second, unrelated problem in this process's own
code, not the sandbox at all. Four separate bugs were actually stacked on top of each other; each one
individually produced the exact same symptom ("nothing renders"), which is what made this look like
one problem for so long.

**Bug 1 -- `render` group membership** (system config, not code): fixed live via `usermod`; belongs in
whatever does this install's user setup (see "Not yet fixed" below).

**Bug 2 -- no frame-pacing handshake at all.** Neither `wpe_view_backend_dispatch_frame_displayed()`
(the generic WPE vsync-pacing ack) nor `wpe_view_backend_exportable_fdo_dispatch_frame_complete()`
(the exportable_fdo backend's own, separate "I'm done with that frame" ack -- a different function on
a different object, `struct wpe_view_backend_exportable_fdo*` not `struct wpe_view_backend*`, see
`view-backend-exportable.h`) was ever called anywhere in this codebase. Confirmed live with a trivial
counter page (`setInterval` incrementing a number on screen every 500ms, served from a throwaway
`python3 -m http.server`, no `nethos.js`/nethosd dependency at all to rule out every other variable):
first paint appeared, then froze forever -- `on_export_shm` fired exactly once, confirmed by a
temporary debug counter, no matter how long the process ran. WPE was withholding every export after
the first because nothing ever told it the first frame had actually been displayed. Dispatching both
acks in `render_shm()` right after `eglSwapBuffers()` fixed it outright -- the same counter page then
updated continuously, screenshot-confirmed frame-to-frame.

**Bug 3 -- no `eglSwapInterval`.** Fixing bug 2 immediately exposed this one: with both acks firing
right after an unthrottled swap, WebKit repainted as fast as the CPU allowed -- `nethos-view-native`
and `WPEWebProcess` both pegged near/above 100% CPU, `WPEWebProcess` RSS climbing continuously, and
paradoxically the screen going back to showing nothing (Wayfire's own compositor falling behind an
unbounded commit rate is indistinguishable from a screenshot of a client that never painted at all).
`eglSwapInterval` had never been called anywhere either, so the driver's own default (evidently non-
blocking here) governed every swap. Added `eglSwapInterval(g_egl_display, 1)` once per surface, right
after that surface's own `eglMakeCurrent` in `ensure_egl_surface()` -- it is a property of the EGL
*surface*, not the context, so it cannot be set once globally. CPU dropped back to a sane range
immediately.

**Bug 4 -- three unguarded `wl_surface_commit()` calls, one of them a genuine race, not just a
missing check.** With frame pacing now correct, a single `role=window` test against the real App
Store (`/apps/store/index.html`) rendered perfectly end to end -- header, live package list, install
buttons, real `Installed` state pulled from nethosd. But launching the real 5-surface shell
(`nethos-session`'s actual `splash`/`desktop`/`panel`/`dock`/`menu` spec set) still went blank, and
`WAYLAND_DEBUG=1` showed why: `wl_display#1.error(zwlr_layer_surface_v1#12, 2, "layer_surface has
never been configured")`. That is a *fatal* Wayland protocol error -- it doesn't just drop one
surface, it tears down the entire `wl_display` connection, killing every other surface on it at once,
which is exactly why the whole shell looked identically blank whether the bug was "nothing paints" or
"one thing paints wrong and takes the rest down with it." Three sites in `bridge.c`'s `on_message()`
called `wl_surface_commit()` directly instead of going through `nethos_surface_repaint()` (which
already correctly checks `s->configured` first, the way `input_rect`'s handler always did):
`"exclusive"` and `"keyboard"` both commit as an immediate side effect of a `nethosHost.*()` call from
shell.js, and shell.js calls both as soon as it has measured its own content -- easily racing ahead of
this surface's first `layer_surface.configure`/`ack_configure` round-trip, especially under five
surfaces loading concurrently. Fixing both (route through `nethos_surface_repaint()`) closed that
race, but object #12 kept erroring identically on every retry -- `WAYLAND_DEBUG=1` traced it to
*splash* specifically (`zwlr_layer_shell_v1#6.get_layer_surface(new id ...#12, wl_surface#11, nil, 3,
"nethos-splash")`), which had already configured and acked cleanly multiple times. The actual
remaining race was in `render_shm()` itself, not `bridge.c`: splash's own `nethosHost.hide()` (called
once the panel has checked in, see `splash.html`) unmaps the surface with `wl_surface_attach(NULL)` +
commit, but WPE can still have one frame already in flight from *before* that hide call, and
`render_shm()` only ever checked `s->configured` -- not `s->visible` -- before painting and
implicitly committing a *real* buffer via `eglSwapBuffers()`. That trailing frame landed on a surface
that had just been unmapped, which Wayfire rejects with the same fatal "never configured" error.
Added `!s->visible` to `render_shm()`'s existing early-return guard. With all four bugs fixed, the
full 5-surface native shell renders completely and correctly: panel, wallpaper, desktop icons, live
widgets (Monitor/News/Watchlist, real data), and the app launcher's live search grid (20 real
results, real icons) all confirmed via `grim` screenshots against the real laptop.

**Not yet fixed, follow-on work:** the `render`-group fix (bug 1) was only ever applied live via
`usermod` on the test laptop -- it needs to land in whatever this install's own user-provisioning
step is (`payload/install-nethos.sh` or `pkg/npkg_bootstrap.py`, matching the pattern
`docs/HANDOFF.md` already documents for PAM/sshd/etc.), or every fresh install will hit this same
wall. `nethos-session`'s own separate, already-documented `WLR_DRM_NO_ATOMIC` bug (sets the variable
too late in its own process's environment to affect Wayfire's already-initialised DRM backend) is
still open and unrelated to any of the above. The actual boot-time win this whole rewrite exists for
still has not been re-measured on a full image rebuild + reboot, only via live-session swaps.

## Menu/Ask/Control Center never opening: two more bugs, found after native became the default

Commit `9cac02c` made the native rewrite the default on Wayfire. The very next report from real use:
dock and panel window management worked, but Menu, Ask and Control Center would not open at all.
Shell.js's `overlayMapped()` (the function behind all three) is the only caller of
`nethosHost.hide()`/`show()` in the whole shell -- panel and dock are never hidden -- so these three
were the only surfaces it was possible to notice this on.

**Bug 5 -- the frame-pacing acks from the previous fix were themselves conditional on visibility.**
`render_shm()` dispatched `frame_complete`/`frame_displayed` right after painting, but painting itself
was guarded on `s->visible` (added at the same time, for a real and separate reason -- see below).
A frame WPE exported *while a surface was hidden* -- which is every overlay's normal starting state,
since shell.js hides them the moment nothing is open -- got its buffer released (that call was already
unconditional) but never acked. WPE then withheld every export after that one, forever, for that
surface specifically, regardless of any later `nethosHost.show()`. Confirmed with a minimal
reproduction: a throwaway page (no `nethos.js`, no nethosd) with a `setInterval` counter and two
`setTimeout`s calling `nethosHost.hide()` then `nethosHost.show()` a few seconds apart, loaded as a
standalone overlay. It painted its first frame fine, went blank on `hide()`, and never came back on
`show()` -- reproducing the exact shape of the report with zero app-specific code involved. Fix: moved
both dispatches out of `render_shm()` into `on_export_shm()`, unconditional, alongside the buffer
release they already sit next to -- they are WPE's own frame-pipeline bookkeeping (“I got your last
frame, send the next one”), not a statement about whether this client chose to visually present that
particular frame.

**Bug 6 -- unmapping a layer-shell surface and later remapping it is fatal under this Wayfire, and
nothing was requesting the reconfigure that would allow it.** Fixing bug 5 alone was not enough: the
same reproduction, retested, now hit `wl_display#1.error(zwlr_layer_surface_v1#N, 2, "layer_surface
has never been configured")` -- confirmed with `WAYLAND_DEBUG=1`, traced to the surface's own real
content commit landing shortly after `show()`. `hide()`'s implementation attached a `NULL` buffer to
unmap the `wl_surface` (the standard, protocol-correct way to unmap, and the same technique this
rewrite's own earlier bug-4 fix relied on to avoid a *different* fatal error -- see the section above).
The trace showed why this one is different: Wayfire accepted two bufferless commits after the unmap
(an `input_rect` call and `show()`'s own repaint) without complaint, but rejected the first commit that
tried to attach a *real* buffer again, as if the surface had reverted to "never configured." Re-issuing
`zwlr_layer_surface_v1_set_size()` with the surface's own existing size, on the theory that any
geometry-affecting request might prompt a fresh `configure`, was tried first and confirmed live *not*
to produce one -- Wayfire simply never sends a new configure just because a client asks nicely after an
unmap.

The actual fix was to stop unmapping at all. `payload/bin/nethos-view`'s own code says outright, in a
comment on its `_repaint()`: its equivalent overlay surface "is deliberately never unmapped" -- for
related reasons, even though its `hide()` still calls GTK's `set_visible(False)`, which does map to an
underlying Wayland unmap. Whatever GTK/gtk4-layer-shell does internally to make that safe to reverse is
not something a direct libwayland client gets for free. `nethos_surface_paint_blank()` (`surface.c`) is
the replacement: on `hide()`, paint one real, fully transparent frame and present it, instead of
attaching `NULL`. This still gets everything unmapping was for -- a real commit that the compositor
cannot ignore, genuinely damaging the region to nothing rather than leaving a stale frame on screen
(the "ghost" bug shell.js's own comments describe at length) -- without the surface ever leaving the
"configured" state a remap would need to re-earn. `show()` no longer needs the `set_size` nudge either;
it was never the actual mechanism, just a hypothesis that happened not to cause harm.

**Verified end to end, both after the synthetic reproduction and separately against the real desktop**,
via the actual production trigger path rather than a direct bridge call -- `curl -X POST
http://127.0.0.1:7777/api/nethbot/ask -d '{"open":true}'` and `.../api/control/toggle` -- confirmed
with `grim` screenshots: Ask opens with the search box focused and receiving keystrokes, closes clean
on a second toggle, reopens clean again, no ghosting, no protocol errors, panel/dock/desktop widgets
unaffected throughout.

**A build-and-deploy trap worth recording**: `payload/nethos-view-native/build.sh` always writes its
output to `~/bin/nethos-view-native` on whatever machine it's run on. That path is not on
`nethos-session`'s `PATH` (`/usr/bin:/usr/local/bin`), so a rebuild during live debugging does nothing
to the actually-running desktop until it's also `sudo install -m 0755`'d to `/usr/bin/nethos-view-native`
-- confirmed live, more than once, by chasing a "why didn't my fix change anything" ghost that turned
out to be testing against a stale binary the whole time. A real image build's `install_desktop()`
handles this automatically; a hand-built live-debug binary does not.

## Back to dma-buf/EGLImage: SHM was never load-bearing, and it was the lag

Reported once the desktop was usable enough for real interaction: scrolling and general UI felt
laggy, "not GPU-accelerated." Rendering itself was hardware-accelerated the whole time -- confirmed
with `eglinfo` on the Wayland platform specifically (not its surfaceless-platform probe, which
does report `llvmpipe` by design and is not what this process uses): Mesa's `crocus` driver, Intel HD
Graphics 4400 (Haswell GT2). The actual cost was the export mechanism. `wpe_fdo_initialize_shm()` --
adopted partway through the white-window investigation above specifically to rule out cross-process
GPU buffer sharing as a variable, at a point where every dma-buf-backed surface rendered nothing with
every API call reporting success -- requires WebKit to read its own GPU-composited frame back into a
CPU-side shared-memory buffer every frame, and this process to re-upload that buffer into a fresh GPU
texture with `glTexImage2D`. A full GPU->CPU->GPU round trip of the whole surface, at up to 60fps, on
hardware old enough (2013-era Haswell) for that memory-bandwidth cost to be very noticeable.

It turned out dma-buf was never actually the problem. The real causes of the white-window bug --
missing `render` group membership, two missing WPE frame-pacing acks, no `eglSwapInterval`, three
Wayland protocol races (see the sections above and their commits `93e9941`/`79a4962`) -- applied
identically to the dma-buf/EGLImage import path and are now fixed; none of them had anything to do
with which buffer-sharing mechanism was in use. Switching back
(`wpe_fdo_initialize_for_egl_display()`, `wpe_view_backend_exportable_fdo_egl_create()`,
`glEGLImageTargetTexture2DOES` instead of `glTexImage2D` -- see `git show 1e8177c:.../surface.c` for
the pre-SHM-detour version this was ported forward from, keeping every fix landed since) confirmed the
theory directly: a sustained scroll that spiked `WPEWebProcess` to 45-127% CPU under SHM sits near 0%
under dma-buf, on the same page, same hardware, same interaction. Content still renders correctly
single-window and full-shell, and the menu/ask/control-center hide/show fix still holds exactly as
before -- none of that logic depends on which export mechanism is active, only on the frame-pacing
acks and `s->visible`/`s->configured` guards being correct, which they still are (now dispatched
unconditionally from all three EGL exportable callbacks, not just the one SHM used, closing the same
class of permanent-stall bug for the unlikely case WPE hands back an SHM buffer anyway under the EGL
backend).

## Still laggy after dma-buf: `eglSwapInterval(1)` was blocking the whole event loop

dma-buf fixed CPU cost -- a plain scroll test dropped from spiking `WPEWebProcess` to 45-127% down to
near zero -- but the report that followed was specific: every cursor hover/focus change and dragging
the volume slider in Control Center still felt slow. Low CPU and low *latency* are different claims,
and this was a latency bug.

Timing instrumentation wrapped directly around `eglSwapBuffers()` (a `clock_gettime()` pair, logged
whenever a single call took more than 3ms) found the cause immediately: 3-19ms per swap, clustered
right around one vsync period (16.67ms at this panel's 60Hz). That is exactly what `eglSwapInterval(1)`
(added earlier, see the frame-pacing section above, to stop WPE re-exporting faster than the CPU could
keep up) is supposed to do -- except Mesa's own implementation of that block, in its Wayland EGL
platform, works by registering a `wl_surface.frame` callback and blocking the *calling thread* until
the compositor fires it. `nethos-view-native` is single-threaded: one GLib main loop handles Wayland
dispatch (every surface's input, not just the one currently swapping) and every surface's rendering,
all on the same thread. One surface's blocking swap therefore stalled reading the *next* pointer-motion
event for every surface on screen, for up to a full vsync period, on every single swap. With several
surfaces capable of animating in the same tick -- the panel clock, a widget's periodic refresh, a
slider actively being dragged -- those blocks stack, which is what turned into "everything feels
slightly laggy" rather than one clearly broken interaction: a plain discrete scroll (my own first test)
happens to generate few enough events, spaced widely enough, not to expose it; continuous pointer
motion during a hover or a drag generates far more.

The fix does explicitly and non-blockingly, only for the one surface that actually needs it, what
`eglSwapInterval(1)` was doing implicitly and expensively for every surface at once: back to
`eglSwapInterval(0)` (swap returns immediately, never blocks), and `render_surface()` now calls
`wl_surface_frame(s->wl_surface)` itself right before each swap, registering a `wl_callback` that rides
along on the same commit. WPE's `frame_complete`/`frame_displayed` acks -- which must still eventually
fire for every export or WPE withholds all further ones (the menu/ask/control-center bug above) -- are
now dispatched from that callback's `done()` handler once the compositor actually confirms presentation
(non-blocking, event-driven, arrives through the same `wl_display_dispatch()` already reading every
other Wayland event), instead of unconditionally right after the swap call returns. `render_surface()`
now returns whether it actually painted, so the three `on_export_*` callbacks in `surface.c` can still
ack immediately, with nothing to wait for, on the frame it declines to paint (hidden or not yet
configured) -- the same distinction bug 5 needed, just routed through a callback instead of an
immediate call for the frame it *does* paint. `nethos_surface_destroy()` destroys a still-pending
`frame_cb` before freeing its surface, since an orphaned callback would otherwise still fire into freed
memory once the compositor eventually got around to it.

Confirmed live: idle CPU stays at 0%, a sustained scroll's CPU stays in the same low range dma-buf
already established, and Ask still opens with keyboard focus and closes clean through the real
production trigger path -- none of the hide/show or protocol-race fixes depended on which pacing
mechanism was active. Whether hover and slider-drag now actually *feel* smooth needs a person's own
hands on the trackpad to confirm; that part of the report is with the user.

## A hidden surface was still catching every click across the whole screen

Found while directly instrumenting and testing pointer routing to verify the frame-callback fix above:
deliberately synthesized pointer motion aimed at the panel and the desktop was instead reported, every
time, against `splash` -- full-screen, invisible, and still the hit-test target for the entire output.

The cause traces back to the earlier switch from a null-buffer `wl_surface_attach()` unmap to
`nethos_surface_paint_blank()` for `hide()` (see "The white-window bug: fixed" above, bugs 5-6). That
switch fixed a real fatal protocol error, but it quietly dropped a guarantee unmapping used to provide
for free: an unmapped `wl_surface` simply is not a hit-test target for anything, so nothing else ever
needed to *also* clear its input region on the way out. A surface kept fully mapped, just painted
blank, has no such free lunch -- it stays exactly as clickable as it ever was, just invisible.
`menu.html`'s own close path was never caught by this because it already calls
`nethosHost.inputRect(0, 0, 0, 0)` itself before every one of its own `hide()` calls (grep `shell.js`).
`splash.html`'s `hide()` never has -- it was written against the old implicit guarantee and had no
reason to think it needed to ask for click-through separately.

Fixed at the bridge layer, not in `splash.html`: `hide()` (`bridge.c`) now clears the surface's input
region itself, alongside the blank paint, so every hidden surface is click-through unconditionally
regardless of whether its own page separately manages `inputRect`. Confirmed at the protocol level
with `WAYLAND_DEBUG=1` rather than trusting the remote pointer-simulation tooling (`wlrctl`'s absolute
coordinates turned out not to map onto real screen pixels in any way this session could reliably
calibrate, which is what motivated checking the wire protocol directly instead): splash's `wl_surface`
gets a `set_input_region` request with a region that has no `add()` call before it -- genuinely empty
-- immediately after its own `hide()` fires, exactly matching the fix.

This one was serious: a full-screen surface silently swallowing every click and hover across the
entire desktop, invisibly, is not a "feels a bit laggy" bug -- it is "most of the desktop doesn't
respond to input at all," and was live on the actual running desktop at the time it was found.

## "Control Centre is 0fps" -- three places paying for backdrop-filter blur twice

Not a GPU question -- `eglinfo` on the Wayland platform already confirmed real hardware acceleration
(Mesa's `crocus` driver, actual Intel HD 4400/Haswell silicon, no software fallback) earlier in this
investigation. `#panel` and `#dock` in `payload/shell/style.css` already carry their own comment
describing this exact bug and its fix, found once before: "the compositor already blurs a transparent
layer surface on the GPU, so a CSS blur on top of that is doubled cost for a worse (over-filtered,
damage-invalidated) result, not skipped cost." That fix gates the CSS-side `backdrop-filter` behind
`:not(.neth-compositor-blur)`, a class `bridge.c` sets unconditionally under Wayfire (which always
blurs behind a transparent layer surface itself, unlike the Python build's sway/Hyprland runtime
check). `.cc-card` (Control Centre), `#menu` (the app launcher), and `.ask-input`/`.ask-log` (Ask)
never received that same gate -- each ran its own 30px `backdrop-filter` blur unconditionally, real
compositor blur behind it or not, paying `nethos.css`'s own documented "by a wide margin the most
expensive thing this stylesheet can ask for" cost on every single frame regardless of whether it
bought anything visually. Fixed identically to `#panel`/`#dock`: gated behind the same class. All
three backgrounds are already 90%+ opaque, so there's effectively no visible difference (confirmed via
screenshot); a rapid pointer-motion CPU sample over Control Centre went from sustained high spikes to
brief, small ones.
