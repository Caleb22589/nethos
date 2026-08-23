/* Registry binding, EGL bring-up shared across every surface, and wl_seat
 * input routing -- the part payload/bin/nethos-view never had to write
 * because GTK did it. wl_pointer/wl_keyboard enter/leave track which
 * nethos_surface currently has focus; every subsequent event is translated
 * and forwarded into that surface's wpe_view_backend via
 * wpe_view_backend_dispatch_*. Verified end to end on the laptop (see
 * docs/NETHOS-VIEW-REWRITE.md): a synthetic button dispatch through this
 * same API reached a real onclick handler.
 */
#include <stdio.h>
#include <string.h>
#include <linux/input-event-codes.h>
#include <sys/mman.h>
#include <unistd.h>

#include <xkbcommon/xkbcommon.h>
#include <wpe/wpe.h>

#include "nethos_view.h"

struct wl_display *g_display;
static struct wl_registry *g_registry;
struct wl_compositor *g_compositor;
struct zwlr_layer_shell_v1 *g_layer_shell;
struct xdg_wm_base *g_xdg_wm_base;
struct zxdg_decoration_manager_v1 *g_decoration_manager;
struct wl_seat *g_seat;

EGLDisplay g_egl_display;
EGLContext g_egl_context;
EGLConfig g_egl_config;

GLuint g_gl_prog, g_gl_vbo;
static bool g_gl_ready;

struct nethos_surface *g_surfaces[NETHOS_MAX_SURFACES];
int g_surface_count;
WebKitWebView *g_related_view;

/* ---- surface lookup for input routing ---- */
static struct nethos_surface *find_by_wl_surface(struct wl_surface *ws) {
    for (int i = 0; i < g_surface_count; i++)
        if (g_surfaces[i] && g_surfaces[i]->wl_surface == ws) return g_surfaces[i];
    return NULL;
}

/* ---- pointer ---- */
static struct wl_pointer *g_pointer;
static struct nethos_surface *g_pointer_focus;
static double g_ptr_x, g_ptr_y;

static void ptr_enter(void *d, struct wl_pointer *p, uint32_t serial,
                       struct wl_surface *ws, wl_fixed_t x, wl_fixed_t y) {
    g_pointer_focus = find_by_wl_surface(ws);
    g_ptr_x = wl_fixed_to_double(x);
    g_ptr_y = wl_fixed_to_double(y);
}
static void ptr_leave(void *d, struct wl_pointer *p, uint32_t serial, struct wl_surface *ws) {
    g_pointer_focus = NULL;
}
static void ptr_motion(void *d, struct wl_pointer *p, uint32_t time, wl_fixed_t x, wl_fixed_t y) {
    g_ptr_x = wl_fixed_to_double(x);
    g_ptr_y = wl_fixed_to_double(y);
    if (!g_pointer_focus || !g_pointer_focus->wpe_backend) return;
    struct wpe_input_pointer_event ev = {
        .type = wpe_input_pointer_event_type_motion, .time = time,
        .x = (int)g_ptr_x, .y = (int)g_ptr_y, .button = 0, .state = 0, .modifiers = 0,
    };
    wpe_view_backend_dispatch_pointer_event(g_pointer_focus->wpe_backend, &ev);
}
static void ptr_button(void *d, struct wl_pointer *p, uint32_t serial, uint32_t time,
                        uint32_t button, uint32_t state) {
    if (!g_pointer_focus || !g_pointer_focus->wpe_backend) return;
    /* WPE's button numbering is 1=left/2=middle/3=right (DOM's 0/1/2 + 1);
     * evdev's BTN_LEFT/RIGHT/MIDDLE are 0x110/0x111/0x112. */
    uint32_t wpe_button = button == BTN_LEFT ? 1 : button == BTN_MIDDLE ? 2
                         : button == BTN_RIGHT ? 3 : 0;
    struct wpe_input_pointer_event ev = {
        .type = wpe_input_pointer_event_type_button, .time = time,
        .x = (int)g_ptr_x, .y = (int)g_ptr_y, .button = wpe_button,
        .state = state, .modifiers = 0,
    };
    wpe_view_backend_dispatch_pointer_event(g_pointer_focus->wpe_backend, &ev);
}
static void ptr_axis(void *d, struct wl_pointer *p, uint32_t time, uint32_t axis, wl_fixed_t value) {
    if (!g_pointer_focus || !g_pointer_focus->wpe_backend) return;
    struct wpe_input_axis_event ev = {
        .type = wpe_input_axis_event_type_motion, .time = time,
        .x = (int)g_ptr_x, .y = (int)g_ptr_y,
        .axis = axis, .value = -wl_fixed_to_int(value), .modifiers = 0,
    };
    wpe_view_backend_dispatch_axis_event(g_pointer_focus->wpe_backend, &ev);
}
static void ptr_frame(void *d, struct wl_pointer *p) {}
static void ptr_axis_source(void *d, struct wl_pointer *p, uint32_t s) {}
static void ptr_axis_stop(void *d, struct wl_pointer *p, uint32_t t, uint32_t a) {}
static void ptr_axis_discrete(void *d, struct wl_pointer *p, uint32_t a, int32_t disc) {}
static const struct wl_pointer_listener pointer_listener = {
    ptr_enter, ptr_leave, ptr_motion, ptr_button, ptr_axis, ptr_frame,
    ptr_axis_source, ptr_axis_stop, ptr_axis_discrete,
};

