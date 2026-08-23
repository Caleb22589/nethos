/* Tick/events fan-out -- direct port of App._tick()/_events()/_deliver() in
 * payload/bin/nethos-view. WebKit suspends a layer-shell surface's
 * WebProcess once it decides nothing is watching it (confirmed real,
 * documented at length in the Python version), so a page's own
 * setInterval/EventSource cannot be trusted; this process holds the one
 * connection and one timer and wakes every surface from the outside.
 *
 * The SSE client is hand-rolled rather than linked against libcurl: it is
 * not installed on the laptop, and the request itself is about as simple as
 * HTTP gets -- one fixed local plaintext endpoint, no redirects, no auth,
 * nethosd's own handler (http.server.BaseHTTPRequestHandler, unchunked)
 * just keeps the connection open and keeps writing "data: ...\n\n" lines.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <pthread.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include <glib.h>

#include "nethos_view.h"

static gboolean tick_cb(gpointer data) {
    for (int i = 0; i < g_surface_count; i++) {
        struct nethos_surface *s = g_surfaces[i];
        if (s && s->webview)
            webkit_web_view_evaluate_javascript(s->webview,
                "window.nethosTick && window.nethosTick();", -1, NULL, NULL, NULL, NULL, NULL);
    }
    return TRUE; /* keep ticking */
}

static gboolean deliver_idle(gpointer data) {
    char *payload = data;
    char script[NETHOS_MAX_SPEC_LEN + 64];
    snprintf(script, sizeof(script), "window.nethosEvent && window.nethosEvent(%s);", payload);
    for (int i = 0; i < g_surface_count; i++) {
        struct nethos_surface *s = g_surfaces[i];
        if (s && s->webview)
            webkit_web_view_evaluate_javascript(s->webview, script, -1, NULL, NULL, NULL, NULL, NULL);
    }
    free(payload);
    return FALSE;
}

static int connect_events(const char *host, int port) {
    int sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) return -1;
    struct sockaddr_in addr = { .sin_family = AF_INET, .sin_port = htons((uint16_t)port) };
    inet_pton(AF_INET, host, &addr.sin_addr);
    if (connect(sock, (struct sockaddr*)&addr, sizeof(addr)) < 0) { close(sock); return -1; }
    char req[256];
    snprintf(req, sizeof(req),
        "GET /api/events HTTP/1.1\r\nHost: %s:%d\r\nConnection: keep-alive\r\n\r\n", host, port);
    if (send(sock, req, strlen(req), 0) < 0) { close(sock); return -1; }
    return sock;
}

static void *events_thread(void *arg) {
    (void)arg;
    for (;;) {
        int sock = connect_events("127.0.0.1", 7777);
        if (sock < 0) { sleep(2); continue; }

        char buf[8192];
        size_t len = 0;
        bool in_body = false;

        for (;;) {
            if (len >= sizeof(buf) - 1) len = 0; /* runaway line, drop and resync */
            ssize_t n = recv(sock, buf + len, sizeof(buf) - 1 - len, 0);
            if (n <= 0) break;
            len += (size_t)n;
            buf[len] = '\0';

            char *line_start = buf;
            for (;;) {
                char *nl = memchr(line_start, '\n', len - (size_t)(line_start - buf));
                if (!nl) break;
                *nl = '\0';
                char *line = line_start;
                size_t linelen = strlen(line);
                if (linelen && line[linelen - 1] == '\r') line[linelen - 1] = '\0';

                if (!in_body) {
                    if (line[0] == '\0') in_body = true; /* blank line ends the headers */
                } else if (!strncmp(line, "data:", 5)) {
                    const char *payload = line + 5;
                    while (*payload == ' ') payload++;
                    if (*payload) g_idle_add(deliver_idle, g_strdup(payload));
                }
                line_start = nl + 1;
            }
            size_t consumed = (size_t)(line_start - buf);
            memmove(buf, line_start, len - consumed);
            len -= consumed;
        }
        close(sock);
        sleep(2); /* nethosd restarts; matches the Python version's backoff */
    }
    return NULL;
}

void nethos_events_start(void) {
    g_timeout_add(1000, tick_cb, NULL);
    pthread_t tid;
    pthread_create(&tid, NULL, events_thread, NULL);
    pthread_detach(tid);
}
