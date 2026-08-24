#!/bin/sh
# Build nethos-view-native and drop the binary into payload/bin/, the same
# place install-nethos.sh's install_desktop() already copies every
# payload/bin/nethos-* binary from with a plain `install -m 0755` -- that
# loop is binary-agnostic, so it needs no change to pick this up.
#
# Second architecture for this rewrite: GTK4 + WebKitGTK + gtk4-layer-shell,
# the same engine payload/bin/nethos-view (Python) already runs, instead of
# Phase 1's raw Wayland/EGL/WPE. No more wayland-scanner protocol generation
# at all -- GTK and gtk4-layer-shell own every bit of compositor-client
# protocol work themselves now. See docs/NETHOS-VIEW-REWRITE.md and
# nethos_view.h's own header comment for why.
#
#     payload/nethos-view-native/build.sh
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/../bin/nethos-view-native"

# gtk4-layer-shell-0 first: its own docs (linking.md) ask to be linked before
# libwayland when linking against libwayland directly, which this process no
# longer does explicitly -- GTK pulls it in transitively -- but keeping the
# package order matching that guidance costs nothing and stays correct if
# that ever changes.
PKGS="gtk4-layer-shell-0 webkitgtk-6.0 gtk4 glib-2.0"

CFLAGS="-std=c11 -Wall -Wextra -Wno-unused-parameter -O2 -g -I$ROOT/src $(pkg-config --cflags $PKGS)"
LIBS="$(pkg-config --libs $PKGS) -lpthread -rdynamic"

SRCS="$ROOT/src/main.c $ROOT/src/spec.c $ROOT/src/surface.c \
      $ROOT/src/bridge.c $ROOT/src/apphost.c $ROOT/src/events.c $ROOT/src/settle.c \
      $ROOT/src/log.c"

# shellcheck disable=SC2086
gcc $CFLAGS -o "$OUT" $SRCS $LIBS

echo "built: $OUT"
