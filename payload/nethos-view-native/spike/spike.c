/* Phase 0 spike: prove a WPE WebKit-rendered page can be presented into a
 * wlr-layer-shell-unstable-v1 surface under Wayfire, via raw wayland-client
 * + EGL, with no GTK involved at all.
 *
 * Pipeline: wl_compositor surface -> zwlr_layer_shell_v1 layer_surface
 * (anchored top, full width, exclusive zone reserved) -> wl_egl_window ->
 * our own EGL window surface for presentation. Separately, a WPE web view
 * renders into an *exported* EGL image via wpebackend-fdo's egl exportable
 * backend; each time one arrives we bind it as a GL texture
 * (GL_OES_EGL_image, glEGLImageTargetTexture2DOES) and blit it into our
 * window surface with a trivial textured-quad shader, then swap buffers and
 * hand the image back to WPE.
 *
 * Not production code -- no resize handling, no damage tracking beyond
 * "redraw the whole surface", no cleanup. Built and run by hand on the
 * laptop over SSH to answer one question: does this pipeline paint at all.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>
#include <wayland-egl.h>
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>

#include <glib.h>
#include <wpe/webkit.h>
#include <wpe/fdo.h>
#include <wpe/fdo-egl.h>

#include "wlr-layer-shell-unstable-v1-client-protocol.h"

static struct timespec t_start;
static double elapsed(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (now.tv_sec - t_start.tv_sec) + (now.tv_nsec - t_start.tv_nsec) / 1e9;
}
static void logts(const char *fmt, ...) {
    fprintf(stderr, "[%.3f] ", elapsed());
    va_list ap; va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap);
    fprintf(stderr, "\n"); fflush(stderr);
}

/* ---- Wayland globals ---- */
static struct wl_display *display;
static struct wl_registry *registry;
static struct wl_compositor *compositor;
static struct zwlr_layer_shell_v1 *layer_shell;
static struct wl_surface *surface;
static struct zwlr_layer_surface_v1 *layer_surface;

static const int SURFACE_W = 1200; /* fallback if compositor doesn't tell us */
static const int SURFACE_H = 40;
static int configured_w, configured_h;
static int configured = 0;

/* ---- EGL / presentation ---- */
static EGLDisplay egl_display;
static EGLContext egl_context;
static EGLConfig egl_config;
static EGLSurface egl_window_surface;
static struct wl_egl_window *egl_window;

static PFNGLEGLIMAGETARGETTEXTURE2DOESPROC eglImageTargetTexture2DOES;

static GLuint prog, vbo, tex;

/* ---- WPE ---- */
static struct wpe_view_backend_exportable_fdo *exportable;
static WebKitWebView *webview;

static const char *VS =
    "attribute vec2 pos;\n"
    "varying vec2 uv;\n"
    "void main() { uv = vec2((pos.x+1.0)*0.5, (1.0-pos.y)*0.5); gl_Position = vec4(pos, 0.0, 1.0); }\n";
static const char *FS =
    "precision mediump float;\n"
    "varying vec2 uv;\n"
    "uniform sampler2D tex;\n"
    "void main() { gl_FragColor = texture2D(tex, uv); }\n";

static GLuint compile(GLenum type, const char *src) {
    GLuint s = glCreateShader(type);
    glShaderSource(s, 1, &src, NULL);
    glCompileShader(s);
    GLint ok = 0;
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char buf[512];
        glGetShaderInfoLog(s, sizeof(buf), NULL, buf);
        logts("shader compile failed: %s", buf);
        exit(1);
    }
    return s;
}

static void gl_setup(void) {
    eglImageTargetTexture2DOES =
        (PFNGLEGLIMAGETARGETTEXTURE2DOESPROC)eglGetProcAddress("glEGLImageTargetTexture2DOES");
    if (!eglImageTargetTexture2DOES) { logts("no glEGLImageTargetTexture2DOES"); exit(1); }

    GLuint vs = compile(GL_VERTEX_SHADER, VS);
    GLuint fs = compile(GL_FRAGMENT_SHADER, FS);
    prog = glCreateProgram();
    glAttachShader(prog, vs); glAttachShader(prog, fs);
    glBindAttribLocation(prog, 0, "pos");
    glLinkProgram(prog);
    GLint ok = 0; glGetProgramiv(prog, GL_LINK_STATUS, &ok);
    if (!ok) { char buf[512]; glGetProgramInfoLog(prog, sizeof(buf), NULL, buf); logts("link failed: %s", buf); exit(1); }

    static const GLfloat quad[] = { -1,-1, 1,-1, -1,1, 1,1 };
    glGenBuffers(1, &vbo);
    glBindBuffer(GL_ARRAY_BUFFER, vbo);
    glBufferData(GL_ARRAY_BUFFER, sizeof(quad), quad, GL_STATIC_DRAW);

    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
}

