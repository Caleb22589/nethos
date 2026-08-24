#!/bin/sh
# Build nethos-view-native and drop the binary into payload/bin/, the same
# place install-nethos.sh's install_desktop() already copies every
# payload/bin/nethos-* binary from with a plain `install -m 0755` -- that
# loop is binary-agnostic, so it needs no change to pick this up.
#
# Protocol code is generated here rather than committed, the way any other
# wayland-scanner-based project does it; only the source XMLs in protocols/
# are vendored (wlr-layer-shell-unstable-v1.xml is not packaged for Debian
# trixie at all, so it has to live in this repo either way -- see
# docs/NETHOS-VIEW-REWRITE.md).
#
#     payload/nethos-view-native/build.sh
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
BUILD="$ROOT/build"
OUT="$ROOT/../bin/nethos-view-native"

mkdir -p "$BUILD"

gen() {
    name="$1"; xml="$2"
    wayland-scanner client-header "$xml" "$BUILD/$name-client-protocol.h"
    wayland-scanner private-code  "$xml" "$BUILD/$name-protocol.c"
}
gen wlr-layer-shell-unstable-v1 "$ROOT/protocols/wlr-layer-shell-unstable-v1.xml"
gen xdg-shell "$ROOT/protocols/xdg-shell.xml"
gen xdg-decoration-unstable-v1 "$ROOT/protocols/xdg-decoration-unstable-v1.xml"

PKGS="wayland-client wayland-egl egl glesv2 wpe-webkit-2.0 wpe-1.0 wpebackend-fdo-1.0 glib-2.0 xkbcommon"

CFLAGS="-std=c11 -Wall -Wextra -Wno-unused-parameter -O2 -g -I$BUILD -I$ROOT/src $(pkg-config --cflags $PKGS)"
LIBS="$(pkg-config --libs $PKGS) -lpthread -rdynamic"

SRCS="$ROOT/src/main.c $ROOT/src/spec.c $ROOT/src/wayland.c $ROOT/src/surface.c \
      $ROOT/src/bridge.c $ROOT/src/apphost.c $ROOT/src/events.c $ROOT/src/settle.c \
      $ROOT/src/log.c \
      $BUILD/wlr-layer-shell-unstable-v1-protocol.c $BUILD/xdg-shell-protocol.c \
      $BUILD/xdg-decoration-unstable-v1-protocol.c"

# shellcheck disable=SC2086
gcc $CFLAGS -o "$OUT" $SRCS $LIBS

echo "built: $OUT"
