/* nethos-view-native -- shared types.
 *
 * Mirrors payload/bin/nethos-view's data model closely enough that anyone
 * who already knows that file can read this one: a `spec` is the same
 * key=value grammar, a `surface` is the same thing Surface.__init__ builds,
 * just with Wayland/EGL/WPE objects standing in for GTK ones.
 */
#ifndef NETHOS_VIEW_H
#define NETHOS_VIEW_H

#include <stdbool.h>
#include <stdint.h>

#include <wayland-client.h>
#include <wayland-egl.h>
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>

#include <wpe/webkit.h>
#include <wpe/fdo.h>
#include <wpe/fdo-egl.h>

/* Back on the dma-buf/EGLImage import path -- zero-copy, GPU to GPU -- after
 * a detour through WPE's plain SHM export path for most of a night's
 * debugging (see docs/NETHOS-VIEW-REWRITE.md's "white-window bug" sections).
 * SHM looked necessary at the time: every dma-buf-backed WPE surface, across
 * three different binaries including the untouched Phase 0 spike, rendered
 * nothing, with every EGL/GL/Wayland call along the way reporting success --
 * switching to SHM (real pixel bytes, no cross-process GPU buffer handle at
 * all) was what proved EGL/GL presentation itself was fine on this hardware
 * and let the elimination chain continue. It turned out dma-buf was never
 * actually the problem: the real causes (missing `render` group membership,
 * two missing WPE frame-pacing acks, no eglSwapInterval, three Wayland
 * protocol races) applied identically to both paths and were fixed without
 * ever touching the import mechanism. SHM's real cost only showed up after
 * the desktop was usable enough to interact with: every frame requires
 * WebKit to read its own GPU-composited output back into a CPU-side
 * shared-memory buffer, and this process to re-upload that back into a GPU
 * texture with glTexImage2D -- a full GPU->CPU->GPU round trip of the whole
 * surface, every frame, confirmed to be the actual lag once the rest of the
 * pipeline was correct. dma-buf/EGLImage shares the composited buffer's GPU
 * memory directly; the only thing that crosses the process boundary is a
 * handle, not the pixels themselves. */

#include "wlr-layer-shell-unstable-v1-client-protocol.h"
#include "xdg-shell-client-protocol.h"
#include "xdg-decoration-unstable-v1-client-protocol.h"

/* Same ceiling nethosd effectively assumes by never running more than a
 * handful of surfaces at once (5 shell surfaces + however many app windows
 * a person actually has open). A fixed array, not a linked list or a
 * realloc'd vector -- this whole program lives at the scale of "a few dozen
 * surfaces, tops", and a fixed bound is one fewer failure mode than a
 * dynamic one for something this close to the boot-time critical path. */
#define NETHOS_MAX_SURFACES 64
#define NETHOS_MAX_SPEC_LEN 2048

enum nethos_role { ROLE_PANEL, ROLE_DOCK, ROLE_OVERLAY, ROLE_WIDGET, ROLE_WINDOW };

struct nethos_spec {
    char url[1024];
    enum nethos_role role;
    char name[128];
    char title[128];
    char anchor[16];      /* "top"/"bottom"/"left"/"right"/"all"/"" */
    char layer[16];       /* "background"/"bottom"/"top"/"overlay" */
    int width, height;
    int exclusive;        /* -1 = "auto" sentinel handled by has_exclusive_auto */
    bool exclusive_auto;
    bool transparent;
    bool keyboard_off;    /* spec's keyboard=off */
};

struct nethos_surface {
    struct nethos_spec spec;

    struct wl_surface *wl_surface;

    /* layer-shell path (role != window) */
    struct zwlr_layer_surface_v1 *layer_surface;

    /* xdg_toplevel path (role == window) */
    struct xdg_surface *xdg_surface;
    struct xdg_toplevel *xdg_toplevel;
    struct zxdg_toplevel_decoration_v1 *decoration;

    int configured_w, configured_h;
    bool configured;
    bool visible;

    struct wl_egl_window *egl_window;
    EGLSurface egl_surface;
    GLuint tex;

    /* Non-NULL while waiting for the compositor to confirm it has actually
     * presented this surface's last swap -- see render_surface()'s comment
     * in surface.c for why this replaced eglSwapInterval(1). */
    struct wl_callback *frame_cb;

    struct wpe_view_backend_exportable_fdo *exportable;
    struct wpe_view_backend *wpe_backend;
    WebKitWebView *webview;

    /* input-rect clipping, applied via wl_surface_set_input_region */
    bool has_input_rect;
    int input_x, input_y, input_w, input_h;

    /* Wake-frames after show(), mirroring nethos-view's _wake ghost-frame
     * fix -- see bridge.c. */
    int wake_frames;
};

/* -- spec.c -- */
bool nethos_parse_spec(const char *text, struct nethos_spec *out);

/* -- surface.c -- */
struct nethos_surface *nethos_surface_create(const struct nethos_spec *spec);
void nethos_surface_render(struct nethos_surface *s);
void nethos_surface_repaint(struct nethos_surface *s);
void nethos_surface_paint_blank(struct nethos_surface *s);
void nethos_surface_destroy(struct nethos_surface *s);

/* -- bridge.c -- */
void nethos_bridge_install(struct nethos_surface *s);

/* -- wayland.c -- shared globals and registry/input plumbing */
extern struct wl_display *g_display;
extern struct wl_compositor *g_compositor;
extern struct zwlr_layer_shell_v1 *g_layer_shell;
extern struct xdg_wm_base *g_xdg_wm_base;
extern struct zxdg_decoration_manager_v1 *g_decoration_manager;
extern struct wl_seat *g_seat;

extern EGLDisplay g_egl_display;
extern EGLContext g_egl_context;
extern EGLConfig g_egl_config;

extern GLuint g_gl_prog, g_gl_vbo;

/* All live surfaces, shell and app windows alike -- tick/events fan-out and
 * apphost both need to walk this. Index 0 is always the first surface
 * created (the shell's "panel" role when running as the shell), matching
 * nethos-view's Surface._related_to being "whichever WebView came first". */
extern struct nethos_surface *g_surfaces[NETHOS_MAX_SURFACES];
extern int g_surface_count;
extern WebKitWebView *g_related_view; /* first view created; NULL until then */

int nethos_wayland_init(void);
void nethos_gl_setup(void); /* compile the blit shader once, first EGL makecurrent */
void nethos_egl_make_current(struct nethos_surface *s);

/* -- apphost.c -- */
void nethos_apphost_start(void);
char *nethos_apphost_socket_path(void); /* caller frees */

/* -- events.c -- */
void nethos_events_start(void); /* SSE thread + 1000ms tick timer */

/* -- settle.c -- */
void nethos_settle_wait(void);

/* -- session logging, shared by settle.c/apphost.c the way nethos-view's
 * _session_log() is shared across the Python file. */
void nethos_session_log(const char *fmt, ...);

#endif /* NETHOS_VIEW_H */
