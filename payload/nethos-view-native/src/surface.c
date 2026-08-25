/* Per-surface creation -- direct port of Surface.__init__ in
 * payload/bin/nethos-view, restricted to the Wayfire-only scope
 * docs/NETHOS-VIEW-REWRITE.md settled on: no _own_chrome (sway keeps the
 * Python build for that -- Wayfire's firedecor decorates role=window
 * surfaces itself, which is what leaving GTK's own decorated=TRUE default in
 * place actually negotiates over xdg-decoration), no minimize/maximize
 * button wiring (firedecor's own buttons already do this under Wayfire).
 *
 * All the compositor-client work Phase 1 did by hand -- layer-shell
 * requests, buffer import, frame-callback pacing -- is now GTK's and
 * gtk4-layer-shell's job. This file only ever builds a GtkWindow, points
 * gtk4-layer-shell at it for role != window, packs a WebKitWebView in as its
 * child, and loads a URL. See nethos_view.h for why.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "nethos_view.h"

struct nethos_surface *g_surfaces[NETHOS_MAX_SURFACES];
int g_surface_count;
WebKitWebView *g_related_view;

/* No browser menu anywhere in the shell -- direct port of the Python
 * version's `lambda *_a: True` connected to the same signal. Returning TRUE
 * suppresses WebKit's own default context menu (Reload, Go Back, Inspect
 * Element); the shell draws its own in JavaScript instead, where it knows
 * what was actually clicked. */
static gboolean suppress_context_menu(void) { return TRUE; }

/* Same file the Python _theme() reads, same "before first paint" reasoning:
 * a theme applied after the page has already rendered shows as a flash of
 * the wrong one. Shared with bridge.c's shim, which is why it lives here
 * rather than static in either file. */
const char *nethos_read_theme(char *buf, size_t n) {
    const char *home = getenv("HOME");
    if (!home) return "";
    char path[1024];
    snprintf(path, sizeof(path), "%s/.config/nethos/theme", home);
    FILE *f = fopen(path, "r");
    if (!f) return "";
    size_t len = fread(buf, 1, n - 1, f);
    fclose(f);
    buf[len] = '\0';
    while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == ' ')) buf[--len] = '\0';
    if (strcmp(buf, "light") == 0 || strcmp(buf, "dark") == 0) return buf;
    return "";
}

/* Set once, globally, the first time any surface needs it -- matches
 * Surface._transparent_css's class-level guard in the Python version. GTK
 * paints the theme's own background square behind a window otherwise; a
 * layer surface with no opaque page over it would show that square instead
 * of the desktop, and even an opaque one would show it as a flash before
 * first paint. */
