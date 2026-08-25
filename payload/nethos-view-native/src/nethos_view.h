/* nethos-view-native -- shared types.
 *
 * Second architecture for this rewrite. Phase 1 hosted WPE WebKit directly
 * against raw Wayland/EGL -- no GTK, no toolkit, this process doing every
 * bit of compositor-client plumbing (layer-shell requests, EGLImage import,
 * frame-callback pacing) by hand. It worked, but WPE's headless-view-backend
 * model turned out to need bugs found and fixed one at a time that GTK's own
 * WebKitGTK port simply does not have to begin with -- see
 * docs/NETHOS-VIEW-REWRITE.md's long account of the white-window bug and the
 * lag that followed it. This version hosts WebKitGTK inside real GtkWindows
 * instead, exactly the engine payload/bin/nethos-view (Python) already runs
 * successfully, and lets GTK/gtk4-layer-shell own the compositor-client side
 * entirely -- this process never touches wl_surface, EGL or a frame callback
 * itself again. What's kept from Phase 1 is everything that was never
 * WPE-specific in the first place: the spec grammar (spec.c, unchanged), the
 * apphost socket (apphost.c, unchanged), the settle wait (settle.c,
 * unchanged), session logging (log.c, unchanged), and the tick/SSE fan-out
 * (events.c, unchanged apart from the wake-before-evaluate calls Python's
 * _deliver() already established as necessary).
 *
 * Mirrors payload/bin/nethos-view's data model closely enough that anyone
 * who already knows that file can read this one: a `spec` is the same
 * key=value grammar, a `surface` is the same thing Surface.__init__ builds.
 */
#ifndef NETHOS_VIEW_H
#define NETHOS_VIEW_H

#include <stdbool.h>
#include <stdint.h>

#include <gtk/gtk.h>
#include <webkit/webkit.h>
#include <gtk4-layer-shell.h>

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
    bool webgl;
    bool keyboard_off;    /* spec's keyboard=off */
};

struct nethos_surface {
    struct nethos_spec spec;

    GtkWindow *window;
    WebKitWebView *webview;

    /* Wake-frames after show(), mirroring nethos-view's _wake ghost-frame
     * fix -- see bridge.c. guint so 0 is a safe "no timer" sentinel
     * (g_source ids are never 0). */
    int wake_frames;
    guint wake_source;
};

/* -- spec.c -- unchanged from Phase 1 */
bool nethos_parse_spec(const char *text, struct nethos_spec *out);

/* -- surface.c -- */
struct nethos_surface *nethos_surface_create(const struct nethos_spec *spec);

/* -- bridge.c -- */
void nethos_bridge_install(struct nethos_surface *s);

/* All live surfaces, shell and app windows alike -- tick/events fan-out and
 * apphost both need to walk this. Index 0 is always the first surface
 * created (the shell's "panel" role when running as the shell), matching
 * nethos-view's Surface._related_to being "whichever WebView came first". */
extern struct nethos_surface *g_surfaces[NETHOS_MAX_SURFACES];
extern int g_surface_count;
extern WebKitWebView *g_related_view; /* first view created; NULL until then */
extern GtkApplication *g_app;

/* -- apphost.c -- unchanged from Phase 1 */
void nethos_apphost_start(void);
char *nethos_apphost_socket_path(void); /* caller frees */

/* -- events.c -- unchanged from Phase 1 */
void nethos_events_start(void); /* SSE thread + 1000ms tick timer */

/* -- settle.c -- unchanged from Phase 1 */
void nethos_settle_wait(void);

/* -- log.c -- unchanged from Phase 1, session logging shared across the
 * file the way nethos-view's _session_log() is shared across the Python
 * one. */
void nethos_session_log(const char *fmt, ...);

#endif /* NETHOS_VIEW_H */
