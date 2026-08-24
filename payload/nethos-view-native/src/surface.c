/* Per-surface creation, WPE/EGL wiring, and rendering. Direct behavioural
 * port of Surface.__init__ in payload/bin/nethos-view, restricted to the
 * Wayfire-only scope docs/NETHOS-VIEW-REWRITE.md settled on: no _own_chrome
 * (sway keeps the Python build for that), no minimize/maximize button
 * wiring (firedecor's own buttons already do this under Wayfire).
 *
 * role != window -> zwlr_layer_surface_v1, matching ROLE_DEFAULTS exactly.
 * role == window -> plain xdg_toplevel with an explicit SERVER_SIDE
 * decoration request -- the same thing GTK's decorated=TRUE actually sends
 * on the wire (see nethos-view lines 397-415), which is what lets
 * firedecor frame it.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "nethos_view.h"

static enum zwlr_layer_shell_v1_layer layer_from_string(const char *s) {
    if (!strcmp(s, "background")) return ZWLR_LAYER_SHELL_V1_LAYER_BACKGROUND;
    if (!strcmp(s, "bottom")) return ZWLR_LAYER_SHELL_V1_LAYER_BOTTOM;
    if (!strcmp(s, "overlay")) return ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY;
    return ZWLR_LAYER_SHELL_V1_LAYER_TOP;
}

/* ---- WPE SHM export -- see nethos_view.h for why this replaced the
 * dma-buf/EGLImage import path entirely. userdata is the owning
 * nethos_surface, so multiple surfaces can share the render path safely
 * (spike.c and the process-sharing diagnostic used file-scope globals
 * instead, fine for one or two surfaces, not for five-plus). ---- */
/* Paint one genuinely transparent frame and present it -- bridge.c's "hide"
 * calls this, instead of the null-buffer wl_surface_attach() an earlier
 * version of this fix used. That version was correct about needing to force
 * a real commit (see the comment on the !s->visible check below), but wrong
 * about how: a null-buffer attach fully *unmaps* the wl_surface, and
 * confirmed live with WAYLAND_DEBUG=1, Wayfire's zwlr_layer_shell_v1
 * implementation treats a subsequent real-buffer commit on a remapped
 * surface as needing a brand new configure/ack round-trip first -- one
 * neither re-issuing set_size() nor anything else this process tried
 * actually prompts Wayfire to send. Every surface that is ever hidden and
 * later shown again (menu/ask/control-center's whole open/close cycle, see
 * shell.js's overlayMapped()) hit this on its first hide, which is why
 * those looked like they simply never opened at all. payload/bin/nethos-view
 * avoids the whole question already -- its own comment on _repaint() says
 * outright that its overlay surface "is deliberately never unmapped" for
 * exactly this class of reason, even though GTK's set_visible(False) is
 * still what its own hide() calls; whatever GTK/gtk4-layer-shell does
 * internally to make that remap safe is not something this direct
 * libwayland client gets for free. Painting real (fully transparent)
 * content instead of unmapping gets the same result the Python build wants
 * -- a real commit that damages the region to nothing, satisfying "unmapping
 * damages" -- without ever leaving the "configured" state a remap would
 * need to re-earn. */