static void draw_frame(EGLImageKHR image) {
    glViewport(0, 0, configured_w, configured_h);
    glClearColor(0.05f, 0.05f, 0.08f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);

    glBindTexture(GL_TEXTURE_2D, tex);
    eglImageTargetTexture2DOES(GL_TEXTURE_2D, image);

    glUseProgram(prog);
    glBindBuffer(GL_ARRAY_BUFFER, vbo);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, 0);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);

    eglSwapBuffers(egl_display, egl_window_surface);
}

/* ---- WPE export callbacks ---- */
static void on_export_fdo_egl_image(void *data, struct wpe_fdo_egl_exported_image *image) {
    if (!configured) {
        /* nowhere to present yet -- release immediately, don't stall WPE */
        wpe_view_backend_exportable_fdo_egl_dispatch_release_exported_image(exportable, image);
        return;
    }
    EGLImageKHR egl_image = wpe_fdo_egl_exported_image_get_egl_image(image);
    logts("export: got image %p", (void*)egl_image);
    draw_frame(egl_image);
    wpe_view_backend_exportable_fdo_egl_dispatch_release_exported_image(exportable, image);
}
static void on_export_shm(void *data, struct wpe_fdo_shm_exported_buffer *buffer) {
    logts("export: shm buffer (unhandled in this spike)");
    wpe_view_backend_exportable_fdo_egl_dispatch_release_shm_exported_buffer(exportable, buffer);
}
static void on_export_egl_image(void *data, EGLImageKHR image) {
    logts("export: bare egl image %p", (void*)image);
    if (configured) draw_frame(image);
    wpe_view_backend_exportable_fdo_egl_dispatch_release_image(exportable, image);
}
static const struct wpe_view_backend_exportable_fdo_egl_client egl_client = {
    .export_fdo_egl_image = on_export_fdo_egl_image,
    .export_shm_buffer = on_export_shm,
    .export_egl_image = on_export_egl_image,
};

static void on_load_changed(WebKitWebView *v, WebKitLoadEvent event, gpointer data) {
    if (event == WEBKIT_LOAD_FINISHED) logts("WEBKIT_LOAD_FINISHED");
}

static gboolean on_wl_readable(GIOChannel *c, GIOCondition cond, gpointer d) {
    wl_display_dispatch(display);
    return TRUE;
}
static gboolean on_flush_tick(gpointer d) {
    wl_display_flush(display);
    return TRUE;
}

/* ---- layer_surface listener ---- */
static void ls_configure(void *data, struct zwlr_layer_surface_v1 *ls, uint32_t serial, uint32_t w, uint32_t h) {
    configured_w = w ? (int)w : SURFACE_W;
    configured_h = h ? (int)h : SURFACE_H;
    logts("layer_surface configure: %dx%d serial=%u", configured_w, configured_h, serial);
    zwlr_layer_surface_v1_ack_configure(ls, serial);

    if (!egl_window) {
        egl_window = wl_egl_window_create(surface, configured_w, configured_h);
        egl_window_surface = eglCreateWindowSurface(egl_display, egl_config,
                                                      (EGLNativeWindowType)egl_window, NULL);
        if (!eglMakeCurrent(egl_display, egl_window_surface, egl_window_surface, egl_context)) {
            logts("eglMakeCurrent failed: 0x%x", eglGetError());
            exit(1);
        }
        gl_setup();
        /* paint one frame immediately so we're not blank while WPE warms up */
        glViewport(0, 0, configured_w, configured_h);
        glClearColor(0.8f, 0.1f, 0.1f, 1.0f); /* red = "surface up, no page yet" */
        glClear(GL_COLOR_BUFFER_BIT);
        eglSwapBuffers(egl_display, egl_window_surface);
    } else {
        wl_egl_window_resize(egl_window, configured_w, configured_h, 0, 0);
    }
    configured = 1;
}
static void ls_closed(void *data, struct zwlr_layer_surface_v1 *ls) {
    logts("layer_surface closed by compositor");
    exit(0);
}
static const struct zwlr_layer_surface_v1_listener ls_listener = { ls_configure, ls_closed };

