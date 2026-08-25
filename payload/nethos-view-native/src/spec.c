/* Spec grammar: bare positional key=value,key=value strings, direct port of
 * parse_spec()/ROLE_DEFAULTS in payload/bin/nethos-view. Same field names,
 * same "role default only fills in what the spec didn't say" behaviour.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "nethos_view.h"

#define MAX_KV 32

struct kv { char key[32]; char value[1024]; };

static const char *kv_find(struct kv *pairs, int n, const char *key) {
    for (int i = 0; i < n; i++)
        if (strcmp(pairs[i].key, key) == 0) return pairs[i].value;
    return NULL;
}

static enum nethos_role role_from_string(const char *s) {
    if (!s) return ROLE_WINDOW;
    if (strcmp(s, "panel") == 0) return ROLE_PANEL;
    if (strcmp(s, "dock") == 0) return ROLE_DOCK;
    if (strcmp(s, "overlay") == 0) return ROLE_OVERLAY;
    if (strcmp(s, "widget") == 0) return ROLE_WIDGET;
    return ROLE_WINDOW;
}

/* ROLE_DEFAULTS from nethos-view: layer/anchor/exclusive per role, applied
 * only where the spec text itself did not set them. */
static void apply_role_defaults(enum nethos_role role, const char **layer,
                                 const char **anchor, const char **exclusive) {
    switch (role) {
    case ROLE_PANEL:
        if (!*layer) *layer = "top";
        if (!*anchor) *anchor = "top";
        if (!*exclusive) *exclusive = "auto";
        break;
    case ROLE_DOCK:
        if (!*layer) *layer = "top";
        if (!*anchor) *anchor = "bottom";
        if (!*exclusive) *exclusive = "0";
        break;
    case ROLE_OVERLAY:
        if (!*layer) *layer = "overlay";
        if (!*exclusive) *exclusive = "0";
        break;
    case ROLE_WIDGET:
        if (!*layer) *layer = "bottom";
        if (!*exclusive) *exclusive = "0";
        break;
    case ROLE_WINDOW:
        if (!*exclusive) *exclusive = "0";
        break;
    }
}

bool nethos_parse_spec(const char *text, struct nethos_spec *out) {
    memset(out, 0, sizeof(*out));
    out->transparent = true; /* spec.get("transparent", "1") not in (0,false,no) */
    out->webgl = false;      /* opt-in per surface; see surface.c */

    struct kv pairs[MAX_KV];
    int n = 0;

    char buf[NETHOS_MAX_SPEC_LEN];
    snprintf(buf, sizeof(buf), "%s", text);

    char *save = NULL;
    char *part = strtok_r(buf, ",", &save);
    while (part && n < MAX_KV) {
        while (*part == ' ') part++;
        if (*part == '\0') { part = strtok_r(NULL, ",", &save); continue; }
        char *eq = strchr(part, '=');
        if (eq) {
            size_t klen = (size_t)(eq - part);
            if (klen >= sizeof(pairs[n].key)) klen = sizeof(pairs[n].key) - 1;
            memcpy(pairs[n].key, part, klen);
            pairs[n].key[klen] = '\0';
            snprintf(pairs[n].value, sizeof(pairs[n].value), "%s", eq + 1);
        } else {
            snprintf(pairs[n].key, sizeof(pairs[n].key), "%s", part);
            pairs[n].value[0] = '\0';
        }
        n++;
        part = strtok_r(NULL, ",", &save);
    }

    const char *url = kv_find(pairs, n, "url");
    if (!url || !*url) return false; /* "surface spec needs url=" */
    snprintf(out->url, sizeof(out->url), "%s", url);

    out->role = role_from_string(kv_find(pairs, n, "role"));

    const char *layer = kv_find(pairs, n, "layer");
    const char *anchor = kv_find(pairs, n, "anchor");
    const char *exclusive = kv_find(pairs, n, "exclusive");
    apply_role_defaults(out->role, &layer, &anchor, &exclusive);

    snprintf(out->layer, sizeof(out->layer), "%s", layer ? layer : "top");
    snprintf(out->anchor, sizeof(out->anchor), "%s", anchor ? anchor : "");

    if (exclusive && strcmp(exclusive, "auto") == 0) {
        out->exclusive_auto = true;
        out->exclusive = 0;
    } else {
        out->exclusive = exclusive ? atoi(exclusive) : 0;
    }

    const char *name = kv_find(pairs, n, "name");
    const char *role_name = out->role == ROLE_PANEL ? "panel" : out->role == ROLE_DOCK ? "dock"
        : out->role == ROLE_OVERLAY ? "overlay" : out->role == ROLE_WIDGET ? "widget" : "window";
    snprintf(out->name, sizeof(out->name), "%s", (name && *name) ? name : role_name);

    const char *title = kv_find(pairs, n, "title");
    snprintf(out->title, sizeof(out->title), "%s", title ? title : out->name);

    /* 0 means "not given" here, not a real size -- surface.c applies the
     * 800x600 xdg_toplevel default only for role=window (matching
     * set_default_size's default), and only sets a fixed layer-shell size
     * when the spec actually asked for one (matching spec.get("height")'s
     * truthiness check, which a defaulted value would defeat). */
    const char *width = kv_find(pairs, n, "width");
    const char *height = kv_find(pairs, n, "height");
    out->width = width ? atoi(width) : 0;
    out->height = height ? atoi(height) : 0;

    const char *transparent = kv_find(pairs, n, "transparent");
    if (transparent && (strcmp(transparent, "0") == 0 || strcmp(transparent, "false") == 0
                         || strcmp(transparent, "no") == 0))
        out->transparent = false;

    const char *webgl = kv_find(pairs, n, "webgl");
    out->webgl = webgl && (strcmp(webgl, "1") == 0 || strcmp(webgl, "true") == 0
                            || strcmp(webgl, "yes") == 0);

    const char *keyboard = kv_find(pairs, n, "keyboard");
    out->keyboard_off = keyboard && strcmp(keyboard, "off") == 0;

    return true;
}
