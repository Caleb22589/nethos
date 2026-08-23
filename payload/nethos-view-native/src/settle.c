/* Direct port of _settle_wait() in payload/bin/nethos-view. Surfaces
 * created in Wayfire's first moments come up permanently one frame behind
 * (closing the launcher shows the launcher, opening the control centre
 * shows what was there before it); waiting for the compositor's own uptime
 * rather than sleeping blindly means a fast machine waits less. Unchanged
 * from the Python version's reasoning -- see its own comment for the full
 * history of why this exists and why it stayed even after it stopped
 * moving the needle on total boot time.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <dirent.h>

#include "nethos_view.h"

static int find_wayfire_pid(void) {
    DIR *d = opendir("/proc");
    if (!d) return -1;
    struct dirent *ent;
    int pid = -1;
    while ((ent = readdir(d))) {
        if (ent->d_name[0] < '0' || ent->d_name[0] > '9') continue;
        char path[64];
        snprintf(path, sizeof(path), "/proc/%s/comm", ent->d_name);
        FILE *f = fopen(path, "r");
        if (!f) continue;
        char comm[64] = {0};
        if (fgets(comm, sizeof(comm), f)) {
            size_t n = strlen(comm);
            if (n && comm[n - 1] == '\n') comm[n - 1] = '\0';
            if (!strcmp(comm, "wayfire")) { pid = atoi(ent->d_name); fclose(f); break; }
        }
        fclose(f);
    }
    closedir(d);
    return pid;
}

static double proc_uptime(void) {
    FILE *f = fopen("/proc/uptime", "r");
    if (!f) return 0.0;
    double up = 0.0;
    if (fscanf(f, "%lf", &up) != 1) up = 0.0;
    fclose(f);
    return up;
}

/* Field 22 overall (starttime), field 20 (index 19) after splitting on the
 * *last* ')' -- comm can itself contain '(' or ')'. Same approach as the
 * Python version's stat.rsplit(")", 1). */
static long wayfire_start_ticks(int pid) {
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/stat", pid);
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    char buf[4096];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = '\0';
    char *close_paren = strrchr(buf, ')');
    if (!close_paren) return -1;
    char *rest = close_paren + 1;
    long fields[24];
    int count = 0;
    char *save = NULL;
    char *tok = strtok_r(rest, " ", &save);
    while (tok && count < 24) { fields[count++] = atol(tok); tok = strtok_r(NULL, " ", &save); }
    /* rest's first token is field 3 (state) overall, so index k here is
     * field (3+k). starttime is field 22 overall -> 3+k=22 -> k=19. Same
     * arithmetic as the Python version's fields[19], which comments itself
     * as "field 22 overall". */
    if (count < 20) return -1;
    return fields[19];
}

void nethos_settle_wait(void) {
    const char *settle_env = getenv("NETHOS_SETTLE");
    double settle = settle_env ? atof(settle_env) : 4.0;

    int pid = find_wayfire_pid();
    nethos_session_log("settle: wayfire pid=%s settle=%g", pid > 0 ? "found" : "none", settle);
    if (pid < 0) return;

    long start_ticks = wayfire_start_ticks(pid);
    if (start_ticks < 0) return;
    long hz = sysconf(_SC_CLK_TCK);
    if (hz <= 0) hz = 100;

    double up = proc_uptime() - (double)start_ticks / (double)hz;
    nethos_session_log("settle: compositor up %.1fs, waiting for %gs", up, settle);
    while (up < settle) {
        struct timespec ts = { 0, 200 * 1000 * 1000 };
        nanosleep(&ts, NULL);
        up = proc_uptime() - (double)start_ticks / (double)hz;
    }
    nethos_session_log("settle: done, compositor up %.1fs", up);
}