/* ---- keyboard ---- */
static struct wl_keyboard *g_keyboard;
static struct nethos_surface *g_keyboard_focus;
static struct wpe_input_xkb_context *g_xkb_ctx;

static void kb_keymap(void *d, struct wl_keyboard *k, uint32_t format, int fd, uint32_t size) {
    if (format != WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1) { close(fd); return; }
    char *map = mmap(NULL, size, PROT_READ, MAP_PRIVATE, fd, 0);
    if (map == MAP_FAILED) { close(fd); return; }
    g_xkb_ctx = wpe_input_xkb_context_get_default();
    struct xkb_context *xc = wpe_input_xkb_context_get_context(g_xkb_ctx);
    struct xkb_keymap *km = xkb_keymap_new_from_string(
        xc, map, XKB_KEYMAP_FORMAT_TEXT_V1, XKB_KEYMAP_COMPILE_NO_FLAGS);
    munmap(map, size);
    close(fd);
    if (km) wpe_input_xkb_context_set_keymap(g_xkb_ctx, km);
}
static void kb_enter(void *d, struct wl_keyboard *k, uint32_t serial,
                      struct wl_surface *ws, struct wl_array *keys) {
    g_keyboard_focus = find_by_wl_surface(ws);
}
static void kb_leave(void *d, struct wl_keyboard *k, uint32_t serial, struct wl_surface *ws) {
    g_keyboard_focus = NULL;
}
static void kb_key(void *d, struct wl_keyboard *k, uint32_t serial, uint32_t time,
                    uint32_t key, uint32_t state) {
    if (!g_xkb_ctx || !g_keyboard_focus || !g_keyboard_focus->wpe_backend) return;
    bool pressed = state == WL_KEYBOARD_KEY_STATE_PRESSED;
    uint32_t hw = key + 8; /* evdev keycode -> xkb keycode offset, same as any wlroots client */
    uint32_t key_code = wpe_input_xkb_context_get_key_code(g_xkb_ctx, hw, pressed);
    uint32_t mods = wpe_input_xkb_context_get_modifiers(g_xkb_ctx, 0, 0, 0, 0);
    struct wpe_input_keyboard_event ev = {
        .time = time, .key_code = key_code, .hardware_key_code = hw,
        .pressed = pressed, .modifiers = mods,
    };
    wpe_view_backend_dispatch_keyboard_event(g_keyboard_focus->wpe_backend, &ev);
}
static void kb_modifiers(void *d, struct wl_keyboard *k, uint32_t serial,
                          uint32_t dep, uint32_t lat, uint32_t lock, uint32_t group) {
    if (!g_xkb_ctx) return;
    struct xkb_state *st = wpe_input_xkb_context_get_state(g_xkb_ctx);
    if (st) xkb_state_update_mask(st, dep, lat, lock, 0, 0, group);
}
static void kb_repeat_info(void *d, struct wl_keyboard *k, int32_t rate, int32_t delay) {}
static const struct wl_keyboard_listener keyboard_listener = {
    kb_keymap, kb_enter, kb_leave, kb_key, kb_modifiers, kb_repeat_info,
};

