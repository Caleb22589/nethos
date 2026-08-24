/* The nethosHost JS bridge -- direct port of _install_bridge()/_on_message()
 * in payload/bin/nethos-view. shell.js (unmodified) calls
 * window.nethosHost.exclusive/inputRect/hide/show/repaint/keyboard from many
 * places; every one of those call sites is load-bearing and none of it
 * changes here. Wire format matches the Python version exactly (a
 * JSON-stringified postMessage) rather than Phase 1's plain-object shim,
 * since WebKitGTK's script-message-received handler hands this process the
 * same JSCValue* either way and there is no reason to diverge from the
 * reference implementation now that nothing about the message path is
 * WPE-specific any more.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include <jsc/jsc.h>

#include "nethos_view.h"

extern const char *nethos_read_theme(char *buf, size_t n); /* surface.c */

/* Commit a frame, whatever the page thinks it is doing -- direct port of
 * Surface._repaint(). Both halves of what a page changes without an
 * explicit draw -- pixels and input region -- only reach the compositor on
 * a commit, and GTK/gtk4-layer-shell only commits one when something
 * actually draws. queue_draw on both the window and the view is what
 * nethos-view calls after every input-region change and after show(), for
 * exactly this reason: an overlay surface is deliberately never unmapped
 * (an unmapped surface's page is suspended and stops hearing events), so
 * dismissing something by hiding a div and releasing the input region does
 * nothing to the compositor at all unless something forces a repaint. */
static void repaint(struct nethos_surface *s) {
    gtk_widget_queue_draw(GTK_WIDGET(s->window));
    gtk_widget_queue_draw(GTK_WIDGET(s->webview));
}

/* Limit clicks to part of the surface -- direct port of
 * Surface._set_input_rect(). An auto-hiding dock is a full-size transparent
 * surface most of the time; without this it swallows every click aimed at
 * the window underneath it. w<=0 or h<=0 means an empty (not NULL) region:
 * NULL means "no restriction, whole surface" per GDK, the opposite of what
 * shell.js means by inputRect(0,0,0,0) -- see the region-vs-NULL history in
 * docs/NETHOS-VIEW-REWRITE.md, the same distinction that mattered under raw
 * Wayland matters here too, cairo_region_create() with nothing added to it
 * being the "genuinely empty, click-through" shape rather than a NULL that
 * would mean the opposite. */
static void set_input_rect(struct nethos_surface *s, int x, int y, int w, int h) {
    GdkSurface *surface = gtk_native_get_surface(GTK_NATIVE(s->window));
    if (!surface) return;
    cairo_region_t *region = cairo_region_create();
    if (w > 0 && h > 0) {
        cairo_rectangle_int_t rect = { x, y, w, h };
        cairo_region_union_rectangle(region, &rect);
    }
    gdk_surface_set_input_region(surface, region);
    cairo_region_destroy(region);
    /* Region changes are queued until the next commit -- same reason
     * _set_input_rect() in nethos-view calls _repaint() at the end. */
    repaint(s);
}

/* Keep drawing for a moment after show() -- direct port of the Python
 * version's _wake() nested function and its own long comment on why: a
 * surface keeps its last buffer while unmapped and presents it again on
 * map, so what appears first is whatever was on screen when it went away,
 * not the new content -- the launcher, when the control centre is what is
 * being opened. The frame that replaces it does not damage the output on
 * its own (the same "ghost" bug elsewhere in this project), so a single
 * queue_draw here loses the race. Twelve redraws at 25ms, cheap and
 * bounded, gives the page and the compositor several chances instead of
 * one and then stops on its own. */
static gboolean wake_tick(gpointer data) {
    struct nethos_surface *s = data;
    if (s->wake_frames <= 0) { s->wake_source = 0; return G_SOURCE_REMOVE; }
    s->wake_frames--;
    repaint(s);
    return G_SOURCE_CONTINUE;
}

/* WebKitGTK hands script-message-received the same real JSCValue* WPE did --
 * JSC bindings are shared WebKit infrastructure, not port-specific -- so
 * direct property access works exactly the way it did in Phase 1's bridge.c,
 * no JSON round-trip (and no new json-glib dependency) needed just because
 * the wire shape above now matches the Python version's JSON.stringify
 * rather than Phase 1's plain object. jsc_value_to_json() decodes what the
 * page already encoded back into a real JS object WebKit hands over as a
 * JSCValue either way. */
