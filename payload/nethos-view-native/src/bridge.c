/* The nethosHost JS bridge -- direct behavioural port of _install_bridge()/
 * _on_message() in payload/bin/nethos-view. shell.js (unmodified, 1670
 * lines) calls window.nethosHost.exclusive/inputRect/hide/show/repaint/
 * keyboard from many places; every one of those call sites is load-bearing
 * (see the grep of shell.js recorded in docs/NETHOS-VIEW-REWRITE.md's
 * Phase 1 notes) and none of it changes here.
 *
 * Wire format differs from the Python version on purpose: that code
 * JSON.stringifies the message and json.loads()s it back because PyGObject
 * only exposes WebKitJavascriptResult as a JSON string. WPE's JSC API
 * (jsc.h) hands the script-message-received signal a real JSCValue*, so
 * this posts a plain JS object and reads its properties directly -- no
 * hand-rolled JSON parser needed. shell.js never sees the difference; it
 * only ever calls the nethosHost.* functions below, never touches the wire
 * shape itself.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include <jsc/jsc.h>

#include "nethos_view.h"

/* Same file the Python _theme() reads, same "before first paint" reasoning:
 * a theme applied after the page has already rendered shows as a flash of
 * the wrong one. */
static const char *read_theme(char *buf, size_t n) {
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

static void set_input_region(struct nethos_surface *s, int x, int y, int w, int h) {
    /* A NULL region means "no restriction, whole surface" per the wl_surface
     * protocol -- the opposite of what shell.js means by inputRect(0,0,0,0).
     * shell.js calls that specifically to make an idle overlay fully
     * click-through (menu.html, splash.html -- see its own comments on
     * overlayMapped()/nethosHost.inputRect(0,0,0,0)). Passing NULL here
     * left those two full-screen, always-on-top ZWLR_LAYER_SHELL_V1_LAYER_
     * OVERLAY surfaces capturing every click across the whole screen
     * indefinitely -- confirmed live: real hardware clicks aimed at a
     * window (Settings) never arrived, because ptr_enter/ptr_button always
     * matched splash or menu first. An *empty* region (a real wl_region
     * object with zero rectangles added to it) is what actually makes a
     * surface pass every click through to whatever is beneath it. */
    struct wl_region *region = wl_compositor_create_region(g_compositor);
    if (w > 0 && h > 0) wl_region_add(region, x, y, w, h);
    wl_surface_set_input_region(s->wl_surface, region);
    wl_region_destroy(region);
    s->has_input_rect = true;
    s->input_x = x; s->input_y = y; s->input_w = w; s->input_h = h;
    /* Region changes are queued until the next commit -- same reason
     * _set_input_rect() in nethos-view calls _repaint() at the end. */
    nethos_surface_repaint(s);
}

static void on_message(WebKitUserContentManager *mgr, JSCValue *value, gpointer data) {
    struct nethos_surface *s = data;
    if (!jsc_value_is_object(value)) return;

    JSCValue *type_v = jsc_value_object_get_property(value, "type");
    if (!type_v || !jsc_value_is_string(type_v)) { if (type_v) g_object_unref(type_v); return; }
    char *type = jsc_value_to_string(type_v);
    g_object_unref(type_v);

    if (strcmp(type, "exclusive") == 0 && s->spec.role != ROLE_WINDOW && s->layer_surface) {
        JSCValue *v = jsc_value_object_get_property(value, "value");
        int n = v ? (int)jsc_value_to_double(v) : 0;
        if (v) g_object_unref(v);
        zwlr_layer_surface_v1_set_exclusive_zone(s->layer_surface, n);
        /* nethos_surface_repaint(), not a direct commit -- it is the one
         * that already checks s->configured (see input_rect below). shell.js
         * calls nethosHost.exclusive() as soon as it has measured its own
         * content, which easily races ahead of this surface's first
         * layer_surface configure/ack round-trip. A commit that lands before
         * that ack is a *fatal* Wayland protocol error under Wayfire
         * ("layer_surface has never been configured"), and a fatal error on
         * one surface tears down the whole wl_display connection -- every
         * other surface on the same connection goes down with it, which is
         * why the entire native shell (not just the panel) stayed an inert
         * void colour with the UI process spinning near 100% CPU (retrying
         * dispatch on a socket that had already been killed) even after the
         * frame-pacing fix below made a single ordinary window render
         * correctly. Confirmed live via the exact error text in the
         * process's own stderr log. */
        nethos_surface_repaint(s);
    } else if (strcmp(type, "input_rect") == 0) {
        JSCValue *vx = jsc_value_object_get_property(value, "x");
        JSCValue *vy = jsc_value_object_get_property(value, "y");
        JSCValue *vw = jsc_value_object_get_property(value, "w");
        JSCValue *vh = jsc_value_object_get_property(value, "h");
        set_input_region(s,
            vx ? (int)jsc_value_to_double(vx) : 0, vy ? (int)jsc_value_to_double(vy) : 0,
            vw ? (int)jsc_value_to_double(vw) : 0, vh ? (int)jsc_value_to_double(vh) : 0);
        if (vx) g_object_unref(vx);
        if (vy) g_object_unref(vy);
        if (vw) g_object_unref(vw);
        if (vh) g_object_unref(vh);
    } else if (strcmp(type, "keyboard") == 0 && s->spec.role != ROLE_WINDOW && s->layer_surface) {
        JSCValue *v = jsc_value_object_get_property(value, "on");
        bool on = v && jsc_value_to_boolean(v);
        if (v) g_object_unref(v);
        /* ON_DEMAND hands the keyboard to a surface only when clicked -- fine
         * for a panel, wrong for a search box. EXCLUSIVE only while
         * something wants it: this surface is never destroyed, so leaving
         * it exclusive would starve every application for the session. Same
         * reasoning as nethos-view's LayerShell.KeyboardMode switch. */
        zwlr_layer_surface_v1_set_keyboard_interactivity(s->layer_surface,
            on ? ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE
               : ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_ON_DEMAND);
        /* Same race, same fix as "exclusive" above: menu.html (the one
         * spec'd with keyboard=on) calls nethosHost.keyboard() as early as
         * nethosHost.exclusive() does, and a raw commit here hit the exact
         * same fatal "never been configured" protocol error once the
         * exclusive-zone race above was closed -- confirmed live, this was
         * the second of two commit sites that needed the guard, not a
         * theoretical twin. */
        nethos_surface_repaint(s);
    } else if (strcmp(type, "repaint") == 0) {
        nethos_surface_repaint(s);
    } else if (strcmp(type, "hide") == 0) {
        s->visible = false;
        /* A real (fully transparent) painted frame, not a null-buffer
         * unmap -- see the long comment on nethos_surface_paint_blank() in
         * surface.c for why: unmapping and later remapping hits a fatal
         * Wayland protocol error under this Wayfire version, confirmed live
         * with WAYLAND_DEBUG=1, and payload/bin/nethos-view's own comments
         * say outright its overlay surface is deliberately never unmapped
         * for related reasons. This still gets what unmapping was for --
         * the compositor cannot ignore a real commit, so the region is
         * genuinely damaged to nothing rather than left showing a stale
         * frame (the "ghost" bug shell.js's own comments describe) -- and
         * s->configured never has to be touched, so there is no
         * reconfiguration to wait for on the way back. */
        wpe_view_backend_remove_activity_state(s->wpe_backend, wpe_view_activity_state_visible);
        nethos_surface_paint_blank(s);
    } else if (strcmp(type, "show") == 0) {
        s->visible = true;
        wpe_view_backend_add_activity_state(s->wpe_backend,
            wpe_view_activity_state_visible | wpe_view_activity_state_in_window);
        /* WPE suspends a hidden view's WebProcess (same reason WebKitGTK
         * does under GTK); marking it visible again should make WPE export
         * a fresh frame on its own next invalidation. nethos-view's GTK
         * port needed an extra dozen forced redraws here because GTK's own
         * frame clock does not always restart itself after being hidden --
         * an EGL/WPE-specific quirk, not necessarily one this path has, so
         * this is a smaller safety net rather than a straight copy: one
         * extra repaint request, not twelve. */
        nethos_surface_repaint(s);
    }
    free(type);
}

void nethos_bridge_install(struct nethos_surface *s) {
    WebKitUserContentManager *mgr = webkit_web_view_get_user_content_manager(s->webview);
    g_signal_connect(mgr, "script-message-received::nethosHost", G_CALLBACK(on_message), s);
    webkit_user_content_manager_register_script_message_handler(mgr, "nethosHost", NULL);

    char theme_buf[16];
    const char *theme = read_theme(theme_buf, sizeof(theme_buf));
    char name_js[160], theme_js[24];
    snprintf(name_js, sizeof(name_js), "\"%s\"", s->spec.name);
    if (*theme) snprintf(theme_js, sizeof(theme_js), "\"%s\"", theme);
    else snprintf(theme_js, sizeof(theme_js), "\"\"");

    char shim[2048];
    snprintf(shim, sizeof(shim),
        "window.nethosHost = {\n"
        "  exclusive: (n) => window.webkit.messageHandlers.nethosHost.postMessage({type:'exclusive', value:n}),\n"
        "  inputRect: (x,y,w,h) => window.webkit.messageHandlers.nethosHost.postMessage({type:'input_rect', x:x, y:y, w:w, h:h}),\n"
        "  hide: () => window.webkit.messageHandlers.nethosHost.postMessage({type:'hide'}),\n"
        "  show: () => window.webkit.messageHandlers.nethosHost.postMessage({type:'show'}),\n"
        "  repaint: () => window.webkit.messageHandlers.nethosHost.postMessage({type:'repaint'}),\n"
        "  keyboard: (on) => window.webkit.messageHandlers.nethosHost.postMessage({type:'keyboard', on: !!on}),\n"
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
}
