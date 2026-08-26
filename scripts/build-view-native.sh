#!/bin/bash
# Build nethos-view-native for x86_64 and publish it as a GitHub release asset.
#
#   scripts/build-view-native.sh              build only, leave it in build/
#   scripts/build-view-native.sh --publish    ...and upload it
#
# Built inside a debian:trixie container, not on this Mac: the binary links
# against gtk4-layer-shell, webkitgtk-6.0 and gtk4, and has to match the glibc
# and library ABI the target actually runs -- which is real Debian trixie
# (npkg converts trixie's own .debs 1:1), not whatever Homebrew has. The -dev
# headers come from apt directly rather than npkg's own repository: npkg's
# repo carries runtime libraries for installed systems, not the -dev packages
# a compiler needs, and a plain debian:trixie container has the real archive.
#
# Published as a release asset rather than committed to the repo, the same
# reasoning as the kernel in publish-repo.sh/release.sh: a compiled binary is
# a reproducible build artifact tied to one ABI, not source, and committing it
# would bloat history with something scripts/build-view-native.sh regenerates
# on demand. One rolling tag, not one per commit -- there is no separate
# "version" for this the way the kernel has 7.2.0; nethos-install always wants
# whatever the source most recently built to, matching the same
# always-fetch-current philosophy nethos-install's payload/pkg refresh uses.
set -euo pipefail

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$HERE/build"
OUT="$BUILD/nethos-view-native-x86_64"
TAG="nethos-view-native-x86_64"
PUBLISH=0
[ "${1:-}" = "--publish" ] && PUBLISH=1

command -v docker >/dev/null || die "docker (colima) is needed"
mkdir -p "$BUILD"

say "Building in debian:trixie"
# --platform linux/amd64 is not optional here, unlike the other build
# scripts: they only extract prebuilt .deb data (architecture-agnostic --
# dpkg-deb -x does not execute the package's own code), but this one runs
# gcc, and a container that defaults to the host's own architecture (this is
# usually run on an Apple Silicon Mac) silently produces an aarch64 binary
# that merely happens to link against aarch64 copies of the same libraries,
# with no error anywhere -- `file` on the result is the only thing that
# catches it, which is how this was actually found.
docker run --rm --platform linux/amd64 -v "$HERE":/nethos:ro -v "$BUILD":/out \
    -w /work debian:trixie bash -euo pipefail -c '
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq gcc pkg-config \
    libgtk-4-dev libgtk4-layer-shell-dev libwebkitgtk-6.0-dev >/dev/null
cp -R /nethos/payload/nethos-view-native /work/src
mkdir -p /work/bin
sh /work/src/build.sh
cp /work/src/../bin/nethos-view-native /out/nethos-view-native-x86_64
strip /out/nethos-view-native-x86_64
'
[ -f "$OUT" ] || die "no binary produced"
chmod 755 "$OUT"
sha256sum "$OUT" | awk '{print $1}' > "$OUT.sha256"
say "Built: $OUT ($(du -h "$OUT" | cut -f1))"

if [ "$PUBLISH" -eq 1 ]; then
    command -v gh >/dev/null || die "gh is not installed"
    gh auth status >/dev/null 2>&1 || die "gh is not logged in"
    say "Publishing to release $TAG"
    if gh release view "$TAG" >/dev/null 2>&1; then
        gh release upload "$TAG" "$OUT" "$OUT.sha256" --clobber
    else
        gh release create "$TAG" "$OUT" "$OUT.sha256" \
            --title "nethos-view-native (x86_64)" \
            --notes "The C/GTK4/WebKitGTK rewrite of nethos-view, built from whatever commit last ran this script. Rolling release, not versioned -- see docs/NETHOS-VIEW-REWRITE.md. Fetched automatically by nethos-install; falls back to the Python nethos-view if unreachable."
    fi
    say "Published: $(gh release view "$TAG" --json url -q .url)"
fi