static void seat_capabilities(void *d, struct wl_seat *s, uint32_t caps) {
    if ((caps & WL_SEAT_CAPABILITY_POINTER) && !g_pointer) {
        g_pointer = wl_seat_get_pointer(s);
        wl_pointer_add_listener(g_pointer, &pointer_listener, NULL);
    }
    if ((caps & WL_SEAT_CAPABILITY_KEYBOARD) && !g_keyboard) {
        g_keyboard = wl_seat_get_keyboard(s);
        wl_keyboard_add_listener(g_keyboard, &keyboard_listener, NULL);
    }
}
static void seat_name(void *d, struct wl_seat *s, const char *name) {}
static const struct wl_seat_listener seat_listener = { seat_capabilities, seat_name };

/* ---- xdg_wm_base ping/pong -- required or Wayfire kills the client as
 * unresponsive the first time it pings. ---- */
static void wm_base_ping(void *d, struct xdg_wm_base *wm, uint32_t serial) {
    xdg_wm_base_pong(wm, serial);
}
static const struct xdg_wm_base_listener wm_base_listener = { wm_base_ping };

/* ---- registry ---- */
static void reg_global(void *d, struct wl_registry *r, uint32_t name,
                        const char *iface, uint32_t ver) {
    if (!strcmp(iface, wl_compositor_interface.name)) {
        g_compositor = wl_registry_bind(r, name, &wl_compositor_interface, 4);
    } else if (!strcmp(iface, zwlr_layer_shell_v1_interface.name)) {
        g_layer_shell = wl_registry_bind(r, name, &zwlr_layer_shell_v1_interface, 4);
    } else if (!strcmp(iface, xdg_wm_base_interface.name)) {
        g_xdg_wm_base = wl_registry_bind(r, name, &xdg_wm_base_interface, 1);
        xdg_wm_base_add_listener(g_xdg_wm_base, &wm_base_listener, NULL);
    } else if (!strcmp(iface, zxdg_decoration_manager_v1_interface.name)) {
        g_decoration_manager = wl_registry_bind(r, name, &zxdg_decoration_manager_v1_interface, 1);
    } else if (!strcmp(iface, wl_seat_interface.name)) {
        g_seat = wl_registry_bind(r, name, &wl_seat_interface, ver < 7 ? ver : 7);
        wl_seat_add_listener(g_seat, &seat_listener, NULL);
    }
}
static void reg_remove(void *d, struct wl_registry *r, uint32_t name) {}
static const struct wl_registry_listener reg_listener = { reg_global, reg_remove };