static void on_message(WebKitUserContentManager *mgr, JSCValue *value, gpointer data) {
    struct nethos_surface *s = data;
    char *json_text = jsc_value_to_string(value);
    JSCValue *parsed = jsc_value_new_from_json(jsc_value_get_context(value), json_text);
    free(json_text);
    if (!jsc_value_is_object(parsed)) { g_object_unref(parsed); return; }

    JSCValue *type_v = jsc_value_object_get_property(parsed, "type");
    if (!type_v || !jsc_value_is_string(type_v)) {
        if (type_v) g_object_unref(type_v);
        g_object_unref(parsed);
        return;
    }
    char *type = jsc_value_to_string(type_v);
    g_object_unref(type_v);

    if (!strcmp(type, "exclusive") && s->spec.role != ROLE_WINDOW) {
        JSCValue *v = jsc_value_object_get_property(parsed, "value");
        int n = v ? (int)jsc_value_to_double(v) : 0;
        if (v) g_object_unref(v);
        gtk_layer_set_exclusive_zone(s->window, n);
    } else if (!strcmp(type, "input_rect")) {
        JSCValue *vx = jsc_value_object_get_property(parsed, "x");
        JSCValue *vy = jsc_value_object_get_property(parsed, "y");
        JSCValue *vw = jsc_value_object_get_property(parsed, "w");
        JSCValue *vh = jsc_value_object_get_property(parsed, "h");
        set_input_rect(s,
            vx ? (int)jsc_value_to_double(vx) : 0, vy ? (int)jsc_value_to_double(vy) : 0,
            vw ? (int)jsc_value_to_double(vw) : 0, vh ? (int)jsc_value_to_double(vh) : 0);
        if (vx) g_object_unref(vx);
        if (vy) g_object_unref(vy);
        if (vw) g_object_unref(vw);
        if (vh) g_object_unref(vh);
    } else if (!strcmp(type, "keyboard") && s->spec.role != ROLE_WINDOW) {
        /* ON_DEMAND hands the keyboard to a surface only when clicked -- fine
         * for a panel, wrong for a search box. EXCLUSIVE only while
         * something wants it: this surface is never unmapped, so leaving it
         * exclusive would starve every application for the session. */
        JSCValue *v = jsc_value_object_get_property(parsed, "on");
        bool on = v && jsc_value_to_boolean(v);
        if (v) g_object_unref(v);
        gtk_layer_set_keyboard_mode(s->window,
            on ? GTK_LAYER_SHELL_KEYBOARD_MODE_EXCLUSIVE : GTK_LAYER_SHELL_KEYBOARD_MODE_ON_DEMAND);
        if (on) gtk_window_present(s->window);
    } else if (!strcmp(type, "repaint")) {
        repaint(s);
    } else if (!strcmp(type, "hide")) {
        gtk_widget_set_visible(GTK_WIDGET(s->window), FALSE);
    } else if (!strcmp(type, "show")) {
        gtk_widget_set_visible(GTK_WIDGET(s->window), TRUE);
        repaint(s);
        gtk_window_present(s->window);
        s->wake_frames = 12;
        if (!s->wake_source) s->wake_source = g_timeout_add(25, wake_tick, s);
    }
    free(type);
    g_object_unref(parsed);
}

void nethos_bridge_install(struct nethos_surface *s) {
    WebKitUserContentManager *mgr = webkit_web_view_get_user_content_manager(s->webview);
    g_signal_connect(mgr, "script-message-received::nethosHost", G_CALLBACK(on_message), s);
    webkit_user_content_manager_register_script_message_handler(mgr, "nethosHost", NULL);

    char theme_buf[16];
    const char *theme = nethos_read_theme(theme_buf, sizeof(theme_buf));

    char *name_js = g_strdup_printf("\"%s\"", s->spec.name);
    char *theme_js = g_strdup_printf("\"%s\"", theme);

    char *shim = g_strdup_printf(
        "window.nethosHost = {\n"
        "  exclusive: (n) => window.webkit.messageHandlers.nethosHost.postMessage(\n"
        "      JSON.stringify({type: 'exclusive', value: n})),\n"
        "  inputRect: (x,y,w,h) => window.webkit.messageHandlers.nethosHost.postMessage(\n"
        "      JSON.stringify({type: 'input_rect', x:x, y:y, w:w, h:h})),\n"
        "  hide: () => window.webkit.messageHandlers.nethosHost.postMessage(\n"
        "      JSON.stringify({type: 'hide'})),\n"
        "  show: () => window.webkit.messageHandlers.nethosHost.postMessage(\n"
        "      JSON.stringify({type: 'show'})),\n"
        "  repaint: () => window.webkit.messageHandlers.nethosHost.postMessage(\n"
        "      JSON.stringify({type: 'repaint'})),\n"
        "  keyboard: (on) => window.webkit.messageHandlers.nethosHost.postMessage(\n"
        "      JSON.stringify({type: 'keyboard', on: !!on})),\n"
        "  surface: %s,\n"
        "};\n"
        "if (%s) { document.documentElement.classList.add('neth-gpu'); }\n"
        "var _t = %s; if (_t) { document.documentElement.classList.add('neth-' + _t); }\n"
        /* Wayfire-only scope: this always runs under Wayfire, which always
         * blurs behind a transparent layer surface itself -- unlike the
         * Python version, which also has to run under sway/Hyprland and
         * checks the environment for which one is live. */
        "document.documentElement.classList.add('neth-compositor-blur');\n",
        name_js, getenv("NETHOS_GPU") ? "true" : "false", theme_js);

    WebKitUserScript *script = webkit_user_script_new(
        shim, WEBKIT_USER_CONTENT_INJECT_TOP_FRAME, WEBKIT_USER_SCRIPT_INJECT_AT_DOCUMENT_START,
        NULL, NULL);
    webkit_user_content_manager_add_script(mgr, script);
    webkit_user_script_unref(script);

    g_free(name_js);
    g_free(theme_js);
    g_free(shim);
}