void nethos_surface_paint_blank(struct nethos_surface *s) {
    if (!s->configured) return;
    nethos_egl_make_current(s);
    glViewport(0, 0, s->configured_w, s->configured_h);
    glClearColor(0.0f, 0.0f, 0.0f, 0.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    eglSwapBuffers(g_egl_display, s->egl_surface);
}

static void render_shm(struct nethos_surface *s, struct wl_shm_buffer *buf) {
    /* !s->visible matters as much as !s->configured: WPE can still have one
     * frame already in flight -- queued for export before nethosHost.hide()
     * ran -- and land it here after the surface has already been painted
     * blank (see nethos_surface_paint_blank() above). Skipping it keeps
     * that deliberate blank frame on screen instead of a stale trailing one
     * silently overwriting it. */
    if (!s->configured || !s->visible || !buf) return;
    nethos_egl_make_current(s);

    wl_shm_buffer_begin_access(buf);
    void *data = wl_shm_buffer_get_data(buf);
    int32_t stride = wl_shm_buffer_get_stride(buf);
    int32_t bw = wl_shm_buffer_get_width(buf);
    int32_t bh = wl_shm_buffer_get_height(buf);

    /* wl_shm's ARGB8888/XRGB8888 are little-endian 32-bit words, i.e. byte
     * order B,G,R,A in memory -- GL_BGRA_EXT is the direct match, avoiding
     * a per-pixel channel swap on every single frame. */
    if (!s->tex) glGenTextures(1, &s->tex);
    glBindTexture(GL_TEXTURE_2D, s->tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    /* GLES2 core has no GL_UNPACK_ROW_LENGTH (that is GLES3/desktop GL); the
     * portable GLES2 equivalent is the GL_EXT_unpack_subimage extension's
     * _EXT-suffixed token, universally available on any driver recent
     * enough to be running WPE at all. */
    glPixelStorei(GL_UNPACK_ROW_LENGTH_EXT, stride / 4);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_BGRA_EXT, bw, bh, 0, GL_BGRA_EXT, GL_UNSIGNED_BYTE, data);
    glPixelStorei(GL_UNPACK_ROW_LENGTH_EXT, 0);
    wl_shm_buffer_end_access(buf);

    glViewport(0, 0, s->configured_w, s->configured_h);
    glClearColor(0.0f, 0.0f, 0.0f, 0.0f);
    glClear(GL_COLOR_BUFFER_BIT);

    glEnable(GL_BLEND);
    glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA); /* premultiplied, matches WPE's export */
    glUseProgram(g_gl_prog);
    glBindBuffer(GL_ARRAY_BUFFER, g_gl_vbo);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, 0);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    glDisable(GL_BLEND);

    eglSwapBuffers(g_egl_display, s->egl_surface);
}

/* export_buffer_resource/export_dmabuf_resource are the other two shapes
 * WPE's base (non-EGL) exportable can hand back a frame in; this backend
 * is configured SHM-only (wpe_fdo_initialize_shm(), see nethos_view.h) so
 * neither should ever actually fire. Not left silent if one does. */
static void on_export_buffer_resource(void *data, struct wl_resource *buffer_resource) {
    struct nethos_surface *s = data;
    fprintf(stderr, "nethos-view-native: '%s' got an unexpected generic buffer export "
            "(SHM-only backend) -- releasing unhandled\n", s->spec.name);
    wpe_view_backend_exportable_fdo_dispatch_release_buffer(s->exportable, buffer_resource);
}
static void on_export_dmabuf_resource(void *data, struct wpe_view_backend_exportable_fdo_dmabuf_resource *r) {
    struct nethos_surface *s = data;
    fprintf(stderr, "nethos-view-native: '%s' got an unexpected dma-buf export (%ux%u, "
            "SHM-only backend) -- unhandled\n", s->spec.name, r->width, r->height);
}
static void on_export_shm(void *data, struct wpe_fdo_shm_exported_buffer *buffer) {
    struct nethos_surface *s = data;
    render_shm(s, wpe_fdo_shm_exported_buffer_get_shm_buffer(buffer));
    /* Unconditional, same as the release call below, and for the same
     * reason: these are WPE's own frame-pipeline bookkeeping, not a
     * statement about whether *we* chose to visually present this
     * particular frame. Nesting them inside render_shm()'s early return
     * (skipped whenever a surface is hidden or not yet configured) meant a
     * frame exported while hidden -- e.g. a layer-shell overlay's normal
     * closed state -- was released but never acked, and WPE then withheld
     * every export after it forever, even once nethosHost.show() ran and
     * flipped s->visible back on: the menu/ask/control-center overlay
     * pattern (hidden by default, shown on demand, see shell.js's
     * overlayMapped()) hit this on literally its first hide, which is why
     * it looked like those surfaces simply never opened at all while the
     * dock and panel -- never hidden -- were unaffected. Two separate acks,
     * to two separate APIs (see view-backend-exportable.h): WPE withholds
     * the next export until BOTH have been sent, confirmed live. */
    wpe_view_backend_exportable_fdo_dispatch_frame_complete(s->exportable);
    wpe_view_backend_dispatch_frame_displayed(s->wpe_backend);
    wpe_view_backend_exportable_fdo_dispatch_release_shm_exported_buffer(s->exportable, buffer);
}