int nethos_wayland_init(void) {
    g_display = wl_display_connect(NULL);
    if (!g_display) { fprintf(stderr, "nethos-view-native: wl_display_connect failed\n"); return -1; }

    g_registry = wl_display_get_registry(g_display);
    wl_registry_add_listener(g_registry, &reg_listener, NULL);
    wl_display_roundtrip(g_display);

    if (!g_compositor || !g_layer_shell || !g_xdg_wm_base) {
        fprintf(stderr, "nethos-view-native: missing a required Wayland global "
                "(compositor=%p layer_shell=%p xdg_wm_base=%p) -- is this really Wayfire?\n",
                (void*)g_compositor, (void*)g_layer_shell, (void*)g_xdg_wm_base);
        return -1;
    }
    if (!g_seat) fprintf(stderr, "nethos-view-native: no wl_seat -- nothing will be clickable\n");
    if (!g_decoration_manager)
        fprintf(stderr, "nethos-view-native: no xdg-decoration manager -- window-role "
                "surfaces will not get firedecor's frame\n");

    g_egl_display = eglGetDisplay((EGLNativeDisplayType)g_display);
    EGLint maj, min;
    if (!eglInitialize(g_egl_display, &maj, &min)) {
        fprintf(stderr, "nethos-view-native: eglInitialize failed: 0x%x\n", eglGetError());
        return -1;
    }
    eglBindAPI(EGL_OPENGL_ES_API);
    EGLint cfg_attribs[] = {
        EGL_SURFACE_TYPE, EGL_WINDOW_BIT, EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE,
    };
    EGLint n = 0;
    if (!eglChooseConfig(g_egl_display, cfg_attribs, &g_egl_config, 1, &n) || n < 1) {
        fprintf(stderr, "nethos-view-native: eglChooseConfig failed\n");
        return -1;
    }
    EGLint ctx_attribs[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
    g_egl_context = eglCreateContext(g_egl_display, g_egl_config, EGL_NO_CONTEXT, ctx_attribs);
    if (g_egl_context == EGL_NO_CONTEXT) {
        fprintf(stderr, "nethos-view-native: eglCreateContext failed: 0x%x\n", eglGetError());
        return -1;
    }
    return 0;
}

void nethos_egl_make_current(struct nethos_surface *s) {
    eglMakeCurrent(g_egl_display, s->egl_surface, s->egl_surface, g_egl_context);
}

static GLuint compile(GLenum type, const char *src) {
    GLuint sh = glCreateShader(type);
    glShaderSource(sh, 1, &src, NULL);
    glCompileShader(sh);
    GLint ok = 0;
    glGetShaderiv(sh, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char buf[512];
        glGetShaderInfoLog(sh, sizeof(buf), NULL, buf);
        fprintf(stderr, "nethos-view-native: shader compile failed: %s\n", buf);
    }
    return sh;
}

void nethos_gl_setup(void) {
    if (g_gl_ready) return;
    g_gl_ready = true;
    static const char *VS =
        "attribute vec2 pos;\nvarying vec2 uv;\n"
        "void main(){uv=vec2((pos.x+1.0)*0.5,(1.0-pos.y)*0.5);gl_Position=vec4(pos,0.0,1.0);}\n";
    static const char *FS =
        "precision mediump float;\nvarying vec2 uv;\nuniform sampler2D tex;\n"
        "void main(){gl_FragColor=texture2D(tex,uv);}\n";
    GLuint vs = compile(GL_VERTEX_SHADER, VS), fs = compile(GL_FRAGMENT_SHADER, FS);
    g_gl_prog = glCreateProgram();
    glAttachShader(g_gl_prog, vs);
    glAttachShader(g_gl_prog, fs);
    glBindAttribLocation(g_gl_prog, 0, "pos");
    glLinkProgram(g_gl_prog);
    GLint link_ok = 0;
    glGetProgramiv(g_gl_prog, GL_LINK_STATUS, &link_ok);
    if (!link_ok) {
        char buf[512];
        glGetProgramInfoLog(g_gl_prog, sizeof(buf), NULL, buf);
        fprintf(stderr, "nethos-view-native: shader LINK FAILED: %s\n", buf);
    }
    static const GLfloat quad[] = { -1, -1, 1, -1, -1, 1, 1, 1 };
    glGenBuffers(1, &g_gl_vbo);
    glBindBuffer(GL_ARRAY_BUFFER, g_gl_vbo);
    glBufferData(GL_ARRAY_BUFFER, sizeof(quad), quad, GL_STATIC_DRAW);
}
