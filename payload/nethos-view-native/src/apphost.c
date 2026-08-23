/* The --apphost Unix socket, same framing as apphost_socket_path()/
 * _apphost_listen() in payload/bin/nethos-view: one spec string per
 * connection, client writes then half-closes, empty string is a liveness
 * probe only. nethosd hardcodes this same path independently
 * ($XDG_RUNTIME_DIR/nethos-apphost.sock) and is not being changed --
 * apphost_send()/ensure_apphost() in nethosd.py talk to this unmodified.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <errno.h>

#include <glib.h>

#include "nethos_view.h"

char *nethos_apphost_socket_path(void) {
    const char *runtime = getenv("XDG_RUNTIME_DIR");
    char base[1024];
    if (runtime && *runtime) {
        snprintf(base, sizeof(base), "%s", runtime);
    } else {
        const char *home = getenv("HOME");
        snprintf(base, sizeof(base), "%s/.cache/nethos", home ? home : "");
    }
    char *path = malloc(1088);
    snprintf(path, 1088, "%s/nethos-apphost.sock", base);
    return path;
}

static gboolean open_spec_idle(gpointer data) {
    char *text = data;
    struct nethos_spec spec;
    if (nethos_parse_spec(text, &spec)) {
        nethos_surface_create(&spec);
    } else {
        fprintf(stderr, "nethos-view-native: apphost failed to parse %s\n", text);
    }
    free(text);
    return FALSE;
}

static void *apphost_listen_thread(void *arg) {
    char *path = arg;

    char dir[1088];
    snprintf(dir, sizeof(dir), "%s", path);
    char *slash = strrchr(dir, '/');
    if (slash) { *slash = '\0'; mkdir(dir, 0755); }

    unlink(path);

    int sock = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sock < 0) { perror("nethos-view-native: apphost socket()"); return NULL; }
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path);
    if (bind(sock, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        perror("nethos-view-native: apphost bind()");
        close(sock);
        return NULL;
    }
    if (listen(sock, 8) < 0) { perror("nethos-view-native: apphost listen()"); close(sock); return NULL; }

    for (;;) {
        int conn = accept(sock, NULL, NULL);
        if (conn < 0) { if (errno == EINTR) continue; break; }

        struct timeval tv = { .tv_sec = 2, .tv_usec = 0 };
        setsockopt(conn, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

        char buf[NETHOS_MAX_SPEC_LEN];
        size_t total = 0;
        for (;;) {
            ssize_t n = recv(conn, buf + total, sizeof(buf) - 1 - total, 0);
            if (n <= 0) break;
            total += (size_t)n;
            if (total >= sizeof(buf) - 1) break;
        }
        buf[total] = '\0';
        close(conn);

        /* Trim trailing whitespace, matching Python's .strip(). */
        while (total > 0 && (buf[total - 1] == '\n' || buf[total - 1] == '\r'
                              || buf[total - 1] == ' ')) buf[--total] = '\0';

        if (total > 0) {
            g_idle_add(open_spec_idle, g_strdup(buf));
        } /* empty = liveness probe only, matching the Python version */
    }
    close(sock);
    return NULL;
}

void nethos_apphost_start(void) {
    char *path = nethos_apphost_socket_path();
    pthread_t tid;
    pthread_create(&tid, NULL, apphost_listen_thread, path);
    pthread_detach(tid);
    /* `path` is freed by the thread's own lifetime -- intentionally leaked
     * here for the process's lifetime rather than tracked across a detached
     * thread boundary; this process only ever creates one apphost thread. */
}
