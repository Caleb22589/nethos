#!/bin/bash
# Build the NETHOS live ISO inside an Arch Linux container.
#
# archiso only runs on Arch, but the container makes that irrelevant: this
# works on any x86_64 Linux (Ubuntu, Fedora, Debian, Mint…), on Windows via
# WSL2, and on an Arch box that simply has not got archiso installed.
#
#     ./scripts/build-iso-container.sh
#
# The ISO lands in ./out. Run it on real x86_64 hardware — building on an
# emulated CPU is what made this slow in the first place.
#
# Options:
#   --engine docker|podman   force a container engine (default: autodetect)
#   --compress zstd|xz       squashfs compression (default zstd, faster)
#   --out DIR                output directory (default ./out)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE=""
COMPRESS=zstd
OUT="$REPO/out"

while [ $# -gt 0 ]; do
    case "$1" in
        --engine) ENGINE="${2:?}"; shift 2 ;;
        --compress) COMPRESS="${2:?}"; shift 2 ;;
        --out) OUT="${2:?}"; shift 2 ;;
        -h|--help) sed -n '2,18p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# sanity
# --------------------------------------------------------------------------
ARCH="$(uname -m)"
if [ "$ARCH" != "x86_64" ] && [ "$ARCH" != "amd64" ]; then
    cat >&2 <<EOF
WARNING: this machine is $ARCH, not x86_64.

The container will run x86_64 under emulation, which is exactly the slowness
you are trying to escape. Run this on the x86 PC instead.
EOF
    printf 'Continue anyway? [y/N] '
    read -r reply
    case "$reply" in y|Y) ;; *) exit 1 ;; esac
fi

if [ -z "$ENGINE" ]; then
    if command -v podman >/dev/null; then ENGINE=podman
    elif command -v docker >/dev/null; then ENGINE=docker
    else
        die "neither podman nor docker found.
  Ubuntu/Debian : sudo apt install podman     (or docker.io)
  Fedora        : sudo dnf install podman
  Arch          : sudo pacman -S podman       (or just: sudo pacman -S archiso)
  Windows       : install WSL2, then run this inside it"
    fi
fi
command -v "$ENGINE" >/dev/null || die "$ENGINE not found"

say "Using $ENGINE on $ARCH"

# mkarchiso needs loop devices and real mounts, so the container has to be
# privileged. That is a genuine grant of host kernel access -- it is why this
# script only ever runs the official Arch image and this repository's own
# build script, and nothing downloaded at runtime.
PRIV=(--privileged)

mkdir -p "$OUT"

# Podman maps the host user into the container by default; Docker runs as root
# and would leave root-owned output behind, so fix ownership afterwards.
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

say "Building — pacstrap plus squashfs, a few minutes on real hardware"

"$ENGINE" run --rm "${PRIV[@]}" \
    -v "$REPO":/nethos \
    -v "$OUT":/out \
    -w /nethos \
    docker.io/library/archlinux:latest \
    bash -euo pipefail -c "
        echo '==> Refreshing package database'
        pacman -Sy --noconfirm --needed archiso rsync git >/dev/null

        echo '==> Running the NETHOS ISO build'
        /nethos/iso/build-iso.sh --out /out --compress $COMPRESS

        # Hand the artefacts back to the invoking user rather than root.
        chown -R $HOST_UID:$HOST_GID /out 2>/dev/null || true
    "

ISO="$(ls -t "$OUT"/*.iso 2>/dev/null | head -1)"
[ -n "$ISO" ] || die "build finished but no ISO appeared in $OUT"

say "Built: $ISO ($(du -h "$ISO" | cut -f1))"
echo
echo "Flash it to a USB stick (this erases the stick):"
echo "  lsblk                                     # identify it carefully"
echo "  sudo dd if=$ISO of=/dev/sdX bs=4M status=progress oflag=sync"
echo
