#!/bin/bash
# Build the NETHOS VM image on the host (macOS).
#
#   1. copy the official Arch cloud image into a working disk and grow it
#   2. bake the cloud-init seed + NETHOS payload into an ISO
#
# Re-runnable: pass --clean to throw away the existing disk and start over.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
BASE="$BUILD/Arch-Linux-x86_64-cloudimg.qcow2"
DISK="$BUILD/nethos.qcow2"
SEED="$BUILD/seed.iso"
DISK_SIZE="${DISK_SIZE:-40G}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "$BASE" ] || die "base image missing: $BASE
Download it with:
  curl -fL -o '$BASE' https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2"

command -v qemu-img >/dev/null || die "qemu-img not found (brew install qemu)"

if [ "${1:-}" = "--clean" ]; then
    say "Removing previous disk"
    rm -f "$DISK"
fi

# ---------------------------------------------------------------- disk ----
if [ -f "$DISK" ]; then
    say "Disk already exists, keeping it: $DISK  (use --clean to rebuild)"
else
    say "Creating working disk from the official Arch cloud image"
    qemu-img convert -O qcow2 "$BASE" "$DISK"
    qemu-img resize "$DISK" "$DISK_SIZE"
    say "Disk ready: $DISK (virtual $DISK_SIZE)"
fi

# ---------------------------------------------------------------- seed ----
say "Staging the cloud-init seed and NETHOS payload"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/nethos-seed.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

cp "$ROOT/cloud-init/user-data" "$STAGE/user-data"
cp "$ROOT/cloud-init/meta-data" "$STAGE/meta-data"
mkdir -p "$STAGE/payload"
cp -R "$ROOT/payload/." "$STAGE/payload/"

# macOS ships no xorriso; hdiutil makes a Joliet ISO9660 image, which is what
# cloud-init's NoCloud datasource looks for (volume label CIDATA).
rm -f "$SEED"
hdiutil makehybrid -quiet -iso -joliet \
    -default-volume-name CIDATA \
    -o "$SEED" "$STAGE"

say "Seed ISO: $SEED ($(du -h "$SEED" | cut -f1))"
say "Build complete. Start the VM with:  scripts/run.sh"
