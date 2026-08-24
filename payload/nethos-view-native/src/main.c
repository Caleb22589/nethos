/* nethos-view-native -- entry point.
 *
 * Same command line as payload/bin/nethos-view (see its own module
 * docstring): bare positional SPEC strings, or --surface SPEC, plus
 * --apphost to run with no initial windows and accept specs over
 * $XDG_RUNTIME_DIR/nethos-apphost.sock. Wayfire only -- selected by
 * NETHOS_VIEW_IMPL=native in nethos-session; sway keeps the Python build.
 *
 * GtkApplication with G_APPLICATION_NON_UNIQUE, not application_id=NULL with
 * no flags the way the Python version's Gtk.Application(application_id=None,
 * flags=0) reads -- GApplication requires NON_UNIQUE to accept a NULL id at
 * all; PyGObject is evidently more forgiving about the omission than the C
 * API is. Uniqueness itself is not wanted here either way: this process's
 * own apphost socket (apphost.c) is what lets nethosd hand it more windows
 * to open, an entirely separate mechanism from GApplication's D-Bus-based
 * single-instance activation.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <execinfo.h>
#include <unistd.h>

#include "nethos_view.h"

GtkApplication *g_app;

static struct nethos_spec g_specs[NETHOS_MAX_SURFACES];
static int g_n_specs;
static bool g_apphost;

/* A crash here has nowhere else to report to -- there is no terminal
 * (Wayfire's autostart, stderr goes to the journal at best) and, unlike a
 * Python traceback, a bare segfault says nothing at all about where. Kept
 * from Phase 1 on the same theory: a rewrite still short on hours of
 * real-hardware runtime will hit more before it hits none. */
static void crash_handler(int sig) {
    void *bt[32];
    int n = backtrace(bt, 32);
    fprintf(stderr, "\nnethos-view-native: crashed (signal %d)\n", sig);
    backtrace_symbols_fd(bt, n, 2);
    _exit(139);
}

/* Direct port of App.do_activate(). */
static void on_activate(GApplication *app, gpointer user_data) {
    bool is_shell = false;
    for (int i = 0; i < g_n_specs; i++) if (g_specs[i].role == ROLE_PANEL) is_shell = true;
    if (is_shell) {
        nethos_session_log("do_activate: entered (native/webkitgtk)");
        nethos_settle_wait();
    }

    for (int i = 0; i < g_n_specs; i++) nethos_surface_create(&g_specs[i]);

    /* hold() keeps the process running with zero *extra* windows between app
     * launches -- GApplication would otherwise treat activate() finishing
     * with no windows as "done". Harmless, if redundant, for the shell,
     * which always has real surfaces of its own by this point. */
    if (g_apphost || is_shell) {
        g_application_hold(app);
        nethos_apphost_start();
    }
    nethos_events_start();
}

int main(int argc, char **argv) {
    signal(SIGSEGV, crash_handler);
    signal(SIGABRT, crash_handler);

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--")) continue;
        if (!strcmp(argv[i], "--apphost")) { g_apphost = true; continue; }
        if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            printf("usage: nethos-view-native SPEC [SPEC ...] | --apphost\n");
            return 0;
        }
        if (!strcmp(argv[i], "--surface")) {
            i++;
            if (i >= argc) { fprintf(stderr, "--surface needs a spec\n"); return 2; }
        }
        if (g_n_specs >= NETHOS_MAX_SURFACES) { fprintf(stderr, "too many surfaces\n"); return 2; }
        if (!nethos_parse_spec(argv[i], &g_specs[g_n_specs])) {
            fprintf(stderr, "surface spec needs url=: %s\n", argv[i]);
            return 2;
        }
        g_n_specs++;
    }

    if (g_n_specs == 0 && !g_apphost) {
        printf("usage: nethos-view-native SPEC [SPEC ...] | --apphost\n");
        return 2;
    }

    if (!gtk_layer_is_supported())
        fprintf(stderr, "nethos-view-native: warning: compositor does not support "
                "zwlr_layer_shell_v1; panels will fall back to plain windows\n");

    g_app = gtk_application_new(NULL, G_APPLICATION_NON_UNIQUE);
    g_signal_connect(g_app, "activate", G_CALLBACK(on_activate), NULL);
    int status = g_application_run(G_APPLICATION(g_app), 0, NULL);
    g_object_unref(g_app);
    return status;
}