/* static/file-scope, not a local in nethos_surface_create(): WPE keeps this
 * pointer past that function's return (the per-surface "data" argument to
 * *_create() below is what varies per surface, this struct of function
 * pointers is not). A local copy here was a dangling-pointer bug that
 * crashed inside libWPEWebKit's own image-export path at first paint --
 * confirmed with a SIGSEGV backtrace, and confirmed innocent by re-running
 * the unmodified Phase 0 spike (which declares this the same way, `static
 * const`) against the same live session and the same page at the same
 * time, with no crash. */
static const struct wpe_view_backend_exportable_fdo_client s_client = {
    .export_buffer_resource = on_export_buffer_resource,
    .export_dmabuf_resource = on_export_dmabuf_resource,
    .export_shm_buffer = on_export_shm,
};

void nethos_surface_render(struct nethos_surface *s) { (void)s; /* driven by export callbacks only */ }

void nethos_surface_repaint(struct nethos_surface *s) {
    /* Force a commit even if WPE has nothing new to export -- the wlr-side
     * half of nethos-view's _repaint(): a pending set_input_region or
     * set_exclusive_zone request is only applied by the compositor on the
     * next wl_surface_commit, and a surface that stopped exporting frames
     * (WPE suspends an invisible one) would otherwise never send one. */
    if (!s->configured) return;
    wl_surface_commit(s->wl_surface);
    wl_display_flush(g_display);
}

/* Tear down everything a closed window was holding: nothing here was ever
 * released before, so every window ever opened -- closed or not -- kept
 * its EGL surface, its GL texture and its Wayland objects for the whole
 * life of the process. On this laptop's old i915 GPU that is a real,
 * finite resource (very likely GEM/dma-buf import slots): opening enough
 * windows in a row made *new* windows render nothing at all -- every EGL
 * and GL call along the way reported success, with a valid non-null
 * EGLImage each time, which is consistent with the *import* succeeding at
 * the API level while the underlying memory never actually maps, giving a
 * texture that reads as transparent zeros rather than a GL error. */
void nethos_surface_destroy(struct nethos_surface *s) {
    for (int i = 0; i < g_surface_count; i++) {
        if (g_surfaces[i] == s) {
            for (int j = i; j < g_surface_count - 1; j++) g_surfaces[j] = g_surfaces[j + 1];
            g_surfaces[--g_surface_count] = NULL;
            break;
        }
    }
    /* If this happened to be the anchor every other view's related-view
     * points at (only possible if it's the very first surface ever
     * created, i.e. a shell surface -- those are never actually closed in
     * normal operation), later surfaces just start a fresh unrelated
     * WebProcess instead of crashing; picking a new anchor from whichever
     * surfaces remain is not worth the bookkeeping for a case this rare. */
    if (g_related_view == s->webview) g_related_view = NULL;

    if (s->tex) glDeleteTextures(1, &s->tex);
    if (s->egl_surface != EGL_NO_SURFACE) eglDestroySurface(g_egl_display, s->egl_surface);
    if (s->egl_window) wl_egl_window_destroy(s->egl_window);

    if (s->webview) g_object_unref(s->webview);
    /* No explicit wpe_view_backend_exportable_fdo_destroy() call: WebKit's
     * own WebKitWebViewBackend takes ownership of the wpe_view_backend at
     * webkit_web_view_backend_new() time and destroys it (and the
     * exportable underneath, via the destroy() in its backend interface)
     * as part of tearing down the view -- calling it again here would be a
     * double free. */

    if (s->decoration) zxdg_toplevel_decoration_v1_destroy(s->decoration);
    if (s->xdg_toplevel) xdg_toplevel_destroy(s->xdg_toplevel);
    if (s->xdg_surface) xdg_surface_destroy(s->xdg_surface);
    if (s->layer_surface) zwlr_layer_surface_v1_destroy(s->layer_surface);
    if (s->wl_surface) wl_surface_destroy(s->wl_surface);

    wl_display_flush(g_display);
    free(s);
}