/* ---- registry ---- */
static void reg_global(void *data, struct wl_registry *r, uint32_t name, const char *iface, uint32_t ver) {
    if (!strcmp(iface, wl_compositor_interface.name))
        compositor = wl_registry_bind(r, name, &wl_compositor_interface, 4);
    else if (!strcmp(iface, zwlr_layer_shell_v1_interface.name))
        layer_shell = wl_registry_bind(r, name, &zwlr_layer_shell_v1_interface, 1);
}
static void reg_remove(void *data, struct wl_registry *r, uint32_t name) {}
static const struct wl_registry_listener reg_listener = { reg_global, reg_remove };

int main(int argc, char **argv) {
    clock_gettime(CLOCK_MONOTONIC, &t_start);
    const char *url = argc > 1 ? argv[1] : "http://127.0.0.1:7777/apps/store/index.html";

    display = wl_display_connect(NULL);
    if (!display) { logts("wl_display_connect failed"); return 1; }
    registry = wl_display_get_registry(display);
    wl_registry_add_listener(registry, &reg_listener, NULL);
    wl_display_roundtrip(display);
    if (!compositor || !layer_shell) { logts("missing compositor or layer_shell global"); return 1; }
    logts("wayland globals bound");

    /* EGL, on the wayland display -- shared between our presentation surface
     * and the wpe fdo egl backend below. */
    egl_display = eglGetDisplay((EGLNativeDisplayType)display);
    EGLint maj, min;
    if (!eglInitialize(egl_display, &maj, &min)) { logts("eglInitialize failed: 0x%x", eglGetError()); return 1; }
    logts("EGL %d.%d", maj, min);
    eglBindAPI(EGL_OPENGL_ES_API);
    EGLint cfg_attribs[] = {
        EGL_SURFACE_TYPE, EGL_WINDOW_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
        EGL_NONE
    };
    EGLint n = 0;
    if (!eglChooseConfig(egl_display, cfg_attribs, &egl_config, 1, &n) || n < 1) {
        logts("eglChooseConfig failed"); return 1;
    }
    EGLint ctx_attribs[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
    egl_context = eglCreateContext(egl_display, egl_config, EGL_NO_CONTEXT, ctx_attribs);
    if (egl_context == EGL_NO_CONTEXT) { logts("eglCreateContext failed: 0x%x", eglGetError()); return 1; }
    logts("EGL context created");

    surface = wl_compositor_create_surface(compositor);
    layer_surface = zwlr_layer_shell_v1_get_layer_surface(
        layer_shell, surface, NULL, ZWLR_LAYER_SHELL_V1_LAYER_TOP, "nethos-spike");
    zwlr_layer_surface_v1_set_anchor(layer_surface,
        ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT);
    zwlr_layer_surface_v1_set_size(layer_surface, 0, SURFACE_H);
    zwlr_layer_surface_v1_set_exclusive_zone(layer_surface, SURFACE_H);
    zwlr_layer_surface_v1_add_listener(layer_surface, &ls_listener, NULL);
    wl_surface_commit(surface);
    logts("layer_surface created, waiting for configure");

    /* Now bring up WPE against the *same* EGL display. */
    if (!wpe_fdo_initialize_for_egl_display(egl_display)) {
        logts("wpe_fdo_initialize_for_egl_display failed"); return 1;
    }
    exportable = wpe_view_backend_exportable_fdo_egl_create(&egl_client, NULL, SURFACE_W, SURFACE_H);
    struct wpe_view_backend *wpe_backend = wpe_view_backend_exportable_fdo_get_view_backend(exportable);
    WebKitWebViewBackend *wk_backend = webkit_web_view_backend_new(wpe_backend, NULL, NULL);
    webview = WEBKIT_WEB_VIEW(g_object_new(WEBKIT_TYPE_WEB_VIEW, "backend", wk_backend, NULL));
    g_signal_connect(webview, "load-changed", G_CALLBACK(on_load_changed), NULL);
    logts("loading %s", url);
    webkit_web_view_load_uri(webview, url);

    /* Pump both the wayland fd and the glib main loop. wl_display_dispatch
     * blocks; run it on its own thread-free interleave via a glib IO watch
     * on the wayland fd instead, so WPE's own glib-driven timers still run. */
    GMainLoop *loop = g_main_loop_new(NULL, FALSE);
    GIOChannel *wl_chan = g_io_channel_unix_new(wl_display_get_fd(display));
    g_io_add_watch(wl_chan, G_IO_IN, on_wl_readable, NULL);
    /* also flush periodically so requests we make get sent even with no
     * incoming activity to wake the io watch */
    g_timeout_add(16, on_flush_tick, NULL);

    g_main_loop_run(loop);
    return 0;
}