static void ensure_transparent_css(void) {
    static bool installed;
    if (installed) return;
    installed = true;

    char theme_buf[16];
    bool dark = strcmp(nethos_read_theme(theme_buf, sizeof(theme_buf)), "dark") == 0;

    GString *css = g_string_new(
        "window, window.background, .background {"
        "  background-color: transparent; background-image: none;"
        "}");
    (void)dark; /* NETHOS's own CSS (nethos.css) handles light/dark for page
                 * content; this provider only ever needs to defeat GTK's
                 * opaque window background, which has no light/dark variant
                 * of its own to pick between. */

    GtkCssProvider *provider = gtk_css_provider_new();
    gtk_css_provider_load_from_string(provider, css->str);
    gtk_style_context_add_provider_for_display(
        gdk_display_get_default(), GTK_STYLE_PROVIDER(provider),
        GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
    g_string_free(css, TRUE);
    g_object_unref(provider);
}

/* ---- layer shell -- direct port of Surface._init_layer_shell ---- */
static void init_layer_shell(struct nethos_surface *s) {
    const struct nethos_spec *spec = &s->spec;
    char ns[160];
    snprintf(ns, sizeof(ns), "nethos-%s", spec->name);

    gtk_layer_init_for_window(s->window);
    gtk_layer_set_namespace(s->window, ns);

    GtkLayerShellLayer layer = GTK_LAYER_SHELL_LAYER_TOP;
    if (!strcmp(spec->layer, "background")) layer = GTK_LAYER_SHELL_LAYER_BACKGROUND;
    else if (!strcmp(spec->layer, "bottom")) layer = GTK_LAYER_SHELL_LAYER_BOTTOM;
    else if (!strcmp(spec->layer, "overlay")) layer = GTK_LAYER_SHELL_LAYER_OVERLAY;
    gtk_layer_set_layer(s->window, layer);

    bool fullscreen = !strcmp(spec->anchor, "all")
        || spec->role == ROLE_OVERLAY || spec->role == ROLE_WIDGET;
    if (fullscreen) {
        gtk_layer_set_anchor(s->window, GTK_LAYER_SHELL_EDGE_TOP, TRUE);
        gtk_layer_set_anchor(s->window, GTK_LAYER_SHELL_EDGE_BOTTOM, TRUE);
        gtk_layer_set_anchor(s->window, GTK_LAYER_SHELL_EDGE_LEFT, TRUE);
        gtk_layer_set_anchor(s->window, GTK_LAYER_SHELL_EDGE_RIGHT, TRUE);
    } else if (*spec->anchor) {
        GtkLayerShellEdge edge = !strcmp(spec->anchor, "top") ? GTK_LAYER_SHELL_EDGE_TOP
            : !strcmp(spec->anchor, "bottom") ? GTK_LAYER_SHELL_EDGE_BOTTOM
            : !strcmp(spec->anchor, "left") ? GTK_LAYER_SHELL_EDGE_LEFT : GTK_LAYER_SHELL_EDGE_RIGHT;
        gtk_layer_set_anchor(s->window, edge, TRUE);
        /* Stretch across the perpendicular axis so a top panel spans the
         * whole width rather than sitting in a corner. */
        if (edge == GTK_LAYER_SHELL_EDGE_TOP || edge == GTK_LAYER_SHELL_EDGE_BOTTOM) {
            gtk_layer_set_anchor(s->window, GTK_LAYER_SHELL_EDGE_LEFT, TRUE);
            gtk_layer_set_anchor(s->window, GTK_LAYER_SHELL_EDGE_RIGHT, TRUE);
        } else {
            gtk_layer_set_anchor(s->window, GTK_LAYER_SHELL_EDGE_TOP, TRUE);
            gtk_layer_set_anchor(s->window, GTK_LAYER_SHELL_EDGE_BOTTOM, TRUE);
        }
    }

    if (spec->exclusive_auto) gtk_layer_auto_exclusive_zone_enable(s->window);
    else gtk_layer_set_exclusive_zone(s->window, spec->exclusive);

    /* ON_DEMAND on every layer surface, not only the ones that want the
     * keyboard -- see the Python version's own long comment on this: with
     * KEYBOARD_MODE_NONE the compositor still delivers pointer motion
     * (hover, tooltips, right-click) but never a button press outside an
     * exclusive zone, which reads as "half the panel doesn't click" with
     * nothing logged anywhere. */
    if (!spec->keyboard_off)
        gtk_layer_set_keyboard_mode(s->window, GTK_LAYER_SHELL_KEYBOARD_MODE_ON_DEMAND);

    /* Only fix a size on surfaces anchored to one edge; a fullscreen layer
     * takes its size from the output. */
    if (!fullscreen) {
        if (spec->height > 0) gtk_widget_set_size_request(GTK_WIDGET(s->window), -1, spec->height);
        if (spec->width > 0) gtk_widget_set_size_request(GTK_WIDGET(s->window), spec->width, -1);
    }
}

struct nethos_surface *nethos_surface_create(const struct nethos_spec *spec) {
    if (g_surface_count >= NETHOS_MAX_SURFACES) {
        fprintf(stderr, "nethos-view-native: too many surfaces, dropping %s\n", spec->name);
        return NULL;
    }
    struct nethos_surface *s = calloc(1, sizeof(*s));
    s->spec = *spec;

    /* Wayland has no way to change a toplevel's app_id after the surface is
     * realised, and this process hosts many different app windows one after
     * another -- g_set_prgname() right before each present() is what
     * payload/bin/nethos-view already relies on to give each its own
     * (GTK reads prgname at realization time, not process start; setting it
     * fresh per window is what makes that safe across several toplevels in
     * one process). Only for role=window: layer surfaces are identified by
     * namespace (set below), not app_id. */
    if (spec->role == ROLE_WINDOW) {
        char prgname[160];
        snprintf(prgname, sizeof(prgname), "nethos-%s", spec->name);
        g_set_prgname(prgname);
    }

    s->window = GTK_WINDOW(gtk_application_window_new(g_app));
    gtk_window_set_title(s->window, spec->title);
    gtk_window_set_default_size(s->window, spec->width > 0 ? spec->width : 800,
                                 spec->height > 0 ? spec->height : 600);

    /* Undecorated only where nothing else draws chrome: every layer-shell
     * surface (gtk4-layer-shell owns the surface role; xdg-decoration does
     * not apply to it anyway). role=window is left at GTK's own
     * decorated=TRUE default, which is what actually sends the SERVER_SIDE
     * xdg-decoration request firedecor answers -- see nethos-view's own
     * long comment on exactly this (decorated=FALSE asks for CLIENT_SIDE,
     * the opposite of what a firedecor-decorated window needs). */
    if (spec->role != ROLE_WINDOW) gtk_window_set_decorated(s->window, FALSE);

    ensure_transparent_css();
    if (spec->role != ROLE_WINDOW) gtk_widget_add_css_class(GTK_WIDGET(s->window), "background");

    /* webkit_web_view_new_with_related_view()'s C constructor is not GI's
     * pick from Python, but is exactly what "related-view" is here: a
     * construct-only property, reachable the same way from C directly.
     * Sharing one WebProcess across every shell surface (and every app
     * window launched through nethosd's real path) is the ~500MB saving
     * documented at length in payload/bin/nethos-view's own comment on this
     * same property. */
    const char *share_env = getenv("NETHOS_SHARE_WEBPROCESS");
    bool share = !(share_env && !strcmp(share_env, "0"));

    /* A surface that asks for WebGL does not share. Sharing is what makes one
     * crash take down the whole shell, and the GL surface is the one likely to
     * crash: with WebGL on, the web process dies on reload on this hardware and
     * every other surface -- dock, desktop, menu -- died with it, because they
     * were all the same process. Isolated, the worst case is a panel that
     * falls back to no panel while the rest of the shell keeps running.
     *
     * It costs a second web process (~180MB measured). Only the panel asks. */
    if (spec->webgl) share = false;

    if (share && g_related_view) {
        s->webview = WEBKIT_WEB_VIEW(g_object_new(WEBKIT_TYPE_WEB_VIEW,
            "related-view", g_related_view, NULL));
    } else {
        s->webview = WEBKIT_WEB_VIEW(g_object_new(WEBKIT_TYPE_WEB_VIEW, NULL));
        /* An isolated view must never become the one everything else relates
         * to, or the isolation inverts and the whole shell joins it. */
        if (!g_related_view && !spec->webgl) g_related_view = s->webview;
    }

    /* No browser menu anywhere in the shell -- see nethos-view's own
     * comment on why: WebKit's default context menu (Reload, Go Back,
     * Inspect Element) says "this is a web page pretending to be an
     * operating system" the moment someone right-clicks the dock. Developer
     * tools stay reachable deliberately, only when asked for. */
    if (!getenv("NETHOS_INSPECTOR") || strcmp(getenv("NETHOS_INSPECTOR"), "1"))
        g_signal_connect(s->webview, "context-menu", G_CALLBACK(suppress_context_menu), NULL);

    WebKitSettings *settings = webkit_web_view_get_settings(s->webview);
    webkit_settings_set_enable_developer_extras(settings, TRUE);
    webkit_settings_set_enable_write_console_messages_to_stdout(settings, TRUE);
    webkit_settings_set_enable_media(settings, FALSE);
    webkit_settings_set_enable_webaudio(settings, FALSE);
    /* WebGL is off by default for the same reason as media and webaudio: a
     * shell surface has no use for it, and every surface here shares one web
     * process, so the machinery would be kept warm for four surfaces that
     * never touch it. The panel asks for it with webgl=1 so that it, and only
     * it, pays -- see shell/liquid.js. */
    webkit_settings_set_enable_webgl(settings, spec->webgl ? TRUE : FALSE);
    webkit_settings_set_media_playback_requires_user_gesture(settings, TRUE);
    webkit_settings_set_enable_page_cache(settings, FALSE);
    webkit_settings_set_enable_html5_database(settings, FALSE);
    webkit_settings_set_enable_html5_local_storage(settings, TRUE);
    webkit_settings_set_javascript_can_open_windows_automatically(settings, FALSE);
    webkit_settings_set_enable_back_forward_navigation_gestures(settings, FALSE);

    if (spec->transparent) {
        /* Field assignment, not GdkRGBA's constructor -- that's a PyGObject
         * boxed-type quirk that doesn't apply here, but the fields
         * themselves are still the real API. */
        GdkRGBA transparent = { 0.0f, 0.0f, 0.0f, 0.0f };
        webkit_web_view_set_background_color(s->webview, &transparent);
    }

    nethos_bridge_install(s);

    gtk_window_set_child(s->window, GTK_WIDGET(s->webview));
    if (spec->role != ROLE_WINDOW) init_layer_shell(s);

    webkit_web_view_load_uri(s->webview, spec->url);
    gtk_window_present(s->window);

    g_surfaces[g_surface_count++] = s;
    return s;
}
