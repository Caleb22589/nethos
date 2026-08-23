/* nethos-view-native -- entry point.
 *
 * Same command line as payload/bin/nethos-view (see its own module
 * docstring): bare positional SPEC strings, or --surface SPEC, plus
 * --apphost to run with no initial windows and accept specs over
 * $XDG_RUNTIME_DIR/nethos-apphost.sock. Wayfire only -- selected by
 * NETHOS_VIEW_IMPL=native in nethos-session; sway keeps the Python build.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <execinfo.h>
#include <unistd.h>

#include <glib.h>
#include <wpe/fdo.h>
#include <wpe/fdo-egl.h>

#include "nethos_view.h"

/* A crash here has nowhere else to report to -- there is no terminal
 * (Wayfire's autostart, stderr goes to the journal at best) and, unlike a
 * Python traceback, a bare segfault says nothing at all about where. This
 * is what actually found the one crash this rewrite hit during development
 * (a dangling stack pointer handed to WPE, see surface.c's s_egl_client) --
 * kept in rather than stripped back out, on the theory that a native
 * rewrite still short on hours of real-hardware runtime will hit more. */
static void crash_handler(int sig) {
    void *bt[32];
    int n = backtrace(bt, 32);
    fprintf(stderr, "\nnethos-view-native: crashed (signal %d)\n", sig);
    backtrace_symbols_fd(bt, n, 2);
    _exit(139);
}

static gboolean on_wl_readable(GIOChannel *chan, GIOCondition cond, gpointer data) {
    wl_display_dispatch(g_display);
    return TRUE;
}
static gboolean on_flush_tick(gpointer data) {
    wl_display_flush(g_display);
    return TRUE;
}

int main(int argc, char **argv) {
    signal(SIGSEGV, crash_handler);
    signal(SIGABRT, crash_handler);
    bool apphost = false;
    struct nethos_spec specs[NETHOS_MAX_SURFACES];
    int n_specs = 0;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--")) continue;
        if (!strcmp(argv[i], "--apphost")) { apphost = true; continue; }
        if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            printf("usage: nethos-view-native SPEC [SPEC ...] | --apphost\n");
            return 0;
        }
        if (!strcmp(argv[i], "--surface")) {
            i++;
            if (i >= argc) { fprintf(stderr, "--surface needs a spec\n"); return 2; }
        }
        if (n_specs >= NETHOS_MAX_SURFACES) { fprintf(stderr, "too many surfaces\n"); return 2; }
        if (!nethos_parse_spec(argv[i], &specs[n_specs])) {
            fprintf(stderr, "surface spec needs url=: %s\n", argv[i]);
            return 2;
        }
        n_specs++;
    }

    if (n_specs == 0 && !apphost) {
        printf("usage: nethos-view-native SPEC [SPEC ...] | --apphost\n");
        return 2;
    }

    if (nethos_wayland_init() != 0) return 1;
    if (!wpe_fdo_initialize_for_egl_display(g_egl_display)) {
        fprintf(stderr, "nethos-view-native: wpe_fdo_initialize_for_egl_display failed\n");
        return 1;
    }

    bool is_shell = false;
    for (int i = 0; i < n_specs; i++) if (specs[i].role == ROLE_PANEL) is_shell = true;
    if (is_shell) {
        nethos_session_log("do_activate: entered (native)");
        nethos_settle_wait();
    }

    for (int i = 0; i < n_specs; i++) nethos_surface_create(&specs[i]);

    if (apphost || is_shell) nethos_apphost_start();
    nethos_events_start();

    GMainLoop *loop = g_main_loop_new(NULL, FALSE);
    GIOChannel *wl_chan = g_io_channel_unix_new(wl_display_get_fd(g_display));
    g_io_add_watch(wl_chan, G_IO_IN, on_wl_readable, NULL);
    g_timeout_add(16, on_flush_tick, NULL);
    g_main_loop_run(loop);
    return 0;
}