/* ---- surface size / EGL plumbing shared by both role paths ---- */
static void ensure_egl_surface(struct nethos_surface *s, int w, int h) {
    if (w <= 0) w = s->spec.width > 0 ? s->spec.width : 800;
    if (h <= 0) h = s->spec.height > 0 ? s->spec.height : 600;
    s->configured_w = w;
    s->configured_h = h;

    if (!s->egl_window) {
        s->egl_window = wl_egl_window_create(s->wl_surface, w, h);
        s->egl_surface = eglCreateWindowSurface(g_egl_display, g_egl_config,
            (EGLNativeWindowType)s->egl_window, NULL);
        if (!eglMakeCurrent(g_egl_display, s->egl_surface, s->egl_surface, g_egl_context))
            fprintf(stderr, "nethos-view-native: '%s' eglMakeCurrent failed: 0x%x\n",
                    s->spec.name, eglGetError());
        /* Without this, eglSwapBuffers returns immediately instead of
         * blocking for the next vblank -- confirmed live: once render_shm
         * started dispatching frame_complete/frame_displayed right after an
         * unthrottled swap (see render_shm), WebKit had nothing pacing it
         * and repainted as fast as the CPU allowed (96%+ CPU, WPEWebProcess
         * RSS climbing continuously, and the screen showing a stuck void
         * colour instead of any surface content -- Wayfire's own compositor
         * falling behind an unbounded commit rate looks identical to "the
         * client never painted anything" from a screenshot, which is what
         * made this easy to mistake for a repeat of the original bug).
         * eglSwapInterval is a property of the EGL *surface*, not the
         * context, so it has to be set here per-surface, once, right after
         * this surface's own eglMakeCurrent. */
        eglSwapInterval(g_egl_display, 1);
        nethos_gl_setup();
    } else {
        wl_egl_window_resize(s->egl_window, w, h, 0, 0);
    }
    wpe_view_backend_dispatch_set_size(s->wpe_backend, (uint32_t)w, (uint32_t)h);
    s->configured = true;
}

/* ---- layer-shell path ---- */
static void ls_configure(void *data, struct zwlr_layer_surface_v1 *ls, uint32_t serial,
                          uint32_t w, uint32_t h) {
    struct nethos_surface *s = data;
    zwlr_layer_surface_v1_ack_configure(ls, serial);
    /* Wayfire sends an initial (0,0) "you decide" configure on some anchor
     * combinations before the real one follows a moment later. Acting on it
     * unconditionally is harmless in itself, but there is no useful size to
     * configure EGL/WPE for yet -- wait for the first real, nonzero one. */
    if (w == 0 && h == 0) return;
    ensure_egl_surface(s, (int)w, (int)h);

    /* "auto" exclusive zone: gtk4-layer-shell's auto_exclusive_zone_enable()
     * tracks size continuously; nethos-session's actual launch line always
     * passes an explicit exclusive=N for every surface, so "auto" is only
     * reachable from a hand-built spec missing exclusive= entirely. A
     * single set at first configure (not continuously re-tracked) matches
     * that real-world usage without the extra bookkeeping a rarely/never
     * exercised path doesn't earn. */
    if (s->spec.exclusive_auto) {
        bool vertical = !strcmp(s->spec.anchor, "top") || !strcmp(s->spec.anchor, "bottom");
        zwlr_layer_surface_v1_set_exclusive_zone(ls, vertical ? s->configured_h : s->configured_w);
    }
}
static void ls_closed(void *data, struct zwlr_layer_surface_v1 *ls) {
    /* Compositor is asking this surface to go away. Shell surfaces should
     * never see this in normal operation; if one does, losing that one
     * surface is a smaller failure than taking the whole host down with
     * it -- matching this program's own top-level goal of never leaving a
     * machine with literally no shell. */
    struct nethos_surface *s = data;
    fprintf(stderr, "nethos-view-native: layer surface '%s' closed by compositor\n", s->spec.name);
    s->configured = false;
}
static const struct zwlr_layer_surface_v1_listener ls_listener = { ls_configure, ls_closed };

