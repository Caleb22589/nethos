#!/bin/bash
# Watch an image build as it happens.
#
#     scripts/watch-build.sh            follow the current build
#     scripts/watch-build.sh --raw      everything, including console noise
#     scripts/watch-build.sh --status   one-shot summary, then exit
#
# The builder's console carries kernel messages, systemd output and UEFI
# firmware complaints alongside the build itself. By default this shows only
# the build's own lines plus anything that looks like a failure.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="${NETHOS_BUILD_LOG:-$ROOT/build/build-image.log}"
[ -f "$LOG" ] || LOG=/tmp/imgbuild.log

MODE=follow
case "${1:-}" in
    --raw) MODE=raw ;;
    --status) MODE=status ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \?//'; exit 0 ;;
esac

[ -f "$LOG" ] || { echo "no build log found (looked in $ROOT/build and /tmp)"; exit 1; }

# The build's own markers, plus real failures. Deliberately not a catch-all:
# the console prints "error" a lot during a perfectly healthy boot.
KEEP='cloud-init\[[0-9]+\]: (---|===|index:|  [0-9]+/[0-9]+|  installed|  note:|  skipped|kernel modules|grub menu|FATAL)|^==>|Built:|Traceback|TypeError|No such file'

status() {
    printf '\033[1mbuilder:\033[0m '
    if pgrep -f qemu-system-aarch64 >/dev/null; then
        cpu=$(ps -eo %cpu,comm | grep qemu-system-aarch64 | grep -v grep \
              | awk '{s+=$1} END {printf "%.0f", s}')
        up=$(ps -eo etime,comm | grep qemu-system-aarch64 | grep -v grep \
             | awk '{print $1}' | head -1)
        if [ "${cpu:-0}" -lt 3 ]; then
            printf 'running but \033[1;33midle\033[0m (%s%% cpu, up %s) — possible stall\n' "$cpu" "$up"
        else
            printf '\033[1;32mworking\033[0m (%s%% cpu, up %s)\n' "$cpu" "$up"
        fi
    else
        printf 'not running\n'
    fi

    img="$ROOT/build/nethos-arm.qcow2"
    [ -f "$img" ] && printf '\033[1mimage:\033[0m   %s\n' "$(du -h "$img" | cut -f1)"
    printf '\033[1mlog:\033[0m     %s\n\n' "$LOG"
    tr -d '\r' < "$LOG" | grep -aE "$KEEP" | tail -12 | cut -c1-160
}

case "$MODE" in
    status) status ;;
    raw)    tail -f "$LOG" ;;
    follow)
        status
        printf '\n\033[2m— following (ctrl-c to stop) —\033[0m\n'
        # --line-buffered or grep sits on its buffer and shows nothing live.
        tail -f -n0 "$LOG" | tr -d '\r' | grep -aE --line-buffered "$KEEP" | cut -c1-160
        ;;
esac
