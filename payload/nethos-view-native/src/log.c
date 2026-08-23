/* Same file and format as nethos-view's _session_log(), so nethos-session's
 * existing boot-time diagnostics keep working unchanged regardless of which
 * implementation wrote a given line. */
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <time.h>
#include <sys/stat.h>

#include "nethos_view.h"

void nethos_session_log(const char *fmt, ...) {
    const char *home = getenv("HOME");
    if (!home) return;
    char dir[1024], path[1088];
    snprintf(dir, sizeof(dir), "%s/.cache/nethos", home);
    mkdir(dir, 0755); /* ignore EEXIST and everything else, same as the Python os.makedirs */
    snprintf(path, sizeof(path), "%s/session.log", dir);

    FILE *f = fopen(path, "a");
    if (!f) return;

    time_t now = time(NULL);
    struct tm tm_now;
    localtime_r(&now, &tm_now);
    char ts[16];
    strftime(ts, sizeof(ts), "%H:%M:%S", &tm_now);

    va_list ap;
    va_start(ap, fmt);
    fprintf(f, "%s ", ts);
    vfprintf(f, fmt, ap);
    fprintf(f, "\n");
    va_end(ap);
    fclose(f);
}