static void create_layer_surface(struct nethos_surface *s) {
    const struct nethos_spec *spec = &s->spec;
    char ns[160];
    snprintf(ns, sizeof(ns), "nethos-%s", spec->name);

    s->layer_surface = zwlr_layer_shell_v1_get_layer_surface(
        g_layer_shell, s->wl_surface, NULL, layer_from_string(spec->layer), ns);
    zwlr_layer_surface_v1_add_listener(s->layer_surface, &ls_listener, s);

    bool fullscreen = !strcmp(spec->anchor, "all")
        || spec->role == ROLE_OVERLAY || spec->role == ROLE_WIDGET;
    if (fullscreen) {
        zwlr_layer_surface_v1_set_anchor(s->layer_surface,
            ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM |
            ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT);
    } else if (!strcmp(spec->anchor, "top")) {
        zwlr_layer_surface_v1_set_anchor(s->layer_surface,
            ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT |
            ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT);
    } else if (!strcmp(spec->anchor, "bottom")) {
        zwlr_layer_surface_v1_set_anchor(s->layer_surface,
            ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT |
            ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT);
    } else if (!strcmp(spec->anchor, "left")) {
        zwlr_layer_surface_v1_set_anchor(s->layer_surface,
            ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP |
            ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM);
    } else if (!strcmp(spec->anchor, "right")) {
        zwlr_layer_surface_v1_set_anchor(s->layer_surface,
            ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT | ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP |
            ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM);
    }

    if (!spec->exclusive_auto)
        zwlr_layer_surface_v1_set_exclusive_zone(s->layer_surface, spec->exclusive);
    /* exclusive_auto is finished once the first configure lands -- see
     * ls_configure. */

    if (!spec->keyboard_off)
        zwlr_layer_surface_v1_set_keyboard_interactivity(
            s->layer_surface, ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_ON_DEMAND);

    if (!fullscreen && strcmp(spec->anchor, "all") != 0 && (spec->width > 0 || spec->height > 0))
        zwlr_layer_surface_v1_set_size(s->layer_surface,
            spec->width > 0 ? (uint32_t)spec->width : 0,
            spec->height > 0 ? (uint32_t)spec->height : 0);

    wl_surface_commit(s->wl_surface);
}

/* ---- xdg_toplevel path (role == window) ---- */
static int s_pending_toplevel_w, s_pending_toplevel_h;

static void xt_configure(void *data, struct xdg_toplevel *t, int32_t w, int32_t h, struct wl_array *states) {
    s_pending_toplevel_w = w;
    s_pending_toplevel_h = h;
}
static void xt_close(void *data, struct xdg_toplevel *t) {
    struct nethos_surface *s = data;
    /* nethosd is not told -- it only ever tracks these windows by
     * swaymsg/wayfire IPC (see list_windows()), same as the Python build
     * for a window-role surface; this process just stops presenting it. */
    nethos_surface_destroy(s);
}
static const struct xdg_toplevel_listener toplevel_listener = { xt_configure, xt_close };

static void xs_configure(void *data, struct xdg_surface *xs, uint32_t serial) {
    struct nethos_surface *s = data;
    xdg_surface_ack_configure(xs, serial);
    ensure_egl_surface(s, s_pending_toplevel_w, s_pending_toplevel_h);
}
static const struct xdg_surface_listener xdg_surface_listener_impl = { xs_configure };

static void create_toplevel_surface(struct nethos_surface *s) {
    s->xdg_surface = xdg_wm_base_get_xdg_surface(g_xdg_wm_base, s->wl_surface);
    xdg_surface_add_listener(s->xdg_surface, &xdg_surface_listener_impl, s);
    s->xdg_toplevel = xdg_surface_get_toplevel(s->xdg_surface);
    xdg_toplevel_add_listener(s->xdg_toplevel, &toplevel_listener, s);
    xdg_toplevel_set_app_id(s->xdg_toplevel, s->spec.name);
    xdg_toplevel_set_title(s->xdg_toplevel, s->spec.title);

    /* The explicit SERVER_SIDE request GTK's decorated=TRUE actually sends
     * -- absence of a request is not equivalent to this on Wayfire, see the
     * comment this mirrors in nethos-view lines 397-415. */
    if (g_decoration_manager) {
        s->decoration = zxdg_decoration_manager_v1_get_toplevel_decoration(
            g_decoration_manager, s->xdg_toplevel);
        zxdg_toplevel_decoration_v1_set_mode(s->decoration, ZXDG_TOPLEVEL_DECORATION_V1_MODE_SERVER_SIDE);
    }

    wl_surface_commit(s->wl_surface);
}

/* ---- WebKit setup, shared by both role paths ---- */
static void apply_settings(WebKitWebView *view) {
    WebKitSettings *settings = webkit_web_view_get_settings(view);
    webkit_settings_set_enable_developer_extras(settings, TRUE);
    webkit_settings_set_enable_write_console_messages_to_stdout(settings, TRUE);
    webkit_settings_set_enable_media(settings, FALSE);
    webkit_settings_set_enable_webaudio(settings, FALSE);
    webkit_settings_set_enable_webgl(settings, FALSE);
    webkit_settings_set_media_playback_requires_user_gesture(settings, TRUE);
    webkit_settings_set_enable_page_cache(settings, FALSE);
    webkit_settings_set_enable_html5_database(settings, FALSE);
    webkit_settings_set_enable_html5_local_storage(settings, TRUE);
    webkit_settings_set_javascript_can_open_windows_automatically(settings, FALSE);
    /* No enable_back_forward_navigation_gestures in WPE's WebKitSettings --
     * that is a GTK touchpad-gesture feature with nothing to hook into
     * here, not an oversight. */
}

static gboolean on_context_menu(WebKitWebView *v, WebKitContextMenu *menu, gpointer d) {
    return getenv("NETHOS_INSPECTOR") && !strcmp(getenv("NETHOS_INSPECTOR"), "1") ? FALSE : TRUE;
}

/* Not silent about a failed load -- a blank surface with no explanation
 * anywhere is exactly the kind of failure this project's own conventions
 * (docs/HANDOFF.md) warn costs a debugging session each time it happens. */
static gboolean on_load_failed(WebKitWebView *v, WebKitLoadEvent e, gchar *uri, GError *err, gpointer d) {
    struct nethos_surface *s = d;
    fprintf(stderr, "nethos-view-native: '%s' failed to load %s: %s\n", s->spec.name, uri, err->message);
    return FALSE;
}

struct nethos_surface *nethos_surface_create(const struct nethos_spec *spec) {
    if (g_surface_count >= NETHOS_MAX_SURFACES) {
        fprintf(stderr, "nethos-view-native: too many surfaces, dropping %s\n", spec->name);
        return NULL;
    }
    struct nethos_surface *s = calloc(1, sizeof(*s));
    s->spec = *spec;
    s->visible = true;
    s->wl_surface = wl_compositor_create_surface(g_compositor);

    int init_w = spec->width > 0 ? spec->width : 800;
    int init_h = spec->height > 0 ? spec->height : 600;
    s->exportable = wpe_view_backend_exportable_fdo_create(&s_client, s, init_w, init_h);
    s->wpe_backend = wpe_view_backend_exportable_fdo_get_view_backend(s->exportable);
    /* Without this, WPE exports exactly one frame (its very first paint)
     * and then suspends the page -- matching this project's own documented
     * "WebKit suspends a background/never-focused surface" behaviour (see
     * nethos-view's App._tick()/_events() comments), which the Python
     * build works around at the GTK widget level (queue_draw()) with no
     * equivalent here. A previous attempt at this exact call, at this
     * exact place, appeared to cause a crash -- but the real cause, found
     * afterward, was an unrelated dangling-pointer bug in the callback
     * struct below (now `static const`); this is believed safe now and is
     * being tried again on that basis. */
    wpe_view_backend_add_activity_state(s->wpe_backend,
        wpe_view_activity_state_visible | wpe_view_activity_state_focused |
        wpe_view_activity_state_in_window);

    WebKitWebViewBackend *wk_backend = webkit_web_view_backend_new(s->wpe_backend, NULL, NULL);
    bool share = true;
    const char *share_env = getenv("NETHOS_SHARE_WEBPROCESS");
    if (share_env && !strcmp(share_env, "0")) share = false;

    if (share && g_related_view) {
        s->webview = WEBKIT_WEB_VIEW(g_object_new(WEBKIT_TYPE_WEB_VIEW,
            "backend", wk_backend, "related-view", g_related_view, NULL));
    } else {
        s->webview = WEBKIT_WEB_VIEW(g_object_new(WEBKIT_TYPE_WEB_VIEW, "backend", wk_backend, NULL));
        if (!g_related_view) g_related_view = s->webview;
    }

    apply_settings(s->webview);
    g_signal_connect(s->webview, "context-menu", G_CALLBACK(on_context_menu), NULL);
    g_signal_connect(s->webview, "load-failed", G_CALLBACK(on_load_failed), s);

    if (spec->transparent) {
        WebKitColor transparent = { 0.0, 0.0, 0.0, 0.0 };
        webkit_web_view_set_background_color(s->webview, &transparent);
    }

    nethos_bridge_install(s);

    if (spec->role == ROLE_WINDOW) create_toplevel_surface(s);
    else create_layer_surface(s);

    webkit_web_view_load_uri(s->webview, spec->url);

    g_surfaces[g_surface_count++] = s;
    return s;
}
